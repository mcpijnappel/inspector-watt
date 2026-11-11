"""api service: history queries over TimescaleDB + Parquet, and a pass-through to the capture service."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request, Response
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from .migrate import migrate
from . import ota
from . import resets as reset_finder
from .agents import AgentAccess
from .phases import (compile_wake, cycle_json, find_cycles, match_counts, mix_current, mix_parts, phase_rows, phase_stats, sleep_rule, snap_time,
                     type_instances, type_kind, type_summary, type_views)
from .trace import MAX_RAW_SAMPLES, SAMPLE_PERIOD_S, TIERS, bin_samples, db_trace, pick_tier, raw_from_files

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@db/tracker")
CAPTURE_URL = os.environ.get("CAPTURE_URL", "http://capture:8100")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MCP_URL = os.environ.get("MCP_URL", "http://mcp:8300")
OTA_PORT = int(os.environ.get("OTA_PORT", str(ota.OTA_PORT)))  # Arduino OTA endpoint (UDP); 0 = off
OTA_PUBLIC_PORT = int(os.environ.get("OTA_PUBLIC_PORT", str(OTA_PORT)))  # where compose publishes it (shown in Settings)

log = logging.getLogger("api")
pool = AsyncConnectionPool(DATABASE_URL, open=False, min_size=1, max_size=8,
                           check=AsyncConnectionPool.check_connection,  # survive database restarts
                           kwargs={"row_factory": dict_row, "autocommit": True})
capture = httpx.AsyncClient(base_url=CAPTURE_URL, timeout=10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open(wait=True, timeout=60)
    await migrate(pool)
    await seed_mcp()
    await seed_flash()
    ota_transport = None
    if OTA_PORT:
        try:
            ota_transport, _ = await ota.start(ota_token, ota_submit, ota_result, port=OTA_PORT)
            log.info("Arduino OTA endpoint on UDP %d", OTA_PORT)
        except OSError as e:
            log.warning("no Arduino OTA endpoint: UDP %d: %s", OTA_PORT, e)
    yield
    if ota_transport:
        ota_transport.close()
    await pool.close()
    await capture.aclose()


app = FastAPI(title="inspector-watt api", lifespan=lifespan)


async def q_all(sql: str, params=None) -> list[dict]:
    async with pool.connection() as conn:
        return await (await conn.execute(sql, params)).fetchall()


async def q_one(sql: str, params=None) -> dict | None:
    rows = await q_all(sql, params)
    return rows[0] if rows else None


def summarize(s: dict) -> dict:
    dur = s["n_samples"] * s["sample_period_s"]
    return {**s, "duration_s": dur, "avg_current_a": s["charge_c"] / dur if dur > 0 else None}


async def get_session(sid: int) -> dict:
    s = await q_one("SELECT s.*, d.name AS device_name FROM sessions s LEFT JOIN devices d ON d.id = s.device_id "
                    "WHERE s.id = %s", (sid,))
    if s is None:
        raise HTTPException(404, "session not found")
    return summarize(s)


def time_range(s: dict, start_s: float | None, end_s: float | None) -> tuple[float, float]:
    a = max(0.0, start_s or 0.0)
    b = s["duration_s"] if end_s is None else min(end_s, s["duration_s"])
    if b <= a:
        raise HTTPException(400, "empty time range")
    return a, b


async def capture_get(path: str, **params) -> dict | None:
    try:
        r = await capture.get(path, params=params)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


@app.get("/api/ready")
async def ready():
    """Answers once the api is up, i.e. after the migrations ran (compose's healthcheck: capture waits for it)."""
    return {"ok": True}


# sessions -------------------------------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    db = await q_one("SELECT 1 AS ok")
    cap = await capture_get("/status")
    return {"db": bool(db), "capture": cap is not None, "mcp": (await mcp_health())["up"]}


@app.get("/api/sessions")
async def list_sessions():
    rows = await q_all("""
        SELECT s.*, d.name AS device_name,
               (SELECT count(*) FROM events e WHERE e.session_id = s.id AND e.kind = 'wake') AS wakes,
               (SELECT coalesce(sum(bytes), 0)::bigint FROM raw_segments r WHERE r.session_id = s.id) AS raw_bytes
        FROM sessions s LEFT JOIN devices d ON d.id = s.device_id ORDER BY s.id DESC""")
    return [summarize(r) for r in rows]


@app.get("/api/sessions/{sid}")
async def session_detail(sid: int):
    s = await get_session(sid)
    s["events"] = {r["kind"]: r["n"] for r in await q_all(
        "SELECT kind, count(*) AS n FROM events WHERE session_id = %s GROUP BY kind", (sid,))}
    s["raw"] = {r["kind"]: {"files": r["files"], "bytes": r["bytes"], "seconds": r["samples"] * SAMPLE_PERIOD_S}
                for r in await q_all(
        "SELECT kind, count(*) AS files, sum(bytes)::bigint AS bytes, sum(n_samples)::bigint AS samples FROM raw_segments "
        "WHERE session_id = %s GROUP BY kind", (sid,))}
    s["uart_lines"] = (await q_one("SELECT count(*) AS n FROM uart_lines WHERE session_id = %s", (sid,)))["n"]
    s["resets"] = reset_finder.summary(await session_resets(s, s["uart_lines"]))
    return s


_reset_cache: dict[tuple[int, int], list[dict]] = {}


async def session_resets(s: dict, n_lines: int | None = None) -> list[dict]:
    """Unexpected board resets in a session (see resets.py). Cached until the session has more log lines."""
    if n_lines is None:
        n_lines = (await q_one("SELECT count(*) AS n FROM uart_lines WHERE session_id = %s", (s["id"],)))["n"]
    key = (s["id"], n_lines)
    if key in _reset_cache:
        return _reset_cache[key]
    # Only the resets that aren't deep-sleep wakes, with the 10 s before each, not the whole log.
    rst = await q_all("SELECT ts FROM uart_lines WHERE session_id = %s AND text LIKE '%%rst:0x%%' "
                      "AND text NOT ILIKE '%%SLEEP%%' ORDER BY ts", (s["id"],))
    lines: list[tuple[float, str]] = []
    seen: set[float] = set()
    for r in rst:
        for x in await q_all("SELECT extract(epoch FROM ts - %s)::float8 AS t, text FROM uart_lines WHERE session_id = %s "
                             "AND ts BETWEEN %s AND %s ORDER BY ts",
                             (s["started_at"], s["id"], r["ts"] - timedelta(seconds=reset_finder.LOOK_BACK_S), r["ts"])):
            if x["t"] not in seen:
                seen.add(x["t"])
                lines.append((x["t"], x["text"]))
    lines.sort()
    on = [r["t"] for r in await q_all("SELECT start_idx * %s AS t FROM events WHERE session_id = %s "
                                       "AND kind IN ('power_on', 'dut_restore')", (SAMPLE_PERIOD_S, s["id"]))]
    flashes = [(r["t"], r["end_t"]) for r in await q_all(
        "SELECT start_idx * %(p)s AS t, coalesce(end_idx, start_idx) * %(p)s AS end_t FROM events "
        "WHERE session_id = %(sid)s AND kind = 'flash'", {"p": SAMPLE_PERIOD_S, "sid": s["id"]})]
    out = reset_finder.find_resets(lines, on, flashes)
    if len(_reset_cache) > 64:
        _reset_cache.clear()
    _reset_cache[key] = out
    return out


class SessionPatch(BaseModel):
    name: str | None = None
    notes: str | None = None
    profile_id: int | None = None


@app.patch("/api/sessions/{sid}")
async def update_session(sid: int, body: SessionPatch):
    await get_session(sid)
    await q_all("UPDATE sessions SET name = coalesce(%s, name), notes = coalesce(%s, notes), "
                "profile_id = coalesce(%s, profile_id) WHERE id = %s RETURNING id", (body.name, body.notes, body.profile_id, sid))
    return await get_session(sid)


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: int):
    s = await get_session(sid)
    if s["status"] == "running":
        raise HTTPException(409, "stop the session first")
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM rollup_10ms WHERE session_id = %s", (sid,))
            await conn.execute("DELETE FROM uart_lines WHERE session_id = %s", (sid,))
            await conn.execute("DELETE FROM sessions WHERE id = %s", (sid,))  # cascades events + raw_segments
    shutil.rmtree(DATA_DIR / "sessions" / str(sid), ignore_errors=True)
    return {"deleted": sid}


