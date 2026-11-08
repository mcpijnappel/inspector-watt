<script lang="ts">
  // Cycle types of a profile: per type the phase list with a role for each phase (begin, end, must have, excluded),
  // a plain sentence of what it does, and how many cycles it finds in this session.
  import { api, type CycleType, type Rule, type TypeResult } from '../lib/api';

  /** reasons: the wake reasons in this session's log (for "Woken by"). */
  type Props = { sessionId: number; rules: Rule[]; types: CycleType[]; phasesChanged: boolean; reasons?: string[] };
  let { sessionId, rules, types = $bindable(), phasesChanged, reasons = [] }: Props = $props();

  type Role = '' | 'begin' | 'must' | 'exclude' | 'stop' | 'end';
  const ROLES: [Role, string][] = [['', '—'], ['begin', 'Begin'], ['must', 'Must have'], ['exclude', 'Excluded'], ['stop', 'End: stop before it'], ['end', 'End: include it']];
  const COLORS = ['#39c5cf', '#f0883e', '#bc8cff', '#3fb950', '#ff7b72', '#e3b341', '#58a6ff'];

  let cur = $state(0);
  let found = $state<TypeResult[] | null>(null);
  let testError = $state('');

  const t = $derived(types[cur] ?? null);
  const names = $derived(rules.map((r) => r.name));

  // How many cycles each type finds here (debounced; with the saved phase rules).
  $effect(() => {
    const snapshot = JSON.stringify(types);
    const ready = types.every((x) => x.name.trim() && x.begin);
    if (!types.length || !ready) {
      found = null;
      return;
    }
    const h = setTimeout(() => {
      api.testTypes(sessionId, JSON.parse(snapshot)).then((r) => { found = r; testError = ''; }).catch((e) => (testError = (e as Error).message));
    }, 350);
    return () => clearTimeout(h);
  });

  function roleOf(x: CycleType, n: string): Role {
    if (x.begin === n) return 'begin';
    if (x.end?.phase === n) return x.end.include ? 'end' : 'stop';
    if (x.must.includes(n)) return 'must';
    if (x.exclude.includes(n)) return 'exclude';
    return '';
  }

  function setRole(n: string, role: Role) {
    const x = types[cur];
    if (!x) return;
    // Take the phase out of every role, then give it the new one; begin and end are one phase each, so they move.
    if (x.begin === n) x.begin = '';
    if (x.end?.phase === n) x.end = null;
    x.must = x.must.filter((m) => m !== n);
    x.exclude = x.exclude.filter((m) => m !== n);
    if (role === 'begin') x.begin = n;
    else if (role === 'stop' || role === 'end') x.end = { phase: n, include: role === 'end' };
    else if (role === 'must') x.must = [...x.must, n];
    else if (role === 'exclude') x.exclude = [...x.exclude, n];
  }

  /** Wake reasons to offer: the ones in this session plus any the type already uses. */
  const wakeChoices = $derived(t ? [...new Set([...reasons, ...(t.woken_by ?? [])])] : reasons);
  function toggleWake(r: string) {
    const x = types[cur];
    if (!x) return;
    const now = x.woken_by ?? [];
    x.woken_by = now.includes(r) ? now.filter((w) => w !== r) : [...now, r];
  }

  function add() {
    let n = types.length + 1;
    while (types.some((x) => x.name === `Type ${n}`)) n++;
    types = [...types, { name: `Type ${n}`, begin: rules[0]?.name ?? '', end: null, must: [], exclude: [], woken_by: [] }];
    cur = types.length - 1;
  }
  function remove(i: number) {
    types = types.filter((_, k) => k !== i);
    cur = Math.max(0, Math.min(cur, types.length - 1));
  }

  /** Phases a type uses that no longer exist (deleted or renamed elsewhere). */
  function missing(x: CycleType): string[] {
    return [x.begin, ...x.must, ...x.exclude, ...(x.end ? [x.end.phase] : [])].filter((n) => n && !names.includes(n));
  }
  function kindOf(x: CycleType): string {
    if (x.end) return 'stretch of a cycle';
    return x.begin === rules[0]?.name ? 'whole wake' : 'period, begin to begin';
  }
  function result(i: number): TypeResult | undefined {
    const f = found?.[i];
    return f && f.name === types[i]?.name.trim() ? f : undefined;
  }
</script>

