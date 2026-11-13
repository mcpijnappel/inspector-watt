import pytest

from datetime import datetime, timezone

from capture.config import Config
from capture.recorder import new_batch
from capture.service import CaptureService
from capture.sessions import SessionManager
from capture.store import StoreUnavailable


class FakeStore:
    def __init__(self):
        self.up = False
        self.written = []

    def write_batch(self, session_id, batch):
        if not self.up:
            raise StoreUnavailable("down")
        self.written.append((session_id, batch))


def test_spool_survives_until_database_is_back(tmp_path):
    store = FakeStore()
    mgr = SessionManager(CaptureService(Config(ppk2_port="sim://", uart_port="sim://", ring_seconds=1)),
                         store, tmp_path, 0)
    batch = new_batch()
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    batch["rollups"].append((7, ts, 1e-6, 2e-6, 1.5e-3, 1000, 0))
    mgr._spool(7, batch)
    mgr._replay_spool()  # database still down: file stays
    assert len(list(mgr.spool_dir.glob("*.pkl"))) == 1 and store.written == []
    store.up = True
    mgr._replay_spool()
    assert list(mgr.spool_dir.glob("*.pkl")) == []
    assert store.written == [(7, batch)] and store.written[0][1]["rollups"][0][1] == ts


def _manager_with_ppk2_state(tmp_path, mode_source=True, mv=3700):
    from capture.ppk2 import PowerMode
    svc = CaptureService(Config(ppk2_port="sim://", uart_port="sim://", ring_seconds=2))
    svc.ppk2.mode = PowerMode.SOURCE if mode_source else PowerMode.AMPERE
    svc.ppk2.voltage_mv = mv
    return svc, SessionManager(svc, FakeStore(), tmp_path, 0)


def _feed_later(ring, current_a):
    import threading
    import time

    import numpy as np

    def run():
        time.sleep(0.1)
        ring.append(np.full(60_000, current_a, np.float32), np.zeros(60_000, np.uint8))
    threading.Thread(target=run, daemon=True).start()


def _rec(cfg, charge_c=0.0, samples=0):
    from types import SimpleNamespace
    return SimpleNamespace(cfg=cfg, charge_c=charge_c, processed_samples=samples)


def test_dut_still_powered_is_detected(tmp_path):
    from capture.recorder import SessionConfig
    svc, mgr = _manager_with_ppk2_state(tmp_path)
    _feed_later(svc.ring, 1.5e-3)  # sleeping XIAO
    assert mgr._dut_still_powered(_rec(SessionConfig(voltage_mv=3700))) == pytest.approx(1.5e-3, rel=1e-6)


def test_dut_off_or_other_voltage_needs_restore(tmp_path):
    from capture.recorder import SessionConfig
    svc, mgr = _manager_with_ppk2_state(tmp_path)
    _feed_later(svc.ring, 0.15e-6)  # PPK2 output off: noise floor
    assert mgr._dut_still_powered(_rec(SessionConfig(voltage_mv=3700))) is None
    svc, mgr = _manager_with_ppk2_state(tmp_path, mv=4200)
    assert mgr._dut_still_powered(_rec(SessionConfig(voltage_mv=3700))) is None  # no need to even measure


class RecordingStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.up = True
        self.created = []
        self.finished = []

    def ping(self):
        return True

    def create_session(self, name, kind, started_at, config, notes):
        self.created.append((name, kind, config))
        return 42

    def finish_session(self, sid, status, stopped_at):
        self.finished.append((sid, status))


