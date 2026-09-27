import json

import pytest

from lead_api.mapping import SalesforceMapper
from lead_api.schema_contract import validate_describe


def test_explicit_mapping_and_null_omission(settings, lead):
    mapper = SalesforceMapper(settings)
    fields = mapper.map(
        "Lead",
        {
            "input.business_name": lead.business_name,
            "place_id": lead.place_id,
            "normalized_name": "bella pizza",
        },
    )
    assert fields["Company"] == "Bella Pizza"
    assert fields["LastName"] == "Business inquiry"
    assert "Phone" not in fields
    assert "businessStatus" not in fields
    with pytest.raises(ValueError, match="Missing required"):
        mapper.map("Lead", {})


def test_mapping_does_not_truncate(settings):
    with pytest.raises(ValueError, match="too long"):
        SalesforceMapper(settings).map("Account", {"input.business_name": "x" * 256})


def test_google_storage_disabled(settings, tmp_path):
    data = json.loads(open(settings.mapping_path).read())
    data["Lead"]["Rating__c"] = {"source": "enrichment.rating", "type": "number"}
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(data))
    settings.mapping_path = str(path)
    with pytest.raises(ValueError, match="contractual"):
        SalesforceMapper(settings)


def test_salesforce_external_id_must_be_unique(settings):
    with pytest.raises(ValueError, match="Unique External ID"):
        validate_describe(
            "Lead",
            {"createable": True, "updateable": True, "fields": []},
            SalesforceMapper(settings),
            settings,
        )


def test_serpapi_profile_requires_explicit_provider_opt_in(settings):
    from datetime import UTC, datetime
    from pathlib import Path

    from lead_api.serpapi import normalize_serp_place

    settings.mapping_path = str(Path(settings.mapping_path).with_name("salesforce-mapping-serpapi.json"))
    with pytest.raises(ValueError, match="contractual"):
        SalesforceMapper(settings)
    settings.places_provider = "serpapi"
    settings.allow_serpapi_crm_storage = True
    mapper = SalesforceMapper(settings)
    enrichment = normalize_serp_place(
        {
            "website": "https://restaurant.example",
            "phone": "+31 55 744 0031",
            "rating": 4.4,
            "reviews": 2220,
            "type_ids": ["restaurant"],
            "open_state": "Open",
            "hours": [{"monday": "10-22"}],
        },
        "nl",
    ).model_copy(
        update={
            "last_review_date": "2026-09-01",
            "last_review_date_status": "LATEST_RETURNED_POST_DATE",
            "enriched_at": datetime(2026, 9, 26, tzinfo=UTC),
        }
    )
    context = {f"enrichment.{k}": v for k, v in enrichment.model_dump(mode="json").items()}
    fields = mapper.map("Lead", {"input.business_name": "Test", **context})
    assert fields["Website"] == "https://restaurant.example"
    assert fields["Phone"] == "+31557440031"
    assert fields["Google_Rating__c"] == 4.4
    assert fields["Google_Reviews__c"] == 2220
    assert fields["Last_Review_Date__c"] == "2026-09-01"
    assert fields["Store_Type__c"] == "restaurant"
    assert fields["Opening_Hours__c"] == "Monday: 10-22"
    assert "Has_Delivery__c" not in fields


def test_serpapi_research_uses_existing_and_standard_salesforce_fields(settings):
    from pathlib import Path

    settings.mapping_path = str(Path(settings.mapping_path).with_name("salesforce-mapping-serpapi.json"))
    settings.places_provider = "serpapi"
    settings.allow_serpapi_crm_storage = True
    fields = SalesforceMapper(settings).map(
        "Lead",
        {
            "input.business_name": "Example Restaurant",
            "enrichment.industry": "Hospitality",
            "enrichment.delivery": False,
            "enrichment.service_options": "Delivery: No; Takeaway: Yes; Dine-in: Yes",
            "enrichment.payment_options": ["Credit cards", "NFC mobile payments"],
            "enrichment.research_signals": [
                "Offerings: Vegan options, Vegetarian options",
                "Menu: https://restaurant.example/menu",
            ],
            "enrichment.latitude": 52.3599026,
            "enrichment.longitude": 5.2548936,
        },
    )
    assert fields["Industry"] == "Hospitality"
    assert fields["Has_Delivery__c"] is False
    assert fields["Service_Options__c"].endswith("Dine-in: Yes")
    assert fields["Payment_Options__c"] == "Credit cards; NFC mobile payments"
    assert fields["Description"].startswith("Offerings:")
    assert fields["Latitude"] == 52.3599026


def test_source_mapping_populates_both_salesforce_fields(settings):
    mapper = SalesforceMapper(settings)
    grid = mapper.map("Lead", {"input.business_name": "Example", "input.source": "GRID"})
    signup = mapper.map("Lead", {"input.business_name": "Example", "input.source": "SELF_SIGNUP"})
    assert grid["LeadSource"] == grid["Lead_Source__c"] == "GRID"
    assert signup["LeadSource"] == "Self Signup" and signup["Lead_Source__c"] == "WEB"
    with pytest.raises(ValueError, match="Unmapped"):
        mapper.map("Lead", {"input.business_name": "Example", "input.source": "UNKNOWN"})


def test_structured_address_maps_to_native_salesforce_fields(settings):
    fields = SalesforceMapper(settings).map(
        "Lead",
        {
            "input.business_name": "Example Restaurant",
            "address.street": "Palmpolstraat 1",
            "address.postal_code": "1327 CA",
            "address.city": "Almere",
            "address.country": "Netherlands",
        },
    )
    assert fields["Street"] == "Palmpolstraat 1"
    assert fields["PostalCode"] == "1327 CA"
    assert fields["City"] == "Almere"
    assert fields["Country"] == "Netherlands"


def test_bag_premises_fields_are_valid_mapping_sources(settings, tmp_path):
    from pathlib import Path

    raw = json.loads(Path(settings.mapping_path).read_text())
    raw["Lead"]["Premises_Status__c"] = {
        "source": "enrichment.premises_check_status",
        "type": "string",
        "max_length": 40,
    }
    raw["Lead"]["Premises_Checked_At__c"] = {
        "source": "enrichment.premises_checked_at",
        "type": "datetime",
    }
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(raw))
    settings.mapping_path = str(path)
    fields = SalesforceMapper(settings).map(
        "Lead",
        {
            "input.business_name": "Bella",
            "enrichment.premises_check_status": "NON_RESIDENTIAL",
            "enrichment.premises_checked_at": "2026-09-27T10:00:00+00:00",
        },
    )

    assert fields["Premises_Status__c"] == "NON_RESIDENTIAL"
    assert fields["Premises_Checked_At__c"] == "2026-09-27T10:00:00+00:00"
