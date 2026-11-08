"""Flashing the DUT over its own USB port with esptool, for uploads from PlatformIO.

One job at a time:
1. A recording is running: keep it (the flash becomes a 'flash' event, so its reboot counts as expected), stop it,
   flash after it stops (the job waits), or cancel. Asked in the UI or in the PlatformIO terminal unless the
   upload already says.
2. The USB console lets go of the port.
3. The port must be there. It vanishes while the DUT deep-sleeps: either wait for its next wake (power-cycling it
   after wake_wait_s if it doesn't come), or power-cycle it right away with the PPK2.
4. esptool write-flash, then a hard reset: the DUT boots the new firmware.
If esptool fails (e.g. the DUT fell asleep just before it connected), the DUT is power-cycled and it's tried once more.
A job still waiting (for an answer, the recording or a wake) can be cancelled, and a newer upload replaces it.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

from .service import CaptureService, DeviceError
from .usb import UsbManager

log = logging.getLogger(__name__)

MAX_TOTAL_BYTES = 16 * 2**20
CHOICE_TIMEOUT_S = 300.0  # nobody answered "what about the recording?": cancel
PORT_WAIT_S = 10.0  # after power-on: the port shows up within ~1 s
POWER_OFF_S = 0.5
ESPTOOL_TIMEOUT_S = 240.0
ESPTOOL_BAUD = "921600"  # ignored by USB-Serial/JTAG, used by a USB-UART bridge
KEEP_JOBS = 10
MAX_LOG_LINES = 3000

CHOICES = ("keep", "stop", "after", "cancel")
DONE = ("done", "failed", "cancelled")
WAITING = ("queued", "waiting_choice", "waiting_recording", "waiting_wake")  # nothing written yet: cancel/replace is safe
WAKE_MODES = ("wait", "always", "cycle")  # wait = power-cycle after wake_wait_s; always = never power-cycle
WAKE_WAIT_S = 90.0  # default: a bit longer than this firmware's 60 s deep sleep
CHIP = re.compile(r"^esp32[a-z0-9]*$")
FLASH_MODE = ("keep", "qio", "qout", "dio", "dout")
FLASH_FREQ = re.compile(r"^(keep|\d{1,3}m)$")
FLASH_SIZE = re.compile(r"^(keep|detect|\d{1,3}MB)$")
PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
META_KEYS = ("project", "env", "version", "git", "host", "user", "source")


class FlashError(RuntimeError):
    pass


class FlashCancelled(Exception):
    pass


@dataclass
class Image:
    offset: int
    data: bytes


@dataclass
class FlashRequest:
    images: list[Image]
    chip: str
    flash_mode: str = "keep"
    flash_freq: str = "keep"
    flash_size: str = "keep"
    meta: dict = field(default_factory=dict)
    on_recording: str = "ask"  # ask | keep | stop | after | cancel
    when_asleep: str = ""  # wait | cycle; "" = the tracker's setting

    def check(self) -> None:
        if not self.images:
            raise FlashError("no images to flash")
        if not CHIP.match(self.chip):
            raise FlashError(f"unknown chip {self.chip!r}")
        if self.flash_mode not in FLASH_MODE or not FLASH_FREQ.match(self.flash_freq) or not FLASH_SIZE.match(self.flash_size):
            raise FlashError("bad flash mode, frequency or size")
        if self.on_recording not in ("ask", *CHOICES):
            raise FlashError("on_recording must be ask, keep, stop, after or cancel")
        if self.when_asleep not in ("", *WAKE_MODES):
            raise FlashError("when_asleep must be wait, always or cycle")
        offsets = [im.offset for im in self.images]
        if len(set(offsets)) != len(offsets):
            raise FlashError("two images at the same offset")
        for im in self.images:
            if im.offset < 0 or im.offset % 0x1000 or im.offset >= 2**27 or not im.data:
                raise FlashError(f"bad image at offset {im.offset:#x}")
        if sum(len(im.data) for im in self.images) > MAX_TOTAL_BYTES:
            raise FlashError("images too large")
        self.meta = {k: str(v)[:200] for k, v in (self.meta or {}).items() if k in META_KEYS and v is not None}


class FlashJob:
    _ids = itertools.count(1)

    def __init__(self, req: FlashRequest):
        self.id = next(self._ids)
        self.req = req
        self.status = "queued"
        self.created = time.time()
        self.finished: float | None = None
        self.progress = 0.0
        self.error: str | None = None
        self.question: dict | None = None  # waiting_choice: what to do with the recording
        self.choice: str | None = None
        self.session_id: int | None = None  # the recording that got a 'flash' event
        self.power_cycled = False
        self.start_idx: int | None = None  # PPK2 stream index when flashing began (live chart shading)
        self.end_idx: int | None = None
        self.wait_until: float | None = None  # waiting_wake: when it power-cycles instead
        self.waiting_for: str | None = None  # waiting_recording: the recording's name
        self._log: list[str] = []
        self._lock = threading.Lock()
        self._answered = threading.Event()
        self._cancel = threading.Event()
        self._cancel_reason = "cancelled in the tracker"
        self._now = threading.Event()  # waiting_wake: power-cycle now
        self.thread: threading.Thread | None = None

    @property
    def done(self) -> bool:
        return self.status in DONE

    def say(self, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        with self._lock:
            self._log.append(line)
            if len(self._log) > MAX_LOG_LINES:
                del self._log[: len(self._log) - MAX_LOG_LINES]

    def finish(self, status: str, error: str | None = None) -> None:
        self.status = status
        self.error = error
        self.finished = time.time()
        self.question = None
        if status == "done":
            self.progress = 100.0
        self.say(f"== {status}" + (f": {error}" if error else ""))

    def to_dict(self, after: int = 0) -> dict:
        with self._lock:
            lines = self._log[max(0, after):]
            total = len(self._log)
        return {
            "id": self.id, "status": self.status, "created": self.created, "finished": self.finished,
            "progress": round(self.progress, 1), "error": self.error, "question": self.question, "choice": self.choice,
            "session_id": self.session_id, "power_cycled": self.power_cycled, "meta": self.req.meta,
            "start_idx": self.start_idx, "end_idx": self.end_idx, "wait_until": self.wait_until,
            "waiting_for": self.waiting_for, "when_asleep": self.req.when_asleep or None,
            "chip": self.req.chip, "images": [{"offset": im.offset, "bytes": len(im.data)} for im in self.req.images],
            "bytes": sum(len(im.data) for im in self.req.images), "log": lines, "log_total": total,
        }


class Flasher:
    def __init__(self, svc: CaptureService, usb: UsbManager, sessions=None):
        self.svc = svc
        self.usb = usb
        self.sessions = sessions  # SessionManager or None (no database)
        self.jobs: list[FlashJob] = []
        self._lock = threading.Lock()
        self.sim = usb.port.startswith("sim://")

    @property
    def busy(self) -> bool:
        return any(not j.done for j in self.jobs)

    def get(self, job_id: int) -> FlashJob:
        for j in self.jobs:
            if j.id == job_id:
                return j
        raise FlashError(f"no flash job {job_id}")

    def recent(self) -> list[FlashJob]:
        """Newest first (kept in memory: the last few since capture started)."""
        return list(reversed(self.jobs))

    def latest(self) -> FlashJob | None:
        return self.jobs[-1] if self.jobs else None

    def submit(self, req: FlashRequest) -> FlashJob:
        req.check()
        replaced = None
        with self._lock:
            cur = next((j for j in self.jobs if not j.done), None)
            if cur is not None:
                if cur.status not in WAITING:
                    raise FlashError("another flash is running")
                replaced = cur
            job = FlashJob(req)
            self.jobs = [*self.jobs[-(KEEP_JOBS - 1):], job]
        meta = req.meta
        job.say(f"Flash {meta.get('project', '?')} {meta.get('env', '')} {meta.get('git', '')}".rstrip()
                + f" from {meta.get('host', '?')}: {len(req.images)} image(s), {job.to_dict()['bytes']} bytes")
        if replaced is not None:
            # A newer build replaces one that's still waiting: the latest wins.
            self._stop_waiting(replaced, f"replaced by upload {job.id}")
            if replaced.thread:
                replaced.thread.join(timeout=5)
            job.say(f"Replaces upload {replaced.id}, which was still waiting")
        job.thread = threading.Thread(target=self._run, args=(job,), name=f"flash-{job.id}", daemon=True)
        job.thread.start()
        return job

    def answer(self, job_id: int, choice: str) -> FlashJob:
        job = self.get(job_id)
        if choice not in CHOICES:
            raise FlashError("choice must be keep, stop, after or cancel")
        if job.status != "waiting_choice":
            raise FlashError("this flash isn't waiting for an answer")
        job.choice = choice
        job._answered.set()
        return job

    def cancel(self, job_id: int) -> FlashJob:
        """Cancel an upload that's still waiting (for an answer, the recording or a wake). Not once it's flashing."""
        job = self.get(job_id)
        if job.status not in WAITING:
            raise FlashError("only an upload that's still waiting can be cancelled")
        self._stop_waiting(job, "cancelled in the tracker")
        return job

    def power_cycle_now(self, job_id: int) -> FlashJob:
        """Waiting for the DUT to wake: stop waiting and power-cycle it now."""
        job = self.get(job_id)
        if job.status != "waiting_wake":
            raise FlashError("this upload isn't waiting for the DUT to wake")
        job._now.set()
        return job

    @staticmethod
    def _stop_waiting(job: FlashJob, reason: str) -> None:
        job._cancel_reason = reason
        job._cancel.set()
        if job.status == "waiting_choice":
            job.choice = "cancel"
            job._answered.set()

    # the job ---------------------------------------------------------------------------------------------
    def _run(self, job: FlashJob) -> None:
        try:
            if not self._handle_recording(job):
                return
            job.status = "preparing"
            ring = self.svc.ring
            ok = attempted = False
            self.usb.hold()
            try:
                for attempt in (1, 2):
                    port = self._ensure_port(job, power_cycle=attempt == 2)
                    if job.start_idx is None:  # no power-cycle: the flash starts when esptool resets the DUT
                        job.start_idx = ring.end
                    attempted = True
                    job.status = "flashing"
                    job.progress = 0.0
                    if self._esptool(job, port):
                        ok = True
                        break
                    if attempt == 1:
                        job.say("esptool failed: power-cycling the DUT and trying once more")
            finally:
                self.usb.release()
                if attempted:
                    job.end_idx = ring.end
                    self._note(job, job.start_idx, ring.end, ok)
            if ok:
                job.finish("done")
            else:
                job.finish("failed", "esptool failed (see the log)")
        except FlashCancelled as e:
            job.finish("cancelled", str(e))
        except (FlashError, DeviceError) as e:
            job.finish("failed", str(e))
        except Exception as e:  # never leave a job hanging
            log.exception("flash job %d failed", job.id)
            job.finish("failed", f"{type(e).__name__}: {e}")
        log.info("flash job %d: %s%s", job.id, job.status, f" ({job.error})" if job.error else "")

    def _handle_recording(self, job: FlashJob) -> bool:
        """False if the job ends here (cancelled)."""
        sm = self.sessions
        active = sm.active if sm else None
        if not active:
            return True
        choice = job.req.on_recording
        if choice == "ask":
            job.question = {"kind": "recording", "session_id": active.id, "session_name": active.name,
                            "options": [{"choice": "keep", "label": "Keep recording, mark the flash"},
                                        {"choice": "stop", "label": "Stop the recording, then flash"},
                                        {"choice": "after", "label": "Flash when the recording stops"},
                                        {"choice": "cancel", "label": "Cancel the flash"}]}
            job.status = "waiting_choice"
            job.say(f"Recording '{active.name}' is running: keep it, stop it, flash after it, or cancel?")
            if not job._answered.wait(CHOICE_TIMEOUT_S):
                job.finish("cancelled", "no answer about the running recording")
                return False
            choice = job.choice
            job.question = None
        job.choice = choice
        job.say(f"Recording: {choice}")
        if choice == "cancel":
            job.finish("cancelled", None if job._cancel_reason == "cancelled in the tracker" else job._cancel_reason)
            return False
        if choice == "after":
            job.status = "waiting_recording"
            job.waiting_for = active.name
            job.say(f"Queued: flashes when recording '{active.name}' stops")
            while sm.active is not None:
                if job._cancel.wait(1.0):
                    raise FlashCancelled(job._cancel_reason)
            job.waiting_for = None
            job.say("The recording stopped: flashing now")
            return True
        if choice == "stop":
            try:
                sm.stop_session()
            except Exception as e:  # stopped meanwhile
                job.say(f"stopping the recording: {e}")
        else:
            job.session_id = active.id
        return True

    def _ensure_port(self, job: FlashJob, power_cycle: bool = False) -> str:
        """The port, there now. Missing (DUT asleep or off): wait for its next wake, or power-cycle it so it boots
        and shows up."""
        if self.usb.present() and not power_cycle:
            return self.usb.port
        ppk2 = self.svc.ppk2
        mode = job.req.when_asleep or self.svc.flash_when_asleep
        forever = mode == "always"  # never power-cycle by itself, also not for the retry
        if forever and ppk2.dut_power is False:
            raise FlashError("the DUT is off and this upload only waits for it to wake: switch the DUT on, "
                             "or let uploads power-cycle it (Settings → Firmware upload)")
        if (forever or (mode == "wait" and not power_cycle)) and ppk2.dut_power is not False:
            wait_s = self.svc.flash_wake_wait_s
            job.status = "waiting_wake"
            job.wait_until = None if forever else time.time() + wait_s
            job.say("DUT asleep: waiting for its next wake (no power-cycle)" if forever
                    else f"DUT asleep: waiting up to {wait_s:.0f} s for its next wake, then power-cycling it")
            deadline = float("inf") if forever else time.monotonic() + wait_s
            try:
                while time.monotonic() < deadline:
                    if self.usb.present():
                        job.say("The DUT woke up")
                        time.sleep(0.2)  # let udev finish (permissions) before esptool opens it
                        return self.usb.port
                    if job._cancel.is_set():
                        raise FlashCancelled(job._cancel_reason)
                    if job._now.is_set():
                        job.say("Power-cycling now (asked in the tracker)")
                        break
                    time.sleep(0.05)
                else:
                    job.say(f"The DUT didn't wake within {wait_s:.0f} s: power-cycling it")
            finally:
                job.wait_until = None
        if not ppk2.voltage_set_by_us:
            raise FlashError("the DUT's USB port isn't there and the PPK2 output isn't set by the tracker: switch the DUT on first")
        job.status = "waking"
        if job.start_idx is None:  # the flash (and its mark) starts with this power-cycle, not with a wait before it
            job.start_idx = self.svc.ring.end
        if ppk2.dut_power:
            job.say("DUT USB port not there (asleep?): power-cycling the DUT")
            ppk2.set_dut_power(False, by="flash")
            time.sleep(POWER_OFF_S)
        else:
            job.say("DUT is off: switching it on")
        ppk2.set_dut_power(True, by="flash")
        job.power_cycled = True
        if self.sim:
            self.usb.sim_factory.wake()
        deadline = time.monotonic() + PORT_WAIT_S
        while not self.usb.present():
            if time.monotonic() > deadline:
                raise FlashError(f"the DUT's USB port ({self.usb.port}) didn't appear: is the USB adapter plugged in?")
            time.sleep(0.05)
        time.sleep(0.2)  # let udev finish (permissions) before esptool opens it
        return self.usb.port

    def _note(self, job: FlashJob, start: int, end: int, ok: bool) -> None:
        if job.session_id is None or not self.sessions:
            return
        data = {**job.req.meta, "result": "ok" if ok else "failed", "bytes": job.to_dict()["bytes"],
                "power_cycled": job.power_cycled, "job": job.id}
        if not self.sessions.note_event("flash", start, max(end, start + 1), **data):
            job.session_id = None  # the recording ended meanwhile

    # esptool ---------------------------------------------------------------------------------------------
    def _esptool(self, job: FlashJob, port: str) -> bool:
        if self.sim:
            return self._sim_esptool(job)
        req = job.req
        with tempfile.TemporaryDirectory(prefix="flash-") as d:
            args = [sys.executable, "-m", "esptool", "--chip", req.chip, "--port", port, "--baud", ESPTOOL_BAUD,
                    # usb-reset: the USB-Serial/JTAG reset sequence. esptool only picks it by itself for ports
                    # under /dev, and here the host's devices are under /host-dev.
                    "--before", "usb-reset", "--after", "hard-reset",
                    "write-flash", "-z", "--flash-mode", req.flash_mode, "--flash-freq", req.flash_freq,
                    "--flash-size", req.flash_size]
            for i, im in enumerate(req.images):
                path = os.path.join(d, f"{i}_{im.offset:#x}.bin")
                with open(path, "wb") as f:
                    f.write(im.data)
                args += [f"{im.offset:#x}", path]
            job.say("$ esptool " + " ".join(a for a in args[3:] if not a.startswith(d)))
            env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "120", "PYTHONUNBUFFERED": "1"}
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            timer = threading.Timer(ESPTOOL_TIMEOUT_S, proc.kill)
            timer.start()
            try:
                self._follow(job, proc, len(req.images))
                rc = proc.wait()
            finally:
                timer.cancel()
        if rc != 0:
            job.say(f"esptool exited with {rc}")
        return rc == 0

    def _follow(self, job: FlashJob, proc: subprocess.Popen, n_images: int) -> None:
        """esptool's output into the job log, with overall progress from its per-image percentages."""
        buf = b""
        done_images = 0
        last_pct = -1.0
        while True:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            buf += chunk
            *parts, buf = re.split(rb"[\r\n]", buf)
            for raw in parts:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                m = PERCENT.search(line)
                if m and "Writing" in line:
                    pct = float(m.group(1))
                    job.progress = max(job.progress, min(99.0, (done_images + pct / 100) / n_images * 100))
                    if int(pct) // 10 == int(last_pct) // 10:
                        continue  # keep the log short: every 10 %
                    last_pct = pct
                elif line.startswith("Wrote"):
                    done_images += 1
                    last_pct = -1.0
                    job.progress = max(job.progress, min(99.0, done_images / n_images * 100))
                job.say(line)
        if buf.strip():
            job.say(buf.decode(errors="replace"))

    def _sim_esptool(self, job: FlashJob) -> bool:
        n = len(job.req.images)
        job.say(f"Connecting... Chip type: {job.req.chip} (simulated)")
        for i, im in enumerate(job.req.images):
            for pct in (0, 25, 50, 75, 100):
                job.say(f"Writing at {im.offset + len(im.data) * pct // 100:#010x} [{pct:3d} %]")
                job.progress = max(job.progress, min(99.0, (i + pct / 100) / n * 100))
                time.sleep(0.05)
            job.say(f"Wrote {len(im.data)} bytes at {im.offset:#010x} (simulated)")
        job.say("Hard resetting via RTS pin...")
        self.usb.sim_factory.flashed += 1
        return True
