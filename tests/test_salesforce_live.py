"""Opt-in Salesforce + PostgreSQL tests, with synthetic Google/partner adapters.

LIVE_SALESFORCE_ORG=alias LIVE_SALESFORCE_ORG_ID=00D... TEST_DATABASE_URL=... pytest -m live
Creates UUID-marked test records and soft-deletes only those records during cleanup.
Never uses real Place IDs or calls Google/BigQuery. Never prints CLI credentials.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from lead_api.deduplication import Deduplicator, Signals
from lead_api.enrichment import canonicalize
from lead_api.errors import ProviderRejected
from lead_api.http import ResilientHTTP
from lead_api.mapping import SalesforceMapper
from lead_api.models import LeadInput, Status
from lead_api.resolution import PlaceResolver
from lead_api.salesforce import Salesforce
from lead_api.schema_contract import check_schema
from lead_api.service import LeadService

pytestmark = [pytest.mark.integration, pytest.mark.live]


@pytest.fixture
async def live_salesforce(settings):
    alias = os.environ.get("LIVE_SALESFORCE_ORG")
    expected_org_id = os.environ.get("LIVE_SALESFORCE_ORG_ID")
    if not alias or not expected_org_id:
        pytest.skip("Set LIVE_SALESFORCE_ORG and LIVE_SALESFORCE_ORG_ID to enable live writes")
    result = await asyncio.to_thread(
        subprocess.run,
        ["sf", "org", "display", "--target-org", alias, "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        env=dict(os.environ, SF_DISABLE_AUTOUPDATE="true", SF_DISABLE_TELEMETRY="true"),
    )
    if result.returncode:
        pytest.fail("Salesforce CLI authorization unavailable")
    auth = json.loads(result.stdout)["result"]
    if auth["id"] != expected_org_id:
        pytest.fail("Unexpected Salesforce org; refusing test writes")
    settings.salesforce_url = auth["instanceUrl"].rstrip("/")
    settings.persistence_enabled = True
    async with httpx.AsyncClient(timeout=10) as client:
        sf = Salesforce(ResilientHTTP(client), settings)
        sf.token, sf.expires = auth["accessToken"], time.monotonic() + 1800
        await check_schema(sf, SalesforceMapper(settings), settings)
        yield sf


@pytest.mark.parametrize("target", ["Lead", "Account"])
async def test_live_qualification_replay_duplicate_and_upsert(live_salesforce, repository, settings, target):
    sf = live_salesforce
    settings.route = target
    suffix = uuid4().hex
    place_id = f"lq-test-{suffix}"
    name = f"LQ integration test {suffix}"
    key = hashlib.sha256(("place:" + place_id).encode()).hexdigest()
    url = f"{sf.base}/sobjects/{target}/{settings.salesforce_external_field}/{key}"
    lead = LeadInput(
        source="INTEGRATION_TEST",
        source_lead_id=suffix,
        business_name=name,
        address="1 Synthetic Test Street",
        place_id=place_id,
    )
    google, partners = AsyncMock(), AsyncMock()
    google.enrich.return_value = canonicalize(
        {"businessStatus": "OPERATIONAL", "types": ["restaurant"], "rating": 4.5, "userRatingCount": 50}
    )
    partners.lookup.return_value = []
    service = LeadService(
        repository,
        PlaceResolver(google, settings),
        Deduplicator(sf, partners),
        google,
        SalesforceMapper(settings),
        sf,
        settings,
    )
    try:
        created = await service.process(lead, "live-create")
        assert created.qualification_status == Status.QUALIFIED
        assert created.salesforce_record_id
        assert created.processing_metadata.persistence_status == "WRITTEN"
        replay = await service.process(lead, "live-replay")
        assert replay.lead_id == created.lead_id
        assert replay.salesforce_record_id == created.salesforce_record_id
        assert replay.processing_metadata.idempotent_replay
        assert replay.normalized_enrichment is None
        other = lead.model_copy(update={"source": "SECOND_TEST_SOURCE"})
        duplicate = await service.process(other, "live-duplicate")
        assert duplicate.qualification_status == Status.DUPLICATE
        assert duplicate.existing_entity_id == created.salesforce_record_id
        google.enrich.assert_awaited_once()
        # A retry of the same provider upsert must update the same record, never insert a second one.
        assert await sf.upsert(target, key, created.salesforce_fields) == created.salesforce_record_id
        matches = await sf.lookup(Signals.from_lead(lead, place_id))
        assert len([c for c in matches if c.place_id == place_id]) == 1
    finally:
        # Resolve by the unique test key even if Salesforce accepted a write but the response was lost.
        try:
            response = await sf.request("GET", url)
        except ProviderRejected as exc:
            if exc.status_code != 404:
                raise
        else:
            record = response.json()
            field = "Company" if target == "Lead" else "Name"
            if record.get(field) != name or record.get(settings.salesforce_place_field) != place_id:
                pytest.fail("Test ownership check failed; fixture was not deleted")
            await sf.request("DELETE", f"{sf.base}/sobjects/{target}/{record['Id']}")
