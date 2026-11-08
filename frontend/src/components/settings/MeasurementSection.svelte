<script lang="ts">
  // Measurement & safety: idle power-off and spike filter (applied in capture right away and kept), the battery
  // sizes, and the settings that only change in .env.
  import { api, type Measurement } from '../../lib/api';
  import { fmtDuration } from '../../lib/format';
  import { live } from '../../lib/live.svelte';
  import { settings } from '../../lib/settings.svelte';

  let m = $state<Measurement | null>(null);
  let error = $state('');
  let saved = $state('');
  let idleMin = $state('');
  let newSize = $state('');

  $effect(() => {
    api.measurement().then((r) => {
      m = r;
      idleMin = String(r.idle_power_off_s / 60);
    }).catch((e) => (error = (e as Error).message));
    void settings.load();
  });

  async function save(change: { idle_power_off_s?: number; spike_filter?: boolean }, what: string) {
    error = '';
    saved = '';
    try {
      m = await api.setMeasurement(change);
      idleMin = String(m.idle_power_off_s / 60);
      saved = what;
      await live.pollStatus();
    } catch (e) {
      error = (e as Error).message;
    }
  }
  function saveIdle(e: SubmitEvent) {
    e.preventDefault();
    const v = parseFloat(idleMin.replace(',', '.'));
    if (!(v >= 0 && v <= 1440)) return void (error = 'Idle power-off: 0 to 1440 minutes (0 = never).');
    void save({ idle_power_off_s: Math.round(v * 60) }, 'idle');
  }
  function addSize(e: SubmitEvent) {
    e.preventDefault();
    const v = parseFloat(newSize.replace(',', '.'));
    if (v > 0) {
      settings.addBattery(v);
      newSize = '';
    }
  }
  const idleIn = $derived(live.status?.idle_power_off?.in_s ?? null);
  const idleChanged = $derived(m != null && parseFloat(idleMin.replace(',', '.')) * 60 !== m.idle_power_off_s);
</script>

