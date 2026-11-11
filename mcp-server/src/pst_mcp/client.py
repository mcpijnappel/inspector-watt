"""The api, as the MCP tools may use it: everything readable, phase-rule profiles writable; hardware only through
`hardware()`, a fixed list of calls the hardware tools make after hardware.py allowed them.

The capture service (power, voltage, recording) is behind the api's /api/device/* proxy. Never: flashing, the
upload token, answering or revoking an agent's own hardware-control request."""

from __future__ import annotations

import os
import re

import httpx


class ApiError(Exception):
    pass


# Writes the MCP server may do: phase-rule profiles and their tests, and linking a session to a profile.
WRITABLE = ("/api/profiles", "/api/sessions/")
WRITE_SUFFIXES = ("/phases/test", "/types/test")


# The hardware tools' calls (only via Api.hardware, after the user allowed the agent session).
HARDWARE = {
    ("POST", "/api/device/ppk2/source"), ("POST", "/api/device/ppk2/dut"), ("POST", "/api/device/ppk2/stream"),
    ("POST", "/api/sessions"), ("POST", "/api/sessions/stop"), ("POST", "/api/device/sessions/save-buffer"),
    ("POST", "/api/device/usb/open"), ("POST", "/api/device/usb/key"), ("POST", "/api/device/usb/send"),
}


def allowed(method: str, path: str) -> bool:
    if path.startswith("/api/flash"):
        # the upload list and a job's log; never flashing or the upload token
        return method == "GET" and bool(re.fullmatch(r"/api/flash/(jobs|\d+)", path))
    if path.startswith("/api/agents"):
        # ask for hardware control, see the answer, log actions; never answer or revoke
        return (method == "POST" and path in ("/api/agents/requests", "/api/agents/actions")) or \
            (method == "GET" and bool(re.fullmatch(r"/api/agents/requests/\d+", path)))
    if path.startswith("/api/mcp"):
        return method == "GET"  # never add, renew or delete tokens, or switch LAN / hardware control
    if method == "GET":
        return True
    if path.startswith("/api/device/") or path in ("/api/sessions", "/api/sessions/stop") or path.startswith("/api/devices"):
        return False
    if path.startswith("/api/profiles"):
        return method in ("POST", "PUT")
    if path.startswith("/api/sessions/"):
        # tests of rules on a session, or PATCH {profile_id} to link one
        return (method == "POST" and path.endswith(WRITE_SUFFIXES)) or method == "PATCH"
    return False


class Api:
    def __init__(self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        # X-PST-Client: the api refuses some things to agents whatever this allow list says (the voltage limit).
        self.http = httpx.AsyncClient(base_url=base_url or os.environ.get("API_URL", "http://api:8000"),
                                      timeout=httpx.Timeout(180.0, connect=5.0), transport=transport,
                                      headers={"X-PST-Client": "mcp"})

    async def request(self, method: str, path: str, params: dict | None = None, json: object = None):
        if not allowed(method, path):
            raise ApiError(f"not allowed from MCP: {method} {path}")
        return await self._send(method, path, params, json)

    async def _send(self, method: str, path: str, params: dict | None, json: object):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            r = await self.http.request(method, path, params=params, json=json)
        except httpx.HTTPError as e:
            raise ApiError(f"api unreachable: {e}") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text
            raise ApiError(f"{r.status_code}: {detail}")
        return r.json()

    async def get(self, path: str, **params):
        return await self.request("GET", path, params=params)

    async def hardware(self, method: str, path: str, json: object = None):
        """A hardware call; only the fixed list above, and only from the hardware tools after they're allowed."""
        if (method, path) not in HARDWARE:
            raise ApiError(f"not a hardware call: {method} {path}")
        return await self._send(method, path, None, json)
