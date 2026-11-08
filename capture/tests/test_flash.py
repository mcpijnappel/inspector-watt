import base64
import time
import types

import pytest
from fastapi.testclient import TestClient

from capture.api import create_app
from capture.config import Config
from capture.flash import Flasher, FlashError, FlashRequest, Image


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out"
        time.sleep(0.02)


@pytest.fixture
def client():
    cfg = Config(ppk2_port="sim://", uart_port="sim://", usb_port="sim://", ring_seconds=10, max_voltage_mv=4200,
                 auto_stream=False)
    with TestClient(create_app(cfg)) as c:
        wait_for(lambda: c.get("/status").json()["ppk2"]["connected"])
        yield c


def body(**kw):
    return {"chip": "esp32c6", "flash_mode": "dio", "flash_freq": "80m", "flash_size": "4MB",
            "images": [{"offset": 0x0, "data_b64": base64.b64encode(b"boot" * 100).decode()},
                       {"offset": 0x10000, "data_b64": base64.b64encode(b"app!" * 1000).decode()}],
            "meta": {"project": "blink", "env": "xiao", "git": "abc123", "junk": "dropped"}, **kw}


def test_flash_awake_dut(client):
    j = client.post("/flash", json=body()).json()
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "done")
    j = client.get(f"/flash/{j['id']}").json()
    assert j["progress"] == 100 and not j["power_cycled"]
    assert j["meta"] == {"project": "blink", "env": "xiao", "git": "abc123"}
    assert any("Wrote" in ln for ln in j["log"])
    assert client.get("/status").json()["flash"]["status"] == "done"
    assert j["start_idx"] is not None and j["end_idx"] >= j["start_idx"]
    jobs = client.get("/flash/jobs").json()
    assert jobs[0]["id"] == j["id"] and "log" not in jobs[0]


def asleep_and_on(client):
    svc = client.app.state.svc
    svc.usb.sim_factory.present = False
    client.post("/ppk2/source", json={"voltage_mv": 3700})
    client.post("/ppk2/dut", json={"on": True})
    return svc


def test_flash_asleep_dut_power_cycles(client):
    asleep_and_on(client)
    j = client.post("/flash", json=body(when_asleep="cycle")).json()
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "done")
    j = client.get(f"/flash/{j['id']}").json()
    assert j["power_cycled"]
    st = client.get("/status").json()["ppk2"]
    assert st["dut_power"] and st["dut_changed"]["by"] == "flash"


def test_flash_waits_for_the_next_wake(client):
    svc = asleep_and_on(client)
    j = client.post("/flash", json=body(when_asleep="wait")).json()
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "waiting_wake")
    assert client.get(f"/flash/{j['id']}").json()["wait_until"] is not None
    svc.usb.sim_factory.present = True  # it wakes up by itself
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "done")
    assert not client.get(f"/flash/{j['id']}").json()["power_cycled"]


def test_flash_power_cycles_when_no_wake_comes_or_on_request(client):
    svc = asleep_and_on(client)
    svc.flash_wake_wait_s = 0.5
    j = client.post("/flash", json=body()).json()  # the tracker's default: wait
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "done")
    assert client.get(f"/flash/{j['id']}").json()["power_cycled"]
    svc.usb.sim_factory.present = False
    svc.flash_wake_wait_s = 600
    j = client.post("/flash", json=body()).json()
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "waiting_wake")
    assert client.post(f"/flash/{j['id']}/now").status_code == 200
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "done")
    assert client.get(f"/flash/{j['id']}").json()["power_cycled"]


def test_always_wait_never_power_cycles(client):
    svc = asleep_and_on(client)
    svc.flash_wake_wait_s = 0.3  # ignored by "always"
    j = client.post("/flash", json=body(when_asleep="always")).json()
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "waiting_wake")
    time.sleep(0.8)
    d = client.get(f"/flash/{j['id']}").json()
    assert d["status"] == "waiting_wake" and d["wait_until"] is None
    svc.usb.sim_factory.present = True
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "done")
    assert not client.get(f"/flash/{j['id']}").json()["power_cycled"]
    client.post("/ppk2/dut", json={"on": False})  # off: it would never wake
    svc.usb.sim_factory.present = False
    j = client.post("/flash", json=body(when_asleep="always")).json()
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "failed")
    assert "only waits" in client.get(f"/flash/{j['id']}").json()["error"]


