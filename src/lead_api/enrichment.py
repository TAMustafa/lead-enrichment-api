from urllib.parse import quote

from .cuisine import classify_cuisine
from .models import Enrichment, phone_e164

SEARCH_MASK = "places.id,places.displayName,places.formattedAddress,nextPageToken"
DETAIL_MASK = ",".join(
    [
        "id",
        "businessStatus",
        "types",
        "primaryType",
        "internationalPhoneNumber",
        "delivery",
        "takeout",
        "rating",
        "userRatingCount",
        "formattedAddress",
        "websiteUri",
        "regularOpeningHours.weekdayDescriptions",
        "attributions",
        "googleMapsUri",
    ]
)
FOOD_TYPES = {
    "restaurant",
    "cafe",
    "bakery",
    "meal_takeaway",
    "meal_delivery",
    "bar",
    "food",
    "grocery_store",
    "supermarket",
}
RESIDENTIAL_TYPES = {"apartment_building", "apartment_complex", "housing_complex"}


def canonicalize(raw: dict) -> Enrichment:
    types = sorted(set(raw.get("types", [])) | ({raw["primaryType"]} if raw.get("primaryType") else set()))
    grocery = bool(set(types) & {"grocery_store", "supermarket"})
    restaurant = bool(
        set(types) & {"restaurant", "cafe", "bar"} or any(t.endswith("_restaurant") for t in types)
    )
    industry = None
    if grocery:
        industry = "Retail"
    elif restaurant:
        industry = "Hospitality"
    elif set(types) & FOOD_TYPES:
        industry = "Food & Beverage"
    commercial = bool(set(types) & FOOD_TYPES or any(t.endswith("_restaurant") for t in types))
    residential = bool(set(types) & RESIDENTIAL_TYPES)
    classification = "UNKNOWN" if commercial == residential else "COMMERCIAL" if commercial else "RESIDENTIAL"
    status = raw.get("businessStatus", "UNKNOWN")
    if status not in {"OPERATIONAL", "CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY"}:
        status = "UNKNOWN"
    return Enrichment(
        **classify_cuisine(types),
        operational_status=status,
        business_types=types,
        industry=industry,
        cuisine_types=[t.removesuffix("_restaurant") for t in types if t.endswith("_restaurant")],
        phone=phone_e164(raw.get("internationalPhoneNumber")),
        delivery=raw.get("delivery"),
        takeaway=raw.get("takeout"),
        rating=raw.get("rating"),
        review_count=raw.get("userRatingCount"),
        address=raw.get("formattedAddress"),
        website=raw.get("websiteUri"),
        opening_hours=raw.get("regularOpeningHours", {}).get("weekdayDescriptions", []),
        premises_classification=classification,
        premises_signals=[
            t for t in types if t in FOOD_TYPES | RESIDENTIAL_TYPES or t.endswith("_restaurant")
        ],
        attribution=["Google Maps"]
        + [a["provider"] for a in raw.get("attributions", []) if a.get("provider")],
        google_maps_url=raw.get("googleMapsUri"),
    )


class GooglePlaces:
    def __init__(self, http, settings):
        self.http, self.settings = http, settings

    def headers(self, mask):
        return {"X-Goog-Api-Key": self.settings.google_api_key.get_secret_value(), "X-Goog-FieldMask": mask}

    async def search(self, name: str, address: str) -> list[dict]:
        result = await self.http.request(
            "google",
            "POST",
            "https://places.googleapis.com/v1/places:searchText",
            headers=self.headers(SEARCH_MASK),
            json={"textQuery": f"{name}, {address}", "pageSize": 20},
        )
        data = result.json()
        # An incomplete candidate set cannot support a deterministic uniqueness assertion.
        if data.get("nextPageToken"):
            return []
        return data.get("places", [])

    async def enrich(self, place_id: str) -> Enrichment:
        result = await self.http.request(
            "google",
            "GET",
            f"https://places.googleapis.com/v1/places/{quote(place_id, safe='')}",
            headers=self.headers(DETAIL_MASK),
        )
        return canonicalize(result.json())
