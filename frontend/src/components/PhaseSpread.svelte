<script lang="ts">
  // One phase over all cycles of the chosen type: a dot per cycle for duration and charge, the typical value and
  // usual range, and the outliers. A dot shows that cycle; an outlier can be compared with a typical cycle.
  import type { PhasesResult } from '../lib/api';
  import { usable, type TypeView } from '../lib/cycletypes';
  import { fmtCharge, fmtSpan } from '../lib/format';

  type Props = { result: PhasesResult; view: TypeView; rule: number; sid: number; current: number | null; ongo: (k: number) => void };
  let { result, view, rule, sid, current, ongo }: Props = $props();

  type Pt = { i: number; first: number; dur: number; charge: number; out: boolean };

  const name = $derived(result.profile?.rules[rule]?.name ?? '');
  const color = $derived(result.profile?.rules[rule]?.color ?? '#8b949e');
  // Usable cycles of the type that have the phase (summed when a period has it more than once).
  const xs = $derived(usable(view));
  const raw = $derived(
    xs.flatMap((x) => {
      const ps = x.phases.filter((p) => p.rule === rule);
      return ps.length ? [{ i: x.k, first: x.first, dur: ps.reduce((a, p) => a + p.end_t - p.t, 0), charge: ps.reduce((a, p) => a + p.charge, 0) }] : [];
    }),
  );
  const notSeen = $derived(xs.length - raw.length);

  function quantile(sorted: number[], q: number): number {
    if (!sorted.length) return NaN;
    const pos = (sorted.length - 1) * q;
    const lo = Math.floor(pos);
    return sorted[lo] + (sorted[Math.min(lo + 1, sorted.length - 1)] - sorted[lo]) * (pos - lo);
  }
  function stats(vs: number[]) {
    const s = [...vs].sort((a, b) => a - b);
    const q1 = quantile(s, 0.25);
    const q3 = quantile(s, 0.75);
    const med = quantile(s, 0.5);
    // Far outside the middle half (3x its width, and at least 10 % from typical): clearly unusual.
    const fence = Math.max(3 * (q3 - q1), 0.1 * Math.abs(med));
    return { med, p10: quantile(s, 0.1), p90: quantile(s, 0.9), min: s[0], max: s[s.length - 1], lo: q1 - fence, hi: q3 + fence };
  }
  const sd = $derived(raw.length ? stats(raw.map((p) => p.dur)) : null);
  const sc = $derived(raw.length ? stats(raw.map((p) => p.charge)) : null);
  const pts = $derived<Pt[]>(
    raw.map((p) => ({ ...p, out: !!sd && !!sc && (p.dur < sd.lo || p.dur > sd.hi || p.charge < sc.lo || p.charge > sc.hi) })),
  );
  const outliers = $derived(
    pts.filter((p) => p.out).sort((a, b) => Math.abs(b.dur - (sd?.med ?? 0)) / (sd?.med || 1) - Math.abs(a.dur - (sd?.med ?? 0)) / (sd?.med || 1)),
  );

  // A typical cycle to compare with: the one whose total charge is the median (Compare steps through the same type).
  const typical = $derived.by(() => (xs.length ? [...xs].sort((a, b) => a.charge - b.charge)[Math.floor(xs.length / 2)] : null));
  const compareHref = (p: Pt) => {
    const ph = encodeURIComponent(`phase:${name}`);
    const type = view.key ? `&type=${encodeURIComponent(view.key)}` : '';
    return `#/compare?a=${sid}:${(typical?.k ?? 0) + 1}&b=${sid}:${p.i + 1}&show=${ph}&align=${ph}${type}`;
  };

  const pos = (v: number, s: { min: number; max: number }) => (s.max > s.min ? ((v - s.min) / (s.max - s.min)) * 100 : 50);
  const tip = (p: Pt) => `Cycle ${p.i + 1}: ${fmtSpan(p.dur)} · ${fmtCharge(p.charge)}${p.out ? ' (unusual)' : ''}`;
  const rows = $derived([
    { key: 'dur' as const, label: 'Duration', s: sd, fmt: fmtSpan },
    { key: 'charge' as const, label: 'Charge', s: sc, fmt: fmtCharge },
  ]);
