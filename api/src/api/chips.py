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

Built-in tables ship with the tracker and were checked against their datasheets by the project (`datasheet` names
the document and revision, `datasheet_url` links it), so they count as checked. Agents (MCP) may write here only when
Settings → Agents allows it; what they save is marked "by agent, not checked" until a person checks it. A built-in a
person or an agent changed stops getting the shipped updates until someone puts it back ("restore"). Checking,
restoring and deleting are for people only.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

BYTE_FUNCS = {"u8": 1, "s8": 1, "u16": 2, "s16": 2, "u16le": 2, "s16le": 2, "u24": 3, "u32": 4, "u32le": 4}
TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([a-z][a-z0-9]*)|(.))")
NAME = r"^[A-Za-z0-9][A-Za-z0-9 ._+/-]*$"
# what a chip is, for the library's type filter (the UI names them: Sensor, I2C switch, Memory, ...)
CATEGORIES = ("sensor", "switch", "memory", "clock", "display", "converter", "gpio", "other")
# what a chip measures, a fixed list so "temperature" finds every temperature sensor; a chip can have several
TAGS = ("temperature", "humidity", "pressure", "gas", "co2", "light", "spectral", "uv", "infrared", "acceleration",
        "rotation", "magnetic", "distance", "current", "voltage", "power", "battery", "analog")


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


def _byte_fn(fn: str, b: list[int], i: int) -> int | None:
    w = BYTE_FUNCS[fn]
    if i + w > len(b):
        return None
    x = b[i:i + w]
    if fn in ("u8", "s8"):
        v = x[0]
    elif fn.endswith("le"):
        v = int.from_bytes(bytes(x), "little")
    else:
        v = int.from_bytes(bytes(x), "big")
    signed = fn.startswith("s")
    return v - (1 << (8 * w)) if signed and v >= 1 << (8 * w - 1) else v


def evaluate(expr: str, data: list[int]) -> float | None:
    """A value formula over the bytes that came back (as the web UI's formula.ts does), or None: not a formula, too
    few bytes, or a division by zero."""
    toks = [m.group(1) or m.group(2) or m.group(3) for m in TOKEN.finditer(expr) if m.group(0).strip()]
    i = 0

    class Bad(Exception):
        pass

    def take(want=None):
        nonlocal i
        if i >= len(toks) or (want is not None and toks[i] != want):
            raise Bad
        i += 1
        return toks[i - 1]

    def atom() -> float:
        t = take()
        if t == "(":
            v = add()
            take(")")
            return v
        if t == "-":
            return -atom()
        if t[0].isdigit():
            return float(t)
        if t in BYTE_FUNCS:
            take("(")
            idx = take()
            take(")")
            if not idx.isdigit():
                raise Bad
            v = _byte_fn(t, data, int(idx))
            if v is None:
                raise Bad
            return float(v)
        raise Bad

    def mul() -> float:
        v = atom()
        while i < len(toks) and toks[i] in ("*", "/"):
            v = v * atom() if take() == "*" else v / atom()
        return v

    def add() -> float:
        v = mul()
        while i < len(toks) and toks[i] in ("+", "-"):
            v = v + mul() if take() == "+" else v - mul()
        return v

    try:
        v = add()
    except (Bad, ZeroDivisionError):
        return None
    return v if i == len(toks) else None


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
    category: Literal[CATEGORIES] | None = None  # type: ignore[valid-type]
    tags: list[Literal[TAGS]] = Field(default_factory=list, max_length=len(TAGS))  # type: ignore[valid-type]
    buses: list[Bus] = Field(min_length=1, max_length=4)
    datasheet: str = Field("", max_length=200)
    datasheet_url: str = Field("", max_length=300, pattern=r"^(https?://\S+)?$")
    notes: str = Field("", max_length=2000)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return [t for t in TAGS if t in v]  # each once, in the list's order

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


_SHT_VALUES = [{"name": "temperature", "unit": "°C", "expr": "-45 + 175 * u16(0) / 65535"},
               {"name": "humidity", "unit": "%RH", "expr": "-6 + 125 * u16(3) / 65535"}]
_SHT_ANSWER = "temperature (2 bytes + CRC), humidity (2 bytes + CRC)"


def _sht(name: str, code: int, wait: float, about: str = _SHT_ANSWER) -> dict:
    return {"code": code, "name": name, "reply": 6, "wait_ms": wait, "about": about, "values": _SHT_VALUES}


