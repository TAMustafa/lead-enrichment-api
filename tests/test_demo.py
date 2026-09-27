import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from lead_api.deduplication import Candidate
from lead_api.demo import DemoInput, create_demo_app
from lead_api.enrichment import canonicalize


def test_demo_rejects_empty_normalized_name():
    with pytest.raises(ValidationError):
        DemoInput(business_name="   ", address="10 Main", source_lead_id="test")


async def test_demo_blocks_remote_clients_and_bad_hosts():
    app = create_demo_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.2", 123)), base_url="http://127.0.0.1:8765"
    ) as client:
        assert (await client.get("/health/live")).status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://attacker.example"
    ) as client:
        assert (await client.get("/health/live")).status_code == 400


@pytest.mark.integration
async def test_demo_streams_real_pipeline_and_prevents_duplicate_writes(repository, settings):
    places = AsyncMock()
    places.search.side_effect = lambda name, address: [
        {"id": "test-place", "displayName": {"text": name}, "formattedAddress": address}
    ]
    places.enrich.return_value = canonicalize(
        {
            "businessStatus": "OPERATIONAL",
            "types": ["restaurant", "italian_restaurant"],
            "rating": 4.5,
            "userRatingCount": 120,
        }
    ).model_copy(update={"research_signals": ["Offerings: Vegan options"]})
    partners = AsyncMock()
    partners.lookup.return_value = []
    app = create_demo_app(places=places, partners=partners)
    settings.persistence_enabled = True
    settings.places_provider = "serpapi"
    settings.allow_serpapi_crm_storage = True
    settings.mapping_path = str(Path(__file__).parents[1] / "salesforce-mapping-serpapi.json")
    app.state.settings, app.state.pool = settings, repository.pool
    app.state.token = "test-csrf"
    app.state.limiter = AsyncMock()
    app.state.limiter.allow.return_value = True
    app.state.places, app.state.partners = places, partners
    sf = AsyncMock()
    records = []
    sf.lookup.side_effect = lambda signals: list(records)

    async def write(obj, key, fields):
        assert fields["Description"] == "Offerings: Vegan options"
        records.append(
            Candidate("salesforce", obj, "test-record", fields["Google_Place_ID__c"], external_key=key)
        )
        return "test-record"

    sf.upsert.side_effect = write
    app.state.salesforce = sf
    headers = {"Origin": "http://127.0.0.1:8765", "X-Demo-Token": "test-csrf"}
    payload = {"business_name": "Bella", "address": "10 Main", "source_lead_id": "one"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://127.0.0.1:8765"
    ) as client:
        assert (await client.post("/demo/api/qualify", json=payload)).status_code == 403
        bad = headers | {"Origin": "https://attacker.example"}
        assert (await client.post("/demo/api/qualify", headers=bad, json=payload)).status_code == 403
        response = await client.post("/demo/api/qualify", headers=headers, json=payload)
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines()]
        completed = [e["stage"] for e in events if e["type"] == "step" and e["state"] == "done"]
        assert completed == [
            "validation",
            "identity",
            "resolution",
            "deduplication",
            "enrichment",
            "qualification",
            "mapping",
            "persistence",
        ]
        assert events[-1]["result"]["qualification_status"] == "QUALIFIED"
        response = await client.post("/demo/api/qualify", headers=headers, json=payload)
        replay = json.loads(response.text.splitlines()[-1])
        assert replay["result"]["processing_metadata"]["idempotent_replay"]
        response = await client.post(
            "/demo/api/qualify", headers=headers, json=payload | {"source_lead_id": "two"}
        )
        duplicate = json.loads(response.text.splitlines()[-1])
        assert duplicate["result"]["qualification_status"] == "DUPLICATE"
        sf.upsert.assert_awaited_once()
