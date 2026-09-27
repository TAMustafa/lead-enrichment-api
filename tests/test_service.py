from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from lead_api.deduplication import Candidate, DedupeResult
from lead_api.enrichment import canonicalize
from lead_api.errors import Busy
from lead_api.mapping import SalesforceMapper
from lead_api.models import ExistingEntity, Status
from lead_api.resolution import Resolution
from lead_api.service import LeadService


class MemoryRepository:
    """Unit-test double; concurrency is tested separately against real PostgreSQL."""

    def __init__(self):
        self.row = {"lead_id": uuid4(), "result": None, "place_id": None}
        self.db = AsyncMock()
        self.db.identity.return_value = self.row
        self.db.intent.return_value = None
        self.db.secondary_claims.return_value = []
        self.db.reserve.return_value = {"target_object": "Lead", "external_key": "key"}

    def signal_keys(self, values):
        return values

    def fingerprint(self, lead):
        return "fingerprint"

    @asynccontextmanager
    async def session(self, lead):
        yield self.db


@pytest.fixture
def service(settings, lead):
    repo = MemoryRepository()
    resolver, dedupe, google, sf = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    resolver.resolve.return_value = Resolution(lead.place_id, 1, "PLACE_ID_SUPPLIED")
    dedupe.check.return_value = DedupeResult([], False, None)
    google.enrich.return_value = canonicalize(
        {"businessStatus": "OPERATIONAL", "types": ["restaurant"], "rating": 4, "userRatingCount": 50}
    )
    sf.upsert.return_value = "00Q123"
    return LeadService(repo, resolver, dedupe, google, SalesforceMapper(settings), sf, settings)


async def test_duplicate_never_enriches_or_writes(service, lead):
    service.deduplicator.check.return_value = DedupeResult(
        [
            ExistingEntity(
                system="salesforce", entity_type="Lead", entity_id="existing", matched_by="PLACE_ID"
            )
        ],
        False,
        None,
    )
    result = await service.process(lead, "request")
    assert result.qualification_status == Status.DUPLICATE
    service.google.enrich.assert_not_awaited()
    service.salesforce.upsert.assert_not_awaited()


async def test_review_never_enriches(service, lead):
    service.resolver.resolve.return_value = Resolution(None, 0.5, "PLACE_MATCH_UNCERTAIN")
    result = await service.process(lead, "request")
    assert result.qualification_status == Status.REVIEW_REQUIRED
    service.deduplicator.check.assert_not_awaited()
    service.google.enrich.assert_not_awaited()


async def test_order_of_dedup_reserve_and_write(service, lead):
    service.settings.persistence_enabled = True
    events = []

    async def check(*args):
        events.append("dedupe")
        return DedupeResult([], False, None)

    async def enrich(*args):
        events.append("enrich")
        return canonicalize(
            {"businessStatus": "OPERATIONAL", "types": ["restaurant"], "rating": 4, "userRatingCount": 50}
        )

    async def reserve(*args):
        events.append("reserve")
        return {"target_object": "Lead", "external_key": "key"}

    async def write(*args):
        events.append("write")
        return "id"

    service.deduplicator.check.side_effect = check
    service.google.enrich.side_effect = enrich
    service.repository.db.reserve.side_effect = reserve
    service.salesforce.upsert.side_effect = write
    result = await service.process(lead, "request")
    assert result.qualification_status == Status.QUALIFIED
    assert events == ["dedupe", "enrich", "reserve", "write"]


async def test_unresolved_other_write_blocks(service, lead):
    service.repository.db.intent.return_value = {"lead_id": uuid4(), "state": "RESERVED"}
    with pytest.raises(Busy):
        await service.process(lead, "request")
    service.google.enrich.assert_not_awaited()
    service.salesforce.upsert.assert_not_awaited()


async def test_unknown_outcome_recovery_does_not_write_again(service, lead):
    service.repository.db.intent.return_value = {
        "lead_id": service.repository.row["lead_id"],
        "state": "RESERVED",
        "target_object": "Lead",
    }
    service.deduplicator.check.return_value = DedupeResult(
        [], False, Candidate("salesforce", "Lead", "recovered", place_id=lead.place_id, external_key="key")
    )
    result = await service.process(lead, "request")
    assert result.processing_metadata.persistence_status == "RECOVERED"
    assert result.salesforce_record_id == "recovered"
    service.google.enrich.assert_not_awaited()
    service.salesforce.upsert.assert_not_awaited()


async def test_dry_run_no_write(service, lead):
    result = await service.process(lead, "request")
    assert result.qualification_status == Status.QUALIFIED
    assert result.salesforce_fields["Company"] == lead.business_name
    service.salesforce.upsert.assert_not_awaited()


async def test_replay_has_same_identity(service, lead):
    result = await service.process(lead, "original")
    service.repository.row["result"] = result.model_dump_json()
    service.google.enrich.reset_mock()
    replay = await service.process(lead, "replay")
    assert replay.lead_id == result.lead_id
    assert replay.processing_metadata.idempotent_replay
    assert replay.processing_metadata.request_id == "replay"
    service.google.enrich.assert_not_awaited()


async def test_residential_reject_prevents_salesforce_write(service, lead):
    service.settings.residential_check_enabled = True
    service.settings.persistence_enabled = True
    checker = AsyncMock()

    async def check(data, address):
        data.premises_check_status = "RESIDENTIAL"

    checker.check.side_effect = check
    service.residential_checker = checker
    result = await service.process(lead, "residential-test")
    assert result.qualification_status == Status.REJECTED
    assert result.qualification_reason_codes == ["BAG_RESIDENTIAL_PREMISES"]
    service.salesforce.upsert.assert_not_awaited()
    service.repository.db.reserve.assert_not_awaited()
