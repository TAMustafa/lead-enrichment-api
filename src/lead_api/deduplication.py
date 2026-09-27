import asyncio
from dataclasses import dataclass
from typing import Protocol

from .models import ExistingEntity, LeadInput, normalize


@dataclass(frozen=True)
class Signals:
    place_id: str
    name: str
    address: str
    phone: str | None

    @classmethod
    def from_lead(cls, lead: LeadInput, place_id: str):
        return cls(place_id, normalize(lead.business_name), normalize(lead.address), lead.phone)


@dataclass(frozen=True)
class Candidate:
    system: str
    entity_type: str
    entity_id: str
    place_id: str | None = None
    name: str | None = None
    address: str | None = None
    phone: str | None = None
    external_key: str | None = None


class DuplicateSource(Protocol):
    async def lookup(self, signals: Signals) -> list[Candidate]: ...


@dataclass
class DedupeResult:
    duplicates: list[ExistingEntity]
    uncertain: bool
    own_record: Candidate | None


class Deduplicator:
    def __init__(self, salesforce: DuplicateSource, partners: DuplicateSource):
        self.sources = [salesforce, partners]

    async def check(self, signals: Signals, own_key: str) -> DedupeResult:
        # An error from ANY source fails closed; an unavailable source is not "no match".
        results = await asyncio.gather(*(source.lookup(signals) for source in self.sources))
        duplicates, uncertain, own = [], False, None
        for candidate in sorted(
            (c for group in results for c in group), key=lambda c: (c.system, c.entity_type, c.entity_id)
        ):
            primary = candidate.place_id == signals.place_id
            name = candidate.name == signals.name and bool(signals.name)
            address = candidate.address == signals.address and bool(signals.address)
            phone = candidate.phone == signals.phone and bool(signals.phone)
            conflicting_place = bool(candidate.place_id and candidate.place_id != signals.place_id)
            if candidate.external_key == own_key and primary:
                own = candidate
                continue
            if primary or ((name and address or phone and name) and not conflicting_place):
                duplicates.append(
                    ExistingEntity(
                        system=candidate.system,
                        entity_type=candidate.entity_type,
                        entity_id=candidate.entity_id,
                        matched_by="PLACE_ID" if primary else "SECONDARY_EXACT",
                    )
                )
            elif phone or name and address:
                uncertain = True
        return DedupeResult(duplicates, uncertain, own)
