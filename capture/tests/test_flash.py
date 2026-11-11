import base64
import struct
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
    assert "switch the board on" in client.get(f"/flash/{j['id']}").json()["error"]


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
                                sim_factory=types.SimpleNamespace(flashed=0, wake=lambda: None, mac="9c:9e:6e:00:00:01",
                                                                  flash={}))
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


# images already on the chip (--skip-flashed) ----------------------------------------------------------------
from capture.flash import FlashJob  # noqa: E402
from capture.flash_esp import (OPTIONS, EspBackend, EsptoolOutput, app_desc, app_slot, esptool_args, find_app,  # noqa: E402
                               image_chip, parse_partition_table)
from capture.sim import SIM_PARTITIONS, partition_table  # noqa: E402


def flash_done(client, b):
    j = client.post("/flash", json=b).json()
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "done")
    return client.get(f"/flash/{j['id']}").json()


def test_images_already_on_the_chip_are_skipped(client):
    first = flash_done(client, body())
    assert first["mac"] == "9c9e6e000001" and [im["skipped"] for im in first["images"]] == [False, False]
    again = flash_done(client, body())
    assert [im["skipped"] for im in again["images"]] == [True, True] and again["progress"] == 100
    assert any("already in flash, skipping write" in ln for ln in again["log"])
    b = body()
    b["images"][1]["data_b64"] = base64.b64encode(b"new!" * 1000).decode()  # the app changed
    changed = flash_done(client, b)
    assert [im["skipped"] for im in changed["images"]] == [True, False]
    full = flash_done(client, body(full_flash=True))
    assert full["full_flash"] and [im["skipped"] for im in full["images"]] == [False, False]


def test_esptool_args():
    r = FlashRequest([Image(0, b"x"), Image(0x10000, b"y")], "esp32c6", options=dict(OPTIONS))
    args = esptool_args(r, "/dev/x", ["/t/0.bin", "/t/1.bin"])
    assert args[args.index("write-flash"):][-5:] == ["--skip-flashed", "0x0", "/t/0.bin", "0x10000", "/t/1.bin"]
    r.full_flash = True
    assert "--skip-flashed" not in esptool_args(r, "/dev/x", ["/t/0.bin", "/t/1.bin"])


def test_esptool_output_parsing():
    """Lines as esptool 5.6 prints them (write-flash --skip-flashed, two images, the first one on the chip)."""
    job = FlashJob(FlashRequest([Image(0, b"x"), Image(0x10000, b"z")], "esp32c6"))
    out = EsptoolOutput(job, 2)
    for ln in ["Connected to ESP32-C6 on /dev/ttyACM0:", "MAC:                9c:9e:6e:ff:fe:77:b3:a4",
               "BASE MAC:           9c:9e:6e:77:b3:a4",
               "Writing '/tmp/flash-k3/0_0x0.bin' at 0x00000000...", "Comparing flash contents against new data...",
               "'/tmp/flash-k3/0_0x0.bin' at 0x00000000 already in flash, skipping write.",
               "Writing '/tmp/flash-k3/1_0x10000.bin' at 0x00010000...", "Comparing flash contents against new data...",
               "Flash will be erased from 0x00010000 to 0x001cbfff...", "Writing at 0x00100000 [ 50 %]",
               "Wrote 1816736 bytes (1097583 compressed) at 0x00010000 in 13.6 seconds (1068.1 kbit/s).",
               "Hash of data verified."]:
        out.line(ln)
    out.finish()
    assert job.mac == "9c9e6efffe77b3a4"  # the first MAC line (EUI-64 on the C6), not BASE MAC
    assert job.skipped == [0x0] and job.progress == 75.0


