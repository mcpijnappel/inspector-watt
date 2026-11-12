"""MCP tools for Inspector Watt: analyse recorded sessions, look at the live measurement, and edit
phase rules / cycle types. Hardware tools (power, voltage, recording, USB console) exist only while the user has
switched them on, and each call needs the user's OK (see hardware.py)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

import asyncio

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from .client import Api, ApiError
from .hardware import HW_ANNOTATIONS, HW_META, Hardware
from .modules import Modules

INSTRUCTIONS = """\
Inspector Watt: a Nordic PPK2 measures the current of a board (the device under test, e.g. a XIAO ESP32-C6) at
100 kS/s while its serial (UART) log is recorded alongside. Use these tools to analyse recordings and the live
measurement.

Hardware (board power, voltage, measuring, recording, the board's USB console) only when the user has switched on
"Hardware control" in the tracker (Settings → Agents); otherwise those tools don't exist and you ask the user.
The first hardware call of a session waits until the user allows this session in the tracker's web UI (tell
them to look there), and Claude Code asks them before every call. The board's USB console: while it's open the
board can't deep-sleep, so close it when done. Firmware is flashed with PlatformIO, never through MCP.

Units: current in A (1e-3 = 1 mA, 1e-6 = 1 µA), charge in C (3.6 C = 1 mAh), time in seconds. Times in a
session are seconds since the session started (start_s/end_s); wall-clock times are ISO 8601.

Concepts:
- session: one recording ("detail" keeps every sample; "soak" keeps 10 ms summaries plus raw data around wakes).
- phase rules (per profile): a phase starts at the first log line matching its rule; the first rule starts a
  new cycle (usually a wake). Each phase runs until the next phase starts. The "Default" profile finds plain
  lines firmware can print at the start of each phase, in this order: Boot (ESP32's "rst:0x..", or "Booting"),
  "Measuring", "Connecting to ...", "Uploading ..." / "Sending ..." / "Publishing ...", "Going to deep sleep
  for N s"; plus "Wake source: <reason>" once per wake. When the user asks you to make their firmware work
  with the tracker, add those lines (flush the UART before sleeping) rather than writing new rules; the guide
  is docs/firmware-logging.md in the Inspector Watt repository.
- cycle types (per profile): kinds of cycles. A type begins at a phase and runs until the next begin, or until
  its end phase ("stretch", e.g. Connect + upload). "must" / "exclude" keep only cycles with / without some phases;
  "woken_by" keeps only cycles with a wake reason (Timer, an interrupt, ...).
  "All wakes" is every cycle. A cycle is "complete" (used in averages) when it ended and has no data gaps.
- wake reason: what woke the board, from the firmware's log line (profile's wake_pattern).
- unexpected resets: every reset in the serial log except deep-sleep wakes and power-ons the tracker caused
  (brownout, watchdog, crash, software reset e.g. after an update, power lost); get_session groups them.
- battery life uses the real mix of complete wakes; what-ifs change sleep time, drop phases or change how
  often a whole-wake type happens.

Sensor library (with the logic analyzer module): chip tables shared by every board say how each chip talks, one
entry per bus (I2C, SPI) with its own registers or commands and formulas that turn bytes into readings, so the Bus
panel names what it decodes. To add
a chip from its datasheet: chips_list / chip_get, then chip_save (the user checks it later); put it on the board with
set_board_chips; bus_decode shows what the board really sends, to check the table against. During a recording the
analyzer decodes the bus around each wake by itself: session_bus reads a session's messages, named the same way.

