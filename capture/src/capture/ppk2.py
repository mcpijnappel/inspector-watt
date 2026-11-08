"""Nordic Power Profiler Kit II (PPK2) protocol + vectorized sample decoder.

Protocol reference: Nordic's pc-nrfconnect-ppk and IRNAS ppk2-api-python (both public).
The PPK2 is USB CDC, so the baud rate is ignored. Once started it always streams 100 kS/s,
4 bytes per sample (uint32 little endian):

    bits  0-13  ADC value
    bits 14-16  measurement range (0 = most sensitive ... 4 = highest current)
    bits 18-23  rolling sample counter (mod 64), used to detect lost samples
    bits 24-31  logic inputs D0-D7
"""

from __future__ import annotations

import enum
import re
import time
from dataclasses import dataclass, field

import numpy as np
from scipy.signal import lfilter

SAMPLE_RATE_HZ = 100_000
SAMPLE_PERIOD_S = 1.0 / SAMPLE_RATE_HZ
BYTES_PER_SAMPLE = 4
VDD_MIN_MV = 800
VDD_MAX_MV = 5000
ADC_MULT = 1.8 / 163840
N_RANGES = 5
COUNTER_MASK = 0x3F

# Nordic's spike filter around range switches.
SPIKE_ALPHA = 0.18
SPIKE_ALPHA_TOP_RANGE = 0.06
SPIKE_SAMPLES = 3


class Cmd(enum.IntEnum):
    AVERAGE_START = 0x06
    AVERAGE_STOP = 0x07
    DEVICE_RUNNING_SET = 0x0C
    REGULATOR_SET = 0x0D
    SET_POWER_MODE = 0x11
    GET_META_DATA = 0x19
    RESET = 0x20


class PowerMode(enum.IntEnum):
    AMPERE = 1
    SOURCE = 2


def voltage_command(mv: int) -> bytes:
    if not VDD_MIN_MV <= mv <= VDD_MAX_MV:
        raise ValueError(f"voltage {mv} mV outside {VDD_MIN_MV}-{VDD_MAX_MV} mV")
    return bytes([Cmd.REGULATOR_SET, (mv >> 8) & 0xFF, mv & 0xFF])


def power_mode_command(mode: PowerMode) -> bytes:
    return bytes([Cmd.SET_POWER_MODE, int(mode)])


def dut_power_command(on: bool) -> bytes:
    return bytes([Cmd.DEVICE_RUNNING_SET, 1 if on else 0])


_CAL_KEY = re.compile(r"(R|GS|GI|O|S|I|UG)([0-4])")


@dataclass
class Calibration:
    """Per-range calibration constants read from the device (GET_META_DATA)."""

    r: np.ndarray = field(default_factory=lambda: np.array([1031.64, 101.65, 10.15, 0.94, 0.043]))
    gs: np.ndarray = field(default_factory=lambda: np.zeros(N_RANGES))
    gi: np.ndarray = field(default_factory=lambda: np.ones(N_RANGES))
    o: np.ndarray = field(default_factory=lambda: np.zeros(N_RANGES))
    s: np.ndarray = field(default_factory=lambda: np.zeros(N_RANGES))
    i: np.ndarray = field(default_factory=lambda: np.zeros(N_RANGES))
    ug: np.ndarray = field(default_factory=lambda: np.ones(N_RANGES))
    fields: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    from_device: bool = False

    @classmethod
    def parse(cls, text: str) -> Calibration:
        cal = cls(raw=text, from_device=True)
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            cal.fields[key] = value
            m = _CAL_KEY.fullmatch(key)
            if not m:
                continue
            val = float(value)
            name, idx = m.group(1).lower(), int(m.group(2))
            if name == "r" and val == 0:  # some units report 0; keep the default
                continue
            getattr(cal, name)[idx] = val
        return cal

    @property
    def vdd_mv(self) -> int | None:
        """Voltage the device reports as currently set (field 'VDD')."""
        try:
            return int(float(self.fields["VDD"]))
        except (KeyError, ValueError):
            return None

    @property
    def mode(self) -> PowerMode | None:
        try:
            return PowerMode(int(self.fields["mode"]))
        except (KeyError, ValueError):
            return None

    def to_dict(self) -> dict:
        return {
            "from_device": self.from_device,
            "vdd_mv": self.vdd_mv,
            "mode": self.mode.name.lower() if self.mode else None,
            "fields": self.fields,
            **{k: getattr(self, k).tolist() for k in ("r", "gs", "gi", "o", "s", "i", "ug")},
        }


