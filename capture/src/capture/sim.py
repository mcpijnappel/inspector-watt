"""Simulated PPK2 and UART (port "sim://") for development without hardware.

The fake sensor sleeps at ~3 uA and wakes every `wake_every_s` for `wake_s` (~4 mA, with a 20 mA radio burst),
printing debug lines over the fake UART.
"""

from __future__ import annotations

import struct
import threading
import time

import numpy as np

from .ppk2 import SAMPLE_RATE_HZ, Calibration, Cmd, encode_samples

SIM_METADATA = (
    "Calibrated: 0\nR0: 1031.64\nR1: 101.65\nR2: 10.15\nR3: 0.94\nR4: 0.043\n"
    + "".join(f"GS{i}: 0\nGI{i}: 1\nO{i}: 0\nS{i}: 0\nI{i}: 0\nUG{i}: 1\n" for i in range(5))
    + "VDD: 3000\nHW: 0\nmode: 2\nIA: 0\nEND\n"
)

WAKE_EVERY_S = 10.0
WAKE_S = 0.5


def sim_current(t: np.ndarray, dut_on: bool, rng: np.random.Generator) -> np.ndarray:
    """Current in amps at stream time t (seconds)."""
    if not dut_on:
        return np.abs(rng.normal(0, 50e-9, len(t)))
    phase = np.mod(t, WAKE_EVERY_S)
    i = 3e-6 + rng.normal(0, 0.2e-6, len(t))
    awake = phase < WAKE_S
    i[awake] = 4e-3 + rng.normal(0, 0.1e-3, int(awake.sum()))
    radio = (phase > 0.2) & (phase < 0.25)
    i[radio] = 20e-3 + rng.normal(0, 0.5e-3, int(radio.sum()))
    return np.abs(i)


class SimPPK2Serial:
    def __init__(self, timeout: float = 0.05):
        self.timeout = timeout
        self._out = bytearray()
        self._streaming = False
        self._dut_on = False
        self._t_ref = 0.0
        self._produced = 0
        self._rng = np.random.default_rng(1)
        self._cal = Calibration.parse(SIM_METADATA)
        self.stalled = False  # tests: pretend the firmware hung (no data, no error)

    def write(self, data: bytes) -> int:
        cmd = data[0]
        if cmd == Cmd.GET_META_DATA:
            self._out += SIM_METADATA.encode()
        elif cmd == Cmd.AVERAGE_START:
            self._streaming = True
            self._t_ref = time.monotonic()
            self._produced = 0
        elif cmd == Cmd.AVERAGE_STOP:
            self._streaming = False
        elif cmd == Cmd.DEVICE_RUNNING_SET:
            self._dut_on = bool(data[1])
        return len(data)

    def _generate(self) -> None:
        due = int((time.monotonic() - self._t_ref) * SAMPLE_RATE_HZ) - self._produced
        if due <= 0:
            return
        t = (np.arange(due) + self._produced) / SAMPLE_RATE_HZ
        cur = sim_current(t, self._dut_on, self._rng)
        self._out += encode_samples(cur, self._produced, cal=self._cal)
        self._produced += due

    def read(self, size: int = 1) -> bytes:
        end = time.monotonic() + self.timeout
        while True:
            if self._streaming and not self.stalled:
                self._generate()
            if len(self._out) >= size or time.monotonic() >= end:
                break
            time.sleep(0.005)
        data = bytes(self._out[:size])
        del self._out[:size]
        return data

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        pass


class SimUartSerial:
    def __init__(self, baudrate: int = 115200, timeout: float = 0.05):
        self.baudrate = baudrate
        self.timeout = timeout
        self._t0 = time.monotonic()
        self._next = 0.0
        self._n = 0
        self._pending = b""

    @property
    def in_waiting(self) -> int:
        return len(self._pending)

    def read(self, size: int = 1) -> bytes:
        if not self._pending:
            time.sleep(self.timeout)
            t = time.monotonic() - self._t0
            if t < self._next:
                return b""
            self._n += 1
            self._next = t + 2.0
            self._pending = f"[sim] t={t:.2f}s tick {self._n} vbat=3.7V\r\n".encode()
        data, self._pending = self._pending[:size], self._pending[size:]
        return data

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        pass


