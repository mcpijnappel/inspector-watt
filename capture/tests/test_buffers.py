import numpy as np

from capture.clocksync import ClockSync
from capture.ringbuffer import RingBuffer, bin_stats, to_json_list
from capture.uart import LineLog, LineSplitter


def test_ring_wraps_and_reads_by_absolute_index():
    r = RingBuffer(10)
    for k in range(4):
        r.append(np.arange(k * 4, k * 4 + 4, dtype=np.float32), np.zeros(4, np.uint8))
    assert (r.start, r.end) == (6, 16)
    start, cur, _ = r.read(0, 100)
    assert start == 6 and cur.tolist() == list(range(6, 16))
    _, cur, _ = r.read(8, 12)
    assert cur.tolist() == [8, 9, 10, 11]


def test_ring_append_larger_than_capacity():
    r = RingBuffer(5)
    r.append(np.arange(12, dtype=np.float32), np.zeros(12, np.uint8))
    assert r.end == 12
    assert r.read(0, 12)[1].tolist() == [7, 8, 9, 10, 11]


def test_ring_skip_fills_nan():
    r = RingBuffer(10)
    r.append(np.arange(8, dtype=np.float32), np.ones(8, np.uint8))
    r.skip(4)
    assert r.end == 12
    _, cur, lg = r.read(6, 12)
    assert cur[:2].tolist() == [6, 7] and np.isnan(cur[2:]).all() and lg[2:].tolist() == [0] * 4
    r.skip(1000)
    assert r.end == 1012 and np.isnan(r.read(0, 2000)[1]).all()


def test_bin_stats_nan_aware():
    cur = np.array([1, 3, np.nan, 2, np.nan, np.nan, 9], np.float32)
    mn, mx, mean, lor = bin_stats(cur, np.array([1, 2, 0, 4, 0, 0, 0], np.uint8), 2)
    assert mn.tolist()[:2] == [1, 2] and mx.tolist()[:2] == [3, 2]
    assert np.isnan(mn[2])  # all-NaN bin stays NaN
    assert lor.tolist() == [3, 4, 0]
    assert to_json_list(mn)[2] is None


def test_line_splitter_timestamps_first_byte_and_flushes_partial():
    s = LineSplitter(idle_s=0.5)
    assert s.feed(b"hel", 1.0, 100.0) == []
    out = s.feed(b"lo\r\nwor", 1.1, 100.1)
    assert out == [(1.0, 100.0, b"hello\r\n")]
    assert s.flush_stale(1.3) == []
    assert s.flush_stale(1.7) == [(1.1, 100.1, b"wor")]


def test_line_log_after():
    log = LineLog(maxlen=3)
    for i in range(5):
        log.add(0, 0, None, f"l{i}")
    assert [ln.text for ln in log.after(-1)] == ["l2", "l3", "l4"]
    assert [ln.text for ln in log.after(3)] == ["l4"]
    assert log.after(4) == []


def test_clock_sync_uses_min_latency_and_tracks_drift():
    c = ClockSync(window_s=10)
    rng = np.random.default_rng(0)
    # PPK2 runs 20 ppm slow: sample n happens at host time 5 + n * 1e-5 * (1 + 20e-6)
    for k in range(1, 6001):  # 10 reads/s for 600 s
        idx = k * 10_000
        true_t = 5 + idx * 1e-5 * (1 + 20e-6)
        c.observe(true_t + 0.001 + abs(rng.normal(0, 0.003)), idx)
    true_now = 5 + 60_000_000 * 1e-5 * (1 + 20e-6)
    assert abs(c.index_at(true_now) - 60_000_000) < 200  # < 2 ms error
    assert abs(c.drift_ppm - 20) < 1


def test_line_splitter_backdates_lines_within_one_read():
    s = LineSplitter(byte_time_s=0.001)
    out = s.feed(b"aaaa\nbb\n", 10.0, 100.0)  # 8 bytes, last byte arrived at t=10.0
    assert [round(t, 6) for t, _, _ in out] == [9.993, 9.998]
    assert [raw for _, _, raw in out] == [b"aaaa\n", b"bb\n"]
