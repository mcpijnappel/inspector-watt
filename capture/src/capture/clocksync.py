"""Maps host monotonic time onto the PPK2 sample clock.

Every PPK2 read gives (arrival time, index of the last sample). USB latency only ever makes data arrive
late, so the smallest "arrival - index * period" seen over a sliding window is the best estimate of when
sample 0 happened. The window lets the estimate follow slow drift between the PPK2 and host clocks.
"""

from __future__ import annotations

from collections import deque

from .ppk2 import SAMPLE_PERIOD_S


class ClockSync:
    def __init__(self, window_s: float = 10.0):
        self.window_s = window_s
        self.reset()

    def reset(self) -> None:
        self._win: deque[tuple[float, float]] = deque()  # (arrival, offset), offsets increasing (monotonic deque)
        self._start: float | None = None  # arrival time of the first read
        self._first_offset: float | None = None  # best offset during the first window (drift reference)

    def observe(self, arrival: float, end_idx: int) -> None:
        offset = arrival - end_idx * SAMPLE_PERIOD_S
        while self._win and self._win[-1][1] >= offset:
            self._win.pop()
        self._win.append((arrival, offset))
        while self._win and arrival - self._win[0][0] > self.window_s:
            self._win.popleft()
        if self._start is None:
            self._start = arrival
        if arrival - self._start <= self.window_s:
            self._first_offset = self._win[0][1]

    @property
    def offset(self) -> float | None:
        """Host monotonic time of sample 0."""
        return self._win[0][1] if self._win else None

    def index_at(self, t: float) -> int | None:
        off = self.offset
        return None if off is None else int(round((t - off) / SAMPLE_PERIOD_S))

    @property
    def drift_ppm(self) -> float | None:
        """How much the PPK2 clock runs slow (+) or fast (-) versus the host, since the stream started."""
        if self._start is None or not self._win:
            return None
        elapsed = self._win[-1][0] - self._start
        if elapsed < 3 * self.window_s:
            return None
        return (self.offset - self._first_offset) / elapsed * 1e6
