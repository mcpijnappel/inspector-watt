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
    monkeypatch.setattr(m.httpx, "post", lambda url, json, timeout: sent.append((len(json["items"]), json["live"], json["record"])) or Resp(len(json["items"])))
    assert a.post_events(r["items"]) == {"live": 3, "recorded": 0, "skipped": 0} and sent == [(2, True, True), (1, True, True)]


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


# decoding during a recording (follow.py) ------------------------------------------------------------------------
def test_quiet_means_no_edge_on_a_bus_pin():
    w = {"i2c_sda": "D0", "i2c_scl": "D1"}
    assert dec.quiet(b"\x03" * 100, w) and dec.quiet(b"\x03\x07\x03\xff", w)  # D2 and up aren't wired
    assert not dec.quiet(b"\x03\x01\x03", w) and dec.quiet(b"\x03\x01", {"i2c_sda": "off", "i2c_scl": "off"})


IDLE = b"\x03" * 400_000  # both I2C lines high: 0.4 s at 1 MHz


def follower(data, monkeypatch, record="wakes"):
    """A module streaming `data` (sample 0 at unix 1000, 1 MHz) that records what it sends to the api."""
    c, a = module(sigrok.parse_scan(FX2), data)
    a.values.update(samplerate_hz=1_000_000, record=record)
    a.check()
    sent = []
    flags = []

    def post(items, live=True, record=True):
        flags.append((live, record))
        sent.extend(items)
        return {"live": len(items)}
    monkeypatch.setattr(a, "post_events", post)
    a.flags = flags
    f = a.follower
    f.step({"recording": None, "wakes": [], "last_seq": 0})  # the module was up before the recording started
    return a, f, sent


def rec(**kw):
    return {"recording": {"id": 7, "started_t": 1000.0, "stopped_t": None, "processed_t": 1000.6, **kw},
            "open_start_t": None, "wakes": [], "last_seq": 0}


def test_decodes_around_each_wake(monkeypatch):
    sig = i2c(SHT40_READ, idle=100_000) + IDLE  # transactions at ~0.10 and ~0.20 s, 0.7 s in all
    a, f, sent = follower(sig, monkeypatch)
    t_cmd = 1000.0 + 100_000 / 1e6
    # a wake 0.09-0.15 s: its window (50 ms before, 100 ms after) holds the command and its answer
    f.step({**rec(), "wakes": [{"seq": 1, "session_id": 7, "start_t": t_cmd - 0.01, "end_t": t_cmd + 0.05}], "last_seq": 1})
    msgs = [i for i in sent if i["kind"] == "i2c"]
    assert [m["data"]["label"] for m in msgs] == ["0x44 W1", "0x44 R6"] and abs(msgs[0]["t"] - t_cmd) < 0.001
    (win,) = [i for i in sent if i["kind"] == "bus_window"]
    assert win["t"] == pytest.approx(t_cmd - 0.06) and win["end_t"] == pytest.approx(t_cmd + 0.15)
    assert win["data"]["messages"] == 2 and f.after_seq == 1 and f.counts["windows"] == 1
    sent.clear()
    f.step({**rec(), "last_seq": 1})  # nothing new: nothing decoded twice
    assert sent == [] and f.windows == []


def test_waits_for_a_wake_window_to_pass(monkeypatch):
    a, f, sent = follower(IDLE, monkeypatch)  # the buffer holds 1000.0-1000.4
    f.step({**rec(), "wakes": [{"seq": 1, "session_id": 7, "start_t": 1000.1, "end_t": 1000.2}], "last_seq": 1})
    assert sent == [] and f.windows == [[pytest.approx(1000.05), pytest.approx(1000.3)]]  # needs data up to 1000.3 + margin


def test_a_running_wake_and_all_of_it_go_in_pieces_without_doubles(monkeypatch):
    import pst_analyzer.follow as fw
    monkeypatch.setattr(fw, "PIECE_S", 0.03)  # cuts right through the messages
    sig = i2c(SHT40_READ * 3, idle=20_000) + IDLE * 2
    a, f, sent = follower(sig, monkeypatch, record="all")
    f.step(rec())
    msgs = [i for i in sent if i["kind"] == "i2c"]
    assert len(msgs) == 6 and len({m["t"] for m in msgs}) == 6
    wins = [i for i in sent if i["kind"] == "bus_window"]
    assert wins[0]["t"] == 1000.0 and all(x["end_t"] == pytest.approx(y["t"]) for x, y in zip(wins, wins[1:]))
    assert f.counts["quiet"] > 0  # the idle stretch at the end needs no sigrok
    # a wake still going on: decoded up to what has arrived
    a2, f2, sent2 = follower(sig, monkeypatch)
    f2.step({**rec(), "open_start_t": 1000.01})
    assert len([i for i in sent2 if i["kind"] == "i2c"]) == 6 and f2.done_t > 1000.2


