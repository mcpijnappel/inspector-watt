"""Session lifecycle: start/stop, writing recorder output to the database, recovery.

Recovery rules:
- PPK2 replugged during a session: the stream resumes on the same timeline (gap event), and with
  auto_restore the session voltage + board power are set again (dut_restore event).
- Capture service restarted during a session: the session continues where the wall clock says it is
  (gap event for the downtime); same auto_restore behaviour.
"""

from __future__ import annotations

import logging
import pickle
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import battery
from .ppk2 import SAMPLE_PERIOD_S, SAMPLE_RATE_HZ
from .recorder import BIN, SessionConfig, SessionRecorder, merge_batches, new_batch
from .service import CaptureService, DeviceError
from .store import Store, StoreUnavailable

log = logging.getLogger(__name__)

TICK_S = 0.5
# After a restart/stall: the board counts as still powered when it draws more than this on average.
# The PPK2 reads ~0.1-0.2 uA with the output off.
DUT_ALIVE_A = 1e-6
ALIVE_CHECK_SAMPLES = 50_000  # 0.5 s
MAX_PENDING_ROLLUPS = 360_000  # 1 h of 10 ms rows kept in memory while the database is unreachable
RETENTION_EVERY_S = 60
BATTERY_SET_EVERY_S = 1.0  # battery emulation: at most one voltage change per second
BATTERY_LOG_MV = 10  # ... and a 'battery' event every 10 mV
# After a restart the PPK2 still outputs the last battery voltage; this close to the target counts as "still set".
BATTERY_KEEP_MV = 50
SPOOL_EVERY_S = 10


class SessionError(RuntimeError):
    pass


@dataclass
class Active:
    id: int
    name: str
    kind: str
    started_at: datetime
    cfg: SessionConfig
    rec: SessionRecorder
    bat_set_at: float = 0.0  # monotonic time of the last battery voltage change
    bat_logged_mv: int | None = None


