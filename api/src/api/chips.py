"""The sensor library: chip types shared by every board, and the formula language for their values.

A chip table says how a chip talks, so the Bus panel can name what it sees and turn bytes into readings. A chip has
one entry per bus it can use (`buses`: I2C, SPI; UART can follow), each with that bus's settings and its own table:

- I2C: the 7-bit `addresses` it answers at.
- SPI: picked by its chip select; the first byte is the register address with a read/write bit (`read_bit`, e.g.
  0x80; `read_when_set`: a set bit means read) and sometimes an auto-increment bit (`inc_bit`). The rest of the
  transfer is the data: MISO for a read, MOSI for a write.

and per bus how its messages go (`style`):

- registers  register addresses: write the address then read = "read register", a longer write = "set register"
             (VEML7700, AS7343, ...). `reg_bytes` / `byte_order`: the width of a register's data.
- commands   a command byte and, a little later, its answer (SHT4x: FD, then 6 bytes after 8.3 ms).
- switch     an I2C switch (TCA9548A): its one-byte writes open ports.
- basic      only that it's there (addresses), no table yet.

A chip that's the same on both buses repeats its table; one that differs (the BME680 pages its registers on SPI)
gets a different one.

Values are formulas over the bytes that came back, in a small language that's parsed, never run as code:
numbers, + - * /, brackets and byte helpers: u8(i) s8(i) u16(i) s16(i) (big-endian), u16le(i) s16le(i), u24(i),
u32(i) u32le(i), with i the index of the first byte. E.g. "-45 + 175 * u16(0) / 65535".

Agents (MCP) may write here only when Settings → Agents allows it; what they save is marked "by agent, not checked"
until a person checks it. Checking and deleting are for people only.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

BYTE_FUNCS = {"u8": 1, "s8": 1, "u16": 2, "s16": 2, "u16le": 2, "s16le": 2, "u24": 3, "u32": 4, "u32le": 4}
TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([a-z][a-z0-9]*)|(.))")
NAME = r"^[A-Za-z0-9][A-Za-z0-9 ._+/-]*$"


class FormulaError(ValueError):
    pass


def check_formula(expr: str) -> int:
    """Parse a value formula; FormulaError if it isn't one. Returns how many bytes it reads (highest index + width)."""
    toks: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            break
        num, word, ch = m.groups()
        pos = m.end()
        if num:
            toks.append(("num", num))
        elif word:
            toks.append(("fn", word))
        elif ch and ch.strip():
            if ch not in "+-*/(),":
                raise FormulaError(f"not allowed in a formula: {ch!r}")
            toks.append(("op", ch))
    need = 0
    i = 0

    def peek():
        return toks[i] if i < len(toks) else ("end", "")

    def take(kind=None, value=None):
        nonlocal i
        t = peek()
        if (kind and t[0] != kind) or (value and t[1] != value):
            raise FormulaError(f"expected {value or kind} at {t[1] or 'the end'!r}")
        i += 1
        return t

    def atom():
        nonlocal need
        t = peek()
        if t == ("op", "("):
            take()
            expr_()
            take("op", ")")
        elif t == ("op", "-"):
            take()
            atom()
        elif t[0] == "num":
            take()
        elif t[0] == "fn":
            if t[1] not in BYTE_FUNCS:
                raise FormulaError(f"unknown function {t[1]!r} (use {', '.join(BYTE_FUNCS)})")
            take()
            take("op", "(")
            idx = take("num")[1]
            if not idx.isdigit():
                raise FormulaError("a byte index is a whole number")
            take("op", ")")
            need = max(need, int(idx) + BYTE_FUNCS[t[1]])
        else:
            raise FormulaError(f"unexpected {t[1] or 'end'!r}")

    def term():
        atom()
        while peek() in (("op", "*"), ("op", "/")):
            take()
            atom()

    def expr_():
        term()
        while peek() in (("op", "+"), ("op", "-")):
            take()
            term()

    if not expr.strip():
        raise FormulaError("empty formula")
    expr_()
    if i != len(toks):
        raise FormulaError(f"unexpected {peek()[1]!r}")
    return need


