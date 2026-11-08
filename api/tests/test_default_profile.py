"""The Default profile (migration 008) against the log lines docs/firmware-logging.md tells firmware to print."""

import json
import re
from importlib import resources

from api.phases import Cycle, compile_wake, find_cycles, type_instances

SQL = resources.files("api.migrations").joinpath("008_default_profile.sql").read_text()
RULES, TYPES = (json.loads(x) for x in re.search(r"VALUES \('Default', '(\[.*?\])', '(\[.*?\])'\)", SQL, re.S).groups())
NAMES = [r["name"] for r in RULES]

# The example from docs/firmware-logging.md, then a Zephyr board and some lines that must not match.
LINES = [
    (0.0, "rst:0x5 (SLEEP_WAKEUP),boot:0x7e (SPI_FAST_FLASH_BOOT)"),
    (0.3, "[I][main.cpp:21] Wake source: Timer"),
    (0.31, "[I][main.cpp:30] Measuring"),
    (0.5, "[I][main.cpp:52] Going to deep sleep for 60 s"),
    (60.0, "rst:0x5 (SLEEP_WAKEUP),boot:0x7e (SPI_FAST_FLASH_BOOT)"),
    (60.3, "[I][main.cpp:21] Wake source: Timer"),
    (60.31, "[I][main.cpp:30] Measuring"),
    (60.5, "[I][net.cpp:12] Connecting to WiFi"),
    (62.0, "[I][net.cpp:40] Uploading 5 readings"),
    (63.0, "[I][main.cpp:52] Going to deep sleep for 60 s"),
    (120.0, "*** Booting Zephyr OS build v3.7.0 ***"),
    (120.1, "<inf> app: Wake source: Cold boot"),  # "boot" alone doesn't start a cycle
    (120.2, "<inf> app: measuring 3 sensors"),
    (120.4, "<inf> app: Rebooting: firmware update"),  # nor does "Rebooting"
    (125.0, "I (1234) app: Booting"),
    (125.1, "I (1235) app: Connecting to LTE"),
    (127.0, "I (3235) app: publishing to MQTT"),
    (128.0, "I (4235) app: Entering light sleep"),
    (190.0, "rst:0x5 (SLEEP_WAKEUP),boot:0x7e (SPI_FAST_FLASH_BOOT)"),
]


def phase_names(c: Cycle) -> list[str]:
    return [NAMES[p.rule] for p in c.phases]


def test_default_profile_phases():
    cs = find_cycles(LINES, RULES, 200.0, compile_wake(None))
    assert [c.t for c in cs] == [0.0, 60.0, 120.0, 125.0, 190.0]
    assert phase_names(cs[0]) == ["Boot", "Measure", "Sleep"]
    assert phase_names(cs[1]) == ["Boot", "Measure", "Connect", "Upload", "Sleep"]
    assert phase_names(cs[2]) == ["Boot", "Measure"]
    assert phase_names(cs[3]) == ["Boot", "Connect", "Upload", "Sleep"]
    assert [c.wake for c in cs] == ["Timer", "Timer", "Cold boot", None, None]
    assert [p.t for p in cs[1].phases] == [60.0, 60.31, 60.5, 62.0, 63.0]


def test_default_profile_types():
    cs = find_cycles(LINES, RULES, 200.0, compile_wake(None))
    for c in cs:
        for p in c.phases:
            p.coverage, p.charge_c = 1.0, (p.end_t - p.t) * 0.01
    by_name = {t["name"]: t for t in TYPES}
    assert set(by_name) == {"Measure only", "With upload", "Upload to upload", "Connect + upload"}
    for t in TYPES:
        assert type_instances(cs, RULES, t)["error"] is None, t["name"]
    measure_only = type_instances(cs, RULES, by_name["Measure only"])["instances"]
    with_upload = type_instances(cs, RULES, by_name["With upload"])["instances"]
    assert len(measure_only) == 2 and len(with_upload) == 2  # 0 s and 120 s; 60 s and 125 s
