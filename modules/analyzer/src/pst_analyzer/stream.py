"""The analyzer's rolling buffer: sigrok-cli streams raw samples (one byte per sample: D0-D7), kept in memory.

sigrok-cli runs with a very large sample limit rather than --continuous: the demo driver gives no data in
continuous mode, and a limit works the same for real analyzers. When it stops (unplugged, or the limit after many
hours) it's started again.

Timing: every chunk arrives at a known computer time, at most a little late (USB, pipes). So the earliest
"arrival - samples / rate" over the last seconds is the best estimate of when sample 0 was taken: like capture's
ClockSync does for the PPK2. That gives each sample a unix time to about a millisecond.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque

log = logging.getLogger("analyzer.stream")

MEMORY_BYTES = 256 * 2**20  # the buffer never takes more than this
MAX_SECONDS = 30.0  # nor more than this many seconds
HUGE_LIMIT = 10**15  # samples: at 24 MHz that's over a year
CHUNK = 1 << 16
SYNC_WINDOW_S = 10.0
RESTART_WAIT_S = 3.0


def buffer_seconds(rate: int) -> float:
    return min(MAX_SECONDS, MEMORY_BYTES / rate)


class Ring:
    """The last `capacity` samples, one byte each, addressed by their absolute sample number."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.buf = bytearray(capacity)
        self.end = 0  # absolute number of the next sample
        self._lock = threading.Lock()

    @property
    def start(self) -> int:
        return max(0, self.end - self.capacity)

    def append(self, data: bytes) -> None:
        with self._lock:
            if len(data) > self.capacity:  # more than fits: only the newest stay, the rest just counts
                self.end += len(data) - self.capacity
                data = data[-self.capacity:]
            n = len(data)
            pos = self.end % self.capacity
            first = min(n, self.capacity - pos)
            self.buf[pos:pos + first] = data[:first]
            if first < n:
                self.buf[:n - first] = data[first:]
            self.end += n

    def read(self, a: int, b: int) -> tuple[int, bytes]:
        """Samples a..b (clipped to what's still there): (first sample number, bytes)."""
        with self._lock:
            a, b = max(a, self.start), min(b, self.end)
            if b <= a:
                return a, b""
            pa, pb = a % self.capacity, b % self.capacity
            if pa < pb:
                return a, bytes(self.buf[pa:pb])
            return a, bytes(self.buf[pa:]) + bytes(self.buf[:pb])


class Timing:
    """Unix time of sample 0, from (arrival time, samples so far) of each chunk."""

    def __init__(self, rate: int, window_s: float = SYNC_WINDOW_S):
        self.rate = rate
        self.window_s = window_s
        self._win: deque[tuple[float, float]] = deque()  # (arrival, offset), offsets increasing

    def observe(self, arrival: float, samples: int) -> None:
        offset = arrival - samples / self.rate
        while self._win and self._win[-1][1] >= offset:
            self._win.pop()
        self._win.append((arrival, offset))
        while self._win and arrival - self._win[0][0] > self.window_s:
            self._win.popleft()

    @property
    def offset(self) -> float | None:
        return self._win[0][1] if self._win else None

    def wall(self, sample: int) -> float | None:
        off = self.offset
        return None if off is None else off + sample / self.rate

    def sample(self, wall: float) -> int | None:
        off = self.offset
        return None if off is None else int(round((wall - off) * self.rate))


class Streamer:
    """Keeps sigrok-cli streaming into the ring; restarts it when it stops."""

    def __init__(self, driver: str, rate: int, conn: str | None = None, popen=subprocess.Popen):
        self.driver, self.rate, self.conn = driver, rate, conn
        self.popen = popen
        self.ring = Ring(int(rate * buffer_seconds(rate)))
        self.timing = Timing(rate)
        self.error: str | None = None
        self.running = False
        self.started_at: float | None = None
        self.restarts = 0
        self._proc = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def command(self) -> list[str]:
        dev = self.driver + (f":conn={self.conn}" if self.conn else "")
        return ["sigrok-cli", "-d", dev, "-C", ",".join(f"D{i}" for i in range(8)), "--config",
                f"samplerate={self.rate}", "--samples", str(HUGE_LIMIT), "-O", "binary"]

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="analyzer-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        if self._thread:
            self._thread.join(timeout=5)
        self.running = False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._proc = p = self.popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as e:
                self.error = f"can't start sigrok-cli: {e}"
                self._stop.wait(RESTART_WAIT_S)
                continue
            # A new run starts at its own sample 0, at ring sample `base`; its timing starts afresh.
            self.timing = Timing(self.rate)
            base = self.ring.end
            self.started_at = time.time()
            got = 0
            while not self._stop.is_set():
                data = p.stdout.read1(CHUNK) if hasattr(p.stdout, "read1") else p.stdout.read(CHUNK)
                if not data:
                    break
                now = time.time()
                self.ring.append(data)
                got += len(data)
                self.timing.observe(now, base + got)
                if not self.running:
                    self.running, self.error = True, None
                    log.info("streaming at %d Hz", self.rate)
            self.running = False
            if self._stop.is_set():
                break
            err = (p.stderr.read() or b"").decode(errors="replace").strip().splitlines()
            p.wait()
            self.error = err[-1] if err else f"sigrok-cli stopped (exit {p.returncode})"
            self.restarts += 1
            log.warning("stream stopped: %s; starting again", self.error)
            self._stop.wait(RESTART_WAIT_S)

    def window(self, t0: float, t1: float) -> tuple[int, bytes, float | None]:
        """Samples between two unix times: (first sample number, bytes, its unix time)."""
        a, b = self.timing.sample(t0), self.timing.sample(t1)
        if a is None or b is None:
            return 0, b"", None
        first, data = self.ring.read(a, b)
        return first, data, self.timing.wall(first)

    def status(self) -> dict:
        return {"running": self.running, "rate": self.rate, "buffer_s": round(buffer_seconds(self.rate), 1),
                "buffered_s": round((self.ring.end - self.ring.start) / self.rate, 1), "error": self.error,
                "restarts": self.restarts}
