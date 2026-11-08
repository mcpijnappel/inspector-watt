import numpy as np

from api.phases import (Cycle, Phase, compile_wake, find_cycles, match_counts, mix_current, mix_parts, sleep_rule, snap_time, split_parts,
                        type_instances, type_kind, type_summary, type_views)

RULES = [
    {"name": "Boot", "match": "contains", "pattern": "rst:0x"},
    {"name": "WiFi", "match": "regex", "pattern": r"Connecting to WiFi \(attempt \d+/\d+\)"},
    {"name": "Connected", "match": "contains", "pattern": "WiFi connected"},
    {"name": "Sleep", "match": "contains", "pattern": "Going to deep sleep"},
]

LINES = [
    (0.0, "rst:0x5 (SLEEP_WAKEUP)"), (5.0, "App start"), (10.3, "Connecting to WiFi (attempt 1/3)..."),
    (11.0, "Connecting to WiFi (attempt 2/3)..."), (12.3, "WiFi connected"), (17.8, "Going to deep sleep"),
    (77.3, "rst:0x5 (SLEEP_WAKEUP)"), (82.0, "Connecting to WiFi (attempt 1/3)"), (95.0, "Going to deep sleep"),  # no "Connected"
    (155.0, "rst:0x1 (POWERON)"),
]


def test_cycles_and_phases():
    cycles = find_cycles(LINES, RULES, 160.0)
    assert [c.t for c in cycles] == [0.0, 77.3, 155.0]
    assert [c.closed for c in cycles] == [True, True, False]
    c0 = cycles[0]
    assert [(p.rule, p.t, p.end_t) for p in c0.phases] == [(0, 0.0, 10.3), (1, 10.3, 12.3), (2, 12.3, 17.8), (3, 17.8, 77.3)]
    # rule 2 not seen in cycle 1: WiFi runs until sleep
    assert [(p.rule, p.t, p.end_t) for p in cycles[1].phases] == [(0, 77.3, 82.0), (1, 82.0, 95.0), (3, 95.0, 155.0)]


def test_match_counts():
    assert match_counts(LINES, RULES, 160.0) == {"cycles": 3, "found": [3, 2, 1, 2], "woken_by": {}}


def test_all_wakes_summary_uses_complete_cycles():
    cycles = find_cycles(LINES, RULES, 160.0)
    for c in cycles:
        for p in c.phases:
            p.coverage = 1.0
            p.charge_c = (p.end_t - p.t) * 0.01
    s = type_summary(type_views(cycles, RULES, [])[0], RULES)
    # complete = closed with data; a missing phase (no "Connected" in cycle 1) doesn't make it incomplete
    assert s["cycles"] == 3 and s["usable"] == 2
    ph = {p["phase"]: p for p in s["phases"]}
    assert ph["Connected"]["in_cycles"] == 1 and ph["Sleep"]["in_cycles"] == 2
    assert abs(ph["Sleep"]["duration_s"] - (59.5 + 60.0) / 2) < 1e-9
    assert abs(s["mean_a"] - 0.01) < 1e-12
    cycles[0].phases[1].coverage = 0.5  # a gap
    assert type_summary(type_views(cycles, RULES, [])[0], RULES)["usable"] == 1


def test_split_parts():
    assert split_parts(10.2, 11.0) == ([(10.2, 11.0)], None)
    edges, mid = split_parts(10.2, 70.7)
    assert edges == [(10.2, 11.0), (70.0, 70.7)] and mid == (11.0, 70.0)


def test_snap_finds_the_step():
    cur = np.r_[np.full(1500, 50e-3), np.full(2500, 1.5e-3)].astype(np.float32)  # step at sample 1500
    t = snap_time(cur, 100.0, 100.02)
    assert abs(t - (100.0 + 1500e-5)) < 0.0005
    assert snap_time(np.full(4000, 1e-3, np.float32), 0.0, 0.02) is None  # flat: no step


# cycle types --------------------------------------------------------------------------------------------------------
TYPE_RULES = [{"name": n} for n in ["Boot", "Init", "WiFi", "NTP", "Poll", "Sleep"]]
M = [(0, 5), (1, 8), (5, 600)]  # measure only
U = [(0, 5), (1, 0.2), (2, 2), (3, 26), (5, 600)]  # upload
UP = [(0, 5), (1, 0.2), (2, 2), (3, 4), (4, 22), (5, 600)]  # upload + poll
CUR = [0.02, 0.03, 0.06, 0.05, 0.05, 0.001]  # A per phase


