"""Decode a window of raw samples (one byte per sample, D0-D7) with sigrok's I2C and SPI decoders.

sigrok-cli reads the raw bytes ("binary" input: its channels are then named 0-7) and prints annotations with
their sample numbers ("--protocol-decoder-samplenum"):

    50-50 i2c-1: Start
    65-170 i2c-1: Address write: 44
    200-320 i2c-1: Data write: FD
    1335-1350 i2c-1: NACK
    50-380 spi-1: 9F 00 00 00          (one transfer: chip select low to high)

These become transactions: I2C from start to stop (a repeated start opens a new part: register write, then read),
SPI per chip-select period with what went out (MOSI) and came back (MISO).
"""

from __future__ import annotations

import re
import subprocess
import tempfile

DECODE_TIMEOUT_S = 120.0
LINE = re.compile(r"^(\d+)-(\d+) (\w+)-\d+: (.*)$")
I2C_ANN = "start:repeat-start:stop:ack:nack:address-read:address-write:data-read:data-write"
PINS = tuple(f"D{i}" for i in range(8))


class DecodeError(RuntimeError):
    pass


def _ch(pin: str) -> str:
    """D3 -> 3: the raw input names its channels by number."""
    if pin not in PINS:
        raise DecodeError(f"not a channel: {pin!r}")
    return pin[1:]


def run_sigrok(data: bytes, rate: int, decoder: str, annotations: str, run=subprocess.run) -> list[tuple[int, int, str]]:
    """(start sample, end sample, text) of each annotation."""
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(data)
        f.flush()
        try:
            p = run(["sigrok-cli", "-I", f"binary:numchannels=8:samplerate={rate}", "-i", f.name, "-P", decoder,
                     "--protocol-decoder-samplenum", "-A", annotations],
                    capture_output=True, text=True, timeout=DECODE_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            raise DecodeError("decoding took too long (a shorter window or a lower sample rate helps)") from e
    if p.returncode != 0:
        raise DecodeError((p.stderr or "sigrok-cli failed").strip().splitlines()[-1])
    out = []
    for line in p.stdout.splitlines():
        m = LINE.match(line.strip())
        if m:
            out.append((int(m[1]), int(m[2]), m[4]))
    return out


def i2c_transactions(anns: list[tuple[int, int, str]]) -> list[dict]:
    """Annotations of sigrok's i2c decoder -> [{start, end, addr, parts: [{rw, bytes}], nack}].

    nack: a device didn't answer its address or refused a byte written to it (not the NACK that ends a read).
    """
    out, cur, prev = [], None, ""
    for a, b, text in sorted(anns, key=lambda x: (x[0], x[1])):
        if text in ("Start", "Start repeat") and cur is None:
            cur = {"start": a, "end": b, "addr": None, "parts": [], "nack": False}
        elif cur is None:
            continue  # the window began inside a transaction
        if text.startswith(("Address write:", "Address read:")):
            addr = int(text.split(":")[1], 16)
            cur["addr"] = cur["addr"] if cur["addr"] is not None else addr
            cur["parts"].append({"rw": "R" if "read" in text else "W", "addr": addr, "bytes": []})
        elif text.startswith(("Data write:", "Data read:")) and cur["parts"]:
            cur["parts"][-1]["bytes"].append(int(text.split(":")[1], 16))
        elif text == "NACK" and not prev.startswith("Data read"):
            cur["nack"] = True  # after a read byte a NACK is normal: the controller says "that was the last one"
        cur["end"] = max(cur["end"], b)
        prev = text
        if text == "Stop":
            if cur["parts"]:
                out.append(cur)
            cur = None
    return out


def i2c_label(t: dict) -> str:
    """0x44 W1 R6 (NACK)"""
    parts = " ".join(f"{p['rw']}{len(p['bytes'])}" for p in t["parts"])
    return f"0x{t['addr']:02X} {parts}" + (" NACK" if t["nack"] else "")


def spi_transfers(mosi: list[tuple[int, int, str]], miso: list[tuple[int, int, str]]) -> list[dict]:
    """sigrok's spi transfer annotations (one run per direction) -> [{start, end, mosi: [..], miso: [..]}]."""
    by_span: dict[tuple[int, int], dict] = {}
    for key, anns in (("mosi", mosi), ("miso", miso)):
        for a, b, text in anns:
            t = by_span.setdefault((a, b), {"start": a, "end": b, "mosi": [], "miso": []})
            t[key] = [int(x, 16) for x in text.split()]
    return sorted(by_span.values(), key=lambda t: t["start"])


def decode(data: bytes, rate: int, wiring: dict, run=subprocess.run) -> dict:
    """Decode the buses wired up: {"i2c": [...], "spi": [...]} with sample numbers relative to `data`."""
    out = {"i2c": [], "spi": []}
    if wiring.get("i2c_sda", "off") != "off" and wiring.get("i2c_scl", "off") != "off":
        pd = f"i2c:scl={_ch(wiring['i2c_scl'])}:sda={_ch(wiring['i2c_sda'])}"
        out["i2c"] = i2c_transactions(run_sigrok(data, rate, pd, f"i2c={I2C_ANN}", run))
    if wiring.get("spi_clk", "off") != "off" and (wiring.get("spi_mosi", "off") != "off" or wiring.get("spi_miso", "off") != "off"):
        pd = f"spi:clk={_ch(wiring['spi_clk'])}"
        for key in ("mosi", "miso", "cs"):
            if wiring.get(f"spi_{key}", "off") != "off":
                pd += f":{key}={_ch(wiring[f'spi_{key}'])}"
        mosi = run_sigrok(data, rate, pd, "spi=mosi-transfer", run) if wiring.get("spi_mosi", "off") != "off" else []
        miso = run_sigrok(data, rate, pd, "spi=miso-transfer", run) if wiring.get("spi_miso", "off") != "off" else []
        out["spi"] = spi_transfers(mosi, miso)
    return out


def check_wiring(w: dict) -> None:
    """Each pin used once; I2C needs both its pins."""
    used = [v for k, v in w.items() if k.startswith(("i2c_", "spi_")) and v != "off"]
    if len(used) != len(set(used)):
        raise DecodeError("a pin is used twice")
    if (w.get("i2c_sda", "off") == "off") != (w.get("i2c_scl", "off") == "off"):
        raise DecodeError("I2C needs both SDA and SCL")



_MASKS: dict[int, bytes] = {}


def bus_pins(wiring: dict) -> int:
    """Bit mask of the channels a bus is wired to (D0 = bit 0)."""
    m = 0
    for k, v in wiring.items():
        if k.startswith(("i2c_", "spi_")) and v in PINS:
            m |= 1 << int(v[1:])
    return m


def quiet(data: bytes, wiring: dict) -> bool:
    """No edge on any bus pin in these samples: nothing to decode. Fast (bytes.translate and count run in C)."""
    mask = bus_pins(wiring)
    if not data or not mask:
        return True
    table = _MASKS.get(mask) or _MASKS.setdefault(mask, bytes(i & mask for i in range(256)))
    t = data.translate(table)
    return t.count(t[:1]) == len(t)