# data -----------------------------------------------------------------------------------------------------
async def live_window(s: dict, i0: int, i1: int, points: int) -> dict | None:
    """Serve [i0, i1) from the capture RAM buffer if this is the running session and the buffer covers it."""
    live = await live_s0(s)
    if live is None:
        return None
    s0, buf = live
    if i0 + s0 < buf["start_idx"] or i1 + s0 > buf["end_idx"]:
        return None
    w = await capture_get("/buffer/window", start=i0 + s0, end=i1 + s0, points=points)
    if w is None:
        return None
    k = w["bin_samples"]
    first = w["start"] - s0
    if k == 1:
        cur = w["current"]
        cols = {"min": cur, "max": cur, "mean": cur}
    else:
        cols = {"min": w["min"], "max": w["max"], "mean": w["mean"]}
    x = ((first + np.arange(len(cols["mean"])) * k) * SAMPLE_PERIOD_S).tolist()
    return {"tier": "raw", "bucket_s": k * SAMPLE_PERIOD_S, "x": x, **cols, "source": "live"}


@app.get("/api/sessions/{sid}/trace")
async def trace(sid: int, start_s: float | None = None, end_s: float | None = None,
                points: int = Query(2000, ge=10, le=20000)):
    """About `points` buckets of min/max/mean current (A) over [start_s, end_s) seconds from session start.
    x = bucket start in seconds. Null values mark gaps."""
    s = await get_session(sid)
    a, b = time_range(s, start_s, end_s)
    tier = pick_tier(b - a, points)
    if tier is None:  # finer than 10 ms per bucket: raw data
        i0, i1 = math.floor(a / SAMPLE_PERIOD_S), math.ceil(b / SAMPLE_PERIOD_S)
        live = await live_window(s, i0, i1, points)
        if live:
            return {**live, "start_s": a, "end_s": b}
        if i1 - i0 <= MAX_RAW_SAMPLES:
            async with pool.connection() as conn:
                cur, coverage = await raw_from_files(conn, DATA_DIR, sid, i0, i1)
            if coverage > 0.5:
                return {**bin_samples(i0, cur, points), "source": "parquet", "coverage": coverage,
                        "start_s": a, "end_s": b}
        tier = TIERS[0]  # no raw stored here (e.g. deep sleep in a soak session): best summary we have
        async with pool.connection() as conn:
            out = await db_trace(conn, s, tier, a, b, points)
        return {**out, "source": "db", "raw_available": False, "start_s": a, "end_s": b}
    async with pool.connection() as conn:
        out = await db_trace(conn, s, tier, a, b, points)
    return {**out, "source": "db", "start_s": a, "end_s": b}


async def live_s0(s: dict) -> tuple[int, dict] | None:
    """(s0, buffer info) if `s` is the running session, so its recent data can come from the RAM buffer."""
    if s["status"] != "running":
        return None
    st = await capture_get("/status")
    sess = (st or {}).get("session") or {}
    if not sess.get("active") or sess["session"]["id"] != s["id"]:
        return None
    return sess["session"]["s0"], st["buffer"]


EXACT_MAX_SAMPLES = 6_000_000  # 60 s of raw data is read for exact statistics; longer ranges use summaries


@app.get("/api/sessions/{sid}/stats")
async def stats(sid: int, start_s: float | None = None, end_s: float | None = None):
    """Average current, charge, min/max over a range, plus wake statistics.
    Exact (from raw samples) when the range is short enough and raw data exists; otherwise from summaries."""
    s = await get_session(sid)
    a, b = time_range(s, start_s, end_s)
    t0 = s["started_at"]
    ta, tb = t0 + timedelta(seconds=a), t0 + timedelta(seconds=b)
    w = await q_one("""SELECT count(*) AS n, avg(end_idx - start_idx)::float8 * %s AS avg_duration_s,
                              avg(charge_c) AS avg_charge_c, max(peak_a) AS max_peak_a, sum(charge_c) AS charge_c
                       FROM events WHERE session_id = %s AND kind = 'wake' AND ts >= %s AND ts < %s""",
                    (SAMPLE_PERIOD_S, sid, ta, tb))
    i0, i1 = math.floor(a / SAMPLE_PERIOD_S), math.ceil(b / SAMPLE_PERIOD_S)
    out = None
    if i1 - i0 <= EXACT_MAX_SAMPLES:
        live = await live_s0(s)
        if live and i0 + live[0] >= live[1]["start_idx"] and i1 + live[0] <= live[1]["end_idx"]:
            r = await capture_get("/buffer/stats", start=i0 + live[0], end=i1 + live[0])
            if r and r["mean_a"] is not None:
                out = {"mean_a": r["mean_a"], "charge_c": r["charge_c"], "min_a": r["min_a"], "max_a": r["max_a"],
                       "coverage": 1 - r["lost"] / max(1, i1 - i0), "exact": True}
        if out is None:
            async with pool.connection() as conn:
                cur, coverage = await raw_from_files(conn, DATA_DIR, sid, i0, i1)
            if coverage > 0.999:
                v = cur[~np.isnan(cur)].astype(np.float64)
                if len(v):
                    out = {"mean_a": float(v.mean()), "charge_c": float(v.sum()) * SAMPLE_PERIOD_S,
                           "min_a": float(v.min()), "max_a": float(v.max()), "coverage": coverage, "exact": True}
    if out is None:
        _, _, table, tcol = pick_tier(b - a, 1000) or TIERS[0]
        r = await q_one(f"SELECT sum(i_sum) AS s, sum(n)::bigint AS n, min(i_min) AS mn, max(i_max) AS mx FROM {table} "
                        f"WHERE session_id = %s AND {tcol} >= %s AND {tcol} < %s", (sid, ta, tb))
        n = int(r["n"] or 0)
        out = {"mean_a": float(r["s"]) / n if n else None, "charge_c": float(r["s"] or 0) * SAMPLE_PERIOD_S,
               "min_a": r["mn"], "max_a": r["mx"], "coverage": n * SAMPLE_PERIOD_S / (b - a), "exact": False}
    total = out["charge_c"] or 1
    return {"start_s": a, "end_s": b, "duration_s": b - a, **out,
            "wakes": {**w, "charge_share": (w["charge_c"] or 0) / total}}


