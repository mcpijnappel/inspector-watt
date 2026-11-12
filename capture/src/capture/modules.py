"""Events from modules (modules/<name>/, e.g. the logic analyzer's decoded bus transactions).

A module sends its events with the computer's clock time (unix seconds): it runs in its own container on the same
machine, so that's the same clock. Here they get the PPK2 stream index (the timeline every chart and recording
uses). During a recording they're stored with it (events.module = "analyzer:1"); the latest ones are also kept in
memory for the Live page, recording or not.
"""

from __future__ import annotations

import threading
import time
from collections import deque

LIVE_KEEP = 5000  # module events kept in memory for Live


class ModuleEvents:
    def __init__(self, clock, sessions=None):
        self.clock = clock  # ClockSync: host monotonic time -> stream index
        self.sessions = sessions  # SessionManager or None (no database)
        self._live: deque[dict] = deque(maxlen=LIVE_KEEP)
        self._seq = 0
        self._lock = threading.Lock()

    def index_at_wall(self, t: float) -> int | None:
        """Stream index of a unix time (on this machine's clock), or None while the PPK2 clock isn't known."""
        return self.clock.index_at(time.monotonic() - (time.time() - t))

    def add(self, module: str, items: list[dict], to_live: bool = True, record: bool = True) -> dict:
        """items: [{kind, t, end_t?, data?}] -> stored in the running recording (if any; record) and kept for Live
        (to_live). The analyzer's live decoding is Live only; what it decodes for a recording goes only into the
        recording while live decoding fills the Live list (else both would show it twice)."""
        live = recorded = skipped = 0
        for it in items:
            start = self.index_at_wall(it["t"])
            if start is None:
                skipped += 1
                continue
            end = self.index_at_wall(it["end_t"]) if it.get("end_t") is not None else None
            data = it.get("data") or {}
            if to_live:
                with self._lock:
                    self._seq += 1
                    self._live.append({"seq": self._seq, "module": module, "kind": it["kind"], "start_idx": start,
                                       "end_idx": end, "data": data})
                live += 1
            if record and self.sessions and self.sessions.note_event(it["kind"], start, end, module=module, **data):
                recorded += 1
        return {"live": live, "recorded": recorded, "skipped": skipped}

    def live(self, after_seq: int = 0, module: str | None = None, since_idx: int | None = None, limit: int = 2000) -> list[dict]:
        with self._lock:
            out = [e for e in self._live if e["seq"] > after_seq and (module is None or e["module"].split(":")[0] == module)
                   and (since_idx is None or (e["end_idx"] or e["start_idx"]) >= since_idx)]
        return out[-limit:]
