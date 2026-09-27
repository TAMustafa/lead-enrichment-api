"""SerpAPI Google Maps adapter. Provider payloads never escape the canonical boundary."""

import logging
import re
from datetime import UTC, datetime

import phonenumbers
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .enrichment import canonicalize
from .errors import DependencyUnavailable


class SerpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LQ_", extra="ignore")
    serpapi_api_key: SecretStr = SecretStr("")
    serpapi_country: str = Field(default="nl", pattern=r"^[a-z]{2}$")


def normalize_serp_place(raw: dict, country: str):
    types = raw.get("type_ids") or ([raw["type_id"]] if raw.get("type_id") else [])
    if not types:
        labels = raw.get("type", [])
        labels = [labels] if isinstance(labels, str) else labels
        types = [re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_") for label in labels]
    # A specific restaurant subtype supplies evidence for the broader restaurant category.
    if any(t.endswith("_restaurant") for t in types):
        types = list(set(types) | {"restaurant"})
    state = str(raw.get("open_state", "")).casefold()
    if raw.get("permanently_closed") is True or "permanently closed" in state:
        status = "CLOSED_PERMANENTLY"
    elif raw.get("temporarily_closed") is True or "temporarily closed" in state:
        status = "CLOSED_TEMPORARILY"
    elif state.startswith(("open", "closes")) or (state.startswith("closed") and "opens" in state):
        status = "OPERATIONAL"
    else:
        status = "UNKNOWN"
    phone = raw.get("phone")
    if phone:
        try:
            number = phonenumbers.parse(phone, country.upper())
            phone = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            phone = None
    services = raw.get("service_options") or {}
    takeaway = services.get("takeaway", services.get("takeout"))
    extension_groups = {}
    for extension in raw.get("extensions") or []:
        if not isinstance(extension, dict):
            continue
        for group, values in extension.items():
            if isinstance(values, list):
                extension_groups[group] = list(
                    dict.fromkeys(value for value in values if isinstance(value, str) and value)
                )
    dining_options = extension_groups.get("dining_options", [])
    planning = extension_groups.get("planning", [])
    catering = True if "Catering" in dining_options else None
    accepts_reservations = (
        True
        if raw.get("reservation") or any("reservation" in value.casefold() for value in planning)
        else None
    )
    research_groups = (
        "highlights",
        "offerings",
        "dining_options",
        "accessibility",
        "planning",
        "children",
        "parking",
    )
    research_signals = [
        f"{group.replace('_', ' ').title()}: {', '.join(extension_groups[group])}"
        for group in research_groups
        if extension_groups.get(group)
    ]
    coordinates = raw.get("gps_coordinates") or {}
    menu = raw.get("menu") or {}
    reservation = raw.get("reservation") or {}
    if isinstance(menu.get("link"), str):
        research_signals.append(f"Menu: {menu['link']}")
    if isinstance(reservation.get("link"), str):
        research_signals.append(f"Reservations: {reservation['link']}")
    hours = raw.get("hours") or []
    if isinstance(hours, dict):
        hours = [hours]
    weekdays = {day: value for entry in hours if isinstance(entry, dict) for day, value in entry.items()}
    lines = [
        f"{day.title()}: {weekdays[day]}"
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        if day in weekdays
    ]
    return canonicalize(
        {
            "businessStatus": status,
            "types": types,
            "internationalPhoneNumber": phone,
            "delivery": services.get("delivery") if type(services.get("delivery")) is bool else None,
            "takeout": takeaway if type(takeaway) is bool else None,
            "rating": raw.get("rating"),
            "userRatingCount": raw.get("reviews"),
            "formattedAddress": raw.get("address"),
            "websiteUri": raw.get("website"),
            "regularOpeningHours": {"weekdayDescriptions": lines},
        }
    ).model_copy(
        update={
            "attribution": ["Google Maps via SerpAPI"],
            "provider": "serpapi",
            "price_range": raw.get("price"),
            "service_options": "; ".join(
                f"{label}: " + ("Yes" if value is True else "No" if value is False else "Unknown")
                for label, value in [
                    ("Delivery", services.get("delivery")),
                    ("Takeaway", takeaway),
                    ("Dine-in", services.get("dine_in")),
                    ("Catering", catering),
                    ("Reservations", accepts_reservations),
                ]
            ),
            "dine_in": services.get("dine_in") if type(services.get("dine_in")) is bool else None,
            "catering": catering,
            "accepts_reservations": accepts_reservations,
            "menu_url": menu.get("link") if isinstance(menu.get("link"), str) else None,
            "reservation_url": (
                reservation.get("link") if isinstance(reservation.get("link"), str) else None
            ),
            "latitude": coordinates.get("latitude"),
            "longitude": coordinates.get("longitude"),
            "payment_options": extension_groups.get("payments", []),
            "research_signals": research_signals,
            "enriched_at": datetime.now(UTC),
        }
    )


class SerpPlaces:
    def __init__(self, http, settings):
        self.http, self.settings = http, settings
        # SerpAPI authenticates in query parameters; never emit HTTP request URLs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def fetch(self, engine="google_maps", **parameters):
        key = self.settings.serpapi_api_key.get_secret_value().strip()
        if not key:
            raise DependencyUnavailable("SerpAPI key is not configured")
        response = await self.http.request(
            "serpapi",
            "GET",
            "https://serpapi.com/search.json",
            params={
                "engine": engine,
                "api_key": key,
                "hl": "en",
                "gl": self.settings.serpapi_country,
                **parameters,
            },
            timeout=20,
        )
        data = response.json()
        if data.get("error") or data.get("search_metadata", {}).get("status") == "Error":
            # Provider errors may echo query strings or credentials. Return only a fixed message.
            raise DependencyUnavailable("SerpAPI could not complete the lookup")
        return data

    async def search(self, name, address):
        data = await self.fetch(type="search", q=f"{name}, {address}")
        if data.get("serpapi_pagination", {}).get("next"):
            return []  # Incomplete candidate set must not produce a confident unique match.
        rows = data.get("local_results", [])
        if data.get("place_results"):
            rows = [data["place_results"]]
        return [
            {
                "id": row["place_id"],
                "displayName": {"text": row.get("title", "")},
                "formattedAddress": row.get("address", ""),
            }
            for row in rows
            if isinstance(row.get("place_id"), str)
            and re.fullmatch(r"[A-Za-z0-9_-]+", row["place_id"])
            and not row["place_id"].isdigit()
        ]  # Never substitute a CID/data_id for a Google Place ID.

    async def enrich(self, place_id):
        data = await self.fetch(type="place", place_id=place_id)
        place = data.get("place_results", {})
        if not place or place.get("place_id") != place_id:
            raise DependencyUnavailable("SerpAPI detail identity could not be verified")
        enrichment = normalize_serp_place(place, self.settings.serpapi_country)
        if enrichment.review_count == 0:
            return enrichment.model_copy(update={"last_review_date_status": "NO_REVIEWS"})
        try:
            reviews = await self.fetch(engine="google_maps_reviews", place_id=place_id, sort_by="newestFirst")
            date, status = latest_review_date(reviews)
        except (DependencyUnavailable, ValueError, TypeError):
            date, status = None, "LOOKUP_UNAVAILABLE"
        return enrichment.model_copy(update={"last_review_date": date, "last_review_date_status": status})


def latest_review_date(payload):
    """Newest original posting date in the returned batch; ordering may prioritize recent edits."""
    dates = []
    reviews = payload.get("reviews", [])
    for review in reviews:
        if review.get("source", "Google") != "Google":
            continue
        value = review.get("iso_date")
        if not isinstance(value, str):
            continue
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is not None and stamp <= datetime.now(UTC):
            dates.append(stamp.astimezone(UTC).date())
    if dates:
        return max(dates).isoformat(), "LATEST_RETURNED_POST_DATE"
    return None, "EXACT_DATE_UNAVAILABLE" if reviews else "NO_REVIEWS_RETURNED"
