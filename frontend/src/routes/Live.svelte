<script lang="ts">
  import BatteryPicker from '../components/BatteryPicker.svelte';
  import CurrentChart from '../components/CurrentChart.svelte';
  import LogPanel from '../components/LogPanel.svelte';
  import UsbConsole from '../components/UsbConsole.svelte';
  import ControlStrip from '../components/ControlStrip.svelte';
  import { api, type ResetGroup } from '../lib/api';
  import { fmtAmps, fmtClock, fmtDuration, fmtLife } from '../lib/format';
  import { indexAt, live, meanFrom, PERIOD, type LiveLine } from '../lib/live.svelte';
  import { settings } from '../lib/settings.svelte';
  import { usb } from '../lib/usb.svelte';
  import type { ChartCursor } from '../lib/types';

  const WINDOWS: [number, string][] = [[10, '10 s'], [30, '30 s'], [60, '1 min'], [120, '2 min'], [300, '5 min']];

  let log = $state(true);
  let showBand = $state(true);
  let showAvg = $state(true);
  let selected = $state<LiveLine | null>(null);
  let saveOpen = $state(false);
  let actionError = $state('');
  let wakeThresholdA = $state(5e-3);

  $effect(() => {
    api.lastConfig().then((l) => { if (l?.config.wake_threshold_ua) wakeThresholdA = l.config.wake_threshold_ua * 1e-6; }).catch(() => {});
  });

  const st = $derived(live.status);
  const p = $derived(st?.ppk2);
  const session = $derived(st?.session?.active ? st.session.session : undefined);
  const presetWindow = $derived(WINDOWS.some(([s]) => s === live.windowS));

  // Unexpected DUT resets in the recording, from the api (re-read every 15 s while recording).
  let resets = $state<{ count: number; groups: ResetGroup[] } | null>(null);
  const sessionId = $derived(session?.id ?? null);
  $effect(() => {
    const id = sessionId;
    resets = null;
    if (id == null) return;
    const load = () => api.session(id).then((d) => { if (sessionId === id) resets = d.resets; }).catch(() => {});
    void load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  });

  const stats = $derived.by(() => {
    void live.version;
    const x = live.x;
    const n = x.length;
    if (!n) return null;
    const last = x[n - 1];
    let peak: number | null = null;
    for (const v of live.mx) if (v != null && (peak == null || v > peak)) peak = v;
    return { now: meanFrom(live.avg, indexAt(x, last - 0.5) + 1), win: meanFrom(live.avg, 0), peak };
  });
  const sessionAvg = $derived(session && session.duration_s > 0 ? session.charge_c / session.duration_s : null);
  const lifeAvg = $derived(sessionAvg ?? stats?.win ?? null);

  /** The last firmware flash, while it's in the live buffer: shaded on the chart. */
  const flashHighlights = $derived.by(() => {
    const f = live.status?.flash;
    const end = live.status?.buffer.end_idx;
    if (!f || f.start_idx == null || end == null || f.status === 'cancelled') return [];
    const running = f.end_idx == null;
    return [{ a: f.start_idx * PERIOD, b: (f.end_idx ?? end) * PERIOD, label: running ? 'flashing…' : `flash ${f.meta.version ?? ''}`.trim() }];
  });
  const markers = $derived.by(() => {
    void live.version;
    const x0 = live.x[0] ?? Infinity;
    const x1 = live.x[live.x.length - 1] ?? -Infinity;
    const out: number[] = [];
    for (const l of live.lines) if (l.x != null && l.x >= x0 && l.x <= x1) out.push(l.x);
    return out;
  });

  const cursor = $derived.by<ChartCursor>(() => {
    void live.version;
    if (!selected || selected.x == null) return null;
    const i = indexAt(live.x, selected.x);
    return { x: selected.x, title: `${fmtClock(selected.wall)} · ${fmtAmps(i >= 0 ? live.avg[i] : null)}`, text: selected.text };
  });

  const clock = (s: number) => {
    const w = live.wallOf(s);
    return w == null ? `${s.toFixed(3)} s` : fmtClock(w);
  };
  const viewText = $derived(live.view ? `${clock(live.view.a)} – ${clock(live.view.b)} (${fmtSpanLocal(live.view.b - live.view.a)})` : '');

  function fmtSpanLocal(s: number): string {
    if (s < 0.001) return `${(s * 1e6).toFixed(0)} µs`;
    if (s < 1) return `${(s * 1e3).toFixed(s < 0.01 ? 2 : 1)} ms`;
    return `${s.toFixed(s < 10 ? 3 : 1)} s`;
  }

  async function startMeasuring() {
    actionError = '';
    try {
      await api.setStream(true);
      await live.pollStatus();
      live.refresh();
    } catch (e) {
      actionError = (e as Error).message;
    }
  }

  async function oneCycle() {
    actionError = '';
    try {
      await live.showOneCycle(wakeThresholdA);
    } catch (e) {
      actionError = (e as Error).message;
    }
  }

  const uartStatus = $derived(
    st ? `UART ${st.uart.connected ? 'connected' : 'offline'} · ${st.uart.baud} baud · ${live.lines.length} lines` : '',
  );