def test_missed_windows_say_why(monkeypatch):
    a, f, sent = follower(IDLE, monkeypatch)
    a.streamer.running = False  # unplugged
    a.error = "no analyzer found"
    f.step({**rec(), "wakes": [{"seq": 1, "session_id": 7, "start_t": 1000.1, "end_t": 1000.2}], "last_seq": 1})
    (win,) = sent
    assert win["kind"] == "bus_window" and win["data"]["missed"] == "no analyzer found" and f.counts["missed"] == 1
    # left the buffer: the buffer now starts at 1000.0, the wake was before it
    a, f, sent = follower(IDLE, monkeypatch)
    f.step({**rec(), "wakes": [{"seq": 1, "session_id": 7, "start_t": 999.0, "end_t": 999.5}], "last_seq": 1})
    assert "left the analyzer's buffer" in sent[0]["data"]["missed"] and sent[0]["end_t"] == 999.6


def test_joining_a_running_recording_starts_from_now_and_a_stop_finishes(monkeypatch):
    a, f, sent = follower(IDLE, monkeypatch)
    f.fresh = True  # the module just started, a recording is running
    f.step({**rec(processed_t=1000.2), "wakes": [{"seq": 3, "session_id": 7, "start_t": 1000.05, "end_t": 1000.1}], "last_seq": 3})
    assert sent == [] and f.done_t == 1000.2  # that wake may have been decoded before the restart
    f.step({**rec(stopped_t=1000.3, processed_t=1000.3), "wakes": [], "last_seq": 3})
    assert f.finished and "recording 7" in a.health()["detail"]
    f.step({"recording": None, "wakes": [], "last_seq": 3})
    assert f.session is None and a.follower.status()["session"] is None


def test_off_decodes_nothing(monkeypatch):
    a, f, sent = follower(i2c(SHT40_READ, idle=100_000) + IDLE, monkeypatch, record="off")
    f.step({**rec(), "wakes": [{"seq": 1, "session_id": 7, "start_t": 1000.09, "end_t": 1000.15}], "last_seq": 1})
    assert sent == [] and f.session is None and f.after_seq == 1


def test_live_decoding_is_live_only_and_a_lease(monkeypatch):
    sig = i2c(SHT40_READ, idle=100_000) + IDLE
    a, f, sent = follower(sig, monkeypatch)
    c = TestClient(create_app(a, background=False))
    f.live_step()
    assert sent == []  # off until the Live page asks
    assert c.post("/live", json={"on": True}).json() == {"live": True, "lease_s": 30.0}
    assert c.get("/status").json()["live"] and "decoding live" in a.health()["detail"]
    f.live_step()  # the last second that has arrived: the buffer ends at 1000.7
    assert [i["data"]["label"] for i in sent] == ["0x44 W1", "0x44 R6"] and a.flags == [(True, False)]
    done = f.live_done
    f.live_step()
    assert len(sent) == 2 and f.live_done == done  # nothing new arrived
    # a recording meanwhile: its windows go only into the recording (the live list has them already)
    f.step({**rec(), "wakes": [{"seq": 1, "session_id": 7, "start_t": 1000.09, "end_t": 1000.15}], "last_seq": 1})
    assert a.flags[-1] == (False, True)
    assert c.post("/live", json={"on": False}).json()["live"] is False and f.live_done is None
    a.live_until = 1.0  # a lease that ran out (monotonic)
    f.live_step()
    assert not a.live_on()


def test_decode_button_goes_into_a_recording_only_when_it_doesnt_decode_itself(monkeypatch):
    a, f, sent = follower(i2c(SHT40_READ, idle=100_000) + IDLE, monkeypatch)
    c = TestClient(create_app(a, background=False))
    c.post("/decode", json={"seconds": 1})
    a.values["record"] = "off"
    c.post("/decode", json={"seconds": 1})
    assert a.flags == [(True, False), (True, True)]
