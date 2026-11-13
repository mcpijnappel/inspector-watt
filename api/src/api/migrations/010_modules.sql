-- Modules (modules/<name>/): their events land in the same events table, marked with the module instance that
-- made them ("analyzer:1"); the instances table lists each one (several analyzers, other backends later).
ALTER TABLE events ADD COLUMN IF NOT EXISTS module TEXT;
CREATE INDEX IF NOT EXISTS events_module ON events (session_id, module) WHERE module IS NOT NULL;

CREATE TABLE IF NOT EXISTS module_instances (
    id         BIGSERIAL PRIMARY KEY,
    module     TEXT NOT NULL,             -- the module ("analyzer")
    backend    TEXT NOT NULL,             -- how it talks to its hardware ("sigrok")
    name       TEXT NOT NULL,             -- what the user sees ("nanoDLA")
    config     JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (module, name)
);