def adc_to_current(adc_raw: np.ndarray, rng: np.ndarray, cal: Calibration, vdd_mv: float) -> np.ndarray:
    """Convert 14-bit ADC values + range to amps (float64). Same formula as Nordic's app."""
    adc = adc_raw.astype(np.float64) * 4.0
    base = (adc - cal.o[rng]) * (ADC_MULT / cal.r[rng])
    return cal.ug[rng] * (base * (cal.gs[rng] * base + cal.gi[rng]) + (cal.s[rng] * (vdd_mv / 1000.0) + cal.i[rng]))


@dataclass
class DecodedBlock:
    current: np.ndarray  # float32 amps, NaN where samples were lost or corrupt
    logic: np.ndarray  # uint8, D0-D7
    lost: int = 0  # samples missing (filled with NaN)
    gaps: int = 0  # number of discontinuities
    corrupt: int = 0  # isolated samples with a bad counter (set to NaN, no gap inserted)
    dropped_bytes: int = 0  # bytes skipped to restore 4-byte alignment


_EMPTY_F = np.empty(0, np.float32)
_EMPTY_U8 = np.empty(0, np.uint8)


def _counters(words: np.ndarray) -> np.ndarray:
    return ((words >> 18) & COUNTER_MASK).astype(np.int16)


def _bad_fraction(words: np.ndarray) -> float:
    if len(words) < 2:
        return 0.0
    d = np.diff(_counters(words)) & COUNTER_MASK
    return float(np.count_nonzero(d != 1)) / len(d)


