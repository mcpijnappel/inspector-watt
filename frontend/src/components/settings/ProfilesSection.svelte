<script lang="ts">
  // Phase-rule profiles (one per firmware): rename, edit rules and cycle types, duplicate, delete; and the record
  // presets.
  import { api, type Preset, type Profile, type SessionSummary } from '../../lib/api';
  import { devices } from '../../lib/devices.svelte';
  import RulesEditor from '../RulesEditor.svelte';
  import SectionHead from './SectionHead.svelte';

  let profiles = $state<Profile[]>([]);
  let sessions = $state<SessionSummary[]>([]);
  let presets = $state<Preset[]>([]);
  let error = $state('');
  let confirmDelete = $state<number | null>(null);
  let confirmPreset = $state<number | null>(null);
  let editing = $state<{ profile: number; session: number } | null>(null);
  let renaming = $state<number | null>(null);
  let usedOpen = $state<number | null>(null); // the profile whose full session list is open

  async function load() {
    try {
      [profiles, sessions, presets] = await Promise.all([api.profiles(), api.sessions(), api.presets()]);
      await devices.load();
    } catch (e) {
      error = (e as Error).message;
    }
  }
  $effect(() => void load());

  const usedBy = (p: Profile) => sessions.filter((s) => s.profile_id === p.id);
  const deviceNames = (p: Profile) => devices.list.filter((d) => d.profile_id === p.id).map((d) => d.name);

  async function act(fn: () => Promise<unknown>) {
    error = '';
    try {
      await fn();
      await load();
    } catch (e) {
      error = (e as Error).message;
    }
  }
  function rename(p: Profile, name: string) {
    renaming = null;
    name = name.trim();
    if (name && name !== p.name) void act(() => api.updateProfile(p.id, { name }));
  }
  function renameKey(e: KeyboardEvent) {
    const el = e.target as HTMLInputElement;
    if (e.key === 'Enter') el.blur();
    if (e.key === 'Escape') {
      renaming = null; // blur after this sees renaming == null and keeps the old name
    }
  }
  const focus = (el: HTMLInputElement) => {
    el.focus();
    el.select();
  };
  function duplicate(p: Profile) {
    let name = `${p.name} copy`;
    for (let n = 2; profiles.some((x) => x.name === name); n++) name = `${p.name} copy ${n}`;
    void act(() => api.createProfile(name, p.rules, p.types));
  }
  function edit(p: Profile) {
    // Test against the newest session that uses it, else the newest session.
    const s = usedBy(p)[0] ?? sessions[0];
    if (s) editing = { profile: p.id, session: s.id };
  }
  function presetSummary(c: Preset['config']): string {
    const parts: string[] = [c.kind ?? 'detail'];
    if (c.battery) parts.push(`battery ${c.battery.chemistry} ${c.battery.mode === 'charge' ? `${c.battery.capacity_mah} mAh` : `${c.battery.sweep_min} min sweep`}`);
    else if (c.voltage_mv) parts.push(`${(c.voltage_mv / 1000).toFixed(2)} V`);
    if (c.wake_threshold_ua) parts.push(`wake ≥ ${c.wake_threshold_ua / 1000} mA`);
    if (c.uart_baud) parts.push(`${c.uart_baud} baud`);
    if (c.max_duration_s) parts.push(`stop after ${c.max_duration_s / 60} min`);
    return parts.join(' · ');
  }
</script>

