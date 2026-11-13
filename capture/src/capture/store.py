"""TimescaleDB access for the capture service (sessions + everything the recorder produces)."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)


class StoreUnavailable(RuntimeError):
    pass


class Store:
    """One connection, serialized with a lock. Reconnects on the next call after a failure."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._conn: psycopg.Connection | None = None
        self._lock = threading.Lock()

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            try:
                # statement_timeout: a hanging query becomes an error (and a retry) instead of blocking recording
                self._conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row, connect_timeout=5,
                                             options="-c statement_timeout=15000")
            except psycopg.OperationalError as e:
                raise StoreUnavailable(f"database not reachable: {e}") from e
        return self._conn

    def _run(self, fn):
        with self._lock:
            try:
                return fn(self._connection())
            except psycopg.OperationalError as e:
                if self._conn is not None:
                    self._conn.close()
                self._conn = None
                raise StoreUnavailable(str(e)) from e

    def ping(self) -> bool:
        try:
            self._run(lambda c: c.execute("SELECT 1"))
            return True
        except StoreUnavailable:
            return False

    def active_device(self) -> dict | None:
        """The device on the bench (settings.device), with its voltage limit and UART speed. The tables come from the api's
        migrations, so this can fail until the api has started once."""
        return self._run(lambda c: c.execute(
            "SELECT d.id, d.name, d.max_mv, d.uart_baud FROM settings s JOIN devices d ON d.id = (s.value->>'id')::bigint "
            "WHERE s.key = 'device'").fetchone())

    def saved_settings(self) -> dict | None:
        """Capture settings saved by the api (settings.capture): they override the environment's defaults."""
        r = self._run(lambda c: c.execute("SELECT value FROM settings WHERE key = 'capture'").fetchone())
        return r["value"] if r else None

    # sessions --------------------------------------------------------------------------------------------
    def create_session(self, name: str, kind: str, started_at: datetime, config: dict, notes: str) -> int:
        def q(c):
            return c.execute(
                "INSERT INTO sessions (name, kind, started_at, config, notes) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (name, kind, started_at, Jsonb(config), notes)).fetchone()["id"]
        return self._run(q)

    def running_session(self) -> dict | None:
        return self._run(lambda c: c.execute("SELECT * FROM sessions WHERE status = 'running'").fetchone())

    def finish_session(self, session_id: int, status: str, stopped_at: datetime) -> None:
        self._run(lambda c: c.execute(
            "UPDATE sessions SET status = %s, stopped_at = %s WHERE id = %s", (status, stopped_at, session_id)))

    def buffer_segments_before(self, session_id: int, idx: int) -> list[dict]:
        return self._run(lambda c: c.execute(
            "SELECT id, path FROM raw_segments WHERE session_id = %s AND kind = 'buffer' AND start_idx + n_samples <= %s",
            (session_id, idx)).fetchall())

    def delete_segments(self, ids: list[int]) -> None:
        self._run(lambda c: c.execute("DELETE FROM raw_segments WHERE id = ANY(%s)", (ids,)))

    # recorder output -------------------------------------------------------------------------------------
    def write_batch(self, session_id: int, batch: dict) -> None:
        """Write everything the recorder produced in one transaction (all or nothing, so retries are safe)."""
        def q(c: psycopg.Connection):
            with c.transaction(), c.cursor() as cur:
                if batch["rollups"]:
                    cur.executemany(
                        "INSERT INTO rollup_10ms (session_id, ts, i_min, i_max, i_sum, n, logic) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", batch["rollups"])
                if batch["uart"]:
                    cur.executemany(
                        "INSERT INTO uart_lines (session_id, ts, sample_idx, wall, text) VALUES (%s, %s, %s, %s, %s)",
                        batch["uart"])
                for ev in batch["events"]:
                    cur.execute(
                        "INSERT INTO events (session_id, kind, start_idx, end_idx, ts, end_ts, peak_a, mean_a, "
                        "charge_c, data, module) VALUES (%(session_id)s, %(kind)s, %(start_idx)s, %(end_idx)s, %(ts)s, "
                        "%(end_ts)s, %(peak_a)s, %(mean_a)s, %(charge_c)s, %(data)s, %(module)s)",
                        {**ev, "session_id": session_id, "data": Jsonb(ev.get("data", {})), "module": ev.get("module")})
                for seg in batch["segments"]:
                    cur.execute(
                        "INSERT INTO raw_segments (session_id, kind, start_idx, n_samples, path, bytes) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (session_id, seg["kind"], seg["start_idx"], seg["n_samples"], seg["path"], seg["bytes"]))
                if batch["progress"] is not None:
                    n_samples, charge = batch["progress"]
                    cur.execute("UPDATE sessions SET n_samples = %s, charge_c = %s WHERE id = %s",
                                (n_samples, charge, session_id))
        self._run(q)
