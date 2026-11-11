import asyncio
import json

import httpx
import pytest

from types import SimpleNamespace

from pst_mcp import hardware, server
from pst_mcp.auth import TokenGate
from pst_mcp.client import Api, ApiError, allowed

SESSIONS = [
    {"id": 2, "name": "1 day test", "notes": "soak", "kind": "soak", "status": "stopped", "started_at": "2026-11-01T10:00:00Z",
     "duration_s": 86400, "avg_current_a": 0.0022, "charge_c": 192, "device_name": "XIAO ESP32-C6", "profile_id": 1, "wakes": 141},
    {"id": 1, "name": "first test", "notes": "", "kind": "detail", "status": "stopped", "started_at": "2026-10-31T10:00:00Z",
     "duration_s": 3600, "avg_current_a": 0.009, "charge_c": 33, "device_name": "Other board", "profile_id": 1, "wakes": 49},
]


def fake_api(calls):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, dict(req.url.params.multi_items()), req.content))
        p = req.url.path
        if p == "/api/sessions":
            return httpx.Response(200, json=SESSIONS)
        if p == "/api/sessions/2/battery":
            return httpx.Response(200, json={"measured": {"days": 18.8}, "params": req.url.params.get_list("without")})
        if p == "/api/sessions/2/uart":
            return httpx.Response(200, json=[{"t": 1.5, "wall": "2026-11-01T10:00:01Z", "text": f"line {i}", "sample_idx": 1} for i in range(3)])
        if p == "/api/sessions/2/phases/test":
            return httpx.Response(200, json={"cycles": 141, "found": [141, 36]})
        if p == "/api/device/status":
            return httpx.Response(200, json={"time": 1000.0, "ppk2": {"streaming": True, "dut_power": True},
                                             "uart": {"lines": 3}, "buffer": {"start_idx": 0, "end_idx": 2_000_000,
                                                                              "end_wall": 1000.0, "sample_period_s": 1e-5}})
        if p == "/api/device/buffer/stats":
            return httpx.Response(200, json={"mean_a": 0.002, "max_a": 0.05, "min_a": 1e-6, "charge_c": 0.02, "lost": 0})
        if p == "/api/device/buffer/window":
            return httpx.Response(200, json={"start": 1_000_000, "bin_samples": 250_000, "mean": [1e-3] * 4, "min": [0] * 4, "max": [2e-3] * 4})
        if p == "/api/device/uart/lines":
            return httpx.Response(200, json=[{"seq": i, "wall": w, "text": f"l{i}"} for i, w in enumerate([980.0, 995.0, 999.5])])
        if p == "/api/mcp/auth":
            return httpx.Response(200, json={"lan": False, "token": None, "hardware": STATE["hardware"]})
        if p == "/api/agents/requests" and req.method == "POST":
            return httpx.Response(200, json={"status": STATE["ask"], "request": {"id": 7}})
        if p == "/api/agents/requests/7":
            return httpx.Response(200, json={"id": 7, "status": STATE["answer"]})
        if p == "/api/agents/actions":
            return httpx.Response(200, json={"ok": True})
        if p == "/api/devices/active":
            return httpx.Response(200, json={"id": 1, "voltage_mv": 3700})
        if p == "/api/chips" and req.method == "GET":
            return httpx.Response(200, json=[{"id": 3, "name": "SHT4x", "body": {"name": "SHT4x", "buses": [{"bus": "i2c", "addresses": [68], "style": "commands"}]},
                                              "source": "built-in", "agent": None, "checked": False, "boards": []}])
        if p == "/api/chips/3" and req.method == "PUT":
            return httpx.Response(200, json={"name": "SHT4x", "checked": False, "boards": []})
        if p == "/api/chips" and req.method == "POST":
            return httpx.Response(200, json={"name": json.loads(req.content)["name"], "checked": False})
        if p == "/api/devices/1/bus-chips":
            return httpx.Response(200, json=json.loads(req.content) if req.method == "PUT" else {"m:68": {"type": "SHT4x"}, "0:16": {"name": "old"}})
        if p.startswith("/api/device/ppk2/"):
            return httpx.Response(200, json={})
        if p == "/api/profiles/1" and req.method == "PUT":
            return httpx.Response(422, json={"detail": "cycle type 'x': no phase called 'Nope'"})
        return httpx.Response(404, json={"detail": "not found"})
    return handler


