-- Inspector Watt schema. Runs once, when the database volume is first created.
--
-- Timeline: every session has its own sample index (0 = session start, 1 step = 10 us on the PPK2 clock).
-- ts columns are started_at + index * 10 us, so all tables line up exactly with the raw Parquet files.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE sessions (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('detail', 'soak')),
    status          TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'stopped', 'interrupted')),
    started_at      TIMESTAMPTZ NOT NULL,   -- time of session sample 0
    stopped_at      TIMESTAMPTZ,
    sample_period_s DOUBLE PRECISION NOT NULL DEFAULT 0.00001,
    n_samples       BIGINT NOT NULL DEFAULT 0,             -- processed so far (= duration / 10 us)
    charge_c        DOUBLE PRECISION NOT NULL DEFAULT 0,   -- total charge drawn (coulomb)
    config          JSONB NOT NULL DEFAULT '{}',           -- voltage, thresholds, margins, ...
    notes           TEXT NOT NULL DEFAULT ''
);
-- Only one PPK2, so only one running session.
CREATE UNIQUE INDEX sessions_one_running ON sessions ((true)) WHERE status = 'running';

-- 10 ms summaries, written by the capture service. Buckets without any valid sample are not stored (= gap).
CREATE TABLE rollup_10ms (
    session_id BIGINT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    i_min      REAL,
    i_max      REAL,
    i_sum      DOUBLE PRECISION NOT NULL,   -- sum of valid samples (A); charge = i_sum * sample_period_s
    n          INTEGER NOT NULL,            -- valid samples (1000 when nothing was lost)
    logic      SMALLINT NOT NULL DEFAULT 0  -- OR of logic inputs D0-D7
);
SELECT create_hypertable('rollup_10ms', by_range('ts', INTERVAL '1 day'));
CREATE UNIQUE INDEX ON rollup_10ms (session_id, ts);
ALTER TABLE rollup_10ms SET (timescaledb.compress, timescaledb.compress_segmentby = 'session_id',
                             timescaledb.compress_orderby = 'ts');
SELECT add_compression_policy('rollup_10ms', INTERVAL '1 day');

-- Coarser levels, maintained by TimescaleDB. materialized_only = false: the newest data is always included.
CREATE MATERIALIZED VIEW rollup_1s WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT session_id, time_bucket(INTERVAL '1 second', ts) AS bucket,
       min(i_min) AS i_min, max(i_max) AS i_max, sum(i_sum) AS i_sum, sum(n)::BIGINT AS n,
       bit_or(logic) AS logic
FROM rollup_10ms GROUP BY session_id, bucket WITH NO DATA;

CREATE MATERIALIZED VIEW rollup_1m WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT session_id, time_bucket(INTERVAL '1 minute', bucket) AS bucket,
       min(i_min) AS i_min, max(i_max) AS i_max, sum(i_sum) AS i_sum, sum(n)::BIGINT AS n,
       bit_or(logic) AS logic
FROM rollup_1s GROUP BY session_id, time_bucket(INTERVAL '1 minute', bucket) WITH NO DATA;

CREATE MATERIALIZED VIEW rollup_1h WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT session_id, time_bucket(INTERVAL '1 hour', bucket) AS bucket,
       min(i_min) AS i_min, max(i_max) AS i_max, sum(i_sum) AS i_sum, sum(n)::BIGINT AS n,
       bit_or(logic) AS logic
FROM rollup_1m GROUP BY session_id, time_bucket(INTERVAL '1 hour', bucket) WITH NO DATA;

SELECT add_continuous_aggregate_policy('rollup_1s', start_offset => INTERVAL '6 hours',
    end_offset => INTERVAL '2 seconds', schedule_interval => INTERVAL '1 minute');
SELECT add_continuous_aggregate_policy('rollup_1m', start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '2 minutes', schedule_interval => INTERVAL '10 minutes');
SELECT add_continuous_aggregate_policy('rollup_1h', start_offset => INTERVAL '60 days',
    end_offset => INTERVAL '2 hours', schedule_interval => INTERVAL '1 hour');

CREATE TABLE uart_lines (
    session_id BIGINT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,   -- on the session timeline
    sample_idx BIGINT,                 -- session sample index; NULL if the PPK2 wasn't streaming
    wall       TIMESTAMPTZ NOT NULL,   -- host clock when received
    text       TEXT NOT NULL
);
SELECT create_hypertable('uart_lines', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON uart_lines (session_id, ts);

-- Things that happened: wake (current above threshold), gap (no data), reconnect, dut_restore, resume,
-- voltage, low_disk, ...
CREATE TABLE events (
    id         BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    start_idx  BIGINT NOT NULL,
    end_idx    BIGINT,
    ts         TIMESTAMPTZ NOT NULL,
    end_ts     TIMESTAMPTZ,
    peak_a     REAL,
    mean_a     REAL,
    charge_c   DOUBLE PRECISION,
    data       JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX ON events (session_id, start_idx);

-- Full-rate raw data lives in Parquet files under DATA_DIR; this table says which file covers what.
CREATE TABLE raw_segments (
    id         BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('detail', 'event', 'buffer')),
    start_idx  BIGINT NOT NULL,
    n_samples  BIGINT NOT NULL,
    path       TEXT NOT NULL,   -- relative to DATA_DIR
    bytes      BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON raw_segments (session_id, start_idx);
