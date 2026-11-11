"""ESP32-family backend for flash.py: esptool over the DUT's own USB port (USB-Serial/JTAG).

- Images already on the chip aren't written (--skip-flashed: MD5 against the flash first).
- An upload with only the app image (option app_only, e.g. from the Arduino OTA endpoint) doesn't know where the
  app goes: the partition table is read from the chip first, the app goes into the factory partition (else ota_0)
  and otadata is erased, so the bootloader boots it.
- Version and project name come from the app image itself (esp_app_desc_t) when the upload doesn't say.
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

from .flash import FlashError, FlashJob, FlashRequest, Image

ESPTOOL_TIMEOUT_S = 240.0
ESPTOOL_BAUD = "921600"  # ignored by USB-Serial/JTAG, used by a USB-UART bridge
CHIP = re.compile(r"^esp32[a-z0-9]*$")
FLASH_MODE = ("keep", "qio", "qout", "dio", "dout")
FLASH_FREQ = re.compile(r"^(keep|\d{1,3}m)$")
FLASH_SIZE = re.compile(r"^(keep|detect|\d{1,3}MB)$")
OPTIONS = {"flash_mode": "keep", "flash_freq": "keep", "flash_size": "keep", "app_only": False}
PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
# esptool's output we read (esptool 5): the chip's MAC when it connects, which image it's on (only printed with
# more than one), and an image --skip-flashed found on the chip already.
MAC_LINE = re.compile(r"^MAC:\s+((?:[0-9a-fA-F]{2}:){5,7}[0-9a-fA-F]{2})\s*$")
IMAGE_LINE = re.compile(r"^Writing '.*?(\d+)_0x[0-9a-f]+\.bin' at 0x")
SKIPPED_LINE = re.compile(r" at 0x[0-9a-f]+ already in flash, skipping write\.$")

# ESP image format: the chip an image was built for (esptool's IMAGE_CHIP_ID per target).
IMAGE_MAGIC = 0xE9
CHIP_IDS = {0: "esp32", 2: "esp32s2", 5: "esp32c3", 9: "esp32s3", 12: "esp32c2", 13: "esp32c6", 16: "esp32h2",
            18: "esp32p4", 20: "esp32c61", 23: "esp32c5", 25: "esp32h21", 28: "esp32h4"}
APP_DESC_MAGIC = 0xABCD5432
ARDUINO_FRAMEWORK = ("arduino-lib-builder",)  # esp_app_desc_t project of Arduino-ESP32's precompiled libraries
PARTITION_TABLE_OFFSET = 0x8000
PARTITION_TABLE_SIZE = 0xC00
PART_MAGIC = b"\xaa\x50"
APP, DATA = 0x00, 0x01
FACTORY, OTA_0, OTA_DATA = 0x00, 0x10, 0x00


@dataclass
class Partition:
    type: int
    subtype: int
    offset: int
    size: int
    label: str


def parse_partition_table(data: bytes) -> list[Partition]:
    out = []
    for i in range(0, len(data) - 31, 32):
        e = data[i:i + 32]
        if e[:2] != PART_MAGIC:
            break  # 0xEBEB (MD5 entry) or erased: the end
        t, st, off, size = struct.unpack_from("<BBII", e, 2)
        out.append(Partition(t, st, off, size, e[12:28].split(b"\0")[0].decode(errors="replace")))
    return out


def app_slot(parts: list[Partition]) -> tuple[Partition, Partition | None]:
    """Where an app-only upload goes: the factory partition, else ota_0; plus otadata (to erase), if any."""
    apps = [p for p in parts if p.type == APP]
    target = next((p for p in apps if p.subtype == FACTORY), None) or next((p for p in apps if p.subtype == OTA_0), None)
    if target is None:
        raise FlashError("the partition table on the chip has no factory or ota_0 app partition")
    otadata = next((p for p in parts if p.type == DATA and p.subtype == OTA_DATA), None)
    return target, otadata


def image_chip(data: bytes) -> str | None:
    """The chip an ESP image (bootloader, app, or a merged image starting with the bootloader) was built for."""
    if len(data) < 24 or data[0] != IMAGE_MAGIC:
        return None
    return CHIP_IDS.get(struct.unpack_from("<H", data, 12)[0])


def app_desc(data: bytes) -> dict | None:
    """esp_app_desc_t of an app image: right after the image header (24 bytes) and the first segment header (8)."""
    if len(data) < 32 + 144 or data[0] != IMAGE_MAGIC or struct.unpack_from("<I", data, 32)[0] != APP_DESC_MAGIC:
        return None

    def s(at: int, n: int) -> str:
        return data[32 + at:32 + at + n].split(b"\0")[0].decode(errors="replace")
    return {"version": s(16, 32), "project": s(48, 32), "time": s(80, 16), "date": s(96, 16), "idf": s(112, 32)}


def find_app(images: list[Image]) -> bytes | None:
    """The app image among the uploaded ones: one at a known offset, or inside a merged image (partition table)."""
    for im in images:
        if app_desc(im.data):
            return im.data
        pt = PARTITION_TABLE_OFFSET - (im.offset or 0)
        if 0 <= pt < len(im.data):
            for p in parse_partition_table(im.data[pt:pt + PARTITION_TABLE_SIZE]):
                rel = p.offset - (im.offset or 0)
                if p.type == APP and 0 <= rel < len(im.data) and app_desc(im.data[rel:]):
                    return im.data[rel:]
    return None


class EspBackend:
    family = "esp32"
    default_power = "ppk2"
    missing = "the DUT's USB port isn't there: is the USB adapter plugged in?"

    def __init__(self, usb):
        self.usb = usb
        self.sim = usb.port.startswith("sim://")

    def present(self) -> bool:
        return self.usb.present()

    def check(self, req: FlashRequest) -> None:
        unknown = set(req.options) - set(OPTIONS)
        if unknown:
            raise FlashError(f"unknown esp32 option(s): {', '.join(sorted(unknown))}")
        req.options = {**OPTIONS, **req.options}
        o = req.options
        if not req.chip:
            req.chip = next((c for c in (image_chip(im.data) for im in req.images) if c), "")
        if not CHIP.match(req.chip or ""):
            raise FlashError(f"unknown chip {req.chip!r}" if req.chip else "chip unknown: say which (e.g. esp32c6)")
        if o["flash_mode"] not in FLASH_MODE or not FLASH_FREQ.match(o["flash_freq"]) or not FLASH_SIZE.match(o["flash_size"]):
            raise FlashError("bad flash mode, frequency or size")
        if o["app_only"]:
            if len(req.images) != 1 or req.images[0].offset is not None or not app_desc(req.images[0].data):
                raise FlashError("an app-only upload is one ESP app image without an offset")
        else:
            for im in req.images:
                if im.offset is None or im.offset < 0 or im.offset % 0x1000 or im.offset >= 2**27:
                    raise FlashError(f"bad image offset {im.offset}")
        desc = app_desc(find_app(req.images) or b"")
        # What the upload says wins; else the app image's own description. Not in Arduino builds: there it comes from
        # the precompiled framework (project "arduino-lib-builder", its git hash), not from the sketch.
        if desc and desc["project"] not in ARDUINO_FRAMEWORK:
            req.meta.setdefault("version", desc["version"])
            req.meta.setdefault("project", desc["project"])

    def flash(self, job: FlashJob, port: str) -> bool:
        req = job.req
        if req.options["app_only"]:
            self._place_app(job, port)
        out = EsptoolOutput(job, len(req.images))
        if self.sim:
            ok = self._sim_write(job, out)
        else:
            with tempfile.TemporaryDirectory(prefix="flash-") as d:
                paths = []
                for i, im in enumerate(req.images):
                    paths.append(os.path.join(d, f"{i}_{im.offset:#x}.bin"))
                    with open(paths[-1], "wb") as f:
                        f.write(im.data)
                args = esptool_args(req, port, paths)
                job.say("$ esptool " + " ".join(a for a in args[3:] if a not in paths))
                ok = run_esptool(args, out.line) == 0
        out.finish()
        return ok

    # app-only uploads -----------------------------------------------------------------------------------
    def _place_app(self, job: FlashJob, port: str) -> None:
        """Turn the one app image into (otadata erased, app at its partition), from the chip's partition table."""
        req = job.req
        if req.images[0].offset is not None:
            return  # already placed (the retry)
        job.say("App image only: reading the partition table from the chip")
        table = self._read_partition_table(job, port)
        target, otadata = app_slot(parse_partition_table(table))
        app = req.images[0].data
        if len(app) > target.size:
            raise FlashError(f"the app ({len(app)} bytes) doesn't fit partition '{target.label}' ({target.size} bytes)")
        req.images = [Image(target.offset, app)]
        if otadata:  # erased otadata: the bootloader boots the factory app, or else ota_0
            req.images.insert(0, Image(otadata.offset, b"\xff" * otadata.size))
        job.say(f"App goes to partition '{target.label}' at {target.offset:#x}"
                + (f", otadata at {otadata.offset:#x} erased so it boots" if otadata else ""))

    def _read_partition_table(self, job: FlashJob, port: str) -> bytes:
        if self.sim:
            return self.usb.sim_factory.flash.get(PARTITION_TABLE_OFFSET, b"")[:PARTITION_TABLE_SIZE]
        with tempfile.TemporaryDirectory(prefix="pt-") as d:
            path = os.path.join(d, "pt.bin")
            args = [sys.executable, "-m", "esptool", "--chip", job.req.chip, "--port", port, "--before", "usb-reset",
                    "--after", "no-reset", "read-flash", f"{PARTITION_TABLE_OFFSET:#x}", f"{PARTITION_TABLE_SIZE:#x}", path]
            lines = []
            if run_esptool(args, lines.append) != 0 or not os.path.exists(path):
                for ln in lines[-5:]:
                    job.say(ln)
                raise FlashError("couldn't read the partition table from the chip")
            with open(path, "rb") as f:
                return f.read()

    def _sim_write(self, job: FlashJob, out: EsptoolOutput) -> bool:
        """esptool's output for these images, as esptool 5 words it (so the parser is tested too). The simulated
        chip remembers what was written, for --skip-flashed."""
        sim = self.usb.sim_factory
        req = job.req
        out.line(f"Connecting... Chip type: {req.chip} (simulated)")
        out.line("MAC:                " + sim.mac)
        n = len(req.images)
        for i, im in enumerate(req.images):
            name = f"'/tmp/sim/{i}_{im.offset:#x}.bin'"
            if n > 1:
                out.line(f"Writing {name} at {im.offset:#010x}...")
            if not req.full_flash:
                out.line("Comparing flash contents against new data...")
                if sim.flash.get(im.offset) == im.data:
                    out.line(f"{name} at {im.offset:#010x} already in flash, skipping write.")
                    continue
            for pct in (0, 25, 50, 75, 100):
                out.line(f"Writing at {im.offset + len(im.data) * pct // 100:#010x} [{pct:3d} %]")
                time.sleep(0.05)
            out.line(f"Wrote {len(im.data)} bytes at {im.offset:#010x} (simulated)")
            out.line("Hash of data verified.")
            sim.flash[im.offset] = im.data
        out.line("Hard resetting via RTS pin...")
        sim.flashed += 1
        return True


