<script lang="ts">
  // CPU and memory of each service and of the whole machine (Settings → Health). The api samples every 10 s and keeps
  // a day in memory: now, and min / avg / max over the last hour or day. CPU is % of one core (Docker's way).
  import { api, type Mmm, type Resources } from '../../lib/api';
  import { fmtDuration } from '../../lib/format';

  const PERIODS: [number, string][] = [[3600, '1 h'], [86400, '24 h']];
  const NAMES: Record<string, [string, string]> = {
    capture: ['Capture', 'PPK2, UART, USB, flashing'],
    api: ['API', 'web requests, sessions'],
    db: ['Database', 'TimescaleDB'],
    mcp: ['MCP server', 'agents'],
    web: ['Web', 'nginx'],
    analyzer: ['Logic analyzer', 'module'],
  };
  const ORDER = ['capture', 'api', 'db', 'mcp', 'web'];

  let period = $state(3600);
  let res = $state<Resources | null>(null);
  let error = $state('');

  $effect(() => {
    const p = period;
    const load = () => api.resources(p).then((r) => { res = r; error = ''; }).catch((e) => (error = (e as Error).message));
    void load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  });

  const services = $derived(
    [...(res?.services ?? [])].sort((a, b) => (ORDER.indexOf(a.name) + 1 || 99) - (ORDER.indexOf(b.name) + 1 || 99) || a.name.localeCompare(b.name)),
  );
  const totalMem = $derived(services.filter((s) => s.running).reduce((t, s) => t + (s.mem?.now ?? 0), 0));
  const totalCpu = $derived(services.filter((s) => s.running).reduce((t, s) => t + (s.cpu?.now ?? 0), 0));
  const host = $derived(res?.host);
  // The machine's CPU in the same unit as the services: % of one core (all cores busy = cores × 100 %)
  const cores = $derived(host?.cores ?? 1);
  const scale = (v: Mmm | null): Mmm | null => (v ? { now: v.now * cores, min: v.min * cores, avg: v.avg * cores, max: v.max * cores } : null);
  const hostCpu = $derived(scale(host?.cpu ?? null));
  const hostCpuLine = $derived((host?.cpu_line ?? []).map((v) => v * cores));

  const MiB = 2 ** 20;
  const mem = (b: number | null | undefined) => (b == null ? '–' : b >= 1024 * MiB ? `${(b / 1024 / MiB).toFixed(2)} GiB` : `${(b / MiB).toFixed(b < 10 * MiB ? 1 : 0)} MiB`);
  /** A min–max range in the unit of the average, without repeating it: "4.62–5.10" (GiB), "350–464" (MiB). */
  const memRange = (v: Mmm) => {
    const g = v.avg >= 1024 * MiB, d = g ? 1024 * MiB : MiB, n = (b: number) => (b / d).toFixed(g ? 2 : b < 10 * MiB ? 1 : 0);
    return `${n(v.min)}–${n(v.max)}`;
  };
  const uptime = (sec: number) => { const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600); return d ? `${d} d ${h} h` : `${h} h ${Math.floor((sec % 3600) / 60)} min`; };
  const pctRange = (v: Mmm) => `${pct(v.min)}–${pct(v.max)}`;
  const pct = (c: number | null | undefined) => (c == null ? '–' : c.toFixed(c < 10 ? 1 : 0));

  /** A small line with its area, the last point marked; `top` fixes the scale (CPU), else it fits the data. */
  function path(vals: number[], w: number, h: number, top?: number): { line: string; area: string; end: [number, number] } | null {
    if (vals.length < 2) return null;
    const lo = top != null ? 0 : Math.min(...vals) * 0.98;
    const hi = top ?? Math.max(...vals);
    const span = hi - lo || 1;
    const pts = vals.map((v, i) => [(i / (vals.length - 1)) * w, h - 2 - ((Math.min(v, hi) - lo) / span) * (h - 4)] as [number, number]);
    const line = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join('');
    return { line, area: `${line}L${w},${h}L0,${h}Z`, end: pts[pts.length - 1] };
  }
</script>

