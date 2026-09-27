import json
from unittest.mock import AsyncMock

import httpx
import pytest

from lead_api.deduplication import Signals
from lead_api.enrichment import DETAIL_MASK, GooglePlaces
from lead_api.errors import DependencyUnavailable
from lead_api.http import ResilientHTTP
from lead_api.partners import BigQueryPartners
from lead_api.salesforce import Salesforce, literal


async def test_places_masks_and_normalization(settings):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"places": [{"id": "one"}]})
        return httpx.Response(
            200,
            json={
                "id": "one",
                "businessStatus": "OPERATIONAL",
                "takeout": False,
                "types": ["restaurant"],
                "userRatingCount": 0,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        google = GooglePlaces(ResilientHTTP(client), settings)
        assert await google.search("Bella Pizza", "10 Main") == [{"id": "one"}]
        result = await google.enrich("one")
        assert result.takeaway is False and result.review_count == 0
    assert requests[1].headers["X-Goog-FieldMask"] == DETAIL_MASK
    assert "*" not in DETAIL_MASK and "reviews" not in DETAIL_MASK
    assert json.loads(requests[0].content)["textQuery"] == "Bella Pizza, 10 Main"


async def test_salesforce_queries_both_objects_and_stable_upsert(settings):
    requests = []

    def handler(request):
        requests.append(request)
        if "oauth2" in request.url.path:
            return httpx.Response(200, json={"access_token": "token"})
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"done": True, "records": []})
        if request.method == "PATCH":
            return httpx.Response(204)
        return httpx.Response(200, json={"Id": "00Q123"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sf = Salesforce(ResilientHTTP(client), settings)
        assert await sf.lookup(Signals("place", "o'reilly", "address", None)) == []
        assert await sf.upsert("Lead", "stable-key", {"Company": "test"}) == "00Q123"
    queries = [r.url.params["q"] for r in requests if r.url.path.endswith("/query")]
    assert any("FROM Lead" in q for q in queries) and any("FROM Account" in q for q in queries)
    assert all("o\\'reilly" in q for q in queries)
    writes = [r for r in requests if r.method == "PATCH"]
    assert writes[0].url.path.endswith("/Lead/Qualification_Key__c/stable-key")


def test_soql_escaping():
    assert literal("a' OR Name != '") == "'a\\' OR Name != \\' '".replace("\\' '", "\\''")


async def test_bigquery_parameter_contract(settings):
    def handler(request):
        body = json.loads(request.content)
        assert "@place" in body["query"] and "malicious'" not in body["query"]
        assert body["useLegacySql"] is False
        assert body["queryParameters"][0]["parameterValue"]["value"] == "malicious'"
        return httpx.Response(
            200,
            json={
                "jobComplete": True,
                "totalRows": "1",
                "rows": [
                    {"f": [{"v": "partner"}, {"v": "malicious'"}, {"v": None}, {"v": None}, {"v": None}]}
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        partners = BigQueryPartners(ResilientHTTP(client), settings)
        partners.token = AsyncMock(return_value="token")
        rows = await partners.lookup(Signals("malicious'", "name", "addr", None))
        assert rows[0].entity_id == "partner"


async def test_retry_and_circuit_breaker():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        http = ResilientHTTP(client)
        for _ in range(4):
            with pytest.raises(DependencyUnavailable):
                await http.request("test", "GET", "https://example.com")
    assert count == 9


async def test_no_retry_for_auth_error():
    client = AsyncMock()
    client.request.return_value = httpx.Response(403)
    with pytest.raises(DependencyUnavailable):
        await ResilientHTTP(client).request("provider", "GET", "https://example.com")
    assert client.request.await_count == 1


async def test_salesforce_refreshes_expired_token_once(settings):
    tokens = 0

    def handler(request):
        nonlocal tokens
        if "oauth2" in request.url.path:
            tokens += 1
            return httpx.Response(200, json={"access_token": str(tokens)})
        if request.headers["Authorization"] == "Bearer 1":
            return httpx.Response(401)
        return httpx.Response(200, json={"done": True, "records": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sf = Salesforce(ResilientHTTP(client), settings)
        assert await sf.lookup(Signals("place", "name", "addr", None)) == []
    assert tokens == 2
