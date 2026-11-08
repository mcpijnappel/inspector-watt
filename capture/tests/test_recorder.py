from datetime import datetime, timezone

import numpy as np
import pyarrow.parquet as pq
import pytest

from capture.recorder import BIN, SEGMENT, RawStream, SessionConfig, SessionRecorder, merge_batches, new_batch
from capture.ringbuffer import RingBuffer
from capture.uart import LineLog

RATE = 100_000
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def profile(seconds: float, wakes=(), sleep_a=3e-6, wake_a=4e-3) -> np.ndarray:
    cur = np.full(int(seconds * RATE), sleep_a, np.float32)
    for a, b in wakes:
        cur[int(a * RATE) : int(b * RATE)] = wake_a
    return cur


def run(cur: np.ndarray, cfg: SessionConfig, tmp_path, s0: int = 0, chunk: int = 50_000, uart=None,
        ring_s: int = 300) -> tuple[SessionRecorder, dict]:
    ring = RingBuffer(ring_s * RATE)
    if s0:
        ring.skip(s0)
    log = uart or LineLog()
    rec = SessionRecorder(1, T0, s0, s0, cfg, ring, log, -1, tmp_path, min_free_bytes=0)
    out = new_batch()
    for i in range(0, len(cur), chunk):
        part = cur[i : i + chunk]
        ring.append(part, np.zeros(len(part), np.uint8))
        out = merge_batches(out, rec.tick())
    out = merge_batches(out, rec.tick(final=True))
    return rec, out


def test_detail_rollups_charge_and_segments(tmp_path):
    cur = profile(150, wakes=[(10, 12)])
    rec, out = run(cur, SessionConfig(kind="detail"), tmp_path, chunk=200_000)
    assert len(out["rollups"]) == 150 * RATE // BIN
    _, ts, i_min, i_max, i_sum, n, _ = out["rollups"][1000]  # bin at 10.00 s
    assert ts == T0.replace(second=10) and n == BIN and i_max == pytest.approx(4e-3)
    assert rec.charge_c == pytest.approx(float(cur.astype(np.float64).sum()) * 1e-5, rel=1e-9)
    assert out["progress"][0] == 150 * RATE

    segs = out["segments"]
    assert [s["kind"] for s in segs] == ["detail"] * 3
    assert [s["n_samples"] for s in segs] == [SEGMENT, SEGMENT, 30 * RATE]
    t = pq.read_table(tmp_path / segs[1]["path"])
    assert t.column("idx")[0].as_py() == SEGMENT
    np.testing.assert_array_equal(t.column("current_a").to_numpy(), cur[SEGMENT : 2 * SEGMENT])


def test_soak_wake_event_and_raw_snippet(tmp_path):
    cur = profile(10, wakes=[(2.0, 2.5)])
    cfg = SessionConfig(kind="soak", wake_threshold_a=500e-6, hold_s=0.3, pre_s=0.1, post_s=0.2)
    rec, out = run(cur, cfg, tmp_path)
    wakes = [e for e in out["events"] if e["kind"] == "wake"]
    assert len(wakes) == 1
    w = wakes[0]
    assert (w["start_idx"], w["end_idx"]) == (200_000, 250_000)
    assert w["peak_a"] == pytest.approx(4e-3)
    assert w["charge_c"] == pytest.approx(4e-3 * 0.5, rel=1e-4)
    segs = out["segments"]
    assert len(segs) == 1 and segs[0]["kind"] == "event"
    assert segs[0]["start_idx"] == 190_000 and segs[0]["n_samples"] == 80_000  # 0.1 s before .. 0.2 s after


