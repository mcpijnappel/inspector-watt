"""DUT debug UART: line splitting, timestamping and a thread-safe log of recent lines."""

from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import dataclass


@dataclass
class UartLine:
    seq: int
    wall: float  # unix time of the line's first byte
    mono: float  # host monotonic time of the line's first byte
    sample_idx: int | None  # PPK2 sample index at that moment (None if the PPK2 isn't streaming)
    text: str

    def to_dict(self) -> dict:
        return {"seq": self.seq, "wall": self.wall, "sample_idx": self.sample_idx, "text": self.text}


class LineSplitter:
    """Splits a byte stream into lines. Each line is stamped with the time its first byte came in.

    One read returns many bytes at once; the read time is when the *last* byte arrived. Earlier bytes are
    back-dated by the UART byte time (10 bits / baud), so lines in one read get their own timestamps.
    A partial line is emitted anyway after `idle_s` without new bytes, or when it reaches `max_len`."""

    def __init__(self, idle_s: float = 0.5, max_len: int = 4096, byte_time_s: float = 0.0):
        self.idle_s = idle_s
        self.byte_time_s = byte_time_s
        self.max_len = max_len
        self._buf = bytearray()
        self._t_first: tuple[float, float] | None = None  # (mono, wall) of first byte in _buf
        self._t_last = 0.0

    def feed(self, data: bytes, mono: float, wall: float) -> list[tuple[float, float, bytes]]:
        out = []
        if data:
            self._t_last = mono
        pos = 0
        for chunk in data.splitlines(keepends=True):
            if not self._buf:
                back = (len(data) - 1 - pos) * self.byte_time_s
                self._t_first = (mono - back, wall - back)
            pos += len(chunk)
            self._buf += chunk
            if self._buf.endswith(b"\n") or len(self._buf) >= self.max_len:
                out.append((*self._t_first, bytes(self._buf)))
                self._buf.clear()
        return out

    def flush_stale(self, mono: float) -> list[tuple[float, float, bytes]]:
        if self._buf and mono - self._t_last >= self.idle_s:
            line = (*self._t_first, bytes(self._buf))
            self._buf.clear()
            return [line]
        return []


def decode_line(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").rstrip("\r\n")


class LineLog:
    """Bounded log of recent lines; readers poll with the last sequence number they saw."""

    def __init__(self, maxlen: int = 20000):
        self._lines: deque[UartLine] = deque(maxlen=maxlen)
        self._next_seq = 0
        self._lock = threading.Lock()

    @property
    def last_seq(self) -> int:
        return self._next_seq - 1

    def add(self, wall: float, mono: float, sample_idx: int | None, text: str) -> UartLine:
        with self._lock:
            line = UartLine(self._next_seq, wall, mono, sample_idx, text)
            self._lines.append(line)
            self._next_seq += 1
            return line

    def seq_before(self, sample_idx: int) -> int:
        """Sequence number of the last line that arrived before PPK2 sample `sample_idx`."""
        with self._lock:
            for line in reversed(self._lines):
                if line.sample_idx is not None and line.sample_idx < sample_idx:
                    return line.seq
            return (self._lines[0].seq - 1) if self._lines else self._next_seq - 1

    def after(self, seq: int, limit: int = 1000) -> list[UartLine]:
        with self._lock:
            if not self._lines:
                return []
            first = self._lines[0].seq
            start = max(0, seq + 1 - first)
            return list(itertools.islice(self._lines, start, start + limit))
