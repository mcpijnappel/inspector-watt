<script lang="ts">
  // Serial lines of cycle A and B on the compare page's aligned time axis.
  // Merged: one list ordered by time; lines that match (same text apart from numbers) share a row, lines only
  // one side has are tinted. Side by side: two lists that scroll together by time.
  import { untrack } from 'svelte';
  import { api, type CycleRow } from '../lib/api';
  import type { Range } from '../lib/live.svelte';

  type Props = {
    sidA: number;
    sidB: number;
    cycA: CycleRow;
    cycB: CycleRow;
    /** B's offset on the chart (s). */
    d: number;
    view: Range;
    /** x under the mouse in the chart: the nearest lines get highlighted. */
    hoverX: number | null;
    onhover: (x: number | null) => void;
  };
  let { sidA, sidB, cycA, cycB, d, view, hoverX, onhover }: Props = $props();

  type Line = { x: number; text: string; key: string; level: string };
  type Row = { x: number; a: Line | null; b: Line | null };

  let mode = $state<'merged' | 'side'>(loadPref());
  let onlyDiff = $state(false);
  let linked = $state(loadLinked());
  let query = $state('');
  let linesA = $state.raw<Line[]>([]);
  let linesB = $state.raw<Line[]>([]);
  let loading = $state(false);
  let truncated = $state(false);
  const LIMIT = 3000;

  function loadPref(): 'merged' | 'side' {
    try {
      return localStorage.getItem('pst.cmp.log') === 'side' ? 'side' : 'merged';
    } catch {
      return 'merged';
    }
  }
  function loadLinked(): boolean {
    try {
      return localStorage.getItem('pst.cmp.linked') !== '0';
    } catch {
      return true;
    }
  }
  function setLinked(v: boolean) {
    linked = v;
    try {
      localStorage.setItem('pst.cmp.linked', v ? '1' : '0');
    } catch { /* storage blocked */ }
  }
  function setMode(m: 'merged' | 'side') {
    mode = m;
    try {
      localStorage.setItem('pst.cmp.log', m);
    } catch { /* storage blocked */ }
  }

  const levelOf = (t: string) => (t.match(/^\[(V|D|I|W|E)\]/) ?? t.match(/^(V|D|I|W|E) \(\d+\)/))?.[1] ?? '';
  // Numbers (timestamps, counters, addresses) differ between runs; the rest says what the line is.
  const keyOf = (t: string) => t.replace(/0x[0-9a-f]+/gi, '#').replace(/\d+(\.\d+)?/g, '#').trim();

  // Lines in the visible range, both sides. Debounced: nudging and zooming fire many changes.
  let token = 0;
  $effect(() => {
    const v = view;
    const off = d;
    const ca = cycA;
    const cb = cycB;
    const a = sidA;
    const b = sidB;
    const t = ++token;
    const timer = setTimeout(async () => {
      loading = true;
      try {
        const [la, lb] = await Promise.all([
          api.sessionUart(a, Math.max(0, ca.t + v.a), ca.t + v.b, LIMIT),
          api.sessionUart(b, Math.max(0, cb.t + v.a - off), cb.t + v.b - off, LIMIT),
        ]);
        if (t !== token) return;
        truncated = la.length >= LIMIT || lb.length >= LIMIT;
        linesA = la.map((l) => ({ x: l.t - ca.t, text: l.text, key: keyOf(l.text), level: levelOf(l.text) }));
        linesB = lb.map((l) => ({ x: l.t - cb.t + off, text: l.text, key: keyOf(l.text), level: levelOf(l.text) }));
      } catch {
        /* the chart shows the error */
      } finally {
        if (t === token) loading = false;
      }
    }, 200);
    return () => clearTimeout(timer);
  });

  /** Pair A and B lines with a longest-common-subsequence on their keys; the rest stays unpaired. */
  function pair(A: Line[], B: Line[]): Row[] {
    const n = A.length;
    const m = B.length;
    if (!n || !m || n * m > 4_000_000) {
      return [...A.map((a) => ({ x: a.x, a, b: null })), ...B.map((b) => ({ x: b.x, a: null, b }))].sort((p, q) => p.x - q.x);
    }
    const L = new Uint16Array((n + 1) * (m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        L[i * (m + 1) + j] = A[i].key === B[j].key ? L[(i + 1) * (m + 1) + j + 1] + 1 : Math.max(L[(i + 1) * (m + 1) + j], L[i * (m + 1) + j + 1]);
      }
    }
    const out: Row[] = [];
    let i = 0;
    let j = 0;
    while (i < n || j < m) {
      if (i < n && j < m && A[i].key === B[j].key) out.push({ x: A[i].x, a: A[i++], b: B[j++] }); // A's time; B's shown as a difference
      else if (j >= m || (i < n && L[(i + 1) * (m + 1) + j] >= L[i * (m + 1) + j + 1])) out.push({ x: A[i].x, a: A[i++], b: null });
      else out.push({ x: B[j].x, a: null, b: B[j++] });
    }
    // Second pass: leftovers with the same text less than 0.5 s apart are the same line (the ordered matching
    // above can miss them when the lines around them differ).
    const used = new Set<Row>();
    for (const r of out) {
      const a = r.a;
      if (!a || r.b) continue;
      const q = out.find((o) => !o.a && o.b && !used.has(o) && o.b.key === a.key && Math.abs(o.b.x - a.x) < 0.5);
      if (q) {
        r.b = q.b;
        used.add(q);
      }
    }
    return out.filter((r) => !used.has(r)).sort((p, r) => p.x - r.x); // stable: pairs keep their order
  }

  const q = $derived(query.trim().toLowerCase());
  const match = (l: Line | null) => !!l && l.text.toLowerCase().includes(q);
  const paired = $derived(pair(linesA, linesB));
  const rows = $derived.by(() => {
    let r = paired;
    if (onlyDiff) r = r.filter((x) => !x.a || !x.b);
    if (q) r = r.filter((x) => match(x.a) || match(x.b));
    return r;
  });
  const sideA = $derived(q ? linesA.filter((l) => match(l)) : linesA);
  const sideB = $derived(q ? linesB.filter((l) => match(l)) : linesB);
  const diffCount = $derived(paired.filter((x) => !x.a || !x.b).length);

  // Highlight: the line(s) nearest to the chart's hover position.
  function nearest(xs: number[], x: number | null): number {
    if (x == null || !xs.length) return -1;
    let best = 0;
    for (let k = 1; k < xs.length; k++) if (Math.abs(xs[k] - x) < Math.abs(xs[best] - x)) best = k;
    return Math.abs(xs[best] - x) <= (view.b - view.a) * 0.01 ? best : -1;
  }
  const hotRow = $derived(nearest(rows.map((r) => r.x), hoverX));
  const hotA = $derived(nearest(sideA.map((l) => l.x), hoverX));
  const hotB = $derived(nearest(sideB.map((l) => l.x), hoverX));

  const fmtX = (x: number) => `${x < 0 ? '−' : ''}${Math.abs(x).toFixed(Math.abs(x) < 10 ? 3 : 2)} s`;

  // Side by side: scrolling one list scrolls the other to the same time.
  let sidesEl = $state<HTMLDivElement>();
  let syncing = false;
  function sync(k: 'a' | 'b') {
    const from = sidesEl?.querySelector<HTMLDivElement>(`[data-side=${k}]`);
    const to = sidesEl?.querySelector<HTMLDivElement>(`[data-side=${k === 'a' ? 'b' : 'a'}]`);
    const toLines = k === 'a' ? sideB : sideA;
    if (!linked || syncing || !from || !to) return;
    const first = [...from.querySelectorAll<HTMLElement>('[data-x]')].find((el) => el.offsetTop + el.offsetHeight > from.scrollTop);
    if (!first) return;
    const x = Number(first.dataset.x);
    const at = toLines.findIndex((l) => l.x >= x);
    const target = at < 0 ? to.scrollHeight : (to.querySelectorAll<HTMLElement>('[data-x]')[at]?.offsetTop ?? 0);
    syncing = true;
    to.scrollTop = target;
    requestAnimationFrame(() => (syncing = false));
  }

  // Keep the highlighted line in view when the chart hover moves (not while the mouse is in the list).
  let inList = false;
  $effect(() => {
    void hotRow;
    void hotA;
    void hotB;
    untrack(() => {
      if (inList) return;
      for (const el of document.querySelectorAll<HTMLElement>('.cmplog .hot')) el.scrollIntoView({ block: 'nearest' });
    });
  });
