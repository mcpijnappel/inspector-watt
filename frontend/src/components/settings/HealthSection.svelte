<script lang="ts">
  // System health: the services, the PPK2 stream, UART, the board's USB, live buffer and recording. Live from the status poll.
  import { api, type Health } from '../../lib/api';
  import { devPath, fmtBytes, fmtClock, fmtDuration } from '../../lib/format';
  import { live } from '../../lib/live.svelte';
  import SectionHead from './SectionHead.svelte';

  let health = $state<Health | null>(null);
  let cal = $state<Record<string, unknown> | null>(null);

  $effect(() => {
    const load = () => api.health().then((h) => (health = h)).catch(() => (health = null));
    void load();
    api.ppk2Metadata().then((m) => (cal = m.parsed)).catch(() => (cal = null));
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });

  const st = $derived(live.status);
  const p = $derived(st?.ppk2);
  const s = $derived(p?.stream);
  const u = $derived(st?.uart);
  const usb = $derived(st?.usb);
  const fl = $derived(st?.flash);
  const buf = $derived(st?.buffer);
  const n = (v: number | null | undefined, d = 0) => (v == null ? '–' : v.toLocaleString(undefined, { maximumFractionDigits: d }));
  const bufS = $derived(buf ? (buf.end_idx - buf.start_idx) * buf.sample_period_s : 0);
  const capS = $derived(buf ? buf.capacity * buf.sample_period_s : 0);
  type Row = [string, string, ('ok' | 'bad' | 'warn' | '')?];
  // Services as one line of pills: all fine = one glance.
  const services = $derived<Row[]>([
    ['API', live.statusError ? 'unreachable' : 'running', live.statusError ? 'bad' : 'ok'],
    ['Capture', health ? (health.capture ? 'running' : 'unreachable') : '…', health ? (health.capture ? 'ok' : 'bad') : ''],
    ['Database', health ? (health.db ? 'running' : 'unreachable') : '…', health ? (health.db ? 'ok' : 'bad') : ''],
    ['MCP server', health ? (health.mcp ? 'running' : 'unreachable') : '…', health ? (health.mcp ? 'ok' : 'warn') : ''],
  ]);
  const writes = $derived<Row>(['Recording writes', !st?.session ? '–' : st.session.active ? (st.session.db_ok ? 'ok' : st.session.error ?? 'waiting for the database') : st.session.error ?? 'not recording',
    !st?.session ? '' : st.session.active ? (st.session.db_ok ? 'ok' : 'bad') : st.session.error ? 'bad' : '']);
</script>

