"""The board's own USB serial port (ESP32 USB-Serial/JTAG), next to the PPK2 and the debug UART.

It is connected through an adapter that blocks the 5 V, so the PPK2 keeps powering the board. The port is kept
closed unless someone asks for it: the firmware counts USB as connected only while the host has the port open and
reads it. Closed, the firmware behaves as if nothing is plugged in (no debug menu, deep sleep allowed). Open, it
shows its debug menu at boot and doesn't deep-sleep, and the port vanishes whenever the board sleeps or resets.

Opening keeps DTR and RTS as the kernel sets them (both on). Clearing them one after the other passes through
RTS on + DTR off, which resets the chip.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

import serial

from .uart import LineLog, LineSplitter, decode_line

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 0.05
POLL_S = 0.1  # how often to look for the port while it's wanted but missing (the boot menu waits only ~5 s)
BAUD = 115200  # ignored by USB-Serial/JTAG

# A menu: a title between dashes ("--- DEBUG MENU ---"), then option lines like "  [n]  Continue normally".
MENU_TITLE = re.compile(r"^\s*-{2,}\s*([^-\s].*?)\s*-{2,}\s*$")
MENU_OPTION = re.compile(r"^\s*\[(\S{1,8})\]\s+(.+?)\s*$")
MENU_SEPARATOR = re.compile(r"^\s*-*\s*$")


class UsbError(RuntimeError):
    pass


class MenuParser:
    """Finds a key menu in the output, so a UI can show it as buttons. It stays up until a key is sent."""

    def __init__(self):
        self.menu: dict | None = None
        self._building: dict | None = None

    def feed(self, text: str, wall: float) -> None:
        m = MENU_TITLE.match(text)
        if m:
            self._building = {"title": m.group(1), "options": [], "at": wall}
            return
        if self._building is None:
            return
        opt = MENU_OPTION.match(text)
        if opt:
            self._building["options"].append({"key": opt.group(1), "label": opt.group(2)})
        elif MENU_SEPARATOR.match(text):
            pass
        else:  # first other line ("Waiting for input..."): the menu is complete
            if self._building["options"]:
                self.menu = {**self._building, "prompt": text.strip()}
            self._building = None

    def clear(self) -> None:
        self.menu = None
        self._building = None


class UsbManager:
    def __init__(self, port: str, sim_factory=None):
        self.port = port
        self.sim_factory = sim_factory  # () -> a fake serial port, for "sim://"
        self.log = LineLog(maxlen=5000)
        self.menu = MenuParser()
        self.wanted = False  # someone has the console open
        self.held = False  # flashing: the port belongs to esptool, never open it here
        self.connected = False
        self.error: str | None = None
        self.opened_at: float | None = None
        self._ser = None
        self._lock = threading.Lock()
        self._splitter = LineSplitter(byte_time_s=0.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="usb", daemon=True)

    # lifecycle -------------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._close()

    def present(self) -> bool:
        """The port exists: the board is powered and awake (it disappears in deep sleep)."""
        if self.port.startswith("sim://"):
            return self.sim_factory is not None and self.sim_factory.present
        return os.path.exists(self.port)

    def _open(self):
        if self.port.startswith("sim://"):
            return self.sim_factory.open()
        return serial.Serial(self.port, BAUD, timeout=READ_TIMEOUT_S, exclusive=True)

    def _close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None
            self.connected = False
            self.opened_at = None
        self.menu.clear()

    def _emit(self, lines) -> None:
        for _mono, wall, raw in lines:
            text = decode_line(raw)
            self.log.add(wall, _mono, None, text)
            self.menu.feed(text, wall)

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._ser is None:
                if not self.wanted or self.held or not self.present():
                    self._stop.wait(POLL_S)
                    continue
                try:
                    ser = self._open()
                    with self._lock:
                        self._ser = ser
                        self.connected = True
                        self.opened_at = time.time()
                        self.error = None
                    log.info("Board USB opened on %s", self.port)
                except Exception as e:  # vanished again, busy, no permission, ...
                    if self.error != str(e):
                        log.warning("Board USB not available: %s", e)
                    self.error = str(e)
                    self._stop.wait(POLL_S)
                continue
            if not self.wanted or self.held:
                self._close()
                log.info("Board USB closed")
                continue
            try:
                data = self._ser.read(1)
                if data and self._ser.in_waiting:
                    data += self._ser.read(self._ser.in_waiting)
            except (serial.SerialException, OSError) as e:
                # Normal: the board went to sleep or reset. Reopened as soon as it's back.
                log.info("Board USB gone: %s", e)
                self._close()
                continue
            now = time.monotonic()
            if data:
                self._emit(self._splitter.feed(data, now, time.time()))
            self._emit(self._splitter.flush_stale(now))

    # commands --------------------------------------------------------------------------------------------
    def set_wanted(self, on: bool) -> None:
        self.wanted = on

    def hold(self) -> None:
        """For flashing: close the port and keep it closed until release()."""
        self.held = True
        deadline = time.monotonic() + 2.0
        while self._ser is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        if self._ser is not None:
            self._close()

    def release(self) -> None:
        self.held = False

    def send(self, text: str, eol: str = "\n") -> int:
        with self._lock:
            if self._ser is None:
                raise UsbError("Board USB is not open" + (" (flashing)" if self.held else ""))
            n = self._ser.write((text + eol).encode())
        self.menu.clear()
        return n

    def status(self) -> dict:
        return {
            "port": self.port,
            "present": self.present(),
            "wanted": self.wanted,
            "connected": self.connected,
            "held": self.held,
            "error": self.error,
            "opened_at": self.opened_at,
            "lines": self.log.last_seq + 1,
            "menu": self.menu.menu,
        }
