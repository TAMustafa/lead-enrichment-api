# Customization guide

Salesforce fields are configured in JSON rather than exposed in the test page. This keeps the user interface small and makes mapping changes reviewable and deployable across all API replicas.

Use:

- `salesforce-mapping-serpapi.json` for the current SerpAPI enrichment flow.
- `salesforce-mapping.json` for the conservative direct-Google profile that stores caller input and identity fields only.

Select the file with `LQ_MAPPING_PATH`. Restart the API after changing it. When persistence is enabled, startup reads Salesforce Describe metadata and rejects incompatible field names, types, lengths, required fields, and picklist values.

## Mapping format

Each object contains Salesforce field API names. A mapping selects one canonical source or a constant value:

```json
{
  "Lead": {
    "Website": {
      "source": "enrichment.website",
      "type": "string",
      "max_length": 255
    },
    "Has_Delivery__c": {
      "source": "enrichment.delivery",
      "type": "boolean"
    },
    "Opening_Hours__c": {
      "source": "enrichment.opening_hours",
      "type": "string",
      "join_with": "\n",
      "max_length": 4000
    },
    "Lead_Enriched__c": {
      "constant": true,
      "type": "boolean"
    }
  }
}
```

Supported mapping types are `string`, `boolean`, `integer`, `number`, `date`, `datetime`, and `multipicklist`. Lists require `join_with` for a string field. Use `allowed_values` for restricted picklists and `value_map` when an input value differs from the Salesforce value.

Null values are omitted. `false` and `0` are preserved. Values are never silently truncated. A mapping failure returns `REVIEW_REQUIRED` with `SALESFORCE_MAPPING_INVALID` and does not write Salesforce.

To stop sending an optional field, remove its entry or set `"enabled": false`. This does not delete the Salesforce field or clear historical records. Identity mappings and Salesforce-required fields must remain enabled.

## Available canonical sources

| Data | Mapping source | Typical Salesforce type |
|---|---|---|
| Business name | `input.business_name` | Text / Company / Name |
| Submitted address | `input.address` | Street or Text |
| Normalized street and house number | `address.street` | Street / Billing Street |
| Postal code | `address.postal_code` | Postal Code |
| City | `address.city` | City |
| Country | `address.country` | Country |
| Submitted source | `input.source` | Picklist |
| Internal ID | `lead_id` | Text(36) |
| Google Place ID | `place_id` | Text |
| Normalized name | `normalized_name` | Text |
| Normalized address | `normalized_address` | Text |
| Normalized phone | `normalized_phone` | Phone or Text |
| Operational status | `enrichment.operational_status` | Text or Picklist |
| Salesforce industry classification | `enrichment.industry` | Standard Industry picklist |
| Business types | `enrichment.business_types` | Long Text with `join_with` |
| Primary cuisine | `enrichment.primary_cuisine` | Text |
| Secondary cuisine | `enrichment.secondary_cuisine` | Text |
| Phone | `enrichment.phone` | Phone |
| Delivery | `enrichment.delivery` | Checkbox |
| Takeaway | `enrichment.takeaway` | Checkbox |
| Dine-in | `enrichment.dine_in` | Checkbox |
| Catering | `enrichment.catering` | Checkbox |
| Accepts reservations | `enrichment.accepts_reservations` | Checkbox |
| Rating | `enrichment.rating` | Number with decimals |
| Review count | `enrichment.review_count` | Integer |
| Provider address | `enrichment.address` | Text |
| Website | `enrichment.website` | URL |
| Menu URL | `enrichment.menu_url` | URL or Description |
| Reservation URL | `enrichment.reservation_url` | URL or Description |
| Latitude | `enrichment.latitude` | Standard Latitude |
| Longitude | `enrichment.longitude` | Standard Longitude |
| Payment methods | `enrichment.payment_options` | Text with `join_with` |
| Research signals | `enrichment.research_signals` | Description/Long Text with `join_with` |
| Opening hours | `enrichment.opening_hours` | Long Text with `join_with` |
| Price range | `enrichment.price_range` | Text |
| Service options | `enrichment.service_options` | Text |
| Latest observed review date | `enrichment.last_review_date` | Date |
| Review-date scope | `enrichment.last_review_date_status` | Text |
| Enrichment timestamp | `enrichment.enriched_at` | DateTime |
| BAG premises result | `enrichment.premises_check_status` | Text or Picklist |
| BAG check timestamp | `enrichment.premises_checked_at` | DateTime |

Provider values can be absent. An omitted nullable checkbox is unknown, even if Salesforce displays an unchecked default. Use `service_options` or add a separate known/unknown field when reporting must distinguish unknown from false.

The supplied SerpAPI profile reuses the org's existing enrichment fields. It writes delivery to `Has_Delivery__c`; takeaway, dine-in, catering, and reservations to `Service_Options__c`; payment methods to `Payment_Options__c`; and detailed offerings, accessibility, parking, menu, and reservation links to the standard `Description` field. It also uses standard `Industry`, `Latitude`, and `Longitude`. Salesforce's standard `Rating` field is intentionally not used for the Google rating because it represents sales priority (`Hot`, `Warm`, or `Cold`).

