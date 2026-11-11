<script lang="ts">
  // Settings: one section at a time (#/settings/<section>), picked from the list on the left (a dropdown on phones).
  import AgentsSection from '../components/settings/AgentsSection.svelte';
  import DevicesSection from '../components/settings/DevicesSection.svelte';
  import FirmwareSection from '../components/settings/FirmwareSection.svelte';
  import HealthSection from '../components/settings/HealthSection.svelte';
  import LibrarySection from '../components/settings/LibrarySection.svelte';
  import MeasurementSection from '../components/settings/MeasurementSection.svelte';
  import ModulesSection from '../components/settings/ModulesSection.svelte';
  import ProfilesSection from '../components/settings/ProfilesSection.svelte';
  import StorageSection from '../components/settings/StorageSection.svelte';
  import '../components/settings/settings.css';

  let { section }: { section?: string } = $props();

  const SECTIONS = [
    { id: 'devices', label: 'Devices & sensors', hint: 'Boards, their chips, the sensor library' },
    { id: 'profiles', label: 'Phase profiles', hint: 'Phase rules, cycle types, record presets' },
    { id: 'measurement', label: 'Measurement & safety', hint: 'Idle power-off, spike filter, batteries' },
    { id: 'firmware', label: 'Firmware upload', hint: 'Upload status, token, build setup' },
    { id: 'storage', label: 'Storage', hint: 'Disk space and data per session' },
    { id: 'modules', label: 'Modules', hint: 'Logic analyzer and other extras' },
    { id: 'agents', label: 'Agents (MCP)', hint: 'Connect AI agents, hardware control' },
    { id: 'health', label: 'System health', hint: 'PPK2 stream, UART, services' },
  ] as const;
  const KEY = 'pst.settings.section';
  // Sub-pages: #/settings/library is the Sensor library tab of "Devices & sensors"
  const SUB: Record<string, string> = { library: 'devices' };
  const known = (id: string | null | undefined) => !!id && (SECTIONS.some((s) => s.id === id) || id in SUB);

  function remembered(): string {
    try {
      return localStorage.getItem(KEY) ?? 'devices';
    } catch {
      return 'devices';
    }
  }
  const page = $derived(known(section) ? section! : known(remembered()) ? remembered() : 'devices');
  const current = $derived(SUB[page] ?? page);
  $effect(() => {
    try {
      localStorage.setItem(KEY, page);
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
      {#if current === 'devices'}
        <div class="subtabs" role="tablist" aria-label="Devices and sensors">
          <a role="tab" href="#/settings/devices" aria-selected={page === 'devices'}>Boards</a>
          <a role="tab" href="#/settings/library" aria-selected={page === 'library'}>Sensor library</a>
        </div>
        {#if page === 'library'}<LibrarySection />{:else}<DevicesSection />{/if}
      {:else if current === 'profiles'}<ProfilesSection />
      {:else if current === 'measurement'}<MeasurementSection />
      {:else if current === 'firmware'}<FirmwareSection />
      {:else if current === 'storage'}<StorageSection />
      {:else if current === 'modules'}<ModulesSection />
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
  .subtabs { display: flex; gap: 4px 20px; border-bottom: 1px solid var(--line); margin-bottom: 14px; }
  .subtabs a { flex-direction: row; padding: 6px 0 8px; border-radius: 0; border-bottom: 2px solid transparent; margin-bottom: -1px; color: var(--muted); font-size: 14px; }
  .subtabs a:hover { background: none; color: var(--text); }
  .subtabs a[aria-selected='true'] { border-bottom-color: var(--accent); color: var(--text); font-weight: 600; }
  .content { min-width: 0; }
  @media (max-width: 760px) {
    .page { padding: 12px; }
    .layout { grid-template-columns: minmax(0, 1fr); gap: 14px; }
    nav { position: static; }
    .wide { display: none; }
    .phone { display: block; }
  }
</style>