@app.get("/api/sessions/{sid}/events")
async def events(sid: int, kind: str | None = None, start_s: float | None = None, end_s: float | None = None,
                 limit: int = Query(1000, le=20000)):
    s = await get_session(sid)
    t0 = s["started_at"]
    a, b = start_s or 0.0, end_s if end_s is not None else s["duration_s"] + 1
    rows = await q_all("""SELECT id, kind, start_idx * %(p)s AS t, end_idx * %(p)s AS end_t, peak_a, mean_a,
                                 charge_c, data
                          FROM events WHERE session_id = %(sid)s AND (%(kind)s::text IS NULL OR kind = %(kind)s)
                            AND ts >= %(a)s AND ts < %(b)s ORDER BY start_idx LIMIT %(limit)s""",
                       {"p": SAMPLE_PERIOD_S, "sid": sid, "kind": kind, "limit": limit,
                        "a": t0 + timedelta(seconds=a), "b": t0 + timedelta(seconds=b)})
    if kind in (None, "reset"):
        # Unexpected resets come from the serial log, not the events table.
        rs = [{"id": f"reset-{i}", "kind": "reset", "t": r["t"], "end_t": None, "peak_a": None, "mean_a": None, "charge_c": None,
               "data": {k: r[k] for k in ("label", "chip", "code", "explanation", "panic")}}
              for i, r in enumerate(await session_resets(s)) if a <= r["t"] < b]
        rows = sorted([*rows, *rs], key=lambda e: e["t"])[:limit]
    return rows


@app.get("/api/sessions/{sid}/uart")
async def uart(sid: int, start_s: float | None = None, end_s: float | None = None, q: str | None = None,
               limit: int = Query(1000, le=20000)):
    s = await get_session(sid)
    t0 = s["started_at"]
    a, b = start_s or 0.0, end_s if end_s is not None else s["duration_s"] + 1
    return await q_all("""SELECT extract(epoch FROM ts - %(t0)s)::float8 AS t, sample_idx, wall, text FROM uart_lines
                          WHERE session_id = %(sid)s AND ts >= %(a)s AND ts < %(b)s
                            AND (%(q)s::text IS NULL OR text ILIKE '%%' || %(q)s || '%%')
                          ORDER BY ts LIMIT %(limit)s""",
                       {"t0": t0, "sid": sid, "q": q, "limit": limit,
                        "a": t0 + timedelta(seconds=a), "b": t0 + timedelta(seconds=b)})


# presets and last settings (record menu) ------------------------------------------------------------------
def form_from_config(c: dict) -> dict:
    """Session config as stored by capture -> the fields of POST /api/sessions."""
    out = {"kind": c.get("kind"), "voltage_mv": c.get("voltage_mv"), "dut_power": c.get("dut_power", True),
           "auto_restore": c.get("auto_restore", True), "uart_baud": c.get("uart_baud"),
           "raw_buffer_hours": c.get("raw_buffer_hours", 0), "max_duration_s": c.get("max_duration_s"),
           "battery": c.get("battery")}
    for src, dst, k in (("wake_threshold_a", "wake_threshold_ua", 1e6), ("hold_s", "hold_ms", 1e3),
                        ("pre_s", "pre_ms", 1e3), ("post_s", "post_ms", 1e3)):
        if c.get(src) is not None:
            out[dst] = round(c[src] * k, 3)
    return out


@app.get("/api/last-session-config")
async def last_config():
    """Settings of the last session of the device on the bench; its defaults if it has none yet."""
    dev = await active_device()
    if dev:
        s = await q_one("SELECT name, config FROM sessions WHERE device_id = %s ORDER BY id DESC LIMIT 1", (dev["id"],))
        if s is None:
            return {"name": None, "config": {"voltage_mv": dev["voltage_mv"], "wake_threshold_ua": dev["wake_threshold_ua"],
                                             "uart_baud": dev["uart_baud"], "dut_power": True}}
    else:
        s = await q_one("SELECT name, config FROM sessions ORDER BY id DESC LIMIT 1")
    return None if s is None else {"name": s["name"], "config": form_from_config(s["config"])}


class PresetIn(BaseModel):
    name: str
    config: dict


@app.get("/api/presets")
async def list_presets():
    return await q_all("SELECT id, name, config FROM presets ORDER BY id")


@app.post("/api/presets")
async def save_preset(body: PresetIn):
    """Create, or replace the preset with the same name."""
    return await q_one("INSERT INTO presets (name, config) VALUES (%s, %s) ON CONFLICT (name) "
                       "DO UPDATE SET config = EXCLUDED.config RETURNING id, name, config",
                       (body.name, Jsonb(body.config)))


@app.delete("/api/presets/{pid}")
async def delete_preset(pid: int):
    await q_all("DELETE FROM presets WHERE id = %s RETURNING id", (pid,))
    return {"deleted": pid}


# phase rules ------------------------------------------------------------------------------------------------
class RuleIn(BaseModel):
    name: str
    color: str = "#8b949e"
    match: str = "contains"
    pattern: str
    snap: bool = False


class TypeEndIn(BaseModel):
    phase: str
    include: bool = False


class TypeIn(BaseModel):
    """A cycle type: begins at a phase, optionally ends at one, and must have / may not have some phases."""
    name: str = Field(min_length=1, max_length=60)
    begin: str
    end: TypeEndIn | None = None
    must: list[str] = []
    exclude: list[str] = []
    woken_by: list[str] = []  # only cycles that begin in a wake with one of these wake reasons


class ProfileIn(BaseModel):
    name: str | None = None
    rules: list[RuleIn] | None = None
    types: list[TypeIn] | None = None
    # Regex for the log line that says what woke the board; its first group is the reason. "" = the default.
    wake_pattern: str | None = Field(None, max_length=300)


def check_wake(pattern: str | None) -> str | None:
    if not pattern:
        return pattern
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise HTTPException(422, f"wake reason line: {e}") from e
    if rx.groups < 1:
        raise HTTPException(422, "wake reason line: put the reason in brackets, e.g. Wake source: (.+)")
    return pattern


def check_rules(rules: list[RuleIn]) -> list[dict]:
    out = []
    for r in rules:
        if r.match not in ("contains", "regex"):
            raise HTTPException(422, f"rule '{r.name}': match must be 'contains' or 'regex'")
        if not r.pattern:
            raise HTTPException(422, f"rule '{r.name}': empty pattern")
        if r.match == "regex":
            try:
                re.compile(r.pattern)
            except re.error as e:
                raise HTTPException(422, f"rule '{r.name}': {e}") from e
        out.append(r.model_dump())
    return out


