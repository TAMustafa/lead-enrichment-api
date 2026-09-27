from uuid import uuid4

import pytest
from fastapi import HTTPException

from lead_api.models import ExistingEntity, LeadResponse, Metadata
from lead_api.status import read_status, status_payload


def test_duplicate_status_has_existing_record_link_and_no_write():
    lead_id = uuid4()
    result = LeadResponse(
        lead_id=lead_id,
        source="GRID",
        source_lead_id="one",
        place_id="place",
        duplicate_status="DUPLICATE",
        qualification_status="DUPLICATE",
        qualification_reason_codes=["EXISTING_ENTITY_FOUND"],
        existing_entities=[
            ExistingEntity(
                system="salesforce", entity_type="Lead", entity_id="00Qtest", matched_by="PLACE_ID"
            )
        ],
        processing_metadata=Metadata(request_id="test", rules_version="test"),
    )
    row = dict(lead_id=lead_id, source="GRID", source_lead_id="one", result=result.model_dump_json())
    data = status_payload(row, "https://example.my.salesforce.com")
    assert data["state"] == "COMPLETED"
    assert data["salesforce_url"].endswith("/Lead/00Qtest/view")
    assert data["result"]["processing_metadata"]["persistence_status"] == "NOT_REQUESTED"
    row["result"] = None
    assert status_payload(row, "https://example.my.salesforce.com")["state"] == "INCOMPLETE"


@pytest.mark.integration
async def test_status_looks_up_stored_decision_without_processing(repository, lead):
    async with repository.session(lead) as session:
        row = await session.identity(lead, repository.fingerprint(lead))
    data = await read_status(repository.pool, row["lead_id"], "https://example.my.salesforce.com")
    assert data["state"] == "INCOMPLETE"
    with pytest.raises(HTTPException) as exc:
        await read_status(repository.pool, uuid4(), "https://example.my.salesforce.com")
    assert exc.value.status_code == 404
