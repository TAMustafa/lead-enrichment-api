"""Local-only SerpAPI/BAG/Salesforce development interface."""

import asyncio
import json
import os
import secrets
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .batch import read_csv_request
from .config import Settings
from .deduplication import Deduplicator
from .errors import Busy, Conflict, DependencyUnavailable
from .http import ResilientHTTP
from .mapping import SalesforceMapper
from .models import LeadInput
from .rate_limit import PostgresRateLimiter
from .repository import Repository
from .residential import ResidentialChecker
from .resolution import PlaceResolver
from .salesforce import Salesforce
from .schema_contract import check_schema
from .serpapi import SerpPlaces, SerpSettings
from .service import LeadService

ROOT = Path(__file__).resolve().parents[2]
HTML = Path(__file__).parent / "static" / "demo.html"
SERP_ENV = ROOT / ".env.serpapi"


class DemoEnvironment(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LQ_", env_file=SERP_ENV, extra="ignore")
    demo_salesforce_org: str = ""


class DemoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_name: str = Field(min_length=1, max_length=250)
    address: str = Field(min_length=3, max_length=255)
    source: Literal["GRID", "SELF_SIGNUP"] = "GRID"
    source_lead_id: str = Field(min_length=1, max_length=200)
    target: Literal["Lead", "Account"] = "Lead"

    @model_validator(mode="after")
    def normalize_input(self):
        lead = LeadInput(
            source=self.source,
            source_lead_id=self.source_lead_id,
            business_name=self.business_name,
            address=self.address,
        )
        self.business_name, self.address = lead.business_name, lead.address
        return self


class DemoPartners:
    async def lookup(self, signals):
        return []


class DemoSalesforce(Salesforce):
    """Existing CLI login pinned locally on first use. No credentials reach the browser."""

    def __init__(self, http, settings):
        super().__init__(http, settings)
        self.target_org = DemoEnvironment().demo_salesforce_org.strip()
        self.pin_path = ROOT / ".demo-data" / "salesforce-org.json"

    async def headers(self):
        async with self.lock:
            if not self.token or time.monotonic() >= self.expires:
                if not self.target_org:
                    raise DependencyUnavailable("Set LQ_DEMO_SALESFORCE_ORG to a development-org CLI alias")
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["sf", "org", "display", "--target-org", self.target_org, "--json"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=dict(os.environ, SF_DISABLE_AUTOUPDATE="true", SF_DISABLE_TELEMETRY="true"),
                )
                if result.returncode:
                    raise DependencyUnavailable("The configured Salesforce CLI login is unavailable")
                auth = json.loads(result.stdout)["result"]
                identity = {"id": auth["id"], "instance_url": auth["instanceUrl"].rstrip("/")}
                try:
                    fd = os.open(self.pin_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pinned = json.loads(self.pin_path.read_text())
                else:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(identity, stream)
                    pinned = identity
                if pinned != identity:
                    raise DependencyUnavailable("Refusing to access a different Salesforce org")
                self.settings.salesforce_url = identity["instance_url"]
                self.token, self.expires = auth["accessToken"], time.monotonic() + 300
        return {"Authorization": f"Bearer {self.token}"}


def create_demo_app(settings=None, pool=None, salesforce=None, places=None, partners=None):
    @asynccontextmanager
    async def lifespan(app):
        secret_path = ROOT / ".demo-data" / "fingerprint.key"
        secret_path.parent.mkdir(exist_ok=True, mode=0o700)
        try:
            fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(48))
        s = settings or Settings(
            _env_file=None,
            database_url=os.environ["LQ_DEMO_DATABASE_URL"],
            api_keys={"demo": secrets.token_urlsafe(36)},
            fingerprint_key=secret_path.read_text(),
            google_api_key="unused",
            salesforce_url="https://example.invalid",
            salesforce_version="v67.0",
            salesforce_client_id="unused",
            salesforce_client_secret="unused",
            bigquery_project="unused",
            bigquery_table="unused.dataset.table",
            mapping_path=str(ROOT / "salesforce-mapping-serpapi.json"),
            places_provider="serpapi",
            allow_serpapi_crm_storage=True,
            persistence_enabled=True,
            allow_google_crm_storage=False,
            source_routes={},
            residential_check_enabled=True,
        )
        db = pool or await asyncpg.create_pool(s.database_url.get_secret_value(), min_size=1, max_size=10)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                sf = salesforce or DemoSalesforce(ResilientHTTP(client), s)
                if salesforce is None:
                    await sf.headers()
                await check_schema(sf, SalesforceMapper(s), s)
                app.state.settings, app.state.pool, app.state.salesforce = s, db, sf
                app.state.provider_http = ResilientHTTP(client)
                app.state.places = places
                app.state.partners = partners or DemoPartners()
                app.state.limiter = PostgresRateLimiter(db, 30)
                app.state.token = secrets.token_urlsafe(32)
                yield
        finally:
            if pool is None:
                await db.close()

    app = FastAPI(title="Lead Qualification Demo", lifespan=lifespan)
    app.mount("/demo/static", StaticFiles(directory=HTML.parent), name="demo-static")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def local_only(request, call_next):
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            return JSONResponse(
                status_code=403, content={"detail": "Demo is available only on this computer"}
            )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/demo", response_class=HTMLResponse)
    async def page(request: Request):
        content = await asyncio.to_thread(HTML.read_text)
        return content.replace("__DEMO_CSRF_TOKEN__", request.app.state.token)

    @app.get("/health/live")
    async def live(request: Request):
        return {"status": "ok", "mode": "serpapi-bag-salesforce-development"}

    def authorize(request):
        if request.headers.get("origin") not in {"http://127.0.0.1:8765", "http://localhost:8765"}:
            raise HTTPException(403, "Use the local demo page")
        if not secrets.compare_digest(request.headers.get("X-Demo-Token", ""), request.app.state.token):
            raise HTTPException(403, "Reload the demo page")

    @app.post("/demo/api/csv/preview")
    async def preview_csv(request: Request):
        authorize(request)
        try:
            leads = await read_csv_request(request)
            if any(lead.source not in {"GRID", "SELF_SIGNUP"} for lead in leads):
                raise ValueError("Demo CSV source must be GRID or SELF_SIGNUP")
            if any(lead.phone or lead.place_id for lead in leads):
                raise ValueError(
                    "Demo CSV does not accept phone or place_id; use the production CSV endpoint"
                )
            if any(len(lead.address) > 255 or len(lead.source_lead_id) > 150 for lead in leads):
                raise ValueError("Demo addresses: max 255 characters; source_lead_id: max 150")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"rows": [lead.model_dump(mode="json") for lead in leads]}

    @app.post("/demo/api/qualify")
    async def run(data: DemoInput, request: Request):
        authorize(request)
        if not await request.app.state.limiter.allow("local-demo"):
            raise HTTPException(429, "Demo limit reached; wait one minute")
        lead = LeadInput(
            source=data.source,
            source_lead_id=data.source_lead_id,
            business_name=data.business_name,
            address=data.address,
        )
        # The target is part of demo idempotency because routing cannot change across retries.
        lead = lead.model_copy(update={"source_lead_id": f"{data.source_lead_id}:{data.target}"})
        s = request.app.state.settings.model_copy(update={"route": data.target})
        google = request.app.state.places
        if google is None:
            config = await asyncio.to_thread(SerpSettings, _env_file=SERP_ENV)
            if not config.serpapi_api_key.get_secret_value().strip():
                raise HTTPException(
                    503, "Paste LQ_SERPAPI_API_KEY into .env.serpapi, then retry. No restart needed."
                )
            google = SerpPlaces(request.app.state.provider_http, config)
        service = LeadService(
            Repository(request.app.state.pool, s.fingerprint_key.get_secret_value()),
            PlaceResolver(google, s),
            Deduplicator(request.app.state.salesforce, request.app.state.partners),
            google,
            SalesforceMapper(s),
            request.app.state.salesforce,
            s,
            residential_checker=(
                ResidentialChecker(request.app.state.provider_http) if s.residential_check_enabled else None
            ),
        )
        queue = asyncio.Queue()

        def emit(event):
            queue.put_nowait({"type": "step", **event})

        async def process():
            try:
                emit({"stage": "validation", "state": "done", "detail": "Input validated and normalized"})
                async with asyncio.timeout(60):
                    result = await service.process(lead, str(uuid4()), observer=emit)
                record_id = result.salesforce_record_id
                obj = result.salesforce_object
                if not record_id:
                    match = next(
                        (
                            e
                            for e in result.existing_entities
                            if e.system == "salesforce" or e.system == "internal"
                        ),
                        None,
                    )
                    if match:
                        record_id, obj = match.entity_id, match.entity_type
                queue.put_nowait(
                    {
                        "type": "result",
                        "result": result.model_dump(mode="json"),
                        "salesforce_url": f"{s.salesforce_url}/lightning/r/{obj}/{record_id}/view"
                        if record_id and obj in {"Lead", "Account"}
                        else None,
                    }
                )
            except (Busy, DependencyUnavailable, TimeoutError) as exc:
                queue.put_nowait(
                    {
                        "type": "error",
                        "message": "Processing incomplete. Retry this request.",
                        "code": type(exc).__name__,
                    }
                )
            except Conflict:
                queue.put_nowait(
                    {"type": "error", "message": "Input changed. Start a new request.", "code": "CONFLICT"}
                )
            except Exception:
                queue.put_nowait(
                    {
                        "type": "error",
                        "message": "Demo processing failed; check local service logs.",
                        "code": "ERROR",
                    }
                )
            finally:
                queue.put_nowait(None)

        async def stream():
            task = asyncio.create_task(process())
            try:
                while (event := await queue.get()) is not None:
                    yield json.dumps(event) + "\n"
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    return app


app = create_demo_app()
