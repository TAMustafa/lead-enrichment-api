import os
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from lead_api.config import Settings
from lead_api.models import LeadInput
from lead_api.repository import Repository


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="postgresql://lead:lead@localhost/leads",
        api_keys={"test": "x" * 32},
        fingerprint_key="s" * 32,
        google_api_key="google-secret",
        salesforce_url="https://example.my.salesforce.com",
        salesforce_client_id="client",
        salesforce_client_secret="secret",
        bigquery_project="test-project",
        bigquery_table="test-project.partners.existing",
        mapping_path=str(Path(__file__).resolve().parents[1] / "salesforce-mapping.json"),
    )


@pytest.fixture
def lead():
    return LeadInput(
        source="grid",
        source_lead_id="123",
        business_name="Bella Pizza",
        address="10 Main Street Amsterdam",
        place_id="place-123",
    )


@pytest.fixture
def empty_source():
    source = AsyncMock()
    source.lookup.return_value = []
    return source


@pytest.fixture
async def repository():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for real PostgreSQL tests")
    pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
    schema = "test_" + uuid4().hex
    async with pool.acquire() as connection:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
    await pool.close()
    pool = await asyncpg.create_pool(url, min_size=1, max_size=5, server_settings={"search_path": schema})
    async with pool.acquire() as connection:
        await connection.execute((Path(__file__).parents[1] / "migrations/001_initial.sql").read_text())
    yield Repository(pool, "s" * 32)
    async with pool.acquire() as connection:
        await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await pool.close()
