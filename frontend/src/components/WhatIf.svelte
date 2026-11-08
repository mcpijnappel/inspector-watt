<script lang="ts">
  // Battery life from the measured mix of wakes, with "what if" changes: sleep longer, drop a phase, a cycle type
  // more or less often. The api works the numbers out (the same as the MCP battery tool); this only asks.
  import BatteryPicker from './BatteryPicker.svelte';
  import { api, type Battery, type PhasesResult } from '../lib/api';
  import { ALL, avgRows, statsFor } from '../lib/cycletypes';
  import { fmtAmps, fmtLife } from '../lib/format';
  import { settings } from '../lib/settings.svelte';

  let { result, sid }: { result: PhasesResult; sid: number } = $props();

  const rules = $derived(result.profile?.rules ?? []);
  const all = $derived(statsFor(result, ALL));
  const nAll = $derived(all?.usable ?? 0);
  const allRows = $derived(avgRows(all));
  const sleepRule = $derived(result.sleep_rule);
  const sleepPhase = $derived(allRows.find((p) => p.rule === sleepRule));
  const awake = $derived(allRows.filter((p) => p.seen > 0 && p.rule !== sleepRule));

  let sleepChoice = $state<'measured' | number>('measured');
  let custom = $state('');
  let skipped = $state<number[]>([]);
  let mixType = $state<string | null>(null);
  let everyText = $state('');
  let bat = $state<Battery | null>(null);
  let error = $state('');

  /** The kinds of wakes the estimate mixes (whole-wake types, or just All wakes). */
  const parts = $derived(bat?.mix ?? []);
  // The type to make more or less frequent: by default the one with the most charge per wake (the upload).
  const mixPick = $derived.by(() => {
    if (parts.length < 2) return null;
    if (mixType && parts.some((v) => v.type === mixType)) return mixType;
    return [...parts].sort((a, b) => (b.charge_c ?? 0) - (a.charge_c ?? 0))[0].type;
  });
  const nowEvery = $derived(parts.find((v) => v.type === mixPick)?.one_in ?? null);
  const every = $derived.by(() => {
    const v = parseFloat(everyText.replace(',', '.'));
    return v >= 1 ? v : null;
  });
  const sleepS = $derived(sleepChoice === 'measured' ? null : sleepChoice);
  const changed = $derived(sleepChoice !== 'measured' || skipped.length > 0 || (every != null && nowEvery != null && Math.abs(every - nowEvery) > 0.01));

  // Ask the api whenever the what-if (or the session's phases) change; the newest answer wins.
  $effect(() => {
    void result;
    const w = { sleep_s: sleepS, without: skipped.map((r) => rules[r]?.name).filter((n): n is string => !!n), type: mixPick, every };
    const ctl = new AbortController();
    const t = setTimeout(() => {
      api.battery(sid, w, ctl.signal).then((r) => { bat = r; error = ''; }).catch((e) => { if (!ctl.signal.aborted) error = (e as Error).message; });
    }, 80);
    return () => { clearTimeout(t); ctl.abort(); };
  });
  const now = $derived(bat?.measured?.mean_a ?? null);
  const whatIf = $derived(bat?.what_if?.mean_a ?? null);
  const hours = (a: number | null) => (a ? settings.batteries.selected_mah / (a * 1000) : NaN);

  function toggle(rule: number) {
    skipped = skipped.includes(rule) ? skipped.filter((r) => r !== rule) : [...skipped, rule];
  }
  function applyCustom(e: SubmitEvent) {
    e.preventDefault();
    const v = parseFloat(custom.replace(',', '.'));
    if (v > 0) sleepChoice = v;
  }
</script>

