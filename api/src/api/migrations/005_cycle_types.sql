-- Cycle types per profile: kinds of cycles (measure-only wakes, uploads, upload-to-upload periods) and stretches
-- of a cycle (connect + upload). Phases are referenced by name:
-- [{"name", "begin": phase, "end": null | {"phase", "include": bool}, "must": [phase], "exclude": [phase]}]
-- No end: a cycle runs until the next begin. "must"/"exclude": it only counts with / without those phases.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS types JSONB NOT NULL DEFAULT '[]';
