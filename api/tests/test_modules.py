"""Modules: the api checks their settings against their own description and keeps strangers out."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import main
from api.modules import check_settings

DESCRIBED = [
    {"key": "enabled", "type": "bool", "default": True},
    {"key": "samplerate_hz", "type": "choice", "choices": [1_000_000, 4_000_000], "default": 4_000_000},
    {"key": "pre_ms", "type": "number", "min": 0, "max": 1000, "default": 50},
    {"key": "name", "type": "text", "max_length": 10, "default": ""},
]


def test_settings_are_checked_against_the_description():
    ok = {"enabled": False, "samplerate_hz": 1_000_000, "pre_ms": 20, "name": "nano"}
    assert check_settings(DESCRIBED, ok) == ok
    for bad in [{"junk": 1}, {"enabled": "yes"}, {"samplerate_hz": 3}, {"pre_ms": 2000}, {"pre_ms": True},
                {"name": "x" * 11}]:
        with pytest.raises(HTTPException):
            check_settings(DESCRIBED, bad)


def test_unknown_modules():
    c = TestClient(main.app)  # no lifespan: refused before the database is needed
    assert c.get("/api/modules/nope/settings").status_code == 404
    assert c.post("/api/modules/nope/events", json={"items": []}).status_code == 404
    assert c.get("/api/modules/nope/anything").status_code == 404