def check_types(types: list[TypeIn], rules: list[dict]) -> list[dict]:
    names = {r["name"] for r in rules}
    seen: set[str] = set()
    out = []
    for t in types:
        name = t.name.strip()
        if name in seen:
            raise HTTPException(422, f"two cycle types are called '{name}'")
        seen.add(name)
        used = [t.begin, *t.must, *t.exclude] + ([t.end.phase] if t.end else [])
        unknown = [n for n in used if n not in names]
        if unknown:
            raise HTTPException(422, f"cycle type '{name}': no phase called '{unknown[0]}'")
        if t.end and t.end.phase == t.begin:
            raise HTTPException(422, f"cycle type '{name}': it can't end at the phase it begins at")
        if set(t.must) & set(t.exclude):
            raise HTTPException(422, f"cycle type '{name}': a phase can't be both required and excluded")
        out.append({**t.model_dump(), "name": name})
    return out


PROFILE_COLS = "id, name, rules, types, wake_pattern, updated_at"


@app.get("/api/profiles")
async def list_profiles():
    return await q_all(f"SELECT {PROFILE_COLS} FROM profiles ORDER BY id")


@app.post("/api/profiles")
async def create_profile(body: ProfileIn):
    if not body.name:
        raise HTTPException(422, "name required")
    rules = check_rules(body.rules or [])
    types = check_types(body.types or [], rules)
    try:
        return await q_one(f"INSERT INTO profiles (name, rules, types, wake_pattern) VALUES (%s, %s, %s, %s) RETURNING {PROFILE_COLS}",
                           (body.name, Jsonb(rules), Jsonb(types), check_wake(body.wake_pattern) or None))
    except psycopg.errors.UniqueViolation as e:
        raise HTTPException(409, "a profile with that name exists") from e


@app.put("/api/profiles/{pid}")
async def update_profile(pid: int, body: ProfileIn):
    rules = None if body.rules is None else check_rules(body.rules)
    types = None
    if body.types is not None:
        if rules is None:
            cur = await q_one("SELECT rules FROM profiles WHERE id = %s", (pid,))
            if cur is None:
                raise HTTPException(404, "profile not found")
        types = check_types(body.types, rules if rules is not None else cur["rules"])
    elif rules is not None:
        # Rules changed without types: keep the types whose phases still exist.
        cur = await q_one("SELECT types FROM profiles WHERE id = %s", (pid,))
        names = {r["name"] for r in rules}
        if cur is not None:
            types = [t for t in cur["types"] if {t["begin"], *t.get("must", []), *t.get("exclude", []),
                                                 *([t["end"]["phase"]] if t.get("end") else [])} <= names]
    wake_set = body.wake_pattern is not None
    r = await q_one(f"UPDATE profiles SET name = coalesce(%s, name), rules = coalesce(%s, rules), "
                    f"types = coalesce(%s, types), wake_pattern = CASE WHEN %s THEN %s ELSE wake_pattern END, "
                    f"updated_at = now() WHERE id = %s RETURNING {PROFILE_COLS}",
                    (body.name, None if rules is None else Jsonb(rules), None if types is None else Jsonb(types),
                     wake_set, check_wake(body.wake_pattern) or None, pid))
    if r is None:
        raise HTTPException(404, "profile not found")
    return r


@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: int):
    await q_all("DELETE FROM profiles WHERE id = %s RETURNING id", (pid,))
    return {"deleted": pid}


async def session_profile(s: dict, profile_id: int | None) -> dict | None:
    pid = profile_id or s.get("profile_id")
    if pid:
        p = await q_one(f"SELECT {PROFILE_COLS} FROM profiles WHERE id = %s", (pid,))
        if p:
            return p
    # Not linked (e.g. saved from the live buffer): the profile used most recently.
    return await q_one("SELECT p.id, p.name, p.rules, p.types, p.wake_pattern, p.updated_at FROM profiles p LEFT JOIN sessions s ON s.profile_id = p.id "
                       "ORDER BY s.id DESC NULLS LAST, p.id LIMIT 1")


async def session_lines(s: dict) -> list[tuple[float, str]]:
    rows = await q_all("SELECT extract(epoch FROM ts - %s)::float8 AS t, text FROM uart_lines WHERE session_id = %s "
                       "ORDER BY ts", (s["started_at"], s["id"]))
    return [(r["t"], r["text"]) for r in rows]


_phase_cache: dict[tuple, tuple[dict, list]] = {}


@app.get("/api/sessions/{sid}/phases")
async def session_phases(sid: int, profile_id: int | None = None):
    """Cycles and phases of a session (from its profile's rules), with charge per phase, averages and the cycles
    of each cycle type."""
    return (await phases_and_cycles(sid, profile_id))[0]


async def phases_and_cycles(sid: int, profile_id: int | None = None) -> tuple[dict, list]:
    s = await get_session(sid)
    prof = await session_profile(s, profile_id)
    if prof is None:
        return {"profile": None, "cycles": [], "types": [], "stats": [], "sleep_rule": -1}, []
    key = (sid, prof["id"], prof["updated_at"], s["n_samples"])
    if key in _phase_cache:
        return _phase_cache[key]
    rules = prof["rules"]
    cycles = find_cycles(await session_lines(s), rules, s["duration_s"], compile_wake(prof.get("wake_pattern")))
    # Snap boundaries of rules that ask for it onto the nearest current step (raw data).
    async with pool.connection() as conn:
        for c in cycles:
            for i, p in enumerate(c.phases):
                if not rules[p.rule].get("snap") or i == 0:
                    continue
                a = max(0.0, p.t - 0.02)
                i0, i1 = round(a / SAMPLE_PERIOD_S), round((p.t + 0.02) / SAMPLE_PERIOD_S)
                cur, cov = await raw_from_files(conn, DATA_DIR, sid, i0, i1)
                t = snap_time(cur, i0 * SAMPLE_PERIOD_S, p.t) if cov > 0.8 else None
                if t is not None and c.phases[i - 1].t < t < p.end_t:
                    p.t = t
                    c.phases[i - 1].end_t = t
        await phase_stats(conn, s, cycles)
    out = {
        "profile": {"id": prof["id"], "name": prof["name"], "rules": rules, "types": prof["types"],
                    "wake_pattern": prof.get("wake_pattern")},
        "cycles": [cycle_json(i, c) for i, c in enumerate(cycles)],
        "types": types_json(cycles, rules, prof["types"]),
    }
    # The numbers the pages show, from the same code as the cycles and battery endpoints (and so the MCP tools):
    # per type (All wakes first, then the profile's types in order) and the sleep phase.
    views = type_views(cycles, rules, prof["types"])
    out["stats"] = [type_summary(v, rules) for v in views]
    out["sleep_rule"] = sleep_rule(views[0], len(rules))
    if len(_phase_cache) > 32:
        _phase_cache.clear()
    _phase_cache[key] = (out, cycles)
    return out, cycles


def types_json(cycles, rules: list[dict], types: list[dict]) -> list[dict]:
    """Per cycle type: kind and its cycles as [start, end) indices into the flat phase list."""
    return [{"name": t["name"], "kind": type_kind(t, rules), **type_instances(cycles, rules, t)} for t in types]


class RulesTestIn(BaseModel):
    rules: list[RuleIn]
    wake_pattern: str | None = None


