"""CPU and memory per service, and of the whole machine, for Settings → Health.

Every compose service runs in its own cgroup slice (`cgroup_parent: pst-<service>.slice`). With systemd's cgroup
driver that's /sys/fs/cgroup/pst.slice/pst-<service>.slice/, mounted read-only at /host-cgroup. A slice's own
counters cover all its processes:

- memory.current  bytes in use now; memory.peak: the most since the slice started
- cpu.stat        usage_usec: CPU time used, so CPU % = its growth / elapsed time (100 % = one core)

The kernel keeps a memory peak per slice, but no CPU peak: the most CPU (and the machine's most memory) is the highest
10 s sample since the api started, kept apart from the 24 h window so it doesn't drop off.

The machine: /proc/meminfo (total, available) and /proc/stat (busy vs idle jiffies); a container sees the host's.
Sampled every 10 s, the last 24 h kept in memory: min / avg / max per period, and a short line for a sparkline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("api.resources")

INTERVAL_S = 10.0
KEEP_S = 24 * 3600
SPARK_POINTS = 60
HOST_WARN = 0.8  # the machine at 80 % of its memory or CPU: shown as a warning


def find_slices(root: Path, prefix: str) -> dict[str, Path]:
    """{service: slice dir} for the slices named <prefix>-<service>.slice, wherever the cgroup driver put them
    (systemd nests them under <prefix>.slice; cgroupfs puts them at the top)."""
    out: dict[str, Path] = {}
    for base in (root / f"{prefix}.slice", root):
        if not base.is_dir():
            continue
        for d in base.iterdir():
            name = d.name
            if d.is_dir() and name.startswith(f"{prefix}-") and name.endswith(".slice"):
                out.setdefault(name[len(prefix) + 1:-len(".slice")], d)
    return out


def _int(path: Path) -> int | None:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def cpu_usec(slice_dir: Path) -> int | None:
    try:
        for line in (slice_dir / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def running(slice_dir: Path) -> bool:
    """Any process left in the slice (a stopped container's slice can stay behind empty)."""
    pids = _int(slice_dir / "pids.current")
    if pids is not None:
        return pids > 0
    try:
        return any(d.is_dir() and d.name.endswith(".scope") for d in slice_dir.iterdir())
    except OSError:
        return False


def host_memory(meminfo: Path) -> tuple[int, int] | None:
    """(total, used) bytes: used = total - available."""
    try:
        f = {k: int(v.split()[0]) * 1024 for k, v in (l.split(":", 1) for l in meminfo.read_text().splitlines() if ":" in l)}
        return f["MemTotal"], f["MemTotal"] - f["MemAvailable"]
    except (OSError, KeyError, ValueError):
        return None


def host_extras(proc: Path) -> dict:
    """Uptime, load average (runnable tasks over 1 / 5 / 15 min) and swap: what the machine says about itself."""
    out: dict = {}
    try:
        out["uptime_s"] = float((proc / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        out["load"] = [float(x) for x in (proc / "loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        pass
    try:
        f = {k: int(v.split()[0]) * 1024 for k, v in (l.split(":", 1) for l in (proc / "meminfo").read_text().splitlines() if ":" in l)}
        if "SwapTotal" in f:
            out["swap_total"], out["swap_used"] = f["SwapTotal"], f["SwapTotal"] - f.get("SwapFree", 0)
    except (OSError, ValueError):
        pass
    return out


def host_cpu(stat: Path) -> tuple[int, int] | None:
    """(busy, total) jiffies over all cores since boot."""
    try:
        v = [int(x) for x in stat.read_text().splitlines()[0].split()[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
        total = sum(v[:8])  # without guest time, which is already in user
        return total - idle, total
    except (OSError, ValueError, IndexError):
        return None


def stats(values: list[float]) -> dict | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {"now": vals[-1], "min": min(vals), "avg": sum(vals) / len(vals), "max": max(vals)}


def spark(values: list[float], n: int = SPARK_POINTS) -> list[float]:
    """At most n points: the mean of each bucket."""
    vals = [v for v in values if v is not None]
    if len(vals) <= n:
        return vals
    step = len(vals) / n
    return [sum(b) / len(b) for b in (vals[int(i * step):int((i + 1) * step)] for i in range(n)) if b]


class Resources:
    def __init__(self, root: str = "/host-cgroup", prefix: str = "pst", proc: str = "/proc", clock=time.time):
        self.root, self.prefix, self.proc, self.clock = Path(root), prefix, Path(proc), clock
        self.cores = os.cpu_count() or 1
        self.series: dict[str, deque] = {}  # service -> (t, mem bytes, cpu % of one core)
        self.host: deque = deque(maxlen=int(KEEP_S / INTERVAL_S) + 1)  # (t, mem used, cpu % of the machine)
        self.mem_total: int | None = None
        self.peak: dict[str, int | None] = {}
        self.up: dict[str, bool] = {}
        self._prev: dict[str, tuple[float, int]] = {}
        self._prev_host: tuple[int, int] | None = None
        self.started = clock()
        self.cpu_peak: dict[str, float] = {}  # service -> most CPU % of one core since the api started
        self.host_peak: dict[str, float] = {}  # "mem" bytes, "cpu" % of the machine, since the api started

    @property
    def available(self) -> bool:
        return bool(find_slices(self.root, self.prefix))

    def sample(self) -> None:
        now = self.clock()
        for svc, d in find_slices(self.root, self.prefix).items():
            mem, use = _int(d / "memory.current"), cpu_usec(d)
            cpu = None
            prev = self._prev.get(svc)
            if use is not None:
                if prev and now > prev[0] and use >= prev[1]:
                    cpu = (use - prev[1]) / ((now - prev[0]) * 1e6) * 100
                self._prev[svc] = (now, use)
            self.peak[svc] = _int(d / "memory.peak")
            self.up[svc] = running(d)
            s = self.series.setdefault(svc, deque(maxlen=int(KEEP_S / INTERVAL_S) + 1))
            if self.up[svc]:
                s.append((now, mem, cpu))
                if cpu is not None:
                    self.cpu_peak[svc] = max(cpu, self.cpu_peak.get(svc, 0.0))
        hm, hc = host_memory(self.proc / "meminfo"), host_cpu(self.proc / "stat")
        cpu = None
        if hc and self._prev_host and hc[1] > self._prev_host[1]:
            cpu = (hc[0] - self._prev_host[0]) / (hc[1] - self._prev_host[1]) * 100
        if hc:
            self._prev_host = hc
        if hm:
            self.mem_total = hm[0]
            self.host.append((now, hm[1], cpu))
            self.host_peak["mem"] = max(hm[1], self.host_peak.get("mem", 0))
        if cpu is not None:
            self.host_peak["cpu"] = max(cpu, self.host_peak.get("cpu", 0.0))

    def report(self, period_s: float) -> dict:
        since = self.clock() - period_s
        services = []
        for svc, s in sorted(self.series.items()):
            rows = [r for r in s if r[0] >= since]
            services.append({"name": svc, "running": self.up.get(svc, False), "peak": self.peak.get(svc),
                             "cpu_peak": self.cpu_peak.get(svc),
                             "mem": stats([r[1] for r in rows]), "cpu": stats([r[2] for r in rows]),
                             "mem_line": spark([r[1] for r in rows]), "cpu_line": spark([r[2] for r in rows])})
        rows = [r for r in self.host if r[0] >= since]
        mem, cpu = stats([r[1] for r in rows]), stats([r[2] for r in rows])
        warn = []
        if mem and self.mem_total and mem["now"] > HOST_WARN * self.mem_total:
            warn.append("memory")
        recent = [r[2] for r in self.host if r[0] >= self.clock() - 60 and r[2] is not None]
        if recent and min(recent) > HOST_WARN * 100:
            warn.append("cpu")
        return {
            "available": bool(services),
            "interval_s": INTERVAL_S, "period_s": period_s, "since_s": self.clock() - self.started,
            "kept_s": (self.host[-1][0] - self.host[0][0]) if len(self.host) > 1 else 0,
            "host": {"cores": self.cores, "mem_total": self.mem_total, "mem": mem, "cpu": cpu, "warn": warn,
                     **host_extras(self.proc),
                     "mem_peak": self.host_peak.get("mem"), "cpu_peak": self.host_peak.get("cpu"),
                     "mem_line": spark([r[1] for r in rows]), "cpu_line": spark([r[2] for r in rows])},
            "services": services,
        }

    async def run(self) -> None:
        """Sample until cancelled (started with the api)."""
        if not self.available:
            log.info("no %s-*.slice cgroups under %s: per-service CPU and memory not available", self.prefix, self.root)
        while True:
            try:
                await asyncio.to_thread(self.sample)
            except Exception:  # noqa: BLE001 - a bad read must not stop the sampling
                log.exception("resource sample failed")
            await asyncio.sleep(INTERVAL_S)
