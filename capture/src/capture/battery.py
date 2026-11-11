"""Battery emulation: the PPK2 output follows a discharging battery during a session.

Chemistries are approximate open-circuit curves per cell (state of charge % -> mV), times the number of cells.
They are rest voltages: the sag under load (current x internal resistance) is not emulated; the PPK2 can't
follow millisecond peaks over USB. A small series resistor between PPK2 and board gives a real sag.

Two ways to drain:
- "charge": state of charge = start - measured charge / emulated capacity. The emulated capacity is the
  real capacity divided by `speedup`, so a 1000 mAh battery at 50x empties after 20 mAh.
- "time": a straight sweep from `start_mv` down to the cutoff over `sweep_min` minutes.

At the cutoff voltage:
- "protect": like a battery protection circuit: the board is switched off and the session stops.
- "hunt": brown-out hunting: the voltage keeps going down (same slope as just before the cutoff) to
  `floor_mv`, then the board is switched off and the session stops.
"""

from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass

# Per-cell curves: (state of charge %, mV), from empty to full. cutoff/floor: per-cell defaults for the form.
CHEMISTRIES: dict[str, dict] = {
    "lipo": {
        "label": "LiPo / Li-ion", "note": "sloped: the voltage tells how full it is",
        "cells": [1], "cutoff_mv": 3000, "floor_mv": 2500,
        "curve": [(0, 3000), (2, 3450), (5, 3610), (10, 3690), (15, 3710), (20, 3730), (25, 3750), (30, 3770),
                  (35, 3790), (40, 3800), (45, 3820), (50, 3840), (55, 3850), (60, 3870), (65, 3910), (70, 3950),
                  (75, 3980), (80, 4020), (85, 4080), (90, 4110), (95, 4150), (100, 4200)],
    },
    "lipo_hv": {
        "label": "LiPo HV", "note": "charged higher, otherwise like LiPo",
        "cells": [1], "cutoff_mv": 3000, "floor_mv": 2500,
        "curve": [(0, 3000), (2, 3450), (5, 3610), (10, 3690), (15, 3720), (20, 3740), (25, 3760), (30, 3780),
                  (35, 3800), (40, 3820), (45, 3840), (50, 3860), (55, 3890), (60, 3920), (65, 3960), (70, 4010),
                  (75, 4060), (80, 4110), (85, 4160), (90, 4220), (95, 4280), (100, 4350)],
    },
    "lifepo4": {
        "label": "LiFePO4", "note": "very flat around 3.2-3.3 V, then a sudden drop",
        "cells": [1], "cutoff_mv": 2500, "floor_mv": 2000,
        "curve": [(0, 2500), (2, 2800), (5, 3000), (10, 3150), (15, 3200), (20, 3220), (30, 3250), (40, 3270),
                  (50, 3280), (60, 3290), (70, 3300), (80, 3320), (90, 3350), (99, 3450), (100, 3600)],
    },
    "lisocl2": {
        "label": "Li-SOCl2 (e.g. ER14505)", "note": "primary cell, extremely flat at 3.6 V, then falls off a cliff",
        "cells": [1], "cutoff_mv": 3000, "floor_mv": 2500,
        "curve": [(0, 3000), (2, 3200), (5, 3400), (10, 3500), (20, 3550), (40, 3580), (60, 3600), (80, 3620),
                  (95, 3640), (100, 3670)],
    },
    "alkaline": {
        "label": "Alkaline AA", "note": "1.5-1.6 V per cell fresh, steady slope; 0.9 V per cell is empty",
        "cells": [2, 3, 4], "cutoff_mv": 900, "floor_mv": 800,
        "curve": [(0, 900), (5, 1050), (10, 1100), (20, 1150), (30, 1190), (40, 1230), (50, 1270), (60, 1320),
                  (70, 1370), (80, 1420), (90, 1480), (100, 1580)],
    },
    "nimh": {
        "label": "NiMH AA (Eneloop, IKEA LADDA)", "note": "flat around 1.2-1.25 V per cell, sharp drop at the end",
        "cells": [2, 3, 4], "cutoff_mv": 1000, "floor_mv": 900,
        "curve": [(0, 1000), (2, 1080), (5, 1130), (10, 1180), (20, 1210), (30, 1230), (40, 1240), (50, 1250),
                  (60, 1260), (70, 1270), (80, 1290), (90, 1310), (95, 1350), (100, 1400)],
    },
}

MAH = 3.6  # coulomb per mAh


