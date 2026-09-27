from lead_api.cuisine import classify_cuisine


def test_italian_does_not_imply_pizza_or_pasta():
    result = classify_cuisine(["italian_restaurant"])
    assert result["primary_cuisine"] == "Italian"
    assert result["secondary_cuisine"] is None
    assert "SECONDARY_CUISINE_UNKNOWN" in result["cuisine_reason_codes"]


def test_explicit_pizza_signal():
    result = classify_cuisine(["italian_restaurant", "pizza_restaurant"])
    assert result["primary_cuisine"] == "Italian"
    assert result["secondary_cuisine"] == "Pizza"
    assert result["cuisine_evidence"] == ["italian_restaurant", "pizza_restaurant"]


def test_conflicting_signals_do_not_depend_on_order():
    signals = ["italian_restaurant", "japanese_restaurant", "pizza_restaurant", "sushi_restaurant"]
    result = classify_cuisine(signals)
    assert result == classify_cuisine(list(reversed(signals)))
    assert result["primary_cuisine"] is None and result["secondary_cuisine"] is None


def test_explicit_asian_umbrella_resolves_multiple_member_cuisines():
    signals = [
        "asian_restaurant",
        "chinese_restaurant",
        "japanese_restaurant",
        "sushi_restaurant",
    ]
    result = classify_cuisine(signals)
    assert result["primary_cuisine"] == "Asian"
    assert result["secondary_cuisine"] == "Sushi"
    assert "PRIMARY_CUISINE_UMBRELLA_MATCHED" in result["cuisine_reason_codes"]
