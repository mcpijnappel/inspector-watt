"""Hardware control by agents (MCP): who asked, who may, for how long.

An agent session (one MCP connection, known by a hash of its Mcp-Session-Id) asks the first time it wants to touch
the hardware. A person answers in the web UI: allow for this session, or deny. An allow lasts until that MCP session
is gone (a new Claude Code session asks again) and at most MAX_GRANT_S; it can be revoked any time. Kept in memory:
a restart of the api forgets everything, so every session asks again.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

MAX_GRANT_S = 8 * 3600
ASK_WAIT_S = 120.0  # an unanswered request expires; the agent's tool call gives up then
DENY_HOLD_S = 300.0  # after a deny, the same session gets "denied" right away for this long
KEEP_REQUESTS = 50


@dataclass
class Request:
    id: int
    session: str  # hash of the MCP session id
    client: str
    host: str
    via: str  # "local" | "lan"
    tool: str
    args: dict
    at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | allowed | denied | expired
    answered_at: float | None = None

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "session", "client", "host", "via", "tool", "args", "at", "status", "answered_at")} | {
            "expires_at": self.at + ASK_WAIT_S if self.status == "pending" else None}


@dataclass
class Grant:
    session: str
    client: str
    host: str
    via: str
    since: float
    until: float
    actions: int = 0
    last_action: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class AgentAccess:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._ids = itertools.count(1)
        self.requests: list[Request] = []
        self.grants: dict[str, Grant] = {}
        self.denied: dict[str, float] = {}  # session -> until

    def _tidy(self) -> None:
        now = self.clock()
        for r in self.requests:
            if r.status == "pending" and now - r.at > ASK_WAIT_S:
                r.status = "expired"
        for s, g in list(self.grants.items()):
            if g.until <= now:
                del self.grants[s]
        for s, until in list(self.denied.items()):
            if until <= now:
                del self.denied[s]
        if len(self.requests) > KEEP_REQUESTS:
            self.requests = self.requests[-KEEP_REQUESTS:]

    def ask(self, session: str, client: str, host: str, via: str, tool: str, args: dict) -> dict:
        """A hardware call from an agent session: allowed now, denied, or a request waiting for a person."""
        self._tidy()
        g = self.grants.get(session)
        if g:
            g.actions += 1
            g.last_action = tool
            return {"status": "allowed", "until": g.until}
        if session in self.denied:
            return {"status": "denied", "until": self.denied[session]}
        # One open request per session: a second call while it waits joins it.
        for r in self.requests:
            if r.session == session and r.status == "pending":
                return {"status": "pending", "request": r.to_dict()}
        r = Request(next(self._ids), session, client[:100], host[:100], via, tool[:60], args, at=self.clock())
        self.requests.append(r)
        return {"status": "pending", "request": r.to_dict()}

    def request(self, rid: int) -> dict | None:
        self._tidy()
        r = next((r for r in self.requests if r.id == rid), None)
        if r is None:
            return None
        d = r.to_dict()
        if r.status == "allowed":
            g = self.grants.get(r.session)
            d["until"] = g.until if g else None
        return d

    def answer(self, rid: int, allow: bool) -> dict:
        self._tidy()
        r = next((r for r in self.requests if r.id == rid), None)
        if r is None:
            raise KeyError(rid)
        if r.status != "pending":
            raise ValueError(f"this request is already {r.status}")
        now = self.clock()
        r.status = "allowed" if allow else "denied"
        r.answered_at = now
        if allow:
            self.grants[r.session] = Grant(r.session, r.client, r.host, r.via, now, now + MAX_GRANT_S, actions=1, last_action=r.tool)
            self.denied.pop(r.session, None)
        else:
            self.denied[r.session] = now + DENY_HOLD_S
        return r.to_dict()

    def revoke(self, session: str | None = None) -> int:
        """End one session's allow, or all of them (session None)."""
        if session is None:
            n = len(self.grants)
            self.grants.clear()
            for r in self.requests:
                if r.status == "pending":
                    r.status = "expired"
            return n
        return 1 if self.grants.pop(session, None) else 0

    def state(self) -> dict:
        self._tidy()
        return {"pending": [r.to_dict() for r in self.requests if r.status == "pending"],
                "grants": [g.to_dict() for g in self.grants.values()],
                "max_grant_s": MAX_GRANT_S, "ask_wait_s": ASK_WAIT_S}
