# Lead Qualification API

A deterministic FastAPI service that accepts restaurant and grocery leads, checks for duplicates, enriches unique businesses, qualifies them, and optionally creates a Salesforce Lead or Account.

The service is a modular monolith. PostgreSQL provides idempotency, audit metadata, write reservations, distributed locks, and rate limits. Redis is not required. Provider calls use async HTTP. No LLM participates in matching, deduplication, or qualification.

## API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/leads/qualify` | Qualify one lead and apply the configured Salesforce routing rule |
| `POST` | `/v1/leads/qualify/csv` | Process up to 100 UTF-8 CSV rows; returns NDJSON |
| `GET` | `/v1/leads/{lead_id}/status` | Read a stored result without rerunning providers or Salesforce writes |
| `GET` | `/health/live` | Process liveness |
| `GET` | `/health/ready` | PostgreSQL and migration readiness |

OpenAPI is available at `/docs` and `/openapi.json`. The three lead endpoints require `X-API-Key`.

## Request one lead

```bash
curl http://127.0.0.1:8000/v1/leads/qualify \
  -H "X-API-Key: $LEAD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "GRID",
    "source_lead_id": "grid-12345",
    "business_name": "Example Restaurant",
    "address": "Palmpolstraat 1, 1327 CA Almere"
  }'
```

`place_id` and `phone` are optional. Supply a known Google Place ID to skip place resolution. Phone input must include an international country code.

`source` plus `source_lead_id` is the idempotency key. Retrying identical input returns the stored decision. Reusing that key with changed input returns HTTP `409`.

An abridged response looks like this (the real `normalized_enrichment` object also includes nullable canonical fields and evidence arrays):

```json
{
  "lead_id": "8ee7603e-25c5-4a0a-b0d0-22a3ff5a2528",
  "business_name": "Example Restaurant",
  "normalized_business_name": "example restaurant",
  "source": "GRID",
  "source_lead_id": "grid-12345",
  "place_id": "ChIJ...",
  "normalized_address": {
    "full": "Palmpolstraat 1, 1327 CA Almere, Netherlands",
    "street": "Palmpolstraat 1",
    "postal_code": "1327 CA",
    "city": "Almere",
    "country": "Netherlands",
    "identity": "palmpolstraat 1 1327 ca almere netherlands"
  },
  "duplicate_status": "CLEAR",
  "existing_entity_id": null,
  "existing_entities": [],
  "qualification_status": "QUALIFIED",
  "qualification_reason_codes": ["ELIGIBILITY_CONFIRMED"],
  "normalized_enrichment": {
    "operational_status": "OPERATIONAL",
    "business_types": ["restaurant", "italian_restaurant"],
    "primary_cuisine": "Italian",
    "secondary_cuisine": null,
    "industry": "Hospitality",
    "phone": "+31361234567",
    "delivery": true,
    "takeaway": null,
    "dine_in": true,
    "rating": 4.4,
    "review_count": 250,
    "last_review_date": "2026-09-26",
    "last_review_date_status": "LATEST_RETURNED_POST_DATE",
    "address": "Palmpolstraat 1, 1327 CA Almere",
    "website": "https://example.invalid",
    "opening_hours": [],
    "premises_check_status": "NON_RESIDENTIAL"
  },
  "confidence": 0.98,
  "salesforce_record_id": "00Q...",
  "salesforce_object": "Lead",
  "salesforce_fields": {},
  "processing_metadata": {
    "request_id": "request-id",
    "rules_version": "2026-09-26.3",
    "duration_ms": 820,
    "idempotent_replay": false,
    "enrichment_omitted_on_replay": false,
    "persistence_status": "WRITTEN"
  }
}
```

Possible decisions are `QUALIFIED`, `REJECTED`, `REVIEW_REQUIRED`, and `DUPLICATE`. `confidence` is the deterministic place-resolution score, not a qualification probability.

## Request a CSV batch

```bash
curl http://127.0.0.1:8000/v1/leads/qualify/csv \
  -H "X-API-Key: $LEAD_API_KEY" \
  -H "Content-Type: text/csv" \
  --data-binary @leads.csv
```

Required headers are `source,source_lead_id,business_name,address`. Optional headers are `place_id,phone`.

```csv
source,source_lead_id,business_name,address
GRID,grid-1001,Example Restaurant,"Palmpolstraat 1, 1327 CA Almere"
SELF_SIGNUP,web-1002,Example Market,"Stationsplein 10, 1012 AB Amsterdam"
```

The entire file is validated before processing. Limits are 256 KB and 100 rows. The response is newline-delimited JSON with one result or error per row. Successful rows are not rolled back if a later row fails. Stable source IDs make an unchanged retry safe.

## Read a result

```bash
curl http://127.0.0.1:8000/v1/leads/8ee7603e-25c5-4a0a-b0d0-22a3ff5a2528/status \
  -H "X-API-Key: $LEAD_API_KEY"
```

This is a read-only PostgreSQL lookup. It does not call SerpAPI, Google, BigQuery, or Salesforce.

## Local test page

The local page uses a configured SerpAPI key, an existing Salesforce CLI login for a development org, an embedded private PostgreSQL instance, and a simulated existing-partner source. It must never target production.

Create an owner-only `.env.serpapi` file:

```dotenv
LQ_SERPAPI_API_KEY=your_key_here
LQ_SERPAPI_COUNTRY=nl
LQ_DEMO_SALESFORCE_ORG=your-dev-org-alias
```