@app.post("/api/sessions/{sid}/phases/test")
async def test_rules(sid: int, body: RulesTestIn):
    """How many cycles each rule matches in this session (no charge calculation; for the rule editor)."""
    s = await get_session(sid)
    rules = check_rules(body.rules)
    return match_counts(await session_lines(s), rules, s["duration_s"], compile_wake(check_wake(body.wake_pattern)))


async def session_views(sid: int) -> tuple[dict, list, list[dict]]:
    res, cycles = await phases_and_cycles(sid)
    if res["profile"] is None:
        raise HTTPException(404, "this session has no phase rules")
    return res, cycles, type_views(cycles, res["profile"]["rules"], res["profile"]["types"])


def pick_view(views: list[dict], name: str | None) -> dict:
    if not name or name == "All wakes":
        return views[0]
    for v in views[1:]:
        if v["name"] == name:
            return v
    raise HTTPException(404, f"no cycle type '{name}' (types: {', '.join(v['name'] for v in views[1:]) or 'none'})")


@app.get("/api/sessions/{sid}/cycles")
async def session_cycles(sid: int, type: str | None = None, index: int | None = None):
    """Per cycle type (all wakes first): counts, typical duration and charge, per-phase table, unusual cycles.
    With `index`: that one cycle of the type (or of all wakes), with its phases and log lines."""
    res, cycles, views = await session_views(sid)
    rules = res["profile"]["rules"]
    if index is None:
        vs = [pick_view(views, type)] if type else views
        return {"session_id": sid, "profile": res["profile"]["name"], "types": [type_summary(v, rules) for v in vs]}
    v = pick_view(views, type)
    if not 0 <= index < len(v["insts"]):
        raise HTTPException(404, f"{v['name'] or 'All wakes'} has {len(v['insts'])} cycles (index from 0)")
    x = v["insts"][index]
    s = await get_session(sid)
    lines = await q_all("""SELECT extract(epoch FROM ts - %(t0)s)::float8 AS t, text FROM uart_lines
                           WHERE session_id = %(sid)s AND ts >= %(a)s AND ts < %(b)s ORDER BY ts LIMIT 300""",
                        {"t0": s["started_at"], "sid": sid, "a": s["started_at"] + timedelta(seconds=x.t0),
                         "b": s["started_at"] + timedelta(seconds=x.t1)})
    return {
        "session_id": sid, "type": v["name"] or "All wakes", "index": index, "start_s": x.t0, "end_s": x.t1,
        "duration_s": x.dur, "charge_c": x.charge, "mean_a": x.charge / x.dur if x.dur > 0 else None,
        "complete": x.ok, "wakes": [x.phases[0][1], x.phases[-1][1]], "woken_by": x.wake,
        "phases": [{"phase": rules[p.rule]["name"], "start_s": p.t, "duration_s": p.end_t - p.t, "charge_c": p.charge_c,
                    "mean_a": p.mean_a, "peak_a": p.peak_a, "coverage": p.coverage} for p, _ in x.phases],
        "log": lines, "log_capped": len(lines) >= 300,
    }


@app.get("/api/sessions/{sid}/battery")
async def session_battery(sid: int, capacity_mah: float = Query(1000, gt=0), sleep_s: float | None = Query(None, gt=0),
                          without: list[str] = Query([]), type: str | None = None, every: float | None = Query(None, ge=1)):
    """Battery life from the measured mix of wakes, and a what-if: another sleep time, phases gone, a whole-wake type
    every N wakes. Ignores self-discharge and the cut-off voltage."""
    res, _cycles, views = await session_views(sid)
    rules = res["profile"]["rules"]
    names = [r["name"] for r in rules]
    unknown = [w for w in without if w not in names]
    if unknown:
        raise HTTPException(422, f"no phase called '{unknown[0]}'")
    parts = mix_parts(views)
    if type and not any(v["name"] == type for v in parts):
        wake_types = [v["name"] for v in views[1:] if v["kind"] == "wake" and not v["error"]]
        raise HTTPException(422, f"'{type}' every N wakes needs whole-wake types that split the wakes one-to-one. "
                                 + (f"These whole-wake types overlap or miss wakes: {', '.join(wake_types)}." if wake_types
                                    else "This profile has no whole-wake types (types that begin at the first phase without an end)."))
    sleep = sleep_rule(views[0], len(rules))
    rows = phase_rows([x for x in views[0]["insts"] if x.ok], len(rules))
    sleep_a = rows[sleep]["mean_a"] if sleep >= 0 else 0.0

    def life(a: float | None) -> dict | None:
        return None if not a else {"mean_a": a, "hours": capacity_mah / (a * 1000), "days": capacity_mah / (a * 1000) / 24}

    now = mix_current(parts, len(rules), sleep, sleep_a or 0.0)
    skip = tuple(names.index(w) for w in without)
    changed = sleep_s is not None or skip or (type and every)
    usable = [[x for x in v["insts"] if x.ok] for v in parts]
    n_all = sum(len(xs) for xs in usable)
    return {
        "session_id": sid, "capacity_mah": capacity_mah,
        "measured": life(now),
        "what_if": life(mix_current(parts, len(rules), sleep, sleep_a or 0.0, sleep_s, skip, type, every)) if changed else None,
        "mix": [{"type": v["name"] or "All wakes", "wakes": len(xs), "one_in": n_all / len(xs) if xs else None,
                 "charge_c": sum(x.charge for x in xs) / len(xs) if xs else None} for v, xs in zip(parts, usable)],
        "sleep": {"phase": names[sleep] if sleep >= 0 else None, "rule": sleep, "mean_a": sleep_a,
                  "duration_s": rows[sleep]["duration_s"] if sleep >= 0 else None},
        "note": "From the real mix of complete wakes. Ignores self-discharge and the cut-off voltage.",
    }


class TypesTestIn(BaseModel):
    types: list[TypeIn]


@app.post("/api/sessions/{sid}/types/test")
async def test_types(sid: int, body: TypesTestIn):
    """Cycle types (not saved yet) on this session's phases, from its saved phase rules (for the type editor)."""
    res, cycles = await phases_and_cycles(sid)
    if res["profile"] is None:
        return []
    rules = res["profile"]["rules"]
    out = []
    for t in body.types:
        d = {**t.model_dump(), "name": t.name.strip()}
        out.append({"name": d["name"], "kind": type_kind(d, rules), **type_instances(cycles, rules, d)})
    return out


# devices ----------------------------------------------------------------------------------------------------
class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    notes: str = ""
    voltage_mv: int = Field(3700, ge=800, le=5000)
    max_mv: int = Field(4200, ge=800, le=5000)
    wake_threshold_ua: float = Field(5000, gt=0)
    uart_baud: int = Field(115200, ge=300, le=12_000_000)  # its debug UART's speed; set when it goes on the bench
    profile_id: int | None = None


DEVICE_COLS = "id, name, notes, voltage_mv, max_mv, wake_threshold_ua, uart_baud, profile_id, updated_at"


def check_device(body: DeviceIn) -> None:
    if body.voltage_mv > body.max_mv:
        raise HTTPException(422, "the default voltage is above the maximum")