<div class="types">
  <p class="intro">A cycle type picks out some cycles: it <strong>begins</strong> at a phase and runs until the next begin, or until its <strong>end</strong> phase (then it's a stretch, like Connect + upload). <strong>Must have</strong> and <strong>Excluded</strong> keep only the cycles with or without a phase; <strong>Woken by</strong> only the ones with a wake reason. Types show up in the cycle menu, the phase table and battery life.</p>
  <div class="split">
    <div class="list" role="listbox" aria-label="Cycle types">
      {#each types as x, i (i)}
        {@const r = result(i)}
        <button type="button" class="item" role="option" aria-selected={i === cur} onclick={() => (cur = i)}>
          <span class="sw" style:background={COLORS[i % COLORS.length]}></span>
          <span class="tx"><span class="tn">{x.name || 'Unnamed'}</span><span class="kind">{kindOf(x)}</span></span>
          <span class="mono count" class:bad={!!r?.error || missing(x).length > 0}>{missing(x).length || r?.error ? '!' : r ? r.instances.length : '…'}</span>
        </button>
      {:else}
        <p class="faint empty">No types yet: every wake counts as one cycle.</p>
      {/each}
      <button class="btn ghost add" type="button" onclick={add}>+ New type</button>
    </div>

    {#if t}
      {@const r = result(cur)}
      <div class="edit">
        <div class="row">
          <input class="input name" type="text" bind:value={t.name} maxlength="60" aria-label="Type name" />
          <span class="tag">{kindOf(t)}</span>
          <span class="spacer"></span>
          <button class="btn small ghost" type="button" onclick={() => remove(cur)}>Delete type</button>
        </div>
        <table class="roles">
          <tbody>
            {#each rules as ph, k (k)}
              {@const role = roleOf(t, ph.name)}
              <tr class:on={role !== ''}>
                <td class="ph"><span class="sw" style:background={ph.color}></span>{ph.name}</td>
                <td>
                  <select class="input small" value={role} onchange={(e) => setRole(ph.name, (e.target as HTMLSelectElement).value as Role)} aria-label="Role of {ph.name}">
                    {#each ROLES as [v, l] (v)}<option value={v}>{l}</option>{/each}
                  </select>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
        <div class="woken" role="group" aria-label="Woken by">
          <span class="muted small">Woken by</span>
          {#each wakeChoices as r (r)}
            <button type="button" class="chip" aria-pressed={(t.woken_by ?? []).includes(r)} onclick={() => toggleWake(r)}>{r}</button>
          {:else}
            <span class="faint small">No wake reasons in this session's log (Phases → Wake reason).</span>
          {/each}
          {#if wakeChoices.length && !(t.woken_by ?? []).length}<span class="faint small">any</span>{/if}
        </div>
        <div class="sentence">
          {#if missing(t).length}
            <span class="error">Uses a phase that isn't there any more: {missing(t).join(', ')}. Pick its role again.</span>
          {:else if !t.begin}
            <span class="error">Pick the phase it begins at.</span>
          {:else}
            Begins at <strong>{t.begin}</strong> and
            {#if !t.end}runs until the next <strong>{t.begin}</strong>{:else if t.end.include}runs through the end of <strong>{t.end.phase}</strong>{:else}stops when <strong>{t.end.phase}</strong> starts{/if}{#if t.must.length}; only cycles with <strong>{t.must.join(' and ')}</strong>{/if}{#if t.exclude.length}; not cycles with <strong>{t.exclude.join(' or ')}</strong>{/if}{#if t.woken_by?.length}; only cycles woken by <strong>{t.woken_by.join(' or ')}</strong>{/if}.
          {/if}
        </div>
        <div class="found small">
          {#if testError}<span class="error">{testError}</span>
          {:else if r?.error}<span class="error">{r.error}</span>
          {:else if r}
            In this session: <strong>{r.instances.length}</strong> cycle{r.instances.length === 1 ? '' : 's'}{#if r.filtered || r.no_end || r.gaps}<span class="faint">{' · left out: '}{[r.filtered ? `${r.filtered} by must have / excluded${t.woken_by?.length ? ' / woken by' : ''}` : '', r.no_end ? `${r.no_end} without the end phase` : '', r.gaps ? `${r.gaps} with a gap` : ''].filter(Boolean).join(', ')}</span>{/if}
          {:else}<span class="faint">Counting…</span>{/if}
          {#if phasesChanged}<span class="faint">{' · counted with the saved phases: save to count with your changes'}</span>{/if}
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  .types { display: flex; flex-direction: column; }
  .intro { margin: 0; padding: 10px 16px; font-size: 13px; color: var(--muted); line-height: 1.5; border-bottom: 1px solid var(--line); }
  .split { display: grid; grid-template-columns: minmax(0, 220px) minmax(0, 1fr); gap: 14px; padding: 12px 16px; max-height: 58vh; overflow: auto; }
  @media (max-width: 640px) { .split { grid-template-columns: minmax(0, 1fr); } }
  .list { display: flex; flex-direction: column; gap: 4px; }
  .item { display: flex; align-items: center; gap: 8px; width: 100%; padding: 7px 9px; border: 1px solid transparent; border-radius: 9px; background: transparent; color: var(--text); font: inherit; text-align: left; cursor: pointer; }
  .item:hover { background: var(--panel2); }
  .item[aria-selected='true'] { border-color: var(--accent); background: var(--sel); }
  .tx { display: flex; flex-direction: column; flex: 1; min-width: 0; }
  .tn { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .kind { font-size: 11px; color: var(--faint); }
  .count { color: var(--muted); font-size: 12px; }
  .count.bad { color: var(--rec); font-weight: 700; }
  .sw { display: inline-block; width: 10px; height: 10px; border-radius: 3px; flex: none; }
  .add { border-style: dashed; margin-top: 4px; }
  .empty { margin: 0; font-size: 13px; padding: 4px 2px; }
  .edit { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .name { width: 220px; min-height: 32px; font-weight: 600; }
  .tag { font-size: 11px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }
  .roles { width: 100%; border-collapse: collapse; font-size: 13px; }
  .roles td { padding: 4px 8px; border-top: 1px solid var(--line); }
  .roles td:first-child { padding-left: 4px; }
  .roles tr.on td { background: var(--sel); }
  .ph { white-space: nowrap; }
  .ph .sw { margin-right: 8px; vertical-align: -1px; }
  .input.small { min-height: 30px; width: auto; font-size: 12px; }
  .sentence { padding: 8px 12px; border-radius: 8px; background: var(--panel2); font-size: 13px; }
  .found { color: var(--muted); }
  .woken { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  .chip { min-height: 28px; padding: 0 10px; border-radius: 999px; border: 1px solid var(--line); background: transparent; color: var(--text); font: inherit; font-size: 12px; cursor: pointer; }
  .chip[aria-pressed='true'] { border: 2px solid var(--accent); background: var(--sel); font-weight: 600; }
  .small { font-size: 12px; }
</style>
