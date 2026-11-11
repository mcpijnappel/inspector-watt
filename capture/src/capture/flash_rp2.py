"""RP2040 / RP2350 backend for flash.py: picotool over the board's own USB.

The upload is one UF2 file (what pico-sdk, arduino-pico and PlatformIO build). The board normally runs firmware with
a USB serial port (pico-sdk stdio_usb, arduino-pico Serial): a "1200-baud touch" (open that port at 1200 baud, then
close it) restarts it into the boot ROM's USB boot loader (BOOTSEL mode). A board already in BOOTSEL mode (blank,
or BOOTSEL held while plugging it in) is flashed straight away. picotool then writes the UF2 (--update: sectors that
already hold the same data aren't written), checks it (--verify) and starts it (--execute).

Firmware that switches its USB off can't be reached this way: hold BOOTSEL while plugging it in (later: SWD).
Uploads default to power "usb": the board runs on USB power, so the PPK2 and a running recording aren't touched.
"""

from __future__ import annotations

import glob
import os
import re
import struct
import subprocess
import tempfile
import threading
import time

import serial

from .flash import FlashError, FlashJob, FlashRequest

PICOTOOL = os.environ.get("PICOTOOL", "picotool")
PICOTOOL_TIMEOUT_S = 180.0
BOOTSEL_WAIT_S = 6.0  # after the touch: the boot loader enumerates within ~1 s
BOOT_WAIT_S = 6.0  # after the load: the new firmware's USB serial port (if it has one)
SYS_USB = "/sys/bus/usb/devices"
RP_VENDOR = "2e8a"  # Raspberry Pi
BOOTSEL_PRODUCTS = {"0003": "rp2040", "000f": "rp2350"}

# UF2: 512-byte blocks; the family ID says which chip a block is for (flag 0x2000).
UF2_MAGIC = (0x0A324655, 0x9E5D5157)
UF2_END = 0x0AB16F30
UF2_FAMILY_FLAG = 0x2000
UF2_NOT_MAIN_FLASH = 0x0001
FAMILIES = {0xE48BFF56: "rp2040", 0xE48BFF59: "rp2350", 0xE48BFF5A: "rp2350", 0xE48BFF5B: "rp2350"}
SHARED_FAMILIES = (0xE48BFF57, 0xE48BFF58)  # RP2350 "absolute" and "data": no chip of their own
CHIPS = ("rp2040", "rp2350")
PERCENT = re.compile(r"(\d{1,3})\s?%")


def uf2_info(data: bytes) -> dict:
    """Check a UF2 file and say what's in it: the chip, blocks, bytes and the address range."""
    if len(data) < 512 or len(data) % 512:
        raise FlashError("not a UF2 file (its size isn't a whole number of 512-byte blocks)")
    chips, start, end, payload, n = set(), None, 0, 0, len(data) // 512
    for i in range(n):
        b = data[i * 512:(i + 1) * 512]
        m0, m1, flags, addr, size, _no, _total, family = struct.unpack_from("<8I", b)
        if (m0, m1) != UF2_MAGIC or struct.unpack_from("<I", b, 508)[0] != UF2_END:
            raise FlashError(f"not a UF2 file (block {i} has no UF2 magic)")
        if size > 476:
            raise FlashError(f"UF2 block {i} says it carries {size} bytes (at most 476)")
        if flags & UF2_NOT_MAIN_FLASH:
            continue
        if flags & UF2_FAMILY_FLAG:
            if family in FAMILIES:
                chips.add(FAMILIES[family])
            elif family not in SHARED_FAMILIES:
                raise FlashError(f"UF2 for another chip family ({family:#010x}): not an RP2040 or RP2350")
        start = addr if start is None else min(start, addr)
        end = max(end, addr + size)
        payload += size
    if len(chips) > 1:
        raise FlashError("UF2 file for both RP2040 and RP2350")
    if not payload:
        raise FlashError("UF2 file with nothing to write")
    return {"chip": next(iter(chips), ""), "blocks": n, "bytes": payload, "start": start, "end": end}


