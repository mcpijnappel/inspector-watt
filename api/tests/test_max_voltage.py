"""The tracker's voltage limit: only the web UI sets it, never the MCP server."""

from fastapi.testclient import TestClient

from api import main


def test_agents_cant_change_the_voltage_limit():
    c = TestClient(main.app)  # no lifespan: refused before the database or capture is needed
    r = c.put("/api/measurement/max-voltage", json={"max_voltage_mv": 5000}, headers={"X-PST-Client": "mcp"})
    assert r.status_code == 403 and "web UI only" in r.json()["detail"]
    assert c.put("/api/measurement/max-voltage", json={"max_voltage_mv": 6000}).status_code == 422
    assert c.put("/api/measurement/max-voltage", json={"max_voltage_mv": 500}).status_code == 422


def test_the_limit_is_not_a_normal_setting():
    assert "max_voltage_mv" not in main.MeasurementIn.model_fields