def test_short_dip_keeps_one_wake(tmp_path):
    cur = profile(6, wakes=[(1.0, 1.4), (1.6, 2.0)])  # 0.2 s dip < 0.5 s hold
    _, out = run(cur, SessionConfig(kind="soak", hold_s=0.5), tmp_path)
    wakes = [e for e in out["events"] if e["kind"] == "wake"]
    assert len(wakes) == 1
    assert (wakes[0]["start_idx"], wakes[0]["end_idx"]) == (100_000, 200_000)
    # the dip counts in the wake's charge, the trailing hold time does not
    assert wakes[0]["charge_c"] == pytest.approx((0.8 * 4e-3 + 0.2 * 3e-6), rel=1e-3)


def test_wake_open_at_stop_is_truncated(tmp_path):
    cur = profile(3, wakes=[(2.0, 3.0)])
    _, out = run(cur, SessionConfig(kind="soak"), tmp_path)
    (w,) = [e for e in out["events"] if e["kind"] == "wake"]
    assert w["data"]["truncated"] and w["end_idx"] == 300_000
    assert out["segments"][-1]["start_idx"] + out["segments"][-1]["n_samples"] == 300_000


def test_soak_rolling_buffer(tmp_path):
    cur = profile(70)
    _, out = run(cur, SessionConfig(kind="soak", raw_buffer_hours=1), tmp_path, chunk=500_000)
    assert [s["kind"] for s in out["segments"]] == ["buffer", "buffer"]


def test_nan_bins_are_skipped_and_dont_end_wakes(tmp_path):
    cur = profile(4, wakes=[(1.0, 3.0)])
    cur[int(1.5 * RATE) : int(2.5 * RATE)] = np.nan  # dropout in the middle of a wake
    _, out = run(cur, SessionConfig(kind="soak", hold_s=0.1), tmp_path)
    assert len(out["rollups"]) == 4 * 100 - 100
    assert len([e for e in out["events"] if e["kind"] == "wake"]) == 1


def test_uart_lines_on_session_timeline(tmp_path):
    log = LineLog()
    log.add(1000.0, 0.0, 5_000_000 + 12_345, "hello")
    log.add(1001.0, 0.0, None, "no stream")
    _, out = run(profile(1), SessionConfig(kind="soak"), tmp_path, s0=5_000_000, uart=log)
    (a, b) = out["uart"]
    assert a[2] == 12_345 and (a[1] - T0).total_seconds() == pytest.approx(0.12345)
    assert b[2] is None and b[1] == datetime.fromtimestamp(1001.0, timezone.utc)


def test_falling_behind_ring_gives_gap(tmp_path):
    ring = RingBuffer(2 * RATE)
    rec = SessionRecorder(1, T0, 0, 0, SessionConfig(kind="detail"), ring, LineLog(), -1, tmp_path, 0)
    ring.append(profile(5), np.zeros(5 * RATE, np.uint8))  # 5 s arrive, buffer holds only 2 s
    batch = rec.tick()
    (gap,) = [e for e in batch["events"] if e["kind"] == "gap"]
    assert gap["start_idx"] == 0 and gap["end_idx"] == 3 * RATE
    assert rec.cursor == 5 * RATE


def test_raw_stream_windows():
    s = RawStream("event")
    s.open(100)
    assert s.pump(150) == []
    s.close(200)
    assert s.pump(180) == []
    s.open(190)  # new wake before close: extend
    assert s.pump(300) == []
    s.close(310)
    assert s.pump(400) == [(100, 310)]
    assert not s.active


def test_round_mantissa():
    from capture.rawstore import round_mantissa
    x = np.r_[np.abs(np.random.default_rng(0).normal(1e-3, 5e-4, 10_000)), -2.5e-7, 0.0, np.nan].astype(np.float32)
    r = round_mantissa(x, 12)
    ok = ~np.isnan(x)
    assert np.isnan(r[~ok]).all()
    assert np.max(np.abs(r[ok] - x[ok]) / np.maximum(np.abs(x[ok]), 1e-30)) <= 2.0 ** -13
    assert (r.view(np.uint32)[ok] & 0x7FF).max() == 0  # the 11 dropped bits are zero
    np.testing.assert_array_equal(round_mantissa(x, 23), x)
