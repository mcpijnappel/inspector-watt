#!/usr/bin/env python3
"""pst_flash.py: upload firmware through Inspector Watt. The tracker flashes the board over its own USB port: an ESP32
while the PPK2 keeps powering it (and wakes it first if it's asleep), an RP2040/RP2350 with picotool (by default on
USB power, without touching the PPK2). One file, Python 3.8+, standard library only.

As a command, from any build system:

    python pst_flash.py build/                      ESP-IDF build folder (flasher_args.json)
    python pst_flash.py build/esp32.esp32.xiao/     arduino-cli --export-binaries folder (*.merged.bin or *.uf2)
    python pst_flash.py firmware.merged.bin         a merged image (written at 0x0)
    python pst_flash.py firmware.bin                one app image: the tracker puts it in the app partition
    python pst_flash.py 0x0 boot.bin 0x8000 pt.bin 0x10000 app.bin
    python pst_flash.py firmware.uf2                RP2040 / RP2350 (pico-sdk, arduino-pico)
    python pst_flash.py --help                      all options

As PlatformIO's upload command (platformio.ini, this file next to it):

    [env:xiao_c6_tracker]
    extends = env:seeed_xiao_esp32c6
    upload_protocol = custom
    extra_scripts = post:pst_flash.py
    custom_tracker_url = http://<tracker host>:8080
    ; custom_tracker_on_recording = ask    ; ask | keep | stop | after | cancel, when a recording is running
    ; custom_tracker_when_asleep = wait    ; wait (then power-cycle) | always (only wait) | cycle (power-cycle now)
    ; custom_tracker_full_flash = yes      ; write every image, also those already on the board
    ; custom_tracker_power = ppk2          ; ppk2 | usb: what powers the board (default: esp32 ppk2, rp2040 usb)

The same for an RP2040 / RP2350 (arduino-pico): extends = env:seeed_xiao_rp2040 (or your pico environment).

The token comes from the environment variable PST_FLASH_TOKEN (the tracker's upload token: Settings → Firmware
upload). PST_URL sets the tracker (or --url, or custom_tracker_url).

If a recording is running and on_recording is "ask", answer here (k/s/a/c + Enter) or in the tracker's web UI.
"after" queues the upload until the recording stops: this command then ends right away and the tracker flashes
later (see its web UI).
"""

import argparse
import base64
import glob
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

POLL_S = 0.5
ON_RECORDING = ("ask", "keep", "stop", "after", "cancel")
WHEN_ASLEEP = ("wait", "always", "cycle")
POWER = ("ppk2", "usb")
# ESP image format: the chip an image was built for (esptool's IMAGE_CHIP_ID per target).
CHIP_IDS = {0: "esp32", 2: "esp32s2", 5: "esp32c3", 9: "esp32s3", 12: "esp32c2", 13: "esp32c6", 16: "esp32h2",
            18: "esp32p4", 20: "esp32c61", 23: "esp32c5", 25: "esp32h21", 28: "esp32h4"}
APP_DESC_MAGIC = 0xABCD5432
# UF2 (RP2040 / RP2350): 512-byte blocks, each with the chip family it's for.
UF2_MAGIC = (0x0A324655, 0x9E5D5157)
UF2_END = 0x0AB16F30
UF2_FAMILIES = {0xE48BFF56: "rp2040", 0xE48BFF59: "rp2350", 0xE48BFF5A: "rp2350", 0xE48BFF5B: "rp2350"}
UF2_FAMILY_ID = {"rp2040": 0xE48BFF56, "rp2350": 0xE48BFF59}  # for a .bin turned into a UF2 (rp2350: Arm)
RP_FLASH = 0x10000000


class UploadError(Exception):
    pass


class Upload:
    """What goes to the tracker: images (offset None = the tracker places it), chip, the family's settings."""

    def __init__(self, images, chip="", family="esp32", options=None, meta=None, source=""):
        self.images = images  # [(offset or None, bytes, name)]
        self.chip = chip
        self.family = family
        self.options = dict(options or {})
        self.meta = dict(meta or {})
        self.meta.setdefault("source", source)
        self.meta.setdefault("host", socket.gethostname())

    def payload(self, on_recording="ask", when_asleep="", full_flash=False, power=""):
        return {"family": self.family, "chip": self.chip, **self.options,
                "images": [{"offset": off, "data_b64": base64.b64encode(data).decode()} for off, data, _ in self.images],
                "on_recording": on_recording, "when_asleep": when_asleep, "full_flash": full_flash, "power": power,
                "meta": {k: v for k, v in self.meta.items() if v}}

    def describe(self):
        if self.family == "rp2":
            where = ", ".join(f"{name} (UF2, {len(data)} bytes)" for _, data, name in self.images)
        else:
            where = ", ".join(f"{name} @ {off:#x}" if off is not None else f"{name} (app partition)"
                              for off, _, name in self.images)
        return f"chip {self.chip or '?'}, {where}"