`Last_Review_Date__c` is populated from the newest original Google review posting date returned by the SerpAPI reviews endpoint. `Review_Date_Status__c` records the scope: `LATEST_RETURNED_POST_DATE` means the latest exact date in the returned review batch. Relative labels and owner-response dates are never substituted. Add these fields and the other desired research fields to a **Restaurant Research** section in your Lead page layout or Dynamic Forms page; the repository does not replace an org's complete layout.

## Adding or removing a Salesforce field

1. Create or identify the Lead or Account field in Salesforce and record its API name, type, length, and picklist choices.
2. Give the integration user read/write field access.
3. Add the entry under `Lead` or `Account` in the selected mapping JSON.
4. Validate JSON, run tests, and restart the API.
5. Submit a new unique lead and inspect `salesforce_fields` in the response.

Duplicate results intentionally do not overwrite existing records. A mapping change also does not backfill historical records.

For a provider value the canonical model does not expose, update the provider adapter, the typed `Enrichment` model, mapping source validation, and contract tests before adding the JSON entry. An arbitrary JSON source cannot make a provider supply new data.

## Lead Source

The supplied SerpAPI mapping translates source values explicitly:

| API source | Standard `LeadSource` | Custom `Lead_Source__c` |
|---|---|---|
| `GRID` | `GRID` | `GRID` |
| `SELF_SIGNUP` | `Self Signup` | `WEB` |

Add a new source to both the mapping and Salesforce picklist. An unmapped or inactive picklist value fails mapping rather than being guessed. Account has no standard Lead Source field.

## Cuisine rules

`src/lead_api/cuisine-rules.json` maps explicit provider type identifiers into application cuisine labels. The classifier never uses an LLM.

Examples:

- `italian_restaurant` plus `pizza_restaurant` produces primary `Italian`, secondary `Pizza`.
- `italian_restaurant` alone produces primary `Italian` and no secondary cuisine.
- Conflicting primary or secondary evidence produces no value at that level and records an ambiguity reason.
- Multiple Asian member cuisines plus an explicit `asian_restaurant` signal produce primary `Asian`; an explicit subtype such as `sushi_restaurant` can independently produce secondary `Sushi`.

Pasta is not inferred merely because a restaurant is Italian. Add or change a rule only when a real provider signal supports the classification. Restart the API after changing the taxonomy.

## Business and address normalization

Input validation applies Unicode NFKC normalization and collapses repeated whitespace in the business name. `normalized_business_name` contains the stable lowercase identity used for duplicate matching while `business_name` keeps its readable capitalization.

Complete Dutch addresses are split deterministically into street (including house number), postal code, city, and country. This uses only native Salesforce address fields. The canonical form uses an uppercase postcode with one space and country `Netherlands`. The API exposes these values in `normalized_address`; Salesforce mappings use the `address.*` sources above. Unknown address formats retain the complete submitted value as the street and leave uncertain components empty rather than guessing.

## Dutch registered-premises check

Set `LQ_RESIDENTIAL_CHECK_ENABLED=true` to check complete Dutch addresses with Kadaster BAG via PDOK. No key is required.

The checker matches street, house number, suffix, postcode, and city before requesting the registered unit. A current unit whose only registered use is `woonfunctie` is rejected with `BAG_RESIDENTIAL_PREMISES`. Explicit non-residential use passes this check. Mixed use, incomplete addresses, foreign addresses, ambiguity, and provider failures return `REVIEW_REQUIRED` with `PREMISES_CHECK_UNCERTAIN`.

BAG describes registered use; it is not proof of current business activity or a physical inspection. Duplicates return before this lookup. Retrying an already stored idempotency key returns its original decision, so use a new source ID when intentionally requalifying after rule changes.

Official references:

- [PDOK BAG APIs](https://www.pdok.nl/ogc-apis/-/article/basisregistratie-adressen-en-gebouwen-ba-1)
- [BAG gebruiksdoel](https://catalogus.kadaster.nl/bag/nl/page/Gebruiksdoel)

## CSV input

The production endpoint accepts raw UTF-8 `text/csv`. Required columns are `source`, `source_lead_id`, `business_name`, and `address`; optional columns are `place_id` and `phone`. Files are limited to 256 KB and 100 rows.

The local page can validate, preview, run, stop, and export a small CSV batch. It accepts `GRID` and `SELF_SIGNUP` and intentionally omits manual enrichment fields. Each row uses the same validation, duplicate checks, qualification, mapping, and write reservation as a single API request.

This is a bounded request/response import, not a durable background job. Use a PostgreSQL-backed import queue around the existing service if scheduled imports later grow to thousands of rows.

## Provider storage guard

Provider availability and permission to retain provider data are separate. `LQ_ALLOW_GOOGLE_CRM_STORAGE` and `LQ_ALLOW_SERPAPI_CRM_STORAGE` are technical guards, not licenses. They default to false. Enable the relevant setting only after confirming the applicable provider agreement and retention design.

Place IDs, internal identity, decisions, and reservations are retained. Raw provider responses are not cached. API responses use `Cache-Control: no-store`.
