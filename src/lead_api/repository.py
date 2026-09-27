import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from uuid import uuid4

from .errors import Busy, Conflict
from .models import LeadInput, LeadResponse


def key64(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=True)


class Repository:
    def __init__(self, pool, secret: str):
        self.pool, self.secret = pool, secret.encode()

    def fingerprint(self, lead: LeadInput) -> str:
        payload = lead.model_dump_json()
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def signal_keys(self, signals: list[str]) -> list[str]:
        return [hmac.new(self.secret, signal.encode(), hashlib.sha256).hexdigest() for signal in signals]

    @asynccontextmanager
    async def session(self, lead: LeadInput):
        async with self.pool.acquire() as connection:
            session = Session(connection)
            try:
                await session.lock("source:" + json.dumps([lead.source, lead.source_lead_id]))
                yield session
            finally:
                # asyncpg pool reset also releases session locks if cancellation interrupts this query.
                await connection.execute("SELECT pg_advisory_unlock_all()")


class Session:
    def __init__(self, connection):
        self.connection = connection

    async def lock(self, value: str):
        if not await self.connection.fetchval("SELECT pg_try_advisory_lock($1)", key64(value)):
            raise Busy("Matching lead is being processed; retry")

    async def identity(self, lead: LeadInput, fingerprint: str):
        row = await self.connection.fetchrow(
            "SELECT * FROM lead_requests WHERE source=$1 AND source_lead_id=$2",
            lead.source,
            lead.source_lead_id,
        )
        if row:
            if not hmac.compare_digest(row["fingerprint"], fingerprint):
                raise Conflict("Idempotency key was already used with a different payload")
            return row
        return await self.connection.fetchrow(
            "INSERT INTO lead_requests(lead_id,source,source_lead_id,fingerprint) "
            "VALUES($1,$2,$3,$4) RETURNING *",
            uuid4(),
            lead.source,
            lead.source_lead_id,
            fingerprint,
        )

    async def set_place(self, lead_id, place_id, confidence):
        await self.connection.execute(
            "UPDATE lead_requests SET place_id=$2,resolution_confidence=$3,updated_at=now() WHERE lead_id=$1",
            lead_id,
            place_id,
            confidence,
        )

    async def intent(self, place_id):
        return await self.connection.fetchrow("SELECT * FROM write_intents WHERE place_id=$1", place_id)

    async def secondary_claims(self, keys, lead_id):
        return await self.connection.fetch(
            "SELECT i.* FROM identity_claims c JOIN write_intents i ON c.place_id=i.place_id "
            "WHERE c.signal_key=ANY($1::text[]) AND i.lead_id != $2",
            keys,
            lead_id,
        )

    async def reserve(self, place_id, lead_id, obj, key, signal_keys=(), rules_version="unspecified"):
        async with self.connection.transaction():
            await self.connection.execute(
                "INSERT INTO write_intents(place_id,lead_id,target_object,external_key,state,rules_version) "
                "VALUES($1,$2,$3,$4,'RESERVED',$5) ON CONFLICT(place_id) DO NOTHING",
                place_id,
                lead_id,
                obj,
                key,
                rules_version,
            )
            row = await self.intent(place_id)
            if row["lead_id"] != lead_id:
                raise Busy("Place has an unresolved write reservation")
            for signal in signal_keys:
                await self.connection.execute(
                    "INSERT INTO identity_claims(signal_key,place_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                    signal,
                    place_id,
                )
                owner = await self.connection.fetchval(
                    "SELECT place_id FROM identity_claims WHERE signal_key=$1", signal
                )
                if owner != place_id:
                    raise Busy("Secondary identity has a prior write reservation")
            await self.audit(lead_id, "WRITE_RESERVED", {"target_object": row["target_object"]})
            return row

    async def audit(self, lead_id, event, metadata):
        await self.connection.execute(
            "INSERT INTO lead_audit(lead_id,event,metadata) VALUES($1,$2,$3::jsonb)",
            lead_id,
            event,
            json.dumps(metadata),
        )

    async def finish(self, response: LeadResponse):
        # No Google content or mapped enrichment is stored. A replay returns decision metadata only.
        stored = response.model_copy(deep=True)
        stored.normalized_enrichment = None
        stored.salesforce_fields = {}
        stored.processing_metadata.enrichment_omitted_on_replay = response.normalized_enrichment is not None
        async with self.connection.transaction():
            if response.salesforce_record_id:
                await self.connection.execute(
                    "UPDATE write_intents SET state='WRITTEN',record_id=$2,updated_at=now() WHERE lead_id=$1",
                    response.lead_id,
                    response.salesforce_record_id,
                )
            await self.connection.execute(
                "UPDATE lead_requests SET result=$2::jsonb,updated_at=now() WHERE lead_id=$1",
                response.lead_id,
                stored.model_dump_json(),
            )
            await self.audit(
                response.lead_id,
                "DECISION",
                {
                    "status": response.qualification_status,
                    "reasons": response.qualification_reason_codes,
                    "rules_version": response.processing_metadata.rules_version,
                    "persistence": response.processing_metadata.persistence_status,
                },
            )
