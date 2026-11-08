"""Cycles and phases from log lines + charge per phase.

Matching: a cycle starts at each line matching rule 0. Inside a cycle, rule k matches the first line at or after
the previous found phase start. A rule that never matches in a cycle is "not seen" there; its time goes to the
phase before it. Phase k runs until the next found phase starts (or the cycle ends).

Charge per phase comes from the 10 ms summaries (buckets cut by a boundary count proportionally), with the
interior of long phases taken from the 1 s summaries so week-long soak sessions stay fast. A boundary can
optionally "snap" to the nearest current step in the raw data (log lines arrive ~1-2 ms late and are printed
after the work they describe).
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

SAMPLE_PERIOD_S = 1e-5
BUCKET_S = 0.01
SNAP_WINDOW_S = 0.02
SNAP_MIN_RATIO = 2.0  # a "step" must change the current at least this much


def compile_rule(rule: dict) -> re.Pattern:
    pattern = rule.get("pattern", "")
    if rule.get("match") == "regex":
        return re.compile(pattern)
    return re.compile(re.escape(pattern))


@dataclass
class Phase:
    rule: int
    t: float
    end_t: float
    charge_c: float = 0.0
    mean_a: float | None = None
    peak_a: float | None = None
    coverage: float = 0.0


@dataclass
class Cycle:
    t: float
    end_t: float
    closed: bool  # ended by the next cycle (not by the end of the session)
    phases: list[Phase] = field(default_factory=list)
    wake: str | None = None  # what woke it, from the firmware's log (see wake_pattern)


# What woke the DUT: the first log line in a cycle matching the profile's wake pattern (its first group).
DEFAULT_WAKE_PATTERN = r"[Ww]ake(?:[ -]?up)? (?:source|reason)\s*:\s*(.+)"


def compile_wake(pattern: str | None) -> re.Pattern:
    return re.compile(pattern or DEFAULT_WAKE_PATTERN)


def find_cycles(lines: list[tuple[float, str]], rules: list[dict], end_s: float, wake: re.Pattern | None = None) -> list[Cycle]:
    """lines: (session seconds, text), sorted by time. wake: the wake reason pattern (one group)."""
    if not rules:
        return []
    rx = [compile_rule(r) for r in rules]
    times = [t for t, _ in lines]
    starts = [t for t, text in lines if rx[0].search(text)]
    cycles = []
    for ci, t0 in enumerate(starts):
        t1 = starts[ci + 1] if ci + 1 < len(starts) else end_s
        found = [(0, t0)]
        lo = bisect.bisect_left(times, t0)
        hi = bisect.bisect_left(times, t1)
        pos = lo + 1
        for k in range(1, len(rules)):
            for j in range(pos, hi):
                if rx[k].search(lines[j][1]):
                    found.append((k, lines[j][0]))
                    pos = j + 1
                    break
        phases = [Phase(rule=k, t=t, end_t=found[i + 1][1] if i + 1 < len(found) else t1) for i, (k, t) in enumerate(found)]
        reason = None
        if wake is not None:
            for j in range(lo, hi):
                m = wake.search(lines[j][1])
                if m:
                    reason = (m.group(1) if m.groups() else m.group(0)).strip() or None
                    break
        cycles.append(Cycle(t=t0, end_t=t1, closed=ci + 1 < len(starts), phases=phases, wake=reason))
    return cycles


def match_counts(lines: list[tuple[float, str]], rules: list[dict], end_s: float, wake: re.Pattern | None = None) -> dict:
    """How many cycles each rule was found in, and the wake reasons found (for the rule editor)."""
    cycles = find_cycles(lines, rules, end_s, wake)
    counts = [0] * len(rules)
    for c in cycles:
        for p in c.phases:
            counts[p.rule] += 1
    return {"cycles": len(cycles), "found": counts, "woken_by": count_wakes(c.wake for c in cycles)}


def count_wakes(reasons) -> dict[str, int]:
    """{reason: count}, most frequent first; cycles without a logged reason are left out."""
    out: dict[str, int] = {}
    for r in reasons:
        if r:
            out[r] = out.get(r, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def snap_time(cur: np.ndarray, t0: float, t: float) -> float | None:
    """Biggest current step in raw samples `cur` (starting at session time t0), near t. None if no clear step."""
    if len(cur) < 200 or np.isnan(cur).mean() > 0.2:
        return None
    c = np.nan_to_num(np.abs(cur), nan=np.nanmedian(np.abs(cur)))
    k = 50  # 0.5 ms moving average
    avg = np.convolve(c, np.ones(k) / k, mode="valid")
    lg = np.log10(np.maximum(avg, 1e-8))
    lag = 20
    d = np.abs(lg[lag:] - lg[:-lag])
    i = int(np.argmax(d))
    if d[i] < math.log10(SNAP_MIN_RATIO):
        return None
    return t0 + (i + lag // 2 + k // 2) * SAMPLE_PERIOD_S


def split_parts(a: float, b: float) -> tuple[list[tuple[float, float]], tuple[float, float] | None]:
    """[a, b) -> 10 ms-tier pieces at the edges + one whole-second piece for the 1 s tier (if long enough).
    Times here are absolute unix seconds so they line up with the summaries' buckets."""
    if b - a < 3:
        return [(a, b)], None
    ia, ib = math.ceil(a), math.floor(b)
    return [(a, float(ia)), (float(ib), b)], (float(ia), float(ib))


