from unittest.mock import AsyncMock

import httpx
import pytest

from lead_api.models import Enrichment, Status
from lead_api.qualification import qualify
from lead_api.residential import ResidentialChecker, classify_unit, exact_unit, parse_address

UNIT = "0034010000010677"
ADDRESS = "Palmpolstraat 1, 1327 CA Almere"


def address_payload(**overrides):
    doc = dict(
        type="adres",
        bron="BAG",
        postcode="1327CA",
        huisnummer=1,
        straatnaam="Palmpolstraat",
        woonplaatsnaam="Almere",
        adresseerbaarobject_id=UNIT,
    )
    doc.update(overrides)
    return {"response": {"numFound": 1, "docs": [doc]}}


def unit_payload(use="woonfunctie", **overrides):
    props = dict(identificatie=UNIT, status="Verblijfsobject in gebruik", gebruiksdoel=use)
    props.update(overrides)
    return {"features": [{"properties": props}], "links": []}


@pytest.mark.parametrize(
    ("use", "expected"),
    [
        ("woonfunctie", "RESIDENTIAL"),
        ("bijeenkomstfunctie", "NON_RESIDENTIAL"),
        ("winkelfunctie", "NON_RESIDENTIAL"),
        ("woonfunctie, winkelfunctie", "UNKNOWN"),
        (["woonfunctie", "winkelfunctie"], "UNKNOWN"),
        ("overige gebruiksfunctie", "UNKNOWN"),
        ("", "UNKNOWN"),
    ],
)
def test_registered_use(use, expected):
    assert classify_unit(unit_payload(use), UNIT)[0] == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"huisletter": "A"},
        {"woonplaatsnaam": "Amsterdam"},
        {"straatnaam": "Wrong street"},
        {"huisnummer": 11},
        {"postcode": "1327CB"},
    ],
)
def test_exact_address_required(changes):
    assert exact_unit(address_payload(**changes), parse_address(ADDRESS)) is None


def test_ambiguous_and_partial_addresses():
    p = address_payload()
    p["response"]["numFound"] = 2
    assert exact_unit(p, parse_address(ADDRESS)) is None
    p["response"]["docs"] *= 2
    assert exact_unit(p, parse_address(ADDRESS)) is None
    assert parse_address("Palmpolstraat 1 Almere") is None
    assert parse_address("Palmpolstraat 1, 1327 CA Almere, Netherlands") == parse_address(ADDRESS)


def test_stale_unit_and_incomplete_response():
    assert classify_unit(unit_payload(status="Verblijfsobject ingetrokken"), UNIT)[0] == "UNKNOWN"
    assert classify_unit(unit_payload(identificatie="wrong"), UNIT)[0] == "UNKNOWN"
    p = unit_payload()
    p["links"] = [{"rel": "next"}]
    assert classify_unit(p, UNIT)[0] == "UNKNOWN"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("RESIDENTIAL", Status.REJECTED),
        ("UNKNOWN", Status.REVIEW_REQUIRED),
        ("NOT_CHECKED", Status.REVIEW_REQUIRED),
        ("NON_RESIDENTIAL", Status.QUALIFIED),
    ],
)
def test_rules(settings, status, expected):
    settings.residential_check_enabled = True
    d = Enrichment(
        operational_status="OPERATIONAL",
        business_types=["restaurant"],
        rating=4,
        review_count=100,
        premises_classification="COMMERCIAL",
        premises_check_status=status,
    )
    assert qualify(d, settings).status == expected


async def test_contract():
    http = AsyncMock()
    http.request.side_effect = [
        httpx.Response(200, json=address_payload()),
        httpx.Response(200, json=unit_payload()),
    ]
    d = Enrichment(operational_status="UNKNOWN")
    await ResidentialChecker(http).check(d, ADDRESS)
    assert d.premises_check_status == "RESIDENTIAL"
    assert http.request.call_args.kwargs["params"]["identificatie"] == UNIT
    assert d.premises_checked_at


async def test_failure_and_missing_address():
    http = AsyncMock()
    http.request.side_effect = TimeoutError()
    d = Enrichment(operational_status="UNKNOWN")
    await ResidentialChecker(http).check(d, ADDRESS)
    assert d.premises_check_status == "UNKNOWN"
    assert d.premises_check_evidence == ["BAG_LOOKUP_UNAVAILABLE"]
    http.reset_mock()
    await ResidentialChecker(http).check(d, "unknown")
    http.request.assert_not_awaited()
