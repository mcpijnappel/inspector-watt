<script lang="ts">
  // Serial log with search, level filter, click-to-select and stick-to-bottom while following.
  import type { Snippet } from 'svelte';
  import { splitLevel } from '../lib/format';
  import type { LiveLine } from '../lib/live.svelte';

  type Props = {
    lines: LiveLine[];
    selected: number | null;
    onselect: (line: LiveLine) => void;
    oncontext?: (line: LiveLine, e: MouseEvent) => void;
    timeOf: (line: LiveLine) => string;
    follow?: boolean;
    status?: string;
    empty?: string;
    /** Times (line x) of unexpected resets: those lines show in red with ⚠ and count as errors. */
    alerts?: number[];
    /** Extra controls next to the title (the Live page's UART | USB switch). */
    head?: Snippet;
  };
  let { lines, selected, onselect, oncontext, timeOf, follow = true, status = '', empty = 'No serial data yet', alerts = [], head }: Props = $props();
  const isAlert = (l: LiveLine) => l.x != null && alerts.some((t) => Math.abs(t - (l.x as number)) < 0.002);

  let query = $state('');
  let level = $state<'all' | 'W' | 'E'>('all');
  let box: HTMLDivElement;
  let stick = $state(true);

  const rows = $derived.by(() => {
    const q = query.trim().toLowerCase();
    return lines
      .map((l) => ({ line: l, alert: alerts.length > 0 && isAlert(l), ...splitLevel(l.text) }))
      .filter((r) => {
        if (level === 'E' && r.level !== 'E' && !r.alert) return false;
        if (level === 'W' && r.level !== 'W' && r.level !== 'E' && !r.alert) return false;
        return !q || r.line.text.toLowerCase().includes(q);
      });
  });

  function onscroll() {
    stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 12;
  }

  $effect(() => {
    void rows;
    if (follow && stick && box) queueMicrotask(() => (box.scrollTop = box.scrollHeight));
  });
</script>

<section class="panel log" aria-label="Serial log">
  <div class="panel-head">
    <h2>Serial log</h2>
    {@render head?.()}
    <span class="spacer"></span>
    <label class="search">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"></circle><path d="M21 21l-4-4"></path></svg>
      <span class="sr-only">Search the log</span>
      <input type="search" placeholder="Search" bind:value={query} />
    </label>
    <div class="levels" role="group" aria-label="Level filter">
      <button type="button" aria-pressed={level === 'all'} onclick={() => (level = 'all')}>All</button>
      <button type="button" class="w" aria-pressed={level === 'W'} onclick={() => (level = 'W')}>W+</button>
      <button type="button" class="e" aria-pressed={level === 'E'} onclick={() => (level = 'E')}>E</button>
    </div>
  </div>
  <div class="lines mono" bind:this={box} {onscroll}>
    {#each rows as r (r.line.seq)}
      <button type="button" class="row" class:sel={r.line.seq === selected} class:alert={r.alert} title={r.alert ? 'Unexpected reset' : undefined} onclick={() => onselect(r.line)}
        oncontextmenu={(e) => { if (oncontext) { e.preventDefault(); oncontext(r.line, e); } }}>
        <span class="t">{timeOf(r.line)}</span>
        <span class="lv lv-{r.alert ? 'E' : r.level || 'x'}">{r.alert ? '⚠' : r.level || '·'}</span>
        <span class="msg">{r.msg}</span>
      </button>
    {:else}
      <div class="empty">{query || level !== 'all' ? 'No matching lines' : empty}</div>
    {/each}
  </div>
  <div class="foot">
    <span>{status}</span>
    {#if !stick && follow}
      <button type="button" class="btn small" onclick={() => { stick = true; box.scrollTop = box.scrollHeight; }}>Jump to newest</button>
    {/if}
  </div>
</section>

<style>
  .log { display: flex; flex-direction: column; min-height: 0; }
  .search { display: flex; align-items: center; gap: 6px; padding: 0 10px; min-height: 32px; border: 1px solid var(--line); border-radius: 8px; color: var(--muted); }
  .search input { width: 120px; border: 0; background: transparent; outline: none; font-size: 13px; }
  .levels { display: inline-flex; gap: 4px; }
  .levels button { padding: 5px 8px; border-radius: 6px; border: 1px solid var(--line); background: transparent; color: var(--text); font-family: var(--mono); font-size: 12px; cursor: pointer; }
  .levels button[aria-pressed='true'] { background: var(--panel2); font-weight: 600; }
  .levels .w { color: var(--warn); }
  .levels .e { color: var(--rec); }
  .lines { flex: 1 1 0; min-height: 200px; overflow: auto; padding: 6px 0; font-size: 12px; line-height: 1.5; }
  .row { display: flex; gap: 10px; width: 100%; text-align: left; padding: 1px 14px; border: 0; background: transparent; cursor: pointer; font-family: var(--mono); font-size: 12px; }
  .row:hover { background: var(--panel2); }
  .row.sel { background: var(--sel); box-shadow: inset 3px 0 0 var(--accent); }
  .row.alert { background: var(--rec-bg); }
  .row.alert .msg { color: var(--rec); }
  .t { color: var(--faint); flex: 0 0 auto; }
  .lv { flex: 0 0 12px; font-weight: 600; color: var(--faint); }
  .lv-I { color: var(--muted); }
  .lv-W { color: var(--warn); }
  .lv-E { color: var(--rec); }
  .msg { min-width: 0; white-space: pre-wrap; word-break: break-word; }
  .empty { padding: 16px 14px; color: var(--faint); font-family: var(--font); }
  .foot { display: flex; align-items: center; gap: 8px; min-height: 40px; padding: 4px 14px; border-top: 1px solid var(--line); font-size: 12px; color: var(--muted); }
  .foot span { flex: 1 1 0; }
</style>
