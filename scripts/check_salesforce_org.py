"""Read-only live schema verification through an existing Salesforce CLI authorization.

Run: uv run python scripts/check_salesforce_org.py --target-org your-dev-org-alias
CLI credentials stay in memory and are never printed or copied into project files.
"""

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

from lead_api.config import Settings
from lead_api.http import ResilientHTTP
from lead_api.mapping import SalesforceMapper
from lead_api.salesforce import Salesforce
from lead_api.schema_contract import validate_describe

ROOT = Path(__file__).resolve().parents[1]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-org", required=True)
    parser.add_argument("--mapping-path", default="salesforce-mapping.json")
    parser.add_argument("--provider", choices=("google", "serpapi"), default="google")
    args = parser.parse_args()
    result = await asyncio.to_thread(
        subprocess.run,
        ["sf", "org", "display", "--target-org", args.target_org, "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        env=dict(os.environ, SF_DISABLE_AUTOUPDATE="true", SF_DISABLE_TELEMETRY="true"),
    )
    try:
        output = json.loads(result.stdout)
        if result.returncode or output.get("status"):
            raise ValueError("CLI authorization unavailable")
        auth = output["result"]
        origin = auth["instanceUrl"]
        token = auth["accessToken"]
    except (ValueError, KeyError) as exc:
        raise SystemExit("Could not read CLI authorization. Run sf org login web for this org.") from exc
    # Unused provider credentials are placeholders, not real credentials. Only Salesforce schema is read.
    settings = Settings(
        _env_file=None,
        database_url="postgresql://unused/unused",
        api_keys={"check": "x" * 32},
        fingerprint_key="x" * 32,
        google_api_key="unused",
        salesforce_url=origin,
        salesforce_client_id="unused",
        salesforce_client_secret="unused",
        bigquery_project="unused",
        bigquery_table="unused.dataset.table",
        mapping_path=str(ROOT / args.mapping_path),
        places_provider=args.provider,
        allow_serpapi_crm_storage=args.provider == "serpapi",
    )
    mapper = SalesforceMapper(settings)
    failures = []
    async with httpx.AsyncClient(timeout=10) as client:
        salesforce = Salesforce(ResilientHTTP(client), settings)
        salesforce.token, salesforce.expires = token, time.monotonic() + 60
        for obj in ("Lead", "Account"):
            response = await salesforce.request("GET", f"{salesforce.base}/sobjects/{obj}/describe")
            describe = response.json()
            try:
                validate_describe(obj, describe, mapper, settings)
                print(f"{obj}: schema contract passed")
            except ValueError as exc:
                failures.append(f"{obj}: {exc}")
                print(failures[-1])
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