async def phase_stats(conn, session: dict, cycles: list[Cycle]) -> None:
    """Fill charge/mean/peak/coverage of every phase (in place)."""
    t0 = session["started_at"].timestamp()
    fine: list[tuple[int, float, float]] = []
    coarse: list[tuple[int, float, float]] = []
    flat = [p for c in cycles for p in c.phases]
    for k, p in enumerate(flat):
        edges, mid = split_parts(t0 + p.t, t0 + p.end_t)
        fine += [(k, a, b) for a, b in edges if b > a]
        if mid and mid[1] > mid[0]:
            coarse.append((k, *mid))
    sums = np.zeros(len(flat))
    counts = np.zeros(len(flat))
    peaks = np.full(len(flat), np.nan)

    async def run(parts, table, tcol, bucket):
        if not parts:
            return
        ks = [p[0] for p in parts]
        a = [datetime.fromtimestamp(p[1], session["started_at"].tzinfo) for p in parts]
        b = [datetime.fromtimestamp(p[2], session["started_at"].tzinfo) for p in parts]
        # LATERAL + plain range bounds on ts: an index range scan per interval instead of one big join.
        rows = await (await conn.execute(f"""
            WITH iv AS (SELECT * FROM unnest(%(k)s::int[], %(a)s::timestamptz[], %(b)s::timestamptz[]) AS t(k, a, b))
            SELECT iv.k, x.s, x.n, x.peak
            FROM iv CROSS JOIN LATERAL (
                SELECT sum(r.i_sum * f.frac) AS s, sum(r.n * f.frac) AS n, max(r.i_max) AS peak
                FROM {table} r
                CROSS JOIN LATERAL (SELECT GREATEST(0, LEAST(1, extract(epoch FROM
                       (LEAST(iv.b, r.{tcol} + %(bucket)s::interval) - GREATEST(iv.a, r.{tcol}))) / %(bucket_s)s)) AS frac) f
                WHERE r.session_id = %(sid)s AND r.{tcol} > iv.a - %(bucket)s::interval AND r.{tcol} < iv.b
            ) x""", {"k": ks, "a": a, "b": b, "sid": session["id"],
                     "bucket": f"{bucket} seconds", "bucket_s": bucket})).fetchall()
        for r in rows:
            k = r["k"]
            sums[k] += float(r["s"] or 0)
            counts[k] += float(r["n"] or 0)
            if r["peak"] is not None:
                peaks[k] = r["peak"] if np.isnan(peaks[k]) else max(peaks[k], r["peak"])

    await run(fine, "rollup_10ms", "ts", BUCKET_S)
    await run(coarse, "rollup_1s", "bucket", 1.0)
    for k, p in enumerate(flat):
        dur = p.end_t - p.t
        p.charge_c = sums[k] * SAMPLE_PERIOD_S
        p.mean_a = sums[k] / counts[k] if counts[k] > 0 else None
        p.peak_a = None if np.isnan(peaks[k]) else float(peaks[k])
        p.coverage = min(1.0, counts[k] * SAMPLE_PERIOD_S / dur) if dur > 0 else 0.0


