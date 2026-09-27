"""Dutch BAG unit-use check. Exact address identity; no geocoding or area inference."""

import asyncio
import re
from datetime import UTC, datetime

from .address import parse_nl_address
from .errors import DependencyUnavailable
from .models import normalize

ADDRESS_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
BAG_URL = "https://api.pdok.nl/kadaster/bag/ogc/v2/collections/verblijfsobject/items"
NON_RESIDENTIAL = {
    "bijeenkomstfunctie",
    "winkelfunctie",
    "industriefunctie",
    "kantoorfunctie",
    "logiesfunctie",
    "onderwijsfunctie",
    "sportfunctie",
    "gezondheidszorgfunctie",
    "celfunctie",
}


def parse_address(address):
    parsed = parse_nl_address(address)
    return parsed[:5] if parsed else None


def exact_unit(payload, address):
    street, number, suffix, postcode, city = address
    response = payload["response"]
    docs = response["docs"]
    if response["numFound"] != len(docs):
        return None
    matches = [
        d
        for d in docs
        if (
            d.get("type") == "adres"
            and d.get("bron") == "BAG"
            and d.get("postcode") == postcode
            and d.get("huisnummer") == number
            and normalize(d.get("straatnaam", "")) == normalize(street)
            and normalize(d.get("woonplaatsnaam", "")) == normalize(city)
            and normalize(" ".join(filter(None, [d.get("huisletter"), d.get("huisnummertoevoeging")])))
            == suffix
        )
    ]
    return matches[0].get("adresseerbaarobject_id") if len(matches) == 1 else None


def classify_unit(payload, unit):
    if any(link.get("rel") == "next" for link in payload.get("links", [])):
        return "UNKNOWN", ["BAG_RESPONSE_INCOMPLETE"]
    features = payload["features"]
    if len(features) != 1:
        return "UNKNOWN", ["BAG_UNIT_UNCERTAIN"]
    data = features[0]["properties"]
    if data.get("identificatie") != unit or data.get("status") != "Verblijfsobject in gebruik":
        return "UNKNOWN", ["BAG_UNIT_NOT_CURRENT"]
    raw = data.get("gebruiksdoel", "")
    uses = set(raw) if isinstance(raw, list) else set(re.split(r"[,;]\s*", raw))
    evidence = [f"BAG {unit}: {', '.join(sorted(uses))}"]
    if uses == {"woonfunctie"}:
        return "RESIDENTIAL", evidence
    if uses and uses <= NON_RESIDENTIAL:
        return "NON_RESIDENTIAL", evidence
    return "UNKNOWN", evidence + ["BAG_MIXED_OR_UNKNOWN_USE"]


class ResidentialChecker:
    def __init__(self, http):
        self.http = http

    async def check(self, data, address):
        data.premises_checked_at = datetime.now(UTC)
        data.premises_check_status = "UNKNOWN"
        data.premises_check_evidence = ["BAG_EXACT_NL_ADDRESS_REQUIRED"]
        parsed = parse_address(address)
        if not parsed:
            return
        try:
            async with asyncio.timeout(6):
                response = await self.http.request(
                    "pdok",
                    "GET",
                    ADDRESS_URL,
                    params={
                        "q": "*:*",
                        "fq": ["type:adres", f"postcode:{parsed[3]}", f"huisnummer:{parsed[1]}"],
                        "rows": 100,
                    },
                )
                unit = exact_unit(response.json(), parsed)
                if not unit or not re.fullmatch(r"\d{16}", unit):
                    return
                response = await self.http.request(
                    "pdok",
                    "GET",
                    BAG_URL,
                    params={"identificatie": unit, "limit": 2, "f": "json"},
                )
                status, evidence = classify_unit(response.json(), unit)
        except (TimeoutError, DependencyUnavailable, ValueError, KeyError, TypeError, AttributeError):
            status, evidence = "UNKNOWN", ["BAG_LOOKUP_UNAVAILABLE"]
        data.premises_check_status = status
        data.premises_check_evidence = evidence
        data.attribution = list(dict.fromkeys(data.attribution + ["Kadaster BAG via PDOK"]))