class SessionManager:
    def __init__(self, svc: CaptureService, store: Store, data_dir: Path, min_free_bytes: int,
                 mantissa_bits: int = 12):
        self.svc = svc
        self.store = store
        self.data_dir = data_dir
        self.min_free_bytes = min_free_bytes
        self.mantissa_bits = mantissa_bits
        self.active: Active | None = None
        self.db_ok = False
        self.last_error: str | None = None
        self._pending = new_batch()
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sessions", daemon=True)
        self._last_retention = 0.0
        self._device_loaded = False
        self._last_spool_check = 0.0
        self.spool_dir = data_dir / "spool"
        svc.ppk2.on_resume = self._on_ppk2_resume

    def settings(self) -> dict:
        return {"min_free_gb": round(self.min_free_bytes / 2**30, 2), "raw_mantissa_bits": self.mantissa_bits}

    def apply_settings(self, s: dict) -> None:
        """Free disk to keep (also for the running recording) and raw precision (from the next recording)."""
        if s.get("min_free_gb") is not None:
            self.min_free_bytes = int(float(s["min_free_gb"]) * 2**30)
            if self.active:
                self.active.rec.min_free_bytes = self.min_free_bytes
        if s.get("raw_mantissa_bits") is not None:
            self.mantissa_bits = int(s["raw_mantissa_bits"])

    # lifecycle -------------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        """Container stop: keep the session 'running' in the database so it resumes on the next start."""
        self._stop_evt.set()
        self._thread.join(timeout=5)
        with self._lock:
            if self.active:
                self._pending = merge_batches(self._pending, self.active.rec.checkpoint())
                if not self._flush():
                    self._spool(self.active.id, self._pending)

    # clock helpers ---------------------------------------------------------------------------------------
    def _wait_for_stream(self, timeout_s: float = 5.0) -> None:
        ppk2 = self.svc.ppk2
        if not ppk2.streaming:
            ppk2.start_stream()
        end = time.monotonic() + timeout_s
        while self.svc.clock.offset is None or self.svc.ring.end == 0:
            if time.monotonic() > end:
                raise SessionError("PPK2 stream did not start")
            time.sleep(0.05)

    def _wall_of(self, stream_idx: int) -> datetime:
        mono = self.svc.clock.offset + stream_idx * SAMPLE_PERIOD_S
        return datetime.fromtimestamp(time.time() - (time.monotonic() - mono), timezone.utc)

    def _stream_idx_of(self, wall: datetime) -> int:
        mono = time.monotonic() - (time.time() - wall.timestamp())
        return round((mono - self.svc.clock.offset) / SAMPLE_PERIOD_S)

    # commands --------------------------------------------------------------------------------------------
    def start_session(self, name: str, cfg: SessionConfig, notes: str = "") -> dict:
        with self._lock:
            if self.active:
                raise SessionError(f"session {self.active.id} is already running")
            if cfg.kind not in ("detail", "soak"):
                raise SessionError("kind must be 'detail' or 'soak'")
            if not self.store.ping():
                raise SessionError("database not reachable")
            bat = battery.BatteryConfig.from_dict(cfg.battery)
            if bat:
                try:
                    bat.check(self.svc.ppk2.max_mv)
                except ValueError as e:
                    raise SessionError(str(e)) from e
                cfg.battery = bat.to_dict()
                cfg.voltage_mv = battery.state(bat, 0.0, 0.0).voltage_mv  # start voltage; follows the battery after
                cfg.dut_power = True
            if cfg.voltage_mv and cfg.voltage_mv > self.svc.ppk2.max_mv:
                raise SessionError(f"{cfg.voltage_mv} mV is above the limit ({self.svc.ppk2.max_mv} mV)")
            if cfg.uart_baud:
                self.svc.uart.set_baud(cfg.uart_baud)
            try:
                self._wait_for_stream()
            except DeviceError as e:
                raise SessionError(str(e)) from e
            s0 = self.svc.ring.end
            started_at = self._wall_of(s0)
            sid = self.store.create_session(name, cfg.kind, started_at, cfg.to_dict(), notes)
            rec = self._recorder(sid, started_at, s0, s0, cfg)
            self.active = Active(sid, name, cfg.kind, started_at, cfg, rec)
            log.info("session %d '%s' (%s) started", sid, name, cfg.kind)
            if cfg.voltage_mv:  # after creating the session, so the boot is recorded
                self._power_up(rec, "power_on")
            return self.status()

    def save_range(self, name: str, start_idx: int, end_idx: int, notes: str = "") -> dict:
        """Store a piece of the live buffer (stream indices [start, end)) as a finished detail session."""
        with self._lock:
            if self.active:
                raise SessionError("a session is recording; its data is stored already")
            if self.svc.clock.offset is None:
                raise SessionError("no live data")
            if not self.store.ping():
                raise SessionError("database not reachable")
            ring = self.svc.ring
            start = max(start_idx, ring.start)
            end = min(end_idx, ring.end)
            end = start + (end - start) // BIN * BIN
            if end - start < BIN:
                raise SessionError("range is too short or no longer in the live buffer")
            cfg = SessionConfig(kind="detail")
            started_at = self._wall_of(start)
            sid = self.store.create_session(name, "detail", started_at, {**cfg.to_dict(), "saved_from_buffer": True},
                                            notes)
            rec = SessionRecorder(sid, started_at, start, start, cfg, ring, self.svc.uart.log,
                                  self.svc.uart.log.seq_before(start), self.data_dir, self.min_free_bytes, 0.0,
                                  self.mantissa_bits)
            rec.stop_at = end
            batch = new_batch()
            while rec.cursor < end:
                before = rec.cursor
                batch = merge_batches(batch, rec.tick())
                if rec.cursor == before:
                    break
            batch = merge_batches(batch, rec.tick(final=True))
            try:
                self.store.write_batch(sid, batch)
            except StoreUnavailable:
                self._spool(sid, batch)
            self.store.finish_session(sid, "stopped", rec.ts(rec.cursor))
            log.info("saved %.1f s of the live buffer as session %d", (rec.cursor - start) * SAMPLE_PERIOD_S, sid)
            return {"saved": sid, "duration_s": (rec.cursor - start) * SAMPLE_PERIOD_S}

    def stop_session(self, dut_off: bool = False) -> dict:
        with self._lock:
            a = self.active
            if not a:
                raise SessionError("no session running")
            self._pending = merge_batches(self._pending, a.rec.tick(final=True))
            for _ in range(10):
                if self._flush():
                    break
                time.sleep(0.5)
            if not self._flush():
                self._spool(a.id, self._pending)
                self._pending = new_batch()
            try:
                self.store.finish_session(a.id, "stopped", a.rec.ts(a.rec.cursor))
            except StoreUnavailable as e:
                self.last_error = str(e)
            self.active = None
            if dut_off:
                try:
                    self.svc.ppk2.set_dut_power(False, by="session")
                except DeviceError:
                    pass
            log.info("session %d stopped", a.id)
            return {"stopped": a.id}

    def note_event(self, kind: str, start_idx: int, end_idx: int | None = None, *, module: str | None = None,
                   **data) -> bool:
        """An event from outside the recorder (a firmware flash, a module's decoded bus transaction) on the running
        session; False if none runs."""
        with self._lock:
            if not self.active:
                return False
            self.active.rec.note(kind, start_idx, end_idx, module=module, **data)
            return True

    def _recorder(self, sid: int, started_at: datetime, s0: int, first_idx: int, cfg: SessionConfig,
                  charge_c: float = 0.0) -> SessionRecorder:
        return SessionRecorder(sid, started_at, s0, first_idx, cfg, self.svc.ring, self.svc.uart.log,
                               self.svc.uart.log.last_seq, self.data_dir, self.min_free_bytes, charge_c,
                               self.mantissa_bits)

    def _wanted_mv(self, rec: SessionRecorder) -> int | None:
        """The voltage the session wants now: fixed, or where the emulated battery is."""
        bat = battery.BatteryConfig.from_dict(rec.cfg.battery)
        if bat:
            return battery.state(bat, rec.charge_c, rec.processed_samples * SAMPLE_PERIOD_S).voltage_mv
        return rec.cfg.voltage_mv

    def _power_up(self, rec: SessionRecorder, kind: str) -> None:
        cfg = rec.cfg
        ppk2 = self.svc.ppk2
        mv = self._wanted_mv(rec)
        if (ppk2.mode is not None and ppk2.mode.name == "SOURCE" and ppk2.voltage_mv == mv
                and ppk2.voltage_set_by_us and (ppk2.dut_power or not cfg.dut_power)):
            # Already in the wanted state: re-sending the commands would only blip the output.
            rec.note("dut_kept", self.svc.ring.end, voltage_mv=mv, reason="already set")
            return
        try:
            self.svc.ppk2.set_source_voltage(mv)
            if cfg.dut_power:
                self.svc.ppk2.set_dut_power(True, by="session")
            rec.note(kind, self.svc.ring.end, voltage_mv=mv, dut_power=cfg.dut_power)
        except DeviceError as e:
            rec.note(kind + "_failed", self.svc.ring.end, error=str(e))
            log.error("session %d: %s failed: %s", rec.session_id, kind, e)

    def _on_ppk2_resume(self, gap_start: int, gap_samples: int, reason: str = "ppk2_reconnect") -> None:
        """Called by the PPK2 reader thread after a replug or stall. That thread must never wait (the PPK2 would
        lose samples), so the work runs in its own short-lived thread."""
        threading.Thread(target=self._handle_ppk2_resume, args=(gap_start, gap_samples, reason), daemon=True).start()

    def _dut_still_powered(self, rec: SessionRecorder) -> float | None:
        """Mean current (A) if the PPK2 still supplies the session voltage and the board draws current, else None.
        Then nothing needs to be restored, and re-sending the commands would only reboot the board."""
        ppk2 = self.svc.ppk2
        mv = self._wanted_mv(rec)
        tolerance = BATTERY_KEEP_MV if rec.cfg.battery else 0
        if (ppk2.mode is None or ppk2.mode.name != "SOURCE" or ppk2.voltage_mv is None or mv is None
                or abs(ppk2.voltage_mv - mv) > tolerance):
            return None
        ring = self.svc.ring
        start = ring.end
        deadline = time.monotonic() + 2.0
        while ring.end - start < ALIVE_CHECK_SAMPLES and time.monotonic() < deadline:
            time.sleep(0.05)
        _, cur, _ = ring.read(start, start + ALIVE_CHECK_SAMPLES)
        if len(cur) < ALIVE_CHECK_SAMPLES // 2:
            return None
        with np.errstate(all="ignore"):
            mean = float(np.nanmean(cur))
        return mean if mean > DUT_ALIVE_A else None

    def _restore(self, rec: SessionRecorder) -> None:
        cfg = rec.cfg
        if not (cfg.auto_restore and cfg.voltage_mv):
            return
        mean = self._dut_still_powered(rec)
        if mean is not None:
            mv = self.svc.ppk2.voltage_mv  # what the PPK2 really outputs (a battery session adjusts it next tick)
            self.svc.ppk2.adopt_source_state(mv, dut_power=True)
            rec.note("dut_kept", self.svc.ring.end, voltage_mv=mv, mean_a=mean)
            log.info("session %d: Board still powered (%.1f uA), not restoring", rec.session_id, mean * 1e6)
        else:
            self._power_up(rec, "dut_restore")

    def _handle_ppk2_resume(self, gap_start: int, gap_samples: int, reason: str) -> None:
        with self._lock:
            a = self.active
            if not a:
                return
            a.rec.note("gap", gap_start, gap_start + gap_samples, reason=reason)
            self._restore(a.rec)

    def _resume_after_restart(self, row: dict) -> None:
        cfg = SessionConfig.from_dict(row["config"])
        self._wait_for_stream()
        started_at = row["started_at"]
        s0 = self._stream_idx_of(started_at)
        end = self.svc.ring.end
        first = s0 + -(-(end - s0) // BIN) * BIN  # first bin boundary at or after the newest sample
        rec = self._recorder(row["id"], started_at, s0, first, cfg, row["charge_c"])
        prev_end = s0 + row["n_samples"]
        if first > prev_end:
            rec.note("gap", prev_end, first, reason="capture_restart")
        self.active = Active(row["id"], row["name"], row["kind"], started_at, cfg, rec)
        log.warning("session %d resumed after restart (%.1f s gap)", row["id"], (first - prev_end) / SAMPLE_RATE_HZ)
        self._restore(rec)

    # background loop ------------------------------------------------------------------------------------
    def _run(self) -> None:
        recovered = False
        while not self._stop_evt.is_set():
            if not self._device_loaded:
                self._load_device()
            if not recovered:
                recovered = self._try_recover()
            if time.monotonic() - self._last_spool_check > SPOOL_EVERY_S:
                self._last_spool_check = time.monotonic()
                self._replay_spool()
            with self._lock:
                if self.active:
                    try:
                        self._pending = merge_batches(self._pending, self.active.rec.tick())
                    except Exception:
                        log.exception("session %d: recorder tick failed", self.active.id)
                    self._flush()
                    self._retention()
                    self._battery_tick()
                    a = self.active
                    limit = a.cfg.max_duration_s if a else None
                    if limit and a.rec.processed_samples * SAMPLE_PERIOD_S >= limit:
                        log.info("session %d reached its max duration (%.0f s)", a.id, limit)
                        try:
                            self.stop_session()
                        except SessionError:
                            pass
            self._stop_evt.wait(TICK_S)

    def _load_device(self) -> None:
        """At startup: the device on the bench (voltage limit, UART speed), and saved settings (idle power-off, spike
        filter); the api sets them and keeps them in the database."""
        try:
            d = self.store.active_device()
            saved = self.store.saved_settings()
        except Exception:  # database not there yet, or the api hasn't created the tables yet: next tick
            return
        if saved:
            self.svc.apply_settings(saved, startup=True)
            self.apply_settings(saved)
            log.info("saved settings: %s", saved)
        self._device_loaded = True
        if d and self.svc.ppk2.device is None:  # the api may have pushed it already
            self.svc.set_device(dict(d))
            log.info("device '%s' (limit %d mV, UART %s baud)", d["name"], d["max_mv"], d.get("uart_baud"))

    def _try_recover(self) -> bool:
        """At startup: continue a session that was running when the service stopped."""
        try:
            row = self.store.running_session()
        except StoreUnavailable as e:
            self.db_ok, self.last_error = False, str(e)
            return False
        if row is None:
            return True
        if not self.svc.ppk2.connected:
            return False  # try again once the PPK2 is there
        try:
            with self._lock:
                self._resume_after_restart(row)
        except Exception as e:
            log.exception("could not resume session %d", row["id"])
            self.last_error = f"resume failed: {e}"
            try:
                self.store.finish_session(row["id"], "interrupted", datetime.now(timezone.utc))
            except StoreUnavailable:
                return False
        return True

    def _flush(self) -> bool:
        a = self.active
        if a is None:
            return True
        p = self._pending
        if not (p["rollups"] or p["uart"] or p["events"] or p["segments"] or p["progress"]):
            return True
        try:
            self.store.write_batch(a.id, p)
            self._pending = new_batch()
            self.db_ok, self.last_error = True, None
            return True
        except StoreUnavailable as e:
            self.db_ok, self.last_error = False, str(e)
            over = len(p["rollups"]) - MAX_PENDING_ROLLUPS
            if over > 0:
                del p["rollups"][:over]
                log.error("session %d: database unreachable too long, dropped %d summaries", a.id, over)
            return False

    # spool: recorder output that couldn't reach the database before shutdown/stop --------------------------
    def _spool(self, session_id: int, batch: dict) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        path = self.spool_dir / f"session-{session_id}-{time.time_ns()}.pkl"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps((session_id, batch)))
        tmp.rename(path)
        log.warning("session %d: database unreachable, %d summaries saved to %s", session_id,
                    len(batch["rollups"]), path.name)

    def _replay_spool(self) -> None:
        if not self.spool_dir.exists():
            return
        for path in sorted(self.spool_dir.glob("session-*.pkl")):
            session_id, batch = pickle.loads(path.read_bytes())
            try:
                self.store.write_batch(session_id, batch)
            except StoreUnavailable:
                return
            path.unlink()
            log.info("session %d: replayed %s into the database", session_id, path.name)

    def _battery_tick(self) -> None:
        """Battery emulation: move the output voltage along the discharge curve; stop at the end."""
        a = self.active
        bat = battery.BatteryConfig.from_dict(a.cfg.battery) if a else None
        if not a or not bat:
            return
        st = battery.state(bat, a.rec.charge_c, a.rec.processed_samples * SAMPLE_PERIOD_S)
        idx = self.svc.ring.end
        if st.done:
            reason = "cutoff" if bat.on_cutoff == "protect" else "floor"
            a.rec.note("battery_end", idx, voltage_mv=st.voltage_mv, soc=st.soc, reason=reason)
            log.info("session %d: emulated battery reached the %s (%d mV); board off", a.id, reason, st.voltage_mv)
            try:
                self.stop_session(dut_off=True)
            except SessionError:
                pass
            return
        ppk2 = self.svc.ppk2
        if (st.voltage_mv == ppk2.voltage_mv or not ppk2.dut_power
                or time.monotonic() - a.bat_set_at < BATTERY_SET_EVERY_S):
            return
        try:
            ppk2.adjust_voltage(st.voltage_mv)
        except DeviceError as e:
            log.warning("session %d: battery voltage change failed: %s", a.id, e)
            return
        a.bat_set_at = time.monotonic()
        if a.bat_logged_mv is None or abs(a.bat_logged_mv - st.voltage_mv) >= BATTERY_LOG_MV:
            a.bat_logged_mv = st.voltage_mv
            a.rec.note("battery", idx, voltage_mv=st.voltage_mv, soc=st.soc, below_cutoff=st.below_cutoff)

    def battery_status(self) -> dict | None:
        a = self.active
        bat = battery.BatteryConfig.from_dict(a.cfg.battery) if a else None
        if not a or not bat:
            return None
        st = battery.state(bat, a.rec.charge_c, a.rec.processed_samples * SAMPLE_PERIOD_S)
        return {**bat.to_dict(), "voltage_mv": st.voltage_mv, "soc": st.soc, "eta_s": st.eta_s,
                "below_cutoff": st.below_cutoff}

    def _retention(self) -> None:
        a = self.active
        if not a or a.cfg.raw_buffer_hours <= 0 or time.monotonic() - self._last_retention < RETENTION_EVERY_S:
            return
        self._last_retention = time.monotonic()
        cutoff = a.rec.processed_samples - int(a.cfg.raw_buffer_hours * 3600 * SAMPLE_RATE_HZ)
        try:
            old = self.store.buffer_segments_before(a.id, cutoff)
            for seg in old:
                (self.data_dir / seg["path"]).unlink(missing_ok=True)
            if old:
                self.store.delete_segments([s["id"] for s in old])
        except StoreUnavailable:
            pass

    # status ----------------------------------------------------------------------------------------------
    def status(self) -> dict:
        a = self.active
        if not a:
            return {"active": False, "db_ok": self.db_ok, "error": self.last_error}
        r = a.rec
        return {
            "active": True,
            "db_ok": self.db_ok,
            "error": self.last_error,
            "session": {
                "id": a.id, "name": a.name, "kind": a.kind, "started_at": a.started_at.isoformat(),
                "config": a.cfg.to_dict(),
                "s0": r.s0,  # stream index of session sample 0 (to read the live buffer)
                "processed_samples": r.processed_samples,
                "duration_s": r.processed_samples * SAMPLE_PERIOD_S,
                "lag_s": (self.svc.ring.end - r.cursor) * SAMPLE_PERIOD_S,
                "charge_c": r.charge_c,
                "awake": r.awake,
                **r.state.__dict__,
                "pending_rows": len(self._pending["rollups"]),
                "battery": self.battery_status(),
            },
        }