Then run:

```bash
uv sync --frozen
uv run python scripts/run_demo.py
```

Open <http://127.0.0.1:8765/demo>. Enter a business name and complete Dutch address. The page keeps optional routing, processing details, and CSV import collapsed. Only qualified, unique businesses are written to the development org. On first use, the demo stores the org ID and instance URL in ignored local state and refuses to use a different org later.

## Run the production API locally

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker, Salesforce OAuth credentials, a provider key, and BigQuery Application Default Credentials.

```bash
uv sync --frozen
uv run python scripts/init_dev_env.py
# Fill the provider, Salesforce, and BigQuery placeholders in .env.
docker compose up -d postgres
docker compose run --rm migrate
uv run uvicorn lead_api.api:app --host 127.0.0.1 --port 8000 --no-access-log
```

Or run the complete stack with `docker compose up --build` after configuring `.env`.

The example configuration uses SerpAPI for lookup and enrichment but selects the conservative Salesforce mapping, which stores caller input and identity fields only. To retain SerpAPI enrichment in Salesforce, confirm the applicable provider terms, select `salesforce-mapping-serpapi.json`, and set `LQ_ALLOW_SERPAPI_CRM_STORAGE=true`. Set `LQ_PLACES_PROVIDER=google` and provide `LQ_GOOGLE_API_KEY` to use the direct Google Places adapter. Enable `LQ_PERSISTENCE_ENABLED` only after Salesforce fields and permissions have been configured. Startup validates the Salesforce schema whenever persistence is enabled.

Production secrets should come from the hosting platform's secret manager. The app also accepts a mounted secrets directory through `LQ_SECRETS_DIR`. Use workload identity or ADC for BigQuery rather than a downloaded long-lived key.

## Processing order

1. Validate and normalize input.
2. Establish the immutable internal `lead_id` and idempotency record in PostgreSQL.
3. Resolve a missing Place ID using conservative name/address scoring.
4. Lock the Place ID and query Salesforce Leads, Salesforce Accounts, and the partner source before enrichment.
5. Return immediately when an existing entity is found.
6. Enrich unique businesses and normalize provider data into the canonical schema.
7. Optionally check the exact Dutch address against Kadaster BAG registered use.
8. Apply deterministic qualification rules.
9. Validate explicit Salesforce field mappings.
10. Reserve the write and upsert Salesforce using a stable external key.

Uncertain place matches, conflicting duplicate signals, missing critical enrichment, ambiguous BAG results, and mapping incompatibilities return `REVIEW_REQUIRED`. Provider or database failures return an error and never become a false clearance.

## Project structure

| File | Responsibility |
|---|---|
| `resolution.py` | Deterministic Place ID candidate selection |
| `deduplication.py`, `partners.py` | Salesforce and existing-partner duplicate policy |
| `enrichment.py`, `serpapi.py` | Direct Google and SerpAPI adapters plus canonical normalization |
| `address.py`, `residential.py` | Canonical address parsing and exact Dutch BAG registered-use lookup |
| `qualification.py`, `cuisine.py` | Deterministic qualification and cuisine rules |
| `mapping.py`, `schema_contract.py` | Typed Salesforce mapping and startup validation |
| `salesforce.py` | OAuth, duplicate queries, and stable-key persistence |
| `repository.py` | PostgreSQL identity, locks, reservations, and audit |
| `service.py` | Ordered workflow orchestration |
| `api.py`, `batch.py`, `status.py` | HTTP, CSV, and status endpoints |
| `demo.py`, `static/` | Local development interface |

Salesforce field configuration and BAG/cuisine behavior are documented in [CUSTOMIZATION.md](CUSTOMIZATION.md). Development-org metadata and deployment steps are in [salesforce/README.md](salesforce/README.md).

## Why PostgreSQL remains

PostgreSQL is required for behavior that external providers cannot safely supply: immutable internal identity, idempotent retries, cross-replica locks, permanent pre-write reservations, audit metadata, and rate limits. Without shared durable state, concurrent requests can both pass Salesforce deduplication before either record becomes visible. Redis is unnecessary for the current design.

All replicas must share the same PostgreSQL primary and stable fingerprint secret. Use direct PostgreSQL or session pooling because advisory locks are session-scoped.

## Tests

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

PostgreSQL integration tests run when `TEST_DATABASE_URL` is set. Salesforce live tests are explicitly opt-in and require the expected development-org ID as a write-safety pin:

```bash
LIVE_SALESFORCE_ORG=your-dev-org-alias \
LIVE_SALESFORCE_ORG_ID=00Dxxxxxxxxxxxxxxx \
TEST_DATABASE_URL=postgresql://lead:lead@localhost:5432/leads \
uv run pytest -q -m live
```

## Operational boundaries

- Salesforce and BigQuery are checked before enrichment. Negative duplicate results are never cached.
- PostgreSQL reservations prevent duplicate writes among cooperating API replicas. Salesforce duplicate rules should also protect against unrelated imports and UI users.
- BigQuery is isolated behind `DuplicateSource.lookup` so it can be replaced by a lower-latency index.
- Google/SerpAPI data storage requires an appropriate contractual basis. Storage guards default to false.
- BAG checks accept exact complete Dutch addresses. Residential-only registered use rejects; mixed, incomplete, foreign, unavailable, or ambiguous results require review when enabled.
- Structured logs omit payloads and credentials. OpenTelemetry export is enabled with `LQ_OTLP_ENDPOINT`.
