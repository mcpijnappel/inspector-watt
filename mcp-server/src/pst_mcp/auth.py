"""Who may use the MCP endpoint.

Directly on the local port (bound to 127.0.0.1 by compose): anyone on this machine. Through the web server
(LAN; nginx adds X-PST-Via: web): only when LAN access is on, with `Authorization: Bearer <token>` for one of the
named tokens. All set in the web UI (Settings → Agents) and read from the api, cached for a few seconds.
The matching token's name goes to the tools as the header X-PST-Token-Name (the hardware banner and log show it)."""

from __future__ import annotations

import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable

Access = dict  # {"lan": bool, "tokens": [{"name", "token"}]} (older: "token": str)
log = logging.getLogger("pst_mcp.auth")


class TokenGate:
    CACHE_S = 5.0

    def __init__(self, app, access: Callable[[], Awaitable[Access]], health: Callable[[], Awaitable[dict]] | None = None):
        self.app = app
        self.access = access
        self.health = health
        self._cached: tuple[float, Access] | None = None

    async def current(self) -> Access:
        now = time.monotonic()
        if self._cached is None or now - self._cached[0] > self.CACHE_S:
            try:
                self._cached = (now, await self.access())
            except Exception:  # api unreachable: keep the last known state, or refuse the LAN
                if self._cached is None:
                    return {"lan": False, "tokens": []}
        return self._cached[1]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        via_lan = bool(headers.get("x-pst-via"))
        # Only this gate may say which token was used: drop a header a client sent itself.
        scope = {**scope, "headers": [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"x-pst-token-name"]}
        if scope.get("path") == "/health" and self.health and not via_lan:
            return await reply(send, 200, await self.health())
        if via_lan:
            a = await self.current()
            tokens = [t for t in a.get("tokens") or [] if t.get("token")]
            if not tokens and a.get("token"):
                tokens = [{"name": "default", "token": a["token"]}]
            if not a.get("lan") or not tokens:
                return await reply(send, 403, {"error": "MCP over the network is off (Settings → Agents in the web UI)"})
            auth = headers.get("authorization", "")
            given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            match = None
            for t in tokens:  # compare with every token (no early exit), so timing doesn't tell which was close
                if hmac.compare_digest(given.encode(), t["token"].encode()) and given:
                    match = t
            if match is None:
                # Say which, so a proxy that drops the header can be told from an old token (never log the token).
                why = ("no Authorization header arrived (a proxy on the way may drop it)" if not auth
                       else "the Authorization header isn't 'Bearer <token>'" if not given
                       else "wrong token (it isn't one of the tokens in Settings → Agents)")
                who = (headers.get("x-forwarded-for") or "").split(",")[0].strip() or headers.get("x-real-ip", "?")
                log.warning("LAN MCP request refused from %s: %s", who, why)
                return await reply(send, 401, {"error": f"{why}. Use: Authorization: Bearer <token from Settings → Agents>"})
            scope["headers"] = [*scope["headers"], (b"x-pst-token-name", match["name"].encode("utf-8", "replace")[:80])]
        return await self.app(scope, receive, send)


async def reply(send, status: int, data: dict):
    body = json.dumps(data).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
