"""Decoding during a recording, by itself: around each wake, or all of it (the "Decode in recordings" setting).

Capture finds the wakes (wake threshold, hold time). Once a second this asks the api for the running recording and
the wakes it found since the last answer, with their unix times (GET /api/modules/analyzer/recording), so nothing
has to call into the module. A wake's window runs from "before each wake" (pre_ms) ahead of its start to "after each
wake" (post_ms) past its end; it's decoded from the buffer once that has passed, and its messages go to the api like
"Decode last 30 s" does (capture stores them with the recording). A wake still going on is decoded in pieces as it
goes, and so is "all of it", so nothing falls out of the buffer first.

Pieces: [a, b) is decoded with OVERLAP_S more on each side and keeps the messages that start inside it, so a message
across a cut is neither lost nor kept twice. Each decoded stretch is also sent as a "bus_window" event (how many
messages; or `missed` and why: the analyzer wasn't streaming, the data had left the buffer), so the session can tell
"no traffic" from "not decoded".

Live decoding (the Live page's toggle, a lease the page renews): the newest second, every second, onto the Live
list only. While it's on, what's decoded for a recording goes only into the recording, so the Live list doesn't
show it twice.

When the module starts while a recording runs, it starts from that moment: it can't know what it decoded before
it restarted, and decoding that again would store it twice.
"""

from __future__ import annotations

import logging
import math

import httpx

from . import decode as dec

log = logging.getLogger("analyzer.follow")

POLL_S = 1.0
OVERLAP_S = 0.05  # longer than any I2C / SPI message
PIECE_S = 4.0  # longest stretch decoded at once (less at high sample rates: MAX_PIECE_SAMPLES)
MAX_PIECE_SAMPLES = 32_000_000
MARGIN_S = 0.3  # stay this far behind the newest sample: it arrives in USB chunks
LIVE_FIRST_S = 1.0  # live decoding starts this far back
LIVE_MIN_S = 0.2  # and decodes at least this much at a time
LIVE_MAX_BEHIND_S = 5.0  # a bus too busy to keep up with: skip ahead, it's only for looking