STATE = {"hardware": False, "ask": "pending", "answer": "allowed"}


@pytest.fixture
def calls(monkeypatch):
    calls = []
    STATE.update(hardware=False, ask="pending", answer="allowed")
    a = Api(base_url="http://api", transport=httpx.MockTransport(fake_api(calls)))
    monkeypatch.setattr(server, "api", a)
    monkeypatch.setattr(server, "hw", hardware.Hardware(a))
    monkeypatch.setattr(hardware, "POLL_S", 0.01)
    return calls


def fake_ctx(session_id="abc-session-123", lan_ip=None):
    headers = {"mcp-session-id": session_id} if session_id else {}
    if lan_ip:
        headers.update({"x-pst-via": "web", "x-real-ip": lan_ip})
    info = SimpleNamespace(name="claude-code", version="2.1.300")

    async def info_msg(_m):
        pass
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers),
                                                           session=SimpleNamespace(client_params=SimpleNamespace(clientInfo=info))),
                           info=info_msg)


def run(coro):
    return asyncio.run(coro)


def test_list_sessions_filters(calls):
    r = run(server.list_sessions(device="xiao"))
    assert [s["id"] for s in r["sessions"]] == [2] and r["total"] == 1
    assert run(server.list_sessions(kind="detail"))["sessions"][0]["id"] == 1


def test_search_log_and_battery_params(calls):
    r = run(server.search_log(2, contains="line", limit=2))
    assert len(r["lines"]) == 2 and r["more"] is True and r["lines"][0]["t_s"] == 1.5
    assert calls[-1][2]["q"] == "line" and calls[-1][2]["limit"] == "3"
    b = run(server.battery_life(2, 1000, without_phases=["WiFi connect", "Poll"]))
    assert b["params"] == ["WiFi connect", "Poll"]  # repeated query parameter
    assert "sleep_s" not in calls[-1][2]  # unset what-ifs aren't sent


def test_live_window(calls):
    r = run(server.live_window(seconds=10))
    assert r["mean_a"] == 0.002 and len(r["rows"]) == 4
    assert r["rows"][0][0] == 10.0  # first bin starts 10 s before the end
    assert [l["text"] for l in r["log"]] == ["l1", "l2"]  # the line 20 s ago is outside the window
    stats_call = next(c for c in calls if c[1] == "/api/device/buffer/stats")
    assert stats_call[2] == {"start": "1000000", "end": "2000000"}


def test_rules_and_errors(calls):
    r = run(server.test_phase_rules(2, [server.Rule(name="Boot", pattern="rst:0x"), server.Rule(name="WiFi", pattern="WiFi")]))
    assert r == {"cycles": 141, "found": [{"phase": "Boot", "in_cycles": 141}, {"phase": "WiFi", "in_cycles": 36}]}
    with pytest.raises(ApiError, match="no phase called 'Nope'"):
        run(server.save_profile(profile_id=1, types=[server.CycleType(name="x", begin="Nope")]))
    with pytest.raises(ApiError, match="needs a name"):
        run(server.save_profile(rules=[]))


