"""The sensor library: chip tables, the formula language, and who may write."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api import chips, main


def test_formulas():
    assert chips.check_formula("-45 + 175 * u16(0) / 65535") == 2
    assert chips.check_formula("-6 + 125 * u16(3) / 65535") == 5
    assert chips.check_formula("(u16le(0) + s8(4)) * 0.0576") == 5
    assert chips.check_formula("u32le(2)") == 6
    for bad in ("", "u16(0", "import os", "u16(x)", "foo(1)", "1 +", "u16(1.5)", "__class__", "1;2", "a.b"):
        with pytest.raises(chips.FormulaError):
            chips.check_formula(bad)


def test_tables_are_checked():
    ok = chips.ChipIn(name="SHT4x", buses=[{"bus": "i2c", "addresses": [0x45, 0x44, 0x44], "style": "commands",
                                            "commands": [{"code": 0xFD, "name": "measure", "reply": 6,
                                                          "values": [{"name": "t", "unit": "°C", "expr": "-45 + 175 * u16(0) / 65535"}]}]}])
    assert ok.on("i2c").addresses == [0x44, 0x45] and ok.on("spi") is None
    assert chips.table(ok) == {"name": "SHT4x", "buses": [{"bus": "i2c", "style": "commands", "addresses": [0x44, 0x45], "commands": [
        {"code": 0xFD, "name": "measure", "reply": 6, "values": [{"name": "t", "unit": "°C", "expr": "-45 + 175 * u16(0) / 65535"}]}]}]}
    i2c = {"bus": "i2c", "addresses": [0x10]}
    for bad in ({"name": "X", "buses": [{"bus": "i2c", "addresses": [0x78]}]},  # not a 7-bit address
                {"name": "X", "buses": [{"bus": "i2c", "addresses": []}]},  # I2C needs an address
                {"name": "X", "buses": []},
                {"name": "X", "buses": [i2c, i2c]},  # each bus once
                {"name": "X", "buses": [{"bus": "uart"}]},  # not (yet) a bus
                {"name": "", "buses": [i2c]},
                {"name": "X<script>", "buses": [i2c]},
                {"name": "X", "buses": [{**i2c, "registers": [{"addr": 1, "name": "A"}, {"addr": 1, "name": "B"}]}]},
                {"name": "X", "buses": [{**i2c, "registers": [{"addr": 1, "name": "A", "values": [{"name": "v", "expr": "eval(1)"}]}]}]},
                {"name": "X", "buses": [{"bus": "spi", "style": "switch"}]},  # a switch is I2C
                {"name": "X", "buses": [{"bus": "spi", "read_bit": 0}]}):
        with pytest.raises(ValidationError):
            chips.ChipIn.model_validate(bad)


def test_a_table_per_bus():
    t = chips.ChipIn(name="BME680", buses=[{"bus": "i2c", "addresses": [0x76], "style": "registers", "registers": [{"addr": 0xD0, "name": "ID"}]},
                                            {"bus": "spi", "style": "registers", "registers": [{"addr": 0x50, "name": "ID (page 0)"}]}])
    assert t.on("spi").read_bit == 0x80 and t.on("spi").addr_mask == 0x7F
    assert [b["registers"][0]["name"] for b in chips.table(t)["buses"]] == ["ID", "ID (page 0)"]
    assert chips.table(t)["buses"][1]["read_bit"] == 0x80  # stored in full
    lis = next(c for c in chips.BUILT_IN if c["name"] == "LIS3DH")
    assert [b["bus"] for b in lis["buses"]] == ["i2c", "spi"] and chips.ChipIn.model_validate(lis).on("spi").addr_mask == 0x3F


def test_built_in_chips_are_valid_tables():
    names = [c["name"] for c in chips.BUILT_IN]
    assert len(names) == len(set(n.lower() for n in names)) >= 24
    for c in chips.BUILT_IN:
        chips.table(c)
    sht = next(c for c in chips.BUILT_IN if c["name"] == "SHT4x")
    # the formula gives the SHT4x's own example: 0x6666 -> 25 °C (175 * 0.4 - 45)
    assert sht["buses"][0]["commands"][0]["values"][0]["expr"] == "-45 + 175 * u16(0) / 65535"


class FakeDb:
    """Just enough of the database for the chip routes."""

    def __init__(self):
        self.rows, self.devices, self.mcp = {}, [{"id": 1, "name": "XIAO", "bus_chips": {"m:68": {"type": "SHT4x"}}}], {}

    async def q_one(self, sql, p=None):
        r = await self.q_all(sql, p)
        return r[0] if r else None

    async def q_all(self, sql, p=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT name, bus_chips FROM devices"):
            return self.devices
        if s.startswith("SELECT id, bus_chips FROM devices"):
            return self.devices
        if s.startswith("UPDATE devices SET bus_chips"):
            next(d for d in self.devices if d["id"] == p[1])["bus_chips"] = p[0].obj
            return [{"id": p[1], "bus_chips": p[0].obj}]
        if s.startswith("SELECT lower(name) AS n, name, body FROM chip_types"):
            return [{"n": r["name"].lower(), "name": r["name"], "body": r["body"]} for r in self.rows.values()]
        if "lower(name) = lower(%s) AND id <>" in s:
            return [r for r in self.rows.values() if r["name"].lower() == p[0].lower() and r["id"] != p[1]]
        if "WHERE lower(name) = lower(%s)" in s:
            return [r for r in self.rows.values() if r["name"].lower() == p[0].lower()]
        if s.startswith("UPDATE chip_types SET body"):
            self.rows[p[1]]["body"] = p[0].obj
            return [{"id": p[1]}]
        if s.startswith("INSERT INTO chip_types"):
            i = len(self.rows) + 1
            src, agent, checked = (p[2], p[3], p[4]) if len(p) > 2 else ("built-in", None, False)
            self.rows[i] = {"id": i, "name": p[0], "body": p[1].obj, "source": src, "agent": agent, "checked": checked}
            return [self.rows[i]]
        if s.startswith("SELECT name FROM chip_types WHERE id"):
            return [{"name": self.rows[p[0]]["name"]}] if p[0] in self.rows else []
        if s.startswith("UPDATE chip_types SET name"):
            if p[5] not in self.rows:
                return []
            self.rows[p[5]].update(name=p[0], body=p[1].obj, source=p[2], agent=p[3], checked=p[4])
            return [self.rows[p[5]]]
        if s.startswith("UPDATE chip_types SET checked"):
            if p[1] not in self.rows:
                return []
            self.rows[p[1]]["checked"] = p[0]
            return [self.rows[p[1]]]
        if s.startswith("DELETE FROM chip_types"):
            r = self.rows.pop(p[0], None)
            return [r] if r else []
        if "FROM chip_types ORDER BY" in s:
            return list(self.rows.values())
        if "FROM chip_types WHERE id" in s:
            return [self.rows[p[0]]] if p[0] in self.rows else []
        raise AssertionError(f"unexpected query: {s}")


@pytest.fixture
def db(monkeypatch):
    f = FakeDb()
    monkeypatch.setattr(main, "q_one", f.q_one)
    monkeypatch.setattr(main, "q_all", f.q_all)

    async def mcp_settings():
        return {"lan": False, "hardware": False, "library": False, **f.mcp}
    monkeypatch.setattr(main, "mcp_settings", mcp_settings)
    return f


SHT = {"name": "SHT4x", "buses": [{"bus": "i2c", "addresses": [0x44], "style": "commands", "commands": [{"code": 0xFD, "name": "measure", "reply": 6}]}]}
AGENT = {"X-PST-Client": "mcp", "X-PST-Agent": "Claude Code"}


def test_people_and_agents_write_differently(db):
    c = TestClient(main.app)
    # agents only with the switch on, and their tables are not checked
    assert c.post("/api/chips", json=SHT, headers=AGENT).status_code == 403
    db.mcp["library"] = True
    r = c.post("/api/chips", json=SHT, headers=AGENT).json()
    assert (r["source"], r["agent"], r["checked"]) == ("agent", "Claude Code", False)
    assert c.post("/api/chips", json=SHT).status_code == 409  # by name, any case
    assert c.post(f"/api/chips/{r['id']}/check", headers=AGENT).status_code == 403
    assert c.post(f"/api/chips/{r['id']}/check").json()["checked"] is True
    assert c.delete(f"/api/chips/{r['id']}", headers=AGENT).status_code == 403
    # an agent's edit makes it "not checked" again; a person's edit counts as checked
    assert c.put(f"/api/chips/{r['id']}", json=SHT, headers=AGENT).json()["checked"] is False
    assert c.put(f"/api/chips/{r['id']}", json=SHT).json()["checked"] is True
    # the boards that use it
    assert c.get("/api/chips").json()[0]["boards"] == ["XIAO"]


def test_a_new_name_follows_on_the_boards(db):
    c = TestClient(main.app)
    cid = c.post("/api/chips", json=SHT).json()["id"]
    assert c.put(f"/api/chips/{cid}", json={**SHT, "name": "SHT40"}).json()["boards"] == ["XIAO"]
    assert db.devices[0]["bus_chips"] == {"m:68": {"type": "SHT40"}}
    assert c.delete(f"/api/chips/{cid}").json() == {"deleted": cid, "boards": ["XIAO"]}


def test_seeding_adds_only_missing_chips(db):
    import asyncio
    from psycopg.types.json import Jsonb
    assert asyncio.run(chips.seed(db.q_one, Jsonb)) == len(chips.BUILT_IN)
    assert asyncio.run(chips.seed(db.q_one, Jsonb)) == 0
    assert all(r["source"] == "built-in" and not r["checked"] for r in db.rows.values())
    # an older built-in table is brought up to date; one somebody changed is left alone
    sht = next(r for r in db.rows.values() if r["name"] == "SHT4x")
    sht["body"] = {"name": "SHT4x", "addresses": [68]}  # an older version of the table
    veml = next(r for r in db.rows.values() if r["name"] == "VEML7700")
    veml.update(body={"name": "VEML7700", "addresses": [16]}, source="you")
    assert asyncio.run(chips.seed(db.q_one, Jsonb)) == 1
    assert sht["body"]["buses"][0]["style"] == "commands" and veml["body"] == {"name": "VEML7700", "addresses": [16]}


def test_board_chip_keys():
    from api.main import BusChip, check_bus_chips
    assert check_bus_chips({"spi": BusChip(type="LIS3DH"), "m:68": BusChip(name="t")}) == {"spi": {"name": "", "type": "LIS3DH"}, "m:68": {"name": "t"}}
    with pytest.raises(Exception):
        check_bus_chips({"spi:2": BusChip(type="x")})


def test_a_board_chip_needs_its_bus_in_the_library(db):
    c = TestClient(main.app)
    c.post("/api/chips", json=SHT)
    c.post("/api/chips", json={"name": "LIS3DH", "buses": [{"bus": "i2c", "addresses": [0x18]}, {"bus": "spi", "inc_bit": 0x40}]})
    assert c.put("/api/devices/1/bus-chips", json={"spi": {"type": "sht4x"}}).status_code == 422  # SHT4x: I2C only
    r = c.put("/api/devices/1/bus-chips", json={"spi": {"type": "lis3dh"}, "m:68": {"type": "SHT4X"}})
    assert r.json() == {"spi": {"name": "", "type": "LIS3DH"}, "m:68": {"name": "", "type": "SHT4x"}}  # the library's spelling
    assert c.put("/api/devices/1/bus-chips", json={"m:68": {"type": "Nope"}}).status_code == 422
