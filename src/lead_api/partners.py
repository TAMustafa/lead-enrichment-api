import asyncio

import google.auth
import google.auth.transport.requests

from .deduplication import Candidate, Signals
from .errors import DependencyUnavailable


class BigQueryPartners:
    """Replace with a low-latency index implementing DuplicateSource without changing orchestration.

    The table contains first-party normalized partner data, never cached Google enrichment.
    """

    def __init__(self, http, settings):
        self.http, self.settings = http, settings
        self.credentials = None
        self.lock = asyncio.Lock()

    async def token(self):
        async with self.lock:
            if self.credentials is None:
                self.credentials, _ = await asyncio.to_thread(
                    google.auth.default, scopes=["https://www.googleapis.com/auth/bigquery.readonly"]
                )
            if not self.credentials.valid:
                await asyncio.to_thread(self.credentials.refresh, google.auth.transport.requests.Request())
            return self.credentials.token

    async def lookup(self, signals: Signals) -> list[Candidate]:
        values = {
            "place": signals.place_id,
            "name": signals.name,
            "address": signals.address,
            "phone": signals.phone or "",
        }
        query = f"""SELECT partner_id, place_id, normalized_name, normalized_address, normalized_phone
        FROM `{self.settings.bigquery_table}`
        WHERE place_id = @place OR (normalized_name = @name AND normalized_address = @address)
           OR (@phone != '' AND normalized_phone = @phone) LIMIT 201"""
        try:
            token = await self.token()
        except Exception as exc:
            raise DependencyUnavailable("BigQuery credentials unavailable") from exc
        response = await self.http.request(
            "bigquery",
            "POST",
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{self.settings.bigquery_project}/queries",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "query": query,
                "useLegacySql": False,
                "parameterMode": "NAMED",
                "timeoutMs": 1500,
                "maxResults": 201,
                "maximumBytesBilled": "1000000000",
                "queryParameters": [
                    {"name": k, "parameterType": {"type": "STRING"}, "parameterValue": {"value": v}}
                    for k, v in values.items()
                ],
            },
        )
        data = response.json()
        if (
            not data.get("jobComplete")
            or data.get("errors")
            or data.get("pageToken")
            or int(data.get("totalRows", 0)) > 200
        ):
            raise DependencyUnavailable("BigQuery lookup incomplete")
        result = []
        for row in data.get("rows", []):
            values = [cell.get("v") for cell in row["f"]]
            result.append(Candidate("bigquery", "Partner", str(values[0]), *values[1:]))
        return result