def _reg(addr: int, name: str, about: str, fields: str = "", rw: str = "rw", **more) -> dict:
    return {"addr": addr, "name": name, "rw": rw, "about": about, **({"fields": fields} if fields else {}), **more}


def _basic(name: str, vendor: str, kind: str, category: str, addresses: list[int], datasheet: str, url: str,
           notes: str = "", spi: dict | None = None, tags: tuple[str, ...] = ()) -> dict:
    """A chip with names, addresses (and SPI settings) only, no register table yet."""
    buses: list[dict] = [{"bus": "i2c", "addresses": addresses}]
    if spi:
        buses.append({"bus": "spi", **spi})
    return {"name": name, "vendor": vendor, "kind": kind, "category": category, **({"tags": list(tags)} if tags else {}), "buses": buses,
            "datasheet": datasheet, "datasheet_url": url, **({"notes": notes} if notes else {})}


_BOSCH_SPI = {"read_bit": 0x80, "max_hz": 10_000_000}  # bit 7 set = read; mode 0 or 3, picked by the clock's idle level
_ST_SPI = {"read_bit": 0x80, "inc_bit": 0x40, "mode": 3, "max_hz": 10_000_000}  # bit 7 = read, bit 6 = auto-increment

# Shipped with the tracker. Every table was checked against the datasheet named in it (document and revision; the link
# is the copy that was read), on 2026-11-12. Full tables for the chips on the first test board; the rest names,
# addresses and SPI settings only, ready for an agent or a person to fill in. Only put in what the datasheet says.
BUILT_IN: list[dict] = [
    {"name": "TCA9548A", "vendor": "Texas Instruments", "kind": "I2C switch, 8 ports", "category": "switch",
     "buses": [{"bus": "i2c", "addresses": list(range(0x70, 0x78)), "style": "switch", "ports": 8}],
     "datasheet": "TI TCA9548A, SCPS207H (revised September 2024)", "datasheet_url": "https://www.ti.com/lit/ds/symlink/tca9548a.pdf",
     "notes": "One control byte: bit n opens port n, several at once are allowed. 0x00 closes all ports, as after power-up "
              "or reset. Reading the switch returns the control byte. Address 0x70 + A2 A1 A0."},
    {"name": "PCA9546A", "vendor": "Texas Instruments / NXP", "kind": "I2C switch, 4 ports", "category": "switch",
     "buses": [{"bus": "i2c", "addresses": list(range(0x70, 0x78)), "style": "switch", "ports": 4}],
     "datasheet": "TI PCA9546A, SCPS148I (revised June 2022)", "datasheet_url": "https://www.ti.com/lit/ds/symlink/pca9546a.pdf",
     "notes": "One control byte: bits 0-3 open ports 0-3 (bits 4-7 unused). 0x00 closes all ports, as after power-up or "
              "reset. Address 0x70 + A2 A1 A0."},
    {"name": "SHT4x", "vendor": "Sensirion", "kind": "Temperature and humidity", "category": "sensor", "tags": ["temperature", "humidity"],
     "datasheet": "Sensirion SHT4x datasheet, version 7.3 (June 2026)",
     "datasheet_url": "https://sensirion.com/media/documents/33FD6951/6A7C10A0/HT_DS_Datasheet_SHT4x_V7.3.pdf",
     "notes": "The address is fixed per part: …-A… 0x44, …-B… 0x45, …-C… 0x46. Each 2-byte word comes with a CRC-8 "
              "(polynomial 0x31, start 0xFF). While it measures, the sensor answers a read with NACK. Waits are the "
              "datasheet's maximum.",
     "buses": [{"bus": "i2c", "addresses": [0x44, 0x45, 0x46], "style": "commands", "commands": [
         _sht("measure (high precision)", 0xFD, 8.3),
         _sht("measure (medium precision)", 0xF6, 4.5),
         _sht("measure (low precision)", 0xE0, 1.6),
         {"code": 0x89, "name": "read serial number", "reply": 6, "about": "2 words, each + CRC",
          "values": [{"name": "serial number", "expr": "u16(0) * 65536 + u16(3)"}]},
         {"code": 0x94, "name": "soft reset", "reply": 0, "wait_ms": 1},
         _sht("heater 200 mW, 1 s, then measure", 0x39, 1110, "heater on for 1 s, then a high-precision measurement"),
         _sht("heater 200 mW, 0.1 s, then measure", 0x32, 120, "heater on for 0.1 s, then a high-precision measurement"),
         _sht("heater 110 mW, 1 s, then measure", 0x2F, 1110, "heater on for 1 s, then a high-precision measurement"),
         _sht("heater 110 mW, 0.1 s, then measure", 0x24, 120, "heater on for 0.1 s, then a high-precision measurement"),
         _sht("heater 20 mW, 1 s, then measure", 0x1E, 1110, "heater on for 1 s, then a high-precision measurement"),
         _sht("heater 20 mW, 0.1 s, then measure", 0x15, 120, "heater on for 0.1 s, then a high-precision measurement"),
     ]}]},
    {"name": "VEML7700", "vendor": "Vishay", "kind": "Ambient light", "category": "sensor", "tags": ["light"],
     "datasheet": "Vishay VEML7700, document 84286, rev. 1.8 (28 November 2024)",
     "datasheet_url": "https://www.vishay.com/docs/84286/veml7700.pdf",
     "notes": "16-bit registers, low byte first. Lux = counts × resolution, which depends on gain and integration time "
              "(0.0042 lx per count at gain ×2 and 800 ms; the datasheet's table has the others). It starts shut down "
              "(ALS_CONF_0 bit 0 = 1).",
     "buses": [{"bus": "i2c", "addresses": [0x10], "style": "registers", "reg_bytes": 2, "byte_order": "little", "registers": [
         _reg(0x00, "ALS_CONF_0", "gain, integration time, persistence, interrupt, shut down",
              "bits 12:11 gain (00 ×1, 01 ×2, 10 ×1/8, 11 ×1/4) · 9:6 integration time (1100 25 ms, 1000 50 ms, "
              "0000 100 ms, 0001 200 ms, 0010 400 ms, 0011 800 ms) · 5:4 persistence · 1 interrupt enable · 0 shut down"),
         _reg(0x01, "ALS_WH", "interrupt high threshold"),
         _reg(0x02, "ALS_WL", "interrupt low threshold"),
         _reg(0x03, "PSM", "power saving mode", "bits 2:1 mode (00 mode 1 … 11 mode 4) · 0 enable"),
         _reg(0x04, "ALS", "ambient light, counts (lux depends on gain and integration time)", rw="r",
              values=[{"name": "light", "unit": "counts", "expr": "u16le(0)"}]),
         _reg(0x05, "WHITE", "white channel, counts", rw="r", values=[{"name": "white", "unit": "counts", "expr": "u16le(0)"}]),
         _reg(0x06, "ALS_INT", "interrupt flags", "bit 15 below the low threshold · 14 above the high threshold", rw="r"),
         _reg(0x07, "ID", "device ID: low byte 0x81, high byte 0xC4", rw="r"),
     ]}]},
    {"name": "AS7343", "vendor": "ams OSRAM", "kind": "Spectral, 14 channels", "category": "sensor", "tags": ["spectral", "infrared"],
     "datasheet": "ams OSRAM AS7343 datasheet DS001046, v6-00 (2023-06-07)",
     "datasheet_url": "https://cdn.sparkfun.com/assets/e/f/3/6/c/AS7343_DS001046_6-00.pdf",
     "notes": "Two-byte values are low byte first. Registers 0x58-0x7F need REG_BANK (CFG0 0xBF bit 4) set, 0x80 and up "
              "need it clear. Power on with PON first, configure, then set SP_EN. Which channel lands in DATA_0-17 "
              "depends on auto_smux (CFG20).",
     "buses": [{"bus": "i2c", "addresses": [0x39], "style": "registers",
                "bank": {"reg": 0xBF, "mask": 0x10, "about": "CFG0 REG_BANK: set for registers 0x58-0x7F, clear for 0x80 and up"},
                "registers": [
         _reg(0x58, "AUXID", "auxiliary ID", "bits 3:0 AUXID", rw="r"),
         _reg(0x59, "REVID", "revision ID", "bits 2:0 REVID", rw="r"),
         _reg(0x5A, "ID", "part number (0x81)", rw="r"),
         _reg(0x65, "CFG10", "flicker detect persistence", "bits 2:0 FD_PERS"),
         _reg(0x66, "CFG12", "channel for the spectral thresholds", "bits 2:0 SP_TH_CH"),
         _reg(0x6B, "GPIO", "GPIO pin", "bit 3 GPIO_INV · 2 GPIO_IN_EN · 1 GPIO_OUT · 0 GPIO_IN (read)"),
         _reg(0x80, "ENABLE", "power on, measurement, wait, SMUX, flicker detect",
              "bit 6 FDEN · 4 SMUXEN · 3 WEN · 1 SP_EN · 0 PON"),
         _reg(0x81, "ATIME", "integration time: number of steps"),
         _reg(0x83, "WTIME", "wait time between measurements"),
         _reg(0x84, "SP_TH_L", "spectral low threshold", bytes=2),
         _reg(0x86, "SP_TH_H", "spectral high threshold", bytes=2),
         _reg(0x90, "STATUS2", "data valid and saturation",
              "bit 6 AVALID · 4 ASAT_DIGITAL · 3 ASAT_ANALOG · 1 FDSAT_ANALOG · 0 FDSAT_DIGITAL", rw="r"),
         _reg(0x91, "STATUS3", "spectral threshold interrupts", "bit 5 INT_SP_H · 4 INT_SP_L", rw="r"),
         _reg(0x93, "STATUS", "interrupt flags, write 1 to clear", "bit 7 ASAT · 3 AINT · 2 FINT · 0 SINT"),
         _reg(0x94, "ASTATUS", "saturation and gain of the data read after it", "bit 7 ASAT_STATUS · 3:0 AGAIN_STATUS", rw="r"),
         _reg(0x95, "DATA", "18 channels, 2 bytes each (DATA_0_L … DATA_17_H)", rw="r", bytes=36),
         _reg(0xBB, "STATUS5", "flicker and SMUX interrupts", "bit 3 SINT_FD · 2 SINT_SMUX", rw="r"),
         _reg(0xBC, "STATUS4", "FIFO overflow, temperature, timing errors", "bit 7 FIFO_OV · 5 OVTEMP · 4 FD_TRIG", rw="r"),
         _reg(0xBF, "CFG0", "register bank, low power, long wait", "bit 5 LOW_POWER · 4 REG_BANK · 2 WLONG"),
         _reg(0xC6, "CFG1", "spectral gain", "bits 4:0 AGAIN (0 = 0.5×, 1 = 1× … 12 = 2048×)"),
         _reg(0xC7, "CFG3", "sleep after interrupt", "bit 4 SAI"),
         _reg(0xC9, "CFG8", "FIFO threshold", "bits 7:6 FIFO_TH"),
         _reg(0xCA, "CFG9", "system interrupts for flicker and SMUX", "bit 6 SIEN_FD · 4 SIEN_SMUX"),
         _reg(0xCD, "LED", "LED driver on the LDR pin", "bit 7 LED_ACT · 6:0 LED_DRIVE (0 = 4 mA, +2 mA per step)"),
         _reg(0xCF, "PERS", "spectral interrupt persistence", "bits 3:0 APERS"),
         _reg(0xD4, "ASTEP", "integration step size, 2.78 µs × (n + 1)", bytes=2,
              values=[{"name": "step", "unit": "µs", "expr": "2.78 * (u16le(0) + 1)"}]),
         _reg(0xD6, "CFG20", "flicker FIFO, automatic channel read-out",
              "bit 7 FD_FIFO_8b · 6:5 auto_smux (0 6 channels, 2 12 channels, 3 18 channels)"),
         _reg(0xD7, "AGC_GAIN_MAX", "highest flicker detect gain", "bits 7:4 AGC_FD_GAIN_MAX"),
         _reg(0xDE, "AZ_CONFIG", "how often to autozero"),
         _reg(0xDF, "FD_CFG0", "flicker data into the FIFO", "bit 7 FIFO_WRITE_FD"),
         _reg(0xE0, "FD_TIME_1", "flicker detect integration time, low bits"),
         _reg(0xE2, "FD_TIME_2", "flicker detect gain and integration time", "bits 7:3 FD_GAIN · 2:0 FD_TIME high bits"),
         _reg(0xE3, "FD_STATUS", "flicker detect result",
              "bit 5 valid · 4 saturated · 3 120 Hz valid · 2 100 Hz valid · 1 120 Hz · 0 100 Hz", rw="r"),
         _reg(0xF5, "CFG6", "SMUX command", "bits 4:3 SMUX_CMD"),
         _reg(0xF9, "INTENAB", "interrupt enables", "bit 7 ASIEN · 3 SP_IEN · 2 F_IEN · 0 SIEN"),
         _reg(0xFA, "CONTROL", "reset, autozero, FIFO clear", "bit 3 SW_RESET · 2 SP_MAN_AZ · 1 FIFO_CLR · 0 CLEAR_SAI_ACT"),
         _reg(0xFC, "FIFO_MAP", "which channels go into the FIFO", "bits 6:1 channels 5-0 · 0 ASTATUS"),
         _reg(0xFD, "FIFO_LVL", "FIFO level (2-byte entries)", rw="r"),
         _reg(0xFE, "FDATA", "FIFO data", rw="r", bytes=2),
     ]}]},
    _basic("BME280", "Bosch Sensortec", "Temperature, humidity, pressure", "sensor", [0x76, 0x77],
           "Bosch BME280 datasheet BST-BME280-DS001-24, revision 1.24 (February 2024)",
           "https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme280-ds002.pdf",
           "chip_id register 0xD0 reads 0x60. SPI: register address bit 7 set = read, mode 0 or 3, up to 10 MHz.", _BOSCH_SPI, tags=('temperature', 'humidity', 'pressure')),
    _basic("BMP280", "Bosch Sensortec", "Temperature and pressure", "sensor", [0x76, 0x77],
           "Bosch BMP280 datasheet BST-BMP280-DS001-26, revision 1.26 (October 2021)",
           "https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bmp280-ds001.pdf",
           "chip_id register 0xD0 reads 0x58. SPI: register address bit 7 set = read, mode 0 or 3, up to 10 MHz.", _BOSCH_SPI, tags=('temperature', 'pressure')),
    _basic("BME680", "Bosch Sensortec", "Temperature, humidity, pressure, gas", "sensor", [0x76, 0x77],
           "Bosch BME680 datasheet BST-BME680-DS001-09, revision 1.9 (February 2024)",
           "https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bme680-ds001.pdf",
           "chip_id reads 0x61 (I2C 0xD0, SPI 0x50 on page 0). On SPI the registers come in two pages: spi_mem_page "
           "(bit 4 of status, 0x73) = 0 for 0x80-0xFF, 1 for 0x00-0x7F.",
           {**_BOSCH_SPI, "bank": {"reg": 0x73, "mask": 0x10, "about": "status spi_mem_page: 1 for registers 0x00-0x7F, 0 for 0x80-0xFF"}}, tags=('temperature', 'humidity', 'pressure', 'gas')),
    _basic("LIS3DH", "STMicroelectronics", "Accelerometer", "sensor", [0x18, 0x19],
           "ST LIS3DH datasheet DocID17530, rev. 2 (December 2016)", "https://www.st.com/resource/en/datasheet/lis3dh.pdf",
           "Address 0x18 + SA0. SPI: bit 7 of the first byte = read, bit 6 = auto-increment, mode 3, up to 10 MHz.", _ST_SPI, tags=('acceleration',)),
    _basic("LIS2DH12", "STMicroelectronics", "Accelerometer", "sensor", [0x18, 0x19],
           "ST LIS2DH12 datasheet DocID025056, rev. 6 (May 2017)", "https://www.st.com/resource/en/datasheet/lis2dh12.pdf",
           "Address 0x18 + SA0. SPI: bit 7 of the first byte = read, bit 6 = auto-increment, mode 3, up to 10 MHz.", _ST_SPI, tags=('acceleration',)),
    _basic("SHT3x", "Sensirion", "Temperature and humidity", "sensor", [0x44, 0x45],
           "Sensirion SHT3x-DIS datasheet, version 7 (December 2022)", "https://sensirion.com/file/datasheet_sht3x_digital",
           "Address 0x44 with ADDR low, 0x45 with ADDR high. 16-bit commands.", tags=('temperature', 'humidity')),
    _basic("AHT20", "Aosong", "Temperature and humidity", "sensor", [0x38],
           "Aosong AHT20 datasheet, version 1.0 (May 2021)", "http://www.aosong.com/userfiles/files/media/Data%20Sheet%20AHT20.pdf", tags=('temperature', 'humidity')),
    _basic("HDC1080", "Texas Instruments", "Temperature and humidity", "sensor", [0x40],
           "TI HDC1080, SNAS672A (revised January 2016)", "https://www.ti.com/lit/ds/symlink/hdc1080.pdf", tags=('temperature', 'humidity')),
    _basic("SCD4x", "Sensirion", "CO2, temperature, humidity", "sensor", [0x62],
           "Sensirion SCD4x datasheet, version 1.5 (July 2023)",
           "https://cdn.sparkfun.com/assets/9/7/5/7/0/Sensirion_SCD4x_Datasheet.pdf", "16-bit commands.", tags=('co2', 'temperature', 'humidity')),
    _basic("BH1750", "Rohm", "Ambient light", "sensor", [0x23, 0x5C],
           "Rohm BH1750FVI datasheet No.11046EDT01, rev. D (November 2011)",
           "https://wemos-docs.readthedocs.io/en/latest/_static/files/bh1750fvi-tr.pdf",
           "Address 0x23 with ADDR low, 0x5C with ADDR high.", tags=('light',)),
    _basic("TSL2591", "ams OSRAM", "Ambient light", "sensor", [0x29],
           "ams TSL2591 datasheet ams163.5 (April 2013)", "https://cdn-shop.adafruit.com/datasheets/TSL25911_Datasheet_EN_v1.pdf", tags=('light', 'infrared')),
    _basic("VL53L0X", "STMicroelectronics", "Time-of-flight distance", "sensor", [0x29],
           "ST VL53L0X datasheet DS11555, rev. 6 (June 2024)", "https://www.st.com/resource/en/datasheet/vl53l0x.pdf",
           "0x29 is the default (0x52 as an 8-bit address); software can change it.", tags=('distance',)),
    _basic("MPU-6050", "TDK InvenSense", "Accelerometer and gyroscope", "sensor", [0x68, 0x69],
           "InvenSense MPU-6000/MPU-6050 product specification PS-MPU-6000A-00, revision 3.1",
           "https://cdn.sparkfun.com/datasheets/Components/General%20IC/PS-MPU-6000A.pdf",
           "Address 0x68 with AD0 low, 0x69 with AD0 high. The MPU-6050 has I2C only (the MPU-6000 adds SPI).", tags=('acceleration', 'rotation')),
    _basic("INA219", "Texas Instruments", "Current and power monitor", "converter", list(range(0x40, 0x50)),
           "TI INA219, SBOS448G (revised December 2015)", "https://www.ti.com/lit/ds/symlink/ina219.pdf",
           "16 addresses from A1 and A0 (each to GND, VS, SDA or SCL).", tags=('current', 'voltage', 'power')),
    _basic("INA226", "Texas Instruments", "Current and power monitor", "converter", list(range(0x40, 0x50)),
           "TI INA226, SBOS547C (revised August 2026)", "https://www.ti.com/lit/ds/symlink/ina226.pdf",
           "16 addresses from A1 and A0 (each to GND, VS, SDA or SCL).", tags=('current', 'voltage', 'power')),
    _basic("ADS1115", "Texas Instruments", "16-bit ADC, 4 channels", "converter", [0x48, 0x49, 0x4A, 0x4B],
           "TI ADS111x, SBAS444E (revised December 2024)", "https://www.ti.com/lit/ds/symlink/ads1115.pdf",
           "ADDR to GND 0x48, VDD 0x49, SDA 0x4A, SCL 0x4B.", tags=('analog',)),
    _basic("MAX17048", "Analog Devices (Maxim)", "Battery fuel gauge", "converter", [0x36],
           "Maxim MAX17048/MAX17049, 19-6171 rev. 7 (November 2016)",
           "https://www.analog.com/media/en/technical-documentation/data-sheets/MAX17048-MAX17049.pdf", tags=('battery', 'voltage')),
    _basic("MCP23017", "Microchip", "16-bit GPIO expander", "gpio", list(range(0x20, 0x28)),
           "Microchip MCP23017/MCP23S17, DS20001952D (2022)",
           "https://ww1.microchip.com/downloads/aemDocuments/documents/APID/ProductDocuments/DataSheets/MCP23017-Data-Sheet-DS20001952.pdf",
           "Address 0x20 + A2 A1 A0. The SPI version is the MCP23S17."),
    _basic("PCF8574", "Texas Instruments / NXP", "8-bit GPIO expander", "gpio", list(range(0x20, 0x28)),
           "TI PCF8574, SCPS068K (revised September 2024)", "https://www.ti.com/lit/ds/symlink/pcf8574.pdf",
           "Address 0x20 + A2 A1 A0; no registers, a write sets the 8 pins and a read returns them. The PCF8574A "
           "answers at 0x38-0x3F instead."),
    _basic("DS3231", "Analog Devices (Maxim)", "Real-time clock", "clock", [0x68],
           "Maxim DS3231, 19-5170 rev. 10 (March 2015)", "https://www.analog.com/media/en/technical-documentation/data-sheets/DS3231.pdf"),
    _basic("PCF8563", "NXP", "Real-time clock", "clock", [0x51],
           "NXP PCF8563 product data sheet, rev. 10 (3 April 2012)", "https://www.nxp.com/docs/en/data-sheet/PCF8563.pdf",
           "Address 0x51 (A2h to write, A3h to read)."),
    _basic("AT24C32", "Microchip (Atmel)", "EEPROM, 4 KB", "memory", list(range(0x50, 0x58)),
           "Atmel AT24C32/AT24C64, 0336K (July 2003)", "https://ww1.microchip.com/downloads/en/DeviceDoc/doc0336.pdf",
           "Address 0x50 + A2 A1 A0. A 2-byte memory address follows the device address."),
    _basic("SSD1306", "Solomon Systech", "OLED display driver", "display", [0x3C, 0x3D],
           "Solomon Systech SSD1306, rev. 1.1 (April 2008)", "https://cdn-shop.adafruit.com/datasheets/SSD1306.pdf",
           "Address 0x3C with SA0 (D/C#) low, 0x3D with it high. Its SPI mode uses a D/C# pin instead of a register "
           "address, so it isn't in the library for SPI."),
]