</script>

<section class="panel cmplog" aria-label="Serial lines of A and B">
  <div class="panel-head">
    <h2>Serial</h2>
    <div class="seg" role="group" aria-label="Layout">
      <button type="button" aria-pressed={mode === 'merged'} onclick={() => setMode('merged')}>Merged</button>
      <button type="button" aria-pressed={mode === 'side'} onclick={() => setMode('side')}>Side by side</button>
    </div>
    {#if mode === 'merged'}
      <label class="check small"><input type="checkbox" bind:checked={onlyDiff} />Only differences ({diffCount})</label>
    {:else}
      <label class="check small" title="Scrolling one list scrolls the other to the same time. Off: each list scrolls on its own, handy when the same events are far apart.">
        <input type="checkbox" checked={linked} onchange={(e) => setLinked((e.target as HTMLInputElement).checked)} />Scroll together
      </label>
    {/if}
    <span class="spacer"></span>
    <input class="input search" type="search" bind:value={query} placeholder="Filter" aria-label="Filter lines" />
  </div>
  <div class="note faint">
    Aligned like the chart; times are A's{loading ? ' · loading…' : ''}{truncated ? ` · first ${LIMIT} lines only: zoom in for the rest` : ''}. Lines that match apart from numbers share a row.
  </div>

  {#if mode === 'merged'}
    <div class="list" role="list" onmouseenter={() => (inList = true)} onmouseleave={() => { inList = false; onhover(null); }}>
      <div class="row head" aria-hidden="true"><span>time</span><span><i class="sw a"></i>A</span><span><i class="sw b"></i>B</span></div>
      {#each rows as r, k (k)}
        <div class="row" class:hot={k === hotRow} class:onlya={r.a && !r.b} class:onlyb={r.b && !r.a} role="listitem" data-x={r.x} onmouseenter={() => onhover(r.x)}>
          <span class="t">{fmtX(r.x)}</span>
          <span class="txt lv-{r.a?.level || 'x'}" title={r.a?.text}>{r.a?.text ?? ''}</span>
          <span class="txt lv-{r.b?.level || 'x'}" title={r.b?.text}>{r.b?.text ?? ''}{#if r.a && r.b && Math.abs(r.b.x - r.a.x) > 0.0005}<em class="dt" title="On the aligned axis B's line comes {fmtX(Math.abs(r.b.x - r.a.x))} {r.b.x > r.a.x ? 'later' : 'earlier'} than A's">B {r.b.x > r.a.x ? '+' : '−'}{fmtX(Math.abs(r.b.x - r.a.x))}</em>{/if}</span>
        </div>
      {:else}
        <div class="empty">{loading ? 'Loading…' : 'No lines in this range.'}</div>
      {/each}
    </div>
  {:else}
    <div class="sides" role="presentation" bind:this={sidesEl} onmouseenter={() => (inList = true)} onmouseleave={() => { inList = false; onhover(null); }}>
      {@render half('a', sideA, hotA)}
      {@render half('b', sideB, hotB)}
    </div>
  {/if}
</section>

{#snippet half(k: 'a' | 'b', ls: Line[], hot: number)}
  <div class="list half" role="list" aria-label="Serial lines of {k.toUpperCase()}" data-side={k} onscroll={() => sync(k)}>
    <div class="row head one" aria-hidden="true"><span>time</span><span><i class="sw {k}"></i>{k.toUpperCase()}</span></div>
    {#each ls as l, i (i)}
      <div class="row one" class:hot={i === hot} role="listitem" data-x={l.x} onmouseenter={() => onhover(l.x)}>
        <span class="t">{fmtX(l.x)}</span><span class="txt lv-{l.level || 'x'}" title={l.text}>{l.text}</span>
      </div>
    {:else}
      <div class="empty">{loading ? 'Loading…' : 'No lines in this range.'}</div>
    {/each}
  </div>
{/snippet}

<style>
  .small { font-size: 13px; min-height: 0; }
  .search { min-height: 32px; width: 200px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); color: var(--text); padding: 0 10px; }
  .note { padding: 6px 14px 0; font-size: 12px; }
  .list { height: 360px; overflow: auto; margin: 8px 0 0; font-family: var(--mono); font-size: 12px; }
  .sides { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0; }
  .half + .half { border-left: 1px solid var(--line); }
  .row { display: grid; grid-template-columns: 84px minmax(0, 1fr) minmax(0, 1fr); gap: 12px; padding: 2px 14px; align-items: baseline; }
  .row.one { grid-template-columns: 84px minmax(0, 1fr); }
  .row.head { position: sticky; top: 0; background: var(--panel); color: var(--muted); font-family: var(--font); font-weight: 600; padding-top: 4px; padding-bottom: 4px; border-bottom: 1px solid var(--line); z-index: 1; }
  .row:not(.head):hover, .row.hot { background: var(--sel); }
  .row.onlya .txt:nth-child(2) { background: color-mix(in srgb, var(--cmp-a) 14%, transparent); }
  .row.onlyb .txt:nth-child(3) { background: color-mix(in srgb, var(--cmp-b) 14%, transparent); }
  .t { color: var(--faint); text-align: right; white-space: nowrap; }
  .txt { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; border-radius: 3px; padding: 0 4px; }
  .lv-W { color: var(--warn); }
  .lv-E { color: var(--rec); }
  .dt { font-style: normal; color: var(--faint); margin-left: 8px; }
  .sw { display: inline-block; width: 12px; height: 3px; margin-right: 6px; vertical-align: middle; }
  .sw.a { background: var(--cmp-a); }
  .sw.b { background: var(--cmp-b); }
  .empty { padding: 16px 14px; color: var(--faint); font-family: var(--font); }
  @media (max-width: 720px) {
    .sides { grid-template-columns: minmax(0, 1fr); }
    .half + .half { border-left: 0; border-top: 1px solid var(--line); }
    .row { grid-template-columns: 64px minmax(0, 1fr) minmax(0, 1fr); gap: 8px; padding: 2px 10px; }
  }
</style>
