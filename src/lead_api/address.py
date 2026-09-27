"""Deterministic normalization of caller-supplied addresses."""

import re
import unicodedata

from .models import CanonicalAddress, normalize

NL_ADDRESS = re.compile(
    r"^(.+?)\s+(\d+)([^,]*?)\s*,?\s+(\d{4})\s*([A-Za-z]{2})\s*,?\s*([^,]+?)"
    r"(?:\s*,\s*(Netherlands|Nederland))?$",
    re.IGNORECASE,
)


def clean_display(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = " ".join(value.split())
    return re.sub(r"\s*,\s*", ", ", value).strip(" ,")


def parse_nl_address(address: str):
    """Return exact Dutch address components used by both CRM mapping and BAG lookup."""
    match = NL_ADDRESS.fullmatch(clean_display(address))
    if not match:
        return None
    street, number, suffix, digits, letters, city, _country = match.groups()
    suffix = re.sub(r"\s+", "", suffix)
    return (
        clean_display(street),
        int(number),
        normalize(suffix),
        digits + letters.upper(),
        clean_display(city),
        f"{number}{suffix}",
    )


def canonicalize_address(address: str) -> CanonicalAddress:
    full = clean_display(address)
    parsed = parse_nl_address(full)
    if not parsed:
        return CanonicalAddress(full=full, street=full, identity=normalize(full))
    street, _number, _suffix, postcode, city, house_number = parsed
    postal_code = f"{postcode[:4]} {postcode[4:]}"
    canonical_full = f"{street} {house_number}, {postal_code} {city}, Netherlands"
    return CanonicalAddress(
        full=canonical_full,
        street=f"{street} {house_number}",
        postal_code=postal_code,
        city=city,
        country="Netherlands",
        identity=normalize(canonical_full),
    )
