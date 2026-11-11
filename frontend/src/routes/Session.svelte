<script lang="ts">
  import { untrack } from 'svelte';
  import BatteryPanel from '../components/BatteryPanel.svelte';
  import BatteryPicker from '../components/BatteryPicker.svelte';
  import CurrentChart from '../components/CurrentChart.svelte';
  import LogPanel from '../components/LogPanel.svelte';
  import PhaseTable from '../components/PhaseTable.svelte';
  import RulesEditor from '../components/RulesEditor.svelte';
  import WhatIf from '../components/WhatIf.svelte';
  import { api, type BatteryForm, type PhasesResult, type ResetGroup, type SessionDetail, type SessionEvent, type SessionStats, type Trace } from '../lib/api';
  import { fmtAmps, fmtBytes, fmtCharge, fmtLife, fmtSessionTime, fmtSpan, fmtStarted, splitLevel } from '../lib/format';
  import { indexAt, type LiveLine, type Range } from '../lib/live.svelte';
  import { settings } from '../lib/settings.svelte';
  import { ALL, statsFor, typeViews, usable } from '../lib/cycletypes';
  import type { ChartBand, ChartCursor, DraftRule } from '../lib/types';
  import { outside as outsideClose } from '../lib/outside';

  let { id }: { id: number } = $props();

  const POINTS = 1600;

  let s = $state<SessionDetail | null>(null);
  let error = $state('');
  let total = $state<SessionStats | null>(null);

  // main chart
  let view = $state<Range>({ a: 0, b: 1 });
  let zoomStack = $state<Range[]>([]);
  let trace = $state<Trace | null>(null);
  let tx: number[] = $state.raw([]);
  let tmn: (number | null)[] = $state.raw([]);
  let tmx: (number | null)[] = $state.raw([]);
  let tavg: (number | null)[] = $state.raw([]);
  let version = $state(0);
  let loading = $state(false);
  let viewStats = $state<SessionStats | null>(null);
  let selection = $state<Range | null>(null);
  let selStats = $state<SessionStats | null>(null);
  let log = $state(true);
  let showBand = $state(true);
  let showAvg = $state(true);

  // overview strip
  let ox: number[] = $state.raw([]);
  let omn: (number | null)[] = $state.raw([]);
  let omx: (number | null)[] = $state.raw([]);
  let oavg: (number | null)[] = $state.raw([]);
  let oversion = $state(0);

  // log + events
  let lines = $state.raw<LiveLine[]>([]);
  let linesCapped = $state(false);
  let selected = $state<LiveLine | null>(null);
  let events = $state<SessionEvent[]>([]);
  let eventTab = $state<'wake' | 'other'>('wake');

  // editing
  let editing = $state(false);
  let editName = $state('');
  let editNotes = $state('');
  let confirmDelete = $state(false);

  // phases
  let phasesRes = $state.raw<PhasesResult | null>(null);
  let phaseMode = $state<'avg' | 'cycle'>('avg');
  /** The shown cycle of the chosen type (index in its list), when picked with the stepper or the table. */
  let cycleSel = $state<number | null>(null);
  let awakeOnly = $state(true);
  /** The chosen cycle type ('' = all wakes), remembered per phase-rule profile. */
  let typeKey = $state(ALL);
  let rulesOpen = $state(false);
  let rulesTab = $state<'phases' | 'types'>('phases');
  let draft = $state<DraftRule | null>(null);
  let ctx = $state<{ x: number; y: number; line: LiveLine } | null>(null);
  let lastPhaseLoad = 0;
  let phaseStyle = $state<'bands' | 'lines' | 'off'>(loadPref('phaseStyle', 'bands') as 'bands' | 'lines' | 'off');
  let overviewPhases = $state<boolean | null>(null); // null = automatic (off when crowded)

  function loadPref(key: string, fallback: string): string {
    try {
      return localStorage.getItem(key) ?? fallback;
    } catch {
      return fallback;
    }
  }
  function setPhaseStyle(v: 'bands' | 'lines' | 'off') {
    phaseStyle = v;
    try {
      localStorage.setItem('phaseStyle', v);
    } catch {
      /* storage blocked */
    }
  }

  const WINDOWS: [number, string][] = [[1, '1 s'], [10, '10 s'], [60, '1 min'], [600, '10 min'], [3600, '1 h']];
  let rangeMode = $state<'time' | 'cycle'>(loadPref('rangeMode', 'time') === 'cycle' ? 'cycle' : 'time');
  function setRangeMode(m: 'time' | 'cycle') {
    rangeMode = m;
    try {
      localStorage.setItem('rangeMode', m);
    } catch {
      /* storage blocked */
    }
    if (m === 'cycle') goCycle(cycleIdx ?? 0);
    else cycleSel = null;
  }
  function setWindow(w: number) {
    const mid = (view.a + view.b) / 2;
    const width = Math.min(w, duration);
    const a = Math.max(0, Math.min(mid - width / 2, duration - width));
    setView({ a, b: a + width });
  }

  let token = 0;
  let selToken = 0;

  const duration = $derived(s?.duration_s ?? 0);
  const span = $derived(view.b - view.a);
  // The overview only marks the visible range once you've zoomed in.
  const wholeView = $derived(view.a <= 1e-3 && view.b >= duration - 1e-3);
  const wakes = $derived(events.filter((e) => e.kind === 'wake'));
  // Resets come grouped from the session (one row per group), not one row each.
  const others = $derived(events.filter((e) => e.kind !== 'wake' && e.kind !== 'reset'));
  const resets = $derived(s?.resets ?? { count: 0, groups: [] });
  const resetTimes = $derived(resets.groups.flatMap((g) => g.times));
  /** Which reset of each group a click on its row shows next. */
  let resetStep = $state<Record<number, number>>({});
  const otherRows = $derived(
    [...others.map((e) => ({ t: e.t, e, g: null as ResetGroup | null, gi: -1 })), ...resets.groups.map((g, gi) => ({ t: g.first_t, e: null, g, gi }))]
      .sort((x, y) => x.t - y.t),
  );
  function showReset(t: number) {
    // The lead-up matters: a few seconds before it, a moment after.
    setView({ a: Math.max(0, t - 3), b: Math.min(duration, t + 1) });
  }
  function stepReset(gi: number) {
    const g = resets.groups[gi];
    const k = resetStep[gi] ?? 0;
    showReset(g.times[k % g.times.length]);
    resetStep = { ...resetStep, [gi]: (k + 1) % g.times.length };
  }
  const resetText = (g: ResetGroup) => `${g.count}× ${g.label}`;

  // firmware flashes by the tracker (uploads from PlatformIO) --------------------------------------------------
  const flashes = $derived(events.filter((e) => e.kind === 'flash'));
  const flashHighlights = $derived(flashes.map((e) => ({ a: e.t, b: e.end_t ?? e.t, label: `flash ${String(e.data.version ?? '')}`.trim() })));
  const str = (v: unknown) => (v == null ? '' : String(v));
  /** Compare the last cycle (of the chosen type) before a flash with the first one after it. */
  function beforeAfterHref(e: SessionEvent): string | null {
    const end = e.end_t ?? e.t;
    const before = insts.filter((c) => c.t1 <= e.t).at(-1);
    const after = insts.find((c) => c.t0 >= end);
    if (!before || !after) return null;
    let h = `#/compare?a=${id}:${before.k + 1}&b=${id}:${after.k + 1}`;
    if (tview?.key) h += `&type=${encodeURIComponent(tview.key)}`;
    return h;
  }
  /** Cycles card: what woke them, grouped (all cycles, like the count next to it). */
  const wokenBy = $derived.by(() => {
    const n: Record<string, number> = {};
    for (const c of phasesRes?.cycles ?? []) if (c.wake) n[c.wake] = (n[c.wake] ?? 0) + 1;
    return Object.entries(n).sort((a, b) => b[1] - a[1]);
  });
  const voltage = $derived(typeof s?.config.voltage_mv === 'number' ? `${((s.config.voltage_mv as number) / 1000).toFixed(2)} V` : 'voltage not set by this session');

  async function loadSession(first = false) {
    try {
      const prevDur = s?.duration_s ?? 0;
      s = await api.session(id);
      error = '';
      if (first) {
        view = { a: 0, b: Math.max(s.duration_s, 0.001) };
        void loadView();
        void loadOverview();
        api.sessionStats(id).then((t) => (total = t)).catch(() => {});
        api.events(id).then((e) => (events = e)).catch(() => {});
        void loadPhases();
      } else if (s.status === 'running') {
        if (Date.now() - lastPhaseLoad > 15000) void loadPhases();
        // Keep following the end when the whole session (or its end) is in view.
        if (Math.abs(view.b - prevDur) < 0.5 && s.duration_s > prevDur) {
          view = { a: zoomStack.length ? view.a : 0, b: s.duration_s };
          void loadView();
        }
        void loadOverview();
        api.sessionStats(id).then((t) => (total = t)).catch(() => {});
        api.events(id).then((e) => (events = e)).catch(() => {});
      }
    } catch (e) {
      error = (e as Error).message;
    }
  }

  // Runs once per session id. untrack: loadSession reads `s`, which must not make this effect re-run.
  $effect(() => {
    void id;
    untrack(() => {
      zoomStack = [];
      selection = null;
      selected = null;
      void loadSession(true);
    });
    const t = setInterval(() => {
      if (untrack(() => s?.status) === 'running') void loadSession();
    }, 3000);
    return () => clearInterval(t);
  });

  async function loadPhases() {
    const first = phasesRes == null;
    lastPhaseLoad = Date.now();
    phasesRes = await api.phases(id).catch(() => null);
    if (phasesRes?.profile) {
      const k = loadPref(`pst.type.${phasesRes.profile.id}`, ALL);
      typeKey = phasesRes.types.some((t) => t.name === k) ? k : ALL;
    }
    if (first && rangeMode === 'cycle' && phasesRes?.cycles.length) goCycle(Math.max(0, centerCycle));
  }

  async function loadOverview() {
    if (!s || s.duration_s <= 0) return;
    const t = await api.trace(id, 0, s.duration_s, 800).catch(() => null);
    if (!t) return;
    ox = t.x;
    omn = t.min;
    omx = t.max;
    oavg = t.mean;
    oversion++;
  }

  async function loadView() {
    if (!s) return;
    const my = ++token;
    loading = true;
    const a = Math.max(0, view.a);
    const b = Math.min(Math.max(s.duration_s, a + 1e-4), view.b);
    try {
      const [t, st, u] = await Promise.all([
        api.trace(id, a, b, POINTS),
        api.sessionStats(id, a, b).catch(() => null),
        api.sessionUart(id, a, b, 2000).catch(() => []),
      ]);
      if (my !== token) return;
      trace = t;
      tx = t.x;
      tmn = t.min;
      tmx = t.max;
      tavg = t.mean;
      version++;
      viewStats = st;
      linesCapped = u.length >= 2000;
      lines = u.map((l, i) => ({ seq: i, wall: Date.parse(l.wall) / 1000, sample_idx: l.sample_idx, text: l.text, x: l.t }));
    } catch (e) {
      if (my === token) error = (e as Error).message;
    } finally {
      if (my === token) loading = false;
    }
  }

  function setView(r: Range, push = true, keepCycle = false) {
    if (!keepCycle) cycleSel = null;
    const a = Math.max(0, Math.min(r.a, r.b));
    const b = Math.min(duration, Math.max(r.a, r.b));
    if (b - a < 2e-4) return;
    if (push) zoomStack = [...zoomStack, { ...view }];
    view = { a, b };
    selection = null;
    selStats = null;
    void loadView();
  }

  function zoomOut() {
    const prev = zoomStack.at(-1);
    if (!prev) return;
    zoomStack = zoomStack.slice(0, -1);
    setView(prev, false);
  }

  function pan(dir: number) {
    const w = span;
    let a = view.a + dir * w * 0.5;
    a = Math.max(0, Math.min(a, duration - w));
    setView({ a, b: a + w }, false);
  }

  async function select(r: Range) {
    selection = r;
    selStats = null;
    const my = ++selToken;
    const st = await api.sessionStats(id, r.a, r.b).catch(() => null);
    if (my === selToken) selStats = st;
  }

  function showEvent(e: SessionEvent) {
    const end = e.end_t ?? e.t + 0.05;
    const pad = Math.max(0.05, (end - e.t) * 0.08);
    setView({ a: e.t - pad, b: end + pad });
  }

  function stepWake(dir: number) {
    const mid = (view.a + view.b) / 2;
    const list = dir > 0 ? wakes.filter((w) => w.t > mid + 1e-6) : wakes.filter((w) => (w.end_t ?? w.t) < mid - 1e-6).reverse();
    const w = list[0];
    if (w) showEvent(w);
  }

  const markers = $derived(lines.map((l) => l.x).filter((x): x is number => x != null));

  // phases -----------------------------------------------------------------------------------------------------
  const rules = $derived(phasesRes?.profile?.rules ?? []);
  const cycles = $derived(phasesRes?.cycles ?? []);
  const views = $derived(typeViews(phasesRes));
  const tview = $derived(views.find((v) => v.key === typeKey) ?? views[0]);
  /** The cycles the stepper goes through: all wakes, or the chosen type's cycles. */
  const insts = $derived(tview?.instances ?? []);
  const sleepRule = $derived(phasesRes?.sleep_rule ?? -1);
  /** All wakes, averaged by the api. */
  const allStats = $derived(statsFor(phasesRes, ALL));
  // Whole-wake types for the Cycles KPI ("104 Measure only · 36 Upload").
  const wakeTypes = $derived(views.filter((v) => v.key !== ALL && v.kind === 'wake' && !v.error));
  function setType(k: string) {
    if (k === '__edit') {
      rulesTab = 'types';
      draft = null;
      rulesOpen = true;
      return;
    }
    typeKey = k;
    try {
      if (phasesRes?.profile) localStorage.setItem(`pst.type.${phasesRes.profile.id}`, k);
    } catch { /* storage blocked */ }
    cycleSel = null;
    goCycle(0);
  }
  const bands = $derived.by<ChartBand[]>(() => {
    const inView = cycles.filter((c) => c.end_t > view.a && c.t < view.b);
    if (inView.length > 400) return [];
    const out: ChartBand[] = [];
    for (const c of inView) for (const p of c.phases) if (p[2] > view.a && p[1] < view.b) out.push({ a: p[1], b: p[2], color: rules[p[0]]?.color ?? '#8b949e' });
    return out;
  });
  // Overview: one thin strip for all cycles, unless that gets too crowded to read.
  const overviewCrowded = $derived(cycles.length > 300);
  const showOverviewPhases = $derived(overviewPhases ?? !overviewCrowded);
  const overviewBands = $derived.by<ChartBand[]>(() => {
    if (!showOverviewPhases) return [];
    const out: ChartBand[] = [];
    for (const c of cycles) for (const p of c.phases) out.push({ a: p[1], b: p[2], color: rules[p[0]]?.color ?? '#8b949e' });
    return out;
  });

  const centerCycle = $derived.by(() => {
    const mid = (view.a + view.b) / 2;
    return insts.findIndex((c) => c.t0 <= mid && mid < c.t1);
  });
  const cycleIdx = $derived(cycleSel ?? (centerCycle >= 0 ? centerCycle : null));
  const cycle = $derived(cycleIdx != null ? (insts[cycleIdx] ?? null) : null);
  /** Compare this cycle of the chosen type with another one of that type; a stretch or period is lined up on
   *  the phase it begins at. */
  const compareHref = $derived.by(() => {
    let h = `#/compare?a=${id}:${(cycle?.k ?? 0) + 1}&b=${id}`;
    if (!tview?.key) return h;
    h += `&type=${encodeURIComponent(tview.key)}`;
    const begin = phasesRes?.profile?.types.find((t) => t.name === tview.key)?.begin;
    if (begin && tview.kind !== 'wake') h += `&align=${encodeURIComponent(`phase:${begin}`)}`;
    return h;
  });
  /** Awake | Whole only makes sense for a whole wake. */
  const canAwake = $derived(tview?.kind === 'wake');

  function goCycle(i: number) {
    const c = insts[i];
    if (!c) return;
    phaseMode = 'cycle';
    // Awake: up to its sleep phase (the last one, for a period).
    const sleep = canAwake ? [...c.phases].reverse().find((p) => p.rule === sleepRule) : undefined;
    const end = awakeOnly && sleep ? sleep.t : c.t1;
    const pad = Math.max(tview?.kind === 'stretch' ? 0.005 : 0.05, (end - c.t0) * 0.03);
    // Up to the cycle's end: the next wake starts right there, so no padding on that side (it would show it).
    setView({ a: c.t0 - pad, b: end >= c.t1 ? c.t1 : end + pad }, true, true);
    cycleSel = i;
  }

  function ruleFromLine(line: LiveLine): DraftRule {
    const msg = splitLevel(line.text).msg.trim();
    const words = msg.replace(/[^A-Za-z ]+/g, ' ').trim().split(/\s+/).slice(0, 3).join(' ');
    const rule = /\d/.test(msg)
      ? { name: words || 'New phase', color: '', match: 'regex' as const, pattern: msg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\d+/g, '\\d+'), snap: false }
      : { name: words || 'New phase', color: '', match: 'contains' as const, pattern: msg, snap: false };
    // Insert after the phase this line falls in (rules are in time order).
    let insertAt = rules.length;
    const t = line.x;
    if (t != null) {
      const c = cycles.find((cc) => cc.t <= t && t < cc.end_t);
      const p = c?.phases.find((pp) => pp[1] <= t && t < pp[2]);
      if (p) insertAt = p[0] + 1;
    }
    return { rule, insertAt };
  }
  const cursor = $derived.by<ChartCursor>(() => {
    void version;
    if (!selected || selected.x == null) return null;
    const i = indexAt(tx, selected.x);
    return { x: selected.x, title: `${fmtSessionTime(selected.x, 1)} · ${fmtAmps(i >= 0 ? tavg[i] : null)}`, text: selected.text };
  });

  const sourceText = $derived.by(() => {
    if (!trace) return '';
    const res = trace.tier === 'raw' ? (trace.bucket_s <= 1.01e-5 ? 'raw 10 µs samples' : `raw data, ${fmtSpan(trace.bucket_s)} per point`) : `${trace.tier} summaries, ${fmtSpan(trace.bucket_s)} per point`;
    const from = trace.source === 'live' ? ' (live buffer)' : trace.source === 'parquet' ? ' (stored raw)' : '';
    return res + from;
  });

  async function saveEdit(e: SubmitEvent) {
    e.preventDefault();
    try {
      await api.patchSession(id, { name: editName.trim() || s?.name, notes: editNotes });
      editing = false;
      await loadSession();
      s = await api.session(id);
    } catch (err) {
      error = (err as Error).message;
    }
  }

  async function remove() {
    try {
      await api.deleteSession(id);
      location.hash = '#/sessions';
    } catch (err) {
      error = (err as Error).message;
    }
  }

  const avg = $derived(s?.avg_current_a ?? null);
  const rawText = $derived(s ? Object.entries(s.raw).map(([k, v]) => `${k} ${fmtBytes(v.bytes)}`).join(' · ') || 'no raw data' : '');
  const gapCount = $derived(s?.events.gap ?? 0);
