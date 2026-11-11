<script lang="ts">
  // Settings: one section at a time (#/settings/<section>), picked from the list on the left (a dropdown on phones).
  import AgentsSection from '../components/settings/AgentsSection.svelte';
  import DevicesSection from '../components/settings/DevicesSection.svelte';
  import FirmwareSection from '../components/settings/FirmwareSection.svelte';
  import HealthSection from '../components/settings/HealthSection.svelte';
  import MeasurementSection from '../components/settings/MeasurementSection.svelte';
  import ProfilesSection from '../components/settings/ProfilesSection.svelte';
  import StorageSection from '../components/settings/StorageSection.svelte';
  import '../components/settings/settings.css';

  let { section }: { section?: string } = $props();

  const SECTIONS = [
    { id: 'devices', label: 'Devices', hint: 'Boards on the bench, voltage limits' },
    { id: 'profiles', label: 'Phase profiles', hint: 'Phase rules, cycle types, record presets' },
    { id: 'measurement', label: 'Measurement & safety', hint: 'Idle power-off, spike filter, batteries' },
    { id: 'firmware', label: 'Firmware upload', hint: 'Upload status, token, build setup' },
    { id: 'storage', label: 'Storage', hint: 'Disk space and data per session' },
    { id: 'agents', label: 'Agents (MCP)', hint: 'Connect AI agents, hardware control' },
    { id: 'health', label: 'System health', hint: 'PPK2 stream, UART, services' },
  ] as const;
  const KEY = 'pst.settings.section';

  function remembered(): string {
    try {
      return localStorage.getItem(KEY) ?? 'devices';
    } catch {
      return 'devices';
    }
  }
  const current = $derived(SECTIONS.some((s) => s.id === section) ? section! : SECTIONS.some((s) => s.id === remembered()) ? remembered() : 'devices');
  $effect(() => {
    try {
      localStorage.setItem(KEY, current);
    } catch { /* storage blocked */ }
  });
</script>

<div class="page">
  <h1>Settings</h1>
  <div class="layout">
    <nav aria-label="Settings sections">
      <select class="input phone" value={current} onchange={(e) => (location.hash = `#/settings/${(e.target as HTMLSelectElement).value}`)} aria-label="Section">
        {#each SECTIONS as s (s.id)}<option value={s.id}>{s.label}</option>{/each}
      </select>
      <ul class="wide">
        {#each SECTIONS as s (s.id)}
          <li><a href="#/settings/{s.id}" class:on={current === s.id} aria-current={current === s.id ? 'page' : undefined}>
            <span class="lbl">{s.label}</span><span class="hint">{s.hint}</span></a></li>
        {/each}
      </ul>
    </nav>
    <main class="content settings">
      {#if current === 'devices'}<DevicesSection />
      {:else if current === 'profiles'}<ProfilesSection />
      {:else if current === 'measurement'}<MeasurementSection />
      {:else if current === 'firmware'}<FirmwareSection />
      {:else if current === 'storage'}<StorageSection />
      {:else if current === 'agents'}<AgentsSection />
      {:else}<HealthSection />{/if}
    </main>
  </div>
</div>

<style>
  .page { max-width: 1180px; margin: 0 auto; padding: 16px 24px 32px; display: flex; flex-direction: column; gap: 14px; }
  h1 { margin: 0; font-size: 22px; }
  .layout { display: grid; grid-template-columns: 230px minmax(0, 1fr); gap: 24px; align-items: start; }
  nav { position: sticky; top: 76px; }
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  a { display: flex; flex-direction: column; gap: 1px; padding: 8px 12px; border-radius: 8px; color: var(--text); text-decoration: none; }
  a:hover { background: var(--panel2); }
  a.on { background: var(--sel); }
  a.on .lbl { color: var(--accent); }
  .lbl { font-weight: 600; font-size: 14px; }
  .phone { display: none; width: 100%; }
  .content { min-width: 0; }
  @media (max-width: 760px) {
    .page { padding: 12px; }
    .layout { grid-template-columns: minmax(0, 1fr); gap: 14px; }
    nav { position: static; }
    .wide { display: none; }
    .phone { display: block; }
  }
</style>
