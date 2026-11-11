"""Unexpected resets of the board, from its serial log.

The ESP32 ROM prints "rst:0x5 (SLEEP_WAKEUP),boot:0x7e (...)" on every reset. Deep-sleep wakes are normal, and so is
a power-on right after the tracker switched the board on, and any reset during or just after a firmware flash by the
tracker (a 'flash' event); everything else is flagged: brownout, watchdog, software
reset (a crash when panic text comes just before it), or a power-on the tracker didn't cause (the supply dropped).
Each reset gets the last log line before it (since the previous reset) as its explanation: firmware logs a
"Rebooting..." line before a restart on purpose, e.g. after an update. Grouping: software resets by that line,
crashes by their panic text, everything else (brownout, watchdog, power lost) by kind alone.
"""

from __future__ import annotations

import bisect
import re

RST = re.compile(r"rst:(0x[0-9a-fA-F]+)\s*\(([^)]*)\)")
PANIC = re.compile(r"Guru Meditation|abort\(\) was called|Backtrace:|panic", re.IGNORECASE)
POWER_ON_GRACE_S = 2.0  # a power-on this soon after the tracker switched the board on is the tracker's own
FLASH_GRACE_S = 5.0  # after a flash ends: the board boots the new firmware
LOOK_BACK_S = 10.0  # explanation and panic text: this far before the reset

LABELS = {"brownout": "brownout", "watchdog": "watchdog", "software": "software reset", "crash": "crash",
          "power_lost": "power lost"}


def classify(text: str) -> tuple[str, str, str] | None:
    """(kind, code, chip reason) for a reset line; None for a deep-sleep wake or a line that isn't a reset.
    kind: brownout, watchdog, software, power_on, or other (the chip's own name)."""
    m = RST.search(text)
    if not m:
        return None
    code, name = m.group(1), m.group(2).strip()
    up = name.upper()
    if "SLEEP" in up:
        return None
    if "BROWN" in up or "BOD" in up:
        kind = "brownout"
    elif "WDT" in up or "WATCHDOG" in up:
        kind = "watchdog"
    elif "POWERON" in up or "POWER_ON" in up:
        kind = "power_on"
    elif "SW" in up or "SOFT" in up:
        kind = "software"
    else:
        kind = "other"
    return kind, code, name


def find_resets(lines: list[tuple[float, str]], tracker_on: list[float],
                flashes: list[tuple[float, float]] = ()) -> list[dict]:
    """lines: (session seconds, text) sorted by time; tracker_on: times the tracker switched the board on;
    flashes: (start, end) of firmware flashes by the tracker.
    Returns the unexpected resets in time order: {t, kind, label, code, chip, explanation, panic}."""
    on = sorted(tracker_on)
    times = [t for t, _ in lines]
    out = []
    for i, (t, text) in enumerate(lines):
        c = classify(text)
        if c is None:
            continue
        if any(a - 0.05 <= t <= b + FLASH_GRACE_S for a, b in flashes):
            continue  # the tracker flashed it: esptool's resets and the power-cycle before
        kind, code, chip = c
        if kind == "power_on":
            k = bisect.bisect_right(on, t + 0.05)  # the event can be stamped just after the line
            if (k and t - on[k - 1] <= POWER_ON_GRACE_S) or t <= POWER_ON_GRACE_S:
                continue  # the tracker switched it on (or the session just started)
            kind = "power_lost"
        lo = bisect.bisect_left(times, t - LOOK_BACK_S)
        # Only since the previous reset: what came before that belongs to the earlier one.
        for j in range(i - 1, lo - 1, -1):
            if RST.search(lines[j][1]):
                lo = j + 1
                break
        before = [x for _, x in lines[lo:i] if x.strip()]
        panic = next((x.strip() for x in before if PANIC.search(x)), None)
        if kind == "software" and panic:
            kind = "crash"
        out.append({"t": t, "kind": kind, "label": LABELS.get(kind, chip.lower() or "reset"), "code": code, "chip": chip,
                    "explanation": before[-1].strip() if before else None, "panic": panic})
    return out


def _key(text: str | None) -> str:
    return re.sub(r"\d+", "#", text or "")


def group_key(r: dict) -> tuple[str, str]:
    """Software resets by the line before them (why it restarted), crashes by their panic text (numbers ignored);
    the rest by kind alone: the line before a brownout or watchdog reset is just whatever it was doing."""
    if r["kind"] == "software":
        return r["kind"], _key(r["explanation"])
    if r["kind"] == "crash":
        return r["kind"], _key(r["panic"])
    return r["kind"], ""


def group(resets: list[dict]) -> list[dict]:
    """Resets of the same kind and cause together, most frequent first."""
    groups: dict[tuple[str, str], dict] = {}
    for r in resets:
        g = groups.setdefault(group_key(r), {
            "kind": r["kind"], "label": r["label"], "chip": r["chip"], "panic": r["panic"],
            # The reason line, for software resets (for the others it's just whatever came before the first one).
            "explanation": r["explanation"] if r["kind"] == "software" else None,
            "count": 0, "first_t": r["t"], "times": []})
        g["count"] += 1
        g["times"].append(r["t"])
    return sorted(groups.values(), key=lambda g: (-g["count"], g["first_t"]))


def summary(resets: list[dict]) -> dict:
    return {"count": len(resets), "groups": group(resets)}
