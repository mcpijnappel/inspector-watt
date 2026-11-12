"""A session's bus messages named the way the Bus panel names them, for agents (MCP session_bus).

The web UI does this in the browser (frontend/src/lib/busfold.ts, BusPanel.svelte); this is the same in Python, so
an agent gets "VEML7700_2 reads ALS: 412 counts" instead of address and bytes. Keep the two in step:

- Chip: "m:<address>" on the main bus, "<port>:<address>" behind an I2C switch. A switch's one-byte writes open
  ports (bit n = port n); a chip seen while the switch has all ports closed is on the main bus. Before the first
  switch write (or after a gap of 2 s: another decoded window) the port isn't known: a chip that shows up behind a
  port elsewhere is "?:<address>", anything else counts as the main bus.
- Kind: probe (address only), reg (write register, read), set (write register + data), cmd (a write and its answer
  read a little later), write, read; SPI with the chip's library table: reg / set from the first byte's read bit.
- From the library (the chip types the board's chip list names): the register's or command's name, and readings
  from its value formulas.
"""

from __future__ import annotations

import statistics

from .chips import evaluate

RESYNC_GAP_S = 2.0
CMD_ANSWER_S = 0.1  # a command's answer comes within this
SPI_KEY = "spi"


def hx(b: list[int] | None) -> str:
    return " ".join(f"{x:02X}" for x in (b or []))


def _parts(e: dict) -> list[dict]:
    return (e.get("data") or {}).get("parts") or []


def _is_switch_write(e: dict, switches: set[int]) -> bool:
    p = _parts(e)
    return (e["kind"] == "i2c" and e["data"].get("addr") in switches and len(p) == 1 and p[0]["rw"] == "W"
            and len(p[0]["bytes"]) == 1)


def guess_switches(events: list[dict]) -> set[int]:
    """Addresses that look like an I2C switch: 0x70-0x77 and only ever one-byte writes."""
    seen: dict[int, bool] = {}
    for e in events:
        a = e["data"].get("addr")
        if e["kind"] != "i2c" or a is None or not 0x70 <= a <= 0x77:
            continue
        p = _parts(e)
        seen[a] = seen.get(a, True) and len(p) == 1 and p[0]["rw"] == "W" and len(p[0]["bytes"]) == 1
    return {a for a, one in seen.items() if one}


def switches_of(events: list[dict], chips: dict, switch_types: set[str]) -> set[int]:
    """Guessed from the traffic, unless the board's chip list says otherwise (a switch flag, or a switch type)."""
    out = guess_switches(events)
    for key, c in chips.items():
        if not key.startswith("m:"):
            continue
        a = int(key.split(":")[1])
        sw = c.get("switch")
        if sw is None and c.get("type"):
            sw = c["type"].lower() in switch_types
        if sw is None:
            continue
        (out.add if sw else out.discard)(a)
    return out


