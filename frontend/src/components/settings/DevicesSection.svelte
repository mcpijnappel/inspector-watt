<script lang="ts">
  // Devices (boards): defaults for power-on and recording, a voltage limit capture enforces, and the phase rules
  // their new sessions get. One card per device; Edit unfolds the form in place.
  import { api, type Device, type DeviceForm, type Profile } from '../../lib/api';
  import { bus } from '../../lib/bus.svelte';
  import { devices } from '../../lib/devices.svelte';
  import { live } from '../../lib/live.svelte';
  import BoardChips from './BoardChips.svelte';
  import SectionHead from './SectionHead.svelte';

  const BAUDS = [9600, 57600, 115200, 230400, 460800, 921600];
  const EMPTY: DeviceForm = { name: '', notes: '', voltage_mv: 3700, max_mv: 4200, wake_threshold_ua: 5000, uart_baud: 115200, profile_id: null };

  let profiles = $state<Profile[]>([]);
  let editing = $state<number | 'new' | null>(null);
  let form = $state<DeviceForm>({ ...EMPTY });
  let volts = $state('');
  let maxVolts = $state('');
  let wakeMa = $state('');
  let error = $state('');
  let busy = $state(false);
  let confirmDelete = $state<number | null>(null);
  let confirmSwitch = $state<number | null>(null);

  $effect(() => {
    devices.load().catch((e) => (error = (e as Error).message));
    api.profiles().then((p) => (profiles = p)).catch(() => {});
  });

  const hardMax = $derived(live.status?.ppk2.hard_max_mv ?? null);
  const recording = $derived(!!live.status?.session?.active);
  const powered = $derived(!!live.status?.ppk2.dut_power);
  const V = (mv: number) => `${(mv / 1000).toFixed(2)} V`;
  const parseMv = (v: string) => Math.round(parseFloat(v.replace(',', '.')) * 1000);

  function edit(d: Device | null) {
    form = d ? { name: d.name, notes: d.notes, voltage_mv: d.voltage_mv, max_mv: d.max_mv, wake_threshold_ua: d.wake_threshold_ua, uart_baud: d.uart_baud, profile_id: d.profile_id } : { ...EMPTY };
    volts = (form.voltage_mv / 1000).toFixed(2);
    maxVolts = (form.max_mv / 1000).toFixed(2);
    wakeMa = String(form.wake_threshold_ua / 1000);
    editing = d ? d.id : 'new';
    error = '';
  }

  async function save(e: SubmitEvent) {
    e.preventDefault();
    const body: DeviceForm = { ...form, name: form.name.trim(), voltage_mv: parseMv(volts), max_mv: parseMv(maxVolts), wake_threshold_ua: parseFloat(wakeMa.replace(',', '.')) * 1000 };
    if (!body.name) return void (error = 'Give it a name.');
    if (!Number.isFinite(body.voltage_mv) || !Number.isFinite(body.max_mv) || !Number.isFinite(body.wake_threshold_ua)) return void (error = 'Check the numbers.');
    busy = true;
    error = '';
    try {
      await devices.save(body, editing === 'new' || editing == null ? undefined : editing);
      await live.pollStatus();
      editing = null;
    } catch (err) {
      error = (err as Error).message;
    } finally {
      busy = false;
    }
  }

  async function remove(id: number) {
    try {
      await devices.remove(id);
      confirmDelete = null;
    } catch (err) {
      error = (err as Error).message;
    }
  }

  async function use(d: Device, switchOff = false) {
    if (powered && !switchOff) return void (confirmSwitch = d.id);
    busy = true;
    error = '';
    try {
      if (switchOff) await api.setDut(false);
      await devices.activate(d.id);
      await live.pollStatus();
      confirmSwitch = null;
    } catch (err) {
      error = (err as Error).message;
    } finally {
      busy = false;
    }
  }

  function summary(d: Device): string {
    const cap = hardMax && hardMax < d.max_mv ? `, capped at ${V(hardMax)} by the tracker's limit` : '';
    const parts = [`${V(d.voltage_mv)} (max ${V(d.max_mv)}${cap})`, `wake ≥ ${d.wake_threshold_ua / 1000} mA`];
    parts.push(`${d.uart_baud} baud`);
    parts.push(d.profile_name ? `phase rules: ${d.profile_name}` : 'phase rules: last used');
    return parts.join(' · ');
  }
</script>

