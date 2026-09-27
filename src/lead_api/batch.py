"""Bounded CSV parsing shared by the API and the local console; no provider calls here."""

import csv
import io

from pydantic import ValidationError

from .models import LeadInput

MAX_BYTES = 256_000
MAX_ROWS = 100
COLUMNS = {"source", "source_lead_id", "business_name", "address", "place_id", "phone"}


def parse_csv(content: str) -> list[LeadInput]:
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("CSV exceeds 256 KB")
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")), strict=True)
    headers = reader.fieldnames or []
    if len(headers) != len(set(headers)) or not set(headers) <= COLUMNS:
        raise ValueError("Duplicate or unsupported CSV columns")
    if not {"source", "source_lead_id", "business_name", "address"} <= set(headers):
        raise ValueError("Required columns: source, source_lead_id, business_name, address")
    leads, identities = [], set()
    try:
        for index, row in enumerate(reader, 2):
            if len(leads) >= MAX_ROWS:
                raise ValueError("CSV is limited to 100 leads")
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"Row {index}: column count does not match the header")
            values = {k: v.strip() for k, v in row.items() if v.strip()}
            try:
                lead = LeadInput(**values)
            except ValidationError as exc:
                locations = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
                raise ValueError(f"Row {index}: invalid or missing {locations}") from exc
            identity = (lead.source, lead.source_lead_id)
            if identity in identities:
                raise ValueError(f"Row {index}: repeated source/source_lead_id")
            identities.add(identity)
            leads.append(lead)
    except csv.Error as exc:
        raise ValueError("Malformed CSV quoting") from exc
    if not leads:
        raise ValueError("CSV contains no leads")
    return leads


async def read_csv_request(request):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BYTES:
            raise ValueError("CSV exceeds 256 KB")
    try:
        return parse_csv(content.decode("utf-8-sig"))
    except UnicodeError as exc:
        raise ValueError("CSV must be UTF-8") from exc
