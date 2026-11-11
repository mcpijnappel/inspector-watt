import threading
import time

import pytest
from fastapi.testclient import TestClient

from pst_analyzer import decode as dec
from pst_analyzer import sigrok
from pst_analyzer.main import Analyzer, create_app
from pst_analyzer.stream import Ring, Streamer, Timing
from signals import i2c, spi

FX2 = """The following devices were found:
fx2lafw:conn=3.12 - Saleae Logic with 8 channels: D0 D1 D2 D3 D4 D5 D6 D7
"""
DEMO = """The following devices were found:
demo - Demo device with 13 channels: D0 D1 D2 D3 D4 D5 D6 D7 A0 A1 A2 A3 A4
"""
SHT40_READ = [(0x44, 0, [0xFD], False), (0x44, 1, [0x66, 0x4C, 0xB2, 0x61, 0x9A, 0x0E], True)]


def test_parse_scan():
    assert sigrok.parse_scan(FX2) == [{"driver": "fx2lafw", "conn": "3.12", "description": "Saleae Logic",
                                       "channels": [f"D{i}" for i in range(8)]}]
    assert sigrok.parse_scan(DEMO)[0]["channels"] == [f"D{i}" for i in range(8)]  # logic channels only
    assert sigrok.parse_scan("sr: fx2lafw: Unable to claim USB interface") == []


# the buffer -----------------------------------------------------------------------------------------------
def test_ring_keeps_the_newest_and_wraps():
    r = Ring(10)
    r.append(bytes(range(7)))
    assert r.read(0, 7) == (0, bytes(range(7)))
    r.append(bytes(range(7, 15)))  # wraps: 0-4 are gone
    assert (r.start, r.end) == (5, 15) and r.read(0, 15) == (5, bytes(range(5, 15)))
    r.append(bytes(range(15, 40)))  # more than fits at once
    assert (r.start, r.end) == (30, 40) and r.read(32, 35) == (32, bytes([32, 33, 34]))


def test_timing_takes_the_earliest_arrival():
    t = Timing(1000)
    t.observe(10.050, 50)   # arrived 0 late: sample 0 at 10.000
    t.observe(10.140, 100)  # 40 ms late
    assert t.offset == pytest.approx(10.0) and t.wall(500) == pytest.approx(10.5) and t.sample(10.25) == 250


# decoding (with the real sigrok-cli) ----------------------------------------------------------------------
def test_decodes_i2c():
    out = dec.decode(i2c(SHT40_READ), 1_000_000, {"i2c_sda": "D0", "i2c_scl": "D1"})
    assert [dec.i2c_label(t) for t in out["i2c"]] == ["0x44 W1", "0x44 R6"]
    assert out["i2c"][1]["parts"][0]["bytes"] == [0x66, 0x4C, 0xB2, 0x61, 0x9A, 0x0E]
    assert out["i2c"][0]["start"] < out["i2c"][0]["end"] < out["i2c"][1]["start"] and out["spi"] == []


def test_decodes_spi_both_ways():
    w = {"i2c_sda": "off", "i2c_scl": "off", "spi_clk": "D2", "spi_mosi": "D3", "spi_miso": "D4", "spi_cs": "D5"}
    out = dec.decode(spi([([0x9F, 0, 0, 0], [0xFF, 0xEF, 0x40, 0x18])]), 1_000_000, w)
    assert out["spi"] == [{"start": out["spi"][0]["start"], "end": out["spi"][0]["end"],
                           "mosi": [0x9F, 0, 0, 0], "miso": [0xFF, 0xEF, 0x40, 0x18]}]


def test_repeated_start_is_one_transaction():
    anns = [(0, 0, "Start"), (1, 9, "Address write: 44"), (10, 18, "Data write: E0"), (19, 19, "Start repeat"),
            (20, 28, "Address read: 44"), (29, 37, "Data read: 12"), (38, 39, "NACK"), (40, 40, "Stop"),
            (50, 60, "Data read: 99")]  # outside a transaction: ignored
    t = dec.i2c_transactions(anns)
    assert len(t) == 1 and dec.i2c_label(t[0]) == "0x44 W1 R1" and (t[0]["start"], t[0]["end"]) == (0, 40)


def test_nack_only_when_a_device_refuses():
    absent = [(0, 0, "Start"), (1, 9, "Address write: 10"), (10, 11, "NACK"), (12, 12, "Stop")]
    refused = [(0, 0, "Start"), (1, 9, "Address write: 44"), (10, 11, "ACK"), (12, 20, "Data write: 94"),
               (21, 22, "NACK"), (23, 23, "Stop")]
    read_end = [(0, 0, "Start"), (1, 9, "Address read: 44"), (10, 11, "ACK"), (12, 20, "Data read: 5A"),
                (21, 22, "NACK"), (23, 23, "Stop")]
    assert [dec.i2c_transactions(a)[0]["nack"] for a in (absent, refused, read_end)] == [True, True, False]


def test_wiring_checks():
    with pytest.raises(dec.DecodeError, match="twice"):
        dec.check_wiring({"i2c_sda": "D0", "i2c_scl": "D0"})
    with pytest.raises(dec.DecodeError, match="both"):
        dec.check_wiring({"i2c_sda": "D0", "i2c_scl": "off"})