async def active_device() -> dict | None:
    r = await q_one("SELECT value FROM settings WHERE key = 'device'")
    did = (r or {}).get("value", {}) or {}
    if not isinstance(did, dict) or did.get("id") is None:
        return None
    return await q_one(f"SELECT {DEVICE_COLS} FROM devices WHERE id = %s", (did["id"],))


async def push_device(dev: dict | None) -> dict:
    """Tell capture which device is on the bench, so it enforces its voltage limit and runs the UART at its speed."""
    body = {"id": dev["id"], "name": dev["name"], "max_mv": dev["max_mv"], "uart_baud": dev["uart_baud"]} if dev else {}
    try:
        r = await capture.put("/ppk2/device", json=body)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"capture service unreachable: {e}") from e
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()


async def recording() -> bool:
    st = await capture_get("/status")
    return bool(((st or {}).get("session") or {}).get("active"))


@app.get("/api/devices")
async def list_devices():
    return await q_all(f"""SELECT {', '.join('d.' + c.strip() for c in DEVICE_COLS.split(','))}, p.name AS profile_name,
                                    (SELECT count(*) FROM sessions s WHERE s.device_id = d.id) AS sessions
                             FROM devices d LEFT JOIN profiles p ON p.id = d.profile_id ORDER BY d.id""")


@app.post("/api/devices")
async def create_device(body: DeviceIn):
    check_device(body)
    try:
        return await q_one(f"INSERT INTO devices (name, notes, voltage_mv, max_mv, wake_threshold_ua, uart_baud, profile_id) "
                           f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {DEVICE_COLS}",
                           (body.name, body.notes, body.voltage_mv, body.max_mv, body.wake_threshold_ua, body.uart_baud,
                            body.profile_id))
    except psycopg.errors.UniqueViolation as e:
        raise HTTPException(409, "a device with that name exists") from e


@app.put("/api/devices/{did:int}")
async def update_device(did: int, body: DeviceIn):
    check_device(body)
    try:
        r = await q_one(f"UPDATE devices SET name = %s, notes = %s, voltage_mv = %s, max_mv = %s, wake_threshold_ua = %s, "
                        f"uart_baud = %s, profile_id = %s, updated_at = now() WHERE id = %s RETURNING {DEVICE_COLS}",
                        (body.name, body.notes, body.voltage_mv, body.max_mv, body.wake_threshold_ua, body.uart_baud,
                         body.profile_id, did))
    except psycopg.errors.UniqueViolation as e:
        raise HTTPException(409, "a device with that name exists") from e
    if r is None:
        raise HTTPException(404, "device not found")
    active = await active_device()
    if active and active["id"] == did:
        await push_device(r)  # a new limit and UART speed apply right away
    return r


@app.delete("/api/devices/{did:int}")
async def delete_device(did: int):
    active = await active_device()
    if active and active["id"] == did:
        raise HTTPException(409, "this device is on the bench: pick another one first")
    await q_all("DELETE FROM devices WHERE id = %s RETURNING id", (did,))
    return {"deleted": did}


@app.get("/api/devices/active")
async def get_active_device():
    return await active_device()


class ActiveIn(BaseModel):
    id: int | None


@app.put("/api/devices/active")
async def set_active_device(body: ActiveIn):
    if await recording():
        raise HTTPException(409, "a session is recording: stop it before changing the device")
    dev = None
    if body.id is not None:
        dev = await q_one(f"SELECT {DEVICE_COLS} FROM devices WHERE id = %s", (body.id,))
        if dev is None:
            raise HTTPException(404, "device not found")
    await push_device(dev)  # first capture: if it refuses, nothing changes
    await q_all("INSERT INTO settings (key, value) VALUES ('device', %s) ON CONFLICT (key) "
                "DO UPDATE SET value = EXCLUDED.value, updated_at = now() RETURNING key",
                (Jsonb({"id": body.id}),))
    return dev


# settings ---------------------------------------------------------------------------------------------------
class SettingIn(BaseModel):
    value: dict | list | str | int | float | bool | None


@app.get("/api/settings/{key}")
async def get_setting(key: str):
    r = await q_one("SELECT value FROM settings WHERE key = %s", (key,))
    return None if r is None else r["value"]


# Settings with their own endpoints (validated, pushed to capture / the MCP server).
OWN_ENDPOINT = {"device": "/api/devices/active", "capture": "/api/measurement", "mcp": "/api/mcp", "flash": "/api/flash/token"}


@app.put("/api/settings/{key}")
async def put_setting(key: str, body: SettingIn):
    if key in OWN_ENDPOINT:
        raise HTTPException(409, f"use {OWN_ENDPOINT[key]} for '{key}'")
    return await save_setting(key, body.value)


async def save_setting(key: str, value) -> dict:
    r = await q_one("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) "
                    "DO UPDATE SET value = EXCLUDED.value, updated_at = now() RETURNING value",
                    (key, Jsonb(value)))
    return r["value"]


async def get_setting_value(key: str):
    r = await q_one("SELECT value FROM settings WHERE key = %s", (key,))
    return None if r is None else r["value"]


# settings page: measurement, storage, agents ----------------------------------------------------------------
class MeasurementIn(BaseModel):
    idle_power_off_s: int | None = Field(None, ge=0, le=86400)
    spike_filter: bool | None = None
    auto_stream: bool | None = None  # start measuring when the PPK2 connects
    min_free_gb: float | None = Field(None, ge=1, le=10000)  # stop writing raw data below this much free disk
    raw_mantissa_bits: int | None = Field(None, ge=8, le=23)  # raw precision of new recordings; 23 = lossless
    flash_when_asleep: str | None = Field(None, pattern="^(wait|always|cycle)$")
    flash_wake_wait_s: float | None = Field(None, ge=5, le=3600)


@app.get("/api/measurement")
async def get_measurement():
    """Capture settings (changeable here, the voltage limit only through PUT /api/measurement/max-voltage), and the
    ones fixed in .env."""
    s = await capture_get("/settings")
    if s is None:
        raise HTTPException(502, "capture service unreachable")
    return s


@app.put("/api/measurement")
async def put_measurement(body: MeasurementIn):
    """Apply in capture now, and keep them (capture loads them again when it starts)."""
    return await apply_capture_settings(body.model_dump(exclude_none=True))


class MaxVoltageIn(BaseModel):
    max_voltage_mv: int = Field(ge=800, le=5000)  # the PPK2 goes to 5 V


@app.put("/api/measurement/max-voltage")
async def put_max_voltage(body: MaxVoltageIn, request: Request):
    """The tracker's voltage limit: nothing goes above it, whatever a device says. Set in the web UI (which asks
    for a confirmation first), never by an agent: the MCP server's requests are refused here, also if its own
    allow list ever let this through. A board powered above a lower limit is switched off."""
    if request.headers.get("x-pst-client") == "mcp":
        raise HTTPException(403, "the voltage limit is changed in the web UI only, not by agents")
    return await apply_capture_settings({"max_voltage_mv": body.max_voltage_mv})