{#snippet cell(v: Mmm | null, f: (x: number) => string, range: (v: Mmm) => string, vals: number[], kind: 'cpu' | 'mem', top?: number, unit = '')}
  {@const p = path(vals, 44, 16, top)}
  <span class="cell">
    {#if p}<svg class="spark {kind}" width="44" height="16" viewBox="0 0 44 16" aria-hidden="true"><path class="area" d={p.area} /><path class="line" d={p.line} /></svg>{/if}
    {#if v}<b>{f(v.avg)}{unit}</b><span class="rng">{range(v)}</span>{:else}<span class="faint">–</span>{/if}
  </span>
{/snippet}

{#snippet gauge(label: string, now: string, of: string, frac: number | null, hot: boolean, vals: number[], kind: 'cpu' | 'mem', top: number | undefined, lines: string[])}
  {@const p = path(vals, 160, 30, top)}
  <div class="gauge" class:hot>
    <div class="g-top"><span class="g-label">{label}</span><b>{now}</b><span class="g-of">of {of}</span></div>
    {#if frac != null}<span class="g-bar"><i style:width="{Math.min(100, frac * 100)}%"></i></span>{/if}
    {#if p}<svg class="spark {kind}" width="160" height="30" viewBox="0 0 160 30" preserveAspectRatio="none" aria-hidden="true"><path class="area" d={p.area} /><path class="line" d={p.line} /></svg>{/if}
    {#each lines as l (l)}<span class="g-line">{l}</span>{/each}
  </div>
{/snippet}

<section class="panel res" aria-label="CPU and memory">
  <div class="head">
    <h3>CPU and memory</h3>
    <span class="spacer"></span>
    <div class="seg" role="group" aria-label="Period">
      {#each PERIODS as [s, label] (s)}<button type="button" aria-pressed={period === s} onclick={() => (period = s)}>{label}</button>{/each}
    </div>
  </div>
  {#if error}
    <p class="note bad">{error}</p>
  {:else if res && !res.available}
    <p class="note">Per-service figures need each service in its own cgroup slice (<code>cgroup_parent</code> in compose.yaml) and the host's <code>/sys/fs/cgroup</code> mounted in the api. Redeploy with <code>./dc up -d</code> to get them.</p>
  {/if}
  {#if res && res.kept_s < period - 60}
    <p class="note">Min · avg · max over the last {fmtDuration(Math.max(res.kept_s, res.interval_s))}: the api keeps what it measured since it started (every {res.interval_s} s, up to a day).</p>
  {/if}
  {#if host}
    <div class="pc">
      <div class="pc-head">
        <span class="dot" class:ok={!host.warn.length} class:warn={host.warn.length}></span><b>This PC</b>
        <span class="muted">{host.cores} cores · {mem(host.mem_total)}{host.uptime_s != null ? ` · up ${uptime(host.uptime_s)}` : ''}</span>
        {#if host.warn.length}<span class="hotnote">{host.warn.join(' and ')} nearly full</span>{/if}
      </div>
      <div class="pc-grid">
        {@render gauge('CPU', `${pct(hostCpu?.now)} %`, `${cores * 100} %`, hostCpu ? hostCpu.now / (cores * 100) : null, host.warn.includes('cpu'), hostCpuLine, 'cpu', cores * 100, [
          hostCpu ? `avg ${pct(hostCpu.avg)} % · ${pctRange(hostCpu)} · most ${pct(host.cpu_peak != null ? host.cpu_peak * cores : null)} %` : '',
          host.load ? `load ${host.load.map((x) => x.toFixed(1)).join(' · ')} (1 · 5 · 15 min)` : '',
        ].filter(Boolean))}
        {@render gauge('Memory', mem(host.mem?.now), mem(host.mem_total), host.mem && host.mem_total ? host.mem.now / host.mem_total : null, host.warn.includes('memory'), host.mem_line, 'mem', undefined, [
          host.mem ? `avg ${mem(host.mem.avg)} · ${memRange(host.mem)} · most ${mem(host.mem_peak)}` : '',
          host.swap_total ? `swap ${mem(host.swap_used)} of ${mem(host.swap_total)}` : 'no swap',
        ].filter(Boolean))}
        <div class="gauge">
          <div class="g-top"><span class="g-label">Inspector Watt</span><b>{mem(totalMem)}</b><span class="g-of">{host.mem_total ? `${((100 * totalMem) / host.mem_total).toFixed(0)} % of memory` : ''}</span></div>
          {#if host.mem_total}<span class="g-bar"><i style:width="{(100 * totalMem) / host.mem_total}%"></i></span>{/if}
          <span class="g-line">CPU {pct(totalCpu)} % of one core · {pct(totalCpu / cores)} % of the PC</span>
          <span class="g-line">{services.filter((x) => x.running).length} services running</span>
        </div>
      </div>
    </div>
  {/if}
  <div class="tbl">
    <table>
      <thead>
        <tr>
          <th>Service</th>
          <th class="num" title="% of one core: 100 % = one core fully busy">CPU <span class="sub">avg · min–max</span></th>
          <th class="num">Memory <span class="sub">avg · min–max</span></th>
          <th class="num" title="Memory: the kernel's peak since the service started. CPU: the highest 10-second average since the api started ({res ? fmtDuration(res.since_s) : '…'} ago); the kernel keeps no CPU peak.">Most</th>
        </tr>
      </thead>
      <tbody>
        {#each services as s (s.name)}
          <tr class:off={!s.running}>
            <td title={s.running ? NAMES[s.name]?.[1] ?? '' : 'not running'}>
              <span class="svc"><span class="dot" class:ok={s.running}></span>{NAMES[s.name]?.[0] ?? s.name}</span>
            </td>
            <td class="num">{@render cell(s.cpu, pct, pctRange, s.cpu_line, 'cpu', Math.max(10, ...s.cpu_line), ' %')}</td>
            <td class="num">{@render cell(s.mem, mem, memRange, s.mem_line, 'mem')}</td>
            <td class="num peak">{mem(s.peak)} · {pct(s.cpu_peak)} %</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
</section>

<style>
  .res { display: flex; flex-direction: column; min-width: 0; }
  .head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; padding: 12px 16px; border-bottom: 1px solid var(--line); }
  h3 { margin: 0; font-size: 14px; }
  .small { font-size: 12px; }
  .note { margin: 0; padding: 8px 16px; font-size: 12px; color: var(--muted); border-bottom: 1px solid var(--line); }
  .note.bad { color: var(--rec); }
  .tbl { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-weight: 500; color: var(--faint); font-size: 11.5px; padding: 6px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th .sub { font-weight: 400; margin-left: 4px; }
  td { padding: 4px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; vertical-align: middle; }
  tr:last-child td { border-bottom: 0; }
  .num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; font-size: 12px; }
  .svc { display: inline-flex; align-items: center; gap: 8px; font-family: var(--font); font-size: 13px; }
  tr.off td { color: var(--faint); }
  .pc { padding: 12px 16px; border-bottom: 1px solid var(--line); display: flex; flex-direction: column; gap: 10px; }
  .pc-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
  .hotnote { color: var(--warn); font-size: 12px; }
  .pc-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
  .gauge { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 5px; min-width: 0; }
  .gauge.hot { border-color: var(--warn); }
  .g-top { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
  .g-label { color: var(--muted); font-size: 12px; margin-right: auto; }
  .g-top b { font-family: var(--mono); font-size: 18px; font-weight: 600; }
  .g-of { color: var(--faint); font-size: 12px; font-family: var(--mono); }
  .gauge.hot .g-top b { color: var(--warn); }
  .g-bar { display: block; height: 5px; border-radius: 3px; background: var(--line); overflow: hidden; }
  .g-bar i { display: block; height: 100%; background: var(--accent); }
  .gauge.hot .g-bar i { background: var(--warn); }
  .gauge .spark { width: 100%; height: 30px; }
  .g-line { color: var(--faint); font-size: 11.5px; font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .cell { display: inline-flex; align-items: center; gap: 8px; }
  .cell b { font-weight: 600; min-width: 8ch; }
  .rng { color: var(--faint); font-size: 11px; min-width: 9ch; text-align: left; }
  .peak { color: var(--muted); }
  .spark { display: block; }
  .spark .line { fill: none; stroke-width: 1.3; }
  .spark.cpu .line { stroke: var(--accent); }
  .spark.cpu .area { fill: var(--accent); }
  .spark.mem .line { stroke: var(--flash); }
  .spark.mem .area { fill: var(--flash); }
  .spark .area { opacity: 0.14; }
  code { font-family: var(--mono); font-size: 11.5px; }
</style>