# the module -----------------------------------------------------------------------------------------------
class FakeStreamer:
    """A streamer whose buffer holds a test signal, sample 0 at unix time 1000."""
    data = b""

    def __init__(self, driver, rate, conn=None):
        self.rate, self.running, self.error, self.conn = rate, False, None, conn
        self.ring = Ring(len(self.data) + 10)
        self.timing = Timing(rate)

    def start(self):
        self.ring.append(self.data)
        self.timing.observe(1000.0 + len(self.data) / self.rate, len(self.data))
        self.running = True

    def stop(self):
        self.running = False

    def window(self, t0, t1):
        return Streamer.window(self, t0, t1)

    def status(self):
        return {"buffered_s": len(self.data) / self.rate}


def module(devices, data=b""):
    FakeStreamer.data = data
    a = Analyzer(scan=lambda driver: devices, api_url="http://nowhere", streamer_cls=FakeStreamer)
    return TestClient(create_app(a, background=False)), a


def test_streams_and_decodes_the_last_seconds():
    c, a = module(sigrok.parse_scan(FX2), i2c(SHT40_READ, idle=20000))
    a.values["samplerate_hz"] = 1_000_000
    a.check()
    assert a.health()["ok"] and "streaming at 1 MHz" in a.health()["detail"]
    r = c.post("/decode", json={"seconds": 1, "post": False}).json()
    assert [i["data"]["label"] for i in r["items"]] == ["0x44 W1", "0x44 R6"]
    first = r["items"][0]
    assert 1000.0 < first["t"] < first["end_t"] < 1000.1 and first["kind"] == "i2c"


def test_noisy_pins_are_capped_and_posted_in_batches(monkeypatch):
    import pst_analyzer.main as m
    monkeypatch.setattr(m, "MAX_ITEMS", 3)
    monkeypatch.setattr(m, "POST_BATCH", 2)
    c, a = module(sigrok.parse_scan(FX2), i2c(SHT40_READ * 3, idle=200))
    a.values["samplerate_hz"] = 1_000_000
    a.check()
    r = a.decode_last(1)
    assert r["found"] == 6 and r["truncated"] and len(r["items"]) == 3
    sent = []

    class Resp:
        def __init__(self, n):
            self.n = n

        def raise_for_status(self):
            pass

        def json(self):
            return {"live": self.n, "recorded": 0, "skipped": 0}
    monkeypatch.setattr(m.httpx, "post", lambda url, json, timeout: sent.append(len(json["items"])) or Resp(len(json["items"])))
    assert a.post_events(r["items"]) == {"live": 3, "recorded": 0, "skipped": 0} and sent == [2, 1]


def test_settings_restart_the_stream_and_check_wiring():
    c, a = module(sigrok.parse_scan(FX2), b"\x00" * 100)
    a.check()
    s = a.streamer
    assert c.put("/settings", json={"samplerate_hz": 2_000_000}).json()["samplerate_hz"] == 2_000_000
    assert a.streamer is not s and a.streamer.rate == 2_000_000
    assert c.put("/settings", json={"spi_clk": "D0"}).status_code == 422  # D0 is I2C SDA
    c.put("/settings", json={"enabled": False})
    assert a.streamer is None and c.get("/module").json()["health"]["detail"] == "switched off"
    assert c.post("/decode", json={"seconds": 1}).status_code == 409


def test_a_broken_stream_scans_again():
    # The analyzer re-enumerated on USB (new address): sigrok keeps failing on the old one until we scan again
    devices = [{"driver": "fx2lafw", "conn": "3.30", "description": "FX2", "channels": []}]
    c, a = module(devices, b"\x00" * 100)
    a.scan_fn = lambda driver: devices
    a.check()
    old = a.streamer
    assert old.conn == "3.30"
    old.running, old.error = False, "No devices found."
    devices[0] = {**devices[0], "conn": "3.64"}
    a.check()
    assert a.streamer is None and "No devices found" in c.get("/module").json()["health"]["detail"]
    a.check()
    assert a.streamer is not old and a.streamer.conn == "3.64" and a.streamer.running


def test_no_analyzer():
    c, a = module([])
    a.check()
    h = c.get("/module").json()["health"]
    assert not h["ok"] and "plugged in" in h["detail"] and a.streamer is None


def test_scan_errors_become_health():
    def broken(driver):
        raise sigrok.SigrokError("sigrok-cli didn't answer")
    a = Analyzer(scan=broken, api_url="http://nowhere")
    a.check()
    assert a.health() == {"ok": False, "detail": "sigrok-cli didn't answer"}


def test_real_stream_from_the_demo_device():
    """sigrok's demo device, streamed for real (in real time) into the buffer."""
    s = Streamer("demo", 1_000_000)
    s.start()
    try:
        deadline = time.time() + 10
        while s.ring.end < 500_000:
            assert time.time() < deadline, s.status()
            time.sleep(0.05)
        assert s.running and s.timing.offset is not None
        end_wall = s.timing.wall(s.ring.end)
        assert abs(end_wall - time.time()) < 0.5  # sample times follow the clock
        first, data, wall = s.window(end_wall - 0.2, end_wall)
        assert 180_000 < len(data) <= 200_001
    finally:
        s.stop()
    assert not s.running