# ESP images ---------------------------------------------------------------------------------------------------
def image_chip(data):
    if len(data) < 24 or data[0] != 0xE9:
        return ""
    return CHIP_IDS.get(struct.unpack_from("<H", data, 12)[0], "")


def is_app_image(data):
    """An ESP app image (not a bootloader or a merged image): esp_app_desc_t right after the headers."""
    return len(data) > 36 and data[0] == 0xE9 and struct.unpack_from("<I", data, 32)[0] == APP_DESC_MAGIC


# UF2 images (RP2040 / RP2350) -----------------------------------------------------------------------------------
def is_uf2(data):
    return len(data) >= 512 and struct.unpack_from("<2I", data) == UF2_MAGIC


def uf2_chip(data):
    """The chip a UF2 file is for, from its blocks' family IDs ("" if none says)."""
    for i in range(0, len(data) - 511, 512):
        magic0, magic1, flags = struct.unpack_from("<3I", data, i)
        if (magic0, magic1) == UF2_MAGIC and flags & 0x2000:
            chip = UF2_FAMILIES.get(struct.unpack_from("<I", data, i + 28)[0])
            if chip:
                return chip
    return ""


def bin_to_uf2(data, chip, base=RP_FLASH):
    """A raw flash image (written at the start of flash) as UF2 blocks of 256 bytes, like picotool writes them."""
    chunks = [data[i:i + 256] for i in range(0, len(data), 256)]
    out = []
    for n, chunk in enumerate(chunks):
        head = struct.pack("<8I", UF2_MAGIC[0], UF2_MAGIC[1], 0x2000, base + n * 256, 256, n, len(chunks),
                           UF2_FAMILY_ID[chip])
        out.append(head + chunk.ljust(476, b"\0") + struct.pack("<I", UF2_END))
    return b"".join(out)


def from_uf2(path, data=None):
    data = data or _read(path)
    if not is_uf2(data):
        raise UploadError(f"{path} isn't a UF2 file")
    name = os.path.basename(path)
    project = name[:-4] if name.endswith(".uf2") else name
    project = project[:-4] if project.endswith(".ino") else project  # arduino-cli: blink.ino.uf2
    return Upload([(None, data, name)], uf2_chip(data), family="rp2",
                  meta={"project": project, "git": _git(os.path.dirname(os.path.abspath(path)))}, source="uf2")


def _read(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise UploadError(f"can't read {path}: {e.strerror}") from None
    if not data:
        raise UploadError(f"{path} is empty")
    return data


# readers: a build's output -> Upload ---------------------------------------------------------------------------
def from_idf(path):
    """ESP-IDF: build/flasher_args.json (written by idf.py build), with project_description.json next to it."""
    if os.path.isdir(path):
        path = os.path.join(path, "flasher_args.json")
    base = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, encoding="utf-8") as f:
            args = json.load(f)
    except (OSError, ValueError) as e:
        raise UploadError(f"can't read {path}: {e}") from None
    files = args.get("flash_files") or {}
    if not files:
        raise UploadError(f"{path} lists no flash_files")
    images = sorted((int(off, 0), _read(os.path.join(base, rel)), os.path.basename(rel)) for off, rel in files.items())
    fs = args.get("flash_settings") or {}
    options = {k: fs[k] for k in ("flash_mode", "flash_freq", "flash_size") if fs.get(k)}
    chip = (args.get("extra_esptool_args") or {}).get("chip", "")
    meta = {}
    try:
        with open(os.path.join(base, "project_description.json"), encoding="utf-8") as f:
            desc = json.load(f)
        meta = {"project": desc.get("project_name"), "version": desc.get("project_version"),
                "git": _git(desc.get("project_path") or base)}
        chip = chip or desc.get("target", "")
    except (OSError, ValueError):
        meta = {"git": _git(base)}
    return Upload(images, chip or image_chip(images[0][1]), options=options, meta=meta, source="esp-idf")