async def apply_capture_settings(change: dict) -> dict:
    try:
        r = await capture.put("/settings", json=change)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"capture service unreachable: {e}") from e
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    await save_setting("capture", {**(await get_setting_value("capture") or {}), **change})
    return r.json()


@app.get("/api/storage")
async def storage():
    """Disk space, database size and raw data per session (largest first)."""
    du = shutil.disk_usage(DATA_DIR)
    db = await q_one("SELECT pg_database_size(current_database()) AS bytes")
    rows = await q_all("""
        SELECT s.id, s.name, s.kind, s.status, s.started_at, s.n_samples * s.sample_period_s AS duration_s,
               coalesce(sum(r.bytes), 0)::bigint AS raw_bytes, count(r.session_id) AS raw_files,
               coalesce(sum(r.n_samples), 0)::bigint * s.sample_period_s AS raw_seconds,
               array_remove(array_agg(DISTINCT r.kind), NULL) AS raw_kinds
        FROM sessions s LEFT JOIN raw_segments r ON r.session_id = s.id
        GROUP BY s.id ORDER BY raw_bytes DESC, s.id DESC""")
    cap = await capture_get("/settings")
    return {
        "disk": {"total_bytes": du.total, "free_bytes": du.free, "used_bytes": du.used},
        "database_bytes": db["bytes"] if db else None,
        "raw_bytes": sum(r["raw_bytes"] for r in rows),
        "min_free_gb": (cap or {}).get("min_free_gb"),
        "sessions": rows,
    }


async def mcp_settings() -> dict:
    """{lan, hardware, tokens: [{id, name, token, created}]}. An older single "token" becomes the token "default"."""
    m = {"lan": False, "hardware": False, **(await get_setting_value("mcp") or {})}
    tokens = m.get("tokens")
    if tokens is None:
        tokens = [{"id": 1, "name": "default", "token": m["token"], "created": None}] if m.get("token") else []
    m.pop("token", None)
    return {**m, "tokens": tokens}


async def save_mcp(m: dict) -> None:
    await save_setting("mcp", {"lan": m["lan"], "hardware": m["hardware"], "tokens": m["tokens"]})


def new_token(tokens: list[dict], name: str) -> dict:
    t = {"id": max((x["id"] for x in tokens), default=0) + 1, "name": name, "token": secrets.token_urlsafe(32),
         "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    tokens.append(t)
    return t


async def seed_mcp() -> None:
    """Once: MCP access over the LAN from MCP_TOKEN in .env (later changed in Settings → Agents)."""
    if await get_setting_value("mcp") is None:
        token = os.environ.get("MCP_TOKEN") or None
        await save_setting("mcp", {"lan": bool(token), "hardware": False,
                                   "tokens": [{"id": 1, "name": "default", "token": token, "created": None}] if token else []})


async def mcp_health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{MCP_URL}/health")
        if r.status_code == 200:
            return {"up": True, **r.json()}
        return {"up": False, "error": f"status {r.status_code}"}
    except httpx.HTTPError as e:
        return {"up": False, "error": str(e) or type(e).__name__}


@app.get("/api/mcp/auth")
async def mcp_auth():
    """For the MCP server's LAN gate: LAN on/off, hardware on/off and the named tokens.
    (Anyone who can open the web UI can see this, like the rest of Settings.)"""
    m = await mcp_settings()
    return {"lan": m["lan"], "hardware": m["hardware"], "tokens": [{"name": t["name"], "token": t["token"]} for t in m["tokens"]]}


@app.get("/api/mcp")
async def mcp_status():
    return {**await mcp_settings(), "server": await mcp_health()}


class McpIn(BaseModel):
    lan: bool | None = None
    hardware: bool | None = None


@app.put("/api/mcp")
async def mcp_set(body: McpIn):
    """LAN access (with tokens) and hardware control by agents; each only when given."""
    m = await mcp_settings()
    if body.lan is not None:
        m["lan"] = body.lan
    if body.hardware is not None:
        m["hardware"] = body.hardware
    if m["lan"] and not m["tokens"]:
        new_token(m["tokens"], "default")
    await save_mcp(m)
    if not m["hardware"]:
        agent_access.revoke()  # switched off: every session's allow ends now
    return await mcp_status()


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


@app.post("/api/mcp/tokens")
async def mcp_add_token(body: TokenIn):
    """A new named token for one machine or agent (e.g. "laptop")."""
    m = await mcp_settings()
    name = body.name.strip()
    if any(t["name"].lower() == name.lower() for t in m["tokens"]):
        raise HTTPException(409, f"there's already a token called '{name}'")
    new_token(m["tokens"], name)
    await save_mcp(m)
    return await mcp_status()


@app.post("/api/mcp/tokens/{tid:int}/renew")
async def mcp_renew_token(tid: int):
    """A new value for one token: the machines using it need the new one; the old one stops working within seconds."""
    m = await mcp_settings()
    t = next((t for t in m["tokens"] if t["id"] == tid), None)
    if t is None:
        raise HTTPException(404, "no such token")
    t["token"] = secrets.token_urlsafe(32)
    t["created"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await save_mcp(m)
    return await mcp_status()


@app.delete("/api/mcp/tokens/{tid:int}")
async def mcp_delete_token(tid: int):
    m = await mcp_settings()
    m["tokens"] = [t for t in m["tokens"] if t["id"] != tid]
    await save_mcp(m)
    return await mcp_status()


# hardware control by agents (MCP) ---------------------------------------------------------------------------
# The MCP server asks here before each hardware call; a person answers in the web UI. See agents.py.
agent_access = AgentAccess()


class AgentAskIn(BaseModel):
    session: str = Field(min_length=8, max_length=128)
    client: str = Field("", max_length=200)
    host: str = Field("", max_length=200)
    via: str = Field("local", pattern="^(local|lan)$")
    tool: str = Field(min_length=1, max_length=60)
    args: dict = {}


class AnswerIn(BaseModel):
    allow: bool


class RevokeIn(BaseModel):
    session: str | None = None


class AgentActionIn(BaseModel):
    session: str = Field(max_length=128)
    client: str = Field("", max_length=200)
    host: str = Field("", max_length=200)
    tool: str = Field(max_length=60)
    args: dict = {}
    result: str = Field(pattern="^(ok|error|denied)$")
    detail: str | None = Field(None, max_length=2000)


@app.get("/api/agents")
async def agents_state():
    """For the web UI: is hardware control on, requests waiting for an answer, sessions that are allowed."""
    m = await mcp_settings()
    return {"hardware": m["hardware"], "lan": m["lan"], **agent_access.state()}


@app.post("/api/agents/requests")
async def agent_ask(body: AgentAskIn):
    """From the MCP server, before a hardware call: allowed, denied, or pending (a person decides in the web UI)."""
    if not (await mcp_settings())["hardware"]:
        raise HTTPException(403, "hardware control by agents is off (Settings → Agents)")
    return agent_access.ask(body.session, body.client, body.host, body.via, body.tool, body.args)


@app.get("/api/agents/requests/{rid:int}")
async def agent_request(rid: int):
    r = agent_access.request(rid)
    if r is None:
        raise HTTPException(404, "no such request")
    return r


@app.post("/api/agents/requests/{rid:int}/answer")
async def agent_answer(rid: int, body: AnswerIn):
    """From the web UI: allow this agent session to control the hardware, or deny it."""
    if body.allow and not (await mcp_settings())["hardware"]:
        raise HTTPException(409, "hardware control by agents is off")
    try:
        return agent_access.answer(rid, body.allow)
    except KeyError:
        raise HTTPException(404, "no such request") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/agents/revoke")
async def agent_revoke(body: RevokeIn):
    """End one session's allow (session) or all of them (no session)."""
    return {"revoked": agent_access.revoke(body.session)}


@app.post("/api/agents/actions")
async def agent_action(body: AgentActionIn):
    """From the MCP server: what an agent did (or tried), for the log in Settings → Agents."""
    await q_all("INSERT INTO agent_actions (session, client, host, tool, args, result, detail) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id", (body.session, body.client, body.host, body.tool, Jsonb(body.args), body.result, body.detail))
    return {"ok": True}


@app.get("/api/agents/actions")
async def agent_actions(limit: int = Query(50, ge=1, le=500)):
    return await q_all("SELECT id, at, session, client, host, tool, args, result, detail FROM agent_actions ORDER BY at DESC LIMIT %s", (limit,))


# capture pass-through (device control, session start/stop) ------------------------------------------------
@app.post("/api/sessions")
async def start_session(request: Request):
    """Start via the capture service, then link the phase-rule profile (given, or the last one used)."""
    body = await request.json()
    profile_id = body.pop("profile_id", None)
    dev = await active_device()
    try:
        r = await capture.post("/sessions", json=body)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"capture service unreachable: {e}") from e
    if r.status_code == 200:
        sid = ((r.json() or {}).get("session") or {}).get("id")
        if sid is not None:
            # Phase rules: given, else the device's, else the last ones used.
            await q_all("UPDATE sessions SET device_id = %s, profile_id = coalesce(%s, %s, (SELECT profile_id FROM sessions "
                        "WHERE profile_id IS NOT NULL ORDER BY id DESC LIMIT 1)) WHERE id = %s RETURNING id",
                        (dev["id"] if dev else None, profile_id, dev["profile_id"] if dev else None, sid))
    return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))


