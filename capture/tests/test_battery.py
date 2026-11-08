"""Battery emulation: discharge curve, drain modes, cutoff behaviour, and the session manager stepping."""

from types import SimpleNamespace

import pytest

from capture import battery
from capture.battery import MAH, BatteryConfig, soc_to_mv, state
from capture.config import Config
from capture.recorder import SessionConfig
from capture.service import CaptureService
from capture.sessions import Active, SessionManager


def test_curve():
    assert soc_to_mv(100) == 4200 and soc_to_mv(0) == 3000 and soc_to_mv(50) == 3840
    assert soc_to_mv(52.5) == pytest.approx(3845)
    assert soc_to_mv(-1) < 3000  # hunt mode: keeps falling below empty


def test_charge_mode_with_speedup():
    cfg = BatteryConfig(capacity_mah=1000, speedup=50)  # emulated: 20 mAh = 72 C
    st = state(cfg, charge_c=36.0, elapsed_s=3600)  # half drawn in an hour
    assert st.soc == pytest.approx(50) and st.voltage_mv == 3840 and not st.done
    assert st.eta_s == pytest.approx(3600)  # the other half at the same rate
    assert state(cfg, 20 * MAH, 7200).done  # empty: protect stops at the cutoff


def test_hunt_goes_below_cutoff_to_floor():
    cfg = BatteryConfig(capacity_mah=100, on_cutoff="hunt", cutoff_mv=3000, floor_mv=2500)
    st = state(cfg, 100 * MAH * 1.01, 100)  # 1 % past empty
    assert st.below_cutoff and not st.done and 2500 < st.voltage_mv < 3000
    st = state(cfg, 100 * MAH * 1.2, 100)
    assert st.done and st.voltage_mv == 2500


def test_time_sweep():
    cfg = BatteryConfig(mode="time", start_mv=4200, cutoff_mv=3000, sweep_min=60)
    assert state(cfg, 0, 0).voltage_mv == 4200
    st = state(cfg, 0, 1800)
    assert st.voltage_mv == 3600 and st.soc is None and st.eta_s == pytest.approx(1800)
    assert state(cfg, 0, 3600).done


def test_check():
    BatteryConfig().check(4200)
    with pytest.raises(ValueError):
        BatteryConfig(mode="time", start_mv=4300).check(4200)  # above MAX_VOLTAGE_MV
    with pytest.raises(ValueError):
        BatteryConfig(floor_mv=3100, cutoff_mv=3000).check(4200)


class FakePPK2:
    def __init__(self, mv):
        self.voltage_mv = mv
        self.dut_power = True
        self.set = []
        self.cfg = SimpleNamespace(max_voltage_mv=4200)

    def adjust_voltage(self, mv):
        self.set.append(mv)
        self.voltage_mv = mv


class FakeRec:
    def __init__(self, cfg):
        self.cfg = cfg
        self.charge_c = 0.0
        self.processed_samples = 0
        self.notes = []

    def note(self, kind, idx, end=None, **data):
        self.notes.append((kind, data))


def _manager(tmp_path, bat: BatteryConfig):
    svc = CaptureService(Config(ppk2_port="sim://", uart_port="sim://", ring_seconds=1))
    mgr = SessionManager(svc, None, tmp_path, 0)
    svc.ppk2 = FakePPK2(4200)
    rec = FakeRec(SessionConfig(voltage_mv=4200, battery=bat.to_dict()))
    mgr.active = Active(1, "bat", "soak", None, rec.cfg, rec)
    stops = []
    mgr.stop_session = lambda dut_off=False: stops.append(dut_off)
    return mgr, svc.ppk2, rec, stops


def test_session_steps_voltage_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr("capture.sessions.BATTERY_SET_EVERY_S", 0)
    mgr, ppk2, rec, stops = _manager(tmp_path, BatteryConfig(capacity_mah=10))  # 36 C
    rec.charge_c, rec.processed_samples = 18.0, 100_000  # 50 %
    mgr._battery_tick()
    assert ppk2.set == [3840] and rec.notes[-1][0] == "battery"
    rec.charge_c = 18.1  # a few mV lower: voltage follows, but no new event (< 10 mV)
    mgr._battery_tick()
    assert len(ppk2.set) == 2 and ppk2.set[-1] < 3840 and len(rec.notes) == 1
    assert mgr.battery_status()["soc"] == pytest.approx(100 - 18.1 / 36 * 100)
    rec.charge_c = 36.0  # empty: protect = DUT off + stop
    mgr._battery_tick()
    assert stops == [True] and rec.notes[-1][0] == "battery_end" and rec.notes[-1][1]["reason"] == "cutoff"


def test_restart_keeps_battery_voltage_within_tolerance(tmp_path):
    from capture.ppk2 import PowerMode
    svc = CaptureService(Config(ppk2_port="sim://", uart_port="sim://", ring_seconds=2))
    svc.ppk2.mode = PowerMode.SOURCE
    svc.ppk2.voltage_mv = 3860  # what the PPK2 still outputs after the restart
    mgr = SessionManager(svc, None, tmp_path, 0)
    rec = SimpleNamespace(cfg=SessionConfig(voltage_mv=4200, battery=BatteryConfig(capacity_mah=10).to_dict()),
                          charge_c=18.0, processed_samples=0)  # target 3840 mV
    assert mgr._wanted_mv(rec) == 3840
    import threading
    import time

    import numpy as np

    def feed():
        time.sleep(0.1)
        svc.ring.append(np.full(60_000, 2e-3, np.float32), np.zeros(60_000, np.uint8))
    threading.Thread(target=feed, daemon=True).start()
    assert mgr._dut_still_powered(rec) == pytest.approx(2e-3, rel=1e-6)  # 20 mV off: no reboot needed
    assert battery.BatteryConfig.from_dict(None) is None


def test_chemistries():
    assert soc_to_mv(50, "nimh", 3) == 3750  # LADDA x3: 1.25 V per cell
    assert soc_to_mv(100, "alkaline", 3) == 4740
    # LiFePO4 is flat: 20-80 % within 0.1 V, while LiPo moves ~0.3 V
    assert soc_to_mv(80, "lifepo4") - soc_to_mv(20, "lifepo4") < 110
    assert soc_to_mv(80) - soc_to_mv(20) > 250
    # a fresh 3xAA alkaline pack is above a 4.2 V limit: start lower
    with pytest.raises(ValueError, match="lower charge"):
        BatteryConfig(chemistry="alkaline", cells=3, cutoff_mv=2700, floor_mv=2400).check(4200)
    BatteryConfig(chemistry="alkaline", cells=3, start_soc=75, cutoff_mv=2700, floor_mv=2400).check(4200)
    with pytest.raises(ValueError):
        BatteryConfig(chemistry="lipo", cells=2).check(5000)  # LiPo is 1 cell here
    st = state(BatteryConfig(chemistry="nimh", cells=3, capacity_mah=10, cutoff_mv=3000, floor_mv=2700), 18.0, 100)
    assert st.soc == pytest.approx(50) and st.voltage_mv == 3750
    # old sessions without a chemistry: LiPo
    assert BatteryConfig.from_dict({"mode": "charge"}).chemistry == "lipo"
    assert {c["id"] for c in battery.chemistries()} >= {"lipo", "lifepo4", "nimh", "alkaline"}
