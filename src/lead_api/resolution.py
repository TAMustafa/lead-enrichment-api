import re
from dataclasses import dataclass

from .models import LeadInput, normalize


@dataclass(frozen=True)
class Resolution:
    place_id: str | None
    confidence: float
    reason: str


def similarity(left: str, right: str) -> float:
    a, b = set(normalize(left).split()), set(normalize(right).split())
    return len(a & b) / len(a | b) if a and b else 0.0


def choose_match(lead: LeadInput, candidates: list[dict], minimum: float, margin: float) -> Resolution:
    scores: dict[str, float] = {}
    for candidate in candidates:
        if not candidate.get("id"):
            continue
        address = candidate.get("formattedAddress", "")
        # Different unit/house/postal numbers must never be fuzzy-matched away.
        if set(re.findall(r"\d+", lead.address)) != set(re.findall(r"\d+", address)):
            score = 0.0
        else:
            score = 0.6 * similarity(
                lead.business_name, candidate.get("displayName", {}).get("text", "")
            ) + 0.4 * similarity(lead.address, address)
        scores[candidate["id"]] = max(score, scores.get(candidate["id"], 0))
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if not ordered:
        return Resolution(None, 0, "PLACE_NOT_FOUND")
    best_id, score = ordered[0]
    second = ordered[1][1] if len(ordered) > 1 else 0
    if score < minimum or score - second < margin:
        return Resolution(None, round(score, 4), "PLACE_MATCH_UNCERTAIN")
    return Resolution(best_id, round(score, 4), "PLACE_RESOLVED")


class PlaceResolver:
    def __init__(self, google, settings):
        self.google, self.settings = google, settings

    async def resolve(self, lead: LeadInput) -> Resolution:
        if lead.place_id:
            return Resolution(lead.place_id, 1, "PLACE_ID_SUPPLIED")
        candidates = await self.google.search(lead.business_name, lead.address)
        return choose_match(
            lead, candidates, self.settings.resolution_min_score, self.settings.resolution_min_margin
        )
