"""Thread-safe circular buffer of full-rate samples, addressed by absolute sample index."""

from __future__ import annotations

import threading
import warnings

import numpy as np


class RingBuffer:
    def __init__(self, capacity: int):
        self.capacity = int(capacity)
        self._cur = np.full(self.capacity, np.nan, np.float32)
        self._logic = np.zeros(self.capacity, np.uint8)
        self._end = 0  # absolute index of the next sample to be written
        self.generation = 0  # bumped on reset so readers notice a new stream
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._end = 0
            self.generation += 1

    @property
    def end(self) -> int:
        return self._end

    @property
    def start(self) -> int:
        return max(0, self._end - self.capacity)

    def append(self, current: np.ndarray, logic: np.ndarray) -> None:
        n = len(current)
        if n == 0:
            return
        with self._lock:
            if n > self.capacity:
                skip = n - self.capacity
                self._end += skip
                current, logic, n = current[skip:], logic[skip:], self.capacity
            pos = self._end % self.capacity
            first = min(n, self.capacity - pos)
            self._cur[pos : pos + first] = current[:first]
            self._logic[pos : pos + first] = logic[:first]
            if first < n:
                self._cur[: n - first] = current[first:]
                self._logic[: n - first] = logic[first:]
            self._end += n

    def skip(self, n: int) -> None:
        """Advance by n samples of 'no data' (NaN), e.g. while the device was unplugged."""
        if n <= 0:
            return
        with self._lock:
            if n >= self.capacity:
                self._cur[:] = np.nan
                self._logic[:] = 0
            else:
                pos = self._end % self.capacity
                first = min(n, self.capacity - pos)
                self._cur[pos : pos + first] = np.nan
                self._logic[pos : pos + first] = 0
                self._cur[: n - first] = np.nan
                self._logic[: n - first] = 0
            self._end += n

    def read(self, start: int, end: int) -> tuple[int, np.ndarray, np.ndarray]:
        """Copy samples [start, end), clamped to what is still buffered. Returns (actual_start, current, logic)."""
        with self._lock:
            start = max(start, self.start)
            end = min(end, self._end)
            if end <= start:
                return start, np.empty(0, np.float32), np.empty(0, np.uint8)
            n = end - start
            pos = start % self.capacity
            first = min(n, self.capacity - pos)
            cur = np.concatenate([self._cur[pos : pos + first], self._cur[: n - first]])
            logic = np.concatenate([self._logic[pos : pos + first], self._logic[: n - first]])
            return start, cur, logic


def bin_stats(cur: np.ndarray, logic: np.ndarray, bin_size: int):
    """Min/max/mean per bin (NaN-aware) and OR of logic bits per bin. An incomplete trailing bin is dropped."""
    nb = len(cur) // bin_size
    if nb == 0:
        e = np.empty(0, np.float32)
        return e, e, e, np.empty(0, np.uint8)
    c = cur[: nb * bin_size].reshape(nb, bin_size)
    lg = logic[: nb * bin_size].reshape(nb, bin_size)
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        mn = np.nanmin(c, axis=1)
        mx = np.nanmax(c, axis=1)
        mean = np.nanmean(c, axis=1)
    return mn, mx, mean, np.bitwise_or.reduce(lg, axis=1)


def to_json_list(arr: np.ndarray) -> list:
    """Float array -> list with None for NaN (JSON has no NaN)."""
    out = arr.astype(object)
    out[np.isnan(arr)] = None
    return out.tolist()
