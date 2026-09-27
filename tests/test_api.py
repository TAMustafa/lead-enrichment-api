from unittest.mock import AsyncMock

import httpx

from lead_api.api import create_app
from lead_api.errors import Conflict


async def test_api_auth_validation_conflict(settings, lead):
    app = create_app(settings)
    app.state.settings = settings
    app.state.limiter = AsyncMock()
    app.state.limiter.allow.return_value = True
    app.state.service = AsyncMock()
    app.state.service.process.side_effect = Conflict("conflict")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.post("/v1/leads/qualify", json=lead.model_dump())).status_code == 401
        headers = {"X-API-Key": "x" * 32}
        invalid = await client.post("/v1/leads/qualify", headers=headers, json={"source": "grid"})
        assert invalid.status_code == 422
        assert "input" not in invalid.json()["detail"][0]
        response = await client.post("/v1/leads/qualify", headers=headers, json=lead.model_dump())
        assert response.status_code == 409
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Request-ID"]
        app.state.limiter.allow.return_value = False
        assert (
            await client.post("/v1/leads/qualify", headers=headers, json=lead.model_dump())
        ).status_code == 429