def esptool_args(req: FlashRequest, port: str, paths: list[str]) -> list[str]:
    o = req.options
    args = [sys.executable, "-m", "esptool", "--chip", req.chip, "--port", port, "--baud", ESPTOOL_BAUD,
            # usb-reset: the USB-Serial/JTAG reset sequence. esptool only picks it by itself for ports
            # under /dev, and here the host's devices are under /host-dev.
            "--before", "usb-reset", "--after", "hard-reset",
            "write-flash", "-z", "--flash-mode", o["flash_mode"], "--flash-freq", o["flash_freq"],
            "--flash-size", o["flash_size"]]
    if not req.full_flash:
        args.append("--skip-flashed")  # MD5 of each image against the flash first: equal = not written
    for im, path in zip(req.images, paths):
        args += [f"{im.offset:#x}", path]
    return args


def run_esptool(args: list[str], on_line) -> int:
    """Run esptool, each output line (also progress lines ending in \\r) to on_line. Its exit code."""
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "120", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    timer = threading.Timer(ESPTOOL_TIMEOUT_S, proc.kill)
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


class EsptoolOutput:
    """esptool's output into the job log: overall progress, the chip's MAC, and which images were skipped."""

    def __init__(self, job: FlashJob, n_images: int):
        self.job = job
        self.n = n_images
        self.image = 0  # the image esptool is on (it only says so with more than one)
        self.last_pct = -1.0
        self.skipped: set[int] = set()

    def line(self, line: str) -> None:
        job = self.job
        line = line.strip()
        if not line:
            return
        if (m := MAC_LINE.match(line)) and job.mac is None:
            job.mac = m.group(1).replace(":", "").lower()
        elif m := IMAGE_LINE.match(line):
            self.image = min(int(m.group(1)), self.n - 1)
            self.last_pct = -1.0
            job.progress = max(job.progress, min(99.0, self.image / self.n * 100))
        elif SKIPPED_LINE.search(line):
            self.skipped.add(self.image)
            job.progress = max(job.progress, min(99.0, (self.image + 1) / self.n * 100))
        elif (m := PERCENT.search(line)) and "Writing" in line:
            pct = float(m.group(1))
            job.progress = max(job.progress, min(99.0, (self.image + pct / 100) / self.n * 100))
            if int(pct) // 10 == int(self.last_pct) // 10:
                return  # keep the log short: every 10 %
            self.last_pct = pct
        job.say(line)

    def finish(self) -> None:
        self.job.skipped = sorted(self.job.req.images[i].offset for i in self.skipped)
