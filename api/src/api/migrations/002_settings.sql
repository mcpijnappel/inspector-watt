-- Small app-wide preferences (battery sizes, ...), shared by every browser.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO settings (key, value) VALUES ('batteries', '{"sizes_mah": [1000], "selected_mah": 1000}')
ON CONFLICT (key) DO NOTHING;
