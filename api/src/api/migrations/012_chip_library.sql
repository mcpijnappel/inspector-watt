-- The sensor library: one table per chip type (addresses, registers or commands, how bytes become values), shared by
-- every board. A board's chips (devices.bus_chips) name their type here. source: built-in, you or agent; checked: a
-- person compared the table with the datasheet.
CREATE TABLE chip_types (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    body JSONB NOT NULL,
    source TEXT NOT NULL DEFAULT 'you',
    agent TEXT,
    checked BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX chip_types_name ON chip_types (lower(name));