def test_only_safe_writes():
    assert allowed("GET", "/api/device/status")
    for m, p in [("POST", "/api/device/ppk2/dut"), ("POST", "/api/device/ppk2/source"), ("POST", "/api/sessions"),
                 ("POST", "/api/sessions/stop"), ("PUT", "/api/devices/active"), ("DELETE", "/api/sessions/3"),
                 ("DELETE", "/api/profiles/1"), ("POST", "/api/device/sessions/save-buffer"),
                 ("POST", "/api/flash"), ("GET", "/api/flash/settings"), ("POST", "/api/flash/1/answer"),
                 ("POST", "/api/device/usb/open"), ("POST", "/api/device/usb/key"),
                 ("POST", "/api/agents/requests/1/answer"), ("POST", "/api/agents/revoke"), ("PUT", "/api/mcp"),
                 ("POST", "/api/flash/token"), ("POST", "/api/mcp/tokens"), ("DELETE", "/api/mcp/tokens/1"),
                 ("POST", "/api/mcp/tokens/1/renew"), ("PUT", "/api/measurement"),
                 ("PUT", "/api/measurement/max-voltage"), ("POST", "/api/modules/analyzer/events"),
                 ("PUT", "/api/modules/analyzer/settings"), ("POST", "/api/modules/analyzer/scan"),
                 ("POST", "/api/chips/3/check"), ("DELETE", "/api/chips/3"), ("PUT", "/api/devices/1"),
                 ("POST", "/api/chips/3"), ("PUT", "/api/chips")]:
        assert not allowed(m, p), (m, p)
    for m, p in [("PUT", "/api/profiles/1"), ("POST", "/api/profiles"), ("POST", "/api/sessions/3/phases/test"),
                 ("POST", "/api/sessions/3/types/test"), ("PATCH", "/api/sessions/3"), ("GET", "/api/flash/jobs"),
                 ("GET", "/api/flash/3"), ("POST", "/api/agents/requests"), ("GET", "/api/agents/requests/3"),
                 ("POST", "/api/agents/actions"), ("POST", "/api/chips"), ("PUT", "/api/chips/3"),
                 ("PUT", "/api/devices/1/bus-chips"), ("POST", "/api/modules/analyzer/decode")]:
        assert allowed(m, p), (m, p)
    with pytest.raises(ApiError, match="not allowed"):
        run(Api(base_url="http://api").request("POST", "/api/device/ppk2/dut", json={"on": True}))


def test_says_it_is_the_mcp_server():
    """The api refuses the voltage limit to requests marked like this, whatever the allow list says."""
    seen = []
    a = Api(base_url="http://api", transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=[])))
    run(a.request("GET", "/api/sessions"))
    assert seen[0].headers["x-pst-client"] == "mcp"


READ_TOOLS = {"list_sessions", "get_session", "range_stats", "current_over_time", "search_log", "list_events",
              "cycle_summary", "get_cycle", "battery_life", "live_status", "live_window", "list_profiles",
              "test_phase_rules", "test_cycle_types", "save_profile", "usb_console_lines", "list_uploads",
              "chips_list", "chip_get", "chip_save", "board_chips", "set_board_chips", "bus_decode"}


def test_tools_are_registered_with_schemas(calls):
    tools = {t.name: t for t in run(server.mcp.list_tools())}
    assert READ_TOOLS == set(tools)  # hardware control is off: those tools aren't listed
    assert "session_id" in tools["battery_life"].inputSchema["properties"]
    assert not any(w in n for n in tools for w in ("power", "voltage", "start", "stop", "record"))


def test_hardware_tools_listed_when_on_and_always_ask(calls):
    STATE["hardware"] = True
    server.hw._cached = None
    tools = {t.name: t for t in run(server.mcp.list_tools())}
    assert set(tools) == READ_TOOLS | server.HARDWARE_TOOLS
    for name in server.HARDWARE_TOOLS:
        t = tools[name]
        assert t.meta == {"anthropic/requiresUserInteraction": True}, name
        assert t.annotations.destructiveHint is True and t.annotations.readOnlyHint is False
        assert "ctx" not in t.inputSchema["properties"]
        dumped = t.model_dump(by_alias=True, exclude_none=True)
        assert dumped["_meta"]["anthropic/requiresUserInteraction"] is True  # what clients receive


