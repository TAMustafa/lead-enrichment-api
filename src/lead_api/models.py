import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE).split())


def phone_e164(value: str | None) -> str | None:
    if not value:
        return None
    try:
        number = phonenumbers.parse(value, None)
        return (
            phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
            if phonenumbers.is_valid_number(number)
            else None
        )
    except phonenumbers.NumberParseException:
        return None


class LeadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    source_lead_id: str = Field(min_length=1, max_length=200)
    business_name: str = Field(min_length=1, max_length=250)
    address: str = Field(min_length=3, max_length=1000)
    place_id: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    phone: str | None = Field(default=None, max_length=40)

    @field_validator("source")
    @classmethod
    def source_case(cls, v):
        return v.upper()

    @field_validator("business_name", "address", "source_lead_id")
    @classmethod
    def clean(cls, v):
        if any(unicodedata.category(c).startswith("C") for c in v) or not normalize(v):
            raise ValueError("Must contain visible text and no control characters")
        return unicodedata.normalize("NFKC", " ".join(v.split()))

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, v):
        if v is None:
            return v
        normalized = phone_e164(v)
        if normalized is None:
            raise ValueError("Phone must be a valid international number with country code")
        return normalized


class Status(StrEnum):
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DUPLICATE = "DUPLICATE"


class ExistingEntity(BaseModel):
    system: Literal["salesforce", "bigquery", "internal"]
    entity_type: str
    entity_id: str
    matched_by: str


class CanonicalAddress(BaseModel):
    """Caller-supplied address normalized into Salesforce-compatible components."""

    full: str
    street: str
    postal_code: str | None = None
    city: str | None = None
    country: str | None = None
    identity: str


class Enrichment(BaseModel):
    operational_status: Literal["OPERATIONAL", "CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY", "UNKNOWN"]
    business_types: list[str] = []
    industry: Literal["Hospitality", "Retail", "Food & Beverage"] | None = None
    cuisine_types: list[str] = []
    primary_cuisine: str | None = None
    secondary_cuisine: str | None = None
    cuisine_evidence: list[str] = []
    cuisine_reason_codes: list[str] = []
    provider: str = "google"
    price_range: str | None = None
    service_options: str | None = None
    enriched_at: datetime | None = None
    last_review_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    last_review_date_status: str = "NOT_REQUESTED"
    premises_check_status: Literal["NOT_CHECKED", "RESIDENTIAL", "NON_RESIDENTIAL", "UNKNOWN"] = "NOT_CHECKED"
    premises_check_evidence: list[str] = []
    premises_checked_at: datetime | None = None
    phone: str | None = None
    delivery: bool | None = None
    takeaway: bool | None = None
    dine_in: bool | None = None
    catering: bool | None = None
    accepts_reservations: bool | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    address: str | None = None
    website: str | None = None
    menu_url: str | None = None
    reservation_url: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    payment_options: list[str] = []
    research_signals: list[str] = []
    opening_hours: list[str] = []
    premises_classification: Literal["COMMERCIAL", "RESIDENTIAL", "UNKNOWN"] = "UNKNOWN"
    premises_signals: list[str] = []
    attribution: list[str] = []
    google_maps_url: str | None = None


class Metadata(BaseModel):
    request_id: str
    rules_version: str
    duration_ms: int = 0
    idempotent_replay: bool = False
    enrichment_omitted_on_replay: bool = False
    persistence_status: Literal["NOT_REQUESTED", "WRITTEN", "RECOVERED"] = "NOT_REQUESTED"


class LeadResponse(BaseModel):
    business_name: str | None = None
    normalized_business_name: str | None = None
    normalized_address: CanonicalAddress | None = None
    lead_id: UUID
    source: str
    source_lead_id: str
    place_id: str | None
    duplicate_status: Literal["CLEAR", "DUPLICATE", "UNCHECKED"]
    existing_entity_id: str | None = None
    existing_entities: list[ExistingEntity] = []
    qualification_status: Status
    qualification_reason_codes: list[str]
    normalized_enrichment: Enrichment | None = None
    confidence: float | None = None
    salesforce_record_id: str | None = None
    salesforce_object: str | None = None
    salesforce_fields: dict[str, str | float | int | bool] = {}
    processing_metadata: Metadata
