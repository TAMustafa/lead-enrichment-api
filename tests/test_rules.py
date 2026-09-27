import pytest
from pydantic import ValidationError

from lead_api.enrichment import canonicalize
from lead_api.models import LeadInput, Status, normalize
from lead_api.qualification import qualify
from lead_api.resolution import PlaceResolver, choose_match


def details(**changes):
    return {
        "businessStatus": "OPERATIONAL",
        "types": ["restaurant", "italian_restaurant"],
        "rating": 4.2,
        "userRatingCount": 25,
        **changes,
    }


def test_normalization(lead):
    assert lead.source == "GRID"
    assert normalize("  BÉLLA, Ｐizza! ") == "bélla pizza"
    assert normalize("10-A Main St") != normalize("10-B Main St")


@pytest.mark.parametrize(
    "patch",
    [
        {"business_name": "\u200b"},
        {"phone": "0201234"},
        {"place_id": "../place"},
        {"source": "a b"},
        {"address": "!!!"},
    ],
)
def test_invalid_input(lead, patch):
    with pytest.raises(ValidationError):
        LeadInput.model_validate(lead.model_dump() | patch)


def test_resolution_threshold_and_ties(lead):
    candidate = {
        "id": "one",
        "displayName": {"text": "Bella Pizza"},
        "formattedAddress": "10 Main Street Amsterdam",
    }
    assert choose_match(lead, [candidate], 0.95, 0.15).place_id == "one"
    assert choose_match(lead, [candidate, candidate | {"id": "two"}], 0.95, 0.15).place_id is None
    assert (
        choose_match(
            lead, [candidate | {"formattedAddress": "11 Main Street Amsterdam"}], 0.95, 0.15
        ).place_id
        is None
    )
    assert choose_match(lead, [], 0.95, 0.15).reason == "PLACE_NOT_FOUND"


async def test_supplied_place_skips_search(lead, settings, empty_source):
    assert (await PlaceResolver(empty_source, settings).resolve(lead)).place_id == lead.place_id
    empty_source.search.assert_not_awaited()


def test_google_normalization_preserves_unknown_and_false():
    data = canonicalize(details(delivery=False, internationalPhoneNumber="+1 650-253-0000"))
    assert data.delivery is False and data.takeaway is None
    assert data.cuisine_types == ["italian"]
    assert data.phone == "+16502530000"
    assert data.premises_classification == "COMMERCIAL"
    assert "businessStatus" not in data.model_dump()
    assert "Google Maps" in data.attribution


@pytest.mark.parametrize(
    ("patch", "status", "reason"),
    [
        ({}, Status.QUALIFIED, "ELIGIBILITY_CONFIRMED"),
        ({"businessStatus": "CLOSED_PERMANENTLY"}, Status.REJECTED, "BUSINESS_CLOSED_PERMANENTLY"),
        ({"businessStatus": "CLOSED_TEMPORARILY"}, Status.REVIEW_REQUIRED, "OPERATIONAL_STATUS_UNCERTAIN"),
        ({"rating": None}, Status.REVIEW_REQUIRED, "REPUTATION_DATA_MISSING"),
        ({"rating": 2.9}, Status.REJECTED, "RATING_BELOW_THRESHOLD"),
        ({"userRatingCount": 9}, Status.REJECTED, "INSUFFICIENT_REVIEWS"),
        ({"types": ["apartment_building"]}, Status.REJECTED, "RESIDENTIAL_PREMISES"),
        (
            {"types": ["restaurant", "apartment_building"]},
            Status.REVIEW_REQUIRED,
            "PREMISES_CLASSIFICATION_UNCERTAIN",
        ),
    ],
)
def test_rules(settings, patch, status, reason):
    result = qualify(canonicalize(details(**patch)), settings)
    assert result.status == status
    assert reason in result.reasons


def test_exact_rating_boundary(settings):
    assert qualify(canonicalize(details(rating=3.0, userRatingCount=10)), settings).status == Status.QUALIFIED
