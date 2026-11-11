"""Device owners: one reader thread per device, reconnecting automatically when a device is replugged."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

import serial

from .clocksync import ClockSync
from .config import Config
from .ppk2 import SAMPLE_RATE_HZ, VDD_MAX_MV, VDD_MIN_MV, Calibration, Decoder, PowerMode, PPK2Link
from .ringbuffer import RingBuffer
from .sim import SimPPK2Serial, SimUartSerial, SimUsbDevice
from .uart import LineLog, LineSplitter, decode_line
from .usb import UsbManager

log = logging.getLogger(__name__)

READ_SIZE = 65536
DEFAULT_UART_BAUD = 115200  # the UART's speed until the board on the bench sets its own
READ_TIMEOUT_S = 0.05
RETRY_S = 1.0
STALL_S = 2.0  # streaming but no data for this long: the PPK2 hung; reconnect


class DeviceError(RuntimeError):
    pass


def keep_dtr_on_close(ser) -> None:
    """Don't drop DTR when the port closes (clear HUPCL).

    Measured on the bench: when DTR drops after the port has been open for a while (e.g. this service
    restarting), the PPK2 sometimes switches its output off, so the board loses power and cold-boots.
    With DTR kept high the output stayed on in every trial. The setting sticks to the tty, so it also
    covers the close that happens when the process exits.
    """
    try:
        import termios
        attrs = termios.tcgetattr(ser.fd)
        attrs[2] &= ~termios.HUPCL
        termios.tcsetattr(ser.fd, termios.TCSANOW, attrs)
    except (ImportError, OSError, AttributeError) as e:
        log.warning("could not keep DTR high on close: %s", e)


@dataclass
class StreamStats:
    samples: int = 0
    lost: int = 0
    gaps: int = 0
    corrupt: int = 0
    dropped_bytes: int = 0
    reconnects: int = 0  # times the stream resumed after the PPK2 was replugged or stalled
    stalls: int = 0  # times the PPK2 stopped sending data while still connected
    bytes_read: int = 0
    decode_s: float = 0.0
    started_mono: float | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "started_mono"}


class RateMeter:
    """Samples per second over the last few seconds of host time."""

    def __init__(self, window_s: float = 5.0):
        self.window_s = window_s
        self._pts: deque[tuple[float, int]] = deque()

    def add(self, t: float, total: int) -> None:
        self._pts.append((t, total))
        while self._pts and t - self._pts[0][0] > self.window_s:
            self._pts.popleft()

    def rate(self) -> float | None:
        if len(self._pts) < 2:
            return None
        (t0, n0), (t1, n1) = self._pts[0], self._pts[-1]
        return (n1 - n0) / (t1 - t0) if t1 > t0 else None

    def reset(self) -> None:
        self._pts.clear()


class PPK2Manager:
    def __init__(self, cfg: Config, ring: RingBuffer, clock: ClockSync):
        self.cfg = cfg
        self.ring = ring
        self.clock = clock
        self._ser = None
        self._link: PPK2Link | None = None
        self._cmd_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ppk2", daemon=True)
        self.decoder = Decoder()
        self.stats = StreamStats()
        self.rate = RateMeter()
        self.calibration: Calibration | None = None
        self.connected = False
        self.streaming = False
        self.auto_stream = cfg.auto_stream  # start measuring when the PPK2 connects (a setting)
        self.hard_max_mv = cfg.max_voltage_mv  # the tracker's voltage limit (a setting; never above VDD_MAX_MV)
        self._want_stream = cfg.auto_stream  # measure whenever connected; resumed automatically after a replug
        self.on_resume = None  # callback(gap_start_idx, gap_samples, reason) after the stream resumed
        self._last_data = 0.0
        self._drop_reason = "ppk2_reconnect"
        self.error: str | None = None
        # Device state as far as we know it. Voltage/mode start from what the device reports in its metadata.
        self.mode: PowerMode | None = None
        self.voltage_mv: int | None = None
        self.voltage_set_by_us = False
        self.dut_power: bool | None = None  # unknown until we set it
        self.dut_changed: dict | None = None  # last board power change: {"on", "by": user|session|idle|limit, "at": unix time}
        self.device: dict | None = None  # the device on the bench: {"id", "name", "max_mv"} (its voltage limit)

    # lifecycle -------------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        if self._link:
            try:
                self._link.stop()
            except Exception:
                pass
        self._close()

    def _open(self):
        if self.cfg.ppk2_port.startswith("sim://"):
            return SimPPK2Serial(timeout=READ_TIMEOUT_S)
        ser = serial.Serial(self.cfg.ppk2_port, timeout=READ_TIMEOUT_S, exclusive=True)
        keep_dtr_on_close(ser)
        return ser

    def _connect(self) -> None:
        ser = self._open()
        link = PPK2Link(ser)
        try:
            link.drain()
            cal = Calibration.parse(link.read_metadata())
        except Exception:
            ser.close()
            raise
        with self._cmd_lock:
            self._ser, self._link = ser, link
            self.calibration = cal
            self.mode = cal.mode
            self.voltage_mv = cal.vdd_mv
            self.voltage_set_by_us = False
            self.decoder = Decoder(cal, vdd_mv=cal.vdd_mv or 3000, spike_filter=self.decoder.spike_filter)
            self.connected = True
            self.streaming = False
            self.error = None
        log.info("PPK2 connected on %s (mode=%s, vdd=%s mV)", self.cfg.ppk2_port, cal.mode, cal.vdd_mv)

    def _close(self) -> None:
        with self._cmd_lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = self._link = None
            self.connected = False
            self.streaming = False
            self.dut_power = None  # the PPK2 resets when replugged

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._ser is None:
                try:
                    self._connect()
                    if self._want_stream and self.stats.started_mono is None:
                        self.start_stream()  # first connect: a fresh stream
                    elif self._want_stream:
                        gap = self._resume_stream()
                        if self.on_resume:
                            self.on_resume(*gap, self._drop_reason)
                        self._drop_reason = "ppk2_reconnect"
                except Exception as e:  # device missing, busy, no permission, ...
                    if self.error != str(e):
                        log.warning("PPK2 not available: %s", e)
                    self.error = str(e)
                    self._stop.wait(RETRY_S)
                continue
            try:
                data = self._ser.read(READ_SIZE)
            except (serial.SerialException, OSError) as e:
                log.warning("PPK2 disconnected: %s", e)
                self.error = f"disconnected: {e}"
                self._close()
                continue
            now = time.monotonic()
            if data and self.streaming:
                self._last_data = now
                self._handle(data, now)
            elif self.streaming and now - self._last_data > STALL_S:
                log.error("PPK2 sent no data for %.1f s while streaming; reconnecting", now - self._last_data)
                self.stats.stalls += 1
                self.error = "stream stalled"
                self._drop_reason = "ppk2_stall"
                self._close()

    def _handle(self, data: bytes, arrival: float) -> None:
        t0 = time.perf_counter()
        blk = self.decoder.feed(data)
        self.ring.append(blk.current, blk.logic)
        self.clock.observe(arrival, self.ring.end)
        s = self.stats
        s.decode_s += time.perf_counter() - t0
        s.bytes_read += len(data)
        s.samples += len(blk.current)
        s.lost += blk.lost
        s.gaps += blk.gaps
        s.corrupt += blk.corrupt
        s.dropped_bytes += blk.dropped_bytes
        self.rate.add(arrival, s.samples)
        if blk.gaps:
            log.warning("PPK2: %d samples lost in %d gap(s)", blk.lost, blk.gaps)

    # commands --------------------------------------------------------------------------------------------
    def _require_link(self) -> PPK2Link:
        if self._link is None:
            raise DeviceError(f"PPK2 not connected ({self.error or 'connecting'})")
        return self._link

    def start_stream(self) -> None:
        with self._cmd_lock:
            link = self._require_link()
            if self.streaming:
                return
            self.decoder.reset()
            self.ring.reset()
            self.clock.reset()
            self.stats = StreamStats(started_mono=time.monotonic())
            self.rate.reset()
            self.streaming = True
            self._want_stream = True
            self._last_data = time.monotonic()
            link.start()

    def _resume_stream(self) -> tuple[int, int]:
        """Continue the same timeline after a replug: the downtime becomes a NaN gap, so sample index stays time."""
        with self._cmd_lock:
            link = self._require_link()
            now_idx = self.clock.index_at(time.monotonic())
            gap_start = self.ring.end
            gap = max(0, now_idx - gap_start) if now_idx is not None else 0
            self.ring.skip(gap)
            self.decoder.reset()
            self.rate.reset()
            s = self.stats
            s.lost += gap
            s.gaps += 1
            s.reconnects += 1
            self.streaming = True
            self._last_data = time.monotonic()
            link.start()
        log.warning("PPK2 stream resumed after reconnect; %.2f s gap", gap / SAMPLE_RATE_HZ)
        return gap_start, gap

    def stop_stream(self) -> None:
        with self._cmd_lock:
            self._want_stream = False
            link = self._require_link()
            link.stop()
            self.streaming = False

    @property
    def max_mv(self) -> int:
        """Highest voltage allowed now: the device's limit, never above the tracker's limit."""
        dev = (self.device or {}).get("max_mv")
        return min(self.hard_max_mv, dev) if dev else self.hard_max_mv

    def _check_mv(self, mv: int) -> None:
        if not VDD_MIN_MV <= mv <= self.max_mv:
            who = f"the limit of {self.device['name']}" if self.device and self.max_mv < self.hard_max_mv else "the tracker's limit"
            raise DeviceError(f"voltage must be {VDD_MIN_MV}-{self.max_mv} mV ({who})")

    def set_device(self, device: dict | None) -> None:
        """A device (and so a voltage limit) was picked. If the board is powered above the new limit, it goes off."""
        self.device = device
        self._enforce_limit()

    def set_hard_max(self, mv: int) -> None:
        """The tracker's own voltage limit changed (Settings). If the board is powered above it, it goes off."""
        if not VDD_MIN_MV <= mv <= VDD_MAX_MV:
            raise DeviceError(f"the limit must be {VDD_MIN_MV}-{VDD_MAX_MV} mV")
        self.hard_max_mv = int(mv)
        self._enforce_limit()

    def _enforce_limit(self) -> None:
        if self.dut_power and self.voltage_mv is not None and self.voltage_mv > self.max_mv:
            log.warning("Board at %d mV is above the new limit (%d mV): switching it off", self.voltage_mv, self.max_mv)
            try:
                self.set_dut_power(False, by="limit")
            except DeviceError:
                pass

    def set_source_voltage(self, mv: int) -> None:
        self._check_mv(mv)
        with self._cmd_lock:
            link = self._require_link()
            # Program the voltage before switching to source mode, so the output never uses an old value.
            link.set_voltage(mv)
            link.set_power_mode(PowerMode.SOURCE)
            self.mode = PowerMode.SOURCE
            self.voltage_mv = mv
            self.voltage_set_by_us = True
            self.decoder.vdd_mv = mv

    def adjust_voltage(self, mv: int) -> None:
        """Change the output voltage while the source is already on (battery emulation): only the regulator
        command is sent, so the output moves to the new voltage without switching off."""
        self._check_mv(mv)
        with self._cmd_lock:
            link = self._require_link()
            if self.mode != PowerMode.SOURCE or not self.voltage_set_by_us:
                raise DeviceError("the source is not set up by this service")
            link.set_voltage(mv)
            self.voltage_mv = mv
            self.decoder.vdd_mv = mv

    def adopt_source_state(self, mv: int, dut_power: bool) -> None:
        """Take over a state the PPK2 already has (after a restart) without sending commands to it."""
        with self._cmd_lock:
            self.mode = PowerMode.SOURCE
            self.voltage_mv = mv
            self.voltage_set_by_us = True
            self.decoder.vdd_mv = mv
            self.dut_power = dut_power

    def set_dut_power(self, on: bool, by: str = "user") -> None:
        with self._cmd_lock:
            link = self._require_link()
            if on and not self.voltage_set_by_us:
                raise DeviceError("set the source voltage first, so the board never powers up at an unexpected voltage")
            link.set_dut_power(on)
            self.dut_power = on
            self.dut_changed = {"on": on, "by": by, "at": time.time()}

    def set_spike_filter(self, on: bool) -> None:
        self.decoder.spike_filter = on

    def status(self) -> dict:
        s = self.stats
        elapsed = time.monotonic() - s.started_mono if s.started_mono else None
        return {
            "port": self.cfg.ppk2_port,
            "connected": self.connected,
            "streaming": self.streaming,
            "error": self.error,
            "mode": self.mode.name.lower() if self.mode else None,
            "voltage_mv": self.voltage_mv,
            "voltage_set_by_us": self.voltage_set_by_us,
            "dut_power": self.dut_power,
            "dut_changed": self.dut_changed,
            "spike_filter": self.decoder.spike_filter,
            "max_voltage_mv": self.max_mv,  # effective: the device's limit, capped by the tracker's limit
            "hard_max_mv": self.hard_max_mv,
            "device": self.device,
            "stream": {
                **s.to_dict(),
                "elapsed_s": elapsed,
                "rate_sps": self.rate.rate(),
                "nominal_rate_sps": SAMPLE_RATE_HZ,
                # share of one CPU core spent decoding (the hot path)
                "decode_load": (s.decode_s / elapsed) if elapsed else None,
                "clock_drift_ppm": self.clock.drift_ppm,
            },
        }


