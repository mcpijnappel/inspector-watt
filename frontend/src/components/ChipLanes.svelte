<script lang="ts">
  // Chip lanes under the current chart, on the chart's own time axis (its scale comes from the chart): one lane per
  // bus (I2C, SPI) with a tick per message (as wide as it took) in its chip's colour. Click a bus to open a lane per
  // chip; click a chip (its lane name, or its name in the key under the lanes) to pin it: it keeps its own lane while
  // the bus is closed. Hover a tick: what it was, and its moment shades in the chart. Decoded stretches of a
  // recording show as a light wash, ones the analyzer missed as hatching. Compare draws A's row over B's.
  import { theme } from '../lib/theme.svelte';
  import { rows as toRows, type ChartScale, type Lane, type LaneMark, type LaneWindow, type Row } from '../lib/lanes';

  type Hover = { a: number; b: number; label: string };
  type Props = {
    lanes: Lane[];
    scale: ChartScale | null;
    windows?: LaneWindow[];
    /** A message under the mouse (null: none): its span in chart x, for the chart's shading. */
    onhover?: (h: Hover | null) => void;
    /** Shaded from outside (a Bus panel row under the mouse): its ticks light up. */
    lit?: Hover | null;
    label?: string;
  };
  let { lanes, scale, windows = [], onhover, lit = null, label = 'Chip lanes' }: Props = $props();

  const LANE_H = 16;
  const OPEN_KEY = 'pst.lanes.open', PIN_KEY = 'pst.lanes.pinned';
  const load = (k: string): string[] => { try { return JSON.parse(localStorage.getItem(k) ?? '[]'); } catch { return []; } };
  const save = (k: string, v: string[]) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private window */ } };
  let open = $state<string[]>(load(OPEN_KEY));
  let pinned = $state<string[]>(load(PIN_KEY));
  const flip = (list: string[], x: string) => (list.includes(x) ? list.filter((y) => y !== x) : [...list, x]);
  function toggleBus(bus: string) { open = flip(open, bus); save(OPEN_KEY, open); }
  function togglePin(chip: string) { pinned = flip(pinned, chip); save(PIN_KEY, pinned); }

  let canvas = $state<HTMLCanvasElement>();
  let tip = $state<{ x: number; y: number; row: Row; m: LaneMark } | null>(null);
  let w = $state(0);

  const shown = $derived(toRows(lanes, new Set(open), new Set(pinned)));
  const height = $derived(shown.length * LANE_H + 4);
  // the key under the lanes: each closed bus's chips with their colour (one entry per chip, A and B together)
  const key = $derived.by(() => {
    const seen = new Map<string, Lane>();
    for (const l of lanes) if (!open.includes(l.bus) && !seen.has(l.chip)) seen.set(l.chip, l);
    return [...seen.values()];
  });
  const sides = $derived(lanes.some((l) => l.side));
  const css = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  function px(x: number, s: ChartScale) {
    return s.left + ((x - s.min) / Math.max(1e-12, s.max - s.min)) * s.width;
  }

  $effect(() => {
    void theme.resolved;
    const c = canvas, s = scale;
    void w; void lit; void tip; void windows;
    if (!c || !s || !shown.length) return;
    const dpr = devicePixelRatio || 1, cw = c.clientWidth;
    c.width = Math.round(cw * dpr);
    c.height = Math.round(height * dpr);
    const ctx = c.getContext('2d')!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, height);
    const right = s.left + s.width;
    for (const win of windows) {
      const a = Math.max(s.left, px(win.a, s)), b = Math.min(right, px(win.b, s));
      if (b <= a) continue;
      if (win.missed) {
        ctx.save();
        ctx.beginPath(); ctx.rect(a, 0, b - a, height); ctx.clip();
        ctx.strokeStyle = css('--rec'); ctx.globalAlpha = 0.35; ctx.lineWidth = 1;
        for (let x = a - height; x < b; x += 6) { ctx.beginPath(); ctx.moveTo(x, height); ctx.lineTo(x + height, 0); ctx.stroke(); }
        ctx.restore();
      } else {
        ctx.fillStyle = css('--sel');
        ctx.fillRect(a, 0, b - a, height);
      }
    }
    ctx.textBaseline = 'middle';
    const gutter = s.left - (sides ? 18 : 8);
    shown.forEach((r, i) => {
      const y0 = 2 + i * LANE_H, mid = y0 + LANE_H / 2;
      if (r.first) {
        ctx.fillStyle = css('--grid');
        ctx.fillRect(s.left, y0, s.width, 1);
      }
      if (r.label) {
        const isBus = r.kind === 'bus';
        ctx.font = `${isBus ? '600 ' : ''}11px ${css('--font') || 'sans-serif'}`;
        ctx.fillStyle = css(isBus ? '--text' : '--muted');
        const x0 = isBus ? 4 : 12;
        let t = isBus ? `${open.includes(r.bus) ? '▾' : '▸'} ${r.label}${!open.includes(r.bus) ? ` ${r.count}` : ''}` : (pinned.includes(r.chip!) ? '• ' : '') + r.label;
        const full = t;
        while (t.length > 3 && ctx.measureText(t).width > gutter - x0) t = t.slice(0, -2);
        if (t !== full) t = t.slice(0, -1) + '…';
        ctx.fillText(t, x0, mid);
      }
      if (r.side) {
        ctx.font = `10px ${css('--mono') || 'monospace'}`;
        ctx.fillStyle = css(r.side === 'a' ? '--trace' : '--faint');
        ctx.fillText(r.side.toUpperCase(), s.left - 13, mid);
      }
      for (const m of r.marks) {
        if (m.x2 < s.min || m.x > s.max) continue;
        const a = px(m.x, s), b = px(m.x2, s);
        const on = tip?.m === m || (lit != null && m.x <= lit.b && m.x2 >= lit.a);
        ctx.fillStyle = m.nack ? css('--rec') : on ? css('--accent') : m.color;
        ctx.fillRect(Math.round(Math.max(a, s.left)), y0 + (on ? 2 : 4), Math.max(2, Math.min(b, right) - Math.max(a, s.left)), LANE_H - (on ? 4 : 8));
      }
    });
  });

  function find(e: MouseEvent): { row: Row; m: LaneMark | null; inLabel: boolean } | null {
    const s = scale;
    if (!s || !canvas) return null;
    const rc = canvas.getBoundingClientRect(), x = e.clientX - rc.left, y = e.clientY - rc.top;
    const row = shown[Math.floor((y - 2) / LANE_H)];
    if (!row) return null;
    if (x < s.left) return { row, m: null, inLabel: true };
    let best: LaneMark | null = null, bd = 5;
    for (const m of row.marks) {
      const a = px(m.x, s), b = Math.max(px(m.x2, s), a + 2);
      const d = x < a ? a - x : x > b ? x - b : 0;
      if (d < bd) { bd = d; best = m; }
    }
    return { row, m: best, inLabel: false };
  }
  function move(e: MouseEvent) {
    const hit = find(e);
    if (canvas) canvas.style.cursor = hit?.inLabel ? 'pointer' : 'default';
    if (!hit?.m) {
      if (tip) { tip = null; onhover?.(null); }
      return;
    }
    const rc = canvas!.getBoundingClientRect();
    tip = { x: e.clientX - rc.left, y: e.clientY - rc.top, row: hit.row, m: hit.m };
    onhover?.({ a: hit.m.x, b: hit.m.x2, label: `${hit.m.who.split(' (')[0]}: ${hit.m.text}` });
  }
  function leave() {
    if (tip) { tip = null; onhover?.(null); }
  }
  function click(e: MouseEvent) {
    const hit = find(e);
    if (!hit?.inLabel) return;
    if (hit.row.kind === 'bus') toggleBus(hit.row.bus);
    else togglePin(hit.row.chip!);
  }
  const fmtMs = (s: number) => (s >= 1 ? `${s.toFixed(3)} s` : `${(s * 1000).toFixed(s < 0.01 ? 2 : 1)} ms`);
