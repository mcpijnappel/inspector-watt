"""Modules (modules/<name>/ in the repo): optional extras with their own containers, like the logic analyzer.

Each module's main container answers GET /module with what it is: its settings (the web UI draws the form from
them), the event types it makes and how to draw them, its MCP tools and its health. Everything else goes through
here, so the same gates apply as for the rest of the API:

- GET  /api/modules                         the known modules: installed or not, health, settings and their values
- GET  /api/modules/<name>/settings         saved values (the module reads them when it starts)
- PUT  /api/modules/<name>/settings         change, check against the module's description, save, pass on
- POST /api/modules/<name>/events           from the module only (nginx refuses it from outside): onto the timeline
- GET  /api/modules/<name>/events/live      its latest events (Live page), recording or not
- GET  /api/modules/<name>/recording        the running recording and its wakes (unix times), for a module that
                                            works around wakes (the analyzer decodes the bus there)
- *    /api/modules/<name>/<path>             passed on to the module

Only our own modules (listed in KNOWN) for now; installed means its container answers.
"""

from __future__ import annotations

import os
import time

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

KNOWN = {"analyzer": os.environ.get("MODULE_ANALYZER_URL", "http://analyzer:8400")}
TITLES = {"analyzer": "Logic analyzer"}  # shown before the module is installed (then it says its own title)
DESCRIBE_TTL_S = 5.0
MISSING_TTL_S = 30.0  # not installed: look again less often (a missing container can take a while to fail)
SETTING_TYPES = ("choice", "number", "bool", "text")


class EventsIn(BaseModel):
    instance: str = Field("1", min_length=1, max_length=20, pattern=r"^[a-z0-9_-]+$")
    items: list[dict] = Field(max_length=2000)
    live: bool = True  # into the Live list
    record: bool = True  # into the running recording


def check_settings(described: list[dict], values: dict) -> dict:
    """Values for a module's settings, checked against its own description. Unknown keys are refused."""
    by_key = {s["key"]: s for s in described}
    out = {}
    for key, v in values.items():
        s = by_key.get(key)
        if s is None:
            raise HTTPException(422, f"unknown setting {key!r}")
        kind = s.get("type")
        if kind == "choice":
            if v not in s.get("choices", []):
                raise HTTPException(422, f"{key}: one of {s.get('choices')}")
        elif kind == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise HTTPException(422, f"{key}: a number")
            if ("min" in s and v < s["min"]) or ("max" in s and v > s["max"]):
                raise HTTPException(422, f"{key}: {s.get('min', '…')} to {s.get('max', '…')}")
        elif kind == "bool":
            if not isinstance(v, bool):
                raise HTTPException(422, f"{key}: true or false")
        elif kind == "text":
            if not isinstance(v, str) or len(v) > s.get("max_length", 200):
                raise HTTPException(422, f"{key}: text, at most {s.get('max_length', 200)} characters")
        else:
            raise HTTPException(500, f"{key}: the module describes an unknown setting type {kind!r}")
        out[key] = v
    return out


def setup(app: FastAPI, capture: httpx.AsyncClient, get_setting, save_setting) -> None:
    """Add the module routes to the api (get_setting / save_setting: the settings table)."""
    client = httpx.AsyncClient(timeout=10)
    cache: dict[str, tuple[float, dict | None, str | None]] = {}

    async def describe(name: str, fresh: bool = False) -> tuple[dict | None, str | None]:
        """The module's description, or None and why not (not installed: its container doesn't answer)."""
        hit = cache.get(name)
        if hit and not fresh and time.monotonic() - hit[0] < (DESCRIBE_TTL_S if hit[1] else MISSING_TTL_S):
            return hit[1], hit[2]
        try:
            r = await client.get(f"{KNOWN[name]}/module", timeout=httpx.Timeout(3.0, connect=1.0))
            r.raise_for_status()
            d, err = r.json(), None
        except (httpx.ConnectError, httpx.ConnectTimeout):
            d, err = None, "not installed"  # its container isn't there (the name doesn't resolve) or not up
        except (httpx.HTTPError, ValueError) as e:
            d, err = None, f"not answering: {type(e).__name__} {e}".strip()
        cache[name] = (time.monotonic(), d, err)
        return d, err

    def known(name: str) -> str:
        if name not in KNOWN:
            raise HTTPException(404, f"no module {name!r}")
        return KNOWN[name]

    async def values(name: str, d: dict | None) -> dict:
        saved = await get_setting(f"module.{name}") or {}
        defaults = {s["key"]: s.get("default") for s in (d or {}).get("settings", [])}
        return {**defaults, **saved}

    @app.get("/api/modules")
    async def list_modules():
        out = []
        for name in KNOWN:
            d, err = await describe(name)
            out.append({"name": name, "title": (d or {}).get("title") or TITLES.get(name, name),
                        "installed": d is not None, "error": err, "module": d,
                        "values": await values(name, d)})
        return out

    @app.get("/api/modules/{name}/settings")
    async def get_module_settings(name: str):
        known(name)
        d, _ = await describe(name)
        return await values(name, d)

    @app.put("/api/modules/{name}/settings")
    async def put_module_settings(name: str, request: Request):
        url = known(name)
        d, err = await describe(name, fresh=True)
        if d is None:
            raise HTTPException(409, f"module {name} isn't running ({err})")
        change = check_settings(d.get("settings", []), await request.json())
        merged = {**(await get_setting(f"module.{name}") or {}), **change}
        try:
            r = await client.put(f"{url}/settings", json=merged)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"module {name} unreachable: {e}") from e
        if r.status_code != 200:
            raise HTTPException(r.status_code, r.text)
        await save_setting(f"module.{name}", merged)
        cache.pop(name, None)  # its health may change with the settings
        return await values(name, d)

    @app.post("/api/modules/{name}/events")
    async def module_events(name: str, body: EventsIn):
        """A module's events (decoded bus transactions, …) with unix times: capture puts them on the timeline."""
        known(name)
        try:
            r = await capture.post("/modules/events", json={"module": f"{name}:{body.instance}", "items": body.items,
                                                            "live": body.live, "record": body.record},
                                   timeout=20)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"capture service unreachable: {e}") from e
        return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))

    @app.get("/api/modules/{name}/events/live")
    async def module_events_live(name: str, request: Request):
        known(name)
        try:
            r = await capture.get("/modules/events/live", params={**request.query_params, "module": name})
        except httpx.HTTPError as e:
            raise HTTPException(502, f"capture service unreachable: {e}") from e
        return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))

    @app.get("/api/modules/{name}/recording")
    async def module_recording(name: str, after_seq: int = 0):
        known(name)
        try:
            r = await capture.get("/modules/recording", params={"after_seq": after_seq})
        except httpx.HTTPError as e:
            raise HTTPException(502, f"capture service unreachable: {e}") from e
        return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))

    @app.api_route("/api/modules/{name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def module_proxy(name: str, path: str, request: Request):
        url = known(name)
        try:
            r = await client.request(request.method, f"{url}/{path}", params=request.query_params,
                                     content=await request.body(), headers={"content-type": "application/json"},
                                     timeout=60)
        except httpx.ConnectError as e:
            raise HTTPException(409, f"module {name} isn't running") from e
        except httpx.HTTPError as e:
            raise HTTPException(502, f"module {name} unreachable: {e}") from e
        return Response(r.content, r.status_code, media_type=r.headers.get("content-type"))