# app-only uploads (Arduino OTA, a single app .bin) and the ESP image helpers ----------------------------------
def app_image(chip_id=13, version="1.2.3", project="blink", size=0x3000, fill=b"\x11"):
    """A minimal ESP app image: header (24 bytes, chip id at 12), one segment header, esp_app_desc_t."""
    hdr = bytes([0xE9, 1, 2, 0x2F]) + struct.pack("<I", 0x42000000) + bytes([0xEE, 0, 0, 0]) + struct.pack("<H", chip_id)
    hdr += b"\0" * (24 - len(hdr))
    desc = struct.pack("<IIII", 0xABCD5432, 0, 0, 0) + b"".join(
        v.encode().ljust(n, b"\0") for v, n in ((version, 32), (project, 32), ("12:00:00", 16), ("Nov 10 2026", 16),
                                                 ("v5.5", 32)))
    return (hdr + struct.pack("<II", 0x3C000020, 256) + desc).ljust(size, fill)


def app_only(data: bytes, **kw):
    return {"images": [{"offset": None, "data_b64": base64.b64encode(data).decode()}], "app_only": True, **kw}


def test_app_only_upload_goes_to_the_app_partition(client):
    app = app_image()
    j = flash_done(client, app_only(app))
    assert j["chip"] == "esp32c6"  # from the image header
    assert j["meta"]["version"] == "1.2.3" and j["meta"]["project"] == "blink"  # from esp_app_desc_t
    assert [im["offset"] for im in j["images"]] == [0xE000, 0x10000]  # otadata erased + app0 (no factory)
    assert any("partition 'app0' at 0x10000" in ln for ln in j["log"])
    assert client.app.state.svc.usb.sim_factory.flash[0x10000] == app
    again = flash_done(client, app_only(app))
    assert [im["skipped"] for im in again["images"]] == [True, True]


def test_app_only_refusals(client):
    assert client.post("/flash", json=app_only(b"not an app" * 10)).status_code == 409
    assert client.post("/flash", json={**app_only(app_image()), "images": [
        {"offset": 0x10000, "data_b64": base64.b64encode(app_image()).decode()}]}).status_code == 409
    assert client.post("/flash", json={**body(), "family": "rp2"}).status_code == 409
    j = client.post("/flash", json=app_only(app_image(size=0x150000))).json()  # bigger than app0 (0x140000)
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] == "failed")
    assert "doesn't fit" in client.get(f"/flash/{j['id']}").json()["error"]


def test_chip_from_the_images(client):
    b = body(chip="")
    b["images"][0]["data_b64"] = base64.b64encode(app_image(chip_id=9)).decode()  # an S3 image
    j = flash_done(client, b)
    assert j["chip"] == "esp32s3"
    unknown = body(chip="")  # no ESP image header anywhere
    assert "chip unknown" in client.post("/flash", json=unknown).json()["detail"]


def test_esp_image_helpers():
    parts = parse_partition_table(partition_table(SIM_PARTITIONS))
    assert [p.label for p in parts] == ["nvs", "otadata", "app0", "app1", "spiffs"]
    target, otadata = app_slot(parts)
    assert (target.label, target.offset, otadata.offset) == ("app0", 0x10000, 0xE000)
    with_factory = parse_partition_table(partition_table([("factory", 0, 0x00, 0x20000, 0x100000), *SIM_PARTITIONS]))
    assert app_slot(with_factory)[0].label == "factory"
    assert image_chip(app_image(chip_id=5)) == "esp32c3" and image_chip(b"\x00" * 64) is None
    assert app_desc(app_image())["idf"] == "v5.5"
    # Arduino builds: the description is the precompiled framework's, so it says nothing about the sketch
    req = FlashRequest([Image(None, app_image(project="arduino-lib-builder", version="8cabf2c"))],
                       options={"app_only": True}, meta={})
    EspBackend(types.SimpleNamespace(port="sim://")).check(req)
    assert "version" not in req.meta and req.chip == "esp32c6"
    # a merged image (esptool merge-bin): bootloader at 0, partition table at 0x8000, app at 0x10000
    merged = bytearray(b"\xff" * 0x13000)
    merged[0x8000:0x8C00] = partition_table(SIM_PARTITIONS)
    merged[0x10000:0x13000] = app_image(version="9.9")
    assert app_desc(find_app([Image(0, bytes(merged))]))["version"] == "9.9"
