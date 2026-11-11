from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    ppk2_port: str = _env("PPK2_PORT", "/dev/ppk2")  # "sim://" for the simulator
    uart_port: str = _env("UART_PORT", "/dev/dut-uart")  # "sim://" for the simulator
    usb_port: str = _env("USB_PORT", "/dev/dut-usb")  # the board's own USB serial; "sim://" for the simulator
    ring_seconds: int = int(_env("RING_SECONDS", "300"))
    database_url: str = _env("DATABASE_URL", "")  # empty = no sessions (live only)
    data_dir: str = _env("DATA_DIR", "/data")
    # Defaults of settings changed in the web UI (Settings → Measurement & safety, Storage), not in .env: the api
    # keeps them in the database and capture loads them when it starts.
    # The tracker's voltage limit: nothing goes above it, whatever a device says. 4.2 V = a 1-cell LiPo (the XIAO's
    # battery input); the PPK2 itself goes to 5 V.
    max_voltage_mv: int = 4200
    min_free_gb: float = 10.0  # stop writing raw data below this much free disk
    auto_stream: bool = True  # start measuring when the PPK2 connects
    idle_power_off_s: int = 600  # switch the board off when nothing records and no browser is open this long (0 = never)
    raw_mantissa_bits: int = 12  # raw file precision; 23 = lossless
    http_port: int = int(_env("HTTP_PORT", "8100"))
    log_level: str = _env("LOG_LEVEL", "INFO")