Start with list_sessions, then get_session / cycle_summary / search_log. For "what is the device doing now",
use live_status and live_window. To add phases: search_log for the lines, test_phase_rules, then save_profile.
"""

HARDWARE_TOOLS = {"dut_power", "power_cycle", "set_voltage", "measuring", "start_recording", "stop_recording",
                  "save_live_range", "usb_console", "usb_key", "usb_send"}


# The sensor library and a board's chips: only with a module that decodes buses (the logic analyzer).
LIBRARY_TOOLS = {"chips_list", "chip_get", "chip_save", "board_chips", "set_board_chips", "bus_decode", "session_bus"}


class TrackerMCP(FastMCP):
    async def list_tools(self):
        """The hardware tools only while Settings → Agents → Hardware control is on; the sensor library's tools only
        while a bus-decoding module is installed."""
        tools = await super().list_tools()
        hidden = (set() if await hw.enabled() else HARDWARE_TOOLS) | (set() if await modules.bus_decoding() else LIBRARY_TOOLS)
        return [t for t in tools if t.name not in hidden]

    async def call_tool(self, name, arguments):
        if name in LIBRARY_TOOLS and not await modules.bus_decoding():
            raise ToolError(f"{name} needs the logic analyzer module, and it isn't installed")
        return await super().call_tool(name, arguments)


mcp = TrackerMCP(
    "inspector-watt",
    instructions=INSTRUCTIONS,
    # Stateful: each client connection gets an Mcp-Session-Id, which hardware control is allowed for.
    stateless_http=False,
    json_response=True,
    # Host check against DNS rebinding (the local port needs no token): local names, and nginx's proxy name.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                 allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "mcp", "mcp:*"]),
)
api = Api()
hw = Hardware(api)
modules = Modules(api)

SessionId = Annotated[int, Field(description="Session id (from list_sessions)")]
StartS = Annotated[float | None, Field(description="Start, seconds since the session started (default: the start)")]
EndS = Annotated[float | None, Field(description="End, seconds since the session started (default: the end)")]


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds") if ts else None


# sessions --------------------------------------------------------------------------------------------------------
@mcp.tool()
async def list_sessions(
    search: Annotated[str | None, Field(description="Only sessions whose name or notes contain this")] = None,
    device: Annotated[str | None, Field(description="Only sessions of this device (name contains)")] = None,
    kind: Annotated[Literal["detail", "soak"] | None, Field(description="Only this kind")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> dict:
    """Recorded sessions, newest first: name, kind, start, duration, average current, charge, device, wakes."""
    rows = await api.get("/api/sessions")
    out = []
    for s in rows:
        text = f"{s.get('name') or ''} {s.get('notes') or ''}".lower()
        if search and search.lower() not in text:
            continue
        if device and device.lower() not in (s.get("device_name") or "").lower():
            continue
        if kind and s.get("kind") != kind:
            continue
        out.append({
            "id": s["id"], "name": s["name"], "kind": s["kind"], "status": s["status"], "started_at": s["started_at"],
            "duration_s": s.get("duration_s"), "avg_current_a": s.get("avg_current_a"), "charge_c": s.get("charge_c"),
            "device": s.get("device_name"), "profile_id": s.get("profile_id"), "wakes": s.get("wakes"),
            **({"notes": s["notes"][:300]} if s.get("notes") else {}),
        })
    return {"sessions": out[:limit], "total": len(out)}


@mcp.tool()
async def get_session(session_id: SessionId) -> dict:
    """One session: settings (voltage, kind, battery emulation, ...), device, notes, event counts, stored data,
    unexpected board resets (grouped by kind and the log line before them), and per cycle type how many cycles it
    has and what woke them."""
    s = await api.get(f"/api/sessions/{session_id}")
    out = {k: s.get(k) for k in ("id", "name", "kind", "status", "started_at", "stopped_at", "duration_s", "avg_current_a",
                                 "charge_c", "notes", "device_name", "profile_id", "config", "events", "uart_lines", "raw",
                                 "resets")}
    try:
        cyc = await api.get(f"/api/sessions/{session_id}/cycles")
        out["profile"] = cyc["profile"]
        out["cycle_types"] = [{"type": t["type"], "kind": t["kind"], "cycles": t["cycles"], "usable": t["usable"],
                               "woken_by": t.get("woken_by")} for t in cyc["types"]]
    except ApiError as e:
        out["cycle_types"] = f"none ({e})"
    return out


@mcp.tool()
async def range_stats(session_id: SessionId, start_s: StartS = None, end_s: EndS = None) -> dict:
    """Average, charge, peak and minimum current over a time range of a session (exact from raw samples when
    they're stored there, else from summaries), plus the wakes in it."""
    return await api.get(f"/api/sessions/{session_id}/stats", start_s=start_s, end_s=end_s)


@mcp.tool()
async def current_over_time(
    session_id: SessionId, start_s: StartS = None, end_s: EndS = None,
    points: Annotated[int, Field(ge=10, le=300, description="Rows to return (time bins)")] = 60,
) -> dict:
    """The current over a time range in bins: rows of [t_s, mean_a, min_a, max_a]. Zoom in by narrowing the
    range; short ranges come from raw samples."""
    t = await api.get(f"/api/sessions/{session_id}/trace", start_s=start_s, end_s=end_s, points=points)
    xs, mean, mn, mx = t.get("x", []), t.get("mean", []), t.get("min", []), t.get("max", [])
    rows = [[round(x, 6), mean[i], mn[i] if i < len(mn) else None, mx[i] if i < len(mx) else None] for i, x in enumerate(xs)]
    step = max(1, len(rows) // points)
    return {"start_s": t.get("start_s"), "end_s": t.get("end_s"), "source": t.get("source"),
            "columns": ["t_s", "mean_a", "min_a", "max_a"], "rows": rows[::step][:points]}


@mcp.tool()
async def search_log(
    session_id: SessionId,
    contains: Annotated[str | None, Field(description="Case-insensitive text to look for")] = None,
    start_s: StartS = None, end_s: EndS = None,
    limit: Annotated[int, Field(ge=1, le=2000)] = 100,
) -> dict:
    """Serial (UART) log lines of a session, in order, with their session time (t_s) and wall-clock time."""
    lines = await api.get(f"/api/sessions/{session_id}/uart", q=contains, start_s=start_s, end_s=end_s, limit=limit + 1)
    return {"lines": [{"t_s": round(l["t"], 4), "wall": l["wall"], "text": l["text"]} for l in lines[:limit]],
            "more": len(lines) > limit}


@mcp.tool()
async def list_events(
    session_id: SessionId,
    kind: Annotated[str | None, Field(description="e.g. wake, gap, reset, flash, power_on, power_off, dut_restore, battery, battery_end")] = None,
    start_s: StartS = None, end_s: EndS = None,
    limit: Annotated[int, Field(ge=1, le=2000)] = 100,
) -> dict:
    """Events recorded during a session: wakes (current above the wake threshold), data gaps, unexpected board
    resets (from the serial log: brownout, watchdog, crash, software reset, power lost; with the log line before
    it), firmware flashes by the tracker (data: project, version, git, host, result; resets during them are
    expected), battery-emulation voltage steps, board power changes, ..."""
    ev = await api.get(f"/api/sessions/{session_id}/events", kind=kind, start_s=start_s, end_s=end_s, limit=limit + 1)
    return {"events": ev[:limit], "more": len(ev) > limit}


# cycles and battery ----------------------------------------------------------------------------------------------
@mcp.tool()
async def cycle_summary(
    session_id: SessionId,
    cycle_type: Annotated[str | None, Field(description="One cycle type by name (default: all wakes and every type)")] = None,
) -> dict:
    """Cycles of a session per cycle type: count, typical / p10-p90 duration and charge, average current, what woke
    them (woken_by: reason -> count), a per-phase table (in how many cycles, duration, charge per cycle, share) and
    unusual cycles (index, start)."""
    return await api.get(f"/api/sessions/{session_id}/cycles", type=cycle_type)


@mcp.tool()
async def get_cycle(
    session_id: SessionId,
    index: Annotated[int, Field(ge=0, description="Cycle index within the type, from 0")],
    cycle_type: Annotated[str | None, Field(description="Cycle type (default: all wakes)")] = None,
) -> dict:
    """One cycle: what woke it, its phases (start, duration, charge, mean and peak current) and the log lines inside it."""
    return await api.get(f"/api/sessions/{session_id}/cycles", type=cycle_type, index=index)


@mcp.tool()
async def battery_life(
    session_id: SessionId,
    capacity_mah: Annotated[float, Field(gt=0, description="Battery capacity in mAh")] = 1000,
    sleep_s: Annotated[float | None, Field(gt=0, description="What if: sleep this long per wake")] = None,
    without_phases: Annotated[list[str] | None, Field(description="What if: these phases didn't happen")] = None,
    cycle_type: Annotated[str | None, Field(description="What if: this whole-wake type happens every N wakes")] = None,
    every_n_wakes: Annotated[float | None, Field(ge=1, description="N for cycle_type")] = None,
) -> dict:
    """Battery life from the session's measured mix of wakes, and optionally a what-if."""
    return await api.request("GET", f"/api/sessions/{session_id}/battery", params={
        "capacity_mah": capacity_mah, "sleep_s": sleep_s, "without": without_phases or None,
        "type": cycle_type, "every": every_n_wakes})


# live ------------------------------------------------------------------------------------------------------------
@mcp.tool()
async def live_status() -> dict:
    """Right now: PPK2 (connected, measuring, board power, voltage, limit), the device on the bench, UART, and the
    session being recorded (if any)."""
    st = await api.get("/api/device/status")
    p = st.get("ppk2", {})
    u = st.get("uart", {})
    sess = st.get("session") or {}
    return {
        "time": iso(st.get("time")),
        "ppk2": {"connected": p.get("connected"), "measuring": p.get("streaming"), "dut_power": p.get("dut_power"),
                 "voltage_mv": p.get("voltage_mv"), "mode": p.get("mode"), "limit_mv": p.get("max_voltage_mv"),
                 "error": p.get("error"), "lost_samples": (p.get("stream") or {}).get("lost")},
        "device": p.get("device"),
        "uart": {"connected": u.get("connected"), "baud": u.get("baud"), "lines": u.get("lines"), "error": u.get("error")},
        "recording": sess.get("session") if sess.get("active") else None,
        "idle_power_off": st.get("idle_power_off"),
        "usb": usb_brief(st.get("usb")),
        "last_flash": flash_brief(st.get("flash")),
    }


def usb_brief(u: dict | None) -> dict | None:
    if not u:
        return None
    return {"present": u.get("present"), "console_open": u.get("wanted"), "flashing": u.get("held"),
            "menu": u.get("menu"), "error": u.get("error"),
            "note": "present = the board is awake (the port disappears in deep sleep); while the console is open the board "
                    "can't deep-sleep"}


def flash_brief(f: dict | None) -> dict | None:
    if not f:
        return None
    return {"id": f.get("id"), "status": f.get("status"), "progress": f.get("progress"), "error": f.get("error"),
            **{k: f.get("meta", {}).get(k) for k in ("project", "env", "version", "git", "host")},
            "started": iso(f.get("created")), "finished": iso(f.get("finished")),
            "seconds": round(f["finished"] - f["created"], 1) if f.get("finished") else None,
            "power_cycled": f.get("power_cycled"), "recording": f.get("session_id"), "choice": f.get("choice")}


@mcp.tool()
async def live_window(
    seconds: Annotated[float, Field(gt=0, le=300, description="How far back, from now (the buffer keeps ~5 min)")] = 10,
    points: Annotated[int, Field(ge=10, le=200, description="Rows in the current profile")] = 40,
) -> dict:
    """The last N seconds of the live measurement: average / peak / charge, a coarse current profile
    (rows of [seconds_ago, mean_a, min_a, max_a]) and the serial lines from that time."""
    st = await api.get("/api/device/status")
    buf = st.get("buffer") or {}
    period = buf.get("sample_period_s", 1e-5)
    end = buf.get("end_idx", 0)
    start = max(buf.get("start_idx", 0), end - round(seconds / period))
    if end - start < 10:
        return {"error": "no live data: the PPK2 isn't measuring", "ppk2": st.get("ppk2", {}).get("streaming")}
    stats = await api.get("/api/device/buffer/stats", start=start, end=end)
    w = await api.get("/api/device/buffer/window", start=start, end=end, points=points)
    k = w.get("bin_samples", 1)
    mean = w.get("mean") or w.get("current") or []
    mn = w.get("min") or w.get("current") or []
    mx = w.get("max") or w.get("current") or []
    rows = [[round((end - (w["start"] + i * k)) * period, 4), mean[i], mn[i], mx[i]] for i in range(len(mean))]
    lines = []
    n = (st.get("uart") or {}).get("lines") or 0
    if n:
        end_wall = buf.get("end_wall") or st.get("time")
        got = await api.get("/api/device/uart/lines", after_seq=max(-1, n - 1 - 500), limit=500)
        lines = [{"seconds_ago": round(end_wall - l["wall"], 3), "text": l["text"]} for l in got
                 if end_wall - l["wall"] <= (end - start) * period]
    return {"seconds": (end - start) * period, "dut_power": st.get("ppk2", {}).get("dut_power"),
            "mean_a": stats.get("mean_a"), "max_a": stats.get("max_a"), "min_a": stats.get("min_a"),
            "charge_c": stats.get("charge_c"), "lost_samples": stats.get("lost"),
            "columns": ["seconds_ago", "mean_a", "min_a", "max_a"], "rows": rows, "log": lines}


# phase rules and cycle types -----------------------------------------------------------------------------------
class Rule(BaseModel):
    name: str = Field(description="Phase name")
    pattern: str = Field(description="Text (or regex) of the log line that starts the phase")
    match: Literal["contains", "regex"] = "contains"
    color: str = Field("#8b949e", description="Colour in the web UI, #rrggbb")
    snap: bool = Field(False, description="Move the start onto the nearest current step (±20 ms)")


class TypeEnd(BaseModel):
    phase: str
    include: bool = Field(False, description="True: run through the end of this phase; false: stop when it starts")


class CycleType(BaseModel):
    name: str
    begin: str = Field(description="Phase that starts a cycle of this type")
    end: TypeEnd | None = Field(None, description="None: run until the next begin")
    must: list[str] = Field([], description="Only cycles that contain these phases")
    exclude: list[str] = Field([], description="Not cycles that contain any of these phases")
    woken_by: list[str] = Field([], description="Only cycles that begin in a wake with one of these wake reasons "
                                                "(e.g. Timer, GPIO; see woken_by in cycle_summary)")


@mcp.tool()
async def list_profiles() -> dict:
    """Phase-rule profiles (one per firmware): their phase rules (in order; the first starts a cycle), cycle
    types, and which sessions use them."""
    profiles = await api.get("/api/profiles")
    sessions = await api.get("/api/sessions")
    for p in profiles:
        p["sessions"] = [s["id"] for s in sessions if s.get("profile_id") == p["id"]]
    return {"profiles": profiles}


@mcp.tool()
async def test_phase_rules(session_id: SessionId, rules: list[Rule]) -> dict:
    """Try phase rules on a session without saving: how many cycles there are and in how many each phase is found."""
    r = await api.request("POST", f"/api/sessions/{session_id}/phases/test", json={"rules": [x.model_dump() for x in rules]})
    return {"cycles": r["cycles"], "found": [{"phase": x.name, "in_cycles": n} for x, n in zip(rules, r["found"])]}


@mcp.tool()
async def test_cycle_types(session_id: SessionId, types: list[CycleType]) -> dict:
    """Try cycle types on a session (with its saved phase rules) without saving: cycles found per type and why
    others were left out."""
    r = await api.request("POST", f"/api/sessions/{session_id}/types/test", json={"types": [x.model_dump() for x in types]})
    return {"types": [{"type": t["name"], "kind": t["kind"], "cycles": len(t["instances"]), "left_out":
                       {"filtered": t["filtered"], "no_end_phase": t["no_end"], "gaps": t["gaps"]}, "error": t["error"]}
                      for t in r]}


@mcp.tool()
async def save_profile(
    profile_id: Annotated[int | None, Field(description="Profile to change; leave out to create a new one")] = None,
    name: Annotated[str | None, Field(description="Name (required for a new profile)")] = None,
    rules: Annotated[list[Rule] | None, Field(description="All phase rules, in order (replaces them)")] = None,
    types: Annotated[list[CycleType] | None, Field(description="All cycle types (replaces them)")] = None,
    wake_pattern: Annotated[str | None, Field(description="Regex for the log line saying what woke the board; its first "
                                                          "group is the reason. Empty string = the default")] = None,
    link_session_id: Annotated[int | None, Field(description="Also make this session use the profile")] = None,
) -> dict:
    """Create or change a phase-rule profile (rules and/or cycle types). Changes apply to every session that uses
    the profile; test first with test_phase_rules / test_cycle_types."""
    body: dict = {}
    if name:
        body["name"] = name
    if rules is not None:
        body["rules"] = [x.model_dump() for x in rules]
    if types is not None:
        body["types"] = [x.model_dump() for x in types]
    if wake_pattern is not None:
        body["wake_pattern"] = wake_pattern
    if profile_id is None:
        if not name:
            raise ApiError("a new profile needs a name")
        p = await api.request("POST", "/api/profiles", json=body)
    else:
        p = await api.request("PUT", f"/api/profiles/{profile_id}", json=body)
    if link_session_id is not None:
        await api.request("PATCH", f"/api/sessions/{link_session_id}", json={"profile_id": p["id"]})
    return {"profile": p, "linked_session": link_session_id}


# the sensor library and a board's chips -------------------------------------------------------------------------
class ChipValue(BaseModel):
    name: str = Field(description="What it is, e.g. temperature")
    unit: str = Field("", description="e.g. °C, %RH, counts")
    expr: str = Field(description="Formula over the bytes that came back: numbers, + - * /, brackets and u8(i) s8(i) "
                                  "u16(i) s16(i) (big-endian), u16le(i) s16le(i), u24(i), u32(i) u32le(i); i = index of "
                                  "the first byte. E.g. -45 + 175 * u16(0) / 65535")


class ChipRegister(BaseModel):
    addr: int = Field(description="Register address (a number; 0x44 = 68)")
    name: str = Field(description="Name as in the datasheet, e.g. STATUS2")
    rw: Literal["r", "w", "rw"] = "rw"
    about: str = Field("", description="What it is, one line")
    fields: str = Field("", description="Its bits, e.g. bit 6 AVALID · bits 2:0 gain")
    bytes: int | None = Field(None, description="How many bytes a read returns, when not the chip's reg_bytes")
    values: list[ChipValue] = Field([], description="Readings from what a read returns")


class ChipCommand(BaseModel):
    code: int = Field(description="Command code (a number)")
    name: str = Field(description="e.g. measure (high precision)")
    reply: int = Field(0, description="Bytes it answers with (read separately, after the wait)")
    wait_ms: float | None = Field(None, description="Time before the answer is ready")
    about: str = ""
    values: list[ChipValue] = Field([], description="Readings from the answer")


class ChipBank(BaseModel):
    reg: int = Field(description="The register with the bank or page bit")
    mask: int = Field(description="The bit(s) that pick the bank, e.g. 0x10")
    about: str = Field("", description="Which bank holds which registers")


class ChipBus(BaseModel):
    bus: Literal["i2c", "spi"] = Field(description="The bus this entry is for; one entry per bus the chip can use")
    addresses: list[int] = Field([], description="I2C: the 7-bit addresses it can answer at (numbers)")
    read_bit: int | None = Field(None, description="SPI: the bit of the first byte that says read or write (usually 0x80)")
    read_when_set: bool | None = Field(None, description="SPI: True (default) = that bit set means read")
    inc_bit: int | None = Field(None, description="SPI: auto-increment bit, if any (ST: 0x40)")
    mode: int | None = Field(None, description="SPI mode 0-3 (leave out when the chip takes more than one)")
    max_hz: int | None = Field(None, description="SPI: highest clock the datasheet allows, in Hz")
    style: Literal["registers", "commands", "switch", "basic"] = Field(
        "basic", description="registers: write a register address then read / write address + data. commands: a "
                             "command, the answer read later. switch: an I2C switch (bit n opens port n). basic: no table")
    reg_bytes: Literal[1, 2] = Field(1, description="Data bytes per register")
    byte_order: Literal["big", "little"] = "big"
    registers: list[ChipRegister] = Field([], description="This bus's registers (repeat them for each bus, or differ)")
    commands: list[ChipCommand] = []
    ports: int | None = Field(None, description="I2C switch: number of ports")
    bank: ChipBank | None = Field(None, description="Only for chips that page their registers (AS7343, BME680 on SPI)")


class ChipTable(BaseModel):
    name: str = Field(description="Chip type, e.g. SHT4x, VEML7700 (a family name when the variants share a table)")
    vendor: str = ""
    kind: str = Field("", description="What it is, e.g. Ambient light")
    category: Literal["sensor", "switch", "memory", "clock", "display", "converter", "gpio", "other"] | None = Field(
        None, description="For the library's type filter: switch = I2C switch/mux, converter = ADC, current or battery "
                          "monitor, gpio = GPIO expander")
    tags: list[Literal["temperature",  # the same list as api chips.TAGS "humidity", "pressure", "gas", "co2", "light", "spectral", "uv", "infrared",
                       "acceleration", "rotation", "magnetic", "distance", "current", "voltage", "power", "battery",
                       "analog"]] = Field([], description="What it measures, as many as fit (its main functions only, "
                                                          "not e.g. an accelerometer's on-chip temperature sensor)")
    buses: list[ChipBus] = Field(description="One entry per bus it talks on (I2C, SPI), each with its own settings and table")
    datasheet: str = Field("", description="Which datasheet (title, document number, revision) the table comes from")
    datasheet_url: str = Field("", description="Link to that datasheet (https://...)")
    notes: str = ""


@mcp.tool()
async def chips_list() -> dict:
    """The sensor library: every chip type with its addresses, how complete its table is, whether it's checked
    (built-in tables are checked against their datasheet by the project; an agent's table until a person checks it),
    and which boards use it."""
    rows = await api.get("/api/chips")
    def bus(b: dict) -> dict:
        return {"bus": b["bus"], **({"addresses": [hex(a) for a in b.get("addresses", [])]} if b["bus"] == "i2c" else {}),
                "style": b.get("style", "basic"), "registers": len(b.get("registers", [])), "commands": len(b.get("commands", []))}
    return {"chips": [{"name": r["name"], "vendor": r["body"].get("vendor", ""), "kind": r["body"].get("kind", ""),
                       "category": r["body"].get("category"), "tags": r["body"].get("tags", []),
                       "buses": [bus(b) for b in r["body"].get("buses", [])],
                       "source": r["source"], "checked": r["checked"], "boards": r["boards"]} for r in rows]}


@mcp.tool()
async def chip_get(name: Annotated[str, Field(description="Chip type name, e.g. AS7343")]) -> dict:
    """One chip table from the sensor library, in the form chip_save takes."""
    for r in await api.get("/api/chips"):
        if r["name"].lower() == name.lower():
            return {"table": r["body"], "source": r["source"], "agent": r["agent"], "checked": r["checked"], "boards": r["boards"]}
    raise ApiError(f"{name} is not in the sensor library (chips_list shows what is)")


@mcp.tool()
async def chip_save(table: ChipTable, ctx: Context) -> dict:
    """Add a chip type to the sensor library, or replace its table (matched by name), e.g. from its datasheet.
    Only put in what the datasheet says: leave out registers or formulas you're not sure of, and name the datasheet
    and its revision. What you save is marked "by agent, not checked" until the user checks it in Settings → Devices &
    sensors → Sensor library; saving over a built-in table makes it yours (the user can put the built-in back). Needs
    "Agents may edit the sensor library" in Settings → Agents; if it's off, ask the user to switch it on."""
    body = table.model_dump(exclude_none=True)
    body["buses"] = [b.model_dump(exclude_none=True) for b in table.buses]
    who = hw.client_name(ctx)
    for r in await api.get("/api/chips"):
        if r["name"].lower() == table.name.lower():
            saved = await api.request("PUT", f"/api/chips/{r['id']}", json=body, agent=who)
            return {"updated": saved["name"], "checked": saved["checked"], "boards": saved["boards"]}
    saved = await api.request("POST", "/api/chips", json=body, agent=who)
    return {"added": saved["name"], "checked": saved["checked"]}


ChipKey = Annotated[str, Field(description="Where the chip sits: m:<address> on the main bus, <port>:<address> behind "
                                           "an I2C switch; address in decimal, e.g. m:68 (0x44) or 2:16 (0x10 on port 2); "
                                           "spi for the SPI chip on the logic analyzer's chip select")]


class BoardChip(BaseModel):
    type: str | None = Field(None, description="Chip type from the sensor library (chips_list)")
    name: str = Field("", description="Its name on this board, e.g. Light north; empty = the type's name")


async def _board(device_id: int | None) -> dict:
    if device_id is None:
        d = await api.get("/api/devices/active")
        if not d:
            raise ApiError("no board on the bench: give device_id (list_sessions shows device names)")
        return d
    for d in await api.get("/api/devices"):
        if d["id"] == device_id:
            return d
    raise ApiError(f"no device {device_id}")


@mcp.tool()
async def board_chips(device_id: Annotated[int | None, Field(description="Device id; default: the board on the bench")] = None) -> dict:
    """The chips on a board's I2C bus: where each sits (main bus or a switch port, address), its type from the
    sensor library and its name."""
    d = await _board(device_id)
    chips = await api.get(f"/api/devices/{d['id']}/bus-chips")
    return {"device": {"id": d["id"], "name": d.get("name")}, "chips": chips}


@mcp.tool()
async def set_board_chips(
    chips: Annotated[dict[ChipKey, BoardChip], Field(description="Chips to add or change, by where they sit")],
    remove: Annotated[list[ChipKey], Field(description="Chips to take off the board")] = [],
    device_id: Annotated[int | None, Field(description="Device id; default: the board on the bench")] = None,
) -> dict:
    """Put chips from the sensor library on a board, rename them or take them off (the others stay as they are).
    Needs "Agents may edit the sensor library" in Settings → Agents."""
    d = await _board(device_id)
    now = await api.get(f"/api/devices/{d['id']}/bus-chips")
    for k in remove:
        now.pop(k, None)
    for k, c in chips.items():
        now[k] = {**now.get(k, {}), **c.model_dump(exclude_none=True)}
    saved = await api.request("PUT", f"/api/devices/{d['id']}/bus-chips", json=now)
    return {"device": {"id": d["id"], "name": d.get("name")}, "chips": saved}


@mcp.tool()
async def bus_decode(seconds: Annotated[float, Field(gt=0, le=30, description="How many of the last seconds")] = 5) -> dict:
    """Decode the logic analyzer's last seconds of I2C/SPI (it keeps 30 s) without putting them on the timeline: the
    messages (address, writes and reads with their bytes, NACK) with times, and the board's chips. Use it to check a
    chip table against what the board really sends. Needs the analyzer module, streaming."""
    r = await api.request("POST", "/api/modules/analyzer/decode", json={"seconds": seconds, "post": False})
    items = r.get("items", [])
    out = [{"time": iso(i["t"]), "kind": i["kind"], **{k: v for k, v in i["data"].items() if k != "label"}} for i in items[:400]]
    chips = {}
    try:
        d = await api.get("/api/devices/active")
        if d:
            chips = await api.get(f"/api/devices/{d['id']}/bus-chips")
    except ApiError:
        pass
    return {"seconds": seconds, "found": r.get("found"), "shown": len(out), "truncated": r.get("truncated") or len(items) > 400,
            "messages": out, "board_chips": chips}


@mcp.tool()
async def session_bus(
    session_id: SessionId,
    start_s: StartS = None, end_s: EndS = None,
    cycle: Annotated[int | None, Field(ge=0, description="One cycle (index from 0, as cycle_summary / get_cycle count "
                                                         "them) instead of start_s / end_s")] = None,
    cycle_type: Annotated[str | None, Field(description="The cycle's type (default: all wakes)")] = None,
    chip: Annotated[str | None, Field(description="Only chips whose name, type or place contains this, e.g. VEML "
                                                  "or port 2")] = None,
    limit: Annotated[int, Field(ge=1, le=2000)] = 300,
) -> dict:
    """The I2C / SPI messages the logic analyzer decoded during a recording (around each wake, or all of it), read
    only. Each message: time (t_s, session seconds), chip (its name on the board, else its type), where it sits
    (m:0x44 on the main bus, port 2:0x10 behind the I2C switch), kind (reg = register read, set = register write,
    cmd = command and its answer, probe, read, write, spi), the register or command name and readings from the
    sensor library, and the bytes. The same message repeated in a row is one entry with times, every_ms and the last
    reading. decoded.missed: stretches the analyzer couldn't decode (and why)."""
    if cycle is not None:
        c = await api.get(f"/api/sessions/{session_id}/cycles", type=cycle_type, index=cycle)
        start_s, end_s = c["start_s"], c["end_s"]
    return await api.get(f"/api/sessions/{session_id}/bus/named", start_s=start_s, end_s=end_s, chip=chip, limit=limit)


# the board's USB console and firmware uploads (read-only) ---------------------------------------------------------
@mcp.tool()
async def usb_console_lines(
    last: Annotated[int, Field(ge=1, le=1000, description="How many of the newest lines")] = 100,
) -> dict:
    """The newest lines from the board's own USB console (only while it was open; it repeats the UART log), and a
    key menu the firmware is waiting on, if any."""
    st = await api.get("/api/device/status")
    u = st.get("usb") or {}
    n = u.get("lines") or 0
    got = await api.get("/api/device/usb/lines", after_seq=max(-1, n - 1 - last), limit=last) if n else []
    return {"usb": usb_brief(u), "lines": [{"time": iso(l["wall"]), "text": l["text"]} for l in got]}


@mcp.tool()
async def list_uploads(
    job_id: Annotated[int | None, Field(description="One upload, with its esptool log")] = None,
) -> dict:
    """Firmware uploads from PlatformIO through the tracker since it started (newest first): what, from where,
    result, how long, whether the board was power-cycled or a recording marked. With job_id: that upload's log."""
    if job_id is not None:
        j = await api.get(f"/api/flash/{job_id}")
        return {**flash_brief(j), "log": j.get("log", [])}
    return {"uploads": [flash_brief(j) for j in await api.get("/api/flash/jobs")]}


# hardware (only when switched on in Settings → Agents; every call needs the user's OK) ------------------------
def _hw_tool():
    return mcp.tool(meta=HW_META, annotations=HW_ANNOTATIONS)


async def _status() -> dict:
    return await api.get("/api/device/status")


def _power_brief(st: dict) -> dict:
    p = st.get("ppk2", {})
    return {"dut_power": p.get("dut_power"), "voltage_mv": p.get("voltage_mv"), "measuring": p.get("streaming"),
            "limit_mv": p.get("max_voltage_mv"), "recording": ((st.get("session") or {}).get("session") or {}).get("name")
            if (st.get("session") or {}).get("active") else None}


@_hw_tool()
async def dut_power(
    on: Annotated[bool, Field(description="true = power the board, false = switch it off")],
    voltage_mv: Annotated[int | None, Field(ge=800, le=5000, description="Voltage to power it at (default: the device's "
                                                                          "voltage, or the one set now)")] = None,
    ctx: Context | None = None,
) -> dict:
    """Switch the board's power (the PPK2 output) on or off. On: sets the voltage first (never above the device's
    limit) and starts measuring if needed. Off doesn't stop a recording."""
    async def act():
        st = await _status()
        p = st.get("ppk2", {})
        if on:
            mv = voltage_mv
            if mv is None:
                dev = await api.get("/api/devices/active")
                mv = (dev or {}).get("voltage_mv") or (p.get("voltage_mv") if p.get("voltage_set_by_us") else None)
            if mv is None:
                raise ApiError("no voltage known: give voltage_mv")
            if not p.get("voltage_set_by_us") or p.get("voltage_mv") != mv or p.get("mode") != "source":
                await api.hardware("POST", "/api/device/ppk2/source", {"voltage_mv": mv})
            await api.hardware("POST", "/api/device/ppk2/dut", {"on": True, "by": "agent"})
            if not p.get("streaming"):
                await api.hardware("POST", "/api/device/ppk2/stream", {"on": True})
        else:
            await api.hardware("POST", "/api/device/ppk2/dut", {"on": False, "by": "agent"})
        return _power_brief(await _status())
    return await hw.run(ctx, "dut_power", {"on": on, "voltage_mv": voltage_mv}, act)


@_hw_tool()
async def power_cycle(
    off_s: Annotated[float, Field(ge=0.2, le=30, description="How long the board stays off, in seconds")] = 1.0,
    ctx: Context | None = None,
) -> dict:
    """Switch the board off, wait, and switch it on again: a cold boot at the voltage set now (e.g. to restart the
    firmware, or to see a full boot in the log). A running recording keeps going and marks both switches, so the
    boot doesn't count as an unexpected reset."""
    async def act():
        p = (await _status()).get("ppk2", {})
        if not p.get("voltage_set_by_us"):
            raise ApiError("the tracker hasn't set the voltage yet: use dut_power(on=true) first")
        if p.get("dut_power"):
            await api.hardware("POST", "/api/device/ppk2/dut", {"on": False, "by": "agent"})
            await asyncio.sleep(off_s)
        await api.hardware("POST", "/api/device/ppk2/dut", {"on": True, "by": "agent"})
        if not p.get("streaming"):
            await api.hardware("POST", "/api/device/ppk2/stream", {"on": True})
        return {**_power_brief(await _status()), "was_on": bool(p.get("dut_power")), "off_s": off_s if p.get("dut_power") else 0}
    return await hw.run(ctx, "power_cycle", {"off_s": off_s}, act)


@_hw_tool()
async def set_voltage(
    voltage_mv: Annotated[int, Field(ge=800, le=5000, description="New PPK2 output voltage (the device's limit applies)")],
    ctx: Context | None = None,
) -> dict:
    """Change the board's supply voltage (the PPK2 output). A running battery-emulation recording sets it by itself."""
    async def act():
        await api.hardware("POST", "/api/device/ppk2/source", {"voltage_mv": voltage_mv})
        return _power_brief(await _status())
    return await hw.run(ctx, "set_voltage", {"voltage_mv": voltage_mv}, act)


@_hw_tool()
async def measuring(
    on: Annotated[bool, Field(description="true = the PPK2 streams current, false = stop measuring")],
    ctx: Context | None = None,
) -> dict:
    """Start or stop measuring (the PPK2 stream). Recording needs it on; the board's power doesn't change."""
    async def act():
        await api.hardware("POST", "/api/device/ppk2/stream", {"on": on})
        return _power_brief(await _status())
    return await hw.run(ctx, "measuring", {"on": on}, act)


@_hw_tool()
async def start_recording(
    name: Annotated[str, Field(min_length=1, max_length=200, description="Session name")],
    kind: Annotated[Literal["detail", "soak"] | None, Field(description="detail = every sample; soak = 10 ms summaries "
                                                                        "+ raw around wakes (default: as last time)")] = None,
    voltage_mv: Annotated[int | None, Field(ge=800, le=5000, description="Default: as last time / the device's")] = None,
    max_duration_s: Annotated[float | None, Field(gt=0, description="Stop by itself after this long")] = None,
    notes: Annotated[str, Field(max_length=2000)] = "",
    ctx: Context | None = None,
) -> dict:
    """Start a recording with the last recording's settings (like the web UI's Record), changing only what you
    give. Powers the board at the session voltage."""
    args = {"name": name, "kind": kind, "voltage_mv": voltage_mv, "max_duration_s": max_duration_s}

    async def act():
        last = await api.get("/api/last-session-config") or {}
        body = {k: v for k, v in (last.get("config") or {}).items() if v is not None}
        body.update({"name": name, "notes": notes, "kind": kind or body.get("kind") or "detail"})
        if voltage_mv is not None:
            body["voltage_mv"] = voltage_mv
            body.pop("battery", None)
        if max_duration_s is not None:
            body["max_duration_s"] = max_duration_s
        r = await api.hardware("POST", "/api/sessions", body)
        return {"recording": r.get("session"), "settings": {k: body.get(k) for k in ("kind", "voltage_mv", "max_duration_s")}}
    return await hw.run(ctx, "start_recording", args, act)


@_hw_tool()
async def stop_recording(
    dut_off: Annotated[bool, Field(description="Also switch the board off")] = False,
    ctx: Context | None = None,
) -> dict:
    """Stop the running recording (it's kept; see get_session)."""
    async def act():
        return await api.hardware("POST", "/api/sessions/stop", {"dut_off": dut_off})
    return await hw.run(ctx, "stop_recording", {"dut_off": dut_off}, act)


@_hw_tool()
async def save_live_range(
    name: Annotated[str, Field(min_length=1, max_length=200)],
    seconds: Annotated[float, Field(gt=0, le=300, description="The last N seconds of the live buffer (~5 min kept)")] = 60,
    notes: Annotated[str, Field(max_length=2000)] = "",
    ctx: Context | None = None,
) -> dict:
    """Keep the last N seconds of the live measurement (and its serial log) as a session, without recording."""
    async def act():
        b = (await _status()).get("buffer") or {}
        end = b.get("end_idx", 0)
        start = max(b.get("start_idx", 0), end - round(seconds / b.get("sample_period_s", 1e-5)))
        if end - start < 10:
            raise ApiError("no live data: the PPK2 isn't measuring")
        return await api.hardware("POST", "/api/device/sessions/save-buffer", {"name": name, "start_idx": start, "end_idx": end, "notes": notes})
    return await hw.run(ctx, "save_live_range", {"name": name, "seconds": seconds}, act)


async def _usb_after(n_before: int, wait_s: float = 1.5) -> dict:
    await asyncio.sleep(wait_s)
    st = await _status()
    got = await api.get("/api/device/usb/lines", after_seq=n_before - 1, limit=200)
    return {"usb": usb_brief(st.get("usb")), "new_lines": [l["text"] for l in got]}


@_hw_tool()
async def usb_console(
    open: Annotated[bool, Field(description="true = open the board's USB console, false = close it")],
    ctx: Context | None = None,
) -> dict:
    """Open or close the board's own USB console. Open: the firmware sees USB, shows its debug menu on the next boot
    and can't deep-sleep (this changes the current you measure). Close it when done."""
    async def act():
        n = ((await _status()).get("usb") or {}).get("lines") or 0
        await api.hardware("POST", "/api/device/usb/open", {"on": open})
        return await _usb_after(n, 1.0)
    return await hw.run(ctx, "usb_console", {"open": open}, act)


@_hw_tool()
async def usb_key(
    key: Annotated[str, Field(min_length=1, max_length=8, description="The key, e.g. 'n' from the menu's [n]")],
    ctx: Context | None = None,
) -> dict:
    """Answer a key menu on the board's USB console (e.g. its debug menu): sends the key alone. Returns the lines
    that followed."""
    async def act():
        n = ((await _status()).get("usb") or {}).get("lines") or 0
        await api.hardware("POST", "/api/device/usb/key", {"key": key})
        return await _usb_after(n)
    return await hw.run(ctx, "usb_key", {"key": key}, act)


@_hw_tool()
async def usb_send(
    text: Annotated[str, Field(min_length=1, max_length=500, description="A line to send (a newline is added)")],
    ctx: Context | None = None,
) -> dict:
    """Send a line to the board over its USB console (it must be open). Returns the lines that followed."""
    async def act():
        n = ((await _status()).get("usb") or {}).get("lines") or 0
        await api.hardware("POST", "/api/device/usb/send", {"text": text, "eol": "\n"})
        return await _usb_after(n)
    return await hw.run(ctx, "usb_send", {"text": text}, act)
