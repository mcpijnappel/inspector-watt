"""Turns the live sample stream (ring buffer) into stored session data.

Every tick it takes the new samples from the ring buffer and produces:
- 10 ms summaries (min/max/sum/count/logic) for the database,
- wake events (mean current above a threshold, ended after `hold_s` below it),
- raw Parquet segments: always for detail sessions; around each wake (+ optional rolling buffer) for soak,
- UART lines mapped onto the session timeline.

The recorder works in stream indices (ring buffer positions); session index = stream index - s0.
It never talks to the database itself: tick() returns a batch, the session manager writes it.
"""

from __future__ import annotations

import logging
import shutil
import threading
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .ppk2 import SAMPLE_PERIOD_S, SAMPLE_RATE_HZ
from .rawstore import write_segment
from .ringbuffer import RingBuffer
from .uart import LineLog

log = logging.getLogger(__name__)

BIN = 1000  # samples per 10 ms bucket
SEGMENT = 60 * SAMPLE_RATE_HZ  # max raw file length (1 min)
MAX_BINS_PER_TICK = 6000  # 60 s; limits the work per tick when catching up


@dataclass
class SessionConfig:
    kind: str = "soak"  # detail | soak
    voltage_mv: int | None = None  # set + board on at start (and again after a dropout) when given
    dut_power: bool = True
    wake_threshold_a: float = 500e-6
    hold_s: float = 0.5  # below threshold this long = wake over
    pre_s: float = 0.1  # raw data kept before a wake (soak)
    post_s: float = 0.5  # raw data kept after a wake (soak)
    raw_buffer_hours: float = 0.0  # soak: also keep continuous raw for the last N hours
    auto_restore: bool = True  # after a PPK2 dropout: restore voltage + board power
    uart_baud: int | None = None
    max_duration_s: float | None = None  # stop the session by itself after this long
    battery: dict | None = None  # battery emulation (battery.BatteryConfig as a dict); None = fixed voltage

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SessionConfig:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def new_batch() -> dict:
    return {"rollups": [], "uart": [], "events": [], "segments": [], "progress": None}


def merge_batches(a: dict, b: dict) -> dict:
    return {
        "rollups": a["rollups"] + b["rollups"],
        "uart": a["uart"] + b["uart"],
        "events": a["events"] + b["events"],
        "segments": a["segments"] + b["segments"],
        "progress": b["progress"] if b["progress"] is not None else a["progress"],
    }


class RawStream:
    """Tracks one raw recording window [open, close) in stream indices; hands out ranges to write,
    cut into segments of at most SEGMENT samples."""

    def __init__(self, kind: str):
        self.kind = kind
        self.cursor: int | None = None
        self.close_at: int | None = None

    @property
    def active(self) -> bool:
        return self.cursor is not None

    def open(self, idx: int) -> None:
        if self.active:
            self.close_at = None  # a new wake before the old window closed: keep going
        else:
            self.cursor, self.close_at = idx, None

    def close(self, idx: int) -> None:
        if self.active:
            self.close_at = max(idx, self.cursor)

    def pump(self, available_end: int, final: bool = False) -> list[tuple[int, int]]:
        if not self.active:
            return []
        limit = available_end if self.close_at is None else min(available_end, self.close_at)
        out = []
        while limit - self.cursor >= SEGMENT:
            out.append((self.cursor, self.cursor + SEGMENT))
            self.cursor += SEGMENT
        closing = self.close_at is not None and limit >= self.close_at
        if closing or final:
            if limit > self.cursor:
                out.append((self.cursor, limit))
            self.cursor = self.close_at = None
        return out


@dataclass
class _Wake:
    start: int  # stream idx
    last: int  # end of the last bin above threshold
    peak: float
    i_sum: float
    n: int
    below: int = 0
    pending_sum: float = 0.0  # trailing below-threshold bins, only counted if the wake continues
    pending_n: int = 0


@dataclass
class RecorderState:
    wakes: int = 0
    segments: int = 0
    raw_bytes: int = 0
    low_disk: bool = False
    gaps: int = 0


