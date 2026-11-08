-- Devices (DUTs) on the bench: their defaults, a voltage limit the capture service enforces, and the phase
-- rules new sessions get. settings.device = {"id": ...} is the device on the bench right now.
CREATE TABLE IF NOT EXISTS devices (
    id                BIGSERIAL PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE,
    notes             TEXT NOT NULL DEFAULT '',
    voltage_mv        INT NOT NULL DEFAULT 3700,
    max_mv            INT NOT NULL DEFAULT 4200,
    wake_threshold_ua REAL NOT NULL DEFAULT 5000,
    uart_baud         INT,
    profile_id        BIGINT REFERENCES profiles (id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS device_id BIGINT REFERENCES devices (id) ON DELETE SET NULL;

INSERT INTO devices (name, notes, voltage_mv, max_mv, wake_threshold_ua, profile_id)
VALUES ('XIAO ESP32-C6', 'Seeed XIAO ESP32-C6 powered through its battery pins. Don''t plug USB in while measuring.',
        3700, 4200, 5000, NULL)  -- 008 gives it the default profile
ON CONFLICT (name) DO NOTHING;

UPDATE sessions SET device_id = (SELECT id FROM devices WHERE name = 'XIAO ESP32-C6') WHERE device_id IS NULL;

INSERT INTO settings (key, value)
VALUES ('device', jsonb_build_object('id', (SELECT id FROM devices WHERE name = 'XIAO ESP32-C6')))
ON CONFLICT (key) DO NOTHING;
