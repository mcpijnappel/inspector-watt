"""RP2040 / RP2350 uploads: UF2 checks, finding a board in BOOTSEL mode, picotool's command and output, and the
whole job on the simulator (touch -> boot loader -> load -> running again), on USB power."""

import base64
import struct
import types

import pytest
from fastapi.testclient import TestClient

from capture import flash as flash_mod
from capture.api import create_app
from capture.config import Config
from capture.flash import Flasher, FlashError, FlashJob, FlashRequest, Image
from capture.flash_rp2 import PicotoolOutput, Rp2Backend, find_bootsel, picotool_args, uf2_info
from capture.sim import SimUsbDevice

RP2040, ABSOLUTE, RP2350_ARM = 0xE48BFF56, 0xE48BFF57, 0xE48BFF59


def wait_for(cond, timeout=8.0):
    import time
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out"
        time.sleep(0.02)


def uf2(payload: bytes, family=RP2040, base=0x10000000, chunk=256, extra=()) -> bytes:
    """UF2 blocks like picotool / elf2uf2 write them (256 bytes each); extra = (family, addr) blocks first."""
    blocks = [(fam, addr, b"\0" * chunk) for fam, addr in extra]
    blocks += [(family, base + i, payload[i:i + chunk]) for i in range(0, len(payload), chunk)]
    out = b""
    for n, (fam, addr, data) in enumerate(blocks):
        head = struct.pack("<8I", 0x0A324655, 0x9E5D5157, 0x2000, addr, len(data), n, len(blocks), fam)
        out += head + data.ljust(476, b"\0") + struct.pack("<I", 0x0AB16F30)
    return out


# the file ---------------------------------------------------------------------------------------------------------
def test_uf2_info_finds_the_chip():
    info = uf2_info(uf2(b"x" * 1000))
    assert info == {"chip": "rp2040", "blocks": 4, "bytes": 1000, "start": 0x10000000, "end": 0x10000000 + 1000}
    # pico-sdk's RP2350 files start with an "absolute" block (errata workaround): not a chip of its own
    assert uf2_info(uf2(b"y" * 600, RP2350_ARM, extra=[(ABSOLUTE, 0x10FFFF00)]))["chip"] == "rp2350"


@pytest.mark.parametrize("data, says", [
    (b"\0" * 100, "512-byte"),
    (b"\0" * 512, "magic"),
    (uf2(b"z" * 300, family=0x68ED2B88), "another chip family"),  # SAMD21
    (uf2(b"z" * 300) + uf2(b"z" * 300, RP2350_ARM), "both"),
])
def test_uf2_info_refuses(data, says):
    with pytest.raises(FlashError, match=says):
        uf2_info(data)


def backend(**sim):
    dev = SimUsbDevice()
    for k, v in sim.items():
        setattr(dev, k, v)
    usb = types.SimpleNamespace(port="sim://", present=lambda: dev.present, sim_factory=dev)
    return Rp2Backend(usb), dev


def test_check_takes_one_uf2_and_its_chip():
    b, _ = backend()
    req = FlashRequest([Image(None, uf2(b"a" * 500))], family="rp2")
    b.check(req)
    assert req.chip == "rp2040"
    for bad, says in [(FlashRequest([Image(0x0, uf2(b"a" * 500))], family="rp2"), "without an offset"),
                      (FlashRequest([Image(None, uf2(b"a" * 500))], family="rp2", options={"x": 1}), "option"),
                      (FlashRequest([Image(None, uf2(b"a" * 500))], "rp2350", family="rp2"), "is for rp2040")]:
        with pytest.raises(FlashError, match=says):
            b.check(bad)


def test_find_bootsel_reads_sysfs(tmp_path):
    def dev(name, vendor, product, bus, num):
        d = tmp_path / name
        d.mkdir()
        for k, v in {"idVendor": vendor, "idProduct": product, "busnum": bus, "devnum": num}.items():
            (d / k).write_text(f"{v}\n")
    dev("1-2", "2e8a", "0003", 1, 7)  # RP2040 in BOOTSEL
    dev("1-3", "2e8a", "000a", 1, 8)  # an RP2040 running pico-sdk firmware: not BOOTSEL
    dev("3-1", "2e8a", "000f", 3, 2)  # RP2350 in BOOTSEL
    dev("1-4", "1915", "c00a", 1, 9)  # the PPK2
    (tmp_path / "usb1").mkdir()  # a root hub without these files
    assert find_bootsel(str(tmp_path)) == [{"chip": "rp2040", "bus": 1, "address": 7},
                                           {"chip": "rp2350", "bus": 3, "address": 2}]


