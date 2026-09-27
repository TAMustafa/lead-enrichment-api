import hashlib
import json
import time

from .address import canonicalize_address
from .deduplication import Signals
from .errors import Busy
from .models import ExistingEntity, LeadResponse, Metadata, Status
from .qualification import qualify
from .resolution import Resolution


class LeadService:
    def __init__(
        self,
        repository,
        resolver,
        deduplicator,
        google,
        mapper,
        salesforce,
        settings,
        residential_checker=None,
    ):
        self.residential_checker = residential_checker
        self.repository, self.resolver, self.deduplicator = repository, resolver, deduplicator
        self.google, self.mapper, self.salesforce, self.settings = google, mapper, salesforce, settings

    async def process(self, lead, request_id, observer=None):
        def emit(stage, state, detail, data=None):
            if observer:
                observer({"stage": stage, "state": state, "detail": detail, "data": data})

        start = time.monotonic()
        async with self.repository.session(lead) as session:
            emit("identity", "running", "Checking source identity and idempotency")
            row = await session.identity(lead, self.repository.fingerprint(lead))
            emit("identity", "done", "Internal lead identity established", {"lead_id": str(row["lead_id"])})
            if row["result"]:
                emit("replay", "done", "Returning the stored decision; no provider writes")
                result = LeadResponse.model_validate_json(row["result"])
                result.processing_metadata.idempotent_replay = True
                result.processing_metadata.request_id = request_id
                result.processing_metadata.duration_ms = int((time.monotonic() - start) * 1000)
                return result
            emit("resolution", "running", "Resolving the business identity")
            resolution = (
                Resolution(row["place_id"], row["resolution_confidence"], "PLACE_ID_REUSED")
                if row["place_id"]
                else await self.resolver.resolve(lead)
            )
            emit(
                "resolution",
                "done",
                resolution.reason,
                {"place_id": resolution.place_id, "confidence": resolution.confidence},
            )
            response = LeadResponse(
                business_name=lead.business_name,
                normalized_business_name=Signals.from_lead(lead, resolution.place_id).name,
                normalized_address=canonicalize_address(lead.address),
                lead_id=row["lead_id"],
                source=lead.source,
                source_lead_id=lead.source_lead_id,
                place_id=resolution.place_id,
                duplicate_status="UNCHECKED",
                qualification_status=Status.REVIEW_REQUIRED,
                qualification_reason_codes=[resolution.reason],
                confidence=resolution.confidence,
                processing_metadata=Metadata(
                    request_id=request_id, rules_version=self.settings.rules_version
                ),
            )

            async def finish():
                response.processing_metadata.duration_ms = int((time.monotonic() - start) * 1000)
                await session.finish(response)
                return response

            if not resolution.place_id:
                return await finish()
            signals = Signals.from_lead(lead, resolution.place_id)
            # Acquire a global ordered set of locks: Place ID and first-party secondary identities.
            locks = [
                "place:" + signals.place_id,
                "name_address:" + json.dumps([signals.name, signals.address]),
            ]
            if signals.phone:
                locks.append("phone:" + signals.phone)
            signal_keys = self.repository.signal_keys([v for v in locks if not v.startswith("place:")])
            for lock in sorted(locks):
                await session.lock(lock)
            await session.set_place(row["lead_id"], resolution.place_id, resolution.confidence)
            intent = await session.intent(resolution.place_id)
            own_key = hashlib.sha256(("place:" + resolution.place_id).encode()).hexdigest()
            if intent and intent["lead_id"] != row["lead_id"]:
                if intent["state"] != "WRITTEN":
                    raise Busy("Prior write outcome is unresolved; retry after reconciliation")
                # Still query all required sources before returning; no enrichment is necessary.
                own_key = "not-owner"
            secondary_claims = await session.secondary_claims(signal_keys, row["lead_id"])
            if any(claim["state"] != "WRITTEN" for claim in secondary_claims):
                raise Busy("Secondary identity has an unresolved write outcome")
            emit("deduplication", "running", "Checking Salesforce Leads, Accounts and the partner source")
            matches = await self.deduplicator.check(signals, own_key)
            emit(
                "deduplication",
                "done",
                "Duplicate checks completed",
                {"matches": len(matches.duplicates), "uncertain": matches.uncertain},
            )
            if intent and intent["lead_id"] != row["lead_id"] and intent["state"] == "WRITTEN":
                matches.duplicates.append(
                    ExistingEntity(
                        system="internal",
                        entity_type=intent["target_object"],
                        entity_id=intent["record_id"],
                        matched_by="PLACE_RESERVATION",
                    )
                )
            if matches.duplicates:
                response.duplicate_status = "DUPLICATE"
                response.qualification_status = Status.DUPLICATE
                response.qualification_reason_codes = ["EXISTING_ENTITY_FOUND"]
                response.existing_entities = matches.duplicates
                response.existing_entity_id = matches.duplicates[0].entity_id
                return await finish()
            if matches.uncertain or (secondary_claims and not intent):
                response.qualification_reason_codes = ["SECONDARY_DUPLICATE_MATCH_UNCERTAIN"]
                return await finish()
            response.duplicate_status = "CLEAR"
            if matches.own_record:
                if not intent or matches.own_record.entity_type != intent["target_object"]:
                    response.qualification_reason_codes = ["WRITE_OWNERSHIP_REQUIRES_REVIEW"]
                    return await finish()
                response.qualification_status = Status.QUALIFIED
                response.qualification_reason_codes = ["QUALIFIED_WRITE_RECOVERED"]
                response.salesforce_record_id = matches.own_record.entity_id
                response.salesforce_object = intent["target_object"]
                response.processing_metadata.persistence_status = "RECOVERED"
                response.processing_metadata.rules_version = intent.get(
                    "rules_version", self.settings.rules_version
                )
                return await finish()
            emit("enrichment", "running", "Retrieving qualification fields")
            response.normalized_enrichment = await self.google.enrich(resolution.place_id)
            emit(
                "enrichment",
                "done",
                "Canonical enrichment is ready",
                response.normalized_enrichment.model_dump(mode="json"),
            )
            enriched_phone = response.normalized_enrichment.phone
            if enriched_phone and enriched_phone != signals.phone:
                # New signals supplement the mandatory pre-enrichment duplicate check.
                await session.lock("phone:" + enriched_phone)
                followup = await self.deduplicator.check(
                    Signals(signals.place_id, signals.name, signals.address, enriched_phone), own_key
                )
                if followup.duplicates:
                    response.duplicate_status = "DUPLICATE"
                    response.qualification_status = Status.DUPLICATE
                    response.qualification_reason_codes = ["EXISTING_ENTITY_FOUND"]
                    response.existing_entities = followup.duplicates
                    response.existing_entity_id = followup.duplicates[0].entity_id
                    return await finish()
                if followup.uncertain:
                    response.duplicate_status = "UNCHECKED"
                    response.qualification_reason_codes = ["SECONDARY_DUPLICATE_MATCH_UNCERTAIN"]
                    return await finish()
            if self.settings.residential_check_enabled:
                emit("location", "running", "Checking registered premises use")
                if self.residential_checker:
                    await self.residential_checker.check(response.normalized_enrichment, lead.address)
                emit(
                    "location",
                    "done",
                    response.normalized_enrichment.premises_check_status,
                    {"evidence": response.normalized_enrichment.premises_check_evidence},
                )
            emit("qualification", "running", "Applying deterministic qualification rules")
            decision = qualify(response.normalized_enrichment, self.settings)
            emit("qualification", "done", str(decision.status), {"reasons": decision.reasons})
            response.qualification_status = decision.status
            response.qualification_reason_codes = decision.reasons
            if decision.status != Status.QUALIFIED:
                return await finish()
            obj = (
                intent["target_object"]
                if intent
                else self.settings.source_routes.get(lead.source, self.settings.route)
            )
            context = {f"input.{k}": v for k, v in lead.model_dump().items()}
            address = response.normalized_address
            context.update(
                {
                    "lead_id": str(row["lead_id"]),
                    "place_id": resolution.place_id,
                    "normalized_name": signals.name,
                    "normalized_address": signals.address,
                    "normalized_phone": signals.phone
                    or (
                        enriched_phone
                        if self.settings.places_provider == "serpapi"
                        and self.settings.allow_serpapi_crm_storage
                        else None
                    ),
                    "address.street": address.street,
                    "address.postal_code": address.postal_code,
                    "address.city": address.city,
                    "address.country": address.country,
                }
            )
            context.update(
                {
                    f"enrichment.{k}": v
                    for k, v in response.normalized_enrichment.model_dump(mode="json").items()
                }
            )
            emit("mapping", "running", "Validating Salesforce field names and types")
            try:
                response.salesforce_fields = self.mapper.map(obj, context)
                emit("mapping", "done", f"Mapped fields for {obj}", response.salesforce_fields)
            except ValueError:
                response.qualification_status = Status.REVIEW_REQUIRED
                response.qualification_reason_codes = ["SALESFORCE_MAPPING_INVALID"]
                return await finish()
            response.salesforce_object = obj
            if self.settings.persistence_enabled:
                emit("persistence", "running", "Reserving identity and writing to Salesforce")
                intent = await session.reserve(
                    resolution.place_id,
                    row["lead_id"],
                    obj,
                    own_key,
                    signal_keys,
                    self.settings.rules_version,
                )
                # Commit the intent before any side effect. Do not clear it on timeout or cancellation.
                response.salesforce_record_id = await self.salesforce.upsert(
                    intent["target_object"], intent["external_key"], response.salesforce_fields
                )
                response.processing_metadata.persistence_status = "WRITTEN"
                emit(
                    "persistence",
                    "done",
                    "Salesforce write confirmed",
                    {"object": obj, "record_id": response.salesforce_record_id},
                )
            return await finish()