SHIPPED = {c["name"].lower(): c for c in BUILT_IN}


# the api ------------------------------------------------------------------------------------------------------------
COLS = "id, name, body, source, agent, checked, created_at, updated_at"


def setup(app, q_one, q_all, mcp_settings, Jsonb) -> None:
    """GET/POST /api/chips, GET/PUT/DELETE /api/chips/{id}, POST /api/chips/{id}/check and /restore."""

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
        """built_in: the tracker ships a table by this name (when source isn't "built-in", someone changed it)."""
        return {**r, "boards": sorted(set(boards.get(r["name"].lower(), []))), "built_in": r["name"].lower() in SHIPPED}

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
        r = await q_one(f"INSERT INTO chip_types (name, body, source, agent, checked) VALUES (%s, %s, %s, %s, %s) "
                        f"RETURNING {COLS}", (body.name, Jsonb(table(body)), "agent" if agent else "you", agent, not agent))
        return row(r, await used())

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
        r = await q_one(f"UPDATE chip_types SET checked = %s WHERE id = %s AND source <> 'built-in' RETURNING {COLS}", (checked, cid))
        if r is None:
            if await q_one("SELECT id FROM chip_types WHERE id = %s", (cid,)):
                raise HTTPException(409, "built-in tables are checked against their datasheet by the project")
            raise HTTPException(404, "no such chip")
        return row(r, await used())

    @app.post("/api/chips/{cid:int}/restore")
    async def restore_chip(cid: int, request: Request):
        """Put a changed built-in back to the table this version ships (it gets later updates again). Not for agents."""
        if is_agent(request):
            raise HTTPException(403, "only a person can put a chip back to its built-in table")
        old = await q_one("SELECT name FROM chip_types WHERE id = %s", (cid,))
        if old is None:
            raise HTTPException(404, "no such chip")
        shipped = SHIPPED.get(old["name"].lower())
        if shipped is None:
            raise HTTPException(409, f"{old['name']} isn't a built-in chip")
        r = await q_one(f"UPDATE chip_types SET name = %s, body = %s, source = 'built-in', agent = NULL, checked = true, "
                        f"updated_at = now() WHERE id = %s RETURNING {COLS}", (shipped["name"], Jsonb(table(shipped)), cid))
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
    nobody changed get this version's table (built-ins count as checked: the project checked them against their
    datasheets). Returns how many were added or updated."""
    n = 0
    for c in BUILT_IN:
        body = table(c)
        r = await q_one("SELECT id, body, source, checked FROM chip_types WHERE lower(name) = lower(%s)", (c["name"],))
        if r is None:
            await q_one("INSERT INTO chip_types (name, body, source, checked) VALUES (%s, %s, 'built-in', true) RETURNING id",
                        (c["name"], Jsonb(body)))
            n += 1
        elif r["source"] == "built-in" and (r["body"] != body or not r["checked"]):
            await q_one("UPDATE chip_types SET body = %s, checked = true, updated_at = now() WHERE id = %s RETURNING id",
                        (Jsonb(body), r["id"]))
            n += 1
    return n