def to_items(events: list[dict], switches: set[int], spi: dict | None = None) -> list[dict]:
    """Messages (t in seconds, data as the analyzer sends it) -> items: chip key, kind and bytes."""
    def walk():
        st, last = ("unknown", None), None
        for e in events:
            if last is not None and e["t"] - last > RESYNC_GAP_S:
                st = ("unknown", None)
            last = e["t"]
            if _is_switch_write(e, switches):
                b = _parts(e)[0]["bytes"][0]
                st = ("closed", None) if b == 0 else ("open", "+".join(str(k) for k in range(8) if b & (1 << k)))
            yield e, st

    main, behind = set(), set()
    states = []
    for e, st in walk():
        states.append(st)
        a = e["data"].get("addr")
        if a is None or _is_switch_write(e, switches):
            continue
        (main if st[0] == "closed" else behind if st[0] == "open" else set()).add(a)

    def port_of(a: int, st) -> str | None:
        if not switches or a in main:
            return None
        if st[0] == "open":
            return st[1]
        return "?" if st[0] == "unknown" and a in behind else None

    out, used = [], set()
    for i, e in enumerate(events):
        if i in used:
            continue
        d = e.get("data") or {}
        base = {"t": e["t"], "end_t": e.get("end_t")}
        if e["kind"] == "spi":
            mosi, miso = d.get("mosi") or [], d.get("miso") or []
            base |= {"key": SPI_KEY, "addr": None, "port": None, "nack": False}
            if spi and mosi:
                bit = spi.get("read_bit", 0x80)
                is_set = bool(mosi[0] & bit)
                read = is_set if spi.get("read_when_set", True) else not is_set
                reg = [mosi[0] & 0xFF & ~bit & ~(spi.get("inc_bit") or 0)]
                out.append({**base, "kind": "reg", "reg": reg, "r": miso[1:]} if read
                           else {**base, "kind": "set", "reg": reg, "w": mosi[1:]})
            else:
                out.append({**base, "kind": "spi", "mosi": mosi, "miso": miso})
            continue
        if e["kind"] != "i2c" or _is_switch_write(e, switches):
            continue
        a, parts, nack = d.get("addr") or 0, _parts(e), bool(d.get("nack"))
        port = port_of(a, states[i])
        base |= {"key": f"{port or 'm'}:{a}", "addr": a, "port": port, "nack": nack}
        if not parts:
            continue
        p0 = parts[0]
        if len(parts) == 1 and p0["rw"] == "W" and not p0["bytes"]:
            out.append({**base, "kind": "probe", "ok": not nack})
        elif len(parts) == 2 and p0["rw"] == "W" and parts[1]["rw"] == "R":
            out.append({**base, "kind": "reg", "reg": p0["bytes"], "r": parts[1]["bytes"]})
        elif len(parts) == 1 and p0["rw"] == "W" and len(p0["bytes"]) >= 2:
            out.append({**base, "kind": "set", "reg": p0["bytes"][:1], "w": p0["bytes"][1:]})
        elif len(parts) == 1 and p0["rw"] == "W":
            j = None  # its answer: a separate read from the same chip a little later (SHT4x: FD, then 6 bytes)
            for n in range(i + 1, min(len(events), i + 6)):
                x = events[n]
                if x["t"] - e["t"] > CMD_ANSWER_S:
                    break
                xp = _parts(x)
                if x["data"].get("addr") == a and len(xp) == 1 and xp[0]["rw"] == "R":
                    j = n
                    break
            if j is not None:
                used.add(j)
                out.append({**base, "kind": "cmd", "w": p0["bytes"], "r": _parts(events[j])[0]["bytes"],
                            "wait_ms": round((events[j]["t"] - e["t"]) * 1000, 2)})
            else:
                out.append({**base, "kind": "write", "w": p0["bytes"]})
        else:
            out.append({**base, "kind": "read", "r": [b for p in parts for b in p["bytes"]]})
    return out


def on_bus(body: dict | None, bus: str) -> dict | None:
    return next((b for b in (body or {}).get("buses", []) if b.get("bus") == bus), None)


def fmt_value(v: float) -> str:
    """As the Bus panel shows a reading (formula.ts fmtValue)."""
    a = abs(v)
    if a >= 1000 or float(v).is_integer():
        return f"{v:.0f}"
    return f"{v:.1f}" if a >= 10 else f"{v:.2f}"


def _values(vals: list[dict] | None, data: list[int] | None) -> dict:
    out = {}
    for v in vals or []:
        x = evaluate(v["expr"], data or [])
        if x is not None:
            out[v["name"]] = fmt_value(x) + (f" {v['unit']}" if v.get("unit") else "")
    return out


