<script lang="ts">
  // Current over time with uPlot: mean line + min/max band, optional log scale,
  // log-line "rug" ticks along the top and a cursor with a small label.
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';
  import { untrack } from 'svelte';
  import { fmtAmpsAxis, fmtRel } from '../lib/format';
  import { theme } from '../lib/theme.svelte';
  import type { ChartBand, ChartCursor } from '../lib/types';
  import type { ChartScale } from '../lib/lanes';
  import type { Range } from '../lib/live.svelte';

  type Props = {
    x: number[];
    mn: (number | null)[];
    mx: (number | null)[];
    avg: (number | null)[];
    version: number;
    log?: boolean;
    markers?: number[];
    /** Times of something that went wrong (unexpected resets): a red line over the full height. */
    alerts?: number[];
    cursor?: ChartCursor;
    showBand?: boolean;
    showAvg?: boolean;
    selection?: Range | null;
    bands?: ChartBand[];
    /** bands: light wash + bottom strip · lines: thin line at each phase start + thin strip · ticks: only the lines */
    bandStyle?: 'bands' | 'lines' | 'ticks';
    /** Things done to the board (a firmware flash): a wash in the flash colour with a line and a label at the start. */
    highlights?: { a: number; b: number; label?: string }[];
    onselect?: (r: Range) => void;
    endLabel?: string;
    height?: number;
    xLabel?: (v: number, latest: number) => string;
    label?: string;
    /** Called when the x scale or the plot area changes (chip lanes under the chart line up with it). */
    onscale?: (s: ChartScale) => void;
  };

  let { x, mn, mx, avg, version, log = true, markers = [], alerts = [], cursor = null, showBand = true, showAvg = true, selection = null, bands = [], bandStyle = 'bands', highlights = [], onselect, endLabel = 'now', height = 360, xLabel, label = 'Current over time', onscale }: Props = $props();

  let host: HTMLDivElement;
  let plot: uPlot | null = null;
  let cursorLeft = $state<number | null>(null);
  const FLOOR = 1e-8; // log scale can't show 0

  const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const latest = () => (x.length ? x[x.length - 1] : 0);
  const clamp = (a: (number | null)[]) => (log ? a.map((v) => (v == null ? null : Math.max(v, FLOOR))) : a);
  const data = () => [x, clamp(mx), clamp(mn), clamp(avg)] as uPlot.AlignedData;

  // Phase bands: a light wash behind the data plus a solid strip along the bottom edge.
  function drawBands(u: uPlot) {
    if (!bands.length) return;
    const ctx = u.ctx;
    const dpr = devicePixelRatio || 1;
    const { top, left, width, height: h } = u.bbox;
    ctx.save();
    const stripH = (bandStyle === 'bands' ? 5 : 3) * dpr;
    let lastLine = -Infinity;
    for (const b of bands) {
      const start = u.valToPos(b.a, 'x', true);
      const x0 = Math.max(left, start);
      const x1 = Math.min(left + width, u.valToPos(b.b, 'x', true));
      if (x1 <= x0) continue;
      ctx.fillStyle = b.color;
      if (bandStyle === 'bands') {
        ctx.globalAlpha = 0.1;
        ctx.fillRect(x0, top, x1 - x0, h);
      } else if (start >= left && start - lastLine >= 3 * dpr) {
        // one thin line per phase start; skipped where lines would sit on top of each other
        ctx.globalAlpha = 0.7;
        ctx.fillRect(Math.round(start), top, Math.max(1, dpr), h);
        lastLine = start;
      }
      if (bandStyle === 'ticks') continue;
      ctx.globalAlpha = 0.9;
      ctx.fillRect(x0, top + h - stripH, Math.max(x1 - x0, 1), stripH);
    }
    ctx.restore();
  }

  function drawHighlights(u: uPlot) {
    if (!highlights.length) return;
    const ctx = u.ctx;
    const dpr = devicePixelRatio || 1;
    const { top, left, width, height: h } = u.bbox;
    ctx.save();
    for (const hl of highlights) {
      const start = u.valToPos(hl.a, 'x', true);
      const x0 = Math.max(left, start);
      const x1 = Math.min(left + width, Math.max(u.valToPos(hl.b, 'x', true), start + 3 * dpr));
      if (x1 <= x0) continue;
      ctx.fillStyle = css('--flash-bg');
      ctx.fillRect(x0, top, x1 - x0, h);
      if (start < left) continue;
      ctx.fillStyle = css('--flash');
      ctx.fillRect(Math.round(start), top, Math.max(1, 1.5 * dpr), h);
      if (hl.label) {
        ctx.font = `600 ${11 * dpr}px ${css('--font') || 'sans-serif'}`;
        ctx.textBaseline = 'top';
        const tw = ctx.measureText(hl.label).width;
        ctx.fillText(hl.label, Math.min(start + 5 * dpr, left + width - tw - 2 * dpr), top + 4 * dpr);
      }
    }
    ctx.restore();
  }

  let lastScale = '';
  function tellScale(u: uPlot) {
    if (!onscale) return;
    const dpr = devicePixelRatio || 1;
    const s = { min: u.scales.x.min ?? 0, max: u.scales.x.max ?? 1, left: u.bbox.left / dpr, width: u.bbox.width / dpr };
    const key = `${s.min}|${s.max}|${s.left}|${s.width}`;
    if (key !== lastScale) {
      lastScale = key;
      onscale(s);
    }
  }

  function drawOverlay(u: uPlot) {
    tellScale(u);
    const ctx = u.ctx;
    const dpr = devicePixelRatio || 1;
    const { top, left, width, height: h } = u.bbox;
    ctx.save();
    ctx.strokeStyle = css('--muted');
    ctx.lineWidth = dpr;
    for (const m of markers) {
      const px = u.valToPos(m, 'x', true);
      if (px < left || px > left + width) continue;
      ctx.beginPath();
      ctx.moveTo(px, top);
      ctx.lineTo(px, top + 7 * dpr);
      ctx.stroke();
    }
    ctx.strokeStyle = css('--rec');
    ctx.lineWidth = 1.5 * dpr;
    for (const m of alerts) {
      const px = u.valToPos(m, 'x', true);
      if (px < left || px > left + width) continue;
      ctx.beginPath();
      ctx.moveTo(px, top);
      ctx.lineTo(px, top + h);
      ctx.stroke();
    }
    ctx.lineWidth = dpr;
    let pos: number | null = null;
    if (cursor) {
      const px = u.valToPos(cursor.x, 'x', true);
      if (px >= left && px <= left + width) {
        ctx.strokeStyle = css('--accent');
        ctx.setLineDash([4 * dpr, 3 * dpr]);
        ctx.beginPath();
        ctx.moveTo(px, top);
        ctx.lineTo(px, top + h);
        ctx.stroke();
        pos = px / dpr;
      }
    }
    ctx.restore();
    cursorLeft = pos;
    // Keep the selection box attached to its time range when the data or zoom changes.
    if (selection) {
      const l = u.valToPos(selection.a, 'x');
      const r = u.valToPos(selection.b, 'x');
      u.setSelect({ left: Math.max(0, l), width: Math.max(0, Math.min(u.bbox.width / dpr, r) - Math.max(0, l)), top: 0, height: u.bbox.height / dpr }, false);
    } else if (u.select.width > 0) {
      u.setSelect({ left: 0, width: 0, top: 0, height: 0 }, false);
    }
  }

  // Log axis: one line and label per decade (1 µA, 10 µA, ...), not uPlot's 1..9 per decade.
  // uPlot rounds tick values before formatting (1e-4 would become 0), so labels come from this list.
  let decadeTicks: number[] = [];
  function decades(u: uPlot, _i: number, min: number, max: number): number[] {
    let out: number[] = [];
    for (let k = Math.ceil(Math.log10(min) - 1e-9); 10 ** k <= max * (1 + 1e-9); k++) out.push(10 ** k);
    // Low charts: label every 2nd/3rd decade so the labels don't overlap.
    const maxTicks = Math.max(2, Math.floor(u.bbox.height / (devicePixelRatio || 1) / 22));
    const every = Math.ceil(out.length / maxTicks);
    if (every > 1) out = out.filter((_, i) => (out.length - 1 - i) % every === 0);
    decadeTicks = out;
    return out;
  }

  // Time axis relative to the newest sample: ticks at round steps back from "now".
  const STEPS = [1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4, 1e-3, 2e-3, 5e-3, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1800, 3600];
  function relativeSplits(u: uPlot, _i: number, min: number, max: number): number[] {
    const end = latest();
    const maxTicks = Math.max(2, Math.floor(u.bbox.width / (devicePixelRatio || 1) / 80));
    const step = STEPS.find((s) => (max - min) / s <= maxTicks) ?? 3600;
    const out: number[] = [];
    for (let t = end; t >= min - 1e-9 && out.length < 200; t -= step) if (t <= max + 1e-9) out.push(t);
    return out.reverse();
  }

  // Absolute time axis (xLabel given): ticks at round multiples of a step.
  function absoluteSplits(u: uPlot, _i: number, min: number, max: number): number[] {
    const maxTicks = Math.max(2, Math.floor(u.bbox.width / (devicePixelRatio || 1) / 90));
    const step = STEPS.find((s) => (max - min) / s <= maxTicks) ?? 3600;
    const out: number[] = [];
    for (let t = Math.ceil(min / step - 1e-9) * step; t <= max + 1e-9 && out.length < 200; t += step) out.push(t);
    return out;
  }

  function build() {
    plot?.destroy();
    const grid = css('--grid');
    const faint = css('--faint');
    const font = `11px ${css('--mono') || 'monospace'}`;
    const axis = { stroke: faint, font, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1 } };
    const opts: uPlot.Options = {
      width: host.clientWidth || 600,
      height,
      legend: { show: false },
      cursor: { show: true, x: true, y: false, points: { show: false }, drag: { x: true, y: false, setScale: false } },
      // Explicit ranges: with no data yet uPlot gets null min/max, which its log scale can't handle.
      scales: {
        x: { time: false, range: (_u, min, max) => (min == null || max == null || min === max ? [min ?? 0, (max ?? 0) + 1] : [min, max]) },
        y: log
          ? { distr: 3, log: 10, range: (_u, min, max) => (min == null || max == null ? [1e-6, 1e-1] : uPlot.rangeLog(Math.max(min, FLOOR), Math.max(max, FLOOR * 10), 10, true)) }
          : { range: (_u, _min, max) => [0, (max || 1e-3) * 1.1] },
      },
      series: [
        {},
        { stroke: 'transparent', points: { show: false } },
        { stroke: 'transparent', points: { show: false } },
        { stroke: showAvg ? css('--trace') : 'transparent', width: 1.5, points: { show: false } },
      ],
      bands: showBand ? [{ series: [1, 2], fill: css('--trace-fill') }] : [],
      axes: [
        {
          ...axis,
          splits: xLabel ? absoluteSplits : relativeSplits,
          values: (_u, splits) => splits.map((v) => (xLabel ? xLabel(v, latest()) : fmtRel(v - latest(), endLabel))),
        },
        {
          ...axis,
          size: 72,
          ...(log ? { splits: decades } : {}),
          values: (_u, splits) => (log && decadeTicks.length === splits.length ? decadeTicks : splits).map((v) => fmtAmpsAxis(v)),
        },
      ],
      hooks: {
        drawClear: [drawBands, drawHighlights],
        draw: [drawOverlay],
        setSelect: [
          (u) => {
            if (u.select.width < 3 || !onselect) return;
            const a = u.posToVal(u.select.left, 'x');
            const b = u.posToVal(u.select.left + u.select.width, 'x');
            if (!selection || Math.abs(selection.a - a) > 1e-9 || Math.abs(selection.b - b) > 1e-9) onselect({ a, b });
          },
        ],
      },
    };
    plot = new uPlot(opts, data(), host);
  }

  // (Re)build when the look changes.
  $effect(() => {
    void theme.resolved;
    void log;
    void height;
    void showBand;
    void showAvg;
    untrack(build);
    return () => {
      plot?.destroy();
      plot = null;
    };
  });

  // New data.
  $effect(() => {
    void version;
    untrack(() => plot?.setData(data()));
  });

  // Cursor, markers or selection changed.
  $effect(() => {
    void cursor;
    void markers;
    void alerts;
    void selection;
    void bands;
    void bandStyle;
    void highlights;
    untrack(() => plot?.redraw(false));
  });

  $effect(() => {
    const ro = new ResizeObserver(() => plot?.setSize({ width: host.clientWidth, height }));
    ro.observe(host);
    return () => ro.disconnect();
  });
</script>

<div class="chart" role="img" aria-label={label}>
  <div bind:this={host}></div>
  {#if cursor && cursorLeft != null}
    <div class="tip" style:left="{cursorLeft}px">
      <div class="mono muted small">{cursor.title}</div>
      <div class="mono text">{cursor.text}</div>
    </div>
  {/if}
</div>

<style>
  .chart { position: relative; min-width: 0; }
  .tip {
    position: absolute; top: 14px; margin-left: 10px; max-width: min(340px, 45%);
    background: var(--panel2); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px;
    box-shadow: var(--shadow); pointer-events: none;
  }
  .small { font-size: 12px; }
  .text { font-size: 12px; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  :global(.chart .u-wrap) { background: transparent; }
</style>