@dataclass
class BatteryConfig:
    mode: str = "charge"  # charge | time
    chemistry: str = "lipo"  # a key of CHEMISTRIES
    cells: int = 1  # in series
    capacity_mah: float = 1000.0  # charge mode: the real battery
    speedup: float = 1.0  # charge mode: emulated capacity = capacity / speedup
    start_soc: float = 100.0  # charge mode: start at this state of charge (%)
    start_mv: int = 4200  # time mode: sweep start
    sweep_min: float = 120.0  # time mode: minutes from start_mv to the cutoff
    cutoff_mv: int = 3000
    on_cutoff: str = "protect"  # protect | hunt
    floor_mv: int = 2500  # hunt: lowest voltage

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> BatteryConfig | None:
        if not d:
            return None
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def check(self, max_mv: int) -> None:
        if self.mode not in ("charge", "time"):
            raise ValueError("battery mode must be 'charge' or 'time'")
        if self.chemistry not in CHEMISTRIES:
            raise ValueError(f"unknown chemistry {self.chemistry!r}")
        if self.cells not in CHEMISTRIES[self.chemistry]["cells"]:
            raise ValueError(f"{CHEMISTRIES[self.chemistry]['label']}: {CHEMISTRIES[self.chemistry]['cells']} cells")
        if self.on_cutoff not in ("protect", "hunt"):
            raise ValueError("on_cutoff must be 'protect' or 'hunt'")
        if self.capacity_mah <= 0 or self.speedup <= 0 or self.sweep_min <= 0:
            raise ValueError("capacity, speed-up and sweep time must be above 0")
        if not 0 < self.start_soc <= 100:
            raise ValueError("start charge must be 1-100 %")
        if not 800 <= self.floor_mv <= self.cutoff_mv:
            raise ValueError("floor must be between 0.80 V and the cutoff")
        start = soc_to_mv(self.start_soc, self.chemistry, self.cells) if self.mode == "charge" else self.start_mv
        if start > max_mv:
            raise ValueError(f"start voltage {start:.0f} mV is above the limit ({max_mv} mV, MAX_VOLTAGE_MV)"
                             + (": start at a lower charge" if self.mode == "charge" else ""))
        if start <= self.cutoff_mv:
            raise ValueError("start voltage must be above the cutoff")


def soc_to_mv(soc: float, chemistry: str = "lipo", cells: int = 1) -> float:
    """Pack voltage at a state of charge. Below 0 % (hunt mode) it keeps falling with the slope of the last
    stretch of the curve."""
    curve = CHEMISTRIES[chemistry]["curve"]
    socs = [p[0] for p in curve]
    mvs = [p[1] for p in curve]
    if soc <= 0:
        v = mvs[0] + soc * (mvs[1] - mvs[0]) / (socs[1] - socs[0])
    elif soc >= 100:
        v = mvs[-1]
    else:
        i = bisect.bisect_right(socs, soc)
        s0, s1, v0, v1 = socs[i - 1], socs[i], mvs[i - 1], mvs[i]
        v = v0 + (v1 - v0) * (soc - s0) / (s1 - s0)
    return v * cells


def chemistries() -> list[dict]:
    """For the record form: label, cell counts, default cutoff/floor and the curve (per cell)."""
    return [{"id": k, **{key: v for key, v in c.items()}} for k, c in CHEMISTRIES.items()]


@dataclass
class BatteryState:
    voltage_mv: int  # what the PPK2 should output now
    soc: float | None  # charge mode: state of charge (%), negative below empty
    done: bool  # reached the end (cutoff in protect mode, floor in hunt mode)
    below_cutoff: bool
    eta_s: float | None  # seconds until the cutoff at the current rate (None = unknown or past it)


def state(cfg: BatteryConfig, charge_c: float, elapsed_s: float) -> BatteryState:
    """Target voltage after drawing `charge_c` coulomb over `elapsed_s` seconds of session."""
    if cfg.mode == "charge":
        emulated_c = cfg.capacity_mah * MAH / cfg.speedup
        soc = cfg.start_soc - 100.0 * charge_c / emulated_c
        mv = soc_to_mv(soc, cfg.chemistry, cfg.cells)
        rate = charge_c / elapsed_s if elapsed_s > 0 else 0.0  # average current (A)
        eta = (soc / 100.0 * emulated_c) / rate if rate > 0 and soc > 0 else None
    else:
        soc = None
        slope = (cfg.start_mv - cfg.cutoff_mv) / (cfg.sweep_min * 60)  # mV per s
        mv = cfg.start_mv - slope * elapsed_s
        eta = (mv - cfg.cutoff_mv) / slope if mv > cfg.cutoff_mv else None
    below = mv <= cfg.cutoff_mv
    end = cfg.cutoff_mv if cfg.on_cutoff == "protect" else cfg.floor_mv
    done = mv <= end
    return BatteryState(voltage_mv=int(round(max(mv, end))), soc=soc, done=done, below_cutoff=below, eta_s=eta)