</script>

<div class="page">
  {#if live.statusError}
    <div class="banner bad">Can't reach the API: {live.statusError}. Retrying…</div>
  {:else if p && !p.connected}
    <div class="banner bad">PPK2 not connected{p.error ? ` (${p.error})` : ''}. Plug it in: it reconnects by itself.</div>
  {:else if p && !p.streaming}
    <div class="banner">Measuring is off. <button class="btn small primary" type="button" onclick={startMeasuring}>Start measuring</button></div>
  {/if}
  {#if actionError}<div class="banner bad">{actionError}</div>{/if}

  <ControlStrip bind:saveOpen />

  <section class="kpis" aria-label="Summary">
    <div class="kpi"><div class="label">Now</div><div class="value">{fmtAmps(stats?.now)}</div><div class="sub">{live.paused ? 'at the end of the view' : 'average of the last 0.5 s'}</div></div>
    <div class="kpi"><div class="label">{live.paused ? 'On screen' : 'Visible window'}</div><div class="value">{fmtAmps(stats?.win)}</div><div class="sub">average · peak {fmtAmps(stats?.peak)}</div></div>
    <div class="kpi">
      <div class="label">Session</div>
      <div class="value">{session ? fmtDuration(session.duration_s) : 'not recording'}</div>
      <div class="sub">{session ? `${session.kind} · average ${fmtAmps(sessionAvg)}` : 'Record, or Save… afterwards'}</div>
      {#if session && resets?.count}
        <a class="sub bad" href="#/sessions/{session.id}" title="Unexpected resets of the DUT in this recording: open the session to see them">⚠ {resets.count} {resets.count === 1 ? 'reset' : 'resets'} · {resets.groups.slice(0, 2).map((g) => `${g.count}× ${g.label}`).join(', ')}{resets.groups.length > 2 ? ', …' : ''}</a>
      {/if}
    </div>
    {#if session?.battery}
      {@const b = session.battery}
      <div class="kpi">
        <div class="label">Emulated battery</div>
        <div class="value">{b.soc != null ? `${b.soc.toFixed(1)} % · ` : ''}{(b.voltage_mv / 1000).toFixed(3)} V</div>
        <div class="sub">{b.below_cutoff ? `below the cutoff, going down to ${(b.floor_mv / 1000).toFixed(2)} V` : b.eta_s != null ? `cutoff (${(b.cutoff_mv / 1000).toFixed(2)} V) in ${b.eta_s < 3600 ? `≈ ${fmtDuration(b.eta_s)}` : fmtLife(b.eta_s / 3600)}` : 'cutoff soon'} · then {b.on_cutoff === 'hunt' ? 'brown-out hunt' : 'DUT off'}</div>
      </div>
    {/if}
    <div class="kpi">
      <div class="label">Battery</div>
      <div class="value">{lifeAvg ? fmtLife(settings.batteries.selected_mah / (lifeAvg * 1000)) : '–'}</div>
      <div class="sub"><BatteryPicker /> at the {session ? 'session' : 'window'} average</div>
    </div>
    <div class="kpi">
      <div class="label">Data</div>
      <div class="value">{p ? `${p.stream.lost} lost` : '–'}</div>
      <div class="sub">{p?.stream.rate_sps ? `${(p.stream.rate_sps / 1000).toFixed(1)} kS/s` : 'no stream'}{p?.stream.clock_drift_ppm != null ? ` · clock ${p.stream.clock_drift_ppm.toFixed(1)} ppm` : ''}</div>
    </div>
  </section>

  <div class="body">
    <section class="panel chartpanel" aria-label="Current">
      <div class="panel-head">
        <h2>Current</h2>
        <div class="seg" role="group" aria-label="Time window">
          {#each WINDOWS as [s, label] (s)}
            <button type="button" aria-pressed={!live.paused && live.windowS === s} onclick={() => live.setWindow(s)}>{label}</button>
          {/each}
          <button type="button" aria-pressed={!live.paused && !presetWindow} title="Find the time between the last two wakes (current above {wakeThresholdA * 1e3} mA) and show one cycle" onclick={oneCycle}>1 cycle</button>
        </div>
        <div class="seg" role="group" aria-label="Scale">
          <button type="button" aria-pressed={log} onclick={() => (log = true)}>Log</button>
          <button type="button" aria-pressed={!log} onclick={() => (log = false)}>Lin</button>
        </div>
        <span class="spacer"></span>
        {#if live.zoomDepth > 0}
          <button class="btn small ghost" type="button" onclick={() => live.zoomOut()}>Zoom out</button>
        {/if}
      </div>

      {#if live.paused}
        <div class="sub-bar">
          <span><strong>Paused</strong> · {viewText}{live.loading ? ' · loading…' : ''}</span>
          <span class="faint">Drag across the chart to select · the live buffer keeps the last 5 minutes</span>
        </div>
      {/if}
      {#if live.selection}
        <div class="sel-bar" aria-live="polite">
          <span class="mono"><strong>Selection {fmtSpanLocal(live.selection.b - live.selection.a)}</strong></span>
          {#if live.selStats}
            <span class="mono">average {fmtAmps(live.selStats.mean_a)}</span>
            <span class="mono">charge {(live.selStats.charge_c * 1e3).toFixed(live.selStats.charge_c < 0.01 ? 3 : 1)} mC</span>
            <span class="mono">peak {fmtAmps(live.selStats.max_a)}</span>
            <span class="mono">min {fmtAmps(live.selStats.min_a)}</span>
            {#if live.selStats.lost}<span class="mono warn">{live.selStats.lost} samples missing</span>{/if}
          {:else}
            <span class="faint">calculating…</span>
          {/if}
          <span class="spacer"></span>
          <button class="btn small" type="button" onclick={() => live.selection && live.zoomTo(live.selection)}>Zoom in</button>
          <button class="btn small ghost" type="button" onclick={() => (saveOpen = true)}>Save…</button>
          <button class="btn small ghost icon" type="button" aria-label="Clear selection" onclick={() => live.clearSelection()}>×</button>
        </div>
      {/if}
      {#if live.message}<div class="info">{live.message}</div>{/if}

      <div class="chartwrap">
        <CurrentChart x={live.x} mn={live.mn} mx={live.mx} avg={live.avg} version={live.version} {log} {markers} {cursor} {showBand} {showAvg} highlights={flashHighlights}
          selection={live.selection} onselect={(r) => live.select(r)} endLabel={live.paused ? 'end' : 'now'} height={400}
          label="Current over time" />
      </div>
      <div class="legend">
        <button type="button" class="toggle" aria-pressed={showAvg} onclick={() => (showAvg = !showAvg)}><i class="sw line"></i>average</button>
        <button type="button" class="toggle" aria-pressed={showBand} onclick={() => (showBand = !showBand)}><i class="sw band"></i>min–max per pixel</button>
        <span><i class="sw tick"></i>log line</span>
        {#if flashHighlights.length}<span><i class="sw flash"></i>firmware flash</span>{/if}
        <span class="faint">Drag to select a range · click a log line to put the cursor there</span>
      </div>
    </section>

    <div class="logwrap">
      {#if usb.tab === 'usb' && live.status?.usb}
        <UsbConsole head={logTabs} />
      {:else}
      <LogPanel lines={live.lines} selected={selected?.seq ?? null} onselect={(l) => (selected = selected?.seq === l.seq ? null : l)}
        timeOf={(l) => fmtClock(l.wall)} follow={!live.paused} status={uartStatus} head={live.status?.usb ? logTabs : undefined} />
      {/if}
    </div>
  </div>
</div>

{#snippet logTabs()}
  <div class="seg" role="group" aria-label="Log source">
    <button type="button" aria-pressed={usb.tab === 'uart'} onclick={() => usb.setTab('uart')}>UART</button>
    <button type="button" aria-pressed={usb.tab === 'usb'} onclick={() => usb.setTab('usb')}
      title="The DUT's own USB console">USB{#if live.status?.usb?.wanted && live.status.usb.menu && usb.tab !== 'usb'}<span class="badge" aria-label="menu waiting"></span>{/if}</button>
  </div>
{/snippet}

<style>
  .badge { display: inline-block; width: 8px; height: 8px; margin-left: 6px; border-radius: 50%; background: var(--warn); }
  .sub.bad { color: var(--rec); display: block; text-decoration: none; }
  a.sub.bad:hover { text-decoration: underline; }
  .page { max-width: 1680px; margin: 0 auto; padding: 16px 24px 24px; display: flex; flex-direction: column; gap: 16px; }
  .banner { display: flex; align-items: center; gap: 12px; padding: 10px 14px; border-radius: 10px; border: 1px solid var(--line); background: var(--panel); }
  .banner.bad { border-color: var(--rec); color: var(--rec); background: var(--rec-bg); }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
  .body { display: flex; flex-wrap: wrap; gap: 16px; align-items: stretch; }
  .chartpanel { flex: 999 1 640px; min-width: 0; display: flex; flex-direction: column; }
  .sub-bar, .sel-bar, .info { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 16px; padding: 8px 16px; border-bottom: 1px solid var(--line); font-size: 13px; }
  .sub-bar { background: var(--panel2); }
  .sel-bar { background: var(--sel); }
  .info { color: var(--muted); }
  .warn { color: var(--warn); }
  .chartwrap { padding: 8px 12px 0 4px; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 16px; padding: 8px 16px 12px; font-size: 12px; color: var(--muted); }
  .legend span, .toggle { display: inline-flex; align-items: center; gap: 6px; }
  .toggle { border: 1px solid var(--line); border-radius: 6px; background: transparent; color: var(--muted); padding: 3px 8px; cursor: pointer; font-size: 12px; }
  .toggle[aria-pressed='false'] { opacity: 0.5; text-decoration: line-through; }
  .sw { display: inline-block; }
  .sw.line { width: 16px; height: 2px; background: var(--trace); }
  .sw.band { width: 16px; height: 10px; background: var(--trace-fill); }
  .sw.tick { width: 1px; height: 10px; background: var(--muted); }
  .sw.flash { width: 12px; height: 10px; background: var(--flash-bg); border-left: 2px solid var(--flash); }
  .logwrap { flex: 1 1 380px; min-width: 0; display: flex; height: 600px; }
  .logwrap > :global(*) { flex: 1 1 0; }
  :global(.u-select) { background: var(--sel) !important; box-shadow: inset 1px 0 0 var(--accent), inset -1px 0 0 var(--accent); }
  @media (max-width: 720px) {
    .page { padding: 12px; gap: 12px; }
    .kpis { gap: 8px; }
    .kpis :global(.kpi) { padding: 10px 12px; }
    .kpis :global(.kpi .value) { font-size: 18px; }
    .logwrap { height: 420px; }
  }
</style>
