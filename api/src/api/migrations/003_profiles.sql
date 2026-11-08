-- Phase rules per firmware profile. A phase starts at the first log line matching its rule (after the
-- previous phase started) and runs until the next phase starts; the first rule starts a new cycle.
-- rules: [{"name", "color", "match": "contains" | "regex", "pattern", "snap": bool}]
CREATE TABLE IF NOT EXISTS profiles (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    rules      JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS profile_id BIGINT REFERENCES profiles (id) ON DELETE SET NULL;