</script>

<div class="spread" aria-label="{name} over all cycles">
  {#if !raw.length}
    <p class="faint">{name} isn't in any {view.key ? view.name : 'complete'} cycle.</p>
  {:else}
    {#each rows as row (row.key)}
      {@const s = row.s!}
      <div class="metric">
        <span class="lbl">{row.label}</span>
        <div class="track">
          <span class="band" style:left="{pos(s.p10, s)}%" style:width="{Math.max(0.5, pos(s.p90, s) - pos(s.p10, s))}%"></span>
          <span class="med" style:left="{pos(s.med, s)}%"></span>
          {#each pts as p (p.i)}
            <button type="button" class="dot" class:out={p.out} class:cur={p.i === current} tabindex="-1"
              style:left="{pos(p[row.key], s)}%" style:--c={color} title={tip(p)} aria-label={tip(p)} onclick={() => ongo(p.i)}></button>
          {/each}
        </div>
        <div class="axis mono faint">
          <span>{row.fmt(s.min)}</span>
          <span>typical <strong>{row.fmt(s.med)}</strong> · most {row.fmt(s.p10)} – {row.fmt(s.p90)}</span>
          <span>{row.fmt(s.max)}</span>
        </div>
      </div>
    {/each}
    <div class="foot">
      <span class="faint">{raw.length} cycles{notSeen ? ` · not seen in ${notSeen}` : ''} · click a dot to show that cycle</span>
      {#if outliers.length}
        <span class="outs">
          {outliers.length} unusual:
          {#each outliers.slice(0, 5) as p (p.i)}
            <span class="out-item">
              <button class="link" type="button" onclick={() => ongo(p.i)}>#{p.i + 1}</button>
              <span class="mono">{fmtSpan(p.dur)} · {fmtCharge(p.charge)}</span>
              {#if typical && typical.k !== p.i}<a href={compareHref(p)} title="Compare this cycle with typical cycle {typical.k + 1}, showing {name}">compare</a>{/if}
            </span>
          {/each}
          {#if outliers.length > 5}<span class="faint">+{outliers.length - 5} more</span>{/if}
        </span>
      {/if}
    </div>
  {/if}
</div>

<style>
  .spread { display: flex; flex-direction: column; gap: 10px; padding: 6px 0 4px; }
  .metric { display: grid; grid-template-columns: 70px minmax(0, 1fr); gap: 2px 10px; align-items: center; }
  .lbl { font-size: 12px; color: var(--muted); font-family: var(--font); }
  .track { position: relative; height: 18px; margin: 0 6px; }
  .band { position: absolute; top: 4px; height: 10px; background: var(--panel2); border-radius: 3px; }
  .med { position: absolute; top: 0; width: 2px; height: 18px; margin-left: -1px; background: var(--text); }
  .dot {
    position: absolute; top: 4px; width: 10px; height: 10px; margin-left: -5px; padding: 0; border-radius: 50%;
    border: 0; background: var(--c); opacity: 0.55; cursor: pointer;
  }
  .dot:hover { opacity: 1; transform: scale(1.3); }
  .dot.out { opacity: 1; background: transparent; border: 2px solid var(--warn); }
  .dot.cur { opacity: 1; outline: 2px solid var(--text); outline-offset: 1px; }
  .axis { grid-column: 2; display: flex; justify-content: space-between; gap: 8px; font-size: 11px; }
  .foot { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; font-family: var(--font); }
  .outs { display: inline-flex; flex-wrap: wrap; gap: 4px 12px; align-items: center; }
  .out-item { display: inline-flex; gap: 6px; align-items: baseline; }
  .link { border: 0; background: none; padding: 0; color: var(--accent); cursor: pointer; font: inherit; }
  p { margin: 0; font-size: 13px; }
</style>
