"""sigrok-cli: find the logic analyzer. (Capturing and decoding come with the next step.)"""

from __future__ import annotations

import re
import subprocess

SCAN_TIMEOUT_S = 15.0  # fx2lafw uploads its firmware on the first scan after plugging in: a few seconds
DEVICE_LINE = re.compile(r"^(?P<driver>[a-z0-9_-]+)(?::(?:conn=)?(?P<conn>\S+))? - (?P<desc>.+?) with (?P<n>\d+) channels?: (?P<channels>.*)$")


class SigrokError(RuntimeError):
    pass


def parse_scan(text: str) -> list[dict]:
    """sigrok-cli --scan output -> [{driver, conn, description, channels: [logic channel names]}]."""
    out = []
    for line in text.splitlines():
        m = DEVICE_LINE.match(line.strip())
        if not m:
            continue
        channels = [c for c in m["channels"].split() if c.startswith("D")]  # logic only (the demo also has A0-A4)
        out.append({"driver": m["driver"], "conn": m["conn"], "description": m["desc"], "channels": channels})
    return out


def scan(driver: str, run=subprocess.run) -> list[dict]:
    """Analyzers sigrok finds with this driver (fx2lafw: every FX2-based one plugged in)."""
    try:
        p = run(["sigrok-cli", "-d", driver, "--scan"], capture_output=True, text=True, timeout=SCAN_TIMEOUT_S)
    except FileNotFoundError as e:
        raise SigrokError("sigrok-cli isn't installed in this container") from e
    except subprocess.TimeoutExpired as e:
        raise SigrokError("sigrok-cli didn't answer (the analyzer may be busy or stuck: replug it)") from e
    devices = parse_scan(p.stdout)
    if not devices and p.returncode != 0:
        raise SigrokError((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout).strip() else "scan failed")
    return devices
