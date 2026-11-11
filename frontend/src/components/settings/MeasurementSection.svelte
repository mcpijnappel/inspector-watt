<script lang="ts">
  // Measurement & safety: idle power-off, spike filter, measuring on connect and raw precision (applied in capture
  // right away and kept), the tracker's voltage limit (behind a confirmation; agents can't change it), the battery
  // sizes, and the settings that only change in .env.
  import { api, type Measurement } from '../../lib/api';
  import { devPath, fmtDuration } from '../../lib/format';
  import { live } from '../../lib/live.svelte';
  import { settings } from '../../lib/settings.svelte';
  import SectionHead from './SectionHead.svelte';

  let m = $state<Measurement | null>(null);
  let error = $state('');
  let saved = $state('');
  let idleMin = $state('');
  let newSize = $state('');
  let limitOpen = $state(false);
  let limitVolts = $state('');
  let limitBusy = $state(false);

  const BITS = [10, 12, 14, 16, 20, 23];
  const V = (mv: number) => `${(mv / 1000).toFixed(2)} V`;
  const bitsNote = (b: number) => (b >= 23 ? 'lossless' : `error ≤ ${(100 / 2 ** (b + 1)).toFixed(b >= 14 ? 4 : 3)} %`);
  const limitMv = $derived(Math.round(parseFloat(limitVolts.replace(',', '.')) * 1000));
  const limitValid = $derived(limitMv >= 800 && limitMv <= 5000);
  const powered = $derived(!!live.status?.ppk2.dut_power);
  const outMv = $derived(live.status?.ppk2.voltage_mv ?? null);

  $effect(() => {
    api.measurement().then((r) => {
      m = r;
      idleMin = String(r.idle_power_off_s / 60);
    }).catch((e) => (error = (e as Error).message));
    void settings.load();
  });

  async function save(change: { idle_power_off_s?: number; spike_filter?: boolean; auto_stream?: boolean; raw_mantissa_bits?: number }, what: string) {
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
  const focus = (el: HTMLInputElement) => {
    el.focus();
    el.select();
  };
  function openLimit() {
    if (!m) return;
    limitVolts = (m.max_voltage_mv / 1000).toFixed(2);
    limitOpen = true;
  }
  async function setLimit() {
    limitBusy = true;
    error = '';
    try {
      m = await api.setMaxVoltage(limitMv);
      limitOpen = false;
      saved = 'limit';
      await live.pollStatus();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      limitBusy = false;
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

<div class="s-section">
  <SectionHead title="Measurement & safety" intro="How the tracker measures, and what keeps a forgotten board from running on." />
  {#if error}<div class="s-banner">{error}</div>{/if}

  {#if m}
    <section class="panel card">
      <form class="item" onsubmit={saveIdle}>
        <div class="txt">
          <strong>Idle power-off</strong>
          <span class="muted small">Switch the board off when nothing records and no browser is open for this long. 0 = never.</span>
          <span class="faint small">{m.idle_power_off_s === 0 ? 'Off.' : idleIn != null ? `Would switch off in ${fmtDuration(idleIn)} if nobody watches.` : 'Not counting now (board off, or recording).'}{saved === 'idle' ? ' Saved.' : ''}</span>
        </div>
        <span class="input-unit"><input type="text" inputmode="decimal" bind:value={idleMin} aria-label="Idle power-off in minutes" size="4" /><span class="muted">min</span></span>
        <button class="btn small" type="submit" disabled={!idleChanged}>Save</button>
      </form>
      <label class="item">
        <div class="txt">
          <strong>Spike filter</strong>
          <span class="muted small">Smooth the PPK2's short range-switch spikes, like Nordic's Power Profiler. Off: see raw range switches.</span>
          {#if saved === 'spike'}<span class="faint small">Saved.</span>{/if}
        </div>
        <input type="checkbox" checked={m.spike_filter} onchange={(e) => save({ spike_filter: (e.target as HTMLInputElement).checked }, 'spike')} />
      </label>
      <label class="item">
        <div class="txt">
          <strong>Measure when the PPK2 connects</strong>
          <span class="muted small">Start the PPK2 stream as soon as it's plugged in or capture starts. Off: only when you start it.</span>
          {#if saved === 'stream'}<span class="faint small">Saved.</span>{/if}
        </div>
        <input type="checkbox" checked={m.auto_stream} onchange={(e) => save({ auto_stream: (e.target as HTMLInputElement).checked }, 'stream')} />
      </label>
      <label class="item">
        <div class="txt">
          <strong>Raw precision</strong>
          <span class="muted small">Significant bits kept per raw sample, from the next recording on. 12 is plenty; 23 keeps every bit (~40 % bigger files).</span>
          {#if saved === 'bits'}<span class="faint small">Saved.</span>{/if}
        </div>
        <select class="input bits" value={m.raw_mantissa_bits} onchange={(e) => save({ raw_mantissa_bits: Number((e.target as HTMLSelectElement).value) }, 'bits')}
          aria-label="Raw precision in bits">
          {#each BITS.includes(m.raw_mantissa_bits) ? BITS : [...BITS, m.raw_mantissa_bits].sort((a, b) => a - b) as b (b)}
            <option value={b}>{b} bits · {bitsNote(b)}</option>
          {/each}
        </select>
      </label>
    </section>

    <section class="panel card">
      <div class="item">
        <div class="txt">
          <strong>Voltage limit</strong>
          <span class="muted small">Nothing goes above it, whatever a device says; each device can set a lower one in <a href="#/settings/devices">Devices</a>. Agents can't change it.</span>
          {#if saved === 'limit'}<span class="faint small">Saved.</span>{/if}
        </div>
        <span class="mono limit">{V(m.max_voltage_mv)}</span>
        <button class="btn small" type="button" onclick={openLimit}>Change…</button>
      </div>
    </section>
  {/if}

  <section class="panel card">
    <div class="txt">
      <strong>Battery sizes</strong>
      <span class="muted small">For battery-life estimates on Live and session pages. The marked one is the default.</span>
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
    <details class="panel s-fold">
      <summary>Set in .env (the live buffer and the ports: they need a restart)</summary>
      <div class="inner">
      <dl>
        <dt>Live buffer <code>RING_SECONDS</code></dt><dd class="mono">{m.fixed.ring_seconds} s <span class="faint">· what Live and "Save…" can reach back</span></dd>
        <dt>Ports <code>PPK2_PORT</code> · <code>UART_PORT</code> · <code>USB_PORT</code></dt><dd class="mono">{devPath(m.fixed.ppk2_port)} · {devPath(m.fixed.uart_port)} · {devPath(m.fixed.usb_port)}</dd>
      </dl>
      <p class="faint small">Change them in <code>.env</code>, then <code>./dc up -d capture</code>.</p>
      </div>
    </details>
  {/if}
</div>

{#if limitOpen && m}
  <!-- The voltage limit protects the hardware: a change needs this extra step (and the api refuses it to agents). -->
  <div class="backdrop" role="presentation" onclick={(e) => e.target === e.currentTarget && !limitBusy && (limitOpen = false)}>
    <div class="dialog panel" role="alertdialog" aria-modal="true" aria-labelledby="limit-title" tabindex="-1"
      onkeydown={(e) => e.key === 'Escape' && !limitBusy && (limitOpen = false)}>
      <h3 id="limit-title">Change the voltage limit?</h3>
      <p class="muted small">Now {V(m.max_voltage_mv)}. The tracker never powers a board above it, whatever its device settings say.</p>
      <label class="field">New limit
        <span class="input-unit"><input type="text" inputmode="decimal" bind:value={limitVolts} aria-label="New voltage limit in volts" use:focus /><span class="muted">V</span></span>
        <span class="hint">{limitValid ? '0.80 to 5.00 V' : 'Give a value from 0.80 to 5.00 V'}</span>
      </label>
      {#if limitValid && limitMv > m.max_voltage_mv}
        <p class="warn small">Higher: boards can then get up to {V(limitMv)}. Check what's on the bench can take it (a 1-cell LiPo input: 4.20 V).</p>
      {:else if limitValid && powered && outMv != null && outMv > limitMv}
        <p class="warn small">The board is powered at {V(outMv)}, above the new limit: it's switched off.</p>
      {/if}
      <div class="dactions">
        <button class="btn ghost" type="button" disabled={limitBusy} onclick={() => (limitOpen = false)}>Cancel</button>
        <button class="btn danger" type="button" disabled={limitBusy || !limitValid || limitMv === m.max_voltage_mv} onclick={setLimit}>
          Set the limit to {limitValid ? V(limitMv) : '…'}</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .limit { font-size: 14px; font-weight: 600; }
  .bits { width: auto; min-height: 34px; }
  .backdrop { position: fixed; inset: 0; z-index: 60; background: rgba(0, 0, 0, 0.45); display: grid; place-items: center; padding: 16px; }
  .dialog { width: min(440px, 100%); padding: 18px 20px; display: flex; flex-direction: column; gap: 12px; box-shadow: var(--shadow); }
  .dialog h3 { margin: 0; font-size: 16px; }
  .warn { color: var(--warn); }
  .dactions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
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
  dl { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); margin: 0; }
  dt, dd { margin: 0; padding: 9px 0; border-top: 1px solid var(--line); font-size: 13px; }
  dt:first-of-type, dt:first-of-type + dd { border-top: 0; }
  dd { overflow-wrap: anywhere; }
  code { font-family: var(--mono); font-size: 11px; color: var(--faint); }
  @media (max-width: 640px) { dl { grid-template-columns: minmax(0, 1fr); } dd { border-top: 0; padding-top: 0; } }
</style>