def find_bootsel(root: str = SYS_USB) -> list[dict]:
    """RP boards in BOOTSEL mode on USB (from sysfs): their chip, bus and address (for picotool)."""
    out = []
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        try:
            with open(os.path.join(d, "idVendor")) as f:
                vendor = f.read().strip()
            with open(os.path.join(d, "idProduct")) as f:
                product = f.read().strip()
            if vendor != RP_VENDOR or product not in BOOTSEL_PRODUCTS:
                continue
            with open(os.path.join(d, "busnum")) as f:
                bus = int(f.read())
            with open(os.path.join(d, "devnum")) as f:
                address = int(f.read())
        except (OSError, ValueError):
            continue
        out.append({"chip": BOOTSEL_PRODUCTS[product], "bus": bus, "address": address})
    return out


def touch_1200(port: str) -> None:
    """Open the port at 1200 baud with DTR off, then close it: the firmware restarts into its boot loader."""
    try:
        s = serial.Serial(port, 1200, timeout=0.1)
        s.dtr = False
        time.sleep(0.1)
        s.close()
    except (serial.SerialException, OSError):
        pass  # it may vanish while we close it: that's the restart


class Rp2Backend:
    family = "rp2"
    default_power = "usb"  # a board on USB power: no PPK2 power-cycle, the recording isn't touched
    missing = ("no RP2040/RP2350 board on USB: plug it in. Firmware without USB serial: hold BOOTSEL while "
               "plugging it in")

    def __init__(self, usb, sys_root: str = SYS_USB):
        self.usb = usb
        self.sys_root = sys_root
        self.sim = usb.port.startswith("sim://")

    def _bootsel(self) -> list[dict]:
        if self.sim:
            sim = self.usb.sim_factory
            return [{"chip": sim.rp_chip, "bus": 1, "address": 9}] if sim.bootsel else []
        return find_bootsel(self.sys_root)

    def present(self) -> bool:
        """The board is there: running (its USB serial port) or in BOOTSEL mode."""
        return self.usb.present() or bool(self._bootsel())

    def check(self, req: FlashRequest) -> None:
        if req.options:
            raise FlashError(f"unknown rp2 option(s): {', '.join(sorted(req.options))}")
        if len(req.images) != 1 or req.images[0].offset is not None:
            raise FlashError("an RP2040/RP2350 upload is one UF2 file, without an offset")
        info = uf2_info(req.images[0].data)
        if req.chip and req.chip not in CHIPS:
            raise FlashError(f"unknown chip {req.chip!r} (rp2040 or rp2350)")
        if req.chip and info["chip"] and req.chip != info["chip"]:
            raise FlashError(f"the UF2 file is for {info['chip']}, not {req.chip}")
        req.chip = req.chip or info["chip"]
        if not req.chip:
            raise FlashError("chip unknown: say which (rp2040 or rp2350)")

    def flash(self, job: FlashJob, port: str) -> bool:
        devs = self._bootsel()
        if not devs:
            if not self.usb.present():
                job.say(self.missing)
                return False
            job.say("Restarting the board into its USB boot loader (1200-baud touch)")
            self._touch(port)
            deadline = time.monotonic() + BOOTSEL_WAIT_S
            while not (devs := self._bootsel()):
                if time.monotonic() > deadline:
                    job.say("The board didn't restart into its boot loader. Its firmware needs USB serial "
                            "(pico-sdk stdio_usb, arduino-pico Serial); else hold BOOTSEL while plugging it in.")
                    return False
                time.sleep(0.1)
        else:
            job.say("Board in BOOTSEL mode")
        if len(devs) > 1:
            job.say(f"{len(devs)} RP boards in BOOTSEL mode: unplug the others")
            return False
        dev = devs[0]
        if dev["chip"] != job.req.chip:
            job.say(f"The board is an {dev['chip'].upper()}, the upload is for {job.req.chip.upper()}")
            return False
        job.progress = 5.0
        ok = self._sim_load(job) if self.sim else self._load(job, dev)
        if ok:
            self._wait_boot(job)
        return ok

    def _touch(self, port: str) -> None:
        if self.sim:
            self.usb.sim_factory.enter_bootsel()
        else:
            touch_1200(port)

    def _load(self, job: FlashJob, dev: dict) -> bool:
        with tempfile.TemporaryDirectory(prefix="flash-") as d:
            path = os.path.join(d, "firmware.uf2")
            with open(path, "wb") as f:
                f.write(job.req.images[0].data)
            args = picotool_args(job.req, path, dev)
            job.say("$ picotool " + " ".join(a if a != path else "firmware.uf2" for a in args[1:]))
            out = PicotoolOutput(job)
            return run_tool(args, out.line) == 0

    def _wait_boot(self, job: FlashJob) -> None:
        deadline = time.monotonic() + BOOT_WAIT_S
        while time.monotonic() < deadline:
            if self.usb.present():
                job.say("The new firmware is running (its USB serial port is back)")
                return
            time.sleep(0.1)
        job.say("The new firmware didn't show a USB serial port (fine if it doesn't use one)")

    def _sim_load(self, job: FlashJob) -> bool:
        """picotool's output for this load, as picotool 2 words it (so the parser is tested too). The simulated
        board remembers what's on it, for --update."""
        sim = self.usb.sim_factory
        data = job.req.images[0].data
        out = PicotoolOutput(job)
        same = not job.req.full_flash and sim.rp_flash == data
        for pct in (0, 25, 50, 75, 100):
            out.line(f"Loading into Flash:   [{'=' * (pct // 10):<10}]  {pct}%")
            time.sleep(0.02 if same else 0.05)
        for pct in (0, 50, 100):
            out.line(f"Verifying Flash:      [{'=' * (pct // 10):<10}]  {pct}%")
        out.line("  OK")
        out.line("")
        out.line("The device was rebooted to start the application.")
        sim.rp_flash = data
        sim.flashed += 1
        sim.leave_bootsel()
        return True


