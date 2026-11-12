"""A session's bus messages named for agents, the way the Bus panel names them (busfold.ts)."""

from fastapi.testclient import TestClient

from api import busnames, main
from api.chips import BUILT_IN, evaluate, table


def i2c(t, addr, *parts, nack=False):
    return {"t": t, "end_t": t + 0.0005, "kind": "i2c",
            "data": {"addr": addr, "nack": nack, "parts": [{"rw": rw, "addr": addr, "bytes": b} for rw, b in parts]}}


LIB = {c["name"].lower(): {**table(c), "name": c["name"]} for c in BUILT_IN}
CHIPS = {"m:112": {"name": "", "type": "TCA9548A"}, "m:68": {"name": "", "type": "SHT4x"},
         "2:16": {"name": "VEML north", "type": "VEML7700"}}


def test_formulas_like_the_web_ui():
    assert evaluate("-45 + 175 * u16(0) / 65535", [0x66, 0x4C]) == -45 + 175 * 0x664C / 65535
    assert evaluate("u16le(0)", [0x9C, 0x01]) == 412 and evaluate("s8(0)", [0xFF]) == -1
    assert evaluate("s16le(0) * 2", [0x00, 0x80]) == -65536 and evaluate("u24(0)", [1, 2, 3]) == 0x010203
    assert evaluate("u16(1)", [1, 2]) is None and evaluate("1 / (2 - 2)", []) is None and evaluate("3 +", []) is None


def test_names_registers_commands_and_ports():
    ev = [i2c(0.010, 0x70, ("W", [0])),
          i2c(0.011, 0x44, ("W", [0xFD])), i2c(0.020, 0x44, ("R", [0x66, 0x4C, 0xB2, 0x61, 0x9A, 0x0E])),
          i2c(0.030, 0x70, ("W", [4])),  # port 2
          i2c(0.031, 0x10, ("W", [0x04]), ("R", [0x9C, 0x01])),
          i2c(0.032, 0x10, ("W", [0x00, 0x00, 0x00])),
          i2c(0.040, 0x29, ("W", []), nack=True)]
    out = busnames.name_session_bus(ev, CHIPS, LIB)
    m = out["messages"]
    assert out["switches"] == ["0x70"] and [x["kind"] for x in m] == ["cmd", "reg", "set", "probe"]
    sht = m[0]
    assert sht["chip"] == "SHT4x" and sht["at"] == "m:0x44" and sht["command"] == "measure (high precision)"
    assert sht["wrote"] == "FD" and sht["wait_ms"] == 9.0 and sht["values"]["temperature"].endswith("°C")
    als = m[1]
    assert als["chip"] == "VEML north" and als["at"] == "port 2:0x10" and als["register"] == "ALS"
    assert als["values"] == {"light": "412 counts"}
    assert m[2]["register"] == "ALS_CONF_0" and m[2]["wrote"] == "00 00"
    assert m[3] == {"t_s": 0.04, "chip": "0x29", "at": "port 2:0x29", "kind": "probe", "nack": True, "answered": False}
    assert out["chips"]["port 2:0x10"]["messages"] == 2


def test_port_unknown_until_the_switch_speaks_and_repeats_fold():
    ev = [i2c(0.001, 0x10, ("W", [0x04]), ("R", [1, 0]))]  # before any switch write (window began mid-way)
    ev += [i2c(0.010, 0x70, ("W", [4])), i2c(0.011, 0x10, ("W", [0x04]), ("R", [2, 0]))]
    ev += [i2c(0.1 + k * 0.01, 0x10, ("W", [0x06]), ("R", [k])) for k in range(5)]  # a status poll
    m = busnames.name_session_bus(ev, CHIPS, LIB)["messages"]
    assert m[0]["at"] == "port ?:0x10" and m[0]["chip"] == "VEML north" and m[1]["at"] == "port 2:0x10"
    poll = m[2]
    assert poll["times"] == 5 and poll["every_ms"] == 10.0 and poll["last"]["read"] == "04" and len(m) == 3


def test_endpoint_reads_the_sessions_board(monkeypatch):
    async def get_session(sid):
        return {"id": sid, "name": "s", "device_id": 1, "device_name": "XIAO", "duration_s": 10.0}

    rows = [{"seq": 1, "module": "analyzer:1", "kind": "i2c", "start_idx": 1100, "end_idx": 1150,
             "data": {"addr": 0x44, "nack": False, "parts": [{"rw": "W", "addr": 0x44, "bytes": [0xFD]}]}},
            {"seq": 2, "module": "analyzer:1", "kind": "i2c", "start_idx": 2000, "end_idx": 2600,
             "data": {"addr": 0x44, "nack": False, "parts": [{"rw": "R", "addr": 0x44, "bytes": [0x66, 0x4C, 0xB2, 0x61, 0x9A, 0x0E]}]}}]

    async def q_all(sql, params=None):
        if "bus_window" in sql:
            return [{"start_idx": 0, "end_idx": 50_000, "data": {"messages": 2}},
                    {"start_idx": 60_000, "end_idx": 70_000, "data": {"messages": 0, "missed": "unplugged"}}]
        if "chip_types" in sql:
            return [{"n": k, "name": v["name"], "body": v} for k, v in LIB.items()]
        assert "module IS NULL" not in sql and params["kinds"] == ["i2c", "spi"]
        return [r for r in rows if params["a"] <= r["start_idx"] < params["b"]][: params["n"]]

    async def q_one(sql, params=None):
        return {"bus_chips": CHIPS}

    monkeypatch.setattr(main, "get_session", get_session)
    monkeypatch.setattr(main, "q_all", q_all)
    monkeypatch.setattr(main, "q_one", q_one)
    c = TestClient(main.app)
    raw = c.get("/api/sessions/3/bus", params={"start_s": 0, "end_s": 1}).json()
    assert [m["seq"] for m in raw["messages"]] == [1, 2] and raw["windows"][1]["missed"] == "unplugged"
    assert c.get("/api/sessions/3/bus", params={"limit": 1}).json()["more"] is True
    named = c.get("/api/sessions/3/bus/named").json()
    (m,) = named["messages"]
    assert m["command"] == "measure (high precision)" and m["t_s"] == 0.011 and named["session"]["board"] == "XIAO"
    assert named["decoded"] == {"windows": 2, "missed": [{"start_s": 0.6, "why": "unplugged"}]}
    assert c.get("/api/sessions/3/bus/named", params={"chip": "veml"}).json()["messages"] == []
