<script lang="ts">
  // Modules: optional extras with their own containers (modules/<name>/). Each one says what it is and which
  // settings it has; the form is drawn from that. Installing happens in .env (COMPOSE_PROFILES), not here.
  import { api, type ModuleInfo, type ModuleSetting } from '../../lib/api';
  import SectionHead from './SectionHead.svelte';

  let mods = $state<ModuleInfo[] | null>(null);
  let error = $state('');
  let saved = $state('');
  let drafts = $state<Record<string, string>>({}); // number fields being typed: "name.key" -> text

  async function load() {
    try {
      mods = await api.modules();
      error = '';
    } catch (e) {
      error = (e as Error).message;
    }
  }
  $effect(() => {
    void load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  });

  async function set(m: ModuleInfo, key: string, value: unknown) {
    saved = '';
    try {
      m.values = await api.setModuleSettings(m.name, { [key]: value });
      saved = `${m.name}.${key}`;
      delete drafts[`${m.name}.${key}`];
      await load();
    } catch (e) {
      error = (e as Error).message;
    }
  }
  const fmtChoice = (s: ModuleSetting, v: number | string) =>
    s.unit === 'Hz' && typeof v === 'number' ? (v >= 1e6 ? `${v / 1e6} MHz` : `${v / 1e3} kHz`) : `${v}${s.unit ? ` ${s.unit}` : ''}`;
  function saveNumber(e: SubmitEvent, m: ModuleInfo, s: ModuleSetting) {
    e.preventDefault();
    const v = parseFloat((drafts[`${m.name}.${s.key}`] ?? '').replace(',', '.'));
    if (!Number.isFinite(v)) return void (error = `${s.label}: a number`);
    void set(m, s.key, v);
  }
</script>

<div class="s-section">
  <SectionHead title="Modules" intro="Optional extras with their own containers, like the logic analyzer. They're installed in .env and set up here." />
  {#if error}<div class="s-banner">{error}</div>{/if}

  {#each mods ?? [] as m (m.name)}
    {@const d = m.module}
    <section class="panel">
      <div class="s-card head">
        <div class="s-status">
          <span class="dot" class:ok={d?.health.ok} class:warn={!!d && !d.health.ok}></span>
          <strong class="state">{m.title}</strong>
          {#if d}<span class="faint small mono">v{d.version}</span>{/if}
          <span class="muted small">{d ? d.health.detail : 'not installed'}</span>
        </div>
        {#if d?.description}<p class="muted small">{d.description}</p>{/if}
      </div>
      {#if !d}
        <div class="s-card body">
          <p class="small">To install it, add <code>{m.name}</code> to <code>COMPOSE_PROFILES</code> in <code>.env</code>, then run <code>./dc up -d</code>. Its README (<code>modules/{m.name}/README.md</code>) has the hardware setup.</p>
          {#if m.error && m.error !== 'not installed'}<p class="faint small">{m.error}</p>{/if}
        </div>
      {:else}
        <div class="s-card body rows">
          {#each d.settings as s (s.key)}
            {@const v = m.values[s.key]}
            {@const id = `${m.name}.${s.key}`}
            {#if s.type === 'bool'}
              <label class="s-row">
                <div class="txt"><strong class="state">{s.label}</strong>{#if s.hint}<span class="hint">{s.hint}{saved === id ? ' Saved.' : ''}</span>{/if}</div>
                <input type="checkbox" checked={!!v} onchange={(e) => set(m, s.key, (e.target as HTMLInputElement).checked)} />
              </label>
            {:else if s.type === 'choice'}
              <label class="s-row">
                <div class="txt"><strong class="state">{s.label}</strong>{#if s.hint}<span class="hint">{s.hint}{saved === id ? ' Saved.' : ''}</span>{/if}</div>
                <select class="input pick" value={v} onchange={(e) => {
                  const raw = (e.target as HTMLSelectElement).value;
                  void set(m, s.key, typeof s.choices?.[0] === 'number' ? Number(raw) : raw);
                }} aria-label={s.label}>
                  {#each s.choices ?? [] as c (c)}<option value={c}>{fmtChoice(s, c)}</option>{/each}
                </select>
              </label>
            {:else if s.type === 'number'}
              <form class="s-row" onsubmit={(e) => saveNumber(e, m, s)}>
                <div class="txt"><strong class="state">{s.label}</strong>{#if s.hint}<span class="hint">{s.hint}{saved === id ? ' Saved.' : ''}</span>{/if}</div>
                <span class="input-unit"><input type="text" inputmode="decimal" size="5" aria-label={s.label}
                  value={drafts[id] ?? String(v ?? '')} oninput={(e) => (drafts[id] = (e.target as HTMLInputElement).value)} />{#if s.unit}<span class="muted">{s.unit}</span>{/if}</span>
                <button class="btn small" type="submit" disabled={drafts[id] == null || drafts[id] === String(v)}>Save</button>
              </form>
            {/if}
          {/each}
        </div>
        {#if d.events.length || d.tools.length}
          <details class="s-fold s-card">
            <summary>What it adds: {d.events.map((e) => e.label).join(', ') || 'no'} lanes · {d.tools.length} MCP tool{d.tools.length === 1 ? '' : 's'}</summary>
            <div class="inner small muted">
              {#each d.tools as t (t.name)}<span><code>{m.name}_{t.name}</code> {t.description}{t.hardware ? ' (asks for the hardware allow)' : ''}</span>{/each}
            </div>
          </details>
        {/if}
      {/if}
    </section>
  {:else}
    {#if mods}<p class="muted">No modules known.</p>{:else if !error}<p class="faint">Loading…</p>{/if}
  {/each}
</div>

<style>
  .head { padding-bottom: 10px; }
  .body { padding-top: 0; }
  .state { font-weight: 500; }
  .rows { gap: 0; }
  .pick { width: auto; min-height: 34px; }
  code { font-family: var(--mono); font-size: 12px; }
</style>