def is_complete(c: Cycle) -> bool:
    """Usable for averages: ended by the next cycle and (nearly) no missing data. Phases may be missing: not every
    wake does everything (that's what cycle types are for)."""
    return c.closed and all(p.coverage > 0.98 for p in c.phases)


def cycle_json(i: int, c: Cycle) -> dict:
    charge = sum(p.charge_c for p in c.phases)
    dur = c.end_t - c.t
    return {
        "i": i, "t": c.t, "end_t": c.end_t, "closed": c.closed,
        "complete": is_complete(c),
        "wake": c.wake,
        "charge_c": charge, "mean_a": charge / dur if dur > 0 else None,
        # compact: [rule, t, end_t, charge_c, mean_a, peak_a, coverage]
        "phases": [[p.rule, p.t, p.end_t, p.charge_c, p.mean_a, p.peak_a, round(p.coverage, 4)] for p in c.phases],
    }



# cycle types ------------------------------------------------------------------------------------------------------
def type_kind(t: dict, rules: list[dict]) -> str:
    """'stretch' (has an end), 'wake' (begins at the cycle start), or 'period' (from one begin to the next)."""
    if t.get("end"):
        return "stretch"
    return "wake" if rules and t.get("begin") == rules[0].get("name") else "period"


def type_instances(cycles: list[Cycle], rules: list[dict], t: dict) -> dict:
    """The cycles of a type, as [start, end) indices into the flat list of all phases (every cycle's phases in
    order). Each phase `begin` starts one; it ends at the `end` phase (before it, or including it) inside the
    stretch up to the next begin, or without an end at the next begin. It only counts when every phase in it has
    (nearly) complete data and is finished, contains every `must` phase and no `exclude` phase, and (with
    `woken_by`) begins in a wake with one of those wake reasons."""
    names = [r.get("name") for r in rules]
    used = [t.get("begin"), *(t.get("must") or []), *(t.get("exclude") or [])] + ([t["end"]["phase"]] if t.get("end") else [])
    missing = [n for n in used if n not in names]
    out = {"instances": [], "filtered": 0, "no_end": 0, "gaps": 0, "error": None}
    if missing:
        out["error"] = "unknown phase: " + ", ".join(dict.fromkeys(str(n) for n in missing))
        return out
    begin = names.index(t["begin"])
    end = names.index(t["end"]["phase"]) if t.get("end") else None
    include = bool(t.get("end") and t["end"].get("include"))
    must = {names.index(n) for n in t.get("must") or []}
    exclude = {names.index(n) for n in t.get("exclude") or []}
    woken_by = set(t.get("woken_by") or [])
    flat = [(p, c) for c in cycles for p in c.phases]
    begins = [k for k, (p, _) in enumerate(flat) if p.rule == begin]
    for b, s in enumerate(begins):
        nxt = begins[b + 1] if b + 1 < len(begins) else len(flat)
        if end is None:
            if b + 1 >= len(begins):
                continue  # runs to the end of the session: not finished
            e = nxt
        else:
            j = next((k for k in range(s + 1, nxt) if flat[k][0].rule == end), None)
            if j is None:
                out["no_end"] += 1
                continue
            e = j + 1 if include else j
        span = flat[s:e]
        if not span:
            continue
        # Data complete, and not the unfinished last phase of a cycle cut off by the end of the session.
        if not all(p.coverage > 0.98 and (c.closed or p is not c.phases[-1]) for p, c in span):
            out["gaps"] += 1
            continue
        seen = {p.rule for p, _ in span}
        if not must <= seen or exclude & seen or (woken_by and span[0][1].wake not in woken_by):
            out["filtered"] += 1
            continue
        out["instances"].append([s, e])
    return out