def wakes(*specs, gap_at=None, open_last=True):
    out, t = [], 0.0
    for i, spec in enumerate(specs):
        c = Cycle(t=t, end_t=0.0, closed=not (open_last and i == len(specs) - 1))
        for r, d in spec:
            c.phases.append(Phase(rule=r, t=t, end_t=t + d, charge_c=d * CUR[r], mean_a=CUR[r], coverage=0.5 if i == gap_at else 1.0))
            t += d
        c.end_t = t
        out.append(c)
    return out


def typ(begin, end=None, include=False, must=(), exclude=()):
    return {"name": "x", "begin": begin, "end": {"phase": end, "include": include} if end else None,
            "must": list(must), "exclude": list(exclude)}


def test_whole_wake_types_split_the_wakes():
    cs = wakes(U, M, M, M, UP, M, M, M, U, M)  # the last one runs into the end of the session
    measure = type_instances(cs, TYPE_RULES, typ("Boot", exclude=["WiFi"]))
    upload = type_instances(cs, TYPE_RULES, typ("Boot", must=["WiFi"]))
    assert len(measure["instances"]) == 6 and measure["filtered"] == 3
    assert len(upload["instances"]) == 3 and upload["filtered"] == 6
    # a wake is its phases: wake 1 = flat phases 5..7
    assert measure["instances"][0] == [5, 8]
    assert type_kind(typ("Boot"), TYPE_RULES) == "wake"


def test_period_runs_to_the_next_begin_across_wakes():
    cs = wakes(U, M, M, M, UP, M, M, M, U, M)
    r = type_instances(cs, TYPE_RULES, typ("WiFi"))
    assert len(r["instances"]) == 2  # WiFi -> next WiFi, twice; the third has no next begin
    s, e = r["instances"][0]
    flat = [p for c in cs for p in c.phases]
    assert flat[s].rule == 2 and flat[e].rule == 2
    # the three measure wakes in between, and the next upload wake's boot (before its WiFi)
    assert sum(1 for p in flat[s:e] if p.rule == 0) == 4
    assert type_kind(typ("WiFi"), TYPE_RULES) == "period"


def test_stretch_stop_before_or_include():
    cs = wakes(U, M, UP, M)
    flat = [p for c in cs for p in c.phases]
    inc = type_instances(cs, TYPE_RULES, typ("WiFi", "NTP", include=True))
    stop = type_instances(cs, TYPE_RULES, typ("WiFi", "NTP"))
    assert [[flat[k].rule for k in range(s, e)] for s, e in inc["instances"]] == [[2, 3], [2, 3]]
    assert [[flat[k].rule for k in range(s, e)] for s, e in stop["instances"]] == [[2], [2]]
    awake = type_instances(cs, TYPE_RULES, typ("Boot", "Sleep"))
    assert len(awake["instances"]) == 4  # a stretch ends inside the wake: the open last one counts too
    poll = type_instances(cs, TYPE_RULES, typ("Boot", "Poll", include=True))
    assert len(poll["instances"]) == 1 and poll["no_end"] == 3
    assert type_kind(typ("WiFi", "NTP"), TYPE_RULES) == "stretch"


def test_gaps_and_unknown_phases():
    cs = wakes(U, M, M, U, M, gap_at=1)
    r = type_instances(cs, TYPE_RULES, typ("Boot", exclude=["WiFi"]))
    assert len(r["instances"]) == 1 and r["gaps"] == 1  # wake 1 has a gap, wake 4 is cut off
    bad = type_instances(cs, TYPE_RULES, typ("Boot", must=["Upload"]))
    assert bad["error"] == "unknown phase: Upload" and bad["instances"] == []


TYPES = [typ("Boot", exclude=["WiFi"]) | {"name": "Measure"}, typ("Boot", must=["WiFi"]) | {"name": "Upload"},
         typ("WiFi", "NTP", include=True) | {"name": "WiFi + NTP"}]


