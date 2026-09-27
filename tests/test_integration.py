import asyncio

import pytest

from lead_api.errors import Busy, Conflict
from lead_api.models import LeadResponse, Metadata, Status
from lead_api.rate_limit import PostgresRateLimiter

pytestmark = pytest.mark.integration


async def test_idempotency_payload_conflict_and_lock_release(repository, lead):
    async with repository.session(lead) as session:
        row = await session.identity(lead, repository.fingerprint(lead))
        with pytest.raises(Busy):
            async with repository.session(lead):
                pass
    async with repository.session(lead) as session:
        replay = await session.identity(lead, repository.fingerprint(lead))
        assert replay["lead_id"] == row["lead_id"]
        with pytest.raises(Conflict):
            await session.identity(lead, "different")


async def test_reservation_survives_connection_loss(repository, lead):
    async with repository.session(lead) as session:
        row = await session.identity(lead, repository.fingerprint(lead))
        await session.reserve(lead.place_id, row["lead_id"], "Lead", "key")
    other = lead.model_copy(update={"source_lead_id": "other"})
    async with repository.session(other) as session:
        row2 = await session.identity(other, repository.fingerprint(other))
        with pytest.raises(Busy):
            await session.reserve(lead.place_id, row2["lead_id"], "Account", "other-key")
        assert (await session.intent(lead.place_id))["lead_id"] == row["lead_id"]


async def test_finish_atomic_and_google_content_not_stored(repository, lead):
    from lead_api.enrichment import canonicalize

    async with repository.session(lead) as session:
        row = await session.identity(lead, repository.fingerprint(lead))
        response = LeadResponse(
            lead_id=row["lead_id"],
            source=lead.source,
            source_lead_id=lead.source_lead_id,
            place_id=lead.place_id,
            duplicate_status="CLEAR",
            qualification_status=Status.QUALIFIED,
            qualification_reason_codes=["ELIGIBILITY_CONFIRMED"],
            normalized_enrichment=canonicalize({"rating": 4.4, "websiteUri": "secret-content"}),
            salesforce_fields={"Enriched_Field__c": "secret-content"},
            processing_metadata=Metadata(request_id="id", rules_version="v1"),
        )
        await session.finish(response)
        saved = await session.identity(lead, repository.fingerprint(lead))
        assert "secret-content" not in saved["result"]
        assert '"normalized_enrichment": null' in saved["result"]
        assert await session.connection.fetchval("SELECT count(*) FROM lead_audit") == 1


async def test_concurrent_rate_limit_is_atomic(repository):
    limiter = PostgresRateLimiter(repository.pool, 10)
    allowed = await asyncio.gather(*(limiter.allow("test-client") for _ in range(30)))
    assert sum(allowed) == 10
    async with repository.pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM rate_limits") == 1
        await connection.execute("UPDATE rate_limits SET window_start = now() - interval '61 seconds'")
    assert await limiter.allow("test-client")


async def test_two_replicas_cannot_write_same_place(repository, settings, lead):
    from unittest.mock import AsyncMock

    from lead_api.deduplication import Candidate, Deduplicator
    from lead_api.enrichment import canonicalize
    from lead_api.mapping import SalesforceMapper
    from lead_api.resolution import PlaceResolver
    from lead_api.service import LeadService

    settings.persistence_enabled = True
    started, release = asyncio.Event(), asyncio.Event()
    sf, partner, google = AsyncMock(), AsyncMock(), AsyncMock()
    records = []
    partner.lookup.return_value = []
    sf.lookup.side_effect = lambda signals: list(records)
    google.enrich.return_value = canonicalize(
        {"businessStatus": "OPERATIONAL", "types": ["restaurant"], "rating": 4, "userRatingCount": 50}
    )

    async def write(obj, key, fields):
        started.set()
        await release.wait()
        records.append(Candidate("salesforce", obj, "record", lead.place_id, external_key=key))
        return "record"

    sf.upsert.side_effect = write

    def replica():
        return LeadService(
            repository,
            PlaceResolver(google, settings),
            Deduplicator(sf, partner),
            google,
            SalesforceMapper(settings),
            sf,
            settings,
        )

    first, second = replica(), replica()
    other = lead.model_copy(update={"source": "SELF_SIGNUP", "source_lead_id": "other"})
    task = asyncio.create_task(first.process(lead, "request-1"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        with pytest.raises(Busy):
            await second.process(other, "request-2")
    finally:
        release.set()
    created = await task
    assert created.qualification_status == Status.QUALIFIED
    duplicate = await second.process(other, "retry")
    assert duplicate.qualification_status == Status.DUPLICATE
    replay = await first.process(lead, "replay")
    assert replay.lead_id == created.lead_id
    assert replay.normalized_enrichment is None
    assert replay.processing_metadata.enrichment_omitted_on_replay
    assert replay.processing_metadata.idempotent_replay
    sf.upsert.assert_awaited_once()
    google.enrich.assert_awaited_once()


async def test_crash_window_recovers_without_second_write(repository, settings, lead):
    from unittest.mock import AsyncMock

    from lead_api.deduplication import Candidate, Deduplicator
    from lead_api.enrichment import canonicalize
    from lead_api.errors import DependencyUnavailable
    from lead_api.mapping import SalesforceMapper
    from lead_api.resolution import PlaceResolver
    from lead_api.service import LeadService

    settings.persistence_enabled = True
    sf, partner, google = AsyncMock(), AsyncMock(), AsyncMock()
    records = []
    partner.lookup.return_value = []
    sf.lookup.side_effect = lambda signals: list(records)
    google.enrich.return_value = canonicalize(
        {"businessStatus": "OPERATIONAL", "types": ["restaurant"], "rating": 4, "userRatingCount": 50}
    )

    async def lost_response(obj, key, fields):
        records.append(Candidate("salesforce", obj, "record", lead.place_id, external_key=key))
        raise DependencyUnavailable("write accepted, response lost")

    sf.upsert.side_effect = lost_response
    service = LeadService(
        repository,
        PlaceResolver(google, settings),
        Deduplicator(sf, partner),
        google,
        SalesforceMapper(settings),
        sf,
        settings,
    )
    with pytest.raises(DependencyUnavailable):
        await service.process(lead, "first")
    original_rules_version = settings.rules_version
    settings.rules_version = "new-version-after-crash"
    recovered = await service.process(lead, "retry")
    assert recovered.processing_metadata.rules_version == original_rules_version
    assert recovered.salesforce_record_id == "record"
    assert recovered.processing_metadata.persistence_status == "RECOVERED"
    sf.upsert.assert_awaited_once()
    async with repository.session(lead) as session:
        assert (await session.intent(lead.place_id))["state"] == "WRITTEN"


async def test_secondary_reservation_survives_crash(repository, lead):
    keys = repository.signal_keys(["name-address"])
    async with repository.session(lead) as session:
        row = await session.identity(lead, repository.fingerprint(lead))
        await session.reserve(lead.place_id, row["lead_id"], "Lead", "key", keys)
    other = lead.model_copy(update={"source_lead_id": "other", "place_id": "different-place"})
    async with repository.session(other) as session:
        row2 = await session.identity(other, repository.fingerprint(other))
        assert len(await session.secondary_claims(keys, row2["lead_id"])) == 1
        with pytest.raises(Busy):
            await session.reserve(other.place_id, row2["lead_id"], "Account", "other-key", keys)
        assert await session.intent(other.place_id) is None  # rollback is atomic