def from_file(path):
    """One .bin: an app image (the tracker puts it in the app partition) or a merged image (written at 0x0)."""
    data = _read(path)
    name = os.path.basename(path)
    if is_uf2(data):
        return from_uf2(path, data)
    meta = {"git": _git(os.path.dirname(os.path.abspath(path)))}
    if is_app_image(data):
        return Upload([(None, data, name)], image_chip(data), options={"app_only": True}, meta=meta, source="app image")
    if data[0] == 0xE9:
        return Upload(split_merged(data, name), image_chip(data), meta=meta, source="merged image")
    raise UploadError(f"{path} isn't an ESP image or a UF2 file (ESP: give offsets, like 0x10000 {name})")


SECTOR = 0x1000
PARTITION_TABLE = 0x8000


def split_merged(data, name="merged"):
    """A merged image (esptool merge-bin, arduino-cli) also holds the erased gaps between partitions: written whole,
    it would wipe the board's NVS (saved settings, WiFi). Split it along its own partition table: the bootloader
    part, the partition table, and the partitions that hold data. Without a partition table: the whole image."""
    def part(off, end):
        chunk = data[off:min(end, len(data))].rstrip(b"\xff")
        if not chunk:
            return None
        n = -(-len(chunk) // SECTOR) * SECTOR  # whole sectors: what esptool erases anyway
        return data[off:off + n]

    table = data[PARTITION_TABLE:PARTITION_TABLE + 0xC00]
    if table[:2] != b"\xaa\x50":
        return [(0x0, data, name)]
    out = [(0x0, part(0, PARTITION_TABLE), f"{name}: bootloader"),
           (PARTITION_TABLE, data[PARTITION_TABLE:PARTITION_TABLE + SECTOR], f"{name}: partition table")]
    for i in range(0, len(table) - 31, 32):
        e = table[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        off, size = struct.unpack_from("<II", e, 4)
        label = e[12:28].split(b"\0")[0].decode(errors="replace")
        out.append((off, part(off, off + size), f"{name}: {label}"))
    return [(off, d, n) for off, d, n in out if d]


def from_pairs(words):
    """0x0 boot.bin 0x8000 partitions.bin 0x10000 app.bin"""
    if len(words) % 2:
        raise UploadError("give offset and file in pairs: 0x0 boot.bin 0x10000 app.bin")
    images = []
    for off, path in zip(words[::2], words[1::2]):
        try:
            images.append((int(off, 0), _read(path), os.path.basename(path)))
        except ValueError:
            raise UploadError(f"{off!r} isn't an offset (like 0x10000)") from None
    images.sort()
    chip = next((c for c in (image_chip(d) for _, d, _ in images) if c), "")
    return Upload(images, chip, meta={"git": _git(os.path.dirname(os.path.abspath(words[1])))}, source="files")


def from_dir(path):
    """A build folder: ESP-IDF's (flasher_args.json), one with a merged image (arduino-cli, esptool merge-bin), or
    one with a UF2 file (arduino-cli for RP2040/RP2350, pico-sdk's build/)."""
    if os.path.exists(os.path.join(path, "flasher_args.json")):
        return from_idf(path)
    merged = sorted(glob.glob(os.path.join(path, "*.merged.bin")) + glob.glob(os.path.join(path, "*merged*.bin")))
    if merged:
        return from_file(merged[0])
    uf2s = sorted(glob.glob(os.path.join(path, "*.uf2")))
    if len(uf2s) == 1:
        return from_uf2(uf2s[0])
    if uf2s:
        raise UploadError(f"{path} has {len(uf2s)} .uf2 files: name the one to upload")
    raise UploadError(f"{path}: no flasher_args.json (ESP-IDF), *.merged.bin or *.uf2 in it")


def from_sources(words):
    if len(words) == 1:
        return from_dir(words[0]) if os.path.isdir(words[0]) else (
            from_idf(words[0]) if words[0].endswith(".json") else from_file(words[0]))
    return from_pairs(words)


def from_platformio(env, source):
    """The images and offsets PlatformIO's own esptool upload would write; for an RP2040/RP2350 build its UF2."""
    mcu = env.BoardConfig().get("build.mcu", "")
    if mcu.startswith("rp2"):
        return _platformio_rp2(env, source, mcu)
    images = [(env.subst(off), env.subst(path)) for off, path in env.get("FLASH_EXTRA_IMAGES", [])]
    images.append((env.subst("$ESP32_APP_OFFSET"), str(source[0])))
    board = env.BoardConfig()
    options = {"flash_mode": env.subst("${__get_board_flash_mode(__env__)}") or board.get("build.flash_mode", "dio"),
               "flash_freq": env.subst("${__get_board_f_image(__env__)}") or "keep",
               "flash_size": board.get("upload.flash_size", "detect")}
    project_dir = env.subst("$PROJECT_DIR")
    meta = {"project": os.path.basename(project_dir), "env": env.subst("$PIOENV"),
            "version": _pio_version(env, project_dir), "git": _git(project_dir)}
    return Upload(sorted((int(off, 0), _read(path), os.path.basename(path)) for off, path in images),
                  board.get("build.mcu", "esp32"), options=options, meta=meta, source="platformio")


def _platformio_rp2(env, source, mcu):
    """arduino-pico (platform raspberrypi): the build's firmware.uf2, else its .bin turned into one."""
    chip = "rp2350" if mcu.startswith("rp2350") else "rp2040"
    stem = os.path.splitext(str(source[0]))[0]  # firmware.elf or firmware.bin
    uf2 = stem + ".uf2"
    if os.path.exists(uf2):
        data = _read(uf2)
    elif os.path.exists(stem + ".bin"):
        data, uf2 = bin_to_uf2(_read(stem + ".bin"), chip), stem + ".bin"
    else:
        raise UploadError(f"no {os.path.basename(uf2)} or .bin next to {os.path.basename(str(source[0]))}")
    project_dir = env.subst("$PROJECT_DIR")
    meta = {"project": os.path.basename(project_dir), "env": env.subst("$PIOENV"),
            "version": _pio_version(env, project_dir), "git": _git(project_dir)}
    return Upload([(None, data, os.path.basename(uf2))], uf2_chip(data) or chip, family="rp2", meta=meta,
                  source="platformio")


def _pio_version(env, project_dir):
    """FIRMWARE_VERSION from the build flags, else a #define in include/*.h."""
    for d in env.get("CPPDEFINES", []):
        if isinstance(d, (list, tuple)) and len(d) == 2 and d[0] == "FIRMWARE_VERSION":
            return str(d[1]).strip('\\"')
    for path in sorted(glob.glob(os.path.join(project_dir, "include", "*.h"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split(None, 2)
                    if len(parts) == 3 and parts[0] == "#define" and parts[1] == "FIRMWARE_VERSION":
                        return parts[2].strip().strip('"')
        except OSError:
            pass
    return None


def _git(path):
    try:
        out = subprocess.run(["git", "describe", "--always", "--dirty", "--tags"], cwd=path,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


# the tracker ----------------------------------------------------------------------------------------------------
def _request(method, url, token, data=None):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = e.reason
        raise UploadError(f"tracker said {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise UploadError(f"can't reach the tracker at {url}: {e.reason}") from None


def _ask_in_terminal(state):
    """Read k/s/a/c from the terminal while the tracker waits; the web UI may answer first."""
    if not sys.stdin or not sys.stdin.isatty():
        return
    try:
        line = sys.stdin.readline().strip().lower()
    except Exception:
        return
    state["answer"] = {"k": "keep", "s": "stop", "a": "after", "c": "cancel"}.get(line[:1])


def send(upload, url, token, on_recording="ask", when_asleep="", full_flash=False, power=""):
    """Upload, follow the job's log, answer the recording question. 0 when flashed (or queued), else 1."""
    url = (url or "").rstrip("/")
    if not url:
        sys.stderr.write("Say which tracker: --url, PST_URL or custom_tracker_url (like http://tracker:8080).\n")
        return 1
    if not token:
        sys.stderr.write("Set PST_FLASH_TOKEN to the tracker's upload token (Settings → Firmware upload).\n")
        return 1
    print(f"Tracker: {url}  {upload.describe()}")
    try:
        job = _request("POST", f"{url}/api/flash", token, upload.payload(on_recording, when_asleep, full_flash, power))
        seen = 0
        asked = False
        state = {"answer": None}
        while True:
            job = _request("GET", f"{url}/api/flash/{job['id']}?after={seen}", token)
            for line in job.get("log", []):
                print("  " + line)
            seen = job.get("log_total", seen)
            if job["status"] == "waiting_choice" and job.get("question"):
                if not asked:
                    asked = True
                    print("  A recording is running. [k] keep it and mark the flash, [s] stop it, [a] flash after it "
                          "stops, [c] cancel (type + Enter here, or answer in the tracker UI)")
                    threading.Thread(target=_ask_in_terminal, args=(state,), daemon=True).start()
                if state["answer"]:
                    try:
                        _request("POST", f"{url}/api/flash/{job['id']}/answer", token, {"choice": state["answer"]})
                    except UploadError as e:  # answered in the UI meanwhile
                        print(f"  {e}")
                    state["answer"] = None
            if job["status"] == "waiting_recording":
                print(f"Queued: the tracker flashes when recording '{job.get('waiting_for')}' stops. "
                      "See its web UI for the result (Settings → Firmware upload).")
                return 0
            if job["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(POLL_S)
    except UploadError as e:
        sys.stderr.write(f"Upload through the tracker failed: {e}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Stopped watching; the tracker finishes the flash on its own.\n")
        return 1
    if job["status"] != "done":
        sys.stderr.write(f"Flash {job['status']}: {job.get('error') or ''}\n")
        return 1
    skipped = sum(1 for im in job.get("images", []) if im.get("skipped"))
    print(f"Flashed{' (board power-cycled first)' if job.get('power_cycled') else ''}"
          + (f", marked in recording {job['session_id']}" if job.get("session_id") else "")
          + (f"; {skipped} of {len(job['images'])} image(s) already on the board, not written" if skipped else ""))
    return 0


# command line ---------------------------------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(
        prog="pst_flash.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Upload firmware through Inspector Watt (the tracker flashes the board over its own USB port).",
        epilog="SOURCE: an ESP-IDF build folder or its flasher_args.json, a folder with a *.merged.bin or a *.uf2, a\n"
               "merged or app .bin, a .uf2 (RP2040/RP2350), or offset/file pairs (0x0 boot.bin 0x10000 app.bin).\n"
               "PST_URL and PST_FLASH_TOKEN (the tracker's upload token) come from the environment.")
    p.add_argument("source", nargs="+", metavar="SOURCE")
    p.add_argument("--url", default=os.environ.get("PST_URL", ""), help="the tracker, like http://tracker:8080")
    p.add_argument("--chip", help="the chip (default: from the build or the image), like esp32c6 or rp2040")
    p.add_argument("--on-recording", choices=ON_RECORDING, default="ask", help="when a recording is running")
    p.add_argument("--when-asleep", choices=WHEN_ASLEEP, default="", help="default: the tracker's setting")
    p.add_argument("--full-flash", action="store_true", help="write every image, also those already on the board")
    p.add_argument("--power", choices=POWER, default="",
                   help="what powers the board: ppk2 (wake / power-cycle it) or usb (just plugged in); "
                        "default: esp32 ppk2, rp2040/rp2350 usb")
    p.add_argument("--flash-mode", help="override the build's (esp32: keep, qio, dio, ...)")
    p.add_argument("--flash-freq", help="override the build's (esp32: keep, 40m, 80m, ...)")
    p.add_argument("--flash-size", help="override the build's (esp32: keep, detect, 4MB, ...)")
    p.add_argument("--dry-run", action="store_true", help="show what would be uploaded, don't send it")
    a = p.parse_args(argv)
    try:
        upload = from_sources(a.source)
    except UploadError as e:
        p.error(str(e))
    if a.chip:
        upload.chip = a.chip
    for key in ("flash_mode", "flash_freq", "flash_size"):
        if getattr(a, key):
            upload.options[key] = getattr(a, key)
    if a.dry_run:
        print(f"Would upload ({upload.meta.get('source')}): {upload.describe()}")
        print(f"  options {upload.options or '-'}; meta {json.dumps({k: v for k, v in upload.meta.items() if v})}")
        return 0
    return send(upload, a.url, os.environ.get("PST_FLASH_TOKEN", ""), a.on_recording, a.when_asleep, a.full_flash,
                a.power)


# PlatformIO -----------------------------------------------------------------------------------------------------
def _pio_option(env, name, default=""):
    try:
        return env.GetProjectOption(name, default)
    except Exception:
        return default


def _pio_upload(source, target, env):
    try:
        upload = from_platformio(env, source)
    except UploadError as e:
        sys.stderr.write(f"{e}\n")
        return 1
    url = os.environ.get("PST_URL") or _pio_option(env, "custom_tracker_url")
    full = str(_pio_option(env, "custom_tracker_full_flash", "")).strip().lower() in ("yes", "true", "1", "on")
    return send(upload, url, os.environ.get("PST_FLASH_TOKEN", ""),
                _pio_option(env, "custom_tracker_on_recording", "ask") or "ask",
                _pio_option(env, "custom_tracker_when_asleep", "") or "", full,
                _pio_option(env, "custom_tracker_power", "") or "")


def _in_platformio():
    try:
        Import  # noqa: F821, B018 (SCons gives extra scripts this)
        return True
    except NameError:
        return False


if _in_platformio():  # first: SCons runs extra scripts with its own globals, whatever __name__ is
    Import("env")  # noqa: F821
    env.Replace(UPLOADCMD=_pio_upload)  # noqa: F821
elif __name__ == "__main__":
    sys.exit(main())
