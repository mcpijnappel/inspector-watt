"""End-to-end test of the API against the simulated PPK2 + UART."""

import time

import pytest
from fastapi.testclient import TestClient

from capture.api import create_app
from capture.config import Config


@pytest.fixture
def client():
    cfg = Config(ppk2_port="sim://", uart_port="sim://", ring_seconds=10, max_voltage_mv=4200, auto_stream=False)
    with TestClient(create_app(cfg)) as c:
        deadline = time.time() + 5
        while not c.get("/status").json()["ppk2"]["connected"]:
            assert time.time() < deadline
            time.sleep(0.05)
        yield c


def test_dut_power_requires_voltage_first(client):
    r = client.post("/ppk2/dut", json={"on": True})
    assert r.status_code == 409


def test_voltage_limit(client):
    assert client.post("/ppk2/source", json={"voltage_mv": 4300}).status_code == 409
    assert client.post("/ppk2/source", json={"voltage_mv": 3700}).json()["voltage_mv"] == 3700


def test_stream_and_live(client):
    client.post("/ppk2/source", json={"voltage_mv": 3700})
    client.post("/ppk2/dut", json={"on": True})
    client.post("/ppk2/stream", json={"on": True})
    time.sleep(0.6)
    st = client.get("/status").json()
    assert st["ppk2"]["streaming"] and st["ppk2"]["dut_power"]
    assert st["ppk2"]["stream"]["samples"] > 30_000
    assert st["ppk2"]["stream"]["lost"] == 0

    end = st["buffer"]["end_idx"]
    w = client.get("/buffer/window", params={"start": 0, "end": end, "points": 100}).json()
    assert w["bin_samples"] > 1 and len(w["max"]) <= 100
    # sim wakes at t=0 for 0.5 s at ~4 mA, radio burst 20 mA at 0.2-0.25 s
    assert max(v for v in w["max"] if v is not None) > 15e-3

    raw = client.get("/buffer/window", params={"start": 0, "end": 50}).json()
    assert raw["bin_samples"] == 1 and len(raw["current"]) == 50

    with client.websocket_connect("/live?bin_us=1000") as ws:
        types = set()
        for _ in range(10):
            msg = ws.receive_json()
            types.add(msg["type"])
            if msg["type"] == "ppk":
                assert msg["bin_samples"] == 100
        assert {"ppk", "status"} <= types

    assert client.get("/uart/lines").status_code == 200
    client.post("/ppk2/stream", json={"on": False})
    assert client.get("/status").json()["ppk2"]["streaming"] is False


def test_stream_resumes_after_replug(client):
    mgr = client.app.state.svc.ppk2
    client.post("/ppk2/stream", json={"on": True})
    time.sleep(0.3)
    mgr._close()  # what an unplug looks like to the reader thread
    time.sleep(0.5)
    deadline = time.time() + 5
    while not mgr.streaming:
        assert time.time() < deadline
        time.sleep(0.05)
    time.sleep(0.3)
    st = client.get("/status").json()["ppk2"]
    assert st["stream"]["reconnects"] == 1 and st["stream"]["lost"] > 0
    assert st["stream"]["stalls"] == 0
    assert st["dut_power"] is None
    # timeline stays continuous: buffer end ~ elapsed time since stream start
    end = client.get("/status").json()["buffer"]["end_idx"]
    assert abs(end / 100_000 - st["stream"]["elapsed_s"]) < 0.2


def test_stalled_stream_reconnects(client):
    mgr = client.app.state.svc.ppk2
    resumed = []
    mgr.on_resume = lambda start, gap, reason: resumed.append(reason)
    client.post("/ppk2/stream", json={"on": True})
    time.sleep(0.3)
    mgr._ser.stalled = True  # firmware hangs: no data, no error
    deadline = time.time() + 8
    while not resumed:
        assert time.time() < deadline
        time.sleep(0.1)
    st = client.get("/status").json()["ppk2"]
    assert resumed == ["ppk2_stall"] and st["stream"]["stalls"] == 1 and st["streaming"]


def test_session_requires_database(client):
    # The sim test app has no DATABASE_URL: session endpoints say so instead of failing.
    assert client.post("/sessions", json={"name": "x", "kind": "detail"}).status_code == 503


