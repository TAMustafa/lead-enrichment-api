from lead_api.address import canonicalize_address, parse_nl_address


def test_dutch_address_is_normalized_into_salesforce_components():
    result = canonicalize_address("  Palmpolstraat 1 ,1327 ca,  Almere ")
    assert result.model_dump() == {
        "full": "Palmpolstraat 1, 1327 CA Almere, Netherlands",
        "street": "Palmpolstraat 1",
        "postal_code": "1327 CA",
        "city": "Almere",
        "country": "Netherlands",
        "identity": "palmpolstraat 1 1327 ca almere netherlands",
    }


def test_address_suffix_and_optional_country_are_preserved():
    result = canonicalize_address("Kerkstraat 12 A, 1012 AB Amsterdam, Nederland")
    assert result.street == "Kerkstraat 12A"
    assert result.country == "Netherlands"
    assert parse_nl_address("Kerkstraat 12A 1012 AB Amsterdam") is not None


def test_unknown_address_format_is_preserved_without_inventing_components():
    result = canonicalize_address("1 Infinite Loop, Cupertino, CA")
    assert result.street == result.full
    assert result.country is None
