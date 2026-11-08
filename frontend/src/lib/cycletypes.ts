// Cycle types on the client: the api finds each type's cycles (as ranges of phases) and works out all averages,
// the typical cycle, the sleep phase and battery life (one copy of that maths, shared with the MCP tools). This
// only shapes the cycles for the charts and steppers, and totals a single cycle per phase.
import type { CycleRow, PhasesResult, TypeResult, TypeSummary } from './api';

export type FlatPhase = { rule: number; t: number; end_t: number; charge: number; mean: number | null; peak: number | null; ci: number };

/** One cycle of a type: a run of phases, possibly over several wakes (a period) or part of one (a stretch). */
export type Instance = {
  /** Index within its type. */
  k: number;
  t0: number;
  t1: number;
  dur: number;
  charge: number;
  /** The wakes (cycle rows) it covers, first and last. */
  first: number;
  last: number;
  phases: FlatPhase[];
  /** Usable for averages (always true for saved types; "All wakes" also lists incomplete wakes). */
  ok: boolean;
};

export type TypeView = {
  /** '' = All wakes, otherwise the type's name. */
  key: string;
  name: string;
  kind: 'wake' | 'period' | 'stretch';
  instances: Instance[];
  error: string | null;
  skipped: { filtered: number; no_end: number; gaps: number };
};

export const ALL = '';

/** Every phase of every cycle, in order: the api's type ranges index into this list. */
export function flatPhases(cycles: CycleRow[]): FlatPhase[] {
  const out: FlatPhase[] = [];
  for (const c of cycles) for (const p of c.phases) out.push({ rule: p[0], t: p[1], end_t: p[2], charge: p[3], mean: p[4], peak: p[5], ci: c.i });
  return out;
}

function instance(k: number, phases: FlatPhase[], ok: boolean): Instance {
  const t0 = phases[0].t;
  const t1 = phases[phases.length - 1].end_t;
  return { k, t0, t1, dur: t1 - t0, charge: phases.reduce((a, p) => a + p.charge, 0), first: phases[0].ci, last: phases[phases.length - 1].ci, phases, ok };
}

function fromResult(flat: FlatPhase[], r: TypeResult): TypeView {
  return {
    key: r.name,
    name: r.name,
    kind: r.kind,
    instances: r.instances.map(([s, e], k) => instance(k, flat.slice(s, e), true)),
    error: r.error,
    skipped: { filtered: r.filtered, no_end: r.no_end, gaps: r.gaps },
  };
}

/** "All wakes" (every cycle; the incomplete ones are shown but not averaged) and then the profile's types. */
export function typeViews(res: PhasesResult | null): TypeView[] {
  if (!res?.cycles.length) return [];
  const flat = flatPhases(res.cycles);
  let pos = 0;
  const all: TypeView = {
    key: ALL,
    name: 'All wakes',
    kind: 'wake',
    instances: res.cycles.map((c, k) => {
      const ps = flat.slice(pos, pos + c.phases.length);
      pos += c.phases.length;
      return ps.length ? instance(k, ps, c.complete) : { k, t0: c.t, t1: c.end_t, dur: c.end_t - c.t, charge: 0, first: c.i, last: c.i, phases: [], ok: false };
    }),
    error: null,
    skipped: { filtered: 0, no_end: 0, gaps: res.cycles.filter((c) => !c.complete).length },
  };
  return [all, ...(res.types ?? []).map((r) => fromResult(flat, r))];
}

export const usable = (v: TypeView) => v.instances.filter((x) => x.ok);

/** The api's averages for a type (All wakes = the first). */
export function statsFor(res: PhasesResult | null | undefined, key: string): TypeSummary | undefined {
  if (!res?.stats?.length) return undefined;
  return key === ALL ? res.stats[0] : res.stats.slice(1).find((s) => s.type === key);
}

/** A row of a phase table. dur/mean: over the cycles that have the phase (seen of n); charge: per cycle over all n,
 *  so the rows add up; times: how often it occurs in a cycle that has it (periods). */
export type PhaseRowView = { rule: number; seen: number; n: number; dur: number | null; mean: number | null; charge: number; times: number; peak: number | null };

/** The api's per-phase averages of a type as table rows. */
export function avgRows(s: TypeSummary | undefined): PhaseRowView[] {
  if (!s) return [];
  return s.phases.map((p) => ({ rule: p.rule, seen: p.in_cycles, n: s.usable, dur: p.duration_s, mean: p.mean_a, charge: p.charge_per_cycle_c, times: p.times_per_cycle ?? 1, peak: p.peak_a }));
}

/** One cycle's phases totalled per phase (a period can have the same phase several times). */
export function cycleRows(x: Instance, nRules: number): PhaseRowView[] {
  return Array.from({ length: nRules }, (_, rule) => {
    const ps = x.phases.filter((p) => p.rule === rule);
    const d = ps.reduce((a, p) => a + p.end_t - p.t, 0);
    const q = ps.reduce((a, p) => a + p.charge, 0);
    const peaks = ps.map((p) => p.peak).filter((v): v is number => v != null);
    return { rule, seen: ps.length ? 1 : 0, n: 1, dur: ps.length ? d : null, mean: d > 0 ? q / d : null, charge: q, times: ps.length || 1, peak: peaks.length ? Math.max(...peaks) : null };
  });
}

/** A cycle of any type in the shape Compare works with (a wake's CycleRow), plus the wake it begins in. */
export type UnitRow = CycleRow & { wakeIndex: number };
export function asCycleRow(x: Instance): UnitRow {
  return {
    i: x.k, t: x.t0, end_t: x.t1, closed: true, complete: x.ok, charge_c: x.charge,
    mean_a: x.dur > 0 ? x.charge / x.dur : null,
    phases: x.phases.map((p) => [p.rule, p.t, p.end_t, p.charge, p.mean, p.peak, 1]),
    wakeIndex: x.first,
  };
}

/** The typical cycle of a type (median charge, from the api); with `not`, a usable one next to it instead. */
export function typicalIndex(v: TypeView | undefined, s: TypeSummary | undefined, not = -1): number | null {
  if (!v?.instances.length) return null;
  const t = s?.typical_index ?? null;
  if (t != null && t !== not) return t;
  const xs = usable(v).filter((x) => x.k !== not);
  if (!xs.length) return 0;
  return (xs.find((x) => x.k > (t ?? -1)) ?? xs[xs.length - 1]).k;
}