<div class="section">
  <h2 class="title">Measurement &amp; safety</h2>
  {#if error}<div class="banner bad">{error}</div>{/if}

  {#if m}
    <section class="panel card">
      <form class="item" onsubmit={saveIdle}>
        <div class="txt">
          <strong>Idle power-off</strong>
          <span class="muted small">Switches the DUT off when nothing records and no browser has the app open for this long, so a forgotten board doesn't keep running. 0 = never.</span>
          <span class="faint small">{m.idle_power_off_s === 0 ? 'Off.' : idleIn != null ? `Would switch off in ${fmtDuration(idleIn)} if nobody watches.` : 'Not counting now (DUT off, or recording).'}{saved === 'idle' ? ' Saved.' : ''}</span>
        </div>
        <span class="input-unit"><input type="text" inputmode="decimal" bind:value={idleMin} aria-label="Idle power-off in minutes" size="4" /><span class="muted">min</span></span>
        <button class="btn small" type="submit" disabled={!idleChanged}>Save</button>
      </form>
      <label class="item">
        <div class="txt">
          <strong>Spike filter</strong>
          <span class="muted small">Smooths the short spikes the PPK2 makes when it switches measuring range, like Nordic's Power Profiler does. Turn off to see raw range switches.</span>
          {#if saved === 'spike'}<span class="faint small">Saved.</span>{/if}
        </div>
        <input type="checkbox" checked={m.spike_filter} onchange={(e) => save({ spike_filter: (e.target as HTMLInputElement).checked }, 'spike')} />
      </label>
    </section>
  {/if}

  <section class="panel card">
    <div class="txt">
      <strong>Battery sizes</strong>
      <span class="muted small">For the battery-life estimates on the Live and session pages. The marked one is the default.</span>
    </div>
    <div class="sizes" role="group" aria-label="Battery sizes">
      {#each settings.batteries.sizes_mah as s (s)}
        <span class="size" class:on={settings.batteries.selected_mah === s}>
          <button class="pick" type="button" aria-pressed={settings.batteries.selected_mah === s} onclick={() => settings.selectBattery(s)}>{s} mAh</button>
          <button class="x" type="button" aria-label="Remove {s} mAh" disabled={settings.batteries.sizes_mah.length < 2} onclick={() => settings.removeBattery(s)}>×</button>
        </span>
      {/each}
      <form class="add" onsubmit={addSize}>
        <span class="input-unit"><input type="text" inputmode="decimal" bind:value={newSize} placeholder="2500" aria-label="New battery size in mAh" size="6" /><span class="muted">mAh</span></span>
        <button class="btn small" type="submit">Add</button>
      </form>
    </div>
  </section>

  {#if m}
    <h3 class="sub">Set in .env</h3>
    <p class="intro muted small">These protect the hardware or need a restart: change them in <code>.env</code>, then <code>./dc up -d capture</code>.</p>
    <section class="panel fixed">
      <dl>
        <dt>Maximum voltage <code>MAX_VOLTAGE_MV</code></dt><dd class="mono">{(m.fixed.max_voltage_mv / 1000).toFixed(2)} V <span class="faint">· each device can set a lower limit in <a href="#/settings/devices">Devices</a></span></dd>
        <dt>Stop writing raw data below <code>MIN_FREE_GB</code></dt><dd class="mono">{m.fixed.min_free_gb} GB free</dd>
        <dt>Live buffer <code>RING_SECONDS</code></dt><dd class="mono">{m.fixed.ring_seconds} s <span class="faint">· what Live and "Save…" can reach back</span></dd>
        <dt>Raw precision <code>RAW_MANTISSA_BITS</code></dt><dd class="mono">{m.fixed.raw_mantissa_bits} bits <span class="faint">· {m.fixed.raw_mantissa_bits >= 23 ? 'lossless' : `error ≤ ${(100 / 2 ** (m.fixed.raw_mantissa_bits + 1)).toFixed(3)} %`}</span></dd>
        <dt>Measure when the PPK2 connects <code>AUTO_STREAM</code></dt><dd class="mono">{m.fixed.auto_stream ? 'yes' : 'no'}</dd>
        <dt>UART baud at start <code>UART_BAUD</code></dt><dd class="mono">{m.fixed.uart_baud}</dd>
        <dt>Ports <code>PPK2_PORT</code> · <code>UART_PORT</code></dt><dd class="mono">{m.fixed.ppk2_port} · {m.fixed.uart_port}</dd>
      </dl>
    </section>
  {/if}
</div>

<style>
  .section { display: flex; flex-direction: column; gap: 12px; }
  .title { margin: 0; font-size: 18px; }
  .sub { margin: 10px 0 0; font-size: 15px; }
  .intro { margin: 0; }
  .small { font-size: 12px; }
  .banner { padding: 10px 14px; border-radius: 10px; }
  .banner.bad { border: 1px solid var(--rec); color: var(--rec); background: var(--rec-bg); }
  .card { padding: 4px 16px; display: flex; flex-direction: column; }
  .card > .txt { padding-top: 10px; }
  .item { display: flex; align-items: center; gap: 12px; padding: 12px 0; }
  .item + .item { border-top: 1px solid var(--line); }
  .txt { display: flex; flex-direction: column; gap: 3px; flex: 1; min-width: 0; }
  .item input[type='checkbox'] { width: 18px; height: 18px; }
  .sizes { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 10px 0 14px; }
  .size { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .size.on { border: 2px solid var(--accent); background: var(--sel); }
  .pick, .x { border: 0; background: transparent; color: var(--text); font: inherit; font-size: 13px; padding: 6px 10px; cursor: pointer; }
  .size.on .pick { font-weight: 600; }
  .x { padding: 6px 8px; color: var(--faint); border-left: 1px solid var(--line); }
  .x:disabled { opacity: 0.4; cursor: default; }
  .add { display: inline-flex; gap: 6px; align-items: center; }
  .fixed { padding: 4px 16px; }
  dl { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); margin: 0; }
  dt, dd { margin: 0; padding: 9px 0; border-top: 1px solid var(--line); font-size: 13px; }
  dt:first-of-type, dt:first-of-type + dd { border-top: 0; }
  dd { overflow-wrap: anywhere; }
  code { font-family: var(--mono); font-size: 11px; color: var(--faint); }
  @media (max-width: 640px) { dl { grid-template-columns: minmax(0, 1fr); } dd { border-top: 0; padding-top: 0; } }
</style>
