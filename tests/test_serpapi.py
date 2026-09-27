import httpx
import pytest

from lead_api.errors import DependencyUnavailable
from lead_api.http import ResilientHTTP
from lead_api.serpapi import SerpPlaces, SerpSettings, normalize_serp_place


def test_normalization_preserves_evidence_and_missing_values():
    data = normalize_serp_place(
        {
            "type_ids": ["italian_restaurant", "pizza_restaurant"],
            "open_state": "Closed ⋅ Opens 9 AM",
            "phone": "020 123 4567",
            "service_options": {"delivery": False},
            "rating": 4.5,
            "reviews": 120,
            "hours": [{"tuesday": "10-20"}, {"monday": "10-22"}],
        },
        "nl",
    )
    assert data.operational_status == "OPERATIONAL"
    assert data.primary_cuisine == "Italian" and data.secondary_cuisine == "Pizza"
    assert data.delivery is False and data.takeaway is None
    assert data.opening_hours == ["Monday: 10-22", "Tuesday: 10-20"]
    assert "restaurant" in data.business_types
    assert data.phone == "+31201234567"


def test_restaurant_research_fields_use_current_serpapi_shape():
    data = normalize_serp_place(
        {
            "type_ids": ["asian_restaurant", "sushi_restaurant"],
            "service_options": {"delivery": False, "takeaway": True, "dine_in": True},
            "gps_coordinates": {"latitude": 52.3599026, "longitude": 5.2548936},
            "menu": {"link": "https://restaurant.example/menu"},
            "reservation": {"link": "https://booking.example/table"},
            "extensions": [
                {"dining_options": ["Dinner", "Catering"]},
                {"planning": ["Accepts reservations"]},
                {"payments": ["Credit cards", "NFC mobile payments"]},
                {"offerings": ["Vegan options", "Vegetarian options"]},
            ],
        },
        "nl",
    )
    assert data.industry == "Hospitality"
    assert data.delivery is False and data.takeaway is True and data.dine_in is True
    assert data.catering is True and data.accepts_reservations is True
    assert data.latitude == 52.3599026 and data.longitude == 5.2548936
    assert data.payment_options == ["Credit cards", "NFC mobile payments"]
    assert data.service_options == (
        "Delivery: No; Takeaway: Yes; Dine-in: Yes; Catering: Yes; Reservations: Yes"
    )
    assert "Menu: https://restaurant.example/menu" in data.research_signals
    assert "Reservations: https://booking.example/table" in data.research_signals


@pytest.mark.parametrize(
    "state,expected",
    [
        ("Permanently closed", "CLOSED_PERMANENTLY"),
        ("Temporarily closed", "CLOSED_TEMPORARILY"),
        ("Closed", "UNKNOWN"),
        ("", "UNKNOWN"),
    ],
)
def test_unknown_not_assumed_operational(state, expected):
    assert normalize_serp_place({"open_state": state}, "nl").operational_status == expected


async def test_search_and_details_use_place_id_and_no_returned_links():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "serpapi.com"
        assert request.url.params["api_key"] == "test-secret"
        if request.url.params["engine"] == "google_maps_reviews":
            assert request.url.params["sort_by"] == "newestFirst"
            return httpx.Response(200, json={"reviews": [{"iso_date": "2026-09-01T12:00:00Z"}]})
        if request.url.params["type"] == "search":
            return httpx.Response(
                200,
                json={
                    "local_results": [
                        {"place_id": "ChIJ-test", "title": "Bella", "address": "10 Main"},
                        {"data_id": "0x123:0x456", "title": "Wrong ID"},
                    ]
                },
            )
        assert request.url.params["place_id"] == "ChIJ-test"
        return httpx.Response(
            200,
            json={
                "place_results": {
                    "place_id": "ChIJ-test",
                    "type_ids": ["restaurant"],
                    "open_state": "Open",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = SerpPlaces(ResilientHTTP(client), SerpSettings(serpapi_api_key="test-secret"))
        rows = await provider.search("Bella", "10 Main")
        assert len(rows) == 1 and rows[0]["id"] == "ChIJ-test"
        assert (await provider.enrich("ChIJ-test")).operational_status == "OPERATIONAL"
    assert len(requests) == 3


async def test_missing_key_does_not_make_request():
    async with httpx.AsyncClient() as client:
        with pytest.raises(DependencyUnavailable, match="not configured"):
            await SerpPlaces(ResilientHTTP(client), SerpSettings(serpapi_api_key="")).search(
                "Bella", "10 Main"
            )


async def test_wrong_detail_identity_is_rejected():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"place_results": {"place_id": "wrong"}})
        )
    ) as client:
        with pytest.raises(DependencyUnavailable, match="identity"):
            await SerpPlaces(ResilientHTTP(client), SerpSettings(serpapi_api_key="test")).enrich("correct")


def test_latest_review_date_does_not_use_owner_response_or_relative_date():
    from lead_api.serpapi import latest_review_date

    assert latest_review_date(
        {"reviews": [{"date": "2 weeks ago", "response": {"iso_date": "2026-09-20T12:00:00Z"}}]}
    ) == (None, "EXACT_DATE_UNAVAILABLE")
    assert latest_review_date(
        {"reviews": [{"iso_date": "2026-09-01T12:00:00Z", "iso_date_of_last_edit": "2026-09-02T12:00:00Z"}]}
    ) == ("2026-09-01", "LATEST_RETURNED_POST_DATE")


def test_recently_edited_old_review_is_not_latest_posting():
    from lead_api.serpapi import latest_review_date

    assert latest_review_date(
        {
            "reviews": [
                {"iso_date": "2025-11-12T10:41:01Z", "iso_date_of_last_edit": "2026-09-25T15:12:50Z"},
                {"iso_date": "2026-09-25T12:23:04Z"},
            ]
        }
    ) == ("2026-09-25", "LATEST_RETURNED_POST_DATE")