<section class="panel" aria-label="Battery life">
  <div class="panel-head"><h2>Battery life</h2><span class="spacer"></span><BatteryPicker /></div>
  {#if !sleepPhase}
    <p class="empty">Needs complete cycles with phases (phase rules).</p>
  {:else}
    <div class="body">
      <div class="result">
        <div>
          <div class="label">Measured</div>
          <div class="big mono">{fmtLife(hours(now))}</div>
          <div class="sub mono">{fmtAmps(now)} average</div>
        </div>
        {#if changed}
          <div class="arrow" aria-hidden="true">→</div>
          <div>
            <div class="label">What if</div>
            <div class="big mono accent">{fmtLife(hours(whatIf))}</div>
            <div class="sub mono">{fmtAmps(whatIf)} average</div>
          </div>
        {/if}
      </div>

      <div class="row" role="group" aria-label="Sleep time per cycle">
        <span class="muted">{rules[sleepRule]?.name ?? 'Sleep'}</span>
        <button type="button" class="pill" aria-pressed={sleepChoice === 'measured'} onclick={() => (sleepChoice = 'measured')}>{(sleepPhase.dur ?? 0).toFixed(0)} s (now)</button>
        {#each [300, 900, 3600] as s (s)}
          <button type="button" class="pill" aria-pressed={sleepChoice === s} onclick={() => (sleepChoice = s)}>{s >= 3600 ? `${s / 3600} h` : `${s / 60} min`}</button>
        {/each}
        <form class="custom" onsubmit={applyCustom}>
          <input type="text" inputmode="decimal" placeholder="s" bind:value={custom} aria-label="Custom sleep time in seconds" />
          <button class="pill" type="submit">Set</button>
        </form>
      </div>

      {#if parts.length > 1 && nowEvery != null}
        <div class="row" role="group" aria-label="How often a cycle type happens">
          <select class="typesel" value={mixPick} onchange={(e) => { mixType = (e.target as HTMLSelectElement).value; everyText = ''; }} aria-label="Cycle type">
            {#each parts as v (v.type)}<option value={v.type}>{v.type}</option>{/each}
          </select>
          <span class="muted">every</span>
          <input class="every" type="text" inputmode="decimal" bind:value={everyText} placeholder={nowEvery.toFixed(1)} aria-label="Every how many wakes" />
          <span class="muted">wakes</span>
          <span class="faint">(now 1 in {nowEvery.toFixed(1)})</span>
        </div>
      {/if}

      <div class="skips">
        <span class="muted">What if this phase is gone:</span>
        {#each awake as p (p.rule)}
          <label class="check"><input type="checkbox" checked={skipped.includes(p.rule)} onchange={() => toggle(p.rule)} />
            <span class="sw" style:background={rules[p.rule]?.color}></span>{rules[p.rule]?.name}
            <span class="faint mono">{(p.dur ?? 0).toFixed(1)} s{p.seen < p.n ? ` · in ${p.seen} of ${p.n}` : ''}</span>
          </label>
        {/each}
      </div>
      <div class="hint">From {nAll} complete wakes{parts.length > 1 ? ` (${parts.map((v) => `${v.wakes} ${v.type}`).join(', ')})` : ''}; sleep at {fmtAmps(sleepPhase.mean)}. Ignores self-discharge and the cut-off voltage.</div>
      {#if error}<div class="hint bad">Can't work out the battery life: {error}</div>{/if}
    </div>
  {/if}
</section>

<style>
  .body { padding: 12px 16px 14px; display: flex; flex-direction: column; gap: 12px; }
  .result { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
  .label { font-size: 12px; color: var(--muted); }
  .big { font-size: 24px; font-weight: 700; }
  .accent { color: var(--accent); }
  .sub { font-size: 12px; color: var(--faint); }
  .arrow { font-size: 22px; color: var(--faint); }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 13px; }
  .pill { min-height: 32px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--line); background: transparent; cursor: pointer; font-size: 13px; }
  .pill[aria-pressed='true'] { border: 2px solid var(--accent); background: var(--sel); font-weight: 600; }
  .custom { display: inline-flex; gap: 4px; }
  .typesel { min-height: 32px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); color: var(--text); padding: 0 8px; font: inherit; }
  .every { width: 56px; min-height: 32px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); padding: 0 8px; font-family: var(--mono); color: var(--text); }
  .custom input { width: 64px; min-height: 32px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); padding: 0 8px; font-family: var(--mono); }
  .skips { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
  .check { min-height: 28px; }
  .sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; }
  .empty { padding: 16px; color: var(--faint); margin: 0; }
  .bad { color: var(--rec); }
</style>
