# Salesforce development-org integration

The metadata project targets Salesforce API version 67.0. Use a non-production org authorized in the Salesforce CLI and replace `your-dev-org-alias` in the commands below.

The bundle provides the identity fields required for deterministic matching, race-safe upserts, and idempotency, plus the Lead enrichment fields used by `salesforce-mapping-serpapi.json`. Address output uses native Salesforce fields: `Street`, `City`, `PostalCode`, and `Country` for Lead, and their `Billing*` equivalents for Account. Street includes the house number.

`Lead_Qualification_API` grants field-level access to every custom field used by the API; assign it to the integration user after deployment. The integration user must already have Lead and Account object access through its profile or another permission set.

From this directory:

```sh
# Validate without applying changes.
sf project deploy start --dry-run --source-dir force-app \
  --target-org your-dev-org-alias --wait 10

# Apply after reviewing the validation result.
sf project deploy start --source-dir force-app \
  --target-org your-dev-org-alias --wait 10

# Grant API field access to the integration user.
sf org assign permset --name Lead_Qualification_API \
  --target-org your-dev-org-alias
```

Then, from the API project root:

```sh
uv run python scripts/check_salesforce_org.py --target-org your-dev-org-alias
uv run python scripts/check_salesforce_org.py --target-org your-dev-org-alias \
  --mapping-path salesforce-mapping-serpapi.json --provider serpapi
```

The checker reads current field metadata and validates API names, types, sizes, permissions, required fields, picklists, and external-ID uniqueness. It never prints or copies CLI credentials and creates no records.

Page layouts are intentionally excluded because deploying a complete layout can overwrite an org's existing arrangement and may depend on unrelated fields. In Salesforce Setup, add the desired API fields to the Lead page layout or Dynamic Forms page. A useful **Restaurant Research** section contains rating, review count, last review date and status, delivery, service and payment options, cuisines, price, business status, enrichment time, and opening hours.

Production authentication uses OAuth client credentials supplied through a secret manager. CLI authorization is limited to local schema checks, metadata deployment, the explicitly local demo, and opt-in live tests.

Live tests require both an alias and the expected 18-character org ID, preventing accidental writes to a different org:

```sh
LIVE_SALESFORCE_ORG=your-dev-org-alias \
LIVE_SALESFORCE_ORG_ID=00Dxxxxxxxxxxxxxxx \
TEST_DATABASE_URL=postgresql://lead:lead@localhost:5432/leads \
uv run pytest -q -m live
```

The tests create UUID-marked Lead and Account fixtures, verify qualification, replay, duplicate handling, and stable-key upserts, then delete only fixtures whose identity fields match. They use synthetic provider and partner adapters and do not call Google Places, SerpAPI, or BigQuery.
