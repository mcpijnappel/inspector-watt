from fastapi.testclient import TestClient

from pst_analyzer import sigrok
from pst_analyzer.main import Analyzer, create_app

FX2 = """The following devices were found:
fx2lafw:conn=3.12 - Saleae Logic with 8 channels: D0 D1 D2 D3 D4 D5 D6 D7
"""
DEMO = """The following devices were found:
demo - Demo device with 13 channels: D0 D1 D2 D3 D4 D5 D6 D7 A0 A1 A2 A3 A4
"""


def test_parse_scan():
    assert sigrok.parse_scan(FX2) == [{"driver": "fx2lafw", "conn": "3.12", "description": "Saleae Logic",
                                       "channels": [f"D{i}" for i in range(8)]}]
    assert sigrok.parse_scan(DEMO)[0]["channels"] == [f"D{i}" for i in range(8)]  # logic channels only
    assert sigrok.parse_scan("sr: fx2lafw: Unable to claim USB interface") == []


def client(devices):
    a = Analyzer(scan=lambda driver: devices, api_url="http://nowhere")
    return TestClient(create_app(a, background=False)), a


def test_describes_itself_and_its_health():
    c, a = client(sigrok.parse_scan(FX2))
    a.rescan()
    d = c.get("/module").json()
    assert d["name"] == "analyzer" and d["contract"] == 1 and d["health"]["ok"]
    assert "Saleae Logic" in d["health"]["detail"] and "8 channels" in d["health"]["detail"]
    assert {s["key"] for s in d["settings"]} == {"enabled", "samplerate_hz", "pre_ms", "post_ms"}
    rates = next(s for s in d["settings"] if s["key"] == "samplerate_hz")["choices"]
    assert rates[0] == 1_000_000 and rates[-1] == 24_000_000  # fx2lafw's maximum
    assert [e["kind"] for e in d["events"]] == ["i2c", "spi"]


def test_no_analyzer_and_settings():
    c, a = client([])
    a.rescan()
    h = c.get("/module").json()["health"]
    assert not h["ok"] and "plugged in" in h["detail"]
    v = c.put("/settings", json={"samplerate_hz": 2_000_000, "junk": 1, "enabled": False}).json()
    assert v["samplerate_hz"] == 2_000_000 and "junk" not in v
    assert c.get("/module").json()["health"]["detail"] == "switched off"


def test_scan_errors_become_health():
    def broken(driver):
        raise sigrok.SigrokError("sigrok-cli didn't answer")
    a = Analyzer(scan=broken, api_url="http://nowhere")
    a.rescan()
    assert a.health() == {"ok": False, "detail": "sigrok-cli didn't answer"}