# cycle type summaries and battery mix: the only copy (the pages and the MCP tools get these numbers) -------------
@dataclass
class Inst:
    """One cycle of a type: a run of phases (with the index of the wake each is in)."""
    k: int
    phases: list[tuple[Phase, int]]
    ok: bool
    wake: str | None = None  # wake reason of the wake it begins in

    @property
    def t0(self) -> float:
        return self.phases[0][0].t

    @property
    def t1(self) -> float:
        return self.phases[-1][0].end_t

    @property
    def dur(self) -> float:
        return self.t1 - self.t0

    @property
    def charge(self) -> float:
        return sum(p.charge_c for p, _ in self.phases)


def type_views(cycles: list[Cycle], rules: list[dict], types: list[dict]) -> list[dict]:
    """[All wakes, then each type]: {name (None = all wakes), kind, insts, error, skipped}."""
    views = [{"name": None, "kind": "wake", "error": None,
              "insts": [Inst(i, [(p, i) for p in c.phases], is_complete(c), c.wake) for i, c in enumerate(cycles) if c.phases],
              "skipped": {"incomplete": sum(1 for c in cycles if not is_complete(c))}}]
    flat = [(p, ci) for ci, c in enumerate(cycles) for p in c.phases]
    for t in types:
        r = type_instances(cycles, rules, t)
        views.append({"name": t["name"], "kind": type_kind(t, rules), "error": r["error"],
                      "insts": [Inst(k, flat[s:e], True, cycles[flat[s][1]].wake) for k, (s, e) in enumerate(r["instances"])],
                      "skipped": {"filtered": r["filtered"], "no_end": r["no_end"], "gaps": r["gaps"]}})
    return views


def _q(xs: list[float], q: float) -> float | None:
    return float(np.quantile(xs, q)) if xs else None


def phase_rows(insts: list[Inst], n_rules: int) -> list[dict]:
    """Per phase over these cycles: duration and mean current over the cycles that have it, charge per cycle over
    all of them (so the rows add up), and how often it occurs in a cycle that has it."""
    n = len(insts)
    rows = []
    for rule in range(n_rules):
        seen = occ = 0
        sd = sq = 0.0
        peak = None
        for x in insts:
            ps = [p for p, _ in x.phases if p.rule == rule]
            if not ps:
                continue
            seen += 1
            occ += len(ps)
            for p in ps:
                sd += p.end_t - p.t
                sq += p.charge_c
                if p.peak_a is not None:
                    peak = p.peak_a if peak is None else max(peak, p.peak_a)
        rows.append({"rule": rule, "seen": seen, "n": n, "duration_s": sd / seen if seen else None,
                     "duration_per_cycle_s": sd / n if n else 0.0, "mean_a": sq / sd if sd > 0 else None,
                     "charge_c": sq / n if n else 0.0, "times": occ / seen if seen else 0.0, "peak_a": peak})
    return rows


