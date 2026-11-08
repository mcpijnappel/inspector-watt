<script lang="ts">
  import { api } from '../lib/api';
  import { fmtDuration } from '../lib/format';
  import { live } from '../lib/live.svelte';
  import { router } from '../lib/router.svelte';
  import { theme } from '../lib/theme.svelte';
  import { devices } from '../lib/devices.svelte';
  import DeviceMenu from './DeviceMenu.svelte';
  import AgentChip from './AgentChip.svelte';
  import UsbChip from './UsbChip.svelte';

  let deviceOpen = $state(false);
  $effect(() => devices.ensure());

  let stopping = $state(false);
  let error = $state('');

  const st = $derived(live.status);
  const p = $derived(st?.ppk2);
  const session = $derived(st?.session?.active ? st.session.session : undefined);
  const nav = [
    { href: '#/live', name: 'live', label: 'Live' },
    { href: '#/sessions', name: 'sessions', label: 'Sessions' },
    { href: '#/compare', name: 'compare', label: 'Compare' },
  ];
  const themeLabel = $derived(theme.mode === 'system' ? 'Theme: system' : theme.mode === 'dark' ? 'Theme: dark' : 'Theme: light');

  async function stop() {
    stopping = true;
    error = '';
    try {
      await api.stopSession();
      await live.pollStatus();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      stopping = false;
    }
  }
</script>

<header class="bar">
  <a class="brand" href="#/live">
    <svg width="24" height="24" viewBox="0 0 64 64" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="27" cy="27" r="18" stroke="var(--text)" stroke-width="5.5"></circle><line x1="40" y1="40" x2="55" y2="55" stroke="var(--text)" stroke-width="7.5"></line><path d="M13 28h5l3-9 4 16 3-12 3 5h7" stroke="var(--trace)" stroke-width="3.5"></path></svg>
    <span>Inspector Watt</span>
  </a>
  <nav aria-label="Main">
    {#each nav as n (n.name)}
      <a href={n.href} class:active={router.route.name === n.name || (n.name === 'sessions' && router.route.name === 'session')} aria-current={router.route.name === n.name ? 'page' : undefined}>{n.label}</a>
    {/each}
  </nav>
  <span class="spacer"></span>

  <div class="devices">
    {#if live.statusError}
      <span class="chip"><span class="dot bad"></span>API unreachable</span>
    {:else if p}
      <span class="chip" title={p.error ?? ''}>
        <span class="dot" class:ok={p.connected && p.streaming} class:bad={!p.connected}></span>
        PPK2 · {p.connected ? (p.streaming ? `${Math.round((p.stream.rate_sps ?? 0) / 1000)} kS/s` : 'idle') : 'offline'}
      </span>
      <span class="chip" title={st?.uart.error ?? ''}>
        <span class="dot" class:ok={st?.uart.connected} class:bad={!st?.uart.connected}></span>UART · {st?.uart.baud}
      </span>
      <UsbChip />
      <div class="anchor">
        <button class="chip power" type="button" aria-expanded={deviceOpen} onclick={() => (deviceOpen = !deviceOpen)}
          title="Device on the bench (switch power on the Live page)">
          <span class="dot" class:ok={p.dut_power}></span><span class="dname">{devices.active?.name ?? 'No device'}</span>
          · {p.dut_power == null ? '?' : p.dut_power ? `on ${((p.voltage_mv ?? 0) / 1000).toFixed(2)} V` : 'off'}
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M6 9l6 6 6-6"></path></svg>
        </button>
        {#if deviceOpen}<DeviceMenu onclose={() => (deviceOpen = false)} />{/if}
      </div>
    {/if}
  </div>

  <div class="rec">
    <AgentChip />
    {#if session}
      <span class="recpill" title="Recording session #{session.id}"><span class="dot rec"></span>REC {fmtDuration(session.duration_s)} · {session.name}</span>
      <button class="btn" type="button" disabled={stopping} onclick={stop}>Stop</button>
    {/if}
    <a class="btn icon ghost gear" class:active={router.route.name === 'settings'} href="#/settings" aria-label="Settings" title="Settings"
      aria-current={router.route.name === 'settings' ? 'page' : undefined}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.6 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.6a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"></path></svg>
    </a>
    <button class="btn icon ghost" type="button" aria-label={themeLabel} title={themeLabel} onclick={() => theme.cycle()}>
      {#if theme.mode === 'system'}
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2"></rect><path d="M8 20h8M12 16v4"></path></svg>
      {:else if theme.mode === 'dark'}
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"></path></svg>
      {:else}
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"></path></svg>
      {/if}
    </button>
  </div>
  {#if error}<div class="error full">{error}</div>{/if}
</header>

<style>
  .bar { position: sticky; top: 0; z-index: 40; display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px; padding: 10px 24px; background: var(--panel); border-bottom: 1px solid var(--line); }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 15px; color: var(--text); text-decoration: none; }
  nav { display: flex; gap: 4px; }
  nav a { padding: 8px 12px; border-radius: 6px; color: var(--muted); text-decoration: none; font-weight: 500; }
  nav a.active { background: var(--panel2); color: var(--text); font-weight: 600; }
  .devices, .rec { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .power { color: var(--text); }
  .anchor { position: relative; }
  .dname { max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .recpill { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: 999px; background: var(--rec-bg); color: var(--rec); font-weight: 600; font-size: 13px; max-width: 360px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .full { flex-basis: 100%; }
  .gear { color: var(--text); text-decoration: none; }
  .gear.active { background: var(--panel2); }
  @media (max-width: 720px) {
    .bar { padding: 10px 12px; }
    .brand span { display: none; }
  }
</style>