class Decoder:
    """Stateful stream decoder. Feed it raw bytes in any chunk size."""

    def __init__(self, cal: Calibration | None = None, vdd_mv: float = 3000, spike_filter: bool = True):
        self.cal = cal or Calibration()
        self.vdd_mv = vdd_mv
        self.spike_filter = spike_filter
        self.reset()

    def reset(self) -> None:
        self._rem = b""
        self._last_counter: int | None = None
        self._ema: float | None = None
        self._ema4: float | None = None
        self._prev_range: int | None = None
        self._spike_carry = 0

    def feed(self, data: bytes) -> DecodedBlock:
        buf = self._rem + data
        dropped = 0
        n = len(buf) // BYTES_PER_SAMPLE
        if n == 0:
            self._rem = buf
            return DecodedBlock(_EMPTY_F, _EMPTY_U8)
        words = np.frombuffer(buf, "<u4", count=n)

        # Lost bytes (not whole samples) would shift every following word. Detect via the counter.
        if n >= 32 and _bad_fraction(words) > 0.25:
            scores = [_bad_fraction(np.frombuffer(buf, "<u4", count=(len(buf) - s) // 4, offset=s)) for s in range(4)]
            shift = int(np.argmin(scores))
            if shift:
                buf = buf[shift:]
                dropped = shift
                n = len(buf) // BYTES_PER_SAMPLE
                words = np.frombuffer(buf, "<u4", count=n)
                self._last_counter = None
        self._rem = buf[n * BYTES_PER_SAMPLE :]

        rng = np.minimum((words >> 14) & 0x7, N_RANGES - 1).astype(np.intp)
        logic = (words >> 24).astype(np.uint8)
        current = adc_to_current(words & 0x3FFF, rng, self.cal, self.vdd_mv)
        if self.spike_filter:
            current = self._filter_spikes(current, rng)
        current = current.astype(np.float32)

        # Counter continuity: lost_each[k] = samples missing right before sample k.
        c = _counters(words)
        prev = np.empty(n, np.int16)
        prev[0] = c[0] - 1 if self._last_counter is None else self._last_counter
        prev[1:] = c[:-1]
        d = (c - prev) & COUNTER_MASK
        lost_each = (d - 1) & COUNTER_MASK
        self._last_counter = int(c[-1])

        # A single garbled sample shows up as two jumps that add up to 2 steps: blank it, don't insert a gap.
        corrupt = 0
        if n >= 2:
            iso = (d[:-1] != 1) & (((d[:-1] + d[1:]) & COUNTER_MASK) == 2)
            if iso.any():
                idx = np.flatnonzero(iso)
                current[idx] = np.nan
                lost_each[idx] = 0
                lost_each[idx + 1] = 0
                corrupt = len(idx)

        total_lost = int(lost_each.sum())
        if total_lost:
            pos = np.arange(n) + np.cumsum(lost_each)
            full_c = np.full(n + total_lost, np.nan, np.float32)
            full_l = np.zeros(n + total_lost, np.uint8)
            full_c[pos] = current
            full_l[pos] = logic
            current, logic = full_c, full_l

        return DecodedBlock(
            current=current,
            logic=logic,
            lost=total_lost,
            gaps=int(np.count_nonzero(lost_each)),
            corrupt=corrupt,
            dropped_bytes=dropped,
        )

    def _filter_spikes(self, x: np.ndarray, rng: np.ndarray) -> np.ndarray:
        """Replace the 3 samples after each range switch with a running average (Nordic behaviour).

        Small simplification vs Nordic: their top-range average skips the first 2 samples after a switch.
        """
        n = len(x)
        if self._ema is None:
            self._ema = self._ema4 = float(x[0])
        a, a4 = SPIKE_ALPHA, SPIKE_ALPHA_TOP_RANGE
        ema, _ = lfilter([a], [1.0, a - 1.0], x, zi=[(1.0 - a) * self._ema])
        ema4, _ = lfilter([a4], [1.0, a4 - 1.0], x, zi=[(1.0 - a4) * self._ema4])
        self._ema, self._ema4 = float(ema[-1]), float(ema4[-1])

        changed = np.empty(n, bool)
        changed[0] = self._prev_range is not None and rng[0] != self._prev_range
        changed[1:] = rng[1:] != rng[:-1]
        self._prev_range = int(rng[-1])

        mask = changed.copy()
        for k in range(1, SPIKE_SAMPLES):
            mask[k:] |= changed[:-k]
        carry = self._spike_carry
        mask[: min(carry, n)] = True
        change_idx = np.flatnonzero(changed)
        last_end = int(change_idx[-1]) + SPIKE_SAMPLES if len(change_idx) else 0
        self._spike_carry = max(carry - n, last_end - n, 0)

        if not mask.any():
            return x
        smooth = np.where(rng == N_RANGES - 1, ema4, ema)
        return np.where(mask, smooth, x)


def encode_samples(current_a: np.ndarray, counter_start: int, logic: np.ndarray | None = None,
                   cal: Calibration | None = None) -> bytes:
    """Inverse of the decoder for a linear calibration (gs=0, gi=1, o=s=i=0, ug=1). Used by the simulator/tests."""
    cal = cal or Calibration()
    current_a = np.asarray(current_a, np.float64)
    n = len(current_a)
    # Pick the most sensitive range where the ADC value fits in 14 bits.
    counts = np.abs(current_a)[:, None] * cal.r[None, :] / (ADC_MULT * 4.0)
    fits = counts <= 16000
    rng = np.where(fits.any(axis=1), np.argmax(fits, axis=1), N_RANGES - 1)
    adc = np.clip(np.rint(counts[np.arange(n), rng]), 0, 0x3FFF).astype(np.uint32)
    counter = (np.arange(n, dtype=np.uint32) + counter_start) & COUNTER_MASK
    lg = np.zeros(n, np.uint32) if logic is None else np.asarray(logic, np.uint32)
    words = adc | (rng.astype(np.uint32) << 14) | (counter << 18) | (lg << 24)
    return words.astype("<u4").tobytes()


class PPK2Link:
    """Command layer on top of an open serial-like object. Reads are done by the owner's reader thread,
    except during connect (read_metadata), when nothing else reads."""

    def __init__(self, ser):
        self.ser = ser

    def send(self, payload: bytes) -> None:
        self.ser.write(payload)

    def drain(self, quiet_s: float = 0.2, max_s: float = 2.0) -> None:
        self.send(bytes([Cmd.AVERAGE_STOP]))
        end = time.monotonic() + max_s
        last_data = time.monotonic()
        self.ser.reset_input_buffer()
        while time.monotonic() < end and time.monotonic() - last_data < quiet_s:
            if self.ser.read(65536):
                last_data = time.monotonic()

    def read_metadata(self, timeout_s: float = 3.0) -> str:
        self.send(bytes([Cmd.GET_META_DATA]))
        buf = b""
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            buf += self.ser.read(4096)
            if b"END" in buf:
                return buf[: buf.index(b"END") + 3].decode("ascii", "replace")
        raise TimeoutError(f"no metadata from PPK2 (got {len(buf)} bytes)")

    def start(self) -> None:
        self.send(bytes([Cmd.AVERAGE_START]))

    def stop(self) -> None:
        self.send(bytes([Cmd.AVERAGE_STOP]))

    def set_voltage(self, mv: int) -> None:
        self.send(voltage_command(mv))

    def set_power_mode(self, mode: PowerMode) -> None:
        self.send(power_mode_command(mode))

    def set_dut_power(self, on: bool) -> None:
        self.send(dut_power_command(on))
