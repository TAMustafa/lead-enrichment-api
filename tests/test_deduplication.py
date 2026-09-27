from unittest.mock import AsyncMock

import pytest

from lead_api.deduplication import Candidate, Deduplicator, Signals
from lead_api.errors import DependencyUnavailable


async def test_checks_both_sources_before_deciding(empty_source, lead):
    sf = AsyncMock()
    sf.lookup.return_value = [Candidate("salesforce", "Account", "001", place_id=lead.place_id)]
    result = await Deduplicator(sf, empty_source).check(Signals.from_lead(lead, lead.place_id), "own")
    assert result.duplicates[0].entity_id == "001"
    empty_source.lookup.assert_awaited_once()


async def test_partner_failure_fails_closed(empty_source, lead):
    failed = AsyncMock()
    failed.lookup.side_effect = DependencyUnavailable("unavailable")
    with pytest.raises(DependencyUnavailable):
        await Deduplicator(empty_source, failed).check(Signals.from_lead(lead, lead.place_id), "own")


@pytest.mark.parametrize(
    ("candidate", "duplicate", "uncertain"),
    [
        (
            Candidate("salesforce", "Lead", "id", name="bella pizza", address="10 main street amsterdam"),
            True,
            False,
        ),
        (
            Candidate(
                "salesforce",
                "Lead",
                "id",
                place_id="different",
                name="bella pizza",
                address="10 main street amsterdam",
            ),
            False,
            True,
        ),
        (Candidate("salesforce", "Lead", "id", phone="+16502530000"), False, True),
        (Candidate("salesforce", "Lead", "id", name="bella pizza"), False, False),
    ],
)
async def test_secondary_signals(empty_source, lead, candidate, duplicate, uncertain):
    sf = AsyncMock()
    sf.lookup.return_value = [candidate]
    signals = Signals.from_lead(lead.model_copy(update={"phone": "+16502530000"}), lead.place_id)
    result = await Deduplicator(sf, empty_source).check(signals, "own")
    assert bool(result.duplicates) is duplicate
    assert result.uncertain is uncertain
