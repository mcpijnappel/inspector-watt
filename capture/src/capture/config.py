from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    ppk2_port: str = _env("PPK2_PORT", "/dev/ppk2")  # "sim://" for the simulator
    uart_port: str = _env("UART_PORT", "/dev/dut-uart")  # "sim://" for the simulator
    uart_baud: int = int(_env("UART_BAUD", "115200"))
    usb_port: str = _env("USB_PORT", "/dev/dut-usb")  # the DUT's own USB serial; "sim://" for the simulator
    ring_seconds: int = int(_env("RING_SECONDS", "300"))
    # Upper limit for the source voltage; protects the sensor against typos. PPK2 hardware max is 5000.
    max_voltage_mv: int = int(_env("MAX_VOLTAGE_MV", "5000"))
    database_url: str = _env("DATABASE_URL", "")  # empty = no sessions (live only)
    data_dir: str = _env("DATA_DIR", "/data")
    min_free_gb: float = float(_env("MIN_FREE_GB", "10"))  # stop writing raw data below this much free disk
    auto_stream: bool = _env("AUTO_STREAM", "1") not in ("0", "false", "no")  # start measuring when the PPK2 connects
    # Switch the DUT off when nothing records and no browser has the app open for this long (0 = never).
    idle_power_off_s: int = int(_env("IDLE_POWER_OFF_S", "600"))
    raw_mantissa_bits: int = int(_env("RAW_MANTISSA_BITS", "12"))  # 23 = lossless raw files
    http_port: int = int(_env("HTTP_PORT", "8100"))
    log_level: str = _env("LOG_LEVEL", "INFO")