class UartManager:
    def __init__(self, cfg: Config, clock: ClockSync, ppk2: PPK2Manager):
        self.cfg = cfg
        self.clock = clock
        self.ppk2 = ppk2
        self.port = cfg.uart_port
        self.baud = DEFAULT_UART_BAUD  # until the board on the bench says (CaptureService.set_device)
        self.log = LineLog()
        self.connected = False
        self.error: str | None = None
        self.bytes_read = 0
        self._ser = None
        self._lock = threading.Lock()
        self._splitter = LineSplitter(byte_time_s=10 / self.baud)  # 8N1 = 10 bits per byte
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="uart", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._close()

    def _open(self):
        if self.port.startswith("sim://"):
            return SimUartSerial(self.baud, timeout=READ_TIMEOUT_S)
        return serial.Serial(self.port, self.baud, timeout=READ_TIMEOUT_S, exclusive=True)

    def _close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None
            self.connected = False

    def _emit(self, lines) -> None:
        for mono, wall, raw in lines:
            idx = self.clock.index_at(mono) if self.ppk2.streaming else None
            self.log.add(wall, mono, idx, decode_line(raw))

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._ser is None:
                try:
                    ser = self._open()
                    with self._lock:
                        self._ser = ser
                        self.connected = True
                        self.error = None
                    log.info("UART connected on %s @ %d baud", self.port, self.baud)
                except Exception as e:
                    if self.error != str(e):
                        log.warning("UART not available: %s", e)
                    self.error = str(e)
                    self._stop.wait(RETRY_S)
                continue
            try:
                # Block for the first byte only, then take whatever else is waiting: returns right after data
                # arrives instead of after the full timeout, which keeps timestamps tight.
                data = self._ser.read(1)
                if data and self._ser.in_waiting:
                    data += self._ser.read(self._ser.in_waiting)
            except (serial.SerialException, OSError) as e:
                log.warning("UART disconnected: %s", e)
                self.error = f"disconnected: {e}"
                self._close()
                continue
            now = time.monotonic()
            if data:
                self.bytes_read += len(data)
                self._emit(self._splitter.feed(data, now, time.time()))
            self._emit(self._splitter.flush_stale(now))

    def set_baud(self, baud: int) -> None:
        if not 300 <= baud <= 12_000_000:
            raise DeviceError("baud rate out of range")
        with self._lock:
            self.baud = baud
            self._splitter.byte_time_s = 10 / baud
            if self._ser is not None:
                self._ser.baudrate = baud

    def send(self, text: str, eol: str = "\n") -> int:
        with self._lock:
            if self._ser is None:
                raise DeviceError(f"UART not connected ({self.error or 'connecting'})")
            return self._ser.write((text + eol).encode())

    def status(self) -> dict:
        return {
            "port": self.port,
            "connected": self.connected,
            "error": self.error,
            "baud": self.baud,
            "bytes_read": self.bytes_read,
            "lines": self.log.last_seq + 1,
        }


