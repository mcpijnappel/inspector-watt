<script lang="ts">
  // Phase rules of a profile: edit, reorder, test live against this session, save (or save as new profile).
  import { api, type CycleType, type Profile, type Rule } from '../lib/api';
  import { outside } from '../lib/outside';
  import type { DraftRule } from '../lib/types';
  import CycleTypesEditor from './CycleTypesEditor.svelte';

  type Props = {
    sessionId: number;
    profileId: number | null;
    draft?: DraftRule | null;
    /** Which tab opens: the phases or the cycle types. */
    tab?: 'phases' | 'types';
    /** Make the session use the saved profile (off when editing from Settings). */
    link?: boolean;
    onclose: () => void;
    onsaved: (profileId: number) => void;
  };
  let { sessionId, profileId, draft = null, tab = 'phases', link = true, onclose, onsaved }: Props = $props();
  // svelte-ignore state_referenced_locally
  let view = $state<'phases' | 'types'>(tab);

  const PALETTE = ['#a371f7', '#db61a2', '#58a6ff', '#3fb950', '#d29922', '#8b949e', '#f0883e', '#39c5cf', '#ff7b72', '#bc8cff'];

  let profiles = $state<Profile[]>([]);
  let current = $state<number | null>(null);
  let name = $state('');
  let rules = $state<Rule[]>([]);
  let types = $state<CycleType[]>([]);
  /** Regex for the log line that says what woke the board ('' = the default). */
  let wakePattern = $state('');
  /** The phases as saved: the type counts use those. */
  let savedRules = '';
  let highlight = $state<number | null>(null);
  let counts = $state<{ cycles: number; found: number[]; woken_by: Record<string, number> } | null>(null);
  let testError = $state('');
  let error = $state('');
  let busy = $state(false);
  let newName = $state('');
  let asNew = $state(false);

  function useProfile(p: Profile | undefined, withDraft: DraftRule | null) {
    current = p?.id ?? null;
    name = p?.name ?? '';
    rules = (p?.rules ?? []).map((r) => ({ ...r }));
    savedRules = JSON.stringify(p?.rules ?? []);
    types = (p?.types ?? []).map((t) => ({ ...t, end: t.end ? { ...t.end } : null, must: [...t.must], exclude: [...t.exclude], woken_by: [...(t.woken_by ?? [])] }));
    wakePattern = p?.wake_pattern ?? '';
    highlight = null;
    if (withDraft) {
      const used = new Set(rules.map((r) => r.color));
      const color = PALETTE.find((c) => !used.has(c)) ?? PALETTE[rules.length % PALETTE.length];
      const at = Math.min(Math.max(0, withDraft.insertAt), rules.length);
      rules.splice(at, 0, { ...withDraft.rule, color });
      highlight = at;
    }
  }

  $effect(() => {
    api.profiles().then((ps) => {
      profiles = ps;
      useProfile(ps.find((p) => p.id === profileId) ?? ps[0], draft);
    }).catch((e) => (error = (e as Error).message));
  });

  // Test the rules against this session while editing (debounced).
  $effect(() => {
    const snapshot = JSON.stringify(rules);
    const wake = wakePattern;
    const valid = rules.every((r) => r.pattern && (r.match !== 'regex' || isRegex(r.pattern))) && wakeOk;
    if (!rules.length || !valid) {
      counts = null;
      testError = valid ? '' : wakeOk ? 'Fix the highlighted pattern' : 'Fix the wake reason line';
      return;
    }
    const t = setTimeout(() => {
      api.testRules(sessionId, JSON.parse(snapshot), wake).then((c) => { counts = c; testError = ''; }).catch((e) => (testError = (e as Error).message));
    }, 350);
    return () => clearTimeout(t);
  });

  /** The wake reason line: empty (the default), or a regex with the reason in brackets. */
  const wakeOk = $derived(!wakePattern || (isRegex(wakePattern) && /\((?!\?)/.test(wakePattern.replace(/\\./g, ''))));
  const wakeCount = $derived(counts ? Object.values(counts.woken_by).reduce((a, b) => a + b, 0) : 0);
  /** Wake reasons seen in this session (for "Woken by" in the cycle types). */
  const reasons = $derived(Object.keys(counts?.woken_by ?? {}));

  function isRegex(p: string) {
    try {
      new RegExp(p);
      return true;
    } catch {
      return false;
    }
  }

  function move(i: number, d: number) {
    const j = i + d;
    if (j < 0 || j >= rules.length) return;
    const r = [...rules];
    [r[i], r[j]] = [r[j], r[i]];
    rules = r;
    highlight = j;
  }

  function add() {
    const used = new Set(rules.map((r) => r.color));
    rules = [...rules, { name: 'New phase', color: PALETTE.find((c) => !used.has(c)) ?? '#8b949e', match: 'contains', pattern: '', snap: false }];
    highlight = rules.length - 1;
  }

  /** Renaming a phase renames it in the cycle types too. */
  function rename(i: number, v: string) {
    const old = rules[i].name;
    rules[i].name = v;
    if (!old || old === v || rules.some((r, k) => k !== i && r.name === old)) return;
    const sw = (n: string) => (n === old ? v : n);
    types = types.map((t) => ({ ...t, begin: sw(t.begin), end: t.end ? { ...t.end, phase: sw(t.end.phase) } : null, must: t.must.map(sw), exclude: t.exclude.map(sw) }));
  }

  const phasesChanged = $derived(JSON.stringify(rules) !== savedRules);
  const typeProblem = $derived.by(() => {
    const names = new Set(rules.map((r) => r.name));
    const seen = new Set<string>();
    for (const t of types) {
      const n = t.name.trim();
      if (!n) return 'A cycle type needs a name';
      if (seen.has(n)) return `Two cycle types are called "${n}"`;
      seen.add(n);
      if (!t.begin) return `Cycle type "${n}": pick the phase it begins at`;
      const used = [t.begin, ...t.must, ...t.exclude, ...(t.end ? [t.end.phase] : [])];
      const gone = used.find((u) => !names.has(u));
      if (gone) return `Cycle type "${n}" uses "${gone}", which isn't a phase any more`;
    }
    return '';
  });

  async function save() {
    busy = true;
    error = '';
    try {
      let id = current;
      if (asNew || id == null) {
        const p = await api.createProfile(newName.trim() || `${name || 'profile'} copy`, rules, types, wakePattern);
        id = p.id;
      } else {
        await api.updateProfile(id, { name: name.trim() || undefined, rules, types, wake_pattern: wakePattern });
      }
      if (link) await api.setSessionProfile(sessionId, id);
      onsaved(id);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }
</script>

<div class="backdrop">
  <div class="dialog panel" role="dialog" aria-modal="true" aria-label="Phase rules" use:outside={onclose}>
    <div class="panel-head">
      <h2>Phase rules</h2>
      <label class="prof">Profile
        <select class="input small" value={String(current ?? '')} onchange={(e) => useProfile(profiles.find((p) => p.id === Number((e.target as HTMLSelectElement).value)), null)}>
          {#each profiles as p (p.id)}<option value={String(p.id)}>{p.name}</option>{/each}
        </select>
      </label>
      <div class="seg" role="group" aria-label="Edit">
        <button type="button" aria-pressed={view === 'phases'} onclick={() => (view = 'phases')}>Phases ({rules.length})</button>
        <button type="button" aria-pressed={view === 'types'} onclick={() => (view = 'types')}>Cycle types ({types.length})</button>
      </div>
      <span class="spacer"></span>
      <button class="btn small ghost icon" type="button" aria-label="Close" onclick={onclose}>×</button>
    </div>
    {#if view === 'types'}
      <CycleTypesEditor {sessionId} {rules} bind:types {phasesChanged} {reasons} />
    {:else}
    <p class="intro">A phase starts at the first log line that matches its rule and runs until the next phase starts. The <strong>first rule starts a new cycle</strong>. Changes apply to every session that uses this profile.</p>

    <div class="rules">
      {#each rules as r, i (i)}
        {@const bad = !r.pattern || (r.match === 'regex' && !isRegex(r.pattern))}
        <div class="rule" class:hl={highlight === i}>
          <div class="top">
            <span class="n mono">{i + 1}</span>
            <input class="color" type="color" bind:value={r.color} aria-label="Colour of {r.name}" />
            <input class="input name" type="text" value={r.name} oninput={(e) => rename(i, (e.target as HTMLInputElement).value)} aria-label="Phase name" />
            {#if counts}
              {@const f = counts.found[i] ?? 0}
              <span class="found" class:low={f < counts.cycles * 0.9}>{f} / {counts.cycles}</span>
            {/if}
            <button class="btn small ghost icon" type="button" aria-label="Move up" disabled={i === 0} onclick={() => move(i, -1)}>↑</button>
            <button class="btn small ghost icon" type="button" aria-label="Move down" disabled={i === rules.length - 1} onclick={() => move(i, 1)}>↓</button>
            <button class="btn small ghost icon" type="button" aria-label="Delete {r.name}" onclick={() => (rules = rules.filter((_, k) => k !== i))}>×</button>
          </div>
          <div class="bottom">
            <span class="muted small">{i === 0 ? 'cycle starts at' : 'starts at'} log line</span>
            <select class="input small match" bind:value={r.match} aria-label="Match type">
              <option value="contains">contains</option>
              <option value="regex">regex</option>
            </select>
            <input class="input small pattern mono" class:bad type="text" bind:value={r.pattern} aria-label="Pattern" />
            <label class="check small" title="Move the boundary onto the nearest current step (±20 ms) in the raw data"><input type="checkbox" bind:checked={r.snap} />snap to current step</label>
          </div>
        </div>
      {/each}
      <button class="btn ghost add" type="button" onclick={add}>+ Add phase (or right-click a log line)</button>
      <div class="rule wake">
        <div class="top"><strong class="small">Wake reason</strong><span class="muted small">the log line that says what woke the board; the reason goes in brackets</span>
          {#if counts}<span class="found" class:low={wakeCount < counts.cycles * 0.9}>{wakeCount} / {counts.cycles}</span>{/if}
        </div>
        <div class="bottom">
          <span class="muted small">regex</span>
          <input class="input small pattern mono" class:bad={!wakeOk} type="text" bind:value={wakePattern} placeholder={'Wake source: (.+)   (empty: lines like "Wake source: Timer")'} aria-label="Wake reason line" />
        </div>
        {#if counts && Object.keys(counts.woken_by).length}
          <div class="bottom muted small">Found: {Object.entries(counts.woken_by).map(([r, n]) => `${n} ${r}`).join(' · ')}</div>
        {/if}
      </div>
    </div>
    {/if}

    <div class="foot">
      <span class="status">
        {#if testError}<span class="error">{testError}</span>{:else if counts}Found in this session: {counts.cycles} cycles{:else}…{/if}
      </span>
      {#if error}<span class="error">{error}</span>{:else if typeProblem}<span class="error">{typeProblem}</span>{/if}
      <span class="spacer"></span>
      <label class="check small"><input type="checkbox" bind:checked={asNew} />as new profile</label>
      {#if asNew}<input class="input small newname" type="text" placeholder="New profile name" bind:value={newName} />{/if}
      <button class="btn ghost" type="button" onclick={onclose}>Cancel</button>
      <button class="btn primary" type="button" disabled={busy || rules.some((r) => !r.pattern) || !!typeProblem} onclick={save}>{busy ? 'Saving…' : 'Save'}</button>
    </div>
  </div>
</div>

<style>
  .backdrop { position: fixed; inset: 0; z-index: 100; background: rgba(0, 0, 0, 0.45); display: flex; align-items: flex-start; justify-content: center; padding: 6vh 12px; overflow: auto; }
  .dialog { width: min(820px, 100%); box-shadow: var(--shadow); }
  .prof { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); }
  .small { font-size: 12px; }
  .input.small { min-height: 32px; width: auto; }
  .intro { margin: 0; padding: 10px 16px; font-size: 13px; color: var(--muted); line-height: 1.5; border-bottom: 1px solid var(--line); }
  .rules { padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; max-height: 58vh; overflow: auto; }
  .rule { border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; }
  .rule.hl { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .top, .bottom { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  .bottom { padding-left: 24px; }
  .n { width: 16px; color: var(--faint); font-size: 12px; }
  .color { width: 28px; height: 28px; padding: 0; border: 1px solid var(--line); border-radius: 6px; background: none; cursor: pointer; }
  .name { flex: 1 1 160px; min-height: 32px; font-weight: 600; }
  .match { width: 100px; }
  .pattern { flex: 1 1 220px; }
  .pattern.bad { border-color: var(--rec); }
  .found { font-family: var(--mono); font-size: 12px; padding: 3px 8px; border-radius: 999px; color: var(--ok); background: rgba(63, 185, 80, 0.14); }
  .found.low { color: var(--warn); background: rgba(227, 179, 65, 0.14); }
  .add { border-style: dashed; }
  .wake .top { gap: 8px; }
  .foot { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--line); font-size: 13px; }
  .status { color: var(--muted); }
  .newname { width: 180px; }
</style>