def test_waiting_upload_can_be_cancelled_or_replaced(client):
    svc = asleep_and_on(client)
    svc.flash_wake_wait_s = 600
    a = client.post("/flash", json=body()).json()
    wait_for(lambda: client.get(f"/flash/{a['id']}").json()["status"] == "waiting_wake")
    b = client.post("/flash", json=body()).json()  # a newer build replaces the waiting one
    a = client.get(f"/flash/{a['id']}").json()
    assert a["status"] == "cancelled" and "replaced by upload" in a["error"]
    wait_for(lambda: client.get(f"/flash/{b['id']}").json()["status"] == "waiting_wake")
    assert client.post(f"/flash/{b['id']}/cancel").status_code == 200
    wait_for(lambda: client.get(f"/flash/{b['id']}").json()["status"] == "cancelled")
    wait_for(lambda: not svc.usb.held)  # the port is free again
    assert client.post(f"/flash/{b['id']}/cancel").status_code == 409  # not waiting any more


def test_flash_asleep_without_tracker_power_fails(client):
    client.app.state.svc.usb.sim_factory.present = False
    j = client.post("/flash", json=body(when_asleep="cycle")).json()
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "failed")
    assert "switch the DUT on" in client.get(f"/flash/{j['id']}").json()["error"]


def test_one_flash_at_a_time_and_bad_requests(client):
    assert client.post("/flash", json=body(chip="avr")).status_code == 409
    assert client.post("/flash", json=body(images=[{"offset": 0x123, "data_b64": "AAAA"}])).status_code == 409
    assert client.post("/flash", json=body(images=[{"offset": 0, "data_b64": "not base64!"}])).status_code == 409
    assert client.post("/flash", json=body(when_asleep="later")).status_code == 409
    client.post("/flash", json=body())
    assert client.post("/flash", json=body()).status_code == 409


def test_console_is_released_during_a_flash_and_reopened(client):
    svc = client.app.state.svc
    client.post("/usb/open", json={"on": True})
    wait_for(lambda: svc.usb.connected)
    j = client.post("/flash", json=body()).json()
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "done")
    wait_for(lambda: svc.usb.connected)  # console still wanted: open again for the new firmware's menu
    assert client.post("/usb/key", json={"key": "n"}).status_code == 200


class FakeSessions:
    def __init__(self):
        self.active = types.SimpleNamespace(id=7, name="soak")
        self.events = []
        self.stopped = False

    def stop_session(self):
        self.stopped = True
        self.active = None

    def note_event(self, kind, start, end, **data):
        if not self.active:
            return False
        self.events.append((kind, start, end, data))
        return True


def flasher_with(sessions):
    svc = types.SimpleNamespace(ring=types.SimpleNamespace(end=100), ppk2=None)
    usb = types.SimpleNamespace(port="sim://", present=lambda: True, hold=lambda: None, release=lambda: None,
                                sim_factory=types.SimpleNamespace(flashed=0, wake=lambda: None))
    return Flasher(svc, usb, sessions)


def req(on_recording="ask"):
    return FlashRequest([Image(0x10000, b"app")], "esp32c6", meta={"git": "abc"}, on_recording=on_recording)


def test_recording_asks_then_keeps_and_marks():
    sm = FakeSessions()
    f = flasher_with(sm)
    job = f.submit(req())
    wait_for(lambda: job.status == "waiting_choice")
    assert [o["choice"] for o in job.question["options"]] == ["keep", "stop", "after", "cancel"]
    f.answer(job.id, "keep")
    wait_for(lambda: job.done)
    assert job.status == "done" and job.session_id == 7
    kind, start, end, data = sm.events[0]
    assert kind == "flash" and data["git"] == "abc" and data["result"] == "ok"


def test_flash_after_the_recording_stops():
    sm = FakeSessions()
    f = flasher_with(sm)
    job = f.submit(req("after"))
    wait_for(lambda: job.status == "waiting_recording")
    assert job.waiting_for == "soak" and f.usb.sim_factory.flashed == 0
    sm.active = None  # the recording stops
    wait_for(lambda: job.done)
    assert job.status == "done" and job.session_id is None and not sm.events  # nothing to mark any more


def test_recording_stop_and_cancel():
    sm = FakeSessions()
    f = flasher_with(sm)
    job = f.submit(req("stop"))
    wait_for(lambda: job.done)
    assert job.status == "done" and sm.stopped and not sm.events

    sm = FakeSessions()
    f = flasher_with(sm)
    job = f.submit(req())
    wait_for(lambda: job.status == "waiting_choice")
    with pytest.raises(FlashError):
        f.answer(job.id, "maybe")
    f.answer(job.id, "cancel")
    wait_for(lambda: job.done)
    assert job.status == "cancelled" and not sm.stopped and f.usb.sim_factory.flashed == 0