</script>

{#if lanes.length}
  <div class="lanes" bind:clientWidth={w}>
    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
    <canvas bind:this={canvas} style:height="{height}px" aria-label="{label}: {lanes.map((l) => `${l.title}${l.side ? ` ${l.side.toUpperCase()}` : ''} ${l.marks.length}`).join(', ')}"
      onmousemove={move} onmouseleave={leave} onclick={click}></canvas>
    {#if tip}
      <div class="tip" style:left="{Math.min(tip.x + 12, Math.max(0, w - 260))}px" style:top="{tip.y + 14}px">
        <b><i class="sq" style:background={tip.m.color}></i>{tip.m.who}</b>{#if tip.row.side} <span class="faint">{tip.row.side.toUpperCase()}</span>{/if}
        <div class="mono">{fmtMs(tip.m.x2 - tip.m.x)} · {tip.m.text}</div>
      </div>
    {/if}
    <div class="foot">
      {#each key as l (l.chip)}
        <button type="button" class="chip" class:pinned={pinned.includes(l.chip)} title="{l.title}: {pinned.includes(l.chip) ? 'unpin (back into the bus lane)' : 'pin: its own lane'}"
          onclick={() => togglePin(l.chip)}><i class="sq" style:background={l.color}></i>{l.label}</button>
      {/each}
      {#if windows.some((x) => x.missed)}<span><i class="hatch"></i>not decoded</span>{/if}
      <span class="faint">click I2C / SPI to open its chips · click a chip to give it its own lane</span>
    </div>
  </div>
{/if}

<style>
  .lanes { position: relative; padding-top: 2px; }
  canvas { display: block; width: 100%; }
  .tip { position: absolute; z-index: 3; pointer-events: none; max-width: 300px; background: var(--panel2); border: 1px solid var(--line);
    border-radius: 8px; padding: 6px 9px; font-size: 12px; box-shadow: var(--shadow); line-height: 1.45; }
  .tip .mono { font-size: 11.5px; overflow-wrap: anywhere; }
  .foot { display: flex; flex-wrap: wrap; gap: 4px 12px; padding: 3px 0 2px 72px; font-size: 11.5px; color: var(--faint); align-items: center; }
  .foot span { display: inline-flex; gap: 5px; align-items: center; }
  .sq { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
  .chip { border: 0; background: transparent; color: var(--muted); cursor: pointer; padding: 1px 4px; border-radius: 4px; font: inherit; display: inline-flex; align-items: center; }
  .chip:hover { background: var(--panel2); color: var(--text); }
  .chip.pinned { color: var(--text); font-weight: 600; }
  .chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .hatch { width: 12px; height: 9px; background: repeating-linear-gradient(135deg, var(--rec) 0 1px, transparent 1px 4px); opacity: 0.6; }
</style>