def test_save_range_from_live_buffer(tmp_path):
    import numpy as np
    from capture.uart import LineLog
    svc = CaptureService(Config(ppk2_port="sim://", uart_port="sim://", ring_seconds=10))
    store = RecordingStore()
    mgr = SessionManager(svc, store, tmp_path, 0)
    svc.clock.observe(1000.0, 0)  # pretend the stream started
    svc.ring.append(np.full(500_000, 2e-3, np.float32), np.zeros(500_000, np.uint8))  # 5 s
    svc.uart.log = LineLog()
    for idx, text in [(50_000, "before"), (150_000, "inside 1"), (250_000, "inside 2"), (450_000, "after")]:
        svc.uart.log.add(0, 0, idx, text)
    out = mgr.save_range("kept", 100_000, 400_000)
    assert out["saved"] == 42 and abs(out["duration_s"] - 3.0) < 1e-9
    assert store.finished == [(42, "stopped")]
    sid, batch = store.written[0]
    assert len(batch["rollups"]) == 300
    assert [u[4] for u in batch["uart"]] == ["inside 1", "inside 2"]
    assert batch["uart"][0][2] == 50_000  # session sample index
    assert sum(s["n_samples"] for s in batch["segments"]) == 300_000
    assert store.created[0][2]["saved_from_buffer"] is True


def test_wake_feed_for_modules(tmp_path):
    """The analyzer learns the recording's wakes (unix times) from the feed, and its events for the last wake are
    still stored for a while after the recording stopped."""
    import time

    import numpy as np
    from capture.recorder import SessionConfig
    from capture.sessions import Active
    svc = CaptureService(Config(ppk2_port="sim://", uart_port="sim://", ring_seconds=10))
    store = RecordingStore()
    mgr = SessionManager(svc, store, tmp_path, 0)
    assert mgr.wake_feed() == {"recording": None, "wakes": [], "last_seq": 0}
    svc.clock.observe(time.monotonic(), 0)  # sample 0 is now
    cfg = SessionConfig(kind="detail", wake_threshold_a=1e-3, hold_s=0.05)
    rec = mgr._recorder(42, datetime.now(timezone.utc), 0, 0, cfg)
    mgr.active = Active(42, "t", "detail", rec.started_at, cfg, rec)
    def feed(seconds, amps):
        n = int(seconds * 100_000)
        svc.ring.append(np.full(n, amps, np.float32), np.zeros(n, np.uint8))
    feed(1.0, 2e-6)
    feed(0.2, 5e-3)  # a wake at 1.0-1.2 s
    feed(0.5, 2e-6)
    feed(0.3, 5e-3)  # a wake that's still going on
    b = rec.tick()
    mgr._note_wakes(mgr.active, b)
    t0 = mgr._wall_t(0)
    f = mgr.wake_feed()
    assert f["recording"]["id"] == 42 and f["recording"]["stopped_t"] is None
    assert abs(f["recording"]["started_t"] - t0) < 1e-6
    assert abs(f["recording"]["processed_t"] - t0 - 2.0) < 1e-6
    (w,) = f["wakes"]
    assert abs(w["start_t"] - t0 - 1.0) < 1e-6 and abs(w["end_t"] - t0 - 1.2) < 1e-6
    assert abs(f["open_start_t"] - t0 - 1.7) < 1e-6
    assert mgr.wake_feed(f["last_seq"])["wakes"] == []  # only what's new
    mgr.stop_session()
    f = mgr.wake_feed(f["last_seq"])
    assert f["recording"]["id"] == 42 and abs(f["recording"]["stopped_t"] - t0 - 2.0) < 1e-6
    assert len(f["wakes"]) == 1 and f["open_start_t"] is None  # stopping closed the open wake
    # the analyzer's messages for that last wake arrive after the stop: still stored with the recording
    assert mgr.note_event("i2c", 180_000, 180_050, module="analyzer:1", addr=0x44)
    assert not mgr.note_event("i2c", 250_000, None, module="analyzer:1")  # after the stop: not part of it
    assert not mgr.note_event("flash", 180_000)  # only modules' events
    mgr._flush_late()
    sid, batch = store.written[-1]
    (ev,) = batch["events"]
    assert sid == 42 and ev["module"] == "analyzer:1" and ev["start_idx"] == 180_000 and ev["data"] == {"addr": 0x44}
    mgr._ended.until = 0  # later on: forgotten
    assert mgr.wake_feed()["recording"] is None and not mgr.note_event("i2c", 180_000, module="analyzer:1")
