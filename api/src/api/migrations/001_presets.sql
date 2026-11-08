-- Session presets for the record menu. config uses the same fields as POST /api/sessions.
CREATE TABLE IF NOT EXISTS presets (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    config     JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO presets (name, config) VALUES
    ('XIAO cycle test', '{"kind": "detail", "voltage_mv": 3700, "dut_power": true, "wake_threshold_ua": 5000, "max_duration_s": 300}'),
    ('Soak week', '{"kind": "soak", "voltage_mv": 3700, "dut_power": true, "wake_threshold_ua": 5000}'),
    ('Full battery', '{"kind": "detail", "voltage_mv": 4200, "dut_power": true, "wake_threshold_ua": 5000}')
ON CONFLICT (name) DO NOTHING;