def test_power_up_skips_commands_when_already_set(client, tmp_path):
    from capture.recorder import SessionConfig, SessionRecorder
    from capture.sessions import SessionManager
    svc = client.app.state.svc
    client.post("/ppk2/source", json={"voltage_mv": 3700})
    client.post("/ppk2/dut", json={"on": True})
    sent = []
    link = svc.ppk2._link
    orig = link.send
    link.send = lambda payload: (sent.append(payload), orig(payload))
    mgr = SessionManager(svc, None, tmp_path, 0)
    rec = SessionRecorder(1, None, 0, 0, SessionConfig(voltage_mv=3700), svc.ring, svc.uart.log, -1, tmp_path, 0)
    mgr._power_up(rec, "power_on")
    assert sent == [] and rec._notes[0]["kind"] == "dut_kept"
    rec2 = SessionRecorder(1, None, 0, 0, SessionConfig(voltage_mv=3300), svc.ring, svc.uart.log, -1, tmp_path, 0)
    mgr._power_up(rec2, "power_on")
    assert len(sent) == 3 and rec2._notes[0]["kind"] == "power_on"  # voltage, source mode, board on


def test_auto_stream_starts_measuring_on_connect():
    cfg = Config(ppk2_port="sim://", uart_port="sim://", ring_seconds=10, auto_stream=True)
    with TestClient(create_app(cfg)) as c:
        deadline = time.time() + 5
        while not c.get("/status").json()["ppk2"]["streaming"]:
            assert time.time() < deadline
            time.sleep(0.05)
        st = c.get("/status").json()["ppk2"]["stream"]
        assert st["reconnects"] == 0


def test_idle_power_off(monkeypatch):
    from capture.service import IdlePowerOff

    monkeypatch.setattr(IdlePowerOff, "CHECK_S", 0.05)
    cfg = Config(ppk2_port="sim://", uart_port="sim://", ring_seconds=10, max_voltage_mv=4200, auto_stream=False,
                 idle_power_off_s=1)
    with TestClient(create_app(cfg)) as c:
        deadline = time.time() + 5
        while not c.get("/status").json()["ppk2"]["connected"]:
            assert time.time() < deadline
            time.sleep(0.05)
        c.post("/ppk2/source", json={"voltage_mv": 3700})
        assert c.post("/ppk2/dut", json={"on": True}).json()["dut_changed"]["by"] == "user"

        # A viewer polling keeps it on.
        for _ in range(15):
            st = c.get("/status", params={"viewer": True}).json()
            time.sleep(0.1)
        assert st["ppk2"]["dut_power"] is True
        assert 0 < st["idle_power_off"]["in_s"] <= 1

        # Nobody watching: off within a second or so.
        time.sleep(1.4)
        st = c.get("/status").json()
        assert st["ppk2"]["dut_power"] is False
        assert st["ppk2"]["dut_changed"]["by"] == "idle"
        assert st["idle_power_off"]["in_s"] is None


def test_device_limit(client):
    st = client.put("/ppk2/device", json={"id": 7, "name": "3V3 board", "max_mv": 3600}).json()
    assert st["max_voltage_mv"] == 3600 and st["hard_max_mv"] == 4200 and st["device"]["name"] == "3V3 board"
    r = client.post("/ppk2/source", json={"voltage_mv": 3700})
    assert r.status_code == 409 and "3V3 board" in r.json()["detail"]
    assert client.post("/ppk2/source", json={"voltage_mv": 3300}).status_code == 200
    # never above the tracker's limit, whatever the device says
    assert client.put("/ppk2/device", json={"id": 8, "name": "big", "max_mv": 5000}).json()["max_voltage_mv"] == 4200
    # no device: the tracker's limit only
    assert client.put("/ppk2/device", json={}).json()["device"] is None


