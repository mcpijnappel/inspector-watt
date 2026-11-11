"""The logic analyzer module's main container (issue #12).

It describes itself to the tracker (GET /module: settings, event types, MCP tools, health) and takes its settings
from the api (saved there; read again at start). While an analyzer is there and the module is on, sigrok-cli
streams all 8 channels into a rolling buffer in memory (stream.py). POST /decode decodes the last seconds of it
(I2C, SPI: decode.py) and sends the transactions to the api as events (POST /api/modules/analyzer/events), which
puts them on the timeline: in the running recording, and in the Live list.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import decode as dec
from . import sigrok
from .stream import Streamer, buffer_seconds

log = logging.getLogger("analyzer")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

API_URL = os.environ.get("API_URL", "http://api:8000")
DRIVER = os.environ.get("ANALYZER_DRIVER", "fx2lafw")
CHECK_EVERY_S = 5.0
MAX_ITEMS = 5000  # transactions per decode: a noisy pin (or the demo device) would make far more
POST_BATCH = 1000  # events per request to the api (it takes up to 2000)
VERSION = "0.2.0"
PIN_CHOICES = ["off", *dec.PINS]

SETTINGS = [
    {"key": "enabled", "type": "bool", "default": True, "label": "On",
     "hint": "Off: the analyzer is left alone (no streaming, no decoding)."},
    # fx2lafw's rates from 1 MHz up; it streams up to 24 MHz on 8 channels if the computer keeps up (USB 2)
    {"key": "samplerate_hz", "type": "choice",
     "choices": [1_000_000, 2_000_000, 4_000_000, 8_000_000, 12_000_000, 16_000_000, 24_000_000], "default": 4_000_000,
     "label": "Sample rate", "unit": "Hz",
     "hint": "4 MHz covers I2C at 400 kHz and SPI to about 1 MHz. Up to 24 MHz for faster SPI: it may drop samples "
             "on a busy machine and fills the buffer 6x faster (about 24 MB per second)."},
    {"key": "i2c_sda", "type": "choice", "choices": PIN_CHOICES, "default": "D0", "label": "I2C SDA", "group": "wiring"},
    {"key": "i2c_scl", "type": "choice", "choices": PIN_CHOICES, "default": "D1", "label": "I2C SCL", "group": "wiring"},
    {"key": "spi_clk", "type": "choice", "choices": PIN_CHOICES, "default": "off", "label": "SPI clock", "group": "wiring"},
    {"key": "spi_mosi", "type": "choice", "choices": PIN_CHOICES, "default": "off", "label": "SPI MOSI", "group": "wiring"},
    {"key": "spi_miso", "type": "choice", "choices": PIN_CHOICES, "default": "off", "label": "SPI MISO", "group": "wiring"},
    {"key": "spi_cs", "type": "choice", "choices": PIN_CHOICES, "default": "off", "label": "SPI chip select", "group": "wiring"},
    {"key": "pre_ms", "type": "number", "min": 0, "max": 1000, "default": 50, "label": "Before each wake", "unit": "ms",
     "hint": "How much before a wake is kept and decoded (in recordings)."},
    {"key": "post_ms", "type": "number", "min": 0, "max": 5000, "default": 100, "label": "After each wake", "unit": "ms",
     "hint": "How much after a wake is kept and decoded (in recordings)."},
]
EVENTS = [
    {"kind": "i2c", "label": "I2C", "draw": "spans", "color": "violet"},
    {"kind": "spi", "label": "SPI", "draw": "spans", "color": "blue"},
]
TOOLS = [
    {"name": "status", "description": "Is the analyzer there and streaming, its sample rate and wiring.", "hardware": False},
    {"name": "decode", "description": "Decode I2C / SPI in the last seconds of the analyzer's buffer.", "hardware": False},
]


class DecodeIn(BaseModel):
    seconds: float = Field(5.0, gt=0, le=30)
    post: bool = True  # send the transactions to the api (timeline, Live list)


class Analyzer:
    def __init__(self, scan=sigrok.scan, api_url: str = API_URL, driver: str = DRIVER, streamer_cls=Streamer,
                 run=None):
        self.scan_fn = scan
        self.api_url = api_url
        self.driver = driver
        self.streamer_cls = streamer_cls
        self.run_fn = run  # sigrok-cli runner for decoding (tests)
        self.values = {s["key"]: s["default"] for s in SETTINGS}
        self.devices: list[dict] = []
        self.error: str | None = "looking for the analyzer"
        self.settings_loaded = False
        self.last_scan: float | None = None
        self.streamer: Streamer | None = None
        self._decode_lock = threading.Lock()
        self._lock = threading.RLock()
        self._stop = threading.Event()

    # settings -------------------------------------------------------------------------------------------
    def apply(self, values: dict) -> None:
        known = {s["key"] for s in SETTINGS}
        new = {**self.values, **{k: v for k, v in values.items() if k in known}}
        dec.check_wiring(new)
        self.values = new

    def load_settings(self) -> bool:
        """The saved settings from the api (it keeps them in its database)."""
        try:
            r = httpx.get(f"{self.api_url}/api/modules/analyzer/settings", timeout=5)
            r.raise_for_status()
            self.apply(r.json())
        except (httpx.HTTPError, dec.DecodeError) as e:
            log.info("settings not loaded yet: %s", e)
            return False
        self.settings_loaded = True
        log.info("settings: %s", self.values)
        return True

    # the analyzer ---------------------------------------------------------------------------------------
    def rescan(self) -> None:
        if not self.values["enabled"]:
            self.devices, self.error = [], "switched off"
            return
        try:
            self.devices = self.scan_fn(self.driver)
            self.error = None if self.devices else "no analyzer found: is it plugged in? (udev rule installed?)"
        except sigrok.SigrokError as e:
            self.devices, self.error = [], str(e)
        self.last_scan = time.time()

    def check(self) -> None:
        """Keep the stream as the settings want it: off, or streaming from the analyzer at the chosen rate."""
        with self._lock:
            s = self.streamer
            want = self.values["enabled"]
            if s and (not want or s.rate != self.values["samplerate_hz"]):
                s.stop()
                self.streamer = s = None
            if not want:
                self.devices, self.error = [], "switched off"
                return
            if s is None:
                self.rescan()  # sigrok can't scan an analyzer that's streaming: only when there's no stream
                if self.devices:
                    dev = self.devices[0]
                    self.streamer = self.streamer_cls(self.driver, self.values["samplerate_hz"], dev.get("conn"))
                    self.streamer.start()
            elif not s.running and s.error:
                self.error = s.error

    def run(self) -> None:
        while not self._stop.is_set():
            if not self.settings_loaded:
                self.load_settings()
            self.check()
            self._stop.wait(CHECK_EVERY_S)

    def stop(self) -> None:
        self._stop.set()
        if self.streamer:
            self.streamer.stop()

    # decoding -------------------------------------------------------------------------------------------
    def decode_last(self, seconds: float) -> dict:
        s = self.streamer
        if s is None or not s.running:
            raise dec.DecodeError("the analyzer isn't streaming" + (f" ({self.error})" if self.error else ""))
        end_wall = s.timing.wall(s.ring.end)
        first, data, first_wall = s.window(end_wall - seconds, end_wall)
        if not data or first_wall is None:
            raise dec.DecodeError("nothing in the buffer yet")
        rate = s.rate
        with self._decode_lock:
            t = time.monotonic()
            buses = dec.decode(data, rate, self.values, **({"run": self.run_fn} if self.run_fn else {}))
            took = time.monotonic() - t
        wall = lambda n: first_wall + n / rate  # noqa: E731
        items = []
        for x in buses["i2c"]:
            items.append({"kind": "i2c", "t": wall(x["start"]), "end_t": wall(x["end"]),
                          "data": {"label": dec.i2c_label(x), "addr": x["addr"], "parts": x["parts"], "nack": x["nack"]}})
        for x in buses["spi"]:
            n = max(len(x["mosi"]), len(x["miso"]))
            items.append({"kind": "spi", "t": wall(x["start"]), "end_t": wall(x["end"]),
                          "data": {"label": f"SPI {n} B", "mosi": x["mosi"], "miso": x["miso"]}})
        items.sort(key=lambda i: i["t"])
        truncated = len(items) > MAX_ITEMS
        return {"from": first_wall, "to": first_wall + len(data) / rate, "samples": len(data), "rate": rate,
                "decode_s": round(took, 2), "items": items[:MAX_ITEMS], "found": len(items), "truncated": truncated}

    def post_events(self, items: list[dict]) -> dict | None:
        total = {"live": 0, "recorded": 0, "skipped": 0}
        for i in range(0, len(items), POST_BATCH):
            try:
                r = httpx.post(f"{self.api_url}/api/modules/analyzer/events", json={"items": items[i:i + POST_BATCH]},
                               timeout=20)
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("events not delivered: %s", e)
                return None
            for k, v in r.json().items():
                total[k] = total.get(k, 0) + v
        return total

    # what it tells the tracker --------------------------------------------------------------------------
    def health(self) -> dict:
        s = self.streamer
        dev = self.devices[0] if self.devices else None
        if s and s.running and dev:
            return {"ok": True, "detail": f"{dev['description']} ({self.driver}), streaming at {s.rate / 1e6:g} MHz, "
                                          f"last {s.status()['buffered_s']:g} s kept"}
        if dev and not (s and s.running):
            return {"ok": False, "detail": f"{dev['description']} found, not streaming: {(s and s.error) or 'starting'}"}
        return {"ok": False, "detail": self.error}

    def describe(self) -> dict:
        return {"name": "analyzer", "title": "Logic analyzer", "version": VERSION, "contract": 1,
                "description": "Decodes I2C and SPI on the board's pins next to the current (sigrok).",
                "health": self.health(), "settings": SETTINGS, "events": EVENTS, "tools": TOOLS,
                "instances": [{"id": "1", "backend": "sigrok", "driver": self.driver, "device": d} for d in self.devices[:1]]}


def create_app(analyzer: Analyzer | None = None, background: bool = True) -> FastAPI:
    a = analyzer or Analyzer()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if background:
            threading.Thread(target=a.run, name="analyzer", daemon=True).start()
        yield
        a.stop()

    app = FastAPI(title="inspector-watt analyzer", lifespan=lifespan)
    app.state.analyzer = a

    @app.get("/module")
    def module():
        return a.describe()

    @app.put("/settings")
    def put_settings(values: dict):
        try:
            a.apply(values)
        except dec.DecodeError as e:
            raise HTTPException(422, str(e)) from e
        a.settings_loaded = True
        a.check()
        return a.values

    @app.get("/status")
    def status():
        return {**a.health(), "values": a.values, "devices": a.devices, "last_scan": a.last_scan,
                "stream": a.streamer.status() if a.streamer else None,
                "buffer_s": buffer_seconds(a.values["samplerate_hz"])}

    @app.post("/decode")
    def decode_now(body: DecodeIn):
        """Decode the last seconds of the buffer; with post, also onto the timeline (Live list, recording)."""
        try:
            r = a.decode_last(body.seconds)
        except dec.DecodeError as e:
            raise HTTPException(409, str(e)) from e
        r["posted"] = a.post_events(r["items"]) if body.post else None
        return r

    return app


app = create_app()
