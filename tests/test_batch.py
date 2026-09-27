import json

import pytest

from lead_api.batch import parse_csv
from lead_api.mapping import SalesforceMapper


def test_csv_quotes_unicode_and_stable_identity():
    rows = parse_csv(
        'source,source_lead_id,business_name,address\nGRID,1,"Café, Deli","10 Main, Amsterdam"\n'
    )
    assert rows[0].business_name == "Café, Deli"
    assert rows[0].address == "10 Main, Amsterdam"


@pytest.mark.parametrize(
    "csv",
    [
        "source,business_name,address\nGRID,Bella,10 Main",
        "source,source_lead_id,business_name,address,address\nGRID,1,Bella,10 Main,10 Main",
        "source,source_lead_id,business_name,address\nGRID,1,Bella,10 Main\nGRID,1,Bella,10 Main",
        "source,source_lead_id,business_name,address,has_delivery\nGRID,1,Bella,10 Main,maybe",
        "source,source_lead_id,business_name,address\nGRID,1,Bella,10 Main,extra",
    ],
)
def test_csv_rejects_invalid_file_before_writes(csv):
    with pytest.raises(ValueError):
        parse_csv(csv)


def test_mapping_and_disabled_fields(settings, tmp_path):
    raw = json.loads(open(settings.mapping_path).read())
    raw["Lead"]["Source__c"] = {"source": "input.source", "type": "string"}
    raw["Lead"]["Disabled__c"] = {"source": "input.phone", "type": "string", "enabled": False}
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(raw))
    settings.mapping_path = str(path)
    fields = SalesforceMapper(settings).map(
        "Lead",
        {"input.business_name": "Bella", "input.source": "GRID", "input.phone": "private"},
    )
    assert fields["Source__c"] == "GRID"
    assert "Disabled__c" not in fields


def test_required_identity_cannot_be_disabled(settings, tmp_path):
    raw = json.loads(open(settings.mapping_path).read())
    raw["Lead"]["Google_Place_ID__c"]["enabled"] = False
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(raw))
    settings.mapping_path = str(path)
    with pytest.raises(ValueError, match="identity"):
        SalesforceMapper(settings)


async def test_csv_endpoint_validates_whole_file_and_isolates_row_errors(settings):
    from unittest.mock import AsyncMock, Mock

    import httpx

    from lead_api.api import create_app
    from lead_api.errors import Conflict

    app = create_app(settings)
    app.state.settings = settings
    app.state.limiter = AsyncMock()
    app.state.limiter.allow.return_value = True
    app.state.service = AsyncMock()
    result = Mock()
    result.model_dump.return_value = {"qualification_status": "QUALIFIED"}
    app.state.service.process.side_effect = [Conflict("changed"), result]
    body = "source,source_lead_id,business_name,address\nGRID,1,Bella,10 Main\nGRID,2,Deli,20 Main\n"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/v1/leads/qualify/csv", content=body)).status_code == 401
        headers = {"X-API-Key": "x" * 32, "Content-Type": "text/csv"}
        invalid = await client.post("/v1/leads/qualify/csv", content=body + "GRID,3,\n", headers=headers)
        assert invalid.status_code == 422
        app.state.service.process.assert_not_called()
        response = await client.post("/v1/leads/qualify/csv", content=body, headers=headers)
        rows = [json.loads(line) for line in response.text.splitlines()]
        assert rows[0]["error"] == "IDEMPOTENCY_CONFLICT"
        assert rows[1]["result"]["qualification_status"] == "QUALIFIED"
        assert app.state.service.process.await_count == 2


def test_list_to_text_mapping_is_explicit(settings, tmp_path):
    raw = json.loads(open(settings.mapping_path).read())
    raw["Lead"]["Hours__c"] = {
        "source": "enrichment.opening_hours",
        "type": "string",
        "join_with": "\n",
        "max_length": 4000,
    }
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(raw))
    settings.mapping_path = str(path)
    settings.allow_google_crm_storage = True
    result = SalesforceMapper(settings).map(
        "Lead",
        {
            "input.business_name": "Bella",
            "enrichment.opening_hours": ["Monday 10-20", "Tuesday 10-20"],
        },
    )
    assert result["Hours__c"] == "Monday 10-20\nTuesday 10-20"