def test_hardware_call_waits_for_the_allow_then_acts_and_logs(calls):
    STATE["hardware"] = True
    r = run(server.dut_power(on=True, ctx=fake_ctx(lan_ip="192.168.1.20")))
    paths = [(m, p) for m, p, _q, _b in calls]
    ask = next(json.loads(b) for m, p, _q, b in calls if p == "/api/agents/requests")
    assert ask["client"] == "claude-code 2.1.300" and ask["host"] == "192.168.1.20" and ask["via"] == "lan"
    assert ask["session"] != "abc-session-123" and len(ask["session"]) == 32  # hashed, never the raw id
    assert ("GET", "/api/agents/requests/7") in paths  # waited for the person
    assert ("POST", "/api/device/ppk2/source") in paths  # voltage first (device default, 3.7 V)
    dut = next(json.loads(b) for m, p, _q, b in calls if p == "/api/device/ppk2/dut")
    assert dut == {"on": True, "by": "agent"}
    logged = json.loads(next(b for m, p, _q, b in calls if p == "/api/agents/actions"))
    assert logged["tool"] == "dut_power" and logged["result"] == "ok"
    assert "dut_power" in r


def test_power_cycle_switches_off_then_on(calls, monkeypatch):
    STATE["hardware"] = True
    orig = Api.get

    async def status_on(self, path, **params):
        if path == "/api/device/status":
            return {"ppk2": {"dut_power": True, "voltage_set_by_us": True, "voltage_mv": 3700, "streaming": True}}
        return await orig(self, path, **params)
    monkeypatch.setattr(Api, "get", status_on)
    r = run(server.power_cycle(off_s=0.2, ctx=fake_ctx()))
    duts = [json.loads(b) for m, p, _q, b in calls if p == "/api/device/ppk2/dut"]
    assert duts == [{"on": False, "by": "agent"}, {"on": True, "by": "agent"}]
    assert r["was_on"] is True and r["off_s"] == 0.2


def test_hardware_refused_when_off_denied_or_without_session(calls):
    with pytest.raises(ApiError, match="switch it on"):
        run(server.measuring(on=True, ctx=fake_ctx()))
    STATE["hardware"] = True
    server.hw._cached = None
    with pytest.raises(ApiError, match="needs an MCP session"):
        run(server.measuring(on=True, ctx=fake_ctx(session_id=None)))
    STATE["answer"] = "denied"
    with pytest.raises(ApiError, match="denied"):
        run(server.measuring(on=True, ctx=fake_ctx()))
    assert not any(p == "/api/device/ppk2/stream" for _m, p, _q, _b in calls)
    logged = json.loads([b for m, p, _q, b in calls if p == "/api/agents/actions"][-1])
    assert logged["result"] == "denied"
    with pytest.raises(ApiError, match="not a hardware call"):
        run(server.api.hardware("POST", "/api/flash", {}))


async def call_gate(access, headers, path="/mcp", fail=False):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def provider():
        if fail:
            raise RuntimeError("api down")
        return access

    async def health():
        return {"ok": True, "tools": []}

    sent = []

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "path": path, "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
    await TokenGate(app, provider, health)(scope, None, send)
    return sent[0]["status"], (json.loads(sent[1]["body"]) if sent[1]["body"] != b"ok" else None)


def test_token_gate():
    on = {"lan": True, "token": "s3cret"}
    lan = {"x-pst-via": "web"}
    assert run(call_gate(on, {}))[0] == 200  # local port: no token needed
    assert run(call_gate({"lan": False, "token": None}, {}))[0] == 200
    status, body = run(call_gate({"lan": False, "token": "s3cret"}, {**lan, "authorization": "Bearer s3cret"}))
    assert status == 403 and "Settings" in body["error"]  # LAN switched off
    status, body = run(call_gate(on, lan))
    assert status == 401 and "no Authorization header" in body["error"]  # e.g. a proxy dropped it
    status, body = run(call_gate(on, {**lan, "authorization": "Bearer nope"}))
    assert status == 401 and "wrong token" in body["error"] and "nope" not in body["error"]
    named = {"lan": True, "tokens": [{"name": "desktop", "token": "aaa"}, {"name": "laptop", "token": "bbb"}]}
    assert run(call_gate(named, {**lan, "authorization": "Bearer bbb"}))[0] == 200
    assert run(call_gate(named, {**lan, "authorization": "Bearer ccc"}))[0] == 401
    assert run(call_gate({"lan": True, "tokens": []}, {**lan, "authorization": "Bearer bbb"}))[0] == 403
    assert "isn't 'Bearer" in run(call_gate(on, {**lan, "authorization": "s3cret"}))[1]["error"]
    assert run(call_gate(on, {**lan, "authorization": "Bearer s3cret"}))[0] == 200
    assert run(call_gate(on, {**lan, "authorization": "Bearer s3cret"}, fail=True))[0] == 403  # api down: refuse