</script>

<div class="page">
  {#if error}<div class="banner bad">{error}</div>{/if}
  {#if s}
    <div class="titlebar">
      <div class="title">
        <div class="crumb"><a href="#/sessions">Sessions</a> / #{s.id}</div>
        {#if editing}
          <form class="edit" onsubmit={saveEdit}>
            <input class="input" type="text" bind:value={editName} aria-label="Session name" />
            <input class="input" type="text" bind:value={editNotes} placeholder="Notes" aria-label="Notes" />
            <button class="btn primary" type="submit">Save</button>
            <button class="btn ghost" type="button" onclick={() => (editing = false)}>Cancel</button>
          </form>
        {:else}
          <h1>{s.name} <span class="badge {s.kind}">{s.kind}</span>
            {#if s.status === 'running'}<span class="badge rec">recording</span>{/if}
            {#if s.status === 'interrupted'}<span class="badge warn">interrupted</span>{/if}
          </h1>
          <div class="meta">{#if s.device_name}{s.device_name} · {/if}{fmtStarted(s.started_at)} · {fmtSpan(s.duration_s)} · {voltage}{s.notes ? ` · ${s.notes}` : ''}</div>
        {/if}
      </div>
      {#if !editing}
        <button class="btn ghost" type="button" onclick={() => { draft = null; rulesTab = 'phases'; rulesOpen = true; }}>Phase rules{phasesRes?.profile ? `: ${phasesRes.profile.name}` : ''}</button>
        <button class="btn ghost" type="button" onclick={() => { editName = s?.name ?? ''; editNotes = s?.notes ?? ''; editing = true; }}>Edit name / notes</button>
        {#if confirmDelete}
          <span class="confirm">Delete for good? <button class="btn danger" type="button" onclick={remove}>Delete</button><button class="btn ghost" type="button" onclick={() => (confirmDelete = false)}>Cancel</button></span>
        {:else}
          <button class="btn ghost" type="button" disabled={s.status === 'running'} onclick={() => (confirmDelete = true)}>Delete</button>
        {/if}
      {/if}
    </div>

    <section class="kpis" aria-label="Summary">
      <div class="kpi"><div class="label">Average current</div><div class="value">{fmtAmps(avg)}</div><div class="sub">whole session</div></div>
      <div class="kpi"><div class="label">Charge</div><div class="value">{fmtCharge(s.charge_c)}</div><div class="sub">{(s.charge_c / 3.6).toFixed(2)} mAh</div></div>
      {#if phasesRes?.cycles.length && allStats}
      <div class="kpi">
        <div class="label">Cycles</div><div class="value">{phasesRes.cycles.length}</div>
        <div class="sub">{#if wakeTypes.length}{wakeTypes.map((v) => `${v.instances.length} ${v.name}`).join(' · ')}{:else}{allStats.usable} complete · {fmtSpan(allStats.duration_s.mean ?? 0)} · {fmtCharge(allStats.charge_c.mean)} each{/if}</div>
        {#if wokenBy.length}<div class="sub" title="What woke the board, from its log (phase profile: wake reason line)">woken by: {wokenBy.map(([r, n]) => `${n} ${r}`).join(' · ')}</div>{/if}
      </div>
      {:else}
      <div class="kpi">
        <div class="label">Wakes</div><div class="value">{wakes.length}</div>
        <div class="sub">{total?.wakes.avg_duration_s ? `${total.wakes.avg_duration_s.toFixed(1)} s · ${fmtCharge(total.wakes.avg_charge_c)} each · ${(total.wakes.charge_share * 100).toFixed(0)} % of charge` : 'current above the wake threshold'}</div>
      </div>
      {/if}
      <div class="kpi">
        <div class="label">Battery</div>
        <div class="value">{avg ? fmtLife(settings.batteries.selected_mah / (avg * 1000)) : '–'}</div>
        <div class="sub"><BatteryPicker /> at the session average</div>
      </div>
      {#if flashes.length}
        {@const lastFlash = flashes[flashes.length - 1]}
        <div class="kpi"><div class="label">Firmware</div>
          <button type="button" class="flashkpi" title="Show the last flash" onclick={() => showEvent(lastFlash)}>
            <span class="value flash">flashed {flashes.length}×</span>
            <span class="sub">last: {str(lastFlash.data.version) || '?'} · {str(lastFlash.data.git)} at {fmtSessionTime(lastFlash.t, duration)}</span>
          </button>
        </div>
      {/if}
      <div class="kpi"><div class="label">Data</div><div class="value">{gapCount} {gapCount === 1 ? 'gap' : 'gaps'}</div>
        {#if resets.count}
          <button type="button" class="resets" title="Unexpected resets of the board (not deep-sleep wakes or power-ons by the tracker). Click: show the first one."
            onclick={() => showReset(resets.groups.reduce((a, g) => Math.min(a, g.first_t), Infinity))}>
            <span class="value bad">{resets.count} {resets.count === 1 ? 'reset' : 'resets'}</span>
            <span class="sub bad">{resets.groups.slice(0, 3).map(resetText).join(' · ')}{resets.groups.length > 3 ? ` · +${resets.groups.length - 3} more` : ''}</span>
          </button>
        {/if}
        <div class="sub">{rawText} · {s.uart_lines} log lines</div></div>
    </section>

    <section class="panel overview" aria-label="Whole session">
      <div class="ovhead">
        <h2>Whole session</h2>
        <span class="faint">drag to zoom</span>
        <span class="spacer"></span>
        {#if cycles.length}
          <label class="check small" title={overviewCrowded ? `${cycles.length} cycles: too many to see at this size, so it starts off` : ''}>
            <input type="checkbox" checked={showOverviewPhases} onchange={(e) => (overviewPhases = (e.target as HTMLInputElement).checked)} />phases{overviewCrowded ? ' (crowded)' : ''}
          </label>
        {/if}
      </div>
      <CurrentChart x={ox} mn={omn} mx={omx} avg={oavg} version={oversion} log={true} showAvg={false} selection={wholeView ? null : view} highlights={flashHighlights}
        onselect={(r) => setView(r)} height={110} xLabel={(v) => fmtSessionTime(v, duration)} label="Peak current over the whole session"
        bands={overviewBands} bandStyle="ticks" alerts={resetTimes} />
    </section>

    <div class="body">
      <div class="left">
      <section class="panel main" aria-label="Current">
        <!-- Row 1: what range you're looking at — by time, or by cycle -->
        <div class="panel-head">
          {#if cycles.length}
            <div class="seg" role="group" aria-label="Navigate by">
              <button type="button" aria-pressed={rangeMode === 'time'} onclick={() => setRangeMode('time')}>Time</button>
              <button type="button" aria-pressed={rangeMode === 'cycle'} onclick={() => setRangeMode('cycle')}>Cycles</button>
            </div>
          {/if}
          {#if rangeMode === 'cycle' && cycles.length}
            <button class="btn small ghost icon" type="button" aria-label="Previous cycle" disabled={(cycleIdx ?? 0) <= 0} onclick={() => goCycle((cycleIdx ?? 1) - 1)}>‹</button>
            <h2 class="mono">{tview?.key ? tview.name : 'Cycle'} {insts.length ? (cycleIdx ?? 0) + 1 : 0} / {insts.length}{#if cycle && !cycle.ok}<span class="warn inc" title="Incomplete cycle: it contains a gap, or it was cut off by the end of the session" aria-label="incomplete cycle"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M12 3l10 18H2z"></path><path d="M12 10v5M12 18v.5"></path></svg></span>{/if}
              <span class="muted">· {fmtSessionTime(view.a, span)} ({fmtSpan(span)})</span></h2>
            <button class="btn small ghost icon" type="button" aria-label="Next cycle" disabled={(cycleIdx ?? -1) >= insts.length - 1} onclick={() => goCycle((cycleIdx ?? -1) + 1)}>›</button>
            <select class="typesel" value={typeKey} onchange={(e) => { const el = e.target as HTMLSelectElement; setType(el.value); el.value = typeKey; }} aria-label="Cycle type" title="Which cycles to step through and average">
              {#each views as v (v.key)}<option value={v.key} disabled={!!v.error}>{v.name} ({v.key ? v.instances.length : usable(v).length}){v.error ? ' – needs fixing' : ''}</option>{/each}
              <option value="__edit">Edit types…</option>
            </select>
            {#if canAwake}
              <div class="seg" role="group" aria-label="Part of the cycle">
                <button type="button" aria-pressed={awakeOnly} onclick={() => { awakeOnly = true; goCycle(cycleIdx ?? 0); }}>Awake</button>
                <button type="button" aria-pressed={!awakeOnly} onclick={() => { awakeOnly = false; goCycle(cycleIdx ?? 0); }}>Whole</button>
              </div>
            {/if}
            <a class="btn small ghost" href={compareHref} title="Compare this cycle (A) with another one (B)">Compare…</a>
          {:else}
            <button class="btn small ghost icon" type="button" aria-label="Earlier" onclick={() => pan(-1)}>‹</button>
            <h2 class="mono">{fmtSessionTime(view.a, span)} – {fmtSessionTime(view.b, span)} <span class="muted">({fmtSpan(span)})</span></h2>
            <button class="btn small ghost icon" type="button" aria-label="Later" onclick={() => pan(1)}>›</button>
            <div class="seg" role="group" aria-label="Window size">
              {#each WINDOWS as [w, label] (w)}
                <button type="button" aria-pressed={Math.abs(span - Math.min(w, duration)) < 1e-6} disabled={w > duration * 1.5 && w !== WINDOWS[0][0]} onclick={() => setWindow(w)}>{label}</button>
              {/each}
              <button type="button" aria-pressed={wholeView} onclick={() => { zoomStack = []; setView({ a: 0, b: duration }, false); }}>All</button>
            </div>
          {/if}
          <span class="spacer"></span>
          {#if zoomStack.length}
            <button class="btn small ghost icon" type="button" title="Back to the previous range" aria-label="Back to the previous range" onclick={zoomOut}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M9 14L4 9l5-5"></path><path d="M4 9h11a5 5 0 010 10h-3"></path></svg>
            </button>
          {/if}
        </div>
        <!-- Row 2: how it's drawn -->
        <div class="dispbar">
          <div class="seg" role="group" aria-label="Scale">
            <button type="button" aria-pressed={log} onclick={() => (log = true)}>Log</button>
            <button type="button" aria-pressed={!log} onclick={() => (log = false)}>Lin</button>
          </div>
          {#if cycles.length}
            <div class="seg" role="group" aria-label="How to show phases">
              <button type="button" aria-pressed={phaseStyle === 'bands'} onclick={() => setPhaseStyle('bands')}>Bands</button>
              <button type="button" aria-pressed={phaseStyle === 'lines'} onclick={() => setPhaseStyle('lines')}>Lines</button>
              <button type="button" aria-pressed={phaseStyle === 'off'} onclick={() => setPhaseStyle('off')}>No phases</button>
            </div>
            <span class="legend-ph">
              {#each rules as r, i (i)}<span><i style:background={r.color}></i>{r.name}</span>{/each}
            </span>
          {/if}
        </div>
        {#if selection}
          <div class="sel-bar" aria-live="polite">
            <strong class="mono">Selection {fmtSpan(selection.b - selection.a)}</strong>
            {#if selStats}
              <span class="mono">average {fmtAmps(selStats.mean_a)}</span>
              <span class="mono">charge {fmtCharge(selStats.charge_c)}</span>
              <span class="mono">peak {fmtAmps(selStats.max_a)}</span>
              <span class="mono">min {fmtAmps(selStats.min_a)}</span>
              <span class="faint">{selStats.exact ? 'exact, from raw samples' : 'from summaries'}</span>
            {:else}<span class="faint">calculating…</span>{/if}
            <span class="spacer"></span>
            <button class="btn small" type="button" onclick={() => selection && setView(selection)}>Zoom in</button>
            <button class="btn small ghost icon" type="button" aria-label="Clear selection" onclick={() => { selection = null; selStats = null; }}>×</button>
          </div>
        {/if}
        <div class="chartwrap">
          <CurrentChart x={tx} mn={tmn} mx={tmx} avg={tavg} {version} {log} {markers} alerts={resetTimes} highlights={flashHighlights} {cursor} {showBand} {showAvg} {selection} bands={phaseStyle === 'off' ? [] : bands} bandStyle={phaseStyle === 'lines' ? 'lines' : 'bands'}
            onselect={select} height={420} xLabel={(v) => fmtSessionTime(v, span)} label="Current in the selected time range" />
        </div>
        <div class="legend">
          <button type="button" class="toggle" aria-pressed={showAvg} onclick={() => (showAvg = !showAvg)}><i class="sw line"></i>average</button>
          <button type="button" class="toggle" aria-pressed={showBand} onclick={() => (showBand = !showBand)}><i class="sw band"></i>min–max per pixel</button>
          <span><i class="sw tick"></i>log line</span>
          {#if viewStats}<span class="mono">on screen: {fmtAmps(viewStats.mean_a)} · {fmtCharge(viewStats.charge_c)} · peak {fmtAmps(viewStats.max_a)}</span>{/if}
          <span class="faint">{sourceText}{loading ? ' · loading…' : ''}</span>
          {#if trace?.raw_available === false}<span class="warn">no raw data stored here (soak keeps raw only around wakes)</span>{/if}
          <span class="faint">· drag to select · click a log line for the cursor · right-click it to start a phase</span>
        </div>
      </section>
      {#if phasesRes?.profile && tview}
        <PhaseTable result={phasesRes} view={tview} current={cycle} mode={phaseMode} onmode={(m) => (phaseMode = m)} sid={id}
          ongo={(i) => { if (rangeMode !== 'cycle') setRangeMode('cycle'); goCycle(i); document.querySelector('.panel.main')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }} />
      {/if}
      </div>

      <aside class="side">
        <div class="logwrap">
          <LogPanel {lines} selected={selected?.seq ?? null} onselect={(l) => (selected = selected?.seq === l.seq ? null : l)}
            oncontext={(l, e) => (ctx = { x: e.clientX, y: e.clientY, line: l })}
            timeOf={(l) => fmtSessionTime(l.x ?? 0, 0.5)} follow={false}
            status={`${lines.length}${linesCapped ? ' (first 2000, zoom in for more)' : ''} lines in view`} empty="No log lines in this range" alerts={resetTimes} />
        </div>

        <section class="panel events" aria-label="Events">
          <div class="panel-head">
            <h2>Events</h2>
            <div class="seg" role="group" aria-label="Event type">
              <button type="button" aria-pressed={eventTab === 'wake'} onclick={() => (eventTab = 'wake')}>Wakes ({wakes.length})</button>
              <button type="button" aria-pressed={eventTab === 'other'} onclick={() => (eventTab = 'other')}>Other ({otherRows.length}){#if resets.count}<span class="bad"> ⚠</span>{/if}</button>
            </div>
            <span class="spacer"></span>
            {#if eventTab === 'wake'}
              <button class="btn small ghost icon" type="button" aria-label="Previous wake" onclick={() => stepWake(-1)}>‹</button>
              <button class="btn small ghost icon" type="button" aria-label="Next wake" onclick={() => stepWake(1)}>›</button>
            {/if}
          </div>
          <div class="evlist mono">
            {#if eventTab === 'wake'}
              {#each wakes as e (e.id)}
                <button type="button" class="ev" class:inview={e.t >= view.a && e.t <= view.b} onclick={() => showEvent(e)}>
                  <span class="t">{fmtSessionTime(e.t, 10)}</span>
                  <span>{fmtSpan((e.end_t ?? e.t) - e.t)}</span><span>{fmtCharge(e.charge_c)}</span><span class="muted">peak {fmtAmps(e.peak_a)}</span>
                </button>
              {:else}
                <div class="empty">No wakes detected (current never went above the wake threshold, or it never went below it).</div>
              {/each}
            {:else}
              {#each otherRows as r (r.g ? `g${r.gi}` : r.e?.id)}
                {#if r.g}
                  {@const g = r.g}
                  <button type="button" class="ev reset" class:inview={g.times.some((t) => t >= view.a && t <= view.b)} onclick={() => stepReset(r.gi)}
                    title={`${g.count}× ${g.label} (${g.chip})${g.panic ? `\n${g.panic}` : ''}${g.explanation ? `\nBefore it: ${g.explanation}` : ''}${g.count > 1 ? '\nClick again for the next one.' : ''}`}>
                    <span class="t">{fmtSessionTime(g.first_t, 10)}</span>
                    <span class="kind bad">reset</span><span class="bad">{g.label}{g.count > 1 ? ` ×${g.count}` : ''}</span><span class="muted expl">{g.panic ?? g.explanation ?? ''}</span>
                  </button>
                {:else if r.e && r.e.kind === 'flash'}
                  {@const e = r.e}
                  {@const cmp = beforeAfterHref(e)}
                  <div class="evflash" class:inview={e.t >= view.a && e.t <= view.b}>
                    <button type="button" class="ev" onclick={() => showEvent(e)} title="Show the flash">
                      <span class="t">{fmtSessionTime(e.t, 10)}</span>
                      <span class="kind flash">flash</span><span>{str(e.data.project)} {str(e.data.version)}</span><span class="muted">{str(e.data.git)}</span>
                    </button>
                    <div class="evmore">
                      <span class="muted">{e.end_t != null ? fmtSpan(e.end_t - e.t) : ''} · from {str(e.data.host) || '?'}{e.data.power_cycled ? ' · power-cycled first' : ''}{e.data.result === 'failed' ? ' · failed' : ''} · resets here are expected</span>
                      {#if cmp}<a class="btn small" href={cmp} title="The last cycle before the flash against the first one after it">Compare before / after</a>{/if}
                    </div>
                  </div>
                {:else if r.e}
                  {@const e = r.e}
                  <button type="button" class="ev" class:inview={e.t >= view.a && e.t <= view.b} onclick={() => showEvent(e)}>
                    <span class="t">{fmtSessionTime(e.t, 10)}</span>
                    <span class="kind">{e.kind}</span><span class="muted">{e.end_t != null ? fmtSpan(e.end_t - e.t) : ''} {e.data.reason ?? ''}</span>
                  </button>
                {/if}
              {:else}
                <div class="empty">Nothing else happened.</div>
              {/each}
            {/if}
          </div>
        </section>
        {#if s.config.battery}
          <BatteryPanel {id} duration={s.duration_s} battery={s.config.battery as BatteryForm} startMv={(s.config.voltage_mv as number | null) ?? null} {wakes} />
        {/if}
        {#if allStats && views.length}<WhatIf result={phasesRes!} sid={id} />{/if}
      </aside>
    </div>

    {#if ctx}
      {@const c = ctx}
      <div class="ctx popover" role="menu" style:left="{Math.min(c.x, innerWidth - 260)}px" style:top="{c.y}px" use:outsideClose={() => (ctx = null)}>
        <button type="button" role="menuitem" onclick={() => { draft = ruleFromLine(c.line); rulesTab = 'phases'; rulesOpen = true; ctx = null; }}>Start a phase here…</button>
        <button type="button" role="menuitem" onclick={() => { selected = c.line; ctx = null; }}>Put the cursor here</button>
      </div>
    {/if}
    {#if rulesOpen}
      <RulesEditor sessionId={id} profileId={phasesRes?.profile?.id ?? null} {draft} tab={rulesTab} onclose={() => (rulesOpen = false)}
        onsaved={() => { rulesOpen = false; draft = null; void loadPhases(); }} />
    {/if}
  {:else if !error}
    <div class="faint">Loading…</div>
  {/if}
</div>

<style>
  .typesel { min-height: 32px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); color: var(--text); padding: 0 8px; font: inherit; font-size: 13px; max-width: 220px; }
  .page { max-width: 1680px; margin: 0 auto; padding: 16px 24px 28px; display: flex; flex-direction: column; gap: 16px; }
  .banner.bad { padding: 10px 14px; border-radius: 10px; border: 1px solid var(--rec); color: var(--rec); background: var(--rec-bg); }
  .titlebar { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 10px; }
  .title { flex: 1 1 420px; min-width: 0; }
  .crumb { font-size: 13px; color: var(--muted); }
  .crumb a { color: var(--muted); }
  h1 { margin: 4px 0 0; font-size: 22px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .meta { font-size: 13px; color: var(--muted); margin-top: 4px; }
  .edit { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
  .edit .input { width: auto; flex: 1 1 200px; }
  .confirm { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .badge.detail { color: var(--accent); background: var(--sel); }
  .badge.soak { color: #a371f7; background: rgba(163, 113, 247, 0.14); }
  .badge.rec { color: var(--rec); background: var(--rec-bg); }
  .badge.warn { color: var(--warn); background: rgba(227, 179, 65, 0.14); }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
  .overview { padding: 8px 12px 6px 4px; }
  .ovhead { display: flex; align-items: center; gap: 10px; padding: 4px 10px 2px; }
  .ovhead .check { min-height: 24px; font-size: 12px; color: var(--muted); }
  .ovhead .check input { width: 15px; height: 15px; }
  .ovhead h2 { margin: 0; font-size: 14px; font-weight: 600; }
  .ovhead span { font-size: 12px; }
  .body { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; }
  .left { flex: 999 1 640px; min-width: 0; display: flex; flex-direction: column; gap: 16px; }
  .main { min-width: 0; display: flex; flex-direction: column; }
  .dispbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; padding: 8px 16px; border-bottom: 1px solid var(--line); font-size: 13px; }
  .inc { display: inline-flex; vertical-align: -1px; margin-left: 4px; }
  .small { font-size: 12px; }
  .legend-ph { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; color: var(--muted); }
  .legend-ph span { display: inline-flex; align-items: center; gap: 5px; }
  .legend-ph i { width: 10px; height: 10px; border-radius: 2px; }
  .ctx { position: fixed; z-index: 90; padding: 6px; display: flex; flex-direction: column; min-width: 220px; }
  .ctx button { text-align: left; min-height: 36px; padding: 0 10px; border: 0; border-radius: 6px; background: transparent; cursor: pointer; }
  .ctx button:hover, .ctx button:focus-visible { background: var(--sel); }
  .main h2 { font-size: 14px; }
  .sub-bar, .sel-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 16px; padding: 8px 16px; border-bottom: 1px solid var(--line); font-size: 13px; }
  .sub-bar { color: var(--muted); }
  .sel-bar { background: var(--sel); }
  .warn { color: var(--warn); }
  .bad { color: var(--rec); }
  .resets { display: flex; flex-direction: column; align-items: flex-start; width: 100%; padding: 0; margin: 2px 0 0; border: 0; background: transparent; text-align: left; cursor: pointer; font: inherit; color: inherit; }
  .resets .value { margin-top: 0; }
  .resets:hover .sub { text-decoration: underline; }
  .ev.reset .expl { overflow: hidden; text-overflow: ellipsis; }
  .chartwrap { padding: 8px 12px 0 4px; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 16px; padding: 8px 16px 12px; font-size: 12px; color: var(--muted); }
  .legend span, .toggle { display: inline-flex; align-items: center; gap: 6px; }
  .toggle { border: 1px solid var(--line); border-radius: 6px; background: transparent; color: var(--muted); padding: 3px 8px; cursor: pointer; font-size: 12px; }
  .toggle[aria-pressed='false'] { opacity: 0.5; text-decoration: line-through; }
  .sw { display: inline-block; }
  .sw.line { width: 16px; height: 2px; background: var(--trace); }
  .sw.band { width: 16px; height: 10px; background: var(--trace-fill); }
  .sw.tick { width: 1px; height: 10px; background: var(--muted); }
  .side { flex: 1 1 380px; min-width: 0; display: flex; flex-direction: column; gap: 16px; }
  .logwrap { display: flex; height: 460px; }
  .logwrap > :global(*) { flex: 1 1 0; }
  .events { display: flex; flex-direction: column; max-height: 360px; }
  .evlist { overflow: auto; padding: 4px 0; font-size: 12px; }
  .ev { display: grid; grid-template-columns: 84px minmax(70px, auto) minmax(60px, auto) 1fr; gap: 10px; white-space: nowrap; width: 100%; padding: 4px 14px; border: 0; background: transparent; text-align: left; cursor: pointer; font-family: var(--mono); font-size: 12px; }
  .ev:hover { background: var(--panel2); }
  .ev.inview { box-shadow: inset 3px 0 0 var(--accent); }
  .ev .t { color: var(--faint); }
  .kind { font-weight: 600; }
  .kind.flash, .value.flash { color: var(--flash); }
  .evflash { background: var(--flash-bg); }
  .evflash.inview { box-shadow: inset 3px 0 0 var(--accent); }
  .evflash .ev:hover { background: transparent; }
  .evmore { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; padding: 0 14px 8px 108px; font-family: var(--font); font-size: 12px; }
  .flashkpi { display: flex; flex-direction: column; gap: 2px; padding: 0; border: 0; background: transparent; color: inherit; text-align: left; cursor: pointer; font: inherit; }
  @media (max-width: 720px) { .evmore { padding-left: 14px; } }
  .empty { padding: 14px; color: var(--faint); font-family: var(--font); }
  :global(.u-select) { background: var(--sel) !important; box-shadow: inset 1px 0 0 var(--accent), inset -1px 0 0 var(--accent); }
  @media (max-width: 720px) {
    .page { padding: 12px; gap: 12px; }
    .logwrap { height: 400px; }
  }
</style>
