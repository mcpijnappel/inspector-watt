import time

from capture.sim import SimUsbDevice
from capture.usb import MenuParser, UsbManager

MENU = ["--- DEBUG MENU ---", "  [n]  Continue normally", "  [r]  Reboot", "------------------", "Waiting for input..."]


def test_menu_parser():
    m = MenuParser()
    for line in ["=== BOOT DIAGNOSTICS ===", "Firmware  : 0.1.0", *MENU]:
        m.feed(line, 1.0)
    assert m.menu["title"] == "DEBUG MENU"
    assert m.menu["options"] == [{"key": "n", "label": "Continue normally"}, {"key": "r", "label": "Reboot"}]
    assert m.menu["prompt"] == "Waiting for input..."
    m.clear()
    assert m.menu is None


def test_menu_parser_ignores_dash_lines_and_titles_without_options():
    m = MenuParser()
    for line in ["------------------", "--- STATUS ---", "all good", "------"]:
        m.feed(line, 1.0)
    assert m.menu is None


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out"
        time.sleep(0.02)


def test_console_opens_on_demand_and_answers_the_menu():
    dev = SimUsbDevice()
    usb = UsbManager("sim://", dev)
    usb.start()
    try:
        time.sleep(0.2)
        assert not usb.connected  # closed until someone asks: the firmware sees no USB
        usb.set_wanted(True)
        wait_for(lambda: usb.status()["menu"] is not None)
        assert [o["key"] for o in usb.status()["menu"]["options"]] == ["n", "r", "c", "f"]
        usb.send("n", "")
        assert usb.status()["menu"] is None
        wait_for(lambda: any("Continuing normally" in ln.text for ln in usb.log.after(-1)))
        usb.set_wanted(False)
        wait_for(lambda: not usb.connected)
    finally:
        usb.stop()


def test_console_waits_for_the_port_while_asleep():
    dev = SimUsbDevice()
    dev.present = False
    usb = UsbManager("sim://", dev)
    usb.start()
    try:
        usb.set_wanted(True)
        time.sleep(0.3)
        assert not usb.connected and usb.error is None
        dev.wake()
        wait_for(lambda: usb.connected)
    finally:
        usb.stop()
