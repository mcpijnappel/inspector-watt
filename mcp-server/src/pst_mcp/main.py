"""ASGI app: the MCP endpoint at /mcp behind the LAN gate, and /health (local only) for the web UI's status."""

from .auth import TokenGate
from .server import api, mcp


async def access() -> dict:
    return await api.get("/api/mcp/auth")


async def health() -> dict:
    tools = await mcp.list_tools()
    # First paragraph of each tool's description, on one line.
    return {"ok": True, "tools": [{"name": t.name, "description": " ".join((t.description or "").strip().split("\n\n")[0].split())}
                                  for t in tools]}


app = TokenGate(mcp.streamable_http_app(), access, health)