<div class="s-section">
  <SectionHead title="Phase profiles" intro="Phase rules and cycle types for one firmware. Sessions using a profile are analysed with it." />
  {#if error}<div class="s-banner">{error}</div>{/if}

  {#each profiles as p (p.id)}
    {@const used = usedBy(p)}
    {@const devs = deviceNames(p)}
    <section class="panel card" aria-label={p.name}>
      <div class="top">
        {#if renaming === p.id}
          <input class="input name" type="text" value={p.name} maxlength="100" aria-label="Profile name" use:focus
            onblur={(e) => renaming === p.id && rename(p, (e.target as HTMLInputElement).value)} onkeydown={renameKey} />
        {:else}
          <h3>{p.name}</h3>
        {/if}
        {#if devs.length}<span class="badge">default for {devs.join(', ')}</span>{/if}
        <span class="muted small">{p.rules.length} phase{p.rules.length === 1 ? '' : 's'} · {p.types.length} cycle type{p.types.length === 1 ? '' : 's'}</span>
      </div>
      <div class="phases">
        {#each p.rules as r (r.name)}<span class="ph"><i style:background={r.color}></i>{r.name}</span>{/each}
      </div>
      {#if p.types.length}<div class="small muted">Cycle types: {p.types.map((t) => t.name).join(', ')}</div>{/if}
      <div class="small">
        {#if used.length}
          {@const list = usedOpen === p.id ? used : used.slice(0, 3)}
          Used by {used.length} session{used.length === 1 ? '' : 's'}: {#each list as s, i (s.id)}{i ? ', ' : ''}<a href="#/sessions/{s.id}">#{s.id} {s.name}</a>{/each}
          {#if used.length > 3}<button class="linkbtn" type="button" onclick={() => (usedOpen = usedOpen === p.id ? null : p.id)}>{usedOpen === p.id ? 'Show 3' : 'Show all'}</button>{/if}
        {:else}<span class="faint">No sessions use it.</span>{/if}
      </div>
      {#if confirmDelete === p.id}
        <div class="s-confirm">Delete {p.name}?{used.length ? ` Its ${used.length} session${used.length === 1 ? '' : 's'} then use the most recently used profile.` : ''}
          <button class="btn small danger" type="button" onclick={() => { confirmDelete = null; void act(() => api.deleteProfile(p.id)); }}>Delete</button>
          <button class="btn small ghost" type="button" onclick={() => (confirmDelete = null)}>Cancel</button>
        </div>
      {:else}
        <div class="actions">
          <button class="btn small" type="button" disabled={!sessions.length} title={sessions.length ? '' : 'Record a session first: rules are tested on one'} onclick={() => edit(p)}>Edit rules &amp; types</button>
          <button class="btn small ghost" type="button" onclick={() => (renaming = p.id)}>Rename</button>
          <button class="btn small ghost" type="button" onclick={() => duplicate(p)}>Duplicate</button>
          <button class="btn small ghost" type="button" disabled={profiles.length < 2} title={profiles.length < 2 ? 'The last profile stays' : ''} onclick={() => (confirmDelete = p.id)}>Delete</button>
        </div>
      {/if}
    </section>
  {/each}

  <h3 class="s-sub">Record presets</h3>
  <p class="s-note">Made from the Record menu: Record ▾ → other settings → Save as preset.</p>
  {#if presets.length}
    <section class="panel list">
      {#each presets as pr (pr.id)}
        <div class="row">
          <div class="txt"><strong>{pr.name}</strong><span class="muted small">{presetSummary(pr.config)}</span></div>
          {#if confirmPreset === pr.id}
            <button class="btn small danger" type="button" onclick={() => { confirmPreset = null; void act(() => api.deletePreset(pr.id)); }}>Delete</button>
            <button class="btn small ghost" type="button" onclick={() => (confirmPreset = null)}>Cancel</button>
          {:else}
            <button class="btn small ghost" type="button" onclick={() => (confirmPreset = pr.id)}>Delete</button>
          {/if}
        </div>
      {/each}
    </section>
  {:else}
    <p class="faint small">No presets yet.</p>
  {/if}
</div>

{#if editing}
  <RulesEditor sessionId={editing.session} profileId={editing.profile} link={false} onclose={() => (editing = null)}
    onsaved={() => { editing = null; void load(); }} />
{/if}

<style>
  .card { padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; }
  .top { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
  .name { width: min(320px, 100%); min-height: 34px; font-weight: 600; }
  h3 { margin: 0; font-size: 15px; font-weight: 600; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--sel); color: var(--accent); font-weight: 600; }
  .phases { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; }
  .ph { display: inline-flex; align-items: center; gap: 6px; }
  .ph i { width: 10px; height: 10px; border-radius: 2px; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .list { display: flex; flex-direction: column; }
  .row { display: flex; align-items: center; gap: 8px; padding: 10px 16px; }
  .row + .row { border-top: 1px solid var(--line); }
  .txt { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
</style>
