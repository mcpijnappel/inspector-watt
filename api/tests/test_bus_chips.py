"""Chip names per board for the Bus tab."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import main


def test_keys_are_where_and_address():
    ok = main.check_bus_chips({"m:68": main.BusChip(name=" SHT40 "), "3:16": main.BusChip(name="VEML7700"),
                               "m:112": main.BusChip(switch=True), "m:57": main.BusChip(name="")})
    assert ok == {"m:68": {"name": "SHT40"}, "3:16": {"name": "VEML7700"}, "m:112": {"name": "", "switch": True}}
    for bad in ("x:68", "8:16", "m:0x44", "m:200", "m:2", "m:68:1"):
        with pytest.raises(HTTPException):
            main.check_bus_chips({bad: main.BusChip(name="a")})


def test_save_and_read(monkeypatch):
    saved = {}

    async def q_one(sql, params=None):
        if sql.startswith("UPDATE"):
            saved[params[1]] = params[0].obj
            return {"bus_chips": params[0].obj}
        return {"bus_chips": saved.get(params[0], {})} if params[0] == 1 else None

    monkeypatch.setattr(main, "q_one", q_one)
    c = TestClient(main.app)
    assert c.put("/api/devices/1/bus-chips", json={"m:68": {"name": "SHT40"}}).json() == {"m:68": {"name": "SHT40"}}
    assert c.get("/api/devices/1/bus-chips").json() == {"m:68": {"name": "SHT40"}}
    assert c.get("/api/devices/2/bus-chips").status_code == 404
    assert c.put("/api/devices/1/bus-chips", json={"m:68": {"name": "x" * 41}}).status_code == 422
    assert c.put("/api/devices/1/bus-chips", json={"z:68": {"name": "x"}}).status_code == 422
