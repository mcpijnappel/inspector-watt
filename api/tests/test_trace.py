import numpy as np

from api.trace import bin_samples, pick_tier, with_gaps


def test_pick_tier():
    assert pick_tier(10, 2000) is None  # 5 ms per point: raw
    assert pick_tier(60, 2000)[0] == "10ms"  # 30 ms per point
    assert pick_tier(3600, 2000)[0] == "1s"
    assert pick_tier(7 * 86400, 2000)[0] == "1m"
    assert pick_tier(365 * 86400, 2000)[0] == "1h"


def test_bin_samples_raw_and_binned():
    cur = np.arange(10, dtype=np.float32)
    raw = bin_samples(100, cur, 20)
    assert raw["bucket_s"] == 1e-5 and raw["mean"] == list(range(10))
    assert raw["x"][0] == 100 * 1e-5
    b = bin_samples(0, cur, 3)  # 4 per bin, last bin partial
    assert b["min"] == [0, 4, 8] and b["max"] == [3, 7, 9]


def test_with_gaps_inserts_null():
    x, cols = with_gaps([0, 1, 2, 5, 6], {"mean": [1, 1, 1, 2, 2]}, 1.0)
    assert x == [0, 1, 2, 3, 5, 6] and cols["mean"] == [1, 1, 1, None, 2, 2]


def test_form_from_config_converts_units():
    from api.main import form_from_config
    f = form_from_config({"kind": "soak", "voltage_mv": 3700, "wake_threshold_a": 0.005, "hold_s": 0.5,
                          "pre_s": 0.1, "post_s": 0.5, "max_duration_s": 300})
    assert f["wake_threshold_ua"] == 5000 and f["hold_ms"] == 500 and f["pre_ms"] == 100
    assert f["kind"] == "soak" and f["max_duration_s"] == 300


def test_raw_from_files_reads_only_needed_row_groups(tmp_path):
    import asyncio

    import pyarrow as pa
    import pyarrow.parquet as pq

    from api.trace import raw_from_files

    cur = np.arange(250_000, dtype=np.float32)
    p = tmp_path / "seg.parquet"
    pq.write_table(pa.table({"idx": np.arange(1000, 251_000), "current_a": cur}), p, row_group_size=100_000)

    class Conn:
        async def execute(self, *_):
            class R:
                async def fetchall(self_inner):
                    return [{"path": "seg.parquet", "start_idx": 1000, "n_samples": 250_000}]
            return R()

    out, cov = asyncio.run(raw_from_files(Conn(), tmp_path, 1, 150_000, 210_000))
    assert cov == 1.0 and out[0] == 149_000 and out[-1] == 208_999
    out, cov = asyncio.run(raw_from_files(Conn(), tmp_path, 1, 250_000, 260_000))  # past the end: partly covered
    assert abs(cov - 0.1) < 1e-9 and out[0] == 249_000 and np.isnan(out[-1])