def test_type_summary_per_phase():
    cs = wakes(U, M, M, M, UP, M, M, M, U, M)
    views = type_views(cs, TYPE_RULES, TYPES)
    assert [v["name"] for v in views] == [None, "Measure", "Upload", "WiFi + NTP"]
    measure = type_summary(views[1], TYPE_RULES)
    assert measure["usable"] == 6 and [p["phase"] for p in measure["phases"]] == ["Boot", "Init", "Sleep"]
    assert abs(sum(p["share"] for p in measure["phases"]) - 1) < 1e-9
    upload = type_summary(views[2], TYPE_RULES)
    poll = next(p for p in upload["phases"] if p["phase"] == "Poll")
    assert poll["in_cycles"] == 1 and abs(poll["charge_per_cycle_c"] - 22 * 0.05 / 3) < 1e-9
    stretch = type_summary(views[3], TYPE_RULES)
    assert [p["phase"] for p in stretch["phases"]] == ["WiFi", "NTP"]
    everything = type_summary(views[0], TYPE_RULES)
    assert everything["usable"] == 9 and len(everything["phases"]) == 6  # all wakes list every phase
    assert [p["rule"] for p in stretch["phases"]] == [2, 3]
    # typical: the cycle with the median charge, as an index within the type
    xs = sorted((x for x in views[2]["insts"] if x.ok), key=lambda x: x.charge)
    assert upload["typical_index"] == xs[len(xs) // 2].k
    assert type_summary({**views[1], "insts": []}, TYPE_RULES)["typical_index"] is None


def test_battery_mix_and_what_ifs():
    cs = wakes(U, M, M, M, UP, M, M, M, U, M)
    views = type_views(cs, TYPE_RULES, TYPES)
    parts = mix_parts(views)
    assert [v["name"] for v in parts] == ["Measure", "Upload"]
    assert mix_parts(type_views(cs, TYPE_RULES, TYPES[1:])) == [views[0]]  # Upload alone doesn't cover all wakes
    sleep = sleep_rule(views[0], 6)
    assert sleep == 5
    usable = [c for c in cs if c.closed]
    real = sum(p.charge_c for c in usable for p in c.phases) / sum(c.end_t - c.t for c in usable)
    now = mix_current(parts, 6, sleep, CUR[5])
    assert abs(now - real) < 1e-12
    assert abs(mix_current([views[0]], 6, sleep, CUR[5]) - real) < 1e-12
    # every wake an upload = the uploads' own average
    ups = [c for c in usable if any(p.rule == 2 for p in c.phases)]
    up_a = sum(p.charge_c for c in ups for p in c.phases) / sum(c.end_t - c.t for c in ups)
    assert abs(mix_current(parts, 6, sleep, CUR[5], pick="Upload", every=1) - up_a) < 1e-12
    assert mix_current(parts, 6, sleep, CUR[5], pick="Upload", every=10) < now
    assert mix_current(parts, 6, sleep, CUR[5], sleep_s=3600) < now  # sleep longer
    assert mix_current(parts, 6, sleep, CUR[5], skip=(3,)) < now  # no NTP


WAKE_LINES = [
    (0.0, "rst:0x1 (POWERON),boot:0x7e"), (0.4, "[I] [Wake] Cold boot"), (5.0, "Going to deep sleep"),
    (60.0, "rst:0x5 (SLEEP_WAKEUP),boot:0x7e"), (60.4, "[I] [Wake] Wake source: Timer"), (65.0, "Going to deep sleep"),
    (120.0, "rst:0x5 (SLEEP_WAKEUP),boot:0x7e"), (120.4, "[I] [Wake] Wake source: GPIO"),
    (125.0, "Going to deep sleep"), (180.0, "rst:0x5 (SLEEP_WAKEUP),boot:0x7e"), (185.0, "Going to deep sleep"),
]
WAKE_RULES = [{"name": "Boot", "match": "contains", "pattern": "rst:0x"}, {"name": "Sleep", "match": "contains", "pattern": "deep sleep"}]
CUSTOM = r"\[Wake\] (?:Wake source: )?(.+)"


def test_wake_reasons():
    cs = find_cycles(WAKE_LINES, WAKE_RULES, 240.0, compile_wake(CUSTOM))
    assert [c.wake for c in cs] == ["Cold boot", "Timer", "GPIO", None]
    # the generic default finds "Wake source: ..." lines of other firmware too (not "Cold boot")
    assert [c.wake for c in find_cycles(WAKE_LINES, WAKE_RULES, 240.0, compile_wake(None))] == [None, "Timer", "GPIO", None]
    assert match_counts(WAKE_LINES, WAKE_RULES, 240.0, compile_wake(CUSTOM))["woken_by"] == {"Cold boot": 1, "Timer": 1, "GPIO": 1}


def test_types_by_wake_reason():
    cs = find_cycles(WAKE_LINES, WAKE_RULES, 240.0, compile_wake(CUSTOM))
    for c in cs:
        for p in c.phases:
            p.coverage, p.charge_c = 1.0, (p.end_t - p.t) * 0.01
    motion = {"name": "Motion", "begin": "Boot", "end": None, "must": [], "exclude": [], "woken_by": ["GPIO"]}
    r = type_instances(cs, WAKE_RULES, motion)
    assert len(r["instances"]) == 1 and r["filtered"] == 2  # the last wake runs to the end: not finished
    views = type_views(cs, WAKE_RULES, [motion])
    assert type_summary(views[0], WAKE_RULES)["woken_by"] == {"Cold boot": 1, "Timer": 1, "GPIO": 1}
    assert type_summary(views[1], WAKE_RULES)["woken_by"] == {"GPIO": 1}
