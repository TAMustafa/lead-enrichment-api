import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from opentelemetry import trace
from opentelemetry.propagate import extract

from .batch import read_csv_request
from .config import Settings
from .deduplication import Deduplicator
from .enrichment import GooglePlaces
from .errors import Busy, Conflict, DependencyUnavailable
from .mapping import SalesforceMapper
from .models import LeadInput, LeadResponse
from .observability import configure
from .partners import BigQueryPartners
from .rate_limit import PostgresRateLimiter
from .repository import Repository
from .residential import ResidentialChecker
from .resolution import PlaceResolver
from .salesforce import Salesforce
from .serpapi import SerpPlaces
from .service import LeadService
from .status import read_status

logger = logging.getLogger("lead_api")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def authenticate(request: Request, key: str | None = Depends(api_key_header)):
    settings = request.app.state.settings
    identity = None
    for client, expected in settings.api_keys.items():
        if secrets.compare_digest((key or "").encode(), expected.get_secret_value().encode()):
            identity = client
    if identity is None:
        raise HTTPException(401, "Invalid API credentials")
    try:
        allowed = await request.app.state.limiter.allow(identity)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as exc:
        raise HTTPException(503, "Rate limiter unavailable", headers={"Retry-After": "2"}) from exc
    if not allowed:
        raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": "60"})
    request.state.batch_client = identity
    return identity


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        s = settings or Settings(_secrets_dir=os.environ.get("LQ_SECRETS_DIR"))
        app.state.settings = s
        mapper = SalesforceMapper(s)
        telemetry = configure(s.otlp_endpoint)
        pool = await asyncpg.create_pool(
            s.database_url.get_secret_value(), min_size=1, max_size=s.pool_size, command_timeout=5
        )
        try:
            async with httpx.AsyncClient(
                timeout=s.http_timeout_seconds,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
                follow_redirects=False,
            ) as client:
                from .http import ResilientHTTP

                http = ResilientHTTP(client)
                salesforce = Salesforce(http, s)
                google = SerpPlaces(http, s) if s.places_provider == "serpapi" else GooglePlaces(http, s)
                if s.persistence_enabled:
                    from .schema_contract import check_schema

                    await check_schema(salesforce, mapper, s)
                app.state.pool = pool
                app.state.limiter = PostgresRateLimiter(pool, s.rate_limit_per_minute)
                app.state.service = LeadService(
                    Repository(pool, s.fingerprint_key.get_secret_value()),
                    PlaceResolver(google, s),
                    Deduplicator(salesforce, BigQueryPartners(http, s)),
                    google,
                    mapper,
                    salesforce,
                    s,
                    residential_checker=ResidentialChecker(http),
                )
                yield
        finally:
            await pool.close()
            await asyncio.to_thread(telemetry.shutdown)

    app = FastAPI(title="Lead Qualification API", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request, call_next):
        request.state.request_id = str(uuid4())
        started = time.monotonic()
        with trace.get_tracer(__name__).start_as_current_span(
            "http.request", context=extract(dict(request.headers))
        ) as span:
            # No input, Place ID, query string, or credentials are logged/traced.
            span.set_attribute("http.request.method", request.method)
            response = await call_next(request)
            span.set_attribute("http.response.status_code", response.status_code)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        logger.info(
            "request_completed",
            extra={
                "request_id": request.state.request_id,
                "status": response.status_code,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Pydantic error defaults include raw input; redact that from responses and logs.
        return JSONResponse(
            status_code=422,
            content={"detail": [{"loc": e["loc"], "type": e["type"], "msg": e["msg"]} for e in exc.errors()]},
        )

    @app.post("/v1/leads/qualify", response_model=LeadResponse, dependencies=[Depends(authenticate)])
    async def qualify_lead(lead: LeadInput, request: Request):
        try:
            async with asyncio.timeout(request.app.state.settings.request_timeout_seconds):
                return await request.app.state.service.process(lead, request.state.request_id)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (Busy, DependencyUnavailable, TimeoutError, asyncpg.PostgresError) as exc:
            raise HTTPException(
                503, "Processing incomplete; retry the identical request", headers={"Retry-After": "2"}
            ) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(502, "Invalid upstream response") from exc

    @app.post("/v1/leads/qualify/csv", dependencies=[Depends(authenticate)])
    async def qualify_csv(request: Request):
        try:
            leads = await read_csv_request(request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        async def stream():
            # Sequential, bounded work: normal locks, identities and write recovery apply to every row.
            for index, lead in enumerate(leads, 1):
                try:
                    if not await request.app.state.limiter.allow(request.state.batch_client):
                        yield json.dumps({"row": index, "error": "RATE_LIMITED", "retryable": True}) + "\n"
                        continue
                    async with asyncio.timeout(request.app.state.settings.request_timeout_seconds):
                        result = await request.app.state.service.process(lead, str(uuid4()))
                    event = {
                        "row": index,
                        "source_lead_id": lead.source_lead_id,
                        "result": result.model_dump(mode="json"),
                    }
                except Conflict:
                    event = {"row": index, "error": "IDEMPOTENCY_CONFLICT", "retryable": False}
                except Exception:
                    event = {"row": index, "error": "PROCESSING_INCOMPLETE", "retryable": True}
                yield json.dumps(event) + "\n"

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.get("/v1/leads/{lead_id}/status", dependencies=[Depends(authenticate)])
    async def lead_status(lead_id: UUID, request: Request):
        return await read_status(request.app.state.pool, lead_id, request.app.state.settings.salesforce_url)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request):
        try:
            async with asyncio.timeout(3):
                async with request.app.state.pool.acquire() as connection:
                    version = await connection.fetchval("SELECT max(version) FROM schema_migrations")
                    if version != 1:
                        raise RuntimeError("Migration required")
        except Exception as exc:
            raise HTTPException(503, "Not ready") from exc
        return {"status": "ready"}

    return app


app = create_app()