def test_picotool_args_and_output():
    r = FlashRequest([Image(None, b"")], "rp2040", family="rp2")
    assert picotool_args(r, "/t/f.uf2", {"bus": 1, "address": 7})[1:] == [
        "load", "--update", "--verify", "--execute", "/t/f.uf2", "--bus", "1", "--address", "7"]
    r.full_flash = True
    assert "--update" not in picotool_args(r, "/t/f.uf2", {"bus": 1, "address": 7})

    job = FlashJob(r)
    out = PicotoolOutput(job)
    for pct in range(0, 101, 5):
        out.line(f"Loading into Flash:   [{'=' * (pct // 4):<25}]  {pct}%")
    assert 89 <= job.progress <= 90
    out.line("Verifying Flash:      [=========================]  100%")
    out.line("  OK")
    lines = job.to_dict()["log"]
    assert lines[:5] == ["Loading into Flash: 0%", "Loading into Flash: 25%", "Loading into Flash: 50%",
                         "Loading into Flash: 75%", "Loading into Flash: 100%"]
    assert "Verifying Flash: 100%" in lines and job.progress == 99


# the whole job, on the simulator -----------------------------------------------------------------------------------
@pytest.fixture
def client():
    cfg = Config(ppk2_port="sim://", uart_port="sim://", usb_port="sim://", ring_seconds=10, max_voltage_mv=4200,
                 auto_stream=False)
    with TestClient(create_app(cfg)) as c:
        wait_for(lambda: c.get("/status").json()["ppk2"]["connected"])
        yield c


def rp_body(data=None, **kw):
    return {"family": "rp2", "images": [{"offset": None, "data_b64": base64.b64encode(data or uf2(b"blink" * 400)).decode()}],
            "meta": {"project": "blink", "env": "xiao_rp2040"}, **kw}


def run(client, **kw):
    j = client.post("/flash", json=rp_body(**kw)).json()
    assert "id" in j, j
    wait_for(lambda: client.get(f"/flash/{j['id']}").json()["status"] in ("done", "failed", "cancelled"))
    return client.get(f"/flash/{j['id']}").json()


def test_running_board_restarts_into_its_boot_loader(client):
    sim = client.app.state.svc.usb.sim_factory
    j = run(client)
    assert j["status"] == "done" and j["chip"] == "rp2040" and j["power"] == "usb" and not j["power_cycled"]
    log = "\n".join(j["log"])
    assert "1200-baud touch" in log and "Loading into Flash: 100%" in log and "is running" in log
    assert sim.flashed == 1 and not sim.bootsel and sim.present
    st = client.get("/status").json()["ppk2"]
    assert not st["dut_power"]  # the PPK2 wasn't touched


def test_board_already_in_bootsel(client):
    sim = client.app.state.svc.usb.sim_factory
    sim.present, sim.bootsel = False, True  # blank board, or BOOTSEL held while plugging in
    j = run(client)
    assert j["status"] == "done" and "Board in BOOTSEL mode" in j["log"] and "1200-baud touch" not in "\n".join(j["log"])


def test_no_board_fails_with_a_hint(client, monkeypatch):
    monkeypatch.setattr(flash_mod, "PORT_WAIT_S", 0.4)
    sim = client.app.state.svc.usb.sim_factory
    sim.present = False
    j = run(client)
    assert j["status"] == "failed" and "plug it in" in j["error"] and "BOOTSEL" in j["error"]


def test_wrong_chip_on_the_board(client):
    sim = client.app.state.svc.usb.sim_factory
    sim.rp_chip = "rp2350"
    j = run(client)
    assert j["status"] == "failed" and sim.flashed == 0
    assert any("is an RP2350, the upload is for RP2040" in ln for ln in j["log"])


def test_rp2_on_ppk2_power_still_power_cycles(client):
    """Soldered to the PPK2 (power ppk2): like an ESP, a missing board is power-cycled first."""
    svc = client.app.state.svc
    svc.usb.sim_factory.present = False
    client.post("/ppk2/source", json={"voltage_mv": 3300})
    client.post("/ppk2/dut", json={"on": True})
    j = run(client, power="ppk2", when_asleep="cycle")
    assert j["status"] == "done" and j["power_cycled"] and j["power"] == "ppk2"


def test_esp_uploads_still_default_to_ppk2(client):
    body = {"chip": "esp32c6", "images": [{"offset": 0x10000, "data_b64": base64.b64encode(b"app!" * 100).decode()}]}
    j = client.post("/flash", json=body).json()
    assert j["power"] == "ppk2"
    r = client.post("/flash", json={**body, "power": "mains"})
    assert r.status_code == 409 and "ppk2 or usb" in r.json()["detail"]


def test_usb_powered_board_leaves_the_recording_alone():
    sessions = types.SimpleNamespace(active=types.SimpleNamespace(id=7, name="soak"), stop_session=None,
                                     note_event=lambda *a, **k: pytest.fail("no flash mark for a USB-powered board"))
    dev = SimUsbDevice()
    usb = types.SimpleNamespace(port="sim://", present=lambda: dev.present, hold=lambda: None, release=lambda: None,
                                sim_factory=dev)
    f = Flasher(types.SimpleNamespace(ring=types.SimpleNamespace(end=100), ppk2=None), usb, sessions)
    job = f.submit(FlashRequest([Image(None, uf2(b"q" * 300))], family="rp2"))
    wait_for(lambda: job.done)
    assert job.status == "done" and job.question is None and job.session_id is None
    assert any("keeps running" in ln for ln in job.to_dict()["log"])
