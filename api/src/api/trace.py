"""Picks the right data source for a time range and returns about `points` buckets of min/max/mean.

Sources, finest first: raw Parquet (10 us) -> rollup_10ms -> rollup_1s -> rollup_1m -> rollup_1h.
For a running session, the newest minutes come from the capture service's RAM buffer instead
(the current raw segment isn't written to disk yet).
"""

from __future__ import annotations

import functools
import math
import warnings
from datetime import timedelta
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

SAMPLE_PERIOD_S = 1e-5
TIERS = [  # (name, bucket seconds, table, time column)
    ("10ms", 0.01, "rollup_10ms", "ts"),
    ("1s", 1.0, "rollup_1s", "bucket"),
    ("1m", 60.0, "rollup_1m", "bucket"),
    ("1h", 3600.0, "rollup_1h", "bucket"),
]
MAX_RAW_SAMPLES = 5_000_000  # 50 s; beyond that the 10 ms tier is used anyway


def pick_tier(span_s: float, points: int):
    """Coarsest DB tier whose bucket is still <= the wanted bucket width (None = raw is needed)."""
    want = span_s / points
    best = None
    for tier in TIERS:
        if tier[1] <= want:
            best = tier
    return best


@functools.lru_cache(maxsize=64)
def _parquet(path: Path) -> pq.ParquetFile:
    return pq.ParquetFile(path)


def nan_to_none(a: np.ndarray) -> list:
    out = a.astype(object)
    out[np.isnan(a.astype(np.float64))] = None
    return out.tolist()


def empty(tier: str, bucket_s: float, **extra) -> dict:
    return {"tier": tier, "bucket_s": bucket_s, "x": [], "min": [], "max": [], "mean": [], **extra}


def with_gaps(x: list, cols: dict[str, list], bucket_s: float) -> tuple[list, dict]:
    """Insert a null point where buckets are missing, so charts break the line instead of bridging the gap."""
    if len(x) < 2:
        return x, cols
    out_x, out = [x[0]], {k: [v[0]] for k, v in cols.items()}
    for i in range(1, len(x)):
        if x[i] - x[i - 1] > bucket_s * 1.5:
            out_x.append(x[i - 1] + bucket_s)
            for k in out:
                out[k].append(None)
        out_x.append(x[i])
        for k, v in cols.items():
            out[k].append(v[i])
    return out_x, out


async def db_trace(conn, session: dict, tier, start_s: float, end_s: float, points: int) -> dict:
    name, bucket_s, table, tcol = tier
    width = max(bucket_s, math.ceil((end_s - start_s) / points / bucket_s) * bucket_s)
    t0 = session["started_at"]
    rows = await (await conn.execute(
        f"""SELECT extract(epoch FROM time_bucket(make_interval(secs => %(w)s), {tcol}, %(origin)s) - %(origin)s) AS x,
                   min(i_min) AS mn, max(i_max) AS mx, sum(i_sum) AS s, sum(n)::bigint AS n
            FROM {table}
            WHERE session_id = %(sid)s AND {tcol} >= %(a)s AND {tcol} < %(b)s
            GROUP BY 1 ORDER BY 1""",
        {"w": width, "origin": t0, "sid": session["id"],
         "a": t0 + timedelta(seconds=start_s), "b": t0 + timedelta(seconds=end_s)})).fetchall()
    x = [float(r["x"]) for r in rows]
    cols = {"min": [r["mn"] for r in rows], "max": [r["mx"] for r in rows],
            "mean": [float(r["s"]) / int(r["n"]) if r["n"] else None for r in rows]}
    x, cols = with_gaps(x, cols, width)
    return {"tier": name, "bucket_s": width, "x": x, **cols}


def bin_samples(start_idx: int, cur: np.ndarray, points: int) -> dict:
    """Raw samples -> at most `points` min/max/mean buckets (or the samples themselves if they fit)."""
    n = len(cur)
    k = max(1, math.ceil(n / points))
    pad = (-n) % k
    c = np.concatenate([cur, np.full(pad, np.nan, np.float32)]).reshape(-1, k)
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mn, mx, mean = np.nanmin(c, 1), np.nanmax(c, 1), np.nanmean(c, 1)
    x = (start_idx + np.arange(len(c)) * k) * SAMPLE_PERIOD_S
    return {"tier": "raw", "bucket_s": k * SAMPLE_PERIOD_S, "x": x.tolist(),
            "min": nan_to_none(mn), "max": nan_to_none(mx), "mean": nan_to_none(mean)}


async def raw_from_files(conn, data_dir: Path, session_id: int, i0: int, i1: int) -> tuple[np.ndarray, float]:
    """Samples [i0, i1) from Parquet segments; NaN where no segment exists. Returns (current, coverage 0-1).
    Only the row groups (1 s each) that overlap the range are read and decompressed."""
    segs = await (await conn.execute(
        "SELECT path, start_idx, n_samples FROM raw_segments WHERE session_id = %s "
        "AND start_idx < %s AND start_idx + n_samples > %s ORDER BY start_idx",
        (session_id, i1, i0))).fetchall()
    out = np.full(i1 - i0, np.nan, np.float32)
    covered = np.zeros(i1 - i0, bool)
    for seg in segs:
        path = data_dir / seg["path"]
        if not path.exists():
            continue
        f = _parquet(path)
        groups, offsets = [], []
        first = seg["start_idx"]
        for g in range(f.metadata.num_row_groups):
            n = f.metadata.row_group(g).num_rows
            if first < i1 and first + n > i0:
                groups.append(g)
                offsets.append(first)
            first += n
        if not groups:
            continue
        t = f.read_row_groups(groups, columns=["current_a"])
        cur = t.column("current_a").to_numpy()
        start = offsets[0]  # the groups are contiguous
        a, b = max(i0, start), min(i1, start + len(cur))
        out[a - i0:b - i0] = cur[a - start:b - start]
        covered[a - i0:b - i0] = True
    return out, float(covered.mean()) if len(covered) else 0.0