class SessionRecorder:
    def __init__(self, session_id: int, started_at: datetime, s0: int, first_idx: int, cfg: SessionConfig,
                 ring: RingBuffer, uart_log: LineLog, uart_seq: int, data_dir: Path,
                 min_free_bytes: int = 10 * 2**30, charge_c: float = 0.0, mantissa_bits: int = 23):
        assert (first_idx - s0) % BIN == 0, "bins must be aligned to the session start"
        self.session_id = session_id
        self.started_at = started_at
        self.s0 = s0
        self.cursor = first_idx
        self.cfg = cfg
        self.ring = ring
        self.uart_log = uart_log
        self.uart_seq = uart_seq
        self.data_dir = data_dir
        self.min_free_bytes = min_free_bytes
        self.mantissa_bits = mantissa_bits
        self.charge_c = charge_c
        self.state = RecorderState()
        self.stop_at: int | None = None  # set when saving a fixed range from the buffer
        self._wake: _Wake | None = None
        self._notes: list[dict] = []
        self._notes_lock = threading.Lock()
        self._hold_bins = max(1, round(cfg.hold_s * SAMPLE_RATE_HZ / BIN))
        self._streams: list[RawStream] = []
        self._event_raw: RawStream | None = None
        self._open_streams(first_idx)

    def _open_streams(self, idx: int) -> None:
        self._streams = []
        if self.cfg.kind == "detail":
            s = RawStream("detail")
            s.open(idx)
            self._streams.append(s)
        else:
            self._event_raw = RawStream("event")
            self._streams.append(self._event_raw)
            if self.cfg.raw_buffer_hours > 0:
                s = RawStream("buffer")
                s.open(idx)
                self._streams.append(s)

    # time helpers ----------------------------------------------------------------------------------------
    def sidx(self, stream_idx: int) -> int:
        return stream_idx - self.s0

    def ts(self, stream_idx: int) -> datetime:
        return self.started_at + timedelta(microseconds=(stream_idx - self.s0) * 10)

    @property
    def processed_samples(self) -> int:
        return self.cursor - self.s0

    @property
    def awake(self) -> bool:
        return self._wake is not None

    # events from outside (reconnects, restores, ...) -------------------------------------------------------
    def note(self, kind: str, stream_idx: int, end_stream_idx: int | None = None, **data) -> None:
        with self._notes_lock:
            self._notes.append({"kind": kind, "start": stream_idx, "end": end_stream_idx, "data": data})

    def _event(self, kind: str, start: int, end: int | None, peak=None, mean=None, charge=None, data=None) -> dict:
        return {"kind": kind, "start_idx": self.sidx(start), "end_idx": None if end is None else self.sidx(end),
                "ts": self.ts(start), "end_ts": None if end is None else self.ts(end),
                "peak_a": peak, "mean_a": mean, "charge_c": charge, "data": data or {}}

    # main step -------------------------------------------------------------------------------------------
    def tick(self, final: bool = False) -> dict:
        batch = new_batch()
        ring = self.ring

        if self.cursor < ring.start:  # fell behind the ring buffer (or a very long dropout)
            new = ring.start + (-(ring.start - self.s0)) % BIN
            batch["events"].append(self._event("gap", self.cursor, new, data={"reason": "buffer_overrun"}))
            self.state.gaps += 1
            log.warning("session %d: recorder fell behind, %.1f s lost", self.session_id,
                        (new - self.cursor) / SAMPLE_RATE_HZ)
            self._end_wake(batch, self.cursor)
            for s in self._streams:
                if s.active and s.cursor < new:
                    s.cursor = new
            self.cursor = new

        limit = ring.end if self.stop_at is None else min(ring.end, self.stop_at)
        nb = min((limit - self.cursor) // BIN, MAX_BINS_PER_TICK)
        if nb > 0:
            start, cur, logic = ring.read(self.cursor, self.cursor + nb * BIN)
            if start != self.cursor or len(cur) != nb * BIN:  # raced with the ring wrapping; next tick handles it
                nb = 0
            else:
                self._process_bins(batch, cur.reshape(nb, BIN), logic.reshape(nb, BIN))
                self.cursor += nb * BIN

        if final:
            self._end_wake(batch, self.cursor, truncated=True)
            if self._event_raw is not None:
                self._event_raw.close(self.cursor)

        self._write_raw(batch, final)
        self._collect_uart(batch)
        with self._notes_lock:
            notes, self._notes = self._notes, []
        for n in notes:
            batch["events"].append(self._event(n["kind"], n["start"], n["end"], data=n["data"]))
        batch["progress"] = (self.processed_samples, self.charge_c)
        return batch

    def checkpoint(self) -> dict:
        """Before the capture service shuts down mid-session: write what we have; resume continues later."""
        batch = self.tick()
        self._end_wake(batch, self.cursor, truncated=True)
        self._write_raw(batch, final=True)
        return batch

    def _process_bins(self, batch: dict, c: np.ndarray, lg: np.ndarray) -> None:
        nb = len(c)
        valid = ~np.isnan(c)
        n = valid.sum(axis=1)
        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore", RuntimeWarning)
            mn = np.nanmin(c, axis=1)
            mx = np.nanmax(c, axis=1)
        s = np.where(valid, c, 0).sum(axis=1, dtype=np.float64)
        lor = np.bitwise_or.reduce(lg, axis=1)
        self.charge_c += float(s.sum()) * SAMPLE_PERIOD_S

        bin_starts = self.cursor + np.arange(nb) * BIN
        for k in np.flatnonzero(n):
            b = int(bin_starts[k])
            batch["rollups"].append((self.session_id, self.ts(b), float(mn[k]), float(mx[k]), float(s[k]),
                                     int(n[k]), int(lor[k])))

        thr = self.cfg.wake_threshold_a
        for k in range(nb):
            if n[k] == 0:
                continue  # no data: doesn't start or end a wake
            b = int(bin_starts[k])
            above = s[k] / n[k] > thr
            w = self._wake
            if w is None:
                if above:
                    self._wake = _Wake(start=b, last=b + BIN, peak=float(mx[k]), i_sum=float(s[k]), n=int(n[k]))
                    if self._event_raw is not None:
                        pre = int(self.cfg.pre_s * SAMPLE_RATE_HZ)
                        self._event_raw.open(max(b - pre, self.ring.start, self.s0))
                continue
            if above:
                w.i_sum += w.pending_sum + float(s[k])
                w.n += w.pending_n + int(n[k])
                w.pending_sum, w.pending_n, w.below = 0.0, 0, 0
                w.last = b + BIN
                w.peak = max(w.peak, float(mx[k]))
            else:
                w.below += 1
                w.pending_sum += float(s[k])
                w.pending_n += int(n[k])
                if w.below >= self._hold_bins:
                    self._end_wake(batch, w.last)

    def _end_wake(self, batch: dict, end: int, truncated: bool = False) -> None:
        w = self._wake
        if w is None:
            return
        self._wake = None
        if not truncated:
            end = w.last  # the wake ended at its last bin above threshold, not after the hold time
        data = {"duration_s": (end - w.start) / SAMPLE_RATE_HZ}
        if truncated:
            data["truncated"] = True
        batch["events"].append(self._event("wake", w.start, end, peak=w.peak, mean=w.i_sum / max(w.n, 1),
                                           charge=w.i_sum * SAMPLE_PERIOD_S, data=data))
        self.state.wakes += 1
        if self._event_raw is not None:
            self._event_raw.close(end + int(self.cfg.post_s * SAMPLE_RATE_HZ))

    def _write_raw(self, batch: dict, final: bool) -> None:
        for stream in self._streams:
            for a, b in stream.pump(self.cursor, final):
                if shutil.disk_usage(self.data_dir).free < self.min_free_bytes:
                    if not self.state.low_disk:
                        self.state.low_disk = True
                        batch["events"].append(self._event("low_disk", a, None, data={"skipped": "raw data"}))
                        log.error("session %d: disk almost full, raw data is no longer written", self.session_id)
                    continue
                self.state.low_disk = False
                start, cur, logic = self.ring.read(a, b)
                if len(cur) == 0:
                    continue
                path, size = write_segment(self.data_dir, self.session_id, stream.kind, self.sidx(start), cur, logic,
                                           self.mantissa_bits)
                batch["segments"].append({"kind": stream.kind, "start_idx": self.sidx(start),
                                          "n_samples": len(cur), "path": path, "bytes": size})
                self.state.segments += 1
                self.state.raw_bytes += size

    def _collect_uart(self, batch: dict) -> None:
        lines = self.uart_log.after(self.uart_seq, 5000)
        if self.stop_at is not None:  # fixed range: only the lines that belong to it
            lines = [ln for ln in lines if ln.sample_idx is not None and self.s0 <= ln.sample_idx < self.stop_at]
        for ln in lines:
            wall = datetime.fromtimestamp(ln.wall, timezone.utc)
            if ln.sample_idx is not None:
                si = ln.sample_idx - self.s0
                ts = self.ts(ln.sample_idx)
            else:
                si, ts = None, wall
            batch["uart"].append((self.session_id, ts, si, wall, ln.text))
        if lines:
            self.uart_seq = lines[-1].seq
