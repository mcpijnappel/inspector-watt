<script lang="ts">
  // Two cycles on one time axis: A and B average current, B shifted by the alignment offset.
  // Phase strips of both along the bottom (A above B). Drag to zoom; hover shows both values.
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';
  import { untrack } from 'svelte';
  import { fmtAmps, fmtAmpsAxis } from '../lib/format';
  import { theme } from '../lib/theme.svelte';
  import type { Range } from '../lib/live.svelte';
  import type { ChartBand, Series } from '../lib/types';

  type Props = {
    a: Series;
    b: Series;
    version: number;
    log?: boolean;
    stripsA?: ChartBand[];
    stripsB?: ChartBand[];
    /** Visible x range (seconds from A's cycle start). */
    view: Range;
    onzoom?: (r: Range) => void;
    /** A marker line (e.g. the log line under the mouse), in chart x. */
    cursorX?: number | null;
    /** Mouse over the chart: x under it, or null when it leaves. */
    onhover?: (x: number | null) => void;
    height?: number;
  };
  let { a, b, version, log = true, stripsA = [], stripsB = [], view, onzoom, cursorX = null, onhover, height = 360 }: Props = $props();

  let host: HTMLDivElement;
  let plot: uPlot | null = null;
  let hover = $state<{ left: number; flip: boolean; x: number; va: number | null; vb: number | null } | null>(null);
  const FLOOR = 1e-8;

  const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const clamp = (v: number | null | undefined) => (v == null ? v : log ? Math.max(v, FLOOR) : v);

  function data(): uPlot.AlignedData {
    // Join both onto one x axis; points only one series has become `undefined` there (spanned, not a gap).
    const j = uPlot.join([[a.x, a.y] as uPlot.AlignedData, [b.x, b.y] as uPlot.AlignedData]);
    return [j[0], (j[1] as (number | null)[]).map(clamp), (j[2] as (number | null)[]).map(clamp)] as uPlot.AlignedData;
  }

  function drawStrips(u: uPlot) {
    const ctx = u.ctx;
    const dpr = devicePixelRatio || 1;
    const { top, left, width, height: h } = u.bbox;
    const sh = 5 * dpr;
    ctx.save();
    const rows: [ChartBand[], number][] = [[stripsA, top + h - 2 * sh - dpr], [stripsB, top + h - sh]];
    for (const [bands, y] of rows) {
      for (const band of bands) {
        const x0 = Math.max(left, u.valToPos(band.a, 'x', true));
        const x1 = Math.min(left + width, u.valToPos(band.b, 'x', true));
        if (x1 <= x0) continue;
        ctx.fillStyle = band.color;
        ctx.globalAlpha = 0.9;
        ctx.fillRect(x0, y, Math.max(1, x1 - x0), sh);
      }
    }
    if (cursorX != null) {
      const px = u.valToPos(cursorX, 'x', true);
      if (px >= left && px <= left + width) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = css('--text');
        ctx.lineWidth = dpr;
        ctx.setLineDash([4 * dpr, 3 * dpr]);
        ctx.beginPath();
        ctx.moveTo(px, top);
        ctx.lineTo(px, top + h);
        ctx.stroke();
      }
    }
    ctx.restore();
  }

  const STEPS = [1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4, 1e-3, 2e-3, 5e-3, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600];
  let step = 1;
  function splits(u: uPlot, _i: number, min: number, max: number): number[] {
    const maxTicks = Math.max(2, Math.floor(u.bbox.width / (devicePixelRatio || 1) / 90));
    step = STEPS.find((s) => (max - min) / s <= maxTicks) ?? 600;
    const out: number[] = [];
    for (let t = Math.ceil(min / step - 1e-9) * step; t <= max + 1e-9 && out.length < 200; t += step) out.push(t);
    return out;
  }
  function tick(v: number): string {
    const r = Math.abs(v) < step / 2 ? 0 : v;
    if (step < 1e-3) return `${(r * 1e3).toFixed(step < 1e-4 ? 2 : 1)} ms`;
    if (step < 1) return `${(r * 1e3).toFixed(0)} ms`;
    return `${Math.round(r)} s`;
  }

  let decadeTicks: number[] = [];
  function decades(u: uPlot, _i: number, min: number, max: number): number[] {
    let out: number[] = [];
    for (let k = Math.ceil(Math.log10(min) - 1e-9); 10 ** k <= max * (1 + 1e-9); k++) out.push(10 ** k);
    const maxTicks = Math.max(2, Math.floor(u.bbox.height / (devicePixelRatio || 1) / 22));
    const every = Math.ceil(out.length / maxTicks);
    if (every > 1) out = out.filter((_, i) => (out.length - 1 - i) % every === 0);
    decadeTicks = out;
    return out;
  }

  function build() {
    plot?.destroy();
    const grid = css('--grid');
    const faint = css('--faint');
    const font = `11px ${css('--mono') || 'monospace'}`;
    const axis = { stroke: faint, font, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1 } };
    plot = new uPlot({
      width: host.clientWidth || 600,
      height,
      legend: { show: false },
      cursor: { show: true, x: true, y: false, points: { show: false }, drag: { x: true, y: false, setScale: false } },
      scales: {
        x: { time: false, range: () => [view.a, view.b] },
        y: log
          ? { distr: 3, log: 10, range: (_u, min, max) => (min == null || max == null ? [1e-6, 1e-1] : uPlot.rangeLog(Math.max(min, FLOOR), Math.max(max, FLOOR * 10), 10, true)) }
          : { range: (_u, _min, max) => [0, (max || 1e-3) * 1.1] },
      },
      series: [
        {},
        { stroke: css('--cmp-a'), width: 1.5, points: { show: false } },
        { stroke: css('--cmp-b'), width: 1.5, points: { show: false } },
      ],
      axes: [
        { ...axis, splits, values: (_u, s) => s.map(tick) },
        { ...axis, size: 72, ...(log ? { splits: decades } : {}), values: (_u, s) => (log && decadeTicks.length === s.length ? decadeTicks : s).map((v) => fmtAmpsAxis(v)) },
      ],
      hooks: {
        draw: [drawStrips],
        setCursor: [
          (u) => {
            const i = u.cursor.idx;
            if (i == null || u.cursor.left == null || u.cursor.left < 0) {
              if (hover) onhover?.(null);
              hover = null;
              return;
            }
            onhover?.(u.posToVal(u.cursor.left, 'x'));
            const near = (k: 1 | 2) => {
              // the joined data has gaps where only the other series has a point: look around
              const ys = u.data[k] as (number | null | undefined)[];
              for (let d = 0; d < 40; d++) {
                if (ys[i - d] != null) return ys[i - d] as number;
                if (ys[i + d] != null) return ys[i + d] as number;
              }
              return null;
            };
            const left = u.cursor.left + u.over.offsetLeft;
            hover = { left, flip: left > host.clientWidth - 200, x: u.data[0][i], va: near(1), vb: near(2) };
          },
        ],
        setSelect: [
          (u) => {
            if (u.select.width < 3 || !onzoom) return;
            const r = { a: u.posToVal(u.select.left, 'x'), b: u.posToVal(u.select.left + u.select.width, 'x') };
            u.setSelect({ left: 0, width: 0, top: 0, height: 0 }, false);
            onzoom(r);
          },
        ],
      },
    }, data(), host);
  }

  $effect(() => {
    void theme.resolved;
    void log;
    void height;
    untrack(build);
    return () => {
      plot?.destroy();
      plot = null;
    };
  });

  $effect(() => {
    void version;
    void view;
    untrack(() => {
      if (!plot) return;
      plot.setData(data(), false);
      plot.setScale('x', { min: view.a, max: view.b });
    });
  });

  $effect(() => {
    void stripsA;
    void stripsB;
    void cursorX;
    untrack(() => plot?.redraw(false));
  });

  $effect(() => {
    const ro = new ResizeObserver(() => plot?.setSize({ width: host.clientWidth, height }));
    ro.observe(host);
    return () => ro.disconnect();
  });

  const fmtX = (s: number) => (Math.abs(s) < 1 ? `${(s * 1e3).toFixed(2)} ms` : `${s.toFixed(4)} s`);
</script>

<div class="chart">
  <div bind:this={host} role="img" aria-label="Current of cycle A and cycle B on one time axis"></div>
  {#if hover}
    <div class="tip mono" class:flip={hover.flip} style:left="{hover.left}px">
      <div class="muted">{fmtX(hover.x)}</div>
      <div><i class="sw" style:background="var(--cmp-a)"></i>A {fmtAmps(hover.va)}</div>
      <div><i class="sw" style:background="var(--cmp-b)"></i>B {fmtAmps(hover.vb)}</div>
    </div>
  {/if}
</div>

<style>
  .chart { position: relative; min-width: 0; }
  .tip {
    position: absolute; top: 10px; margin-left: 10px; pointer-events: none; font-size: 12px;
    background: var(--panel2); border: 1px solid var(--line); border-radius: 8px; padding: 6px 9px; box-shadow: var(--shadow);
    display: flex; flex-direction: column; gap: 2px; white-space: nowrap;
  }
  .tip.flip { transform: translateX(-100%); margin-left: -10px; }
  .sw { display: inline-block; width: 12px; height: 3px; margin-right: 6px; vertical-align: middle; }
  :global(.chart .u-wrap) { background: transparent; }
</style>
