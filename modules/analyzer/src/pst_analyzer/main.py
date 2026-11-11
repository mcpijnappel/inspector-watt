"""The logic analyzer module's main container (issue #12).

It describes itself to the tracker (GET /module: settings, event types, MCP tools, health), takes its settings
from the api (saved there; read again at start), and keeps looking for the analyzer. Capturing around wakes and
decoding I2C / SPI come in the next step; then decoded transactions go to the api as events
(POST /api/modules/analyzer/events) and onto the recording's timeline.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from . import sigrok

log = logging.getLogger("analyzer")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

API_URL = os.environ.get("API_URL", "http://api:8000")
DRIVER = os.environ.get("ANALYZER_DRIVER", "fx2lafw")
SCAN_EVERY_S = 10.0
VERSION = "0.1.0"

SETTINGS = [
    {"key": "enabled", "type": "bool", "default": True, "label": "On",
     "hint": "Off: the analyzer is left alone (no streaming, no decoding)."},
    # fx2lafw's rates from 1 MHz up; it streams up to 24 MHz on 8 channels if the computer keeps up (USB 2)
    {"key": "samplerate_hz", "type": "choice",
     "choices": [1_000_000, 2_000_000, 4_000_000, 8_000_000, 12_000_000, 16_000_000, 24_000_000], "default": 4_000_000,
     "label": "Sample rate", "unit": "Hz",
     "hint": "4 MHz covers I2C at 400 kHz and SPI to about 1 MHz. Up to 24 MHz for faster SPI: it may drop samples "
             "on a busy machine and fills the buffer 6x faster (about 24 MB per second)."},
    {"key": "pre_ms", "type": "number", "min": 0, "max": 1000, "default": 50, "label": "Before each wake", "unit": "ms",
     "hint": "How much before a wake is kept and decoded."},
    {"key": "post_ms", "type": "number", "min": 0, "max": 5000, "default": 100, "label": "After each wake", "unit": "ms",
     "hint": "How much after a wake is kept and decoded."},
]
EVENTS = [
    {"kind": "i2c", "label": "I2C", "draw": "spans", "color": "violet"},
    {"kind": "spi", "label": "SPI", "draw": "spans", "color": "blue"},
]
TOOLS = [
    {"name": "status", "description": "Is the analyzer there, its sample rate and channels.", "hardware": False},
]


class Analyzer:
    def __init__(self, scan=sigrok.scan, api_url: str = API_URL, driver: str = DRIVER):
        self.scan_fn = scan
        self.api_url = api_url
        self.driver = driver
        self.values = {s["key"]: s["default"] for s in SETTINGS}
        self.devices: list[dict] = []
        self.error: str | None = "looking for the analyzer"
        self.settings_loaded = False
        self.last_scan: float | None = None
        self._stop = threading.Event()

    # settings -------------------------------------------------------------------------------------------
    def apply(self, values: dict) -> None:
        known = {s["key"] for s in SETTINGS}
        self.values.update({k: v for k, v in values.items() if k in known})

    def load_settings(self) -> bool:
        """The saved settings from the api (it keeps them in its database)."""
        try:
            r = httpx.get(f"{self.api_url}/api/modules/analyzer/settings", timeout=5)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.info("settings not loaded yet: %s", e)
            return False
        self.apply(r.json())
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

    def run(self) -> None:
        while not self._stop.is_set():
            if not self.settings_loaded:
                self.load_settings()
            self.rescan()
            self._stop.wait(SCAN_EVERY_S)

    def stop(self) -> None:
        self._stop.set()

    # what it tells the tracker --------------------------------------------------------------------------
    def health(self) -> dict:
        dev = self.devices[0] if self.devices else None
        detail = (f"{dev['description']} ({self.driver}{', ' + dev['conn'] if dev.get('conn') else ''}), "
                  f"{len(dev['channels'])} channels" if dev else self.error)
        return {"ok": dev is not None, "detail": detail}

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
        a.apply(values)
        a.settings_loaded = True
        a.rescan()
        return a.values

    @app.get("/status")
    def status():
        return {**a.describe()["health"], "values": a.values, "devices": a.devices, "last_scan": a.last_scan}

    @app.post("/scan")
    def scan_now():
        a.rescan()
        return {"devices": a.devices, "error": a.error}

    return app


app = create_app()
