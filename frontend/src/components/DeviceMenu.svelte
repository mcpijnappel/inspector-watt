<script lang="ts">
  // Pick the device on the bench. Changing it while the board is powered switches the board off first (the next
  // board may not take the same voltage); not possible while a session records.
  import { api, type Device } from '../lib/api';
  import { devices } from '../lib/devices.svelte';
  import { live } from '../lib/live.svelte';
  import { outside } from '../lib/outside';
  import { place } from '../lib/place';

  let { onclose }: { onclose: () => void } = $props();

  let confirm = $state<Device | null>(null);
  let busy = $state(false);
  let error = $state('');

  const recording = $derived(!!live.status?.session?.active);
  const powered = $derived(!!live.status?.ppk2.dut_power);

  async function pick(d: Device, switchOff = false) {
    if (d.id === devices.activeId) return onclose();
    if (powered && !switchOff) {
      confirm = d;
      return;
    }
    busy = true;
    error = '';
    try {
      if (switchOff) await api.setDut(false);
      await devices.activate(d.id);
      await live.pollStatus();
      onclose();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  const V = (mv: number) => `${(mv / 1000).toFixed(2)} V`;
</script>

<div class="popover menu" use:outside={onclose} use:place role="dialog" aria-label="Device on the bench">
  <div class="title">Device on the bench</div>
  {#if confirm}
    <div class="confirm">
      <p>{devices.active?.name ?? 'The board'} is powered. Switch it off and change to <strong>{confirm.name}</strong>?</p>
      <div class="row">
        <button class="btn small" type="button" disabled={busy} onclick={() => confirm && pick(confirm, true)}>Switch off and change</button>
        <button class="btn small ghost" type="button" onclick={() => (confirm = null)}>Cancel</button>
      </div>
    </div>
  {:else}
    <div class="list" role="listbox" aria-label="Devices">
      {#each devices.list as d (d.id)}
        <button type="button" class="item" role="option" aria-selected={d.id === devices.activeId} disabled={busy || (recording && d.id !== devices.activeId)} onclick={() => pick(d)}>
          <span class="check" aria-hidden="true">{d.id === devices.activeId ? '✓' : ''}</span>
          <span class="txt">
            <span class="name">{d.name}</span>
            <span class="hint">{V(d.voltage_mv)} · max {V(d.max_mv)}{d.profile_name ? ` · ${d.profile_name}` : ''}</span>
          </span>
        </button>
      {:else}
        <p class="hint pad">No devices yet.</p>
      {/each}
    </div>
    {#if recording}<p class="hint pad">A session is recording: stop it to change the device.</p>{/if}
  {/if}
  {#if error}<p class="error pad">{error}</p>{/if}
  <a class="manage" href="#/settings/devices" onclick={onclose}>Manage devices…</a>
</div>

<style>
  .menu { right: 0; top: calc(100% + 8px); width: 300px; padding: 8px; display: flex; flex-direction: column; gap: 4px; }
  .title { font-size: 12px; font-weight: 600; color: var(--muted); padding: 4px 8px; }
  .list { display: flex; flex-direction: column; gap: 2px; }
  .item { display: flex; align-items: flex-start; gap: 8px; width: 100%; padding: 8px; border: 0; border-radius: 8px; background: transparent; color: var(--text); cursor: pointer; text-align: left; }
  .item:hover:not(:disabled), .item[aria-selected='true'] { background: var(--panel2); }
  .item:disabled { opacity: 0.5; cursor: default; }
  .check { width: 14px; color: var(--accent); font-weight: 700; }
  .txt { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .confirm { padding: 4px 8px 8px; display: flex; flex-direction: column; gap: 8px; }
  .confirm p { margin: 0; font-size: 13px; }
  .row { display: flex; gap: 6px; flex-wrap: wrap; }
  .pad { padding: 4px 8px; margin: 0; }
  .manage { display: block; padding: 8px; border-top: 1px solid var(--line); margin-top: 4px; font-size: 13px; text-decoration: none; }
</style>
