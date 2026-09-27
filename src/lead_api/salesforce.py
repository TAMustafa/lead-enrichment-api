import asyncio
import time
from urllib.parse import quote

from .deduplication import Candidate, Signals
from .errors import DependencyUnavailable, ProviderRejected


def literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class Salesforce:
    """OAuth client credentials and async REST. Configure a dedicated integration principal."""

    def __init__(self, http, settings):
        self.http, self.settings = http, settings
        self.token = None
        self.expires = 0.0
        self.lock = asyncio.Lock()

    @property
    def base(self):
        return f"{self.settings.salesforce_url.rstrip('/')}/services/data/{self.settings.salesforce_version}"

    async def headers(self):
        async with self.lock:
            if not self.token or time.monotonic() >= self.expires:
                response = await self.http.request(
                    "salesforce_auth",
                    "POST",
                    self.settings.salesforce_url.rstrip("/") + "/services/oauth2/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.settings.salesforce_client_id.get_secret_value(),
                        "client_secret": self.settings.salesforce_client_secret.get_secret_value(),
                    },
                )
                self.token = response.json()["access_token"]
                self.expires = time.monotonic() + 300
        return {"Authorization": f"Bearer {self.token}"}

    async def request(self, method: str, url: str, **kwargs):
        headers = await self.headers()
        try:
            return await self.http.request("salesforce", method, url, headers=headers, **kwargs)
        except ProviderRejected as exc:
            if exc.status_code != 401:
                raise
            async with self.lock:
                # Do not invalidate a newer token refreshed by another concurrent request.
                if headers["Authorization"] == f"Bearer {self.token}":
                    self.expires = 0
            return await self.http.request("salesforce", method, url, headers=await self.headers(), **kwargs)

    async def lookup(self, signals: Signals) -> list[Candidate]:
        s = self.settings
        fields = [
            "Id",
            s.salesforce_place_field,
            s.salesforce_name_field,
            s.salesforce_address_field,
            s.salesforce_phone_field,
            s.salesforce_external_field,
        ]
        where = (
            f"{s.salesforce_place_field} = {literal(signals.place_id)} OR "
            + f"({s.salesforce_name_field} = {literal(signals.name)} AND "
            + f"{s.salesforce_address_field} = {literal(signals.address)})"
        )
        if signals.phone:
            where += f" OR {s.salesforce_phone_field} = {literal(signals.phone)}"

        async def query(obj):
            response = await self.request(
                "GET",
                self.base + "/query",
                params={"q": f"SELECT {','.join(fields)} FROM {obj} WHERE {where} LIMIT 201"},
            )
            data = response.json()
            if not data.get("done", False) or len(data.get("records", [])) > 200:
                raise DependencyUnavailable("Salesforce duplicate candidate limit exceeded")
            return [
                Candidate(
                    "salesforce",
                    obj,
                    r["Id"],
                    r.get(s.salesforce_place_field),
                    r.get(s.salesforce_name_field),
                    r.get(s.salesforce_address_field),
                    r.get(s.salesforce_phone_field),
                    r.get(s.salesforce_external_field),
                )
                for r in data.get("records", [])
            ]

        groups = await asyncio.gather(query("Lead"), query("Account"))
        return [c for group in groups for c in group]

    async def upsert(self, obj: str, key: str, fields: dict) -> str:
        # This field MUST be Unique + External ID. Stable key survives unknown write outcomes.
        url = f"{self.base}/sobjects/{obj}/{self.settings.salesforce_external_field}/{quote(key, safe='')}"
        await self.request("PATCH", url, json=fields)
        response = await self.request("GET", url)
        return response.json()["Id"]