class Value(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    unit: str = Field("", max_length=16)
    expr: str = Field(min_length=1, max_length=200)

    @field_validator("expr")
    @classmethod
    def _formula(cls, v: str) -> str:
        try:
            check_formula(v)
        except FormulaError as e:
            raise ValueError(f"formula {v!r}: {e}") from e
        return v


class Register(BaseModel):
    addr: int = Field(ge=0, le=0xFFFF)
    name: str = Field(min_length=1, max_length=32)
    rw: Literal["r", "w", "rw"] = "rw"
    about: str = Field("", max_length=200)
    fields: str = Field("", max_length=300)
    bytes: int | None = Field(None, ge=1, le=64)
    values: list[Value] = Field(default_factory=list, max_length=8)


class Command(BaseModel):
    code: int = Field(ge=0, le=0xFFFF)
    name: str = Field(min_length=1, max_length=48)
    reply: int = Field(0, ge=0, le=64)
    wait_ms: float | None = Field(None, ge=0, le=10000)
    about: str = Field("", max_length=200)
    values: list[Value] = Field(default_factory=list, max_length=8)


class Bank(BaseModel):
    reg: int = Field(ge=0, le=0xFF)
    mask: int = Field(ge=1, le=0xFF)
    about: str = Field("", max_length=200)


class Table(BaseModel):
    """How the chip's messages go on one bus, and their register or command table."""
    style: Literal["registers", "commands", "switch", "basic"] = "basic"
    reg_bytes: Literal[1, 2] = 1
    byte_order: Literal["big", "little"] = "big"
    registers: list[Register] = Field(default_factory=list, max_length=256)
    commands: list[Command] = Field(default_factory=list, max_length=128)
    bank: Bank | None = None

    @field_validator("registers")
    @classmethod
    def _unique_regs(cls, v: list[Register]) -> list[Register]:
        seen = [r.addr for r in v]
        if len(seen) != len(set(seen)):
            raise ValueError("a register address is listed twice")
        return v

    @field_validator("commands")
    @classmethod
    def _unique_cmds(cls, v: list[Command]) -> list[Command]:
        seen = [c.code for c in v]
        if len(seen) != len(set(seen)):
            raise ValueError("a command code is listed twice")
        return v


class I2cBus(Table):
    bus: Literal["i2c"]
    addresses: list[int] = Field(min_length=1, max_length=16)
    ports: int | None = Field(None, ge=2, le=8)  # an I2C switch's number of ports

    @field_validator("addresses")
    @classmethod
    def _addresses(cls, v: list[int]) -> list[int]:
        if any(not 0x03 <= a <= 0x77 for a in v):
            raise ValueError("I2C addresses are 0x03 to 0x77 (7-bit)")
        return sorted(set(v))


class SpiBus(Table):
    bus: Literal["spi"]
    style: Literal["registers", "commands", "basic"] = "basic"
    read_bit: int = Field(0x80, ge=1, le=0xFF)  # the bit of the first byte that says read or write
    read_when_set: bool = True  # bit set = read (most chips); False: bit set = write
    inc_bit: int | None = Field(None, ge=1, le=0xFF)  # auto-increment ("MS") bit, if the chip has one
    mode: int | None = Field(None, ge=0, le=3)
    max_hz: int | None = Field(None, ge=1, le=200_000_000)

    @property
    def addr_mask(self) -> int:
        return 0xFF & ~self.read_bit & ~(self.inc_bit or 0)


Bus = Annotated[I2cBus | SpiBus, Field(discriminator="bus")]


class ChipIn(BaseModel):
    name: str = Field(min_length=1, max_length=40, pattern=NAME)
    vendor: str = Field("", max_length=60)
    kind: str = Field("", max_length=80)
    buses: list[Bus] = Field(min_length=1, max_length=4)
    datasheet: str = Field("", max_length=200)
    notes: str = Field("", max_length=2000)

    @field_validator("buses")
    @classmethod
    def _one_each(cls, v: list) -> list:
        kinds = [b.bus for b in v]
        if len(kinds) != len(set(kinds)):
            raise ValueError("each bus once (one entry for I2C, one for SPI)")
        return v

    def on(self, bus: str):
        return next((b for b in self.buses if b.bus == bus), None)


def table(c: dict | ChipIn) -> dict:
    """The stored body: what's filled in (defaults left out, easier to read and diff), but each bus entry keeps its
    bus and SPI its read/write bit."""
    m = c if isinstance(c, ChipIn) else ChipIn.model_validate(c)
    out = m.model_dump(exclude_defaults=True)
    out["buses"] = [{"bus": b.bus, **b.model_dump(exclude_defaults=True),
                     **({"read_bit": b.read_bit, "read_when_set": b.read_when_set} if b.bus == "spi" else {})}
                    for b in m.buses]
    return out


def _sht(precision: str, code: int, wait: float) -> dict:
    return {"code": code, "name": f"measure ({precision} precision)", "reply": 6, "wait_ms": wait,
            **({"about": "temperature (2 bytes + CRC), humidity (2 bytes + CRC)"} if precision == "high" else {}),
            "values": [{"name": "temperature", "unit": "°C", "expr": "-45 + 175 * u16(0) / 65535"},
                       {"name": "humidity", "unit": "%RH", "expr": "-6 + 125 * u16(3) / 65535"}]}


# Shipped with the tracker (source "built-in", not checked until you check them). Full tables for the chips on the
# first test board; the rest names and addresses (and SPI settings) only, ready for an agent to fill in.
BUILT_IN: list[dict] = [
    {"name": "TCA9548A", "vendor": "Texas Instruments", "kind": "I2C switch, 8 ports",
     "buses": [{"bus": "i2c", "addresses": list(range(0x70, 0x78)), "style": "switch", "ports": 8}],
     "datasheet": "TCA9548A low-voltage 8-channel I2C switch", "notes": "One control byte: bit n opens port n. 0x00 closes all ports."},
    {"name": "PCA9546A", "vendor": "NXP / Texas Instruments", "kind": "I2C switch, 4 ports",
     "buses": [{"bus": "i2c", "addresses": list(range(0x70, 0x78)), "style": "switch", "ports": 4}],
     "notes": "One control byte: bits 0-3 open ports 0-3."},
    {"name": "SHT4x", "vendor": "Sensirion", "kind": "Temperature and humidity", "datasheet": "SHT4x datasheet",
     "buses": [{"bus": "i2c", "addresses": [0x44, 0x45, 0x46], "style": "commands", "commands": [
         _sht("high", 0xFD, 8.3), _sht("medium", 0xF6, 4.5), _sht("low", 0xE0, 1.6),
         {"code": 0x89, "name": "read serial number", "reply": 6},
         {"code": 0x94, "name": "soft reset", "reply": 0, "wait_ms": 1},
     ]}]},
    {"name": "VEML7700", "vendor": "Vishay", "kind": "Ambient light", "datasheet": "VEML7700 datasheet",
     "buses": [{"bus": "i2c", "addresses": [0x10], "style": "registers", "reg_bytes": 2, "byte_order": "little", "registers": [
         {"addr": 0x00, "name": "ALS_CONF_0", "rw": "w", "about": "gain, integration time, interrupt, shut down",
          "fields": "bits 12:11 gain · 9:6 integration time · 1 interrupt enable · 0 shut down"},
         {"addr": 0x01, "name": "ALS_WH", "rw": "w", "about": "interrupt high threshold"},
         {"addr": 0x02, "name": "ALS_WL", "rw": "w", "about": "interrupt low threshold"},
         {"addr": 0x03, "name": "PSM", "rw": "w", "about": "power saving mode", "fields": "bits 2:1 mode · 0 enable"},
         {"addr": 0x04, "name": "ALS", "rw": "r", "about": "ambient light, counts (lux depends on gain and time)",
          "values": [{"name": "light", "unit": "counts", "expr": "u16le(0)"}]},
         {"addr": 0x05, "name": "WHITE", "rw": "r", "about": "white channel, counts",
          "values": [{"name": "white", "unit": "counts", "expr": "u16le(0)"}]},
         {"addr": 0x06, "name": "ALS_INT", "rw": "r", "about": "interrupt flags"},
         {"addr": 0x07, "name": "ID", "rw": "r", "about": "device ID"},
     ]}]},
    {"name": "AS7343", "vendor": "ams OSRAM", "kind": "Spectral, 14 channels", "datasheet": "AS7343 datasheet",
     "buses": [{"bus": "i2c", "addresses": [0x39], "style": "registers",
                "bank": {"reg": 0xBF, "mask": 0x10, "about": "REG_BANK: registers below 0x80 need bank 1"}, "registers": [
         {"addr": 0x5A, "name": "ID", "rw": "r", "about": "part number (0x81)"},
         {"addr": 0x80, "name": "ENABLE", "about": "power on and measurement on", "fields": "bit 1 SP_EN · bit 0 PON"},
         {"addr": 0x81, "name": "ATIME", "about": "integration time: number of steps"},
         {"addr": 0x90, "name": "STATUS2", "rw": "r", "about": "data valid and saturation", "fields": "bit 6 AVALID"},
         {"addr": 0x93, "name": "STATUS", "about": "interrupt flags, write to clear"},
         {"addr": 0x94, "name": "ASTATUS", "rw": "r", "about": "saturation and gain of this read"},
         {"addr": 0x95, "name": "DATA", "rw": "r", "bytes": 36, "about": "18 channels, 2 bytes each, from DATA_0_L"},
         {"addr": 0xBF, "name": "CFG0", "about": "register bank, low power", "fields": "bit 4 REG_BANK"},
         {"addr": 0xC6, "name": "CFG1", "about": "gain (AGAIN)"},
         {"addr": 0xD4, "name": "ASTEP", "bytes": 2, "about": "integration step size"},
         {"addr": 0xD6, "name": "CFG20", "about": "auto SMUX mode", "fields": "bits 6:5 auto_smux"},
     ]}]},
] + [
    # I2C and SPI: on SPI the first byte is the register address, bit 7 set = read (ST: bit 6 = auto-increment)
    {"name": n, "vendor": v, "kind": k, "buses": [{"bus": "i2c", "addresses": a}, {"bus": "spi", **spi}]} for n, v, k, a, spi in [
        ("BME280", "Bosch", "Temperature, humidity, pressure", [0x76, 0x77], {"read_bit": 0x80, "mode": 0}),
        ("BMP280", "Bosch", "Temperature and pressure", [0x76, 0x77], {"read_bit": 0x80, "mode": 0}),
        ("LIS3DH", "STMicroelectronics", "Accelerometer", [0x18, 0x19], {"read_bit": 0x80, "inc_bit": 0x40, "mode": 3}),
        ("LIS2DH12", "STMicroelectronics", "Accelerometer", [0x18, 0x19], {"read_bit": 0x80, "inc_bit": 0x40, "mode": 3}),
    ]
] + [
    {"name": n, "vendor": v, "kind": k, "buses": [{"bus": "i2c", "addresses": a}]} for n, v, k, a in [
        ("SHT3x", "Sensirion", "Temperature and humidity", [0x44, 0x45]),
        ("AHT20", "Aosong", "Temperature and humidity", [0x38]),
        ("HDC1080", "Texas Instruments", "Temperature and humidity", [0x40]),
        ("BME680", "Bosch", "Temperature, humidity, pressure, gas", [0x76, 0x77]),
        ("SCD4x", "Sensirion", "CO2, temperature, humidity", [0x62]),
        ("BH1750", "Rohm", "Ambient light", [0x23, 0x5C]),
        ("TSL2591", "ams OSRAM", "Ambient light", [0x29]),
        ("VL53L0X", "STMicroelectronics", "Time-of-flight distance", [0x29]),
        ("MPU-6050", "TDK InvenSense", "Accelerometer and gyroscope", [0x68, 0x69]),
        ("INA219", "Texas Instruments", "Current and power monitor", list(range(0x40, 0x50))),
        ("INA226", "Texas Instruments", "Current and power monitor", list(range(0x40, 0x50))),
        ("ADS1115", "Texas Instruments", "16-bit ADC, 4 channels", [0x48, 0x49, 0x4A, 0x4B]),
        ("MCP23017", "Microchip", "16-bit GPIO expander", list(range(0x20, 0x28))),
        ("PCF8574", "NXP / Texas Instruments", "8-bit GPIO expander", list(range(0x20, 0x28))),
        ("DS3231", "Analog Devices (Maxim)", "Real-time clock", [0x68]),
        ("PCF8563", "NXP", "Real-time clock", [0x51]),
        ("MAX17048", "Analog Devices (Maxim)", "Battery fuel gauge", [0x36]),
        ("AT24C32", "Microchip", "EEPROM, 4 KB", list(range(0x50, 0x58))),
        ("SSD1306", "Solomon Systech", "OLED display driver", [0x3C, 0x3D]),
    ]
]


# the api ------------------------------------------------------------------------------------------------------------
COLS = "id, name, body, source, agent, checked, created_at, updated_at"


def setup(app, q_one, q_all, mcp_settings, Jsonb) -> None:
    """GET/POST /api/chips, GET/PUT/DELETE /api/chips/{id}, POST /api/chips/{id}/check."""

    def is_agent(request: Request) -> str | None:
        """The MCP server's requests: the agent's name for the record, else None (a person in the web UI)."""
        if request.headers.get("x-pst-client") != "mcp":
            return None
        return request.headers.get("x-pst-agent") or "agent"

    async def may_write(request: Request) -> str | None:
        agent = is_agent(request)
        if agent and not (await mcp_settings()).get("library"):
            raise HTTPException(403, "agents may not edit the sensor library: switch it on in Settings → Agents")
        return agent

    async def used() -> dict[str, list[str]]:
        """Chip type name (lower case) -> the boards that use it."""
        out: dict[str, list[str]] = {}
        for d in await q_all("SELECT name, bus_chips FROM devices ORDER BY id"):
            for c in (d["bus_chips"] or {}).values():
                if c.get("type"):
                    out.setdefault(c["type"].lower(), []).append(d["name"])
        return out

    def row(r: dict, boards: dict) -> dict:
        return {**r, "boards": sorted(set(boards.get(r["name"].lower(), [])))}

    @app.get("/api/chips")
    async def list_chips():
        boards = await used()
        return [row(r, boards) for r in await q_all(f"SELECT {COLS} FROM chip_types ORDER BY lower(name)")]

    @app.get("/api/chips/{cid:int}")
    async def get_chip(cid: int):
        r = await q_one(f"SELECT {COLS} FROM chip_types WHERE id = %s", (cid,))
        if r is None:
            raise HTTPException(404, "no such chip")
        return row(r, await used())

    @app.post("/api/chips")
    async def create_chip(body: ChipIn, request: Request):
        agent = await may_write(request)
        if await q_one("SELECT id FROM chip_types WHERE lower(name) = lower(%s)", (body.name,)):
            raise HTTPException(409, f"{body.name} is in the library already: update it instead")
        return await q_one(f"INSERT INTO chip_types (name, body, source, agent, checked) VALUES (%s, %s, %s, %s, %s) "
                           f"RETURNING {COLS}", (body.name, Jsonb(table(body)), "agent" if agent else "you", agent, not agent))

    @app.put("/api/chips/{cid:int}")
    async def update_chip(cid: int, body: ChipIn, request: Request):
        """A person's edit counts as checked; an agent's edit makes it "not checked" again. A new name is followed
        on every board that uses the chip."""
        agent = await may_write(request)
        old = await q_one("SELECT name FROM chip_types WHERE id = %s", (cid,))
        if old is None:
            raise HTTPException(404, "no such chip")
        clash = await q_one("SELECT id FROM chip_types WHERE lower(name) = lower(%s) AND id <> %s", (body.name, cid))
        if clash:
            raise HTTPException(409, f"another chip is called {body.name}")
        r = await q_one(f"UPDATE chip_types SET name = %s, body = %s, source = %s, agent = %s, checked = %s, "
                        f"updated_at = now() WHERE id = %s RETURNING {COLS}",
                        (body.name, Jsonb(table(body)), "agent" if agent else "you", agent, not agent, cid))
        if old["name"] != body.name:
            for d in await q_all("SELECT id, bus_chips FROM devices"):
                chips = d["bus_chips"] or {}
                hit = {k: {**c, "type": body.name} for k, c in chips.items() if (c.get("type") or "").lower() == old["name"].lower()}
                if hit:
                    await q_one("UPDATE devices SET bus_chips = %s WHERE id = %s RETURNING id", (Jsonb({**chips, **hit}), d["id"]))
        return row(r, await used())

    @app.post("/api/chips/{cid:int}/check")
    async def check_chip(cid: int, request: Request, checked: bool = True):
        """A person compared the table with the datasheet (or takes that back). Not for agents."""
        if is_agent(request):
            raise HTTPException(403, "only a person can mark a chip table as checked")
        r = await q_one(f"UPDATE chip_types SET checked = %s WHERE id = %s RETURNING {COLS}", (checked, cid))
        if r is None:
            raise HTTPException(404, "no such chip")
        return row(r, await used())

    @app.delete("/api/chips/{cid:int}")
    async def delete_chip(cid: int, request: Request):
        if is_agent(request):
            raise HTTPException(403, "agents can't delete chips from the library")
        r = await q_one("DELETE FROM chip_types WHERE id = %s RETURNING name", (cid,))
        if r is None:
            raise HTTPException(404, "no such chip")
        return {"deleted": cid, "boards": (await used()).get(r["name"].lower(), [])}


async def seed(q_one, Jsonb) -> int:
    """Put the built-in chips in the library: the ones that aren't there yet (by name) are added, and built-in ones
    nobody changed get this version's table. Returns how many were added or updated."""
    n = 0
    for c in BUILT_IN:
        body = table(c)
        r = await q_one("SELECT id, body, source FROM chip_types WHERE lower(name) = lower(%s)", (c["name"],))
        if r is None:
            await q_one("INSERT INTO chip_types (name, body, source, checked) VALUES (%s, %s, 'built-in', false) RETURNING id",
                        (c["name"], Jsonb(body)))
            n += 1
        elif r["source"] == "built-in" and r["body"] != body:
            await q_one("UPDATE chip_types SET body = %s, updated_at = now() WHERE id = %s RETURNING id", (Jsonb(body), r["id"]))
            n += 1
    return n
