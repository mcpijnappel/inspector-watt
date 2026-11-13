<script lang="ts">
  // Compare two cycles (same or different sessions): overlay, alignment with fine-tuning, per-phase differences.
  //
  // Chart x = seconds since A's cycle start. B is drawn at (seconds since B's cycle start) + offset, so a
  // positive offset moves B later. Auto-align computes the offset; −/+ and the arrow keys fine-tune it.
  import { untrack } from 'svelte';
  import CompareChart from '../components/CompareChart.svelte';
  import CompareLog from '../components/CompareLog.svelte';
  import ChipLanes from '../components/ChipLanes.svelte';
  import type { BusEvent } from '../lib/bus.svelte';
  import type { Chips } from '../lib/busfold';
  import { library } from '../lib/chips.svelte';
  import { lanesPref, pairLanes, setLanesPref, type ChartScale } from '../lib/lanes';
  import { api, type CycleRow, type PhasesResult, type SessionSummary } from '../lib/api';
  import { ALL, asCycleRow, avgRows, flatPhases, statsFor, typeViews, typicalIndex, usable, type FlatPhase, type TypeView, type UnitRow } from '../lib/cycletypes';
  import { fmtAmps, fmtCharge, fmtSpan } from '../lib/format';
  import type { Range } from '../lib/live.svelte';
  import { router } from '../lib/router.svelte';
  import type { ChartBand, Series } from '../lib/types';

  type Side = { sid: number | null; cycle: number | null };
  type Key = 'a' | 'b';
  const STEPS: [number, string][] = [[1e-5, '10 µs'], [1e-3, '1 ms'], [1e-2, '10 ms'], [0.1, '100 ms']];
  const GREY = '#8b949e';
  const KEYS: Key[] = ['a', 'b'];

  // state ----------------------------------------------------------------------------------------------------
  const q = router.route.query;
  let sessions = $state<SessionSummary[]>([]);
  let phases = $state<Record<number, PhasesResult>>({});
  let side = $state<Record<Key, Side>>({ a: parseSide(q.get('a')), b: parseSide(q.get('b')) });
  /** The cycle type both sides step through ('' = all wakes); side.cycle is an index in that type's list. */
  let typeKey = $state(q.get('type') ?? ALL);
  /** What the chart shows: 'awake' (up to the sleep phase), 'whole' (the cycle), 'phase:<name>' or 'type:<name>'
   *  (a stretch type: the stretch that begins in the cycle). */
  let show = $state(q.get('show') ?? (loadPref('pst.cmp.awake', '1') === '1' ? 'awake' : 'whole'));
  let log = $state(true);
  let align = $state(q.get('align') ?? 'start');
  let lineText = $state(q.get('line') ?? '');
  /** Threshold for "current rises above", in mA as typed; empty = the session's wake threshold. */
  let thrText = $state(q.get('thr') ?? '');
  let autoD = $state(0);
  let nudge = $state(Number(q.get('n') ?? 0) || 0);
  let step = $state(Number(loadPref('pst.cmp.step', '0.001')) || 1e-3);
  let zoom = $state<Range | null>(null);
  let tableMode = $state<'cycles' | 'avg'>('cycles');
  let alignNote = $state('');
  let error = $state('');
  let loading = $state(false);
  // bus messages of both cycles (stored with their sessions), as chip lanes under the overlay
  let busA = $state.raw<BusEvent[]>([]);
  let busB = $state.raw<BusEvent[]>([]);
  let chipsA = $state<Chips>({});
  let chipsB = $state<Chips>({});
  let scale = $state<ChartScale | null>(null);
  let laneHover = $state<{ a: number; b: number; label: string } | null>(null);
  let showLanes = $state(lanesPref());
  let traceA = $state.raw<Series>({ x: [], y: [] });
  let traceB = $state.raw<Series>({ x: [], y: [] });
  let version = $state(0);
  /** Mouse over the chart (x), and over a log line (x): they mark each other. */
  let chartHover = $state<number | null>(null);
  let logHover = $state<number | null>(null);

  const d = $derived(autoD + nudge);

  function parseSide(v: string | null): Side {
    const m = /^(\d+)(?::(\d+))?$/.exec(v ?? '');
    return m ? { sid: Number(m[1]), cycle: m[2] ? Number(m[2]) - 1 : null } : { sid: null, cycle: null };
  }
  function loadPref(key: string, fallback: string): string {
    try {
      return localStorage.getItem(key) ?? fallback;
    } catch {
      return fallback;
    }
  }
  function savePref(key: string, v: string) {
    try {
      localStorage.setItem(key, v);
    } catch { /* storage blocked */ }
  }

  // loading --------------------------------------------------------------------------------------------------
  async function loadPhases(sid: number): Promise<PhasesResult | null> {
    if (phases[sid]) return phases[sid];
    try {
      const r = await api.phases(sid);
      phases = { ...phases, [sid]: r };
      return r;
    } catch (e) {
      error = (e as Error).message;
      return null;
    }
  }

  /** A session's cycles of the chosen type (all wakes if its profile doesn't have the type). */
  function viewFor(r: PhasesResult | undefined): TypeView | undefined {
    const vs = typeViews(r ?? null);
    return vs.find((v) => v.key === typeKey) ?? vs[0];
  }
  function typical(r: PhasesResult, not = -1): number | null {
    const v = viewFor(r);
    return typicalIndex(v, statsFor(r, v?.key ?? ALL), not);
  }
  /** Default B in A's session: the next usable cycle after A (with the shown stretch, if one is shown). */
  function nextAfter(r: PhasesResult, after: number): number | undefined {
    const units = (viewFor(r)?.instances ?? []).map(asCycleRow);
    const has = (c: UnitRow) => !effShow.startsWith('type:') || !!stretch(c, r, effShow.slice(5));
    return units.find((c, i) => c.complete && i > after && has(c))?.i;
  }

  $effect(() => {
    untrack(async () => {
      try {
        sessions = await api.sessions();
      } catch (e) {
        error = (e as Error).message;
        return;
      }
      // No sessions given: the newest session with cycles, two typical cycles of it.
      if (side.a.sid == null) {
        for (const s of sessions) {
          const r = await loadPhases(s.id);
          if (r?.cycles.length) {
            side.a = { sid: s.id, cycle: null };
            if (side.b.sid == null) side.b = { sid: s.id, cycle: null };
            break;
          }
        }
      }
      if (side.b.sid == null) side.b = { ...side.a, cycle: null };
      const [ra, rb] = await Promise.all([side.a.sid != null ? loadPhases(side.a.sid) : null, side.b.sid != null ? loadPhases(side.b.sid) : null]);
      if (ra && side.a.cycle == null) side.a.cycle = typical(ra);
      if (rb && side.b.cycle == null) {
        // Same session: the next usable cycle after A, so the two differ.
        const same = side.b.sid === side.a.sid;
        const next = same ? nextAfter(rb, side.a.cycle ?? -1) : undefined;
        side.b.cycle = next ?? typical(rb, same ? (side.a.cycle ?? -1) : -1);
      }
    });
  });

  const resA = $derived(side.a.sid != null ? phases[side.a.sid] : undefined);
  const resB = $derived(side.b.sid != null ? phases[side.b.sid] : undefined);
  const viewA = $derived(viewFor(resA));
  const viewB = $derived(viewFor(resB));
  const unitsA = $derived((viewA?.instances ?? []).map(asCycleRow));
  const unitsB = $derived((viewB?.instances ?? []).map(asCycleRow));
  const cycA = $derived<UnitRow | null>(side.a.cycle != null ? (unitsA[side.a.cycle] ?? null) : null);
  const cycB = $derived<UnitRow | null>(side.b.cycle != null ? (unitsB[side.b.cycle] ?? null) : null);
  /** Types both sessions' profiles have (by name), working in both. */
  const sharedTypes = $derived.by(() => {
    const a = (resA?.types ?? []).filter((t) => !t.error);
    return a.filter((t) => resB?.types?.some((u) => u.name === t.name && !u.error)).map((t) => ({ name: t.name, kind: t.kind }));
  });
  const kind = $derived(typeKey ? (sharedTypes.find((t) => t.name === typeKey)?.kind ?? 'wake') : 'wake');
  const unitName = $derived(typeKey || 'Cycle');
  // A session whose profile doesn't have the chosen type: back to all wakes.
  $effect(() => {
    if (typeKey && resA && resB && !sharedTypes.some((t) => t.name === typeKey)) untrack(() => setType(ALL));
  });
  function setType(k: string) {
    typeKey = k;
    zoom = null;
    const ra = resA;
    const rb = resB;
    if (!ra || !rb) return;
    side.a = { ...side.a, cycle: typical(ra) };
    const same = side.b.sid === side.a.sid;
    side.b = { ...side.b, cycle: (same ? nextAfter(rb, side.a.cycle ?? -1) : undefined) ?? typical(rb, same ? (side.a.cycle ?? -1) : -1) };
  }
  const sessA = $derived(sessions.find((s) => s.id === side.a.sid));
  const sessB = $derived(sessions.find((s) => s.id === side.b.sid));

  const sleepRule = (r: PhasesResult | undefined): number => r?.sleep_rule ?? -1;
  /** Length of a cycle's awake part: up to its (last) sleep phase. */
  function awakeLen(c: CycleRow, r: PhasesResult): number {
    const sleep = [...c.phases].reverse().find((p) => p[0] === sleepRule(r));
    return (sleep ? sleep[1] : c.end_t) - c.t;
  }
  /** What's shown, as it applies to this type: periods and stretches have no awake part or inner stretch. */
  const effShow = $derived(kind !== 'wake' && (show === 'awake' || show.startsWith('type:')) ? 'whole' : show);
  const flats = new WeakMap<PhasesResult, FlatPhase[]>();
  /** The stretch of a stretch type that begins in cycle c's (first) wake, as session seconds. */
  function stretch(c: UnitRow, r: PhasesResult, name: string): [number, number] | null {
    const t = r.types?.find((x) => x.name === name && x.kind === 'stretch');
    if (!t) return null;
    let flat = flats.get(r);
    if (!flat) flats.set(r, (flat = flatPhases(r.cycles)));
    const hit = t.instances.find(([a]) => flat![a]?.ci === c.wakeIndex);
    return hit ? [flat[hit[0]].t, flat[hit[1] - 1].end_t] : null;
  }
  /** The shown part of a cycle in seconds since its start, or null when the shown phase isn't in it. */
  function part(c: UnitRow, r: PhasesResult): [number, number] | null {
    if (effShow.startsWith('type:')) {
      const st = stretch(c, r, effShow.slice(5));
      return st ? [st[0] - c.t, st[1] - c.t] : null;
    }
    if (effShow.startsWith('phase:')) {
      const k = r.profile?.rules.findIndex((x) => x.name === effShow.slice(6)) ?? -1;
      const p = c.phases.find((x) => x[0] === k);
      return p ? [p[1] - c.t, p[2] - c.t] : null;
    }
    return [0, effShow === 'whole' ? c.end_t - c.t : awakeLen(c, r)];
  }
  const showNote = $derived.by(() => {
    if (!(effShow.startsWith('phase:') || effShow.startsWith('type:')) || !cycA || !cycB || !resA || !resB) return '';
    const missA = !part(cycA, resA);
    const missB = !part(cycB, resB);
    if (!missA && !missB) return '';
    const what = effShow.slice(effShow.indexOf(':') + 1);
    return `${what} isn't in cycle ${missA && missB ? 'A or B' : missA ? 'A' : 'B'}: that side shows its awake part.`;
  });
  const wholeLabel = $derived(kind === 'stretch' ? `Whole ${typeKey}` : kind === 'period' ? 'Whole period' : 'Whole cycle');
  const showLabel = $derived(effShow === 'awake' ? 'Awake part' : effShow === 'whole' ? wholeLabel : effShow.startsWith('type:') ? effShow.slice(5) : `Phase: ${effShow.slice(6)}`);

  function setShow(v: string) {
    show = v;
    if (v === 'awake' || v === 'whole') savePref('pst.cmp.awake', v === 'awake' ? '1' : '0');
    else if (v.startsWith('type:')) {
      // A stretch: align on the phase it begins at.
      const begin = resA?.profile?.types?.find((t) => t.name === v.slice(5))?.begin;
      if (begin && sharedPhases.includes(begin)) align = `phase:${begin}`;
    } else align = v; // a phase: align on its start (same "phase:<name>" value)
  }

  const full = $derived.by<Range>(() => {
    if (!cycA || !cycB || !resA || !resB) return { a: 0, b: 1 };
    const pa = part(cycA, resA) ?? [0, awakeLen(cycA, resA)];
    const pb = part(cycB, resB) ?? [0, awakeLen(cycB, resB)];
    const a = Math.min(pa[0], pb[0] + d);
    const b = Math.max(pa[1], pb[1] + d);
    const pad = Math.min(Math.max(effShow.startsWith('phase:') || effShow.startsWith('type:') || kind === 'stretch' ? 0.002 : 0.02, (b - a) * 0.02), 2);
    // A whole cycle or period ends where the next one starts: padding there would show the next wake.
    return { a: a - pad, b: effShow === 'whole' && kind !== 'stretch' ? b : b + pad };
  });
  const view = $derived(zoom ?? full);

  $effect(() => {
    const dev = sessA?.device_id;
    if (dev != null && busA.length) api.busChips(dev).then((c) => (chipsA = c)).catch(() => {});
  });
  $effect(() => {
    const dev = sessB?.device_id;
    if (dev != null && busB.length) api.busChips(dev).then((c) => (chipsB = c)).catch(() => {});
  });
  const P = 1e-5;
  const lanes = $derived.by(() => {
    if (!showLanes || !cycA || !cycB) return [];
    const ta = cycA.t, tb = cycB.t, off = d;
    return pairLanes([busA, busB], [chipsA, chipsB], [(i) => i * P - ta, (i) => i * P - tb + off]);
  });

  // Traces for the view, both sides. Debounced: nudging fires many changes.
  let traceToken = 0;
  $effect(() => {
    const v = view;
    const off = d;
    const ca = cycA;
    const cb = cycB;
    if (!ca || !cb || side.a.sid == null || side.b.sid == null) return;
    const sa = side.a.sid;
    const sb = side.b.sid;
    const token = ++traceToken;
    const t = setTimeout(async () => {
      loading = true;
      try {
        const [ta, tb, ba, bb] = await Promise.all([
          api.trace(sa, Math.max(0, ca.t + v.a), ca.t + v.b, 1500),
          api.trace(sb, Math.max(0, cb.t + v.a - off), cb.t + v.b - off, 1500),
          api.sessionBus(sa, Math.max(0, ca.t + v.a), ca.t + v.b).catch(() => null),
          api.sessionBus(sb, Math.max(0, cb.t + v.a - off), cb.t + v.b - off).catch(() => null),
        ]);
        if (token !== traceToken) return;
        busA = ba?.messages ?? [];
        busB = bb?.messages ?? [];
        if (busA.length || busB.length) library.ensure();
        traceA = { x: ta.x.map((x) => x - ca.t), y: ta.mean };
        traceB = { x: tb.x.map((x) => x - cb.t + off), y: tb.mean };
        version++;
        error = '';
      } catch (e) {
        if (token === traceToken) error = (e as Error).message;
      } finally {
        if (token === traceToken) loading = false;
      }
    }, 120);
    return () => clearTimeout(t);
  });

  // alignment ------------------------------------------------------------------------------------------------
  const rulesA = $derived(resA?.profile?.rules ?? []);
  const rulesB = $derived(resB?.profile?.rules ?? []);
  const sharedPhases = $derived(rulesA.map((r) => r.name).filter((n) => rulesB.some((r) => r.name === n)));
  /** Stretch types both sides' profiles have (by name). */
  const sharedStretches = $derived(kind === 'wake' ? sharedTypes.filter((t) => t.kind === 'stretch').map((t) => t.name) : []);

  function phaseStart(c: CycleRow, r: PhasesResult, name: string): number | null {
    const k = r.profile?.rules.findIndex((x) => x.name === name) ?? -1;
    const p = c.phases.find((x) => x[0] === k);
    return p ? p[1] - c.t : null;
  }

  function sessionThreshold(s: SessionSummary | undefined): number {
    const v = Number(s?.config?.wake_threshold_a);
    return Number.isFinite(v) && v > 0 ? v : 5e-3;
  }
  const threshold = $derived.by(() => {
    const v = parseFloat(thrText.replace(',', '.'));
    return Number.isFinite(v) && v > 0 ? v * 1e-3 : sessionThreshold(sessA);
  });

  /** First upward crossing of `thr` since the device went to sleep in the previous cycle (seconds since this
   *  cycle's start). The wake often begins before the cycle's first log line, sometimes by seconds. */
  async function riseTime(sid: number, r: PhasesResult, c: UnitRow, thr: number, len: number): Promise<number | null> {
    const sleep = r.cycles[c.wakeIndex - 1]?.phases.find((p) => p[0] === sleepRule(r));
    const a = Math.max(0, sleep ? sleep[1] : c.t - 5, c.t - 900);
    const b = c.t + Math.min(len, 60);
    const coarse = await api.trace(sid, a, b, 4000);
    const find = (x: number[], ys: (number | null)[]) => {
      for (let i = 1; i < ys.length; i++) {
        const p = ys[i - 1];
        const v = ys[i];
        if (p != null && v != null && p <= thr && v > thr) return x[i];
      }
      return null;
    };
    const t0 = find(coarse.x, coarse.max);
    if (t0 == null) return null;
    // Refine in a window around it (raw data when the session has it).
    const w = Math.max(coarse.bucket_s * 2, 2e-3);
    const fine = await api.trace(sid, Math.max(0, t0 - w), t0 + w, 2000);
    return (find(fine.x, fine.max) ?? t0) - c.t;
  }

  async function lineTime(sid: number, c: CycleRow, text: string): Promise<number | null> {
    const lines = await api.sessionUart(sid, c.t, c.end_t, 1, text);
    return lines.length ? lines[0].t - c.t : null;
  }

  let alignToken = 0;
  async function autoAlign() {
    const ca = cycA;
    const cb = cycB;
    const ra = resA;
    const rb = resB;
    if (!ca || !cb || !ra || !rb || side.a.sid == null || side.b.sid == null) return;
    const token = ++alignToken;
    alignNote = '';
    let ta: number | null = 0;
    let tb: number | null = 0;
    try {
      if (align === 'manual') {
        autoD = 0;
        return;
      } else if (align.startsWith('phase:')) {
        const name = align.slice(6);
        ta = phaseStart(ca, ra, name);
        tb = phaseStart(cb, rb, name);
      } else if (align === 'current') {
        const thr = threshold;
        [ta, tb] = await Promise.all([riseTime(side.a.sid, ra, ca, thr, awakeLen(ca, ra)), riseTime(side.b.sid, rb, cb, thr, awakeLen(cb, rb))]);
      } else if (align === 'line') {
        if (!lineText.trim()) {
          alignNote = 'Type part of a log line, then press Enter.';
          return;
        }
        [ta, tb] = await Promise.all([lineTime(side.a.sid, ca, lineText.trim()), lineTime(side.b.sid, cb, lineText.trim())]);
      }
    } catch (e) {
      if (token === alignToken) alignNote = (e as Error).message;
      return;
    }
    if (token !== alignToken) return;
    if (ta == null || tb == null) {
      const where = ta == null && tb == null ? 'A or B' : ta == null ? 'A' : 'B';
      const why = align === 'current'
        ? `the current in ${where} never rises above ${fmtAmps(threshold)}: it doesn't drop below that while asleep. Try a higher threshold`
        : align === 'line' ? `no log line in ${where} contains "${lineText.trim()}"` : `${where} has no such phase in this cycle`;
      alignNote = `Can't align: ${why}. Aligned on the cycle start instead.`;
      autoD = 0;
      return;
    }
    autoD = ta - tb;
  }

  // Re-align when the cycles or the method change (not on every nudge).
  let firstAlign = true;
  $effect(() => {
    void cycA;
    void cycB;
    void align;
    void effShow;
    untrack(() => {
      if (!firstAlign) nudge = 0;
      if (cycA && cycB) firstAlign = false;
      zoom = null;
      void autoAlign();
    });
  });

  function nudgeBy(dir: number) {
    nudge = Math.round((nudge + dir * step) * 1e5) / 1e5;
  }

  function onkey(e: KeyboardEvent) {
    const t = e.target as HTMLElement;
    if (t.closest('input, select, textarea')) return;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      nudgeBy(e.key === 'ArrowLeft' ? -1 : 1);
    }
  }

  // Keep the URL in step, so a comparison can be reopened or shared.
  $effect(() => {
    const p = new URLSearchParams();
    if (side.a.sid != null) p.set('a', `${side.a.sid}${side.a.cycle != null ? `:${side.a.cycle + 1}` : ''}`);
    if (side.b.sid != null) p.set('b', `${side.b.sid}${side.b.cycle != null ? `:${side.b.cycle + 1}` : ''}`);
    if (align !== 'start') p.set('align', align);
    if (show !== 'awake') p.set('show', show);
    if (typeKey) p.set('type', typeKey);
    if (align === 'line' && lineText) p.set('line', lineText);
    if (align === 'current' && thrText) p.set('thr', thrText);
    if (nudge) p.set('n', String(nudge));
    const hash = `#/compare?${p.toString()}`;
    if (location.hash !== hash) history.replaceState(null, '', hash);
  });

  // picking --------------------------------------------------------------------------------------------------
  async function pickSession(k: Key, sid: number) {
    const r = await loadPhases(sid);
    side[k] = { sid, cycle: r ? typical(r) : null };
  }
  function stepCycle(k: Key, dir: number) {
    const units = k === 'a' ? unitsA : unitsB;
    const c = side[k].cycle;
    if (c == null) return;
    const n = c + dir;
    if (n >= 0 && n < units.length) side[k] = { ...side[k], cycle: n };
  }
  function swap() {
    const a = side.a;
    side.a = side.b;
    side.b = a;
  }

  // table + summary ------------------------------------------------------------------------------------------
  /** seen/n: in how many of the averaged cycles the phase occurs. */
  type Val = { dur: number | null; charge: number | null; seen?: number; n?: number; times?: number };
  // Averages per side over the usable cycles of the type, from the api (as on the session page): duration over the
  // cycles that have a phase, charge per cycle over all of them, so the rows add up to the total.
  const statsOf = (v: TypeView | undefined, r: PhasesResult | undefined) => (v && r ? statsFor(r, v.key) : undefined);
  const avgA = $derived(avgRows(statsOf(viewA, resA)));
  const avgB = $derived(avgRows(statsOf(viewB, resB)));
  function avgTotal(v: TypeView | undefined, r: PhasesResult | undefined): Val & { mean: number | null } {
    const st = statsOf(v, r);
    if (!st?.usable) return { dur: null, charge: null, mean: null, n: 0 };
    return { dur: st.duration_s.mean, charge: st.charge_c.mean, mean: st.mean_a, n: st.usable };
  }
  type Row = { name: string; color: string; a: Val; b: Val; delta: number | null; big: boolean; main: boolean };

  function phaseVals(r: PhasesResult | undefined, c: CycleRow | null, name: string): Val {
    const k = r?.profile?.rules.findIndex((x) => x.name === name) ?? -1;
    if (k < 0 || !r) return { dur: null, charge: null };
    if (tableMode === 'avg') {
      const p = (r === resA ? avgA : avgB).find((x) => x.rule === k);
      return p && p.seen ? { dur: p.dur, charge: p.charge, seen: p.seen, n: p.n, times: p.times } : { dur: null, charge: null };
    }
    // Every time it occurs (a period has several boots and sleeps).
    const ps = c?.phases.filter((x) => x[0] === k) ?? [];
    return ps.length ? { dur: ps.reduce((a, p) => a + p[2] - p[1], 0), charge: ps.reduce((a, p) => a + p[3], 0), times: ps.length } : { dur: null, charge: null };
  }
  function totalVals(r: PhasesResult | undefined, c: CycleRow | null): Val {
    if (tableMode === 'avg') return r === resA ? avgTotal(viewA, resA) : avgTotal(viewB, resB);
    return c ? { dur: c.end_t - c.t, charge: c.charge_c } : { dur: null, charge: null };
  }

  const totA = $derived(totalVals(resA, cycA));
  const totB = $derived(totalVals(resB, cycB));
  const rows = $derived.by<Row[]>(() => {
    const names = [...rulesA.map((r) => r.name), ...rulesB.map((r) => r.name).filter((n) => !rulesA.some((r) => r.name === n))];
    const bigAt = Math.max(5e-4, Math.abs(totA.charge ?? 0) * 0.05);
    const out = names.map((name) => {
      const a = phaseVals(resA, cycA, name);
      const b = phaseVals(resB, cycB, name);
      const delta = a.charge != null && b.charge != null ? b.charge - a.charge : null;
      const color = rulesA.find((r) => r.name === name)?.color ?? rulesB.find((r) => r.name === name)?.color ?? GREY;
      return { name, color, a, b, delta, big: delta != null && Math.abs(delta) >= bigAt, main: false };
    }).filter((r) => !typeKey || r.a.dur != null || r.b.dur != null); // a type: only its own phases
    const total = (totB.charge ?? 0) - (totA.charge ?? 0);
    const top = out.filter((r) => r.big).sort((x, y) => Math.abs(y.delta ?? 0) - Math.abs(x.delta ?? 0))[0];
    if (top && Math.abs(top.delta ?? 0) >= Math.abs(total) * 0.4) top.main = true;
    return out;
  });
  const totalDelta = $derived(totA.charge != null && totB.charge != null ? totB.charge - totA.charge : null);

  const summary = $derived.by(() => {
    if (totalDelta == null || !totA.charge) return null;
    const unit = typeKey ? `${typeKey}${kind === 'stretch' ? '' : kind === 'period' ? ' period' : ''}` : 'cycle';
    const per = tableMode === 'avg' ? `per average ${unit}` : `per ${unit}`;
    const pct = (totalDelta / totA.charge) * 100;
    if (Math.abs(pct) < 1) return { head: `A and B use about the same charge ${per}`, detail: `(${fmtCharge(Math.abs(totalDelta))} apart).`, tail: '' };
    const head = `B uses ${fmtCharge(Math.abs(totalDelta))} (${Math.abs(pct).toFixed(0)} %) ${totalDelta < 0 ? 'less' : 'more'}`;
    const main = rows.find((r) => r.main);
    let tail = '';
    if (main) {
      const share = Math.abs((main.delta ?? 0) / totalDelta) * 100;
      const da = main.a.dur ?? 0;
      const db = main.b.dur ?? 0;
      const why = Math.abs(db - da) > 0.1 * Math.max(da, db)
        ? `it lasts ${fmtSpan(da)} in A and ${fmtSpan(db)} in B`
        : `same length, but ${fmtAmps((main.a.charge ?? 0) / (da || 1))} vs ${fmtAmps((main.b.charge ?? 0) / (db || 1))} on average`;
      tail = `${share >= 90 ? 'Almost all' : share >= 60 ? 'Most' : 'A large part'} of it is ${main.name}: ${why}.`;
    }
    return { head, detail: per + '.', tail };
  });

  // chart decorations ----------------------------------------------------------------------------------------
  function strips(c: CycleRow | null, r: PhasesResult | undefined, off: number): ChartBand[] {
    if (!c || !r) return [];
    return c.phases.map((p) => ({ a: p[1] - c.t + off, b: p[2] - c.t + off, color: r.profile?.rules[p[0]]?.color ?? GREY }));
  }
  const stripsA = $derived(strips(cycA, resA, 0));
  const stripsB = $derived(strips(cycB, resB, d));

  const fmtOffset = (s: number) => `${s >= 0 ? '+' : '−'}${Math.abs(s) < 1 ? `${(Math.abs(s) * 1e3).toFixed(2)} ms` : `${Math.abs(s).toFixed(5)} s`}`;
  const cycleText = (c: CycleRow | null) => (c ? `${fmtCharge(c.charge_c)} · ${fmtSpan(c.end_t - c.t)} · ${fmtAmps(c.mean_a)}` : '–');
  const avgLabel = (v: TypeView | undefined) => {
    const n = v ? usable(v).length : 0;
    return `average of ${n} ${typeKey || (n === 1 ? 'cycle' : 'cycles')}`;
  };
  const avgText = (v: TypeView | undefined, r: PhasesResult | undefined) => {
    const t = avgTotal(v, r);
    return t.charge != null ? `${fmtCharge(t.charge)} · ${fmtSpan(t.dur ?? 0)} · ${fmtAmps(t.mean)}` : '–';
  };
