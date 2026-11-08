import pytest

from api.agents import ASK_WAIT_S, DENY_HOLD_S, MAX_GRANT_S, AgentAccess


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ask(a, session="s1" * 8, tool="dut_power"):
    return a.ask(session, "claude-code 2.1", "192.168.1.20", "lan", tool, {"on": True})


def test_allow_lasts_for_the_session_until_the_max():
    clock = Clock()
    a = AgentAccess(clock)
    r = ask(a)
    assert r["status"] == "pending"
    rid = r["request"]["id"]
    assert ask(a)["request"]["id"] == rid  # a second call while waiting joins the same request
    a.answer(rid, True)
    assert a.request(rid)["status"] == "allowed"
    assert ask(a, tool="set_voltage")["status"] == "allowed"
    assert a.state()["grants"][0]["actions"] == 2
    assert ask(a, session="other-session")["status"] == "pending"  # another session asks for itself
    clock.t += MAX_GRANT_S + 1
    assert ask(a)["status"] == "pending"  # expired: ask again


def test_deny_holds_then_asks_again():
    clock = Clock()
    a = AgentAccess(clock)
    rid = ask(a)["request"]["id"]
    a.answer(rid, False)
    assert ask(a)["status"] == "denied"
    clock.t += DENY_HOLD_S + 1
    assert ask(a)["status"] == "pending"


def test_unanswered_requests_expire_and_cannot_be_answered():
    clock = Clock()
    a = AgentAccess(clock)
    rid = ask(a)["request"]["id"]
    clock.t += ASK_WAIT_S + 1
    assert a.request(rid)["status"] == "expired"
    with pytest.raises(ValueError):
        a.answer(rid, True)
    assert a.state()["pending"] == []


def test_revoke_one_or_all():
    a = AgentAccess(Clock())
    for s in ("session-a-xxxxxx", "session-b-xxxxxx"):
        a.answer(ask(a, session=s)["request"]["id"], True)
    assert a.revoke("session-a-xxxxxx") == 1
    assert ask(a, session="session-a-xxxxxx")["status"] == "pending"
    assert a.revoke() == 1  # session b; the pending request of a expires too
    assert a.state() ["grants"] == [] and a.state()["pending"] == []
