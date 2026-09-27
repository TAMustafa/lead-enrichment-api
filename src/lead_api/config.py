import re
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LQ_", env_file=".env", extra="ignore")
    database_url: SecretStr
    # Map stable client identities to secrets; inject from a secret manager or mounted .env.
    api_keys: dict[str, SecretStr]
    fingerprint_key: SecretStr = Field(min_length=32)
    places_provider: Literal["google", "serpapi"] = "google"
    serpapi_api_key: SecretStr = SecretStr("")
    serpapi_country: str = Field(default="nl", pattern=r"^[a-z]{2}$")
    google_api_key: SecretStr = SecretStr("")
    salesforce_url: str
    salesforce_client_id: SecretStr
    salesforce_client_secret: SecretStr
    salesforce_version: str = "v67.0"
    salesforce_place_field: str = "Google_Place_ID__c"
    salesforce_external_field: str = "Qualification_Key__c"
    salesforce_name_field: str = "Normalized_Business_Name__c"
    salesforce_address_field: str = "Normalized_Address__c"
    salesforce_phone_field: str = "Normalized_Phone__c"
    bigquery_project: str
    bigquery_table: str
    route: Literal["Lead", "Account"] = "Lead"
    source_routes: dict[str, Literal["Lead", "Account"]] = {}
    persistence_enabled: bool = False
    mapping_path: str = "salesforce-mapping.json"
    # Requires an independent contractual basis; ordinary Places access is insufficient.
    allow_google_crm_storage: bool = False
    allow_serpapi_crm_storage: bool = False
    request_timeout_seconds: float = Field(default=20, gt=0, le=60)
    http_timeout_seconds: float = Field(default=3, gt=0, le=10)
    rate_limit_per_minute: int = Field(default=60, gt=0)
    pool_size: int = Field(default=20, ge=2, le=200)
    resolution_min_score: float = Field(default=0.95, ge=0, le=1)
    resolution_min_margin: float = Field(default=0.15, ge=0, le=1)
    min_rating: float = Field(default=3.0, ge=0, le=5)
    min_reviews: int = Field(default=10, ge=0)
    allowed_types: set[str] = {"restaurant", "meal_takeaway", "meal_delivery", "grocery_store", "supermarket"}
    residential_check_enabled: bool = False
    rules_version: str = "2026-09-26.3"
    otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def check_security(self):
        if any(source != source.upper() for source in self.source_routes):
            raise ValueError("Source routing keys must be uppercase normalized source names")
        if not self.api_keys or any(len(v.get_secret_value()) < 32 for v in self.api_keys.values()):
            raise ValueError("Configure at least one API key of 32+ characters")
        from urllib.parse import urlparse

        parsed = urlparse(self.salesforce_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("Salesforce URL must be an HTTPS origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Invalid Salesforce origin")
        for name in (
            self.salesforce_place_field,
            self.salesforce_external_field,
            self.salesforce_name_field,
            self.salesforce_address_field,
            self.salesforce_phone_field,
        ):
            if not IDENTIFIER.fullmatch(name):
                raise ValueError("Invalid Salesforce field identifier")
        if not re.fullmatch(r"v\d+\.0", self.salesforce_version):
            raise ValueError("Invalid Salesforce API version")
        if not re.fullmatch(r"[a-z][a-z0-9-]*\.[A-Za-z_][\w]*\.[A-Za-z_][\w]*", self.bigquery_table):
            raise ValueError("BigQuery table must be project.dataset.table")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", self.bigquery_project):
            raise ValueError("Invalid BigQuery project")
        return self
