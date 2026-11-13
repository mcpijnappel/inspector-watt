"""HTTP + WebSocket API of the capture service (internal; the api service and debugging tools talk to it)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .config import Config
from .ppk2 import SAMPLE_PERIOD_S
from .recorder import SessionConfig
from .ringbuffer import bin_stats, to_json_list
from .service import CaptureService, DeviceError
from . import battery
from .flash import Flasher, FlashError, FlashRequest, Image
from .modules import ModuleEvents
from .sessions import SessionError, SessionManager
from .store import Store
from .usb import UsbError

log = logging.getLogger(__name__)

LIVE_FRAME_S = 0.04  # 25 frames/s
MAX_BINS_PER_FRAME = 5000
MAX_WINDOW_POINTS = 100_000


class VoltageIn(BaseModel):
    voltage_mv: int = Field(ge=800, le=5000)


class OnOffIn(BaseModel):
    on: bool
    by: str = Field("user", pattern="^(user|agent)$")  # who switched (board power): the web UI or an agent (MCP)


class SettingsIn(BaseModel):
    idle_power_off_s: int | None = Field(None, ge=0, le=86400)
    spike_filter: bool | None = None
    auto_stream: bool | None = None
    max_voltage_mv: int | None = Field(None, ge=800, le=5000)  # the tracker's limit (the api only lets the web UI set it)
    min_free_gb: float | None = Field(None, ge=1, le=10000)
    raw_mantissa_bits: int | None = Field(None, ge=8, le=23)
    flash_when_asleep: str | None = Field(None, pattern="^(wait|always|cycle)$")
    flash_wake_wait_s: float | None = Field(None, ge=5, le=3600)


class BaudIn(BaseModel):
    baud: int


class SendIn(BaseModel):
    text: str
    eol: str = "\n"


class KeyIn(BaseModel):
    key: str = Field(min_length=1, max_length=8)


class ImageIn(BaseModel):
    offset: int | None = Field(None, ge=0)  # None: the backend places it (esp32 app_only)
    data_b64: str


class FlashIn(BaseModel):
    """One upload. family picks the backend (esp32, rp2); the esp32 settings stay top-level (as uploads have always
    sent them)."""
    family: str = "esp32"
    images: list[ImageIn] = Field(min_length=1, max_length=16)
    chip: str = ""  # "": from the images
    flash_mode: str = "keep"
    flash_freq: str = "keep"
    flash_size: str = "keep"
    app_only: bool = False
    on_recording: str = "ask"
    when_asleep: str = ""
    full_flash: bool = False
    power: str = ""  # ppk2 | usb; "": the family's default (esp32: ppk2, rp2: usb)
    meta: dict = {}

    def request(self) -> FlashRequest:
        try:
            images = [Image(im.offset, base64.b64decode(im.data_b64, validate=True)) for im in self.images]
        except (binascii.Error, ValueError) as e:
            raise FlashError(f"image data is not base64: {e}") from e
        options = {"flash_mode": self.flash_mode, "flash_freq": self.flash_freq, "flash_size": self.flash_size,
                   "app_only": self.app_only} if self.family == "esp32" else {}
        return FlashRequest(images, self.chip, self.family, options, self.meta, self.on_recording,
                            self.when_asleep, self.full_flash, self.power)


class ModuleEventIn(BaseModel):
    kind: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_.-]+$")
    t: float  # unix time (this machine's clock)
    end_t: float | None = None
    data: dict = {}


class ModuleEventsIn(BaseModel):
    module: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9_-]+(:[a-z0-9_-]+)?$")  # "analyzer:1"
    items: list[ModuleEventIn] = Field(max_length=2000)
    live: bool = True  # into the Live list
    record: bool = True  # into the running recording


class ChoiceIn(BaseModel):
    choice: str


class SessionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern="^(detail|soak)$")
    notes: str = ""
    voltage_mv: int | None = Field(None, ge=800, le=5000)
    dut_power: bool = True
    wake_threshold_ua: float = Field(500, gt=0)
    hold_ms: float = Field(500, ge=10)
    pre_ms: float = Field(100, ge=0, le=60_000)
    post_ms: float = Field(500, ge=0, le=60_000)
    raw_buffer_hours: float = Field(0, ge=0, le=168)
    auto_restore: bool = True
    uart_baud: int | None = None
    max_duration_s: float | None = Field(None, gt=0)
    battery: BatteryIn | None = None

    def config(self) -> SessionConfig:
        return SessionConfig(kind=self.kind, voltage_mv=self.voltage_mv, dut_power=self.dut_power,
                             wake_threshold_a=self.wake_threshold_ua * 1e-6, hold_s=self.hold_ms / 1000,
                             pre_s=self.pre_ms / 1000, post_s=self.post_ms / 1000,
                             raw_buffer_hours=self.raw_buffer_hours, auto_restore=self.auto_restore,
                             uart_baud=self.uart_baud, max_duration_s=self.max_duration_s,
                             battery=self.battery.model_dump() if self.battery else None)


class BatteryIn(BaseModel):
    mode: str = Field("charge", pattern="^(charge|time)$")
    chemistry: str = "lipo"
    cells: int = Field(1, ge=1, le=4)
    capacity_mah: float = Field(1000, gt=0)
    speedup: float = Field(1, gt=0, le=100_000)
    start_soc: float = Field(100, gt=0, le=100)
    start_mv: int = Field(4200, ge=800, le=5000)
    sweep_min: float = Field(120, gt=0)
    cutoff_mv: int = Field(3000, ge=800, le=5000)
    on_cutoff: str = Field("protect", pattern="^(protect|hunt)$")
    floor_mv: int = Field(2500, ge=800, le=5000)


class DeviceIn(BaseModel):
    id: int | None = None
    name: str | None = None
    max_mv: int | None = Field(None, ge=800, le=5000)
    uart_baud: int | None = Field(None, ge=300, le=12_000_000)  # its debug UART's speed


class StopIn(BaseModel):
    dut_off: bool = False


class SaveRangeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    start_idx: int
    end_idx: int
    notes: str = ""


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config()
    svc = CaptureService(cfg)
    sessions: SessionManager | None = None
    if cfg.database_url:
        data_dir = Path(cfg.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        sessions = SessionManager(svc, Store(cfg.database_url), data_dir, int(cfg.min_free_gb * 2**30),
                                  cfg.raw_mantissa_bits)
    flasher = Flasher(svc, svc.usb, sessions)
    # Don't switch the board off under a recording or a flash.
    svc.idle.busy = (lambda: sessions.active is not None or flasher.busy) if sessions else (lambda: flasher.busy)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc.start()
        if sessions:
            sessions.start()
        yield
        if sessions:
            sessions.shutdown()
        svc.stop()

    app = FastAPI(title="capture", lifespan=lifespan)
    app.state.svc = svc
    app.state.sessions = sessions
    app.state.flasher = flasher

    def require_sessions() -> SessionManager:
        if sessions is None:
            raise HTTPException(503, "no database configured (DATABASE_URL)")
        return sessions

    def refuse(e: Exception) -> HTTPException:
        log.warning("request refused: %s", e)
        return HTTPException(409, str(e))

    def device_call(fn, *args):
        try:
            fn(*args)
        except DeviceError as e:
            raise refuse(e) from e

    def buffer_info() -> dict:
        r = svc.ring
        end = r.end
        off = svc.clock.offset
        # Unix time of sample `end`, so clients can turn stream indices into clock times.
        end_wall = None if off is None else time.time() - (time.monotonic() - (off + end * SAMPLE_PERIOD_S))
        return {"generation": r.generation, "start_idx": r.start, "end_idx": end, "end_wall": end_wall,
                "sample_period_s": SAMPLE_PERIOD_S, "capacity": r.capacity}

    @app.get("/status")
    def status(viewer: bool = False):
        if viewer:  # polled by an open browser: someone is watching
            svc.idle.viewer_seen()
        return {"ppk2": svc.ppk2.status(), "uart": svc.uart.status(), "usb": svc.usb.status(),
                "flash": flash_brief(), "buffer": buffer_info(), "idle_power_off": svc.idle.status(),
                "session": sessions.status() if sessions else None, "time": time.time()}

    @app.get("/sessions/current")
    def current_session():
        return require_sessions().status()

    @app.post("/sessions")
    def start_session(body: SessionIn):
        try:
            return require_sessions().start_session(body.name, body.config(), body.notes)
        except (SessionError, DeviceError) as e:
            raise refuse(e) from e

    @app.post("/sessions/save-buffer")
    def save_buffer(body: SaveRangeIn):
        """Keep a piece of the live buffer (stream indices) as a session, e.g. after watching without recording."""
        try:
            return require_sessions().save_range(body.name, body.start_idx, body.end_idx, body.notes)
        except SessionError as e:
            raise refuse(e) from e

    @app.post("/sessions/stop")
    def stop_session(body: StopIn):
        try:
            return require_sessions().stop_session(body.dut_off)
        except SessionError as e:
            raise refuse(e) from e

    @app.get("/ppk2/metadata")
    def metadata():
        cal = svc.ppk2.calibration
        if cal is None:
            raise HTTPException(409, "PPK2 not connected yet")
        return {"raw": cal.raw, "parsed": cal.to_dict()}

    @app.post("/ppk2/source")
    def set_source(body: VoltageIn):
        device_call(svc.ppk2.set_source_voltage, body.voltage_mv)
        return svc.ppk2.status()

    @app.post("/ppk2/dut")
    def set_dut(body: OnOffIn):
        device_call(svc.ppk2.set_dut_power, body.on, body.by)
        if sessions:
            # Mark it in a running recording: the boot after a power-on is then the tracker's own, not "power lost".
            sessions.note_event("power_on" if body.on else "power_off", svc.ring.end, by=body.by)
        return svc.ppk2.status()

    @app.put("/ppk2/device")
    def set_device(body: DeviceIn):
        """The device on the bench: its voltage limit (max_mv null = no device: the tracker's limit only) and its UART
        speed."""
        device_call(svc.set_device, body.model_dump() if body.max_mv else None)
        return svc.ppk2.status()

    @app.post("/ppk2/stream")
    def set_stream(body: OnOffIn):
        device_call(svc.ppk2.start_stream if body.on else svc.ppk2.stop_stream)
        return svc.ppk2.status()

    @app.post("/ppk2/spike-filter")
    def set_spike_filter(body: OnOffIn):
        svc.ppk2.set_spike_filter(body.on)
        return svc.ppk2.status()

    def all_settings() -> dict:
        return {**svc.settings(), **(sessions.settings() if sessions else {})}

    @app.get("/settings")
    def get_settings():
        return all_settings()

    @app.put("/settings")
    def put_settings(body: SettingsIn):
        """Change settings while running (the api saves them; they're loaded again at startup)."""
        s = body.model_dump()
        device_call(svc.apply_settings, s)
        if sessions:
            sessions.apply_settings(s)
        return all_settings()

    @app.get("/battery/chemistries")
    def battery_chemistries():
        return battery.chemistries()

    # modules: their events onto the timeline (and into the running recording) ------------------------------
    module_events = ModuleEvents(svc.clock, sessions)
    app.state.module_events = module_events

    @app.post("/modules/events")
    def add_module_events(body: ModuleEventsIn):
        return module_events.add(body.module, [it.model_dump() for it in body.items], body.live, body.record)

    @app.get("/modules/recording")
    def module_recording(after_seq: int = 0):
        """For modules: the running recording and its wakes (unix times), to decode the bus around each."""
        if sessions is None:
            return {"recording": None, "wakes": [], "last_seq": 0}
        return sessions.wake_feed(after_seq)

    @app.get("/modules/events/live")
    def live_module_events(after_seq: int = 0, module: str | None = None, since_idx: int | None = None,
                           limit: int = Query(2000, le=5000)):
        return module_events.live(after_seq, module, since_idx, limit)

    @app.get("/uart/lines")
    def uart_lines(after_seq: int = -1, limit: int = Query(1000, le=10000)):
        return [line.to_dict() for line in svc.uart.log.after(after_seq, limit)]

    @app.put("/uart/config")
    def uart_config(body: BaudIn):
        device_call(svc.uart.set_baud, body.baud)
        return svc.uart.status()

    @app.post("/uart/send")
    def uart_send(body: SendIn):
        try:
            n = svc.uart.send(body.text, body.eol)
        except DeviceError as e:
            raise HTTPException(409, str(e)) from e
        return {"sent_bytes": n}

    # the board's own USB: console (opened on demand) and flashing -----------------------------------------------
    def flash_brief() -> dict | None:
        """The running (or last) flash job without its log, for status polls."""
        j = flasher.latest()
        if j is None:
            return None
        d = j.to_dict(after=1 << 30)
        d.pop("log")
        return d

    @app.get("/usb")
    def usb_status():
        return svc.usb.status()

    @app.post("/usb/open")
    def usb_open(body: OnOffIn):
        """Open (on) or close the console. While it's open the firmware sees USB connected: no deep sleep."""
        svc.usb.set_wanted(body.on)
        return svc.usb.status()

    @app.get("/usb/lines")
    def usb_lines(after_seq: int = -1, limit: int = Query(1000, le=5000)):
        return [line.to_dict() for line in svc.usb.log.after(after_seq, limit)]

    @app.post("/usb/send")
    def usb_send(body: SendIn):
        try:
            return {"sent_bytes": svc.usb.send(body.text, body.eol)}
        except UsbError as e:
            raise refuse(e) from e

    @app.post("/usb/key")
    def usb_key(body: KeyIn):
        """Answer a menu: the key alone, no line end (the firmware reads one character)."""
        try:
            return {"sent_bytes": svc.usb.send(body.key, "")}
        except UsbError as e:
            raise refuse(e) from e

    if svc.usb.sim_factory is not None:
        @app.post("/sim/usb")
        def sim_usb(body: OnOffIn):
            """Simulator only: the board's USB port there (awake) or gone (asleep), for trying uploads to a sleeping board."""
            svc.usb.sim_factory.present = body.on
            return svc.usb.status()

    @app.post("/flash")
    def flash_start(body: FlashIn):
        try:
            return flasher.submit(body.request()).to_dict()
        except FlashError as e:
            raise refuse(e) from e

    @app.get("/flash")
    def flash_latest():
        j = flasher.latest()
        return j.to_dict() if j else None

    @app.get("/flash/jobs")
    def flash_jobs():
        """The last flash jobs, newest first, without their logs."""
        out = []
        for j in flasher.recent():
            d = j.to_dict(after=1 << 30)
            d.pop("log")
            out.append(d)
        return out

    @app.get("/flash/{job_id}")
    def flash_job(job_id: int, after: int = 0):
        try:
            return flasher.get(job_id).to_dict(after)
        except FlashError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/flash/{job_id}/cancel")
    def flash_cancel(job_id: int):
        """Cancel an upload that's still waiting (for an answer, the recording, or the board to wake)."""
        try:
            return flasher.cancel(job_id).to_dict()
        except FlashError as e:
            raise refuse(e) from e

    @app.post("/flash/{job_id}/now")
    def flash_now(job_id: int):
        """Waiting for the board to wake: power-cycle it now instead."""
        try:
            return flasher.power_cycle_now(job_id).to_dict()
        except FlashError as e:
            raise refuse(e) from e

    @app.post("/flash/{job_id}/answer")
    def flash_answer(job_id: int, body: ChoiceIn):
        try:
            return flasher.answer(job_id, body.choice).to_dict()
        except FlashError as e:
            raise refuse(e) from e

    @app.get("/buffer/window")
    def buffer_window(start: int, end: int, points: int = Query(2000, ge=10, le=MAX_WINDOW_POINTS)):
        """Samples [start, end) from the RAM buffer: raw if they fit in `points`, else min/max/mean bins."""
        if end <= start:
            raise HTTPException(400, "end must be > start")
        bin_size = max(1, math.ceil((end - start) / points))
        actual_start, cur, logic = svc.ring.read(start, end)
        if bin_size == 1:
            return {**buffer_info(), "start": actual_start, "bin_samples": 1,
                    "current": to_json_list(cur), "logic": logic.tolist()}
        mn, mx, mean, lor = bin_stats(cur, logic, bin_size)
        return {**buffer_info(), "start": actual_start, "bin_samples": bin_size,
                "min": to_json_list(mn), "max": to_json_list(mx), "mean": to_json_list(mean), "logic": lor.tolist()}

    @app.get("/buffer/stats")
    def buffer_stats(start: int, end: int):
        """Exact statistics of samples [start, end) from the RAM buffer (for a selected range)."""
        actual, cur, _logic = svc.ring.read(start, end)
        valid = cur[~np.isnan(cur)].astype(np.float64)
        n = len(valid)
        return {
            "start": actual, "end": actual + len(cur), "duration_s": len(cur) * SAMPLE_PERIOD_S,
            "mean_a": float(valid.mean()) if n else None,
            "min_a": float(valid.min()) if n else None,
            "max_a": float(valid.max()) if n else None,
            "charge_c": float(valid.sum()) * SAMPLE_PERIOD_S,
            "lost": len(cur) - n,
        }

    @app.websocket("/live")
    async def live(ws: WebSocket, bin_us: int = 1000):
        """Pushes new data ~25x/s: {"type":"ppk", start, bin_samples, min, max, mean, logic} binned to `bin_us`,
        {"type":"uart", lines:[...]}, and {"type":"status", ...} once per second."""
        await ws.accept()
        bin_samples = max(1, round(bin_us * 1e-6 / SAMPLE_PERIOD_S))
        ring = svc.ring
        gen = ring.generation
        cursor = ring.end - ring.end % bin_samples
        uart_seq = svc.uart.log.last_seq
        next_status = 0.0
        try:
            while True:
                await asyncio.sleep(LIVE_FRAME_S)
                if ring.generation != gen or cursor < ring.start:  # new stream, or client fell behind
                    gen = ring.generation
                    cursor = max(ring.start, ring.end - ring.end % bin_samples)
                nbins = min((ring.end - cursor) // bin_samples, MAX_BINS_PER_FRAME)
                if nbins > 0:
                    start, cur, logic = ring.read(cursor, cursor + nbins * bin_samples)
                    mn, mx, mean, lor = bin_stats(cur, logic, bin_samples)
                    await ws.send_json({"type": "ppk", "generation": gen, "start": start, "bin_samples": bin_samples,
                                        "sample_period_s": SAMPLE_PERIOD_S, "min": to_json_list(mn),
                                        "max": to_json_list(mx), "mean": to_json_list(mean), "logic": lor.tolist()})
                    cursor = start + len(mn) * bin_samples
                lines = svc.uart.log.after(uart_seq, 500)
                if lines:
                    uart_seq = lines[-1].seq
                    await ws.send_json({"type": "uart", "lines": [ln.to_dict() for ln in lines]})
                now = time.monotonic()
                if now >= next_status:
                    next_status = now + 1.0
                    await ws.send_json({"type": "status", "ppk2": svc.ppk2.status(), "uart": svc.uart.status(),
                                        "usb": svc.usb.status(), "flash": flash_brief()})
        except (WebSocketDisconnect, RuntimeError):
            return

    return app
