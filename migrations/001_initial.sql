BEGIN;
CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS lead_requests (
    lead_id uuid PRIMARY KEY,
    source text NOT NULL,
    source_lead_id text NOT NULL,
    fingerprint text NOT NULL,
    place_id text,
    resolution_confidence double precision,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source, source_lead_id)
);
-- Permanent reservation, committed BEFORE the external write. Never expire on a timer.
CREATE TABLE IF NOT EXISTS write_intents (
    place_id text PRIMARY KEY,
    lead_id uuid NOT NULL UNIQUE REFERENCES lead_requests(lead_id),
    external_key text NOT NULL UNIQUE,
    target_object text NOT NULL CHECK (target_object IN ('Lead','Account')),
    state text NOT NULL CHECK (state IN ('RESERVED','WRITTEN')),
    rules_version text NOT NULL,
    record_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS identity_claims (
    signal_key text PRIMARY KEY,
    place_id text NOT NULL REFERENCES write_intents(place_id)
);
CREATE TABLE IF NOT EXISTS lead_audit (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lead_id uuid NOT NULL REFERENCES lead_requests(lead_id),
    event text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lead_audit_lead_idx ON lead_audit(lead_id, created_at);
CREATE TABLE IF NOT EXISTS rate_limits (
    client_key text PRIMARY KEY,
    window_start timestamptz NOT NULL,
    request_count integer NOT NULL CHECK (request_count > 0)
);
INSERT INTO schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;
COMMIT;
