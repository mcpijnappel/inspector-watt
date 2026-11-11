"""Hardware control by agents: who is asking, whether they may, and a log of what they did.

Three locks, all needed:
1. Settings → Agents → "Hardware control" (off by default). Off: these tools aren't listed and refuse to run.
2. A person allows each agent session once, in the web UI (a banner). The allow belongs to that MCP session
   (a hash of its Mcp-Session-Id): a new Claude Code session asks again. It also ends after a few hours.
3. Each tool carries _meta["anthropic/requiresUserInteraction"], so Claude Code asks the user on every call, in
   every permission mode (auto and bypassPermissions too) and never offers "don't ask again".
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations

from .client import Api, ApiError

# Claude Code: a permission prompt on every call, whatever the mode (see the module docstring).
HW_META = {"anthropic/requiresUserInteraction": True}
HW_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
POLL_S = 1.0
WAIT_S = 125.0  # a bit longer than the api keeps a request open
SETTING_CACHE_S = 2.0


@dataclass
class Who:
    session: str
    client: str
    host: str
    via: str

    def body(self) -> dict:
        return {"session": self.session, "client": self.client, "host": self.host}


class Hardware:
    def __init__(self, api: Api):
        self.api = api
        self._cached: tuple[float, bool] | None = None

    async def enabled(self) -> bool:
        """Settings → Agents → Hardware control (cached briefly; unreachable api = off)."""
        now = time.monotonic()
        if self._cached is None or now - self._cached[0] > SETTING_CACHE_S:
            try:
                on = bool((await self.api.get("/api/mcp/auth")).get("hardware"))
            except ApiError:
                on = False
            self._cached = (now, on)
        return self._cached[1]

    def client_name(self, ctx: Context | None) -> str | None:
        """The MCP client's name and version (e.g. "claude-code 2.1"), for what it saves."""
        try:
            info = ctx.request_context.session.client_params.clientInfo
            return f"{info.name} {info.version}".strip()
        except AttributeError:
            return None

    def who(self, ctx: Context | None) -> Who:
        rc = getattr(ctx, "request_context", None) if ctx else None
        req = getattr(rc, "request", None)
        headers = getattr(req, "headers", None) or {}
        sid = headers.get("mcp-session-id")
        if not sid:
            raise ApiError("hardware control needs an MCP session (this connection has no Mcp-Session-Id)")
        info = None
        try:
            info = rc.session.client_params.clientInfo
        except AttributeError:
            pass
        client = f"{info.name} {info.version}".strip() if info else "unknown client"
        token = headers.get("x-pst-token-name")  # set by the gate for LAN requests: which named token
        if token:
            client = f"{client} (token: {token})"
        via_lan = bool(headers.get("x-pst-via"))
        # Behind a reverse proxy the first X-Forwarded-For address is the agent's machine (a label only: it can be faked).
        fwd = (headers.get("x-forwarded-for") or "").split(",")[0].strip()
        host = (fwd or headers.get("x-real-ip") or "the network") if via_lan else "this machine"
        return Who(hashlib.sha256(sid.encode()).hexdigest()[:32], client, host, "lan" if via_lan else "local")

    async def authorize(self, ctx: Context | None, tool: str, args: dict) -> Who:
        if not await self.enabled():
            raise ApiError("Hardware control by agents is off. The user can switch it on in the tracker: Settings → Agents.")
        who = self.who(ctx)
        r = await self.api.request("POST", "/api/agents/requests", json={**who.body(), "via": who.via, "tool": tool, "args": args})
        if r["status"] == "pending":
            rid = r["request"]["id"]
            if ctx:
                await ctx.info("Waiting for the user to allow hardware control in the tracker's web UI (a banner at the top).")
            deadline = time.monotonic() + WAIT_S
            while r["status"] == "pending" and time.monotonic() < deadline:
                await asyncio.sleep(POLL_S)
                r = await self.api.get(f"/api/agents/requests/{rid}")
        if r["status"] == "allowed":
            return who
        await self.log(who, tool, args, "denied", r["status"])
        if r["status"] == "denied":
            raise ApiError("The user denied hardware control for this session in the tracker. Ask them before trying again.")
        raise ApiError("Nobody answered the hardware-control request in the tracker's web UI within 2 minutes. "
                       "Ask the user to open the tracker and allow it, then try again.")

    async def log(self, who: Who, tool: str, args: dict, result: str, detail: str | None = None) -> None:
        try:
            await self.api.request("POST", "/api/agents/actions",
                                   json={**who.body(), "tool": tool, "args": args, "result": result, "detail": (detail or "")[:2000] or None})
        except ApiError:
            pass  # the log must never stop an action that already happened

    async def run(self, ctx: Context | None, tool: str, args: dict, action):
        """Authorize, run `action()` (a coroutine function), log the outcome."""
        who = await self.authorize(ctx, tool, args)
        try:
            out = await action()
        except ApiError as e:
            await self.log(who, tool, args, "error", str(e))
            raise
        await self.log(who, tool, args, "ok")
        return out