def type_summary(view: dict, rules: list[dict]) -> dict:
    xs = [x for x in view["insts"] if x.ok]
    durs = [x.dur for x in xs]
    qs = [x.charge for x in xs]
    total_q = sum(qs)
    rows = [r for r in phase_rows(xs, len(rules)) if r["seen"] or view["name"] is None]
    out_phases = [{"phase": rules[r["rule"]]["name"], "rule": r["rule"], "in_cycles": r["seen"], "duration_s": r["duration_s"],
                   "mean_a": r["mean_a"], "charge_per_cycle_c": r["charge_c"],
                   "share": r["charge_c"] * len(xs) / total_q if total_q > 0 else None,
                   **({"times_per_cycle": r["times"]} if r["times"] > 1.05 else {}), "peak_a": r["peak_a"]} for r in rows]
    # Unusual cycles: duration or charge far outside the middle half (3x its width, at least 10 % of typical).
    unusual = []
    if len(xs) >= 5:
        fences = []
        for vals in (durs, qs):
            q1, q3, med = np.quantile(vals, [0.25, 0.75, 0.5])
            f = max(3 * (q3 - q1), 0.1 * abs(med))
            fences.append((q1 - f, q3 + f, med))
        for x in xs:
            dev = max(abs(x.dur - fences[0][2]) / (fences[0][2] or 1), abs(x.charge - fences[1][2]) / (fences[1][2] or 1))
            if not (fences[0][0] <= x.dur <= fences[0][1]) or not (fences[1][0] <= x.charge <= fences[1][1]):
                unusual.append((dev, {"index": x.k, "start_s": x.t0, "duration_s": x.dur, "charge_c": x.charge}))
        unusual = [u for _, u in sorted(unusual, key=lambda u: -u[0])[:10]]
    return {
        "type": view["name"] or "All wakes", "kind": view["kind"], "error": view["error"],
        "cycles": len(view["insts"]), "usable": len(xs), "skipped": view["skipped"],
        "duration_s": {"mean": float(np.mean(durs)) if xs else None, "typical": _q(durs, 0.5), "p10": _q(durs, 0.1), "p90": _q(durs, 0.9)},
        "charge_c": {"mean": float(np.mean(qs)) if xs else None, "typical": _q(qs, 0.5), "p10": _q(qs, 0.1), "p90": _q(qs, 0.9)},
        "mean_a": total_q / sum(durs) if xs and sum(durs) > 0 else None,
        # The cycle with the median charge: a "typical" one (index within the type).
        "typical_index": sorted(xs, key=lambda x: x.charge)[len(xs) // 2].k if xs else None,
        "woken_by": count_wakes(x.wake for x in xs),
        "phases": out_phases,
        "unusual": unusual,
    }


def sleep_rule(all_view: dict, n_rules: int) -> int:
    """The sleep phase: the lowest average current over all usable wakes."""
    rows = [r for r in phase_rows([x for x in all_view["insts"] if x.ok], n_rules) if r["mean_a"] is not None]
    return min(rows, key=lambda r: r["mean_a"])["rule"] if rows else -1


def mix_parts(views: list[dict]) -> list[dict]:
    """The whole-wake types when together they are exactly the usable wakes (each in one type), else all wakes."""
    all_ok = sorted(x.k for x in views[0]["insts"] if x.ok)
    wakes = [v for v in views[1:] if v["kind"] == "wake" and not v["error"] and v["insts"]]
    covered = [x.phases[0][1] for v in wakes for x in v["insts"]]
    if len(wakes) >= 2 and sorted(covered) == all_ok:
        return wakes
    return [views[0]]


def mix_current(parts: list[dict], n_rules: int, sleep: int, sleep_a: float, sleep_s: float | None = None,
                skip: tuple[int, ...] = (), pick: str | None = None, every: float | None = None) -> float | None:
    """Average current of the mix of wakes; what-ifs: another sleep time, phases gone, a type every N wakes."""
    usable = [[x for x in v["insts"] if x.ok] for v in parts]
    sizes = [len(u) for u in usable]
    total = sum(sizes)
    if not total:
        return None
    pi = next((i for i, v in enumerate(parts) if v["name"] == pick), -1) if pick is not None and every and every >= 1 else -1
    q = d = 0.0
    for i, xs in enumerate(usable):
        f = sizes[i] / total
        if pi >= 0:
            rest = total - sizes[pi]
            f = 1 / every if i == pi else (sizes[i] / rest) * (1 - 1 / every) if rest else 0.0
        if not f:
            continue
        rows = phase_rows(xs, n_rules)
        cq = sum(r["charge_c"] for r in rows)
        cd = sum(r["duration_per_cycle_s"] for r in rows)
        for k in skip:
            cq -= rows[k]["charge_c"]
            cd -= rows[k]["duration_per_cycle_s"]
        if sleep_s is not None and sleep >= 0:
            cq += sleep_a * sleep_s - rows[sleep]["charge_c"]
            cd += sleep_s - rows[sleep]["duration_per_cycle_s"]
        q += f * cq
        d += f * cd
    return q / d if d > 0 else None
