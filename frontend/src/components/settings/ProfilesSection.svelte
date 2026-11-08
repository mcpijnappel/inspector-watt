<script lang="ts">
  // Phase-rule profiles (one per firmware): rename, edit rules and cycle types, duplicate, delete; and the record
  // presets.
  import { api, type Preset, type Profile, type SessionSummary } from '../../lib/api';
  import { devices } from '../../lib/devices.svelte';
  import RulesEditor from '../RulesEditor.svelte';

  let profiles = $state<Profile[]>([]);
  let sessions = $state<SessionSummary[]>([]);
  let presets = $state<Preset[]>([]);
  let error = $state('');
  let confirmDelete = $state<number | null>(null);
  let confirmPreset = $state<number | null>(null);
  let editing = $state<{ profile: number; session: number } | null>(null);

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
    name = name.trim();
    if (name && name !== p.name) void act(() => api.updateProfile(p.id, { name }));
  }
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

<div class="section">
  <h2 class="title">Phase profiles</h2>
  <p class="intro muted">A profile holds the phase rules and cycle types for one firmware. Every session that uses it is analysed with them, and a device can give it to its new sessions.</p>
  {#if error}<div class="banner bad">{error}</div>{/if}

  {#each profiles as p (p.id)}
    {@const used = usedBy(p)}
    {@const devs = deviceNames(p)}
    <section class="panel card" aria-label={p.name}>
      <div class="top">
        <input class="input name" type="text" value={p.name} maxlength="100" aria-label="Profile name"
          onchange={(e) => rename(p, (e.target as HTMLInputElement).value)} onkeydown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()} />
        <span class="muted small">{p.rules.length} phase{p.rules.length === 1 ? '' : 's'} · {p.types.length} cycle type{p.types.length === 1 ? '' : 's'}</span>
      </div>
      <div class="phases">
        {#each p.rules as r (r.name)}<span class="ph"><i style:background={r.color}></i>{r.name}</span>{/each}
      </div>
      {#if p.types.length}<div class="small muted">Cycle types: {p.types.map((t) => t.name).join(', ')}</div>{/if}
      <div class="small">
        {#if used.length}Used by {#each used.slice(0, 6) as s, i (s.id)}{i ? ', ' : ''}<a href="#/sessions/{s.id}">#{s.id} {s.name}</a>{/each}{used.length > 6 ? ` and ${used.length - 6} more` : ''}{:else}<span class="faint">No sessions use it.</span>{/if}
        {#if devs.length}<span class="muted"> · default for {devs.join(', ')}</span>{/if}
      </div>
      {#if confirmDelete === p.id}
        <div class="confirm">Delete {p.name}?{used.length ? ` Its ${used.length} session${used.length === 1 ? '' : 's'} then use the most recently used profile.` : ''}
          <button class="btn small danger" type="button" onclick={() => { confirmDelete = null; void act(() => api.deleteProfile(p.id)); }}>Delete</button>
          <button class="btn small ghost" type="button" onclick={() => (confirmDelete = null)}>Cancel</button>
        </div>
      {:else}
        <div class="actions">
          <button class="btn small" type="button" disabled={!sessions.length} title={sessions.length ? '' : 'Record a session first: rules are tested on one'} onclick={() => edit(p)}>Edit rules &amp; types</button>
          <button class="btn small ghost" type="button" onclick={() => duplicate(p)}>Duplicate</button>
          <button class="btn small ghost" type="button" disabled={profiles.length < 2} title={profiles.length < 2 ? 'The last profile stays' : ''} onclick={() => (confirmDelete = p.id)}>Delete</button>
        </div>
      {/if}
    </section>
  {/each}

  <h2 class="title sub">Record presets</h2>
  <p class="intro muted">Saved from the Record menu (Record ▾ → other settings → Save as preset).</p>
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
  .section { display: flex; flex-direction: column; gap: 12px; }
  .title { margin: 0; font-size: 18px; }
  .title.sub { margin-top: 14px; }
  .intro { margin: 0; font-size: 13px; }
  .banner { padding: 10px 14px; border-radius: 10px; }
  .banner.bad { border: 1px solid var(--rec); color: var(--rec); background: var(--rec-bg); }
  .card { padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; }
  .top { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
  .name { width: min(320px, 100%); min-height: 34px; font-weight: 600; }
  .phases { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; }
  .ph { display: inline-flex; align-items: center; gap: 6px; }
  .ph i { width: 10px; height: 10px; border-radius: 2px; }
  .small { font-size: 12px; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; }
  .confirm { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; padding: 8px 10px; border-radius: 8px; background: var(--panel2); }
  .btn.danger { background: var(--rec-strong); border-color: var(--rec-strong); color: #fff; }
  .list { display: flex; flex-direction: column; }
  .row { display: flex; align-items: center; gap: 8px; padding: 10px 16px; }
  .row + .row { border-top: 1px solid var(--line); }
  .txt { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
</style>