class CaptureService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ring = RingBuffer(cfg.ring_seconds * SAMPLE_RATE_HZ)
        self.clock = ClockSync()
        self.ppk2 = PPK2Manager(cfg, self.ring, self.clock)
        self.uart = UartManager(cfg, self.clock, self.ppk2)
        self.usb = UsbManager(cfg.usb_port, SimUsbDevice() if cfg.usb_port.startswith("sim://") else None)

        self.idle = IdlePowerOff(self.ppk2, cfg.idle_power_off_s)
        # Firmware uploads to a sleeping board: wait for its next wake (power-cycle after wake_wait_s), always wait
        # (never power-cycle), or power-cycle right away.
        self.flash_when_asleep = "wait"
        self.flash_wake_wait_s = 90.0

    def set_device(self, device: dict | None) -> None:
        """The board on the bench: its voltage limit (PPK2) and the speed its debug UART runs at."""
        self.ppk2.set_device({k: device[k] for k in ("id", "name", "max_mv")} if device and device.get("max_mv") else None)
        if device and device.get("uart_baud") and device["uart_baud"] != self.uart.baud:
            self.uart.set_baud(int(device["uart_baud"]))
            log.info("UART at %d baud for '%s'", self.uart.baud, device.get("name"))

    def settings(self) -> dict:
        """Settings: the ones that can change while running, then the ones from the environment (.env).
        (min_free_gb and raw_mantissa_bits: the session manager's, see SessionManager.settings.)"""
        return {
            "idle_power_off_s": self.idle.after_s,
            "spike_filter": self.ppk2.decoder.spike_filter,
            "flash_when_asleep": self.flash_when_asleep,
            "flash_wake_wait_s": self.flash_wake_wait_s,
            "auto_stream": self.ppk2.auto_stream,
            "max_voltage_mv": self.ppk2.hard_max_mv,
            "min_free_gb": self.cfg.min_free_gb,
            "raw_mantissa_bits": self.cfg.raw_mantissa_bits,
            "fixed": {
                "ring_seconds": self.cfg.ring_seconds,
                "ppk2_port": self.cfg.ppk2_port,
                "uart_port": self.cfg.uart_port,
                "usb_port": self.cfg.usb_port,
            },
        }

    def apply_settings(self, s: dict, startup: bool = False) -> None:
        """Settings from the api: changed in the UI now, or the saved ones when capture starts (startup)."""
        if s.get("max_voltage_mv") is not None:
            self.ppk2.set_hard_max(int(s["max_voltage_mv"]))
        if s.get("auto_stream") is not None:
            self.ppk2.auto_stream = bool(s["auto_stream"])
            if startup and not self.ppk2.auto_stream and self.ppk2.streaming:
                try:  # it may have started before the saved settings were read
                    self.ppk2.stop_stream()
                except DeviceError:
                    pass
        if s.get("idle_power_off_s") is not None:
            self.idle.after_s = int(s["idle_power_off_s"])
            self.idle.viewer_seen()  # count from now, not from before the change
        if s.get("spike_filter") is not None:
            self.ppk2.set_spike_filter(bool(s["spike_filter"]))
        if s.get("flash_when_asleep") in ("wait", "always", "cycle"):
            self.flash_when_asleep = s["flash_when_asleep"]
        if s.get("flash_wake_wait_s") is not None:
            self.flash_wake_wait_s = float(s["flash_wake_wait_s"])

    def start(self) -> None:
        self.ppk2.start()
        self.uart.start()
        self.usb.start()
        self.idle.start()

    def stop(self) -> None:
        self.idle.stop()
        self.ppk2.stop()
        self.uart.stop()
        self.usb.stop()