def test_device_sets_the_uart_speed(client):
    assert client.get("/status").json()["uart"]["baud"] == 115200  # until a board is on the bench
    client.put("/ppk2/device", json={"id": 7, "name": "3V3 board", "max_mv": 3600, "uart_baud": 921600})
    assert client.get("/status").json()["uart"]["baud"] == 921600
    client.put("/ppk2/device", json={"id": 1, "name": "XIAO", "max_mv": 4200, "uart_baud": 115200})
    assert client.get("/status").json()["uart"]["baud"] == 115200
    assert client.put("/ppk2/device", json={"id": 2, "name": "x", "max_mv": 4200, "uart_baud": 7}).status_code == 422
    assert "uart_baud" not in client.get("/settings").json()["fixed"]


def test_lower_limit_switches_dut_off(client):
    client.post("/ppk2/source", json={"voltage_mv": 4000})
    client.post("/ppk2/dut", json={"on": True})
    st = client.put("/ppk2/device", json={"id": 1, "name": "LiFePO4 board", "max_mv": 3650}).json()
    assert st["dut_power"] is False and st["dut_changed"]["by"] == "limit"
    # at or under the limit nothing happens
    client.post("/ppk2/source", json={"voltage_mv": 3300})
    client.post("/ppk2/dut", json={"on": True})
    assert client.put("/ppk2/device", json={"id": 1, "name": "LiFePO4 board", "max_mv": 3600}).json()["dut_power"] is True


def test_settings_change_while_running(monkeypatch):
    from capture.service import IdlePowerOff

    monkeypatch.setattr(IdlePowerOff, "CHECK_S", 0.05)
    cfg = Config(ppk2_port="sim://", uart_port="sim://", ring_seconds=10, max_voltage_mv=4200, auto_stream=False,
                 idle_power_off_s=0)  # off at start: the check still runs, so turning it on later works
    with TestClient(create_app(cfg)) as c:
        deadline = time.time() + 5
        while not c.get("/status").json()["ppk2"]["connected"]:
            assert time.time() < deadline
            time.sleep(0.05)
        s = c.get("/settings").json()
        assert s["idle_power_off_s"] == 0 and s["spike_filter"] is True
        assert s["max_voltage_mv"] == 4200 and s["fixed"]["ring_seconds"] == 10 and s["auto_stream"] is False
        assert s["min_free_gb"] == 10 and s["raw_mantissa_bits"] == 12 and "max_voltage_mv" not in s["fixed"]
        assert c.put("/settings", json={"idle_power_off_s": 100000}).status_code == 422
        c.post("/ppk2/source", json={"voltage_mv": 3700})
        c.post("/ppk2/dut", json={"on": True})
        time.sleep(0.5)
        assert c.get("/status").json()["ppk2"]["dut_power"] is True  # never switched off with 0

        s = c.put("/settings", json={"idle_power_off_s": 1, "spike_filter": False}).json()
        assert s["idle_power_off_s"] == 1 and s["spike_filter"] is False
        assert c.get("/status").json()["ppk2"]["spike_filter"] is False
        time.sleep(1.6)
        st = c.get("/status").json()
        assert st["ppk2"]["dut_power"] is False and st["ppk2"]["dut_changed"]["by"] == "idle"
        assert c.put("/settings", json={}).json()["idle_power_off_s"] == 1  # nothing given: nothing changes


def test_voltage_limit_is_a_setting(client):
    client.post("/ppk2/source", json={"voltage_mv": 4000})
    client.post("/ppk2/dut", json={"on": True})
    s = client.put("/settings", json={"max_voltage_mv": 3600}).json()
    assert s["max_voltage_mv"] == 3600
    st = client.get("/status").json()["ppk2"]
    assert st["hard_max_mv"] == 3600 and st["max_voltage_mv"] == 3600
    assert st["dut_power"] is False and st["dut_changed"]["by"] == "limit"  # 4.0 V was above the new limit
    r = client.post("/ppk2/source", json={"voltage_mv": 3700})
    assert r.status_code == 409 and "the tracker's limit" in r.json()["detail"]
    assert client.put("/settings", json={"max_voltage_mv": 5100}).status_code == 422
    # a device's own limit still counts, under the tracker's
    assert client.put("/ppk2/device", json={"id": 1, "name": "b", "max_mv": 3300}).json()["max_voltage_mv"] == 3300
    s = client.put("/settings", json={"auto_stream": True, "min_free_gb": 20, "raw_mantissa_bits": 16}).json()
    assert s["auto_stream"] is True