</script>

<svelte:window onkeydown={onkey} />

<div class="page">
  <div class="head">
    <h1>Compare</h1>
    <select class="typesel" value={typeKey} onchange={(e) => setType((e.target as HTMLSelectElement).value)} aria-label="Cycle type" title="Which kind of cycles to compare (cycle types are set per phase profile)">
      <option value="">All wakes</option>
      {#each sharedTypes as t (t.name)}<option value={t.name}>{t.name}</option>{/each}
    </select>
    <div class="pickers">
      {#each KEYS as k (k)}
        {@const r = k === 'a' ? resA : resB}
        {@const c = k === 'a' ? cycA : cycB}
        {@const units = k === 'a' ? unitsA : unitsB}
        <div class="pick" aria-label="Cycle {k.toUpperCase()}" role="group">
          <span class="sw {k}"></span><strong>{k.toUpperCase()}</strong>
          <select aria-label="Session {k.toUpperCase()}" value={side[k].sid} onchange={(e) => pickSession(k, Number((e.target as HTMLSelectElement).value))}>
            {#each sessions as s (s.id)}<option value={s.id}>#{s.id} {s.name}</option>{/each}
          </select>
          {#if units.length}
            <button class="btn icon small ghost" type="button" aria-label="Previous cycle {k.toUpperCase()}" disabled={!side[k].cycle} onclick={() => stepCycle(k, -1)}>‹</button>
            <span class="mono cyc">{unitName} {(side[k].cycle ?? 0) + 1} / {units.length}</span>
            <button class="btn icon small ghost" type="button" aria-label="Next cycle {k.toUpperCase()}" disabled={(side[k].cycle ?? Infinity) >= units.length - 1} onclick={() => stepCycle(k, 1)}>›</button>
            {#if c && !c.complete}<span class="warn" title="Incomplete cycle: data has a gap, or the session ended in it">⚠</span>{/if}
          {:else if r}
            <span class="faint">no {typeKey || 'cycles'}</span>
          {/if}
        </div>
        {#if k === 'a'}<button class="btn icon ghost" type="button" aria-label="Swap A and B" title="Swap A and B" onclick={swap}>⇄</button>{/if}
      {/each}
    </div>
  </div>

  {#if error}<div class="banner bad">{error}</div>{/if}
  {#if (resA && !resA.cycles.length) || (resB && !resB.cycles.length)}
    <div class="banner">No cycles in {resA && !resA.cycles.length ? `#${side.a.sid}` : `#${side.b.sid}`}: its phase rules don't match the log. Set them up on the session page.</div>
  {/if}

  <section class="panel result" aria-label="Result">
    <div class="seg" role="group" aria-label="Compare these cycles or the session averages">
      <button type="button" aria-pressed={tableMode === 'cycles'} onclick={() => (tableMode = 'cycles')}>These cycles</button>
      <button type="button" aria-pressed={tableMode === 'avg'} onclick={() => (tableMode = 'avg')}>Session averages{typeKey ? `: ${typeKey}` : ''}</button>
    </div>
    <div><span class="lbl"><i class="sw a"></i>A · {tableMode === 'avg' ? avgLabel(viewA) : unitName.toLowerCase() === 'cycle' ? 'cycle' : unitName}</span><div class="big mono">{tableMode === 'avg' ? avgText(viewA, resA) : cycleText(cycA)}</div></div>
    <div><span class="lbl"><i class="sw b"></i>B · {tableMode === 'avg' ? avgLabel(viewB) : unitName.toLowerCase() === 'cycle' ? 'cycle' : unitName}</span><div class="big mono">{tableMode === 'avg' ? avgText(viewB, resB) : cycleText(cycB)}</div></div>
    {#if summary}
      <p class="sentence"><strong>{summary.head}</strong> {summary.detail} {summary.tail}</p>
    {/if}
  </section>

  <section class="panel" aria-label="Overlay">
    <div class="panel-head">
      <h2>{showLabel}, average current</h2>
      <label class="alignsel">Show
        <select value={effShow} onchange={(e) => setShow((e.target as HTMLSelectElement).value)}>
          {#if kind === 'wake'}<option value="awake">Awake part</option>{/if}
          <option value="whole">{wholeLabel}</option>
          {#each sharedPhases as n (n)}<option value="phase:{n}">Phase: {n}</option>{/each}
          {#each sharedStretches as n (n)}<option value="type:{n}">{n}</option>{/each}
        </select>
      </label>
      <div class="seg" role="group" aria-label="Scale">
        <button type="button" aria-pressed={log} onclick={() => (log = true)}>Log</button>
        <button type="button" aria-pressed={!log} onclick={() => (log = false)}>Lin</button>
      </div>
      <span class="spacer"></span>
      <label class="alignsel">Align on
        <select bind:value={align}>
          <option value="start">Cycle start</option>
          {#each sharedPhases as n (n)}<option value="phase:{n}">Phase start: {n}</option>{/each}
          <option value="current">Current rises above…</option>
          <option value="line">A log line containing…</option>
          <option value="manual">Manual only</option>
        </select>
      </label>
      {#if align === 'current'}
        <form onsubmit={(e) => { e.preventDefault(); nudge = 0; autoAlign(); }} class="thr">
          <span class="input-unit"><input type="text" inputmode="decimal" bind:value={thrText} placeholder={String(sessionThreshold(sessA) * 1e3)} aria-label="Threshold in mA" size="5" /><span class="muted">mA</span></span>
          <button class="btn small" type="submit">Align</button>
        </form>
      {/if}
      {#if align === 'line'}
        <form onsubmit={(e) => { e.preventDefault(); nudge = 0; autoAlign(); }}>
          <input class="input linein" type="text" bind:value={lineText} placeholder="e.g. WiFi connected" aria-label="Log line text" />
          <button class="btn small" type="submit">Align</button>
        </form>
      {/if}
    </div>

    <div class="tune" role="group" aria-label="Fine-tune alignment">
      <span class="strong">B offset</span>
      <button class="btn icon" type="button" aria-label="Move B earlier" onclick={() => nudgeBy(-1)}>−</button>
      <span class="offset mono" title="Auto-align {fmtOffset(autoD)} + fine-tune {fmtOffset(nudge)}">{fmtOffset(d)}</span>
      <button class="btn icon" type="button" aria-label="Move B later" onclick={() => nudgeBy(1)}>+</button>
      <span class="muted">step</span>
      <div class="seg" role="group" aria-label="Step">
        {#each STEPS as [s, label] (s)}
          <button type="button" aria-pressed={step === s} onclick={() => { step = s; savePref('pst.cmp.step', String(s)); }}>{label}</button>
        {/each}
      </div>
      <button class="btn small ghost" type="button" disabled={!nudge} onclick={() => (nudge = 0)}>Reset</button>
      <span class="spacer"></span>
      <span class="faint small">←/→ keys nudge · drag across the chart to zoom</span>
      {#if zoom}<button class="btn small" type="button" onclick={() => (zoom = null)}>Zoom out</button>{/if}
    </div>
    {#if alignNote}<div class="note">{alignNote}</div>{/if}
    {#if showNote}<div class="note">{showNote}</div>{/if}

    <div class="chartwrap" class:busy={loading}>
      <CompareChart a={traceA} b={traceB} {version} {log} {view} {stripsA} {stripsB} onzoom={(r) => (zoom = r)} height={380}
        cursorX={logHover} onhover={(x) => (chartHover = x)} shade={laneHover} onscale={(sc) => (scale = sc)} />
      {#if lanes.length}<ChipLanes {lanes} {scale} onhover={(h) => (laneHover = h)} label="Chip lanes: A's bus messages over B's" />{/if}
    </div>
    <div class="legend">
      <span><i class="sw a"></i>A #{side.a.sid} {sessA?.name ?? ''} · cycle {(side.a.cycle ?? 0) + 1}</span>
      <span><i class="sw b"></i>B #{side.b.sid} {sessB?.name ?? ''} · cycle {(side.b.cycle ?? 0) + 1}</span>
      <span class="faint">Coloured strips along the bottom: phases of A (upper) and B (lower)</span>
      {#if busA.length || busB.length}<label class="lanetog"><input type="checkbox" bind:checked={showLanes} onchange={() => setLanesPref(showLanes)} /> chip lanes (A over B)</label>{/if}
    </div>
  </section>

  {#if cycA && cycB && side.a.sid != null && side.b.sid != null}
    <CompareLog sidA={side.a.sid} sidB={side.b.sid} {cycA} {cycB} {d} {view} hoverX={chartHover} onhover={(x) => (logHover = x)} />
  {/if}

  <section class="panel tablewrap" aria-label="Per phase">
    <table>
      <thead>
        <tr><th scope="col">Phase</th><th scope="col" class="num">A duration</th><th scope="col" class="num">B duration</th><th scope="col" class="num">A charge</th><th scope="col" class="num">B charge</th><th scope="col" class="num">B − A</th></tr>
      </thead>
      <tbody class="mono">
        {#each rows as r (r.name)}
          {@const picked = show === `phase:${r.name}`}
          <tr class:main={r.main} class:picked>
            <td class="name">
              <button class="rowpick" type="button" aria-pressed={picked} title={picked ? 'Back to the awake part' : 'Show only this phase in the chart'}
                onclick={() => setShow(picked ? 'awake' : `phase:${r.name}`)}><span class="swatch" style:background={r.color}></span>{r.name}{#if Math.max(r.a.times ?? 1, r.b.times ?? 1) > 1.05}<span class="faint times">{` ×${+(r.a.times ?? 1).toFixed(1)}${(r.b.times ?? 1) !== (r.a.times ?? 1) ? ` / ×${+(r.b.times ?? 1).toFixed(1)}` : ''}`}</span>{/if}{#if picked}<span class="faint"> · shown</span>{/if}</button>
            </td>
            <td class="num">{#if r.a.dur == null}not seen{:else}{#if r.a.seen != null && r.a.n != null && r.a.seen < r.a.n}<span class="faint inn">in {r.a.seen} of {r.a.n} · </span>{/if}{fmtSpan(r.a.dur)}{/if}</td>
            <td class="num">{#if r.b.dur == null}not seen{:else}{#if r.b.seen != null && r.b.n != null && r.b.seen < r.b.n}<span class="faint inn">in {r.b.seen} of {r.b.n} · </span>{/if}{fmtSpan(r.b.dur)}{/if}</td>
            <td class="num">{fmtCharge(r.a.charge)}</td>
            <td class="num">{fmtCharge(r.b.charge)}</td>
            <td class="num delta" class:sig={r.big} class:less={r.big && (r.delta ?? 0) < 0} class:more={r.big && (r.delta ?? 0) > 0}>
              {r.delta == null ? '–' : `${r.delta > 0 ? '+' : '−'}${fmtCharge(Math.abs(r.delta))}`}
            </td>
          </tr>
        {/each}
        <tr class="total">
          <td class="name">{tableMode === 'avg' ? `Average ${typeKey || 'cycle'}` : wholeLabel}</td>
          <td class="num">{totA.dur == null ? '–' : fmtSpan(totA.dur)}</td>
          <td class="num">{totB.dur == null ? '–' : fmtSpan(totB.dur)}</td>
          <td class="num">{fmtCharge(totA.charge)}</td>
          <td class="num">{fmtCharge(totB.charge)}</td>
          <td class="num delta">{totalDelta == null ? '–' : `${totalDelta > 0 ? '+' : '−'}${fmtCharge(Math.abs(totalDelta))}`}</td>
        </tr>
      </tbody>
    </table>
  </section>
</div>

<style>
  .page { max-width: 1680px; margin: 0 auto; padding: 16px 24px 28px; display: flex; flex-direction: column; gap: 16px; }
  .head { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 16px; }
  h1 { margin: 0; font-size: 22px; font-weight: 700; }
  .pickers { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .pick { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px; min-height: 40px; padding: 2px 6px 2px 12px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel); }
  .pick select { max-width: 260px; min-height: 32px; border: 0; background: transparent; color: var(--text); }
  .cyc { font-size: 13px; white-space: nowrap; }
  .typesel { min-height: 40px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel); color: var(--text); padding: 0 10px; font: inherit; font-weight: 600; max-width: 240px; }
  .inn { font-size: 11px; }
  .times { font-weight: 400; font-size: 12px; }
  .warn { color: var(--warn); }
  .sw { display: inline-block; width: 14px; height: 3px; flex: 0 0 auto; vertical-align: middle; margin-right: 4px; }
  .sw.a { background: var(--cmp-a); }
  .sw.b { background: var(--cmp-b); }
  .banner { padding: 10px 14px; border-radius: 10px; border: 1px solid var(--line); background: var(--panel); }
  .banner.bad { border-color: var(--rec); color: var(--rec); background: var(--rec-bg); }
  .result { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 32px; padding: 14px 16px; }
  .lbl { font-size: 12px; color: var(--muted); }
  .big { font-size: 20px; font-weight: 600; }
  .sentence { flex: 1 1 320px; margin: 0; line-height: 1.5; }
  .alignsel { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: var(--muted); }
  .alignsel select, .linein { min-height: 34px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel); color: var(--text); padding: 0 8px; }
  .linein { width: 220px; }
  .thr, form:has(.linein) { display: inline-flex; align-items: center; gap: 6px; }
  .thr .input-unit { width: 110px; min-height: 34px; }
  .tune { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 10px 14px 0; padding: 8px 10px; border-radius: 10px; background: var(--panel2); font-size: 13px; }
  .strong { font-weight: 600; }
  .offset { display: inline-flex; align-items: center; justify-content: center; min-height: 36px; min-width: 120px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel); font-weight: 600; }
  .small { font-size: 12px; }
  .note { margin: 8px 14px 0; font-size: 13px; color: var(--warn); }
  .chartwrap { padding: 8px 12px 0 4px; transition: opacity 0.15s; }
  .chartwrap.busy { opacity: 0.7; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 18px; padding: 6px 16px 12px; font-size: 12px; color: var(--muted); }
  .lanetog { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
  .lanetog input { margin: 0; accent-color: var(--accent); }
  .tablewrap { overflow-x: auto; }
  table { width: 100%; min-width: 600px; border-collapse: collapse; }
  th { text-align: left; font-size: 12px; font-weight: 600; color: var(--muted); padding: 12px 8px; }
  th:first-child, td:first-child { padding-left: 16px; }
  th:last-child, td:last-child { padding-right: 16px; }
  td { padding: 9px 8px; border-top: 1px solid var(--line); font-size: 13px; }
  .num { text-align: right; white-space: nowrap; }
  .name { font-family: var(--font); font-weight: 600; }
  .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 8px; }
  tr.main { background: var(--hl); }
  tr.picked td { background: var(--sel); }
  .rowpick { border: 0; background: none; padding: 0; color: var(--text); font: inherit; font-weight: 600; cursor: pointer; text-align: left; display: inline-flex; align-items: center; }
  .rowpick:hover { text-decoration: underline; }
  .delta { color: var(--muted); }
  .delta.sig { font-weight: 700; }
  .delta.less { color: var(--less); }
  .delta.more { color: var(--more); }
  tr.total td { border-top-width: 2px; font-weight: 700; }
  tr.total .delta { color: var(--text); }
  @media (max-width: 720px) {
    .page { padding: 12px; gap: 12px; }
    .pick select { max-width: 160px; }
  }
</style>