class Follower:
    def __init__(self, analyzer, get=None):
        self.a = analyzer
        self.get = get or httpx.get
        self.poll_s = POLL_S
        self.session: int | None = None  # the recording being followed
        self.after_seq = 0
        self.fresh = True  # nothing seen since the module started
        self.windows: list[list[float]] = []  # wake windows still to decode: [start, end], unix times, in order
        self.done_t: float | None = None  # decoded up to here
        self.finished = False  # the recording stopped and everything up to its end is done
        self.error: str | None = None
        self.counts = {"windows": 0, "messages": 0, "missed": 0, "quiet": 0}
        self.live_done: float | None = None  # live decoding: done up to here

    # what the api says ----------------------------------------------------------------------------------------
    def fetch(self) -> dict | None:
        try:
            r = self.get(f"{self.a.api_url}/api/modules/analyzer/recording", params={"after_seq": self.after_seq},
                         timeout=5)
            r.raise_for_status()
            self.error = None
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            self.error = f"can't ask the api about recordings: {e}"
            return None

    def _start(self, rec: dict, joined: bool) -> None:
        """joined: the recording was running before the module started: from now on (see the module docstring)."""
        self.session, self.windows, self.finished = rec["id"], [], False
        self.counts = {"windows": 0, "messages": 0, "missed": 0, "quiet": 0}
        self.done_t = rec["processed_t"] if joined else None
        log.info("following recording %s%s", rec["id"], " (from now on: the module started during it)" if joined else "")

    def step(self, feed: dict | None) -> None:
        if feed is None:
            return
        rec = feed.get("recording")
        mode = self.a.values.get("record", "wakes")
        first, self.fresh = self.fresh, False
        if rec is None or mode == "off" or not self.a.values.get("enabled", True):
            if self.session is not None and rec is None:
                log.info("recording %s is over", self.session)
                self.session = None
            self.after_seq = feed.get("last_seq", self.after_seq)
            return
        if rec["id"] != self.session:
            self._start(rec, joined=first)
        self.after_seq = feed.get("last_seq", self.after_seq)
        if self.finished:
            return
        pre, post = self.a.values["pre_ms"] / 1000, self.a.values["post_ms"] / 1000
        stop = rec.get("stopped_t")
        for w in feed.get("wakes", []):
            self._add([w["start_t"] - pre, w["end_t"] + post])
        windows = list(self.windows)
        if mode == "all":
            windows = [[rec["started_t"], math.inf]]
        elif feed.get("open_start_t") is not None:
            windows.append([feed["open_start_t"] - pre, math.inf])
        if stop is not None:
            windows = [[a, min(b, stop)] for a, b in windows if a < stop]
        self._decode(windows, rec["processed_t"] if stop is None else math.inf)
        if stop is not None and all(self.done_t is not None and b <= self.done_t for _, b in windows):
            self.finished = True
            log.info("recording %s: done (%s)", self.session, self.brief())

    def _add(self, w: list[float]) -> None:
        if self.windows and w[0] <= self.windows[-1][1]:
            self.windows[-1][1] = max(self.windows[-1][1], w[1])
        else:
            self.windows.append(w)

    # decoding -------------------------------------------------------------------------------------------------
    def _decode(self, windows: list[list[float]], known_until: float) -> None:
        """Decode what's due of each window: all of a window that has passed, a running one in whole pieces."""
        buf = self.a.buffered()
        for a, b in windows:
            start = a if self.done_t is None else max(a, self.done_t)
            if start >= b:
                continue
            if buf is None:  # the analyzer isn't streaming: what has passed is missed (a running window in pieces)
                upto = min(b, known_until)
                if not math.isinf(upto) and upto - start >= (PIECE_S if upto < b else 0) and upto > start:
                    self._missed(start, upto, self.a.error or "the analyzer wasn't streaming")
                break
            oldest, newest = buf
            ready = newest - MARGIN_S - OVERLAP_S
            if start < oldest:  # it left the buffer before we got to it
                cut = min(b, oldest)
                self._missed(start, cut, "the data had left the analyzer's buffer (decoding fell behind)")
                start = cut
                if start >= b:
                    continue
            end = min(b, ready)
            piece = min(PIECE_S, MAX_PIECE_SAMPLES / max(1, self.a.streamer.rate))
            while end - start >= piece or (end == b and end > start):
                p1 = min(start + piece, end)
                if not self._piece(start, p1):
                    return
                start = p1
            if end < b:
                break  # the rest isn't there yet: later windows can't be either
        self.windows = [w for w in self.windows if self.done_t is None or w[1] > self.done_t]

    def _piece(self, p0: float, p1: float) -> bool:
        try:
            r = self.a.decode_span(p0 - OVERLAP_S, p1 + OVERLAP_S, skip_quiet=True)
        except dec.DecodeError as e:
            self._missed(p0, p1, str(e))
            return False
        items = [i for i in r["items"] if p0 <= i["t"] < p1]
        win = {"kind": "bus_window", "t": p0, "end_t": p1,
               "data": {"label": f"bus: {len(items)} messages", "messages": len(items),
                        **({"truncated": True} if r["truncated"] else {}), **({"quiet": True} if r.get("quiet") else {})}}
        if self.a.post_events([*items, win], live=not self.a.live_on()) is None:
            return False  # the api isn't there: try again next time
        self.done_t = p1
        self.counts["windows"] += 1
        self.counts["messages"] += len(items)
        self.counts["quiet"] += 1 if r.get("quiet") else 0
        return True

    def _missed(self, p0: float, p1: float, why: str) -> None:
        log.warning("recording %s: %.2f s not decoded: %s", self.session, p1 - p0, why)
        if self.a.post_events([{"kind": "bus_window", "t": p0, "end_t": p1,
                                "data": {"label": f"bus not decoded: {why}", "messages": 0, "missed": why}}],
                              live=not self.a.live_on()) is None:
            return
        self.done_t = p1
        self.counts["missed"] += 1

    # live decoding ----------------------------------------------------------------------------------------------
    def live_step(self) -> None:
        """While the Live page asks for it: decode what arrived since the last time, onto the Live list only."""
        if not self.a.live_on():
            self.live_done = None
            return
        buf = self.a.buffered()
        if buf is None:
            return
        oldest, newest = buf
        ready = newest - MARGIN_S - OVERLAP_S
        start = self.live_done if self.live_done is not None else ready - LIVE_FIRST_S
        start = max(start, oldest, ready - LIVE_MAX_BEHIND_S)
        if ready - start < LIVE_MIN_S:
            return
        p1 = min(ready, start + min(PIECE_S, MAX_PIECE_SAMPLES / max(1, self.a.streamer.rate)))
        try:
            r = self.a.decode_span(start - OVERLAP_S, p1 + OVERLAP_S, skip_quiet=True)
        except dec.DecodeError:
            return
        items = [i for i in r["items"] if start <= i["t"] < p1]
        if items and self.a.post_events(items, live=True, record=False) is None:
            return
        self.live_done = p1

    # what it tells the tracker --------------------------------------------------------------------------------
    def brief(self) -> str:
        if self.session is None:
            return ""
        c = self.counts
        return (f"recording {self.session}: {c['windows']} stretches decoded, {c['messages']} messages"
                + (f", {c['missed']} missed" if c["missed"] else ""))

    def status(self) -> dict:
        return {"session": self.session, "mode": self.a.values.get("record", "wakes"), "done_t": self.done_t,
                "pending": len(self.windows), "finished": self.finished, "error": self.error, **self.counts}
