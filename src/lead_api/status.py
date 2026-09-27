"""Read persisted decisions without provider calls or Salesforce mutations."""

from fastapi import HTTPException

from .models import LeadResponse


def status_payload(row, salesforce_url):
    result = LeadResponse.model_validate_json(row["result"]) if row["result"] else None
    link = None
    if result:
        obj, record_id = result.salesforce_object, result.salesforce_record_id
        if not record_id:
            entity = next(
                (
                    e
                    for e in result.existing_entities
                    if e.system in {"salesforce", "internal"} and e.entity_type in {"Lead", "Account"}
                ),
                None,
            )
            if entity:
                obj, record_id = entity.entity_type, entity.entity_id
        if record_id and obj in {"Lead", "Account"}:
            link = f"{salesforce_url}/lightning/r/{obj}/{record_id}/view"
    return {
        "lead_id": str(row["lead_id"]),
        "source": row["source"],
        "source_lead_id": row["source_lead_id"],
        "state": "COMPLETED" if result else "INCOMPLETE",
        "result": result.model_dump(mode="json") if result else None,
        "salesforce_url": link,
        "message": (
            "Decision restored from history. No new Salesforce write."
            if result
            else "No final decision saved. It may still be running; retry the same request to recover."
        ),
    }


async def read_status(pool, lead_id, salesforce_url):
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT lead_id,source,source_lead_id,result FROM lead_requests WHERE lead_id=$1", lead_id
        )
    if row is None:
        raise HTTPException(404, "Lead request not found")
    return status_payload(row, salesforce_url)
