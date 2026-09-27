import json
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import IDENTIFIER

FIRST_PARTY = {
    "input.business_name",
    "input.address",
    "input.phone",
    "input.source",
    "input.source_lead_id",
    "lead_id",
    "place_id",
    "normalized_name",
    "normalized_address",
    "normalized_phone",
    "address.street",
    "address.postal_code",
    "address.city",
    "address.country",
}
ENRICHMENT = {
    "operational_status",
    "industry",
    "price_range",
    "service_options",
    "enriched_at",
    "last_review_date",
    "last_review_date_status",
    "business_types",
    "cuisine_types",
    "primary_cuisine",
    "secondary_cuisine",
    "phone",
    "delivery",
    "takeaway",
    "dine_in",
    "catering",
    "accepts_reservations",
    "rating",
    "review_count",
    "address",
    "website",
    "menu_url",
    "reservation_url",
    "latitude",
    "longitude",
    "payment_options",
    "research_signals",
    "opening_hours",
    "premises_classification",
    "premises_check_status",
    "premises_checked_at",
}
BAG_ENRICHMENT = {"premises_check_status", "premises_checked_at"}


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str | None = None
    constant: str | int | float | bool | None = None
    type: Literal["string", "boolean", "integer", "number", "multipicklist", "date", "datetime"]
    max_length: int = Field(default=255, gt=0)
    enabled: bool = True
    join_with: str | None = Field(default=None, max_length=5)
    required: bool = False
    allowed_values: list[str] | None = None
    value_map: dict[str, str] | None = None

    @model_validator(mode="after")
    def source_or_constant(self):
        if (self.source is None) == (self.constant is None):
            raise ValueError("Specify exactly one source or constant")
        if self.value_map is not None and (self.type != "string" or self.source is None):
            raise ValueError("Value maps require a string source")
        return self


class SalesforceMapper:
    def __init__(self, settings):
        self.settings = settings
        raw = json.loads(Path(settings.mapping_path).read_text())
        if set(raw) != {"Lead", "Account"}:
            raise ValueError("Mappings must define Lead and Account")
        self.mapping = {
            obj: {field: FieldMapping.model_validate(spec) for field, spec in fields.items()}
            for obj, fields in raw.items()
        }
        for obj, fields in self.mapping.items():
            self.mapping[obj] = fields = {k: v for k, v in fields.items() if v.enabled}
            required = {"Company", "LastName"} if obj == "Lead" else {"Name"}
            if not required <= fields.keys() or not all(fields[f].required for f in required):
                raise ValueError(f"Missing required {obj} fields")
            identity_fields = {
                settings.salesforce_place_field: "place_id",
                settings.salesforce_name_field: "normalized_name",
                settings.salesforce_address_field: "normalized_address",
                settings.salesforce_phone_field: "normalized_phone",
            }
            for field, source in identity_fields.items():
                if field not in fields or fields[field].source != source or fields[field].type != "string":
                    raise ValueError(f"Missing identity mapping for {obj}.{field}")
            for field, spec in fields.items():
                if not IDENTIFIER.fullmatch(field) or field in {"Id", settings.salesforce_external_field}:
                    raise ValueError("Invalid or reserved field API name")
                if spec.source is not None and spec.source not in FIRST_PARTY:
                    if (
                        not spec.source.startswith("enrichment.")
                        or spec.source.split(".", 1)[1] not in ENRICHMENT
                    ):
                        raise ValueError("Unknown mapping source")
                    source_name = spec.source.split(".", 1)[1]
                    if source_name not in BAG_ENRICHMENT:
                        allowed = (
                            settings.allow_serpapi_crm_storage
                            if settings.places_provider == "serpapi"
                            else settings.allow_google_crm_storage
                        )
                        if not allowed:
                            raise ValueError(
                                "Provider-derived CRM storage requires an independent contractual basis"
                            )

    def map(self, obj: str, context: dict) -> dict:
        output = {}
        for field, spec in self.mapping[obj].items():
            value = context.get(spec.source) if spec.source else spec.constant
            if value is None:
                if spec.required:
                    raise ValueError(f"Missing required mapped field {field}")
                continue
            if spec.value_map is not None:
                if not isinstance(value, str) or value not in spec.value_map:
                    raise ValueError(f"Unmapped source value for {field}")
                value = spec.value_map[value]
            if spec.type == "date":
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError(f"Expected ISO date for {field}")
            elif spec.type == "datetime":
                if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                    raise ValueError(f"Expected timezone-aware ISO datetime for {field}")
            if spec.join_with is not None:
                if (
                    spec.type != "string"
                    or not isinstance(value, list)
                    or any(not isinstance(v, str) for v in value)
                ):
                    raise ValueError(f"Invalid list-to-text mapping {field}")
                value = spec.join_with.join(value)
            if spec.type == "multipicklist":
                if not isinstance(value, list) or any(not isinstance(v, str) or ";" in v for v in value):
                    raise ValueError(f"Invalid multipicklist {field}")
                if spec.allowed_values and not set(value) <= set(spec.allowed_values):
                    raise ValueError(f"Unknown picklist value for {field}")
                value = ";".join(value)
            elif spec.type == "string" and not isinstance(value, str):
                raise ValueError(f"Expected string for {field}")
            elif spec.type == "boolean" and type(value) is not bool:
                raise ValueError(f"Expected boolean for {field}")
            elif spec.type == "integer" and type(value) is not int:
                raise ValueError(f"Expected integer for {field}")
            elif spec.type == "number" and type(value) not in (float, int):
                raise ValueError(f"Expected number for {field}")
            if isinstance(value, str):
                if len(value) > spec.max_length:
                    raise ValueError(f"Mapped field too long: {field}")
                if spec.type != "multipicklist" and spec.allowed_values and value not in spec.allowed_values:
                    raise ValueError(f"Unknown picklist value for {field}")
            output[field] = value
        return output
