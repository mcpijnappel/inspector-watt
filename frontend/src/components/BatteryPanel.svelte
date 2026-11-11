<script lang="ts">
  // Battery-emulation session: the voltage over time, how it ended, and the voltage at the last wake
  // (in a brown-out hunt, that is about where the board gave up).
  import { api, type BatteryForm, type Chemistry, type SessionEvent } from '../lib/api';
  import { batteryLabel, loadChemistries } from '../lib/battery';
  import { fmtSessionTime, fmtSpan } from '../lib/format';

  type Props = { id: number; duration: number; battery: BatteryForm; startMv: number | null; wakes: SessionEvent[] };
  let { id, duration, battery, startMv, wakes }: Props = $props();

  let steps = $state<SessionEvent[]>([]);
  let end = $state<SessionEvent | null>(null);
  let chems = $state<Chemistry[]>([]);
  $effect(() => {
    loadChemistries().then((c) => (chems = c)).catch(() => {});
  });

  $effect(() => {
    void id;
    api.events(id, 'battery').then((e) => (steps = e)).catch(() => {});
    api.events(id, 'battery_end').then((e) => (end = e[0] ?? null)).catch(() => {});
  });

  const mvOf = (e: SessionEvent) => Number(e.data.voltage_mv);
  /** (t, mV) points: start voltage, every logged step, the end. */
  const points = $derived.by<[number, number][]>(() => {
    const out: [number, number][] = [];
    if (startMv) out.push([0, startMv]);
    for (const e of steps) out.push([e.t, mvOf(e)]);
    if (end) out.push([end.t, mvOf(end)]);
    return out;
  });
  const vAt = (t: number) => {
    let v: number | null = null;
    for (const [pt, mv] of points) if (pt <= t) v = mv;
    return v;
  };

  const lastWake = $derived(wakes.length ? wakes[wakes.length - 1] : null);
  const lastWakeMv = $derived(lastWake ? vAt(lastWake.t) : null);
  const finalMv = $derived(points.length ? points[points.length - 1][1] : null);

  // chart geometry
  const W = 600;
  const H = 120;
  const lo = $derived(Math.min(battery.on_cutoff === 'hunt' ? battery.floor_mv : battery.cutoff_mv, ...points.map((p) => p[1])) - 20);
  const hi = $derived(Math.max(startMv ?? 0, ...points.map((p) => p[1])) + 20);
  const xOf = (t: number) => (duration > 0 ? (t / duration) * W : 0);
  const yOf = (mv: number) => H - ((mv - lo) / (hi - lo || 1)) * H;
  const path = $derived.by(() => {
    if (!points.length) return '';
    let d = `M${xOf(points[0][0]).toFixed(1)},${yOf(points[0][1]).toFixed(1)}`;
    for (let i = 1; i < points.length; i++) d += ` H${xOf(points[i][0]).toFixed(1)} V${yOf(points[i][1]).toFixed(1)}`;
    if (!end) d += ` H${W}`;
    return d;
  });
  const fmtMah = (m: number) => `${m.toFixed(m < 0.1 ? 4 : m < 10 ? 2 : 1)} mAh`;
  const V = (mv: number | null) => (mv == null ? '–' : `${(mv / 1000).toFixed(3)} V`);
  const settingsText = $derived(
    battery.mode === 'charge'
      ? `${batteryLabel(battery, chems)} ${battery.capacity_mah} mAh${battery.speedup !== 1 ? `, ${battery.speedup}× faster (as ${fmtMah(battery.capacity_mah / battery.speedup)})` : ''}, from ${battery.start_soc} %`
      : `sweep from ${(battery.start_mv / 1000).toFixed(2)} V to the cutoff in ${battery.sweep_min} min`,
  );
</script>

<section class="panel" aria-label="Emulated battery">
  <div class="panel-head"><h2>Emulated battery</h2><span class="faint small">{settingsText}</span></div>
  <div class="body">
    <svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" aria-label="Supply voltage over the session, from {V(startMv)} to {V(finalMv)}">
      <line x1="0" x2={W} y1={yOf(battery.cutoff_mv)} y2={yOf(battery.cutoff_mv)} class="cut" vector-effect="non-scaling-stroke" />
      <path d={path} class="v" vector-effect="non-scaling-stroke" />
      {#if lastWake && lastWakeMv != null}
        <circle cx={xOf(lastWake.t)} cy={yOf(lastWakeMv)} r="3" class="dot" />
      {/if}
    </svg>
    <div class="axis mono faint"><span>{V(startMv)}</span><span>cutoff {(battery.cutoff_mv / 1000).toFixed(2)} V (dashed)</span><span>{fmtSpan(duration)}</span></div>
    <dl>
      <dt>Ended</dt>
      <dd>
        {#if end}
          {end.data.reason === 'floor' ? `reached the floor (${V(mvOf(end))})` : `reached the cutoff (${V(mvOf(end))})`} after {fmtSpan(end.t)}: board off
        {:else}
          at {V(finalMv)}{battery.mode === 'charge' && steps.length && steps[steps.length - 1].data.soc != null ? ` (${Number(steps[steps.length - 1].data.soc).toFixed(1)} %)` : ''}, before the cutoff
        {/if}
      </dd>
      <dt>Last wake</dt>
      <dd>{lastWake ? `${fmtSessionTime(lastWake.t, duration)} at ${V(lastWakeMv)}` : 'none'}{lastWake && end && end.t - lastWake.t > 60 ? ` · nothing for ${fmtSpan(end.t - lastWake.t)} before the end: the brown-out is around here` : ''}</dd>
    </dl>
  </div>
</section>

<style>
  .body { padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 6px; }
  svg { width: 100%; height: 120px; display: block; }
  .v { fill: none; stroke: var(--accent); stroke-width: 1.75; }
  .cut { stroke: var(--rec); stroke-dasharray: 4 3; stroke-width: 1; }
  .dot { fill: var(--warn); }
  .axis { display: flex; justify-content: space-between; font-size: 11px; }
  .small { font-size: 12px; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; margin: 6px 0 0; font-size: 13px; }
  dt { color: var(--muted); }
  dd { margin: 0; }
</style>