def test_health_is_local_only():
    on = {"lan": True, "token": "s3cret"}
    status, body = run(call_gate(on, {}, path="/health"))
    assert status == 200 and body["ok"] is True
    # via the LAN, /health isn't special (nginx only forwards /mcp anyway)
    assert run(call_gate(on, {"x-pst-via": "web"}, path="/health"))[0] == 401


def test_gate_names_the_token_and_drops_a_forged_name():
    seen = {}

    async def app(scope, receive, send):
        seen["h"] = dict(scope["headers"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def provider():
        return {"lan": True, "tokens": [{"name": "laptop", "token": "bbb"}]}

    async def send(_m):
        pass

    lan = [(b"x-pst-via", b"web"), (b"authorization", b"Bearer bbb"), (b"x-pst-token-name", b"forged")]
    run(TokenGate(app, provider)({"type": "http", "path": "/mcp", "headers": lan}, None, send))
    assert seen["h"][b"x-pst-token-name"] == b"laptop"
    local = [(b"x-pst-token-name", b"forged")]  # local port: no token, and no name either
    run(TokenGate(app, provider)({"type": "http", "path": "/mcp", "headers": local}, None, send))
    assert b"x-pst-token-name" not in seen["h"]


def test_sensor_library_tools(calls):
    ctx = SimpleNamespace(request_context=SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(
        clientInfo=SimpleNamespace(name="claude-code", version="2.1")))))
    # an existing chip is updated, a new one added; the agent's name goes along
    i2c = server.ChipBus(bus="i2c", addresses=[0x44], style="commands")
    assert run(server.chip_save(server.ChipTable(name="sht4x", buses=[i2c]), ctx))["updated"] == "SHT4x"
    added = server.ChipTable(name="LIS3DH", buses=[server.ChipBus(bus="i2c", addresses=[0x18]), server.ChipBus(bus="spi", inc_bit=0x40)])
    assert run(server.chip_save(added, ctx))["added"] == "LIS3DH"
    puts = [c for c in calls if c[0] in ("PUT", "POST") and c[1].startswith("/api/chips")]
    assert [c[1] for c in puts] == ["/api/chips/3", "/api/chips"]
    assert run(server.chip_get("SHT4X"))["table"]["buses"][0]["style"] == "commands"
    assert run(server.chips_list())["chips"][0]["buses"] == [{"bus": "i2c", "addresses": ["0x44"], "style": "commands", "registers": 0, "commands": 0}]
    sent = json.loads(next(c for c in calls if c[0] == "POST" and c[1] == "/api/chips")[3])
    assert sent["buses"] == [{"bus": "i2c", "addresses": [0x18], "style": "basic", "reg_bytes": 1, "byte_order": "big", "registers": [], "commands": []},
                             {"bus": "spi", "addresses": [], "inc_bit": 0x40, "style": "basic", "reg_bytes": 1, "byte_order": "big", "registers": [], "commands": []}]
    # board chips: changes merge, removals drop, the rest stays
    r = run(server.set_board_chips({"1:16": server.BoardChip(type="VEML7700", name="Light 1")}, remove=["0:16"]))
    assert r["chips"] == {"m:68": {"type": "SHT4x"}, "1:16": {"type": "VEML7700", "name": "Light 1"}}