def name_items(items: list[dict], chips: dict, library: dict[str, dict], switches: set[int]) -> list[dict]:
    """Items -> what an agent reads: chip (board name or type), where it sits, kind, register / command name, bytes,
    readings. library: lower-case chip type name -> its table."""
    def type_of(key: str, addr: int | None) -> dict | None:
        if key.startswith("?:"):  # port not known: the type the port chips at that address share
            types = {c.get("type") for k, c in chips.items() if k[:1].isdigit() and int(k.split(":")[1]) == addr}
            return library.get(next(iter(types)).lower()) if len(types) == 1 and None not in types else None
        t = chips.get(key, {}).get("type")
        return library.get(t.lower()) if t else None

    def name_of(key: str, addr: int | None, body: dict | None) -> str:
        if chips.get(key, {}).get("name"):
            return chips[key]["name"]
        if key.startswith("?:"):
            names = {c.get("name") for k, c in chips.items() if k[:1].isdigit() and int(k.split(":")[1]) == addr}
            if len(names) == 1 and next(iter(names)):
                return next(iter(names))
        if body:
            return body.get("name", "")
        return "SPI" if key == SPI_KEY else f"0x{addr or 0:02X}"

    out = []
    for it in items:
        key, addr = it["key"], it["addr"]
        body = type_of(key, addr)
        m = {"t_s": round(it["t"], 6), "chip": name_of(key, addr, body),
             **({"type": body["name"]} if body and body.get("name") else {}),
             "at": key if key == SPI_KEY else (f"m:0x{addr:02X}" if it["port"] is None else f"port {it['port']}:0x{addr:02X}"),
             "kind": it["kind"]}
        if it.get("nack"):
            m["nack"] = True
        if it["kind"] == "probe":
            m["answered"] = bool(it.get("ok"))
        t = on_bus(body, SPI_KEY if key == SPI_KEY else "i2c")
        if it["kind"] in ("reg", "set"):
            m["reg"] = hx(it["reg"])
            if t and t.get("registers"):
                mask = (0xFF & ~t.get("read_bit", 0x80) & ~(t.get("inc_bit") or 0)) if t.get("bus") == "spi" else 0xFFFF
                r = next((x for x in t["registers"] if x["addr"] & mask == (it["reg"] or [None])[0]), None)
                if r:
                    m["register"] = r["name"]
                    if it["kind"] == "reg":
                        m["values"] = _values(r.get("values"), it.get("r"))
        elif it["kind"] in ("cmd", "write") and t and t.get("commands"):
            c = next((x for x in t["commands"] if x["code"] == (it.get("w") or [None])[0]), None)
            if c:
                m["command"] = c["name"]
                if it["kind"] == "cmd":
                    m["values"] = _values(c.get("values"), it.get("r"))
        for k, label in (("w", "wrote"), ("r", "read"), ("mosi", "mosi"), ("miso", "miso")):
            if it.get(k):
                m[label] = hx(it[k])
        if it.get("wait_ms") is not None:
            m["wait_ms"] = it["wait_ms"]
        if not m.get("values"):
            m.pop("values", None)
        out.append(m)
    return out


def _sig(m: dict) -> tuple:
    return (m["at"], m["kind"], m.get("reg"), m.get("register"), m.get("command"), m.get("wrote"), m.get("answered"))


def fold_repeats(msgs: list[dict], least: int = 3) -> list[dict]:
    """Runs of the same message (same chip, register or command, same bytes written) in a row -> one entry with
    times, every_ms and the last one's reading (a status poll: how often, and what it read last)."""
    out, i = [], 0
    while i < len(msgs):
        j = i + 1
        while j < len(msgs) and _sig(msgs[j]) == _sig(msgs[i]):
            j += 1
        if j - i >= least:
            run = msgs[i:j]
            gaps = [b["t_s"] - a["t_s"] for a, b in zip(run, run[1:])]
            last = {k: run[-1][k] for k in ("read", "values") if k in run[-1]}
            out.append({**msgs[i], "times": j - i, "every_ms": round(statistics.median(gaps) * 1000, 2),
                        "until_s": run[-1]["t_s"], **({"last": last} if last else {})})
        else:
            out.extend(msgs[i:j])
        i = j
    return out


def name_session_bus(events: list[dict], chips: dict, library: dict[str, dict], fold: bool = True) -> dict:
    """Everything for session_bus: the named messages (repeats folded) and per chip how many."""
    switch_types = {n for n, b in library.items() if (on_bus(b, "i2c") or {}).get("style") == "switch"}
    switches = switches_of(events, chips, switch_types)
    spi_type = chips.get(SPI_KEY, {}).get("type")
    spi = on_bus(library.get(spi_type.lower()), "spi") if spi_type else None
    msgs = name_items(to_items(events, switches, spi), chips, library, switches)
    per_chip: dict[str, dict] = {}
    for m in msgs:
        c = per_chip.setdefault(m["at"], {"chip": m["chip"], **({"type": m["type"]} if "type" in m else {}), "messages": 0})
        c["messages"] += 1
    return {"switches": [f"0x{a:02X}" for a in sorted(switches)], "chips": per_chip,
            "messages": fold_repeats(msgs) if fold else msgs}