@app.post("/api/sessions/stop")
async def stop_session(request: Request):
    return await forward(request, "/sessions/stop")


@app.api_route("/api/device/{path:path}", methods=["GET", "POST", "PUT"])
async def device(path: str, request: Request):
    """Everything the capture service offers: status, ppk2/*, uart/*, usb/*, buffer/*, sessions/current.
    Flashing only through /api/flash, which checks the token."""
    if path.startswith("flash") and request.method != "GET":
        raise HTTPException(403, "flash through /api/flash")
    return await forward(request, "/" + path)


# firmware flashing (uploads from PlatformIO) --------------------------------------------------------------
async def flash_settings() -> dict:
    return await get_setting_value("flash") or {"token": None}


async def seed_flash() -> None:
    """Once: a token for uploads, FLASH_TOKEN from .env or a new one (shown and renewed in Settings later)."""
    if await get_setting_value("flash") is None:
        await save_setting("flash", {"token": os.environ.get("FLASH_TOKEN") or secrets.token_urlsafe(32)})


async def check_flash_token(request: Request) -> None:
    token = (await flash_settings()).get("token")
    given = request.headers.get("authorization", "")
    if not token or not secrets.compare_digest(given.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "flash token missing or wrong (Authorization: Bearer <token>)")


@app.get("/api/flash/settings")
async def get_flash_settings():
    """The upload token (anyone who can open the web UI can see it, like the MCP token) and the OTA port."""
    return {**await flash_settings(), "ota_port": OTA_PUBLIC_PORT if OTA_PORT else None}


@app.post("/api/flash/token")
async def new_flash_token():
    """A new upload token; the old one stops working at once."""
    await save_setting("flash", {**await flash_settings(), "token": secrets.token_urlsafe(32)})
    return await flash_settings()


async def ota_token() -> str | None:
    return (await flash_settings()).get("token")


async def ota_submit(image: bytes, ip: str) -> int | None:
    """An Arduino OTA upload: its app image as a flash job like any upload (recording question in the web UI)."""
    try:
        r = await capture.post("/flash", json=ota.app_only_job(image, ip), timeout=60)
    except httpx.HTTPError as e:
        log.warning("OTA upload from %s: capture unreachable: %s", ip, e)
        return None
    if r.status_code != 200:
        log.warning("OTA upload from %s refused: %s", ip, r.text[:200])
        return None
    return r.json()["id"]


async def ota_result(job_id: int) -> bool:
    """Wait for the OTA upload's flash job: True once flashed (or queued until the recording stops)."""
    try:
        while True:
            await asyncio.sleep(ota.POLL_S)
            j = (await capture.get(f"/flash/{job_id}", params={"after": 1 << 30})).json()
            if j["status"] == "waiting_recording":
                return True  # queued until the recording stops: the web UI shows the result
            if j["status"] in ("done", "failed", "cancelled"):
                if j["status"] != "done":
                    log.warning("OTA flash job %d %s: %s", job_id, j["status"], j.get("error"))
                return j["status"] == "done"
    except httpx.HTTPError as e:
        log.warning("OTA flash job %d: capture unreachable: %s", job_id, e)
        return False


@app.post("/api/flash")
async def flash(request: Request):
    """Start a flash: {images: [{offset, data_b64}], chip, flash_mode, flash_freq, flash_size, on_recording, meta}.
    Returns the job; follow it with GET /api/flash/{id}?after=<lines seen>."""
    await check_flash_token(request)
    try:
        r = await capture.post("/flash", content=await request.body(), headers={"content-type": "application/json"},
                               timeout=60)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"capture service unreachable: {e}") from e
    return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))


@app.get("/api/flash")
async def flash_latest(request: Request):
    return await forward(request, "/flash")


@app.get("/api/flash/jobs")
async def flash_jobs(request: Request):
    """The last uploads since capture started, newest first."""
    return await forward(request, "/flash/jobs")


@app.get("/api/flash/{job_id:int}")
async def flash_job(job_id: int, request: Request):
    return await forward(request, f"/flash/{job_id}")


@app.post("/api/flash/{job_id:int}/cancel")
async def flash_cancel(job_id: int, request: Request):
    """Cancel an upload that's still waiting (from the UI)."""
    return await forward(request, f"/flash/{job_id}/cancel")


@app.post("/api/flash/{job_id:int}/now")
async def flash_now(job_id: int, request: Request):
    """Waiting for the board to wake: power-cycle it now (from the UI)."""
    return await forward(request, f"/flash/{job_id}/now")


@app.post("/api/flash/{job_id:int}/answer")
async def flash_answer(job_id: int, request: Request):
    """What to do with a running recording: {choice: keep|stop|cancel}. From the UI or the upload script."""
    return await forward(request, f"/flash/{job_id}/answer")


async def forward(request: Request, path: str) -> Response:
    try:
        r = await capture.request(request.method, path, params=request.query_params,
                                  content=await request.body(), headers={"content-type": "application/json"})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"capture service unreachable: {e}") from e
    return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))