{#snippet card(title: string, rows: Row[])}
  <section class="panel card" aria-label={title}>
    <h3>{title}</h3>
    <dl>
      {#each rows as [k, v, state] (k)}
        <dt>{k}</dt><dd class="mono" class:ok={state === 'ok'} class:bad={state === 'bad'} class:warn={state === 'warn'}>{v}</dd>
      {/each}
    </dl>
  </section>
{/snippet}

<div class="s-section">
  <SectionHead title="System health" intro="Whether every part of the tracker is running and keeping up." />
  {#if live.statusError}<div class="s-banner">Can't reach the API: {live.statusError}</div>{/if}
  <section class="panel services" aria-label="Services">
    {#each [...services, writes] as [k, v, state] (k)}
      <span class="pill" class:ok={state === 'ok'} class:bad={state === 'bad'} class:warn={state === 'warn'} title="{k}: {v}">
        {#if state}<span class="dot" class:ok={state === 'ok'} class:bad={state === 'bad'} class:warn={state === 'warn'}></span>{/if}{k}{state === 'ok' && k !== 'Recording writes' ? '' : `: ${v}`}</span>
    {/each}
  </section>
  <div class="grid">
    {#if p}
      {@render card('PPK2', [
        ['Port', devPath(p.port)],
        ['Connected', p.connected ? 'yes' : `no${p.error ? ` (${p.error})` : ''}`, p.connected ? 'ok' : 'bad'],
        ['Measuring', p.streaming ? 'yes' : 'no', p.streaming ? 'ok' : 'warn'],
        ['Sample rate', s?.rate_sps ? `${n(s.rate_sps / 1000, 2)} kS/s of ${n((s.nominal_rate_sps ?? 100000) / 1000)}` : '–'],
        ['Lost samples', `${n(s?.lost)} of ${n(s?.samples)}`, s?.lost ? 'warn' : 'ok'],
        ['Gaps · corrupt · dropped', `${n(s?.gaps)} · ${n(s?.corrupt)} · ${s?.dropped_bytes ? fmtBytes(s.dropped_bytes) : '0'}`, s?.gaps || s?.corrupt ? 'warn' : ''],
        ['Reconnects · stalls', `${n(s?.reconnects)} · ${n(s?.stalls)}`, s?.reconnects || s?.stalls ? 'warn' : ''],
        ['Decoder load', s?.decode_load != null ? `${n(s.decode_load * 100, 1)} %` : '–', (s?.decode_load ?? 0) > 0.5 ? 'warn' : ''],
        ['Clock drift', s?.clock_drift_ppm != null ? `${n(s.clock_drift_ppm, 3)} ppm` : '–'],
        ['Measuring for', s?.elapsed_s ? fmtDuration(s.elapsed_s) : '–'],
        ['Mode · voltage', `${p.mode ?? '–'} · ${p.voltage_mv != null ? `${(p.voltage_mv / 1000).toFixed(2)} V` : '–'}`],
        ['Spike filter', p.spike_filter ? 'on' : 'off'],
      ])}
    {/if}
    {#if u}
      {@render card('UART (board serial)', [
        ['Port', devPath(u.port)],
        ['Connected', u.connected ? 'yes' : `no${u.error ? ` (${u.error})` : ''}`, u.connected ? 'ok' : 'bad'],
        ['Baud', String(u.baud)],
        ['Received', `${fmtBytes(u.bytes_read)} · ${n(u.lines)} lines`],
      ])}
    {/if}
    {#if usb}
      {@render card('USB port (console, flashing)', [
        ['Port', devPath(usb.port)],
        ['There now', usb.present ? 'yes (board awake)' : 'no (asleep, off or unplugged)', usb.present ? 'ok' : ''],
        ['Console', usb.held ? 'held by a flash' : usb.wanted ? (usb.connected ? 'open: the board can\'t deep-sleep' : 'open, waiting for the board') : 'closed', usb.wanted || usb.held ? 'warn' : ''],
        ['Menu waiting', usb.menu ? usb.menu.title : 'no', usb.menu ? 'warn' : ''],
        ['Error', usb.error ?? 'none', usb.error ? 'bad' : ''],
        ['Lines received', n(usb.lines)],
        ['Last upload', fl ? `${fl.status}${fl.finished ? ` at ${fmtClock(fl.finished).slice(0, 5)}` : ''} · ${fl.meta.version ?? ''} ${fl.meta.git ?? ''}`.trim() : 'none since start',
          fl?.status === 'failed' ? 'bad' : fl?.status === 'done' ? 'ok' : ''],
      ])}
    {/if}
    {#if st}
      {@render card('Live buffer & power', [
        ['Live buffer', `${fmtDuration(bufS)} of ${fmtDuration(capS)}`],
        ['Board power', p?.dut_power == null ? 'unknown (after a restart)' : p.dut_power ? 'on' : 'off'],
        ['Idle power-off', st.idle_power_off.after_s ? `after ${fmtDuration(st.idle_power_off.after_s)}${st.idle_power_off.in_s != null ? ` · in ${fmtDuration(st.idle_power_off.in_s)}` : ''}` : 'off'],
        ['Recording', st.session?.active && st.session.session ? `#${st.session.session.id} ${st.session.session.name} · ${fmtDuration(st.session.session.duration_s)}` : 'no'],
      ])}
    {/if}
  </div>
  {#if cal}
    <details class="panel s-fold">
      <summary>PPK2 calibration data</summary>
      <pre class="mono inner">{JSON.stringify(cal, null, 2)}</pre>
    </details>
  {/if}
</div>

<style>
  .services { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px; font-size: 12px; background: var(--panel2); color: var(--muted); }
  .pill.ok { background: var(--ok-bg); color: var(--ok); }
  .pill.bad { background: var(--rec-bg); color: var(--rec); }
  .pill.warn { background: var(--warn-bg); color: var(--warn); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; align-items: start; }
  .card { padding: 12px 16px; }
  h3 { margin: 0 0 6px; font-size: 14px; }
  /* one divider per row: no column gap, so the label's and the value's lines join */
  dl { display: grid; grid-template-columns: auto minmax(0, 1fr); margin: 0; }
  dt, dd { margin: 0; padding: 6px 0; border-top: 1px solid var(--line); font-size: 12px; }
  dt:first-of-type, dt:first-of-type + dd { border-top: 0; }
  dt { color: var(--muted); padding-right: 12px; }
  dd { text-align: right; overflow-wrap: anywhere; }
  dd.ok { color: var(--ok); }
  dd.bad { color: var(--rec); }
  dd.warn { color: var(--warn); }
  pre { margin: 0; font-size: 11px; max-height: 300px; overflow: auto; }
</style>