SIM_MENU = (
    "USB connected\r\n"
    "[I] current firmware version: sim-1.0\r\n"
    "\r\n=== BOOT DIAGNOSTICS ===\r\nFirmware  : sim-1.0\r\nFree heap : 200000 bytes\r\n========================\r\n\r\n"
)
SIM_MENU_KEYS = (
    "--- DEBUG MENU ---\r\n"
    "  [n]  Continue normally\r\n"
    "  [r]  Reboot\r\n"
    "  [c]  Clear all NVS + reboot (factory reset)\r\n"
    "  [f]  Clear storage + continue\r\n"
    "------------------\r\n"
    "Waiting for input...\r\n"
)


# The simulated chip's partition table (Arduino's default layout): name, type, subtype, offset, size.
SIM_PARTITIONS = [("nvs", 1, 0x02, 0x9000, 0x5000), ("otadata", 1, 0x00, 0xE000, 0x2000),
                  ("app0", 0, 0x10, 0x10000, 0x140000), ("app1", 0, 0x11, 0x150000, 0x140000),
                  ("spiffs", 1, 0x82, 0x290000, 0x160000)]


def partition_table(parts) -> bytes:
    """ESP partition table bytes (32-byte entries, then erased flash)."""
    out = b"".join(b"\xaa\x50" + struct.pack("<BBII", t, st, off, size) + name.encode().ljust(16, b"\0") + b"\0" * 4
                   for name, t, st, off, size in parts)
    return out + b"\xff" * (0xC00 - len(out))


class SimUsbDevice:
    """The board's USB port in the simulator. `present` is False while it 'sleeps' (tests set it); the tracker's
    power-cycle wakes it, like a real boot."""

    def __init__(self):
        self.present = True
        self.flashed = 0  # simulated flashes (tests)
        self.mac = "9c:9e:6e:00:00:01"  # what simulated esptool reads from the chip
        self.flash: dict[int, bytes] = {0x8000: partition_table(SIM_PARTITIONS)}  # by offset, what's on the chip
        # as an RP2040/RP2350 board (rp2 uploads): in BOOTSEL mode (boot loader) or not, and the last UF2 loaded
        self.rp_chip = "rp2040"
        self.bootsel = False
        self.rp_flash: bytes | None = None

    def open(self):
        if not self.present:
            raise OSError("sim USB: no such device (asleep)")
        return SimUsbSerial()

    def wake(self) -> None:
        self.present = True

    def enter_bootsel(self) -> None:
        """The 1200-baud touch: the serial port goes away, the boot loader shows up."""
        self.present = False
        self.bootsel = True

    def leave_bootsel(self) -> None:
        """picotool --execute: the new firmware runs, with its serial port."""
        self.bootsel = False
        self.present = True


class SimUsbSerial:
    """Boots into the debug menu when opened (the firmware sees the open port), then answers keys like the real one."""

    def __init__(self, timeout: float = 0.05):
        self.timeout = timeout
        self._pending = (SIM_MENU + SIM_MENU_KEYS).encode()
        self._running = False
        self._next_tick = 0.0
        self._lock = threading.Lock()  # read and write come from different threads

    @property
    def in_waiting(self) -> int:
        return len(self._pending)

    def read(self, size: int = 1) -> bytes:
        if not self._pending:
            time.sleep(self.timeout)
        with self._lock:
            if not self._pending and self._running and time.monotonic() >= self._next_tick:
                self._next_tick = time.monotonic() + 2.0
                self._pending += b"[I] Deep Sleep blocked: USB Serial connected.\r\n"
            data, self._pending = self._pending[:size], self._pending[size:]
        return data

    def write(self, data: bytes) -> int:
        with self._lock:
            return self._write(data)

    def _write(self, data: bytes) -> int:
        key = data.decode(errors="replace").strip()[:1].lower()
        if self._running:
            self._pending += f"[I] sim got: {data.decode(errors='replace').strip()}\r\n".encode()
        elif key == "n":
            self._pending += b"Continuing normally.\r\n"
            self._running = True
        elif key == "r":
            self._pending += b"Rebooting...\r\n" + (SIM_MENU + SIM_MENU_KEYS).encode()
        elif key in ("c", "f"):
            self._pending += b"Done.\r\n" + SIM_MENU_KEYS.encode()
        else:
            self._pending += b"Unknown key. Please choose from the menu.\r\n" + SIM_MENU_KEYS.encode()
        return len(data)

    def close(self) -> None:
        pass
