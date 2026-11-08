from api.resets import classify, find_resets, group, summary

SLEEP = "rst:0x5 (SLEEP_WAKEUP),boot:0x7e (SPI_FAST_FLASH_BOOT)"
POWERON = "rst:0x1 (POWERON),boot:0x7e (SPI_FAST_FLASH_BOOT)"


def test_classify():
    assert classify(SLEEP) is None
    assert classify("[I] nothing to see") is None
    assert classify(POWERON)[0] == "power_on"
    assert classify("rst:0xf (BROWN_OUT_RST),boot:0x7e")[0] == "brownout"
    assert classify("rst:0x7 (TG0WDT_HPSYS),boot:0x7e")[0] == "watchdog"
    assert classify("rst:0xc (SW_CPU),boot:0x7e")[:2] == ("software", "0xc")
    assert classify("rst:0x15 (USB_UART_HPSYS),boot:0x7e") == ("other", "0x15", "USB_UART_HPSYS")


def test_tracker_power_on_is_not_flagged():
    lines = [(0.05, POWERON), (60.0, SLEEP), (893.85, POWERON), (2000.0, "[I] Going to deep sleep"), (2001.0, POWERON)]
    # the tracker switched the DUT on at 0 and 893.83 (session start, restore after a gap); 2001 is a supply drop
    rs = find_resets(lines, [0.0, 893.83])
    assert [(r["t"], r["kind"], r["label"]) for r in rs] == [(2001.0, "power_lost", "power lost")]
    assert rs[0]["explanation"] == "[I] Going to deep sleep"


def test_crash_and_explanation():
    lines = [
        (10.0, "[I] Polling"), (11.0, "Guru Meditation Error: Core  0 panic'ed (Load access fault)"),
        (11.1, "Backtrace: 0x4200 0x4201"), (11.5, "rst:0xc (SW_CPU),boot:0x7e"),
        (30.0, "[I] FW Update: Verified and applied successfully. Rebooting..."), (30.4, "rst:0xc (SW_CPU),boot:0x7e"),
        (50.0, "[I] old line"), (75.0, "rst:0x7 (TG1WDT_HPSYS),boot:0x7e"),  # nothing in the 10 s before
    ]
    rs = find_resets(lines, [])
    assert [r["kind"] for r in rs] == ["crash", "software", "watchdog"]
    assert rs[0]["panic"].startswith("Guru Meditation") and rs[0]["explanation"].startswith("Backtrace")
    assert rs[1]["explanation"].endswith("Rebooting...") and rs[1]["panic"] is None
    assert rs[2]["explanation"] is None


def test_group_by_kind_and_cause():
    rs = [{"t": t, "kind": k, "label": k, "chip": "X", "explanation": e, "panic": None} for t, k, e in [
        (1, "watchdog", "[I] tick 12"), (5, "watchdog", "[I] other"), (9, "brownout", None),
        (12, "software", "FW Update: Rebooting..."), (20, "software", "Factory reset 404"), (30, "software", "FW Update: Rebooting...")]]
    g = group(rs)
    # watchdogs together whatever came before; software resets by their reason line
    assert [(x["kind"], x["count"], x["first_t"]) for x in g] == [
        ("watchdog", 2, 1), ("software", 2, 12), ("brownout", 1, 9), ("software", 1, 20)]
    assert g[0]["times"] == [1, 5] and g[1]["times"] == [12, 30]
    assert summary(rs)["count"] == 6


def test_explanation_stops_at_the_previous_reset():
    lines = [(1.0, "Guru Meditation Error: Core 0 panic'ed"), (1.5, "rst:0xc (SW_CPU),boot:0x7e"),
             (5.0, "FW Update: Verified and applied successfully. Rebooting..."), (5.3, "rst:0xc (SW_CPU),boot:0x7e"),
             (8.0, "rst:0x5 (SLEEP_WAKEUP),boot:0x7e"), (9.0, "rst:0x7 (TG1WDT_HPSYS),boot:0x7e")]
    rs = find_resets(lines, [])
    assert [r["kind"] for r in rs] == ["crash", "software", "watchdog"]  # the panic belongs to the first only
    assert rs[2]["explanation"] is None  # nothing since the deep-sleep wake before it


def test_resets_during_a_flash_are_expected():
    lines = [(10.0, "rst:0x15 (USB_UART_HPSYS),boot:0x0 (DOWNLOAD(USB/UART0))"),
             (14.0, "rst:0x15 (USB_UART_HPSYS),boot:0x7e (SPI_FAST_FLASH_BOOT)"),
             (30.0, "rst:0xc (SW_CPU),boot:0x7e (SPI_FAST_FLASH_BOOT)")]
    rs = find_resets(lines, [], [(9.5, 12.0)])
    assert [r["t"] for r in rs] == [30.0]  # the boot 2 s after the flash is the new firmware