def picotool_args(req: FlashRequest, path: str, dev: dict) -> list[str]:
    args = [PICOTOOL, "load"]
    if not req.full_flash:
        args.append("--update")  # sectors that already hold this data aren't written
    args += ["--verify", "--execute", path, "--bus", str(dev["bus"]), "--address", str(dev["address"])]
    return args


def run_tool(args: list[str], on_line) -> int:
    """Run a tool, each output line (also progress lines ending in \\r) to on_line. Its exit code."""
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "120"}
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    except FileNotFoundError:
        on_line(f"{args[0]} isn't installed in the capture service")
        return 127
    timer = threading.Timer(PICOTOOL_TIMEOUT_S, proc.kill)
    timer.start()
    try:
        buf = b""
        while True:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            buf += chunk
            *parts, buf = re.split(rb"[\r\n]", buf)
            for raw in parts:
                on_line(raw.decode(errors="replace"))
        on_line(buf.decode(errors="replace"))
        return proc.wait()
    finally:
        timer.cancel()


class PicotoolOutput:
    """picotool's output into the job log: loading is 5-90 % of the job, verifying 90-99 %; the log only gets
    every 25 % of a progress bar."""

    STAGES = {"Loading": (5.0, 90.0), "Verifying": (90.0, 99.0)}

    def __init__(self, job: FlashJob):
        self.job = job
        self.stage = ""
        self.last = -1

    def line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        stage = next((s for s in self.STAGES if line.startswith(s)), None)
        m = PERCENT.search(line)
        if stage and m:
            pct = int(m.group(1))
            lo, hi = self.STAGES[stage]
            self.job.progress = max(self.job.progress, min(99.0, lo + (hi - lo) * pct / 100))
            if stage == self.stage and pct // 25 == self.last // 25 and pct != 100:
                return
            self.stage, self.last = stage, pct
            line = re.sub(r":\s*\[[=> ]*\]\s*", ": ", line)  # keep "Loading into Flash: 50%", not the bar
        self.job.say(line)
