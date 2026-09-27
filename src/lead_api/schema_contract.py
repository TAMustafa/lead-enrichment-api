ALLOWED = {
    "date": {"date"},
    "datetime": {"datetime"},
    "string": {"string", "textarea", "phone", "url", "email", "picklist", "id"},
    "boolean": {"boolean"},
    "integer": {"int", "double"},
    "number": {"double", "currency", "percent"},
    "multipicklist": {"multipicklist"},
}


def validate_describe(obj, describe, mapper, settings):
    if not describe.get("createable") or not describe.get("updateable"):
        raise ValueError(f"{obj} must be createable and updateable")
    fields = {field["name"]: field for field in describe["fields"]}
    external = fields.get(settings.salesforce_external_field, {})
    if not external.get("externalId") or not external.get("unique") or not external.get("createable"):
        raise ValueError("Salesforce qualification key must be a writable Unique External ID")
    if external.get("type") != "string" or external.get("length", 0) < 64:
        raise ValueError("Salesforce qualification key must be Text(64+) ")
    for api_name, spec in mapper.mapping[obj].items():
        actual = fields.get(api_name, {})
        if not actual.get("createable") or not actual.get("updateable"):
            raise ValueError(f"{obj}.{api_name} must be writable")
        if actual.get("type") not in ALLOWED[spec.type]:
            raise ValueError(f"{obj}.{api_name} has an incompatible Salesforce data type")
        if spec.type in {"string", "multipicklist"} and spec.max_length > actual.get("length", 0):
            raise ValueError(f"{obj}.{api_name} mapping exceeds Salesforce field length")
        if actual.get("restrictedPicklist"):
            allowed = {v["value"] for v in actual.get("picklistValues", []) if v.get("active")}
            if spec.constant is not None and spec.constant not in allowed:
                raise ValueError(f"{obj}.{api_name} constant is not an active picklist value")
            if spec.source and (not spec.allowed_values or not set(spec.allowed_values) <= allowed):
                raise ValueError(f"{obj}.{api_name} requires explicit active picklist values")
    supplied = set(mapper.mapping[obj]) | {settings.salesforce_external_field}
    for name, field in fields.items():
        if (
            field.get("createable")
            and not field.get("nillable")
            and not field.get("defaultedOnCreate")
            and name not in supplied
        ):
            raise ValueError(f"Unmapped Salesforce required field: {obj}.{name}")


async def check_schema(salesforce, mapper, settings):
    # Called once at startup; no shared cache or additional infrastructure is needed.
    for obj in ("Lead", "Account"):
        response = await salesforce.request(
            "GET",
            f"{salesforce.base}/sobjects/{obj}/describe",
        )
        validate_describe(obj, response.json(), mapper, settings)
