<script lang="ts">
  // "Where the energy goes": per phase duration, average current, charge and share — averaged over the cycles of
  // the chosen type, or one cycle.
  import type { PhasesResult } from '../lib/api';
  import { avgRows, cycleRows, statsFor, usable, type Instance, type TypeView } from '../lib/cycletypes';
  import { fmtAmps, fmtCharge, fmtSpan } from '../lib/format';
  import PhaseSpread from './PhaseSpread.svelte';

  type Props = {
    result: PhasesResult;
    /** The chosen cycle type (or All wakes). */
    view: TypeView;
    /** The cycle shown in the chart. */
    current: Instance | null;
    mode: 'avg' | 'cycle';
    onmode: (m: 'avg' | 'cycle') => void;
    sid: number;
    /** Show one of the type's cycles in the chart (from the spread). */
    ongo: (k: number) => void;
  };
  let { result, view, current, mode, onmode, sid, ongo }: Props = $props();
  /** The phase whose spread over all cycles is unfolded. */
  let open = $state<number | null>(null);

  const rules = $derived(result.profile?.rules ?? []);
  const xs = $derived(usable(view));
  /** The api's averages for this type. */
  const stats = $derived(statsFor(result, view.key));
  type Row = { rule: number; dur: number | null; mean: number | null; charge: number | null; seen: number; n: number; times: number };
  const rows = $derived.by<Row[]>(() => {
    // The api lists the phases this type has (a stretch only its own; all wakes every phase).
    const avg = xs.length ? avgRows(stats) : [];
    const one = current ? cycleRows(current, rules.length).filter((p) => (avg.length ? avg.some((a) => a.rule === p.rule) : p.seen > 0)) : [];
    const st = mode === 'avg' ? avg : one;
    return st.map((p) => ({ rule: p.rule, dur: p.seen ? p.dur : null, mean: p.mean, charge: p.seen ? p.charge : null, seen: p.seen, n: p.n, times: p.times }));
  });
  const totalCharge = $derived(rows.reduce((a, r) => a + (r.charge ?? 0), 0));
  const totalDur = $derived(mode === 'avg' ? (stats?.duration_s.mean ?? 0) : (current?.dur ?? 0));
  const maxShare = $derived(Math.max(0.0001, ...rows.map((r) => (r.charge ?? 0) / (totalCharge || 1))));
  const label = $derived(view.key ? view.name : 'All wakes');
</script>

<section class="panel" aria-label="Where the energy goes">
  <div class="panel-head">
    <h2>Where the energy goes</h2>
    <div class="seg" role="group" aria-label="Average or one cycle">
      <button type="button" aria-pressed={mode === 'avg'} onclick={() => onmode('avg')}>Average: {label} ({xs.length})</button>
      <button type="button" aria-pressed={mode === 'cycle'} disabled={!current} onclick={() => onmode('cycle')}>{current ? `${view.key ? view.name : 'Cycle'} ${current.k + 1}` : 'This cycle'}</button>
    </div>
    <span class="spacer"></span>
    <span class="faint small">{result.profile?.name}</span>
  </div>
  {#if !rules.length}
    <p class="empty">No phase rules yet. Right-click a log line and choose "Start a phase here".</p>
  {:else}
    <div class="wrap">
      <table>
        <thead>
          <tr><th scope="col">Phase</th><th scope="col">Starts at log line</th><th scope="col" class="num">Duration</th><th scope="col" class="num">Average</th><th scope="col" class="num">Charge</th><th scope="col" class="share">Share</th></tr>
        </thead>
        <tbody class="mono">
          {#each rows as r (r.rule)}
            {@const share = (r.charge ?? 0) / (totalCharge || 1)}
            <tr class:unseen={!r.seen}>
              <td class="name">
                <button class="unfold" type="button" aria-expanded={open === r.rule} title="All cycles: spread and unusual ones"
                  onclick={() => (open = open === r.rule ? null : r.rule)}>
                  <span class="chev" aria-hidden="true">{open === r.rule ? '▾' : '▸'}</span><span class="sw" style:background={rules[r.rule]?.color}></span>{rules[r.rule]?.name}{#if r.times > 1.05}<span class="faint times"> ×{r.times.toFixed(1)}</span>{/if}
                </button>
              </td>
              <td class="pat" title={rules[r.rule]?.pattern}>{rules[r.rule]?.pattern}</td>
              <td class="num">{#if r.dur == null}not seen{:else}{#if mode === 'avg' && r.seen < r.n}<span class="faint inn" title="Only in {r.seen} of the {r.n} cycles: its duration is over those, its charge per cycle over all {r.n}">in {r.seen} of {r.n} · </span>{/if}{fmtSpan(r.dur)}{/if}</td>
              <td class="num">{fmtAmps(r.mean)}</td>
              <td class="num strong">{fmtCharge(r.charge)}</td>
              <td class="share">
                <span class="bar"><span style:width="{(share / maxShare) * 100}%" style:background={rules[r.rule]?.color}></span></span>
                <span class="pct">{r.seen ? `${(share * 100).toFixed(1)} %` : ''}</span>
              </td>
            </tr>
            {#if open === r.rule}
              <tr class="spreadrow"><td colspan="6"><PhaseSpread {result} {view} rule={r.rule} {sid} current={current?.k ?? null} {ongo} /></td></tr>
            {/if}
          {/each}
          <tr class="total">
            <td class="name">{mode === 'avg' ? `Average ${view.kind === 'stretch' ? 'stretch' : view.kind === 'period' ? 'period' : 'cycle'}` : 'This cycle'}</td><td></td>
            <td class="num">{fmtSpan(totalDur)}</td>
            <td class="num">{fmtAmps(totalDur > 0 ? totalCharge / totalDur : null)}</td>
            <td class="num">{fmtCharge(totalCharge)}</td><td></td>
          </tr>
        </tbody>
      </table>
    </div>
  {/if}
</section>

<style>
  .small { font-size: 12px; }
  .wrap { overflow-x: auto; }
  table { width: 100%; min-width: 640px; border-collapse: collapse; }
  th { text-align: left; font-size: 12px; font-weight: 600; color: var(--muted); padding: 8px; }
  td { padding: 7px 8px; border-top: 1px solid var(--line); font-size: 13px; }
  th:first-child, td:first-child { padding-left: 16px; }
  th:last-child, td:last-child { padding-right: 16px; }
  .num { text-align: right; white-space: nowrap; }
  .strong { font-weight: 600; }
  .name { font-family: var(--font); font-weight: 600; white-space: nowrap; }
  .sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 8px; }
  .pat { font-size: 12px; color: var(--muted); max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .share { width: 26%; }
  .share { white-space: nowrap; }
  .bar { display: inline-block; vertical-align: middle; width: calc(100% - 60px); height: 8px; border-radius: 4px; background: var(--panel2); overflow: hidden; }
  .bar span { display: block; height: 100%; }
  .pct { display: inline-block; width: 52px; text-align: right; color: var(--muted); }
  .unseen { opacity: 0.55; }
  .inn { font-size: 11px; }
  .times { font-weight: 400; font-size: 12px; }
  .unfold { border: 0; background: none; padding: 0; color: var(--text); font: inherit; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; text-align: left; }
  .unfold:hover { text-decoration: underline; }
  .chev { width: 14px; color: var(--faint); font-size: 11px; }
  .spreadrow td { border-top: 0; background: var(--bg); padding-top: 4px; padding-bottom: 10px; }
  .total td { border-top: 2px solid var(--line); font-weight: 700; }
  .empty { padding: 16px; color: var(--faint); margin: 0; }
</style>