<div class="s-section">
  <SectionHead title="Boards" intro={`The board on the bench sets the Turn on voltage, record defaults and phase rules for new sessions.${bus.analyzer ? ' Each board lists the chips and sensors on its bus, from the sensor library.' : ''}`}>
    {#snippet action()}<button class="btn primary" type="button" disabled={editing === 'new'} onclick={() => edit(null)}>Add device</button>{/snippet}
  </SectionHead>
  {#if error && editing == null}<div class="s-banner">{error}</div>{/if}

  {#snippet editor()}
    <form class="form" onsubmit={save}>
      <div class="grid">
        <label class="field wide">Name<input class="input" type="text" bind:value={form.name} maxlength="100" /></label>
        <label class="field">Default voltage<span class="input-unit"><input type="text" inputmode="decimal" bind:value={volts} /><span class="muted">V</span></span><span class="hint">Turn on and new recordings</span></label>
        <label class="field">Maximum<span class="input-unit"><input type="text" inputmode="decimal" bind:value={maxVolts} /><span class="muted">V</span></span>
          <span class="hint">{hardMax && parseMv(maxVolts) > hardMax ? `capped at ${V(hardMax)} by the tracker's limit` : 'nothing goes above this'}</span></label>
        <label class="field">Wake threshold<span class="input-unit"><input type="text" inputmode="decimal" bind:value={wakeMa} /><span class="muted">mA</span></span></label>
        <label class="field">UART baud
          <select class="input" bind:value={form.uart_baud}>
            {#each BAUDS.includes(form.uart_baud) ? BAUDS : [...BAUDS, form.uart_baud].sort((a, b) => a - b) as b (b)}<option value={b}>{b}</option>{/each}
          </select>
          <span class="hint">Its debug log; set when it goes on the bench</span>
        </label>
        <label class="field">Phase rules
          <select class="input" bind:value={form.profile_id}>
            <option value={null}>the last ones used</option>
            {#each profiles as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
          </select>
        </label>
        <label class="field wide">Notes<textarea class="input" rows="3" bind:value={form.notes} placeholder="Hardware revision, mods, how it's wired…"></textarea></label>
      </div>
      {#if error}<div class="error">{error}</div>{/if}
      <div class="actions">
        <button class="btn ghost" type="button" onclick={() => (editing = null)}>Cancel</button>
        <button class="btn primary" type="submit" disabled={busy}>{editing === 'new' ? 'Add' : 'Save'}</button>
      </div>
    </form>
  {/snippet}

  {#if editing === 'new'}<section class="panel card">{@render editor()}</section>{/if}

  {#each devices.list as d (d.id)}
    {@const isActive = d.id === devices.activeId}
    <section class="panel card" class:active={isActive} aria-label={d.name}>
      {#if editing === d.id}
        {@render editor()}
      {:else}
        <div class="top">
          <h2>{d.name}</h2>
          {#if isActive}<span class="badge on">on the bench</span>{/if}
          <span class="spacer"></span>
          <a class="small" href="#/sessions?device={d.id}">{d.sessions ?? 0} session{d.sessions === 1 ? '' : 's'} →</a>
        </div>
        <div class="mono small sum">{summary(d)}</div>
        {#if d.notes}<p class="notes">{d.notes}</p>{/if}
        {#if confirmSwitch === d.id}
          <div class="s-confirm">{devices.active?.name ?? 'The board'} is powered. Switch it off and put {d.name} on the bench?
            <button class="btn small" type="button" disabled={busy} onclick={() => use(d, true)}>Switch off and change</button>
            <button class="btn small ghost" type="button" onclick={() => (confirmSwitch = null)}>Cancel</button>
          </div>
        {:else if confirmDelete === d.id}
          <div class="s-confirm">Delete {d.name}? Its sessions stay, without a device.
            <button class="btn small danger" type="button" onclick={() => remove(d.id)}>Delete</button>
            <button class="btn small ghost" type="button" onclick={() => (confirmDelete = null)}>Cancel</button>
          </div>
        {:else}
          <div class="actions left">
            {#if !isActive}<button class="btn small" type="button" disabled={busy || recording} title={recording ? 'A session is recording' : ''} onclick={() => use(d)}>Put on the bench</button>{/if}
            <button class="btn small ghost" type="button" onclick={() => edit(d)}>Edit</button>
            {#if !isActive}<button class="btn small ghost" type="button" onclick={() => (confirmDelete = d.id)}>Delete</button>{/if}
          </div>
        {/if}
        {#if bus.analyzer}<BoardChips device={d} others={devices.list.filter((x) => x.id !== d.id)} />{/if}
      {/if}
    </section>
  {:else}
    {#if devices.loaded}<p class="muted">No devices yet. Add the board you're measuring.</p>{/if}
  {/each}
</div>

<style>
  .card { padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; }
  .card.active { border-color: var(--accent); }
  .top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  h2 { margin: 0; font-size: 16px; }
  .badge.on { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--sel); color: var(--accent); font-weight: 600; }
  .small { font-size: 12px; }
  .sum { color: var(--muted); }
  .notes { margin: 0; font-size: 13px; white-space: pre-wrap; }
  .actions { display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
  .actions.left { justify-content: flex-start; }
  .form { display: flex; flex-direction: column; gap: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
  .wide { grid-column: 1 / -1; }
  textarea.input { resize: vertical; font: inherit; padding: 8px 10px; }
  select.input { min-height: 38px; }
</style>