class IdlePowerOff:
    """Switches the board off when nothing records and no browser has looked for `after_s` seconds, so a forgotten
    board doesn't keep running and opening the app never finds it powered without a deliberate action."""

    CHECK_S = 10.0

    def __init__(self, ppk2: PPK2Manager, after_s: int):
        self.ppk2 = ppk2
        self.after_s = after_s
        self.busy = lambda: False  # set to "a session is recording" once sessions exist
        self._last_viewer = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="idle-power-off", daemon=True)

    def viewer_seen(self) -> None:
        self._last_viewer = time.monotonic()

    def remaining_s(self) -> float | None:
        """Seconds until the board is switched off, or None if it won't be (disabled, off already, recording)."""
        if self.after_s <= 0 or self.ppk2.dut_power is False or not self.ppk2.connected or self.busy():
            return None
        return max(0.0, self.after_s - (time.monotonic() - self._last_viewer))

    def start(self) -> None:
        self._thread.start()  # always: the time can be changed while running (0 = never)

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.CHECK_S):
            if self.remaining_s() != 0.0:
                continue
            try:
                self.ppk2.set_dut_power(False, by="idle")
                log.info("Board switched off: nothing recording and no viewer for %d s", self.after_s)
            except DeviceError as e:
                log.warning("idle power-off failed: %s", e)

    def status(self) -> dict:
        return {"after_s": self.after_s, "in_s": self.remaining_s()}
