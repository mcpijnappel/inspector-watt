"""Full-rate raw samples as Parquet files.

One file per segment, columns: idx (session sample index, int64), current_a (float32), logic (uint8).
idx makes every file self-describing for ad-hoc analysis, e.g. with DuckDB:
    SELECT avg(current_a) FROM 'data/sessions/12/detail/*.parquet' WHERE idx BETWEEN 0 AND 6000000;
Row groups of 1 s let readers skip straight to the part they need.

current_a is rounded to `mantissa_bits` significant bits before writing (default 12: max relative error
1.2e-4, far below the PPK2's accuracy). The low bits are ADC noise and barely compress; dropping them
saves ~40 %. 23 = lossless.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .ppk2 import SAMPLE_RATE_HZ

ROW_GROUP = SAMPLE_RATE_HZ  # 1 s
LOSSLESS = 23


def round_mantissa(x: np.ndarray, bits: int) -> np.ndarray:
    """Round float32 values to `bits` mantissa bits (round half up on the magnitude). NaN stays NaN."""
    x = np.asarray(x, np.float32)
    if bits >= LOSSLESS:
        return x
    drop = LOSSLESS - bits
    u = x.view(np.uint32)
    r = (u + np.uint32(1 << (drop - 1))) & np.uint32((0xFFFFFFFF << drop) & 0xFFFFFFFF)
    out = r.view(np.float32)
    return np.where(np.isnan(x), x, out)


def segment_path(session_id: int, kind: str, start_idx: int) -> str:
    return f"sessions/{session_id}/{kind}/{start_idx:013d}.parquet"


def write_segment(data_dir: Path, session_id: int, kind: str, start_idx: int,
                  current: np.ndarray, logic: np.ndarray, mantissa_bits: int = 12) -> tuple[str, int]:
    """Write one segment atomically. Returns (path relative to data_dir, bytes)."""
    rel = segment_path(session_id, kind, start_idx)
    final = data_dir / rel
    final.parent.mkdir(parents=True, exist_ok=True)
    n = len(current)
    table = pa.table({
        "idx": pa.array(np.arange(start_idx, start_idx + n, dtype=np.int64)),
        "current_a": pa.array(round_mantissa(current, mantissa_bits)),
        "logic": pa.array(np.asarray(logic, np.uint8)),
    }).replace_schema_metadata({"sample_period_s": "1e-05", "mantissa_bits": str(mantissa_bits)})
    tmp = final.with_suffix(".tmp")
    pq.write_table(
        table, tmp,
        compression="zstd",
        row_group_size=ROW_GROUP,
        use_dictionary=["logic"],
        column_encoding={"idx": "DELTA_BINARY_PACKED", "current_a": "BYTE_STREAM_SPLIT"},
        write_statistics=["idx"],
    )
    os.replace(tmp, final)
    return rel, final.stat().st_size
