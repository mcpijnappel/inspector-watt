"""Flashing the DUT over its own USB port: the job flow, the same for every controller family.

One job at a time:
1. A recording is running: keep it (the flash becomes a 'flash' event, so its reboot counts as expected), stop it,
   flash after it stops (the job waits), or cancel. Asked in the UI or in the PlatformIO terminal unless the
   upload already says.
2. The USB console lets go of the port.
3. The port must be there. It vanishes while the DUT deep-sleeps: either wait for its next wake (power-cycling it
   after wake_wait_s if it doesn't come), or power-cycle it right away with the PPK2.
4. The family's backend writes the images (esp32: esptool, flash_esp.py; rp2: picotool, flash_rp2.py), then the
   DUT boots the new firmware.
An upload with power "usb" is for a board on USB power, not on the PPK2: steps 1 and 3 don't apply (the recording
isn't touched, nothing is power-cycled); the board just has to be plugged in. Each family has a default (esp32: ppk2,
rp2: usb).
If that fails (e.g. the DUT fell asleep just before it connected), the DUT is power-cycled and it's tried once more.
A job still waiting (for an answer, the recording or a wake) can be cancelled, and a newer upload replaces it.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from .service import CaptureService, DeviceError
from .usb import UsbManager

log = logging.getLogger(__name__)

MAX_TOTAL_BYTES = 16 * 2**20
CHOICE_TIMEOUT_S = 300.0  # nobody answered "what about the recording?": cancel
PORT_WAIT_S = 10.0  # after power-on: the port shows up within ~1 s
POWER_OFF_S = 0.5
KEEP_JOBS = 10
MAX_LOG_LINES = 3000

CHOICES = ("keep", "stop", "after", "cancel")
DONE = ("done", "failed", "cancelled")
WAITING = ("queued", "waiting_choice", "waiting_recording", "waiting_wake")  # nothing written yet: cancel/replace is safe
WAKE_MODES = ("wait", "always", "cycle")  # wait = power-cycle after wake_wait_s; always = never power-cycle
POWERS = ("ppk2", "usb")  # what powers the DUT: the PPK2 (wake / power-cycle it) or its own USB (just plugged in)
WAKE_WAIT_S = 90.0  # default: a bit longer than this firmware's 60 s deep sleep
META_KEYS = ("project", "env", "version", "git", "host", "user", "source")


class FlashError(RuntimeError):
    pass


class FlashCancelled(Exception):
    pass


@dataclass
class Image:
    offset: int | None  # None: the backend works out where it goes (e.g. an ESP app image from Arduino OTA)
    data: bytes


@dataclass
class FlashRequest:
    images: list[Image]
    chip: str = ""  # "": the backend works it out from the images, if it can
    family: str = "esp32"  # which backend flashes it
    options: dict = field(default_factory=dict)  # the backend's own (esp32: flash_mode/freq/size, app_only)
    meta: dict = field(default_factory=dict)
    on_recording: str = "ask"  # ask | keep | stop | after | cancel
    when_asleep: str = ""  # wait | cycle; "" = the tracker's setting
    full_flash: bool = False  # write every image, also those already on the chip
    power: str = ""  # ppk2 | usb; "" = the family's default

    def check(self) -> None:
        """What every family shares; the backend checks the rest (chip, options, offsets)."""
        if not self.images:
            raise FlashError("no images to flash")
        if self.on_recording not in ("ask", *CHOICES):
            raise FlashError("on_recording must be ask, keep, stop, after or cancel")
        if self.when_asleep not in ("", *WAKE_MODES):
            raise FlashError("when_asleep must be wait, always or cycle")
        if self.power not in ("", *POWERS):
            raise FlashError("power must be ppk2 or usb")
        offsets = [im.offset for im in self.images if im.offset is not None]
        if len(set(offsets)) != len(offsets):
            raise FlashError("two images at the same offset")
        if not all(im.data for im in self.images):
            raise FlashError("empty image")
        if sum(len(im.data) for im in self.images) > MAX_TOTAL_BYTES:
            raise FlashError("images too large")
        self.meta = {k: str(v)[:200] for k, v in (self.meta or {}).items() if k in META_KEYS and v is not None}


class Backend(Protocol):
    """Flashes one controller family. check() runs when the upload comes in, flash() once the board is there
    (present(): its USB port, or a boot loader of its own)."""
    family: str
    default_power: str  # ppk2 | usb, when the upload doesn't say
    missing: str  # power "usb" and the board isn't there: what to tell the user

    def check(self, req: FlashRequest) -> None: ...

    def present(self) -> bool: ...

    def flash(self, job: FlashJob, port: str) -> bool: ...


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
        self.mac: str | None = None  # the chip's MAC (hex, no colons) as the backend read it: which board got flashed
        self.skipped: list[int] | None = None  # offsets of images already on the chip (None: not flashed yet)
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
            "family": self.req.family, "chip": self.req.chip, "mac": self.mac, "full_flash": self.req.full_flash,
            "power": self.req.power,
            "images": [{"offset": im.offset, "bytes": len(im.data),
                        "skipped": None if self.skipped is None else im.offset in self.skipped}
                       for im in self.req.images],
            "bytes": sum(len(im.data) for im in self.req.images), "log": lines, "log_total": total,
        }


class Flasher:
    def __init__(self, svc: CaptureService, usb: UsbManager, sessions=None, backends: list[Backend] | None = None):
        from .flash_esp import EspBackend  # (they import this module)
        from .flash_rp2 import Rp2Backend
        self.svc = svc
        self.usb = usb
        self.sessions = sessions  # SessionManager or None (no database)
        self.backends = {b.family: b for b in (backends or [EspBackend(usb), Rp2Backend(usb)])}
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
        backend = self.backends.get(req.family)
        if backend is None:
            raise FlashError(f"can't flash {req.family!r} (this tracker: {', '.join(self.backends)})")
        req.power = req.power or backend.default_power
        req.check()
        backend.check(req)
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
                    if self.backends[job.req.family].flash(job, port):
                        ok = True
                        break
                    if attempt == 1:
                        job.say("Flashing failed: " + ("power-cycling the DUT and trying once more"
                                                       if job.req.power == "ppk2" else "trying once more"))
            finally:
                self.usb.release()
                if attempted:
                    job.end_idx = ring.end
                    self._note(job, job.start_idx, ring.end, ok)
            if ok:
                job.finish("done")
            else:
                job.finish("failed", "flashing failed (see the log)")
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
        if job.req.power == "usb":
            job.say(f"Recording '{active.name}' keeps running: this board is on USB power, not on the PPK2")
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

    def _present(self, job: FlashJob) -> bool:
        return self.backends[job.req.family].present()

    def _ensure_port(self, job: FlashJob, power_cycle: bool = False) -> str:
        """The port, there now. Missing (DUT asleep or off): wait for its next wake, or power-cycle it so it boots
        and shows up. On USB power: it just has to be plugged in (a moment's grace for a board that's restarting)."""
        if job.req.power == "usb":
            deadline = time.monotonic() + PORT_WAIT_S / 2
            while not self._present(job):
                if time.monotonic() > deadline:
                    raise FlashError(self.backends[job.req.family].missing)
                time.sleep(0.1)
            return self.usb.port
        if self._present(job) and not power_cycle:
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
                    if self._present(job):
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
        while not self._present(job):
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
