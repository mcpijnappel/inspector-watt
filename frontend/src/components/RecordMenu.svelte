<script lang="ts">
  // Record menu: one click with the last settings, presets, or the full form.
  import { api, type BatteryForm, type Chemistry, type Preset, type SessionForm } from '../lib/api';
  import { batteryLabel, loadChemistries, maxStartSoc, socToMv } from '../lib/battery';
  import { autoName } from '../lib/format';
  import { devices } from '../lib/devices.svelte';
  import { live } from '../lib/live.svelte';
  import { outside } from '../lib/outside';
  import { place } from '../lib/place';

  let { onclose }: { onclose: () => void } = $props();

  const DEFAULTS: SessionForm = {
    name: '', kind: 'detail', notes: '', voltage_mv: 3700, dut_power: true, wake_threshold_ua: 5000,
    hold_ms: 500, pre_ms: 100, post_ms: 500, raw_buffer_hours: 0, auto_restore: true, uart_baud: null, max_duration_s: null,
  };

  let presets = $state<Preset[]>([]);
  let last = $state<Partial<SessionForm> | null>(null);
  /** Name of the session the last settings come from (null: the device's defaults, it has no sessions yet). */
  let lastName = $state<string | null>(null);
  let form = $state<SessionForm>({ ...DEFAULTS, name: autoName('detail') });
  let nameTouched = $state(false);
  let showForm = $state(false);
  let presetName = $state('');
  let busy = $state(false);
  let error = $state('');

  // Text fields for the form (converted on submit)
  let volts = $state('3.70');
  let thresholdMa = $state('5');
  let stopMin = $state('');

  // Battery emulation (instead of a fixed voltage)
  const BAT_DEFAULTS: BatteryForm = { mode: 'charge', chemistry: 'lipo', cells: 1, capacity_mah: 1000, speedup: 50, start_soc: 100, start_mv: 4200, sweep_min: 120, cutoff_mv: 3000, on_cutoff: 'protect', floor_mv: 2500 };
  let batOn = $state(false);
  let bat = $state<BatteryForm>({ ...BAT_DEFAULTS });
  let batStartV = $state('4.20');
  let batCutoffV = $state('3.00');
  let batFloorV = $state('2.50');
  const emulatedMah = $derived(bat.speedup > 0 ? bat.capacity_mah / bat.speedup : 0);
  let chems = $state<Chemistry[]>([]);
  const chem = $derived(chems.find((c) => c.id === bat.chemistry));
  const maxMv = $derived(live.status?.ppk2.max_voltage_mv ?? 4200);
  const fullMv = $derived(chem ? socToMv(chem, 100, bat.cells) : 0);

  /** A chemistry or cell count was picked: its cutoff/floor, and a start the voltage limit allows. */
  function applyChemistry() {
    const c = chem;
    if (!c) return;
    if (!c.cells.includes(bat.cells)) bat.cells = c.cells.includes(3) ? 3 : c.cells[0];
    batCutoffV = ((c.cutoff_mv * bat.cells) / 1000).toFixed(2);
    batFloorV = ((c.floor_mv * bat.cells) / 1000).toFixed(2);
    const full = socToMv(c, 100, bat.cells);
    bat.start_soc = full > maxMv ? maxStartSoc(c, bat.cells, maxMv) : 100;
    batStartV = (Math.min(full, maxMv) / 1000).toFixed(2);
  }

  // Curve preview: voltage over charge used (full on the left), with the cutoff and the voltage limit.
  const PW = 260;
  const PH = 64;
  const preview = $derived.by(() => {
    const c = chem;
    if (!c) return null;
    const lo = Math.min(c.floor_mv * bat.cells, ...c.curve.map((p) => p[1] * bat.cells)) - 50;
    const hi = Math.max(fullMv, maxMv) + 50;
    const y = (mv: number) => PH - ((mv - lo) / (hi - lo)) * PH;
    const x = (soc: number) => ((100 - soc) / 100) * PW;
    const d = c.curve.map(([s, v], i) => `${i ? 'L' : 'M'}${x(s).toFixed(1)},${y(v * bat.cells).toFixed(1)}`).join(' ');
    return { d, cut: y(Number(batCutoffV.replace(',', '.')) * 1000), limit: y(maxMv), start: x(bat.mode === 'charge' ? bat.start_soc : 100) };
  });

  function load(cfg: Partial<SessionForm>) {
    form = { ...DEFAULTS, ...cfg, name: form.name, notes: '' };
    volts = form.voltage_mv == null ? '' : (form.voltage_mv / 1000).toFixed(2);
    thresholdMa = String(form.wake_threshold_ua / 1000);
    stopMin = form.max_duration_s ? String(form.max_duration_s / 60) : '';
    batOn = !!cfg.battery;
    bat = { ...BAT_DEFAULTS, ...(cfg.battery ?? {}) };
    batStartV = (bat.start_mv / 1000).toFixed(2);
    batCutoffV = (bat.cutoff_mv / 1000).toFixed(2);
    batFloorV = (bat.floor_mv / 1000).toFixed(2);
    if (!nameTouched) form.name = autoName(form.kind);
  }

  $effect(() => {
    api.presets().then((p) => (presets = p)).catch(() => {});
    loadChemistries().then((c) => (chems = c)).catch(() => {});
    api.lastConfig().then((l) => {
      last = l?.config ?? null;
      lastName = l?.name ?? null;
      if (l) load(l.config);
    }).catch(() => {});
  });

  function summary(c: Partial<SessionForm>): string {
    const parts: string[] = [c.kind ?? 'detail'];
    const b = c.battery;
    if (b) {
      parts.push(b.mode === 'charge' ? `${batteryLabel(b, chems)} ${b.capacity_mah} mAh${b.speedup !== 1 ? ` at ${b.speedup}× speed` : ''}` : `battery sweep ${b.sweep_min} min`);
      parts.push(b.on_cutoff === 'hunt' ? `brown-out hunt to ${(b.floor_mv / 1000).toFixed(2)} V` : `off at ${(b.cutoff_mv / 1000).toFixed(2)} V`);
    } else {
      if (c.voltage_mv) parts.push(`${(c.voltage_mv / 1000).toFixed(2)} V`);
      if (c.dut_power !== false) parts.push('board on');
    }
    if (c.wake_threshold_ua) parts.push(`wake ≥ ${c.wake_threshold_ua / 1000} mA`);
    if (c.max_duration_s) parts.push(`stop after ${c.max_duration_s / 60} min`);
    return parts.join(' · ');
  }

  function formToRequest(): SessionForm {
    const v = parseFloat(volts.replace(',', '.'));
    const t = parseFloat(thresholdMa.replace(',', '.'));
    const m = parseFloat(stopMin.replace(',', '.'));
    return {
      ...form,
      name: form.name.trim() || autoName(form.kind),
      voltage_mv: Number.isFinite(v) ? Math.round(v * 1000) : null,
      wake_threshold_ua: Number.isFinite(t) ? t * 1000 : 5000,
      max_duration_s: Number.isFinite(m) && m > 0 ? m * 60 : null,
      battery: batOn ? { ...bat, start_mv: toMv(batStartV, 4200), cutoff_mv: toMv(batCutoffV, 3000), floor_mv: toMv(batFloorV, 2500) } : null,
    };
  }

  function toMv(v: string, fallback: number): number {
    const x = parseFloat(v.replace(',', '.'));
    return Number.isFinite(x) ? Math.round(x * 1000) : fallback;
  }

  async function start(cfg: SessionForm) {
    busy = true;
    error = '';
    try {
      await api.startSession(cfg);
      await live.pollStatus();
      onclose();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  const startWith = (c: Partial<SessionForm>) => start({ ...DEFAULTS, ...c, name: autoName(c.kind ?? 'detail'), notes: '' });

  async function savePreset() {
    const name = presetName.trim();
    if (!name) return;
    const { name: _n, notes: _x, ...config } = formToRequest();
    try {
      await api.savePreset(name, config);
      presets = await api.presets();
      presetName = '';
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function removePreset(p: Preset) {
    await api.deletePreset(p.id).catch(() => {});
    presets = await api.presets();
  }

  const estimate = $derived(
    form.kind === 'detail'
      ? 'Disk: about 0.7 GB per hour while the sensor is busy.'
      : 'Disk: 10 ms summaries (~0.1 GB/week) plus raw data around each wake; depends on how long the sensor is awake.',
  );
</script>

<div class="popover menu" use:outside={onclose} use:place role="dialog" aria-label="Start recording">
  <div class="top">
    <button class="btn record big" type="button" disabled={busy} onclick={() => startWith(last ?? DEFAULTS)}>
      <span class="dot" style="background:#fff"></span>Record
    </button>
    <div class="hint center">{last ? `${lastName ? 'Last settings' : 'Device defaults'}: ${summary(last)}` : `Defaults: ${summary(DEFAULTS)}`}</div>
    {#if devices.active}<div class="hint center">for <strong>{devices.active.name}</strong> · <a href="#/settings/devices">change</a></div>{/if}
  </div>

  {#if presets.length}
    <div class="sect">
      <div class="title">Presets</div>
      {#each presets as p (p.id)}
        <div class="preset">
          <button class="pick" type="button" disabled={busy} onclick={() => startWith(p.config)}>
            <span class="pname">{p.name}</span>
            <span class="hint">{summary(p.config)}</span>
          </button>
          <button class="btn icon ghost small" type="button" aria-label="Load {p.name} into the form" title="Edit in form" onclick={() => { load(p.config); showForm = true; }}>✎</button>
          <button class="btn icon ghost small" type="button" aria-label="Delete preset {p.name}" title="Delete" onclick={() => removePreset(p)}>×</button>
        </div>
      {/each}
    </div>
  {/if}

  <div class="sect">
    <button class="link" type="button" aria-expanded={showForm} onclick={() => (showForm = !showForm)}>{showForm ? '▾' : '▸'} New session with other settings</button>
    {#if showForm}
      <form class="form" onsubmit={(e) => { e.preventDefault(); start(formToRequest()); }}>
        <label class="field">Name
          <input class="input" type="text" bind:value={form.name} oninput={() => (nameTouched = true)} />
        </label>
        <div class="field">
          <span>Kind</span>
          <div class="kinds">
            <button type="button" class="kind" aria-pressed={form.kind === 'detail'} onclick={() => { form.kind = 'detail'; if (!nameTouched) form.name = autoName('detail'); }}>
              <strong>Detail</strong><span class="hint">Every sample kept. For a cycle or a short test.</span>
            </button>
            <button type="button" class="kind" aria-pressed={form.kind === 'soak'} onclick={() => { form.kind = 'soak'; if (!nameTouched) form.name = autoName('soak'); }}>
              <strong>Soak</strong><span class="hint">Summaries + raw around each wake. For days or weeks.</span>
            </button>
          </div>
        </div>
        <div class="field">
          <span>Supply</span>
          <div class="seg" role="group" aria-label="Supply">
            <button type="button" aria-pressed={!batOn} onclick={() => (batOn = false)}>Fixed voltage</button>
            <button type="button" aria-pressed={batOn} onclick={() => (batOn = true)}>Battery emulation</button>
          </div>
        </div>
        {#if batOn}
          <fieldset class="bat">
            <legend class="sr-only">Battery emulation</legend>
            <div class="two">
              <label class="field">Chemistry
                <select class="input" bind:value={bat.chemistry} onchange={applyChemistry}>
                  {#each chems as c (c.id)}<option value={c.id}>{c.label}</option>{/each}
                </select>
              </label>
              {#if chem && chem.cells.length > 1}
                <label class="field">Cells
                  <select class="input" bind:value={bat.cells} onchange={applyChemistry}>
                    {#each chem.cells as n (n)}<option value={n}>{n} in series</option>{/each}
                  </select>
                </label>
              {/if}
            </div>
            {#if chem && preview}
              <div class="curve">
                <svg viewBox="0 0 {PW} {PH}" preserveAspectRatio="none" role="img" aria-label="{chem.label} discharge curve: {(fullMv / 1000).toFixed(2)} V full">
                  {#if fullMv > maxMv}<line x1="0" x2={PW} y1={preview.limit} y2={preview.limit} class="limit" vector-effect="non-scaling-stroke" />{/if}
                  <line x1="0" x2={PW} y1={preview.cut} y2={preview.cut} class="cut" vector-effect="non-scaling-stroke" />
                  <path d={preview.d} class="v" vector-effect="non-scaling-stroke" />
                  {#if bat.mode === 'charge' && bat.start_soc < 100}<line x1={preview.start} x2={preview.start} y1="0" y2={PH} class="start" vector-effect="non-scaling-stroke" />{/if}
                </svg>
                <div class="hint">
                  {(fullMv / 1000).toFixed(2)} V full → {((chem.curve[0][1] * bat.cells) / 1000).toFixed(2)} V empty · {chem.note}.
                  {#if fullMv > maxMv}<span class="warnt">Full is above your {(maxMv / 1000).toFixed(2)} V limit (red), so it starts at {bat.start_soc} %.</span>{/if}
                </div>
              </div>
            {/if}
            <div class="seg" role="group" aria-label="Drain by">
              <button type="button" aria-pressed={bat.mode === 'charge'} onclick={() => (bat.mode = 'charge')}>Drain by charge used</button>
              <button type="button" aria-pressed={bat.mode === 'time'} onclick={() => (bat.mode = 'time')}>Time sweep</button>
            </div>
            {#if bat.mode === 'charge'}
              <div class="three">
                <label class="field">Battery<span class="input-unit"><input type="number" min="1" step="any" bind:value={bat.capacity_mah} /><span class="muted">mAh</span></span></label>
                <label class="field">Speed up<span class="input-unit"><input type="number" min="1" step="any" bind:value={bat.speedup} /><span class="muted">×</span></span></label>
                <label class="field">Start at<span class="input-unit"><input type="number" min="1" max="100" step="any" bind:value={bat.start_soc} /><span class="muted">%</span></span></label>
              </div>
              <div class="hint">Emptied by what the board really draws{bat.speedup !== 1 ? `, as if it held ${emulatedMah.toFixed(emulatedMah < 10 ? 2 : 1)} mAh (${bat.speedup}× faster)` : ''}.</div>
            {:else}
              <div class="two">
                <label class="field">From<span class="input-unit"><input type="text" inputmode="decimal" bind:value={batStartV} /><span class="muted">V</span></span></label>
                <label class="field">Down to the cutoff in<span class="input-unit"><input type="number" min="1" step="any" bind:value={bat.sweep_min} /><span class="muted">min</span></span></label>
              </div>
            {/if}
            <div class="two">
              <label class="field">Cutoff<span class="input-unit"><input type="text" inputmode="decimal" bind:value={batCutoffV} /><span class="muted">V</span></span></label>
              {#if bat.on_cutoff === 'hunt'}
                <label class="field">Keep going down to<span class="input-unit"><input type="text" inputmode="decimal" bind:value={batFloorV} /><span class="muted">V</span></span></label>
              {/if}
            </div>
            <div class="field">
              <span>At the cutoff</span>
              <div class="kinds">
                <button type="button" class="kind" aria-pressed={bat.on_cutoff === 'protect'} onclick={() => (bat.on_cutoff = 'protect')}>
                  <strong>Protection</strong><span class="hint">Board off and stop, like a battery protection circuit.</span>
                </button>
                <button type="button" class="kind" aria-pressed={bat.on_cutoff === 'hunt'} onclick={() => (bat.on_cutoff = 'hunt')}>
                  <strong>Brown-out hunt</strong><span class="hint">Keep lowering the voltage to find where the board gives up.</span>
                </button>
              </div>
            </div>
          </fieldset>
        {/if}
        <div class="two">
          {#if !batOn}
          <label class="field">Voltage
            <span class="input-unit"><input type="text" inputmode="decimal" bind:value={volts} /><span class="muted">V</span></span>
            <span class="hint">empty = don't touch the PPK2 output</span>
          </label>
          {/if}
          <label class="field">Wake threshold
            <span class="input-unit"><input type="text" inputmode="decimal" bind:value={thresholdMa} /><span class="muted">mA</span></span>
            <span class="hint">current above this = awake</span>
          </label>
        </div>
        <div class="two">
          <label class="field">Stop after
            <span class="input-unit"><input type="text" inputmode="decimal" bind:value={stopMin} placeholder="no limit" /><span class="muted">min</span></span>
          </label>
          {#if !batOn}<label class="check"><input type="checkbox" bind:checked={form.dut_power} />Power the board at start</label>{/if}
        </div>
        <details class="adv">
          <summary>Advanced: raw margins, rolling buffer, recovery</summary>
          <div class="two">
            <label class="field">Raw before wake<span class="input-unit"><input type="number" min="0" bind:value={form.pre_ms} /><span class="muted">ms</span></span></label>
            <label class="field">Raw after wake<span class="input-unit"><input type="number" min="0" bind:value={form.post_ms} /><span class="muted">ms</span></span></label>
            <label class="field">Wake ends after<span class="input-unit"><input type="number" min="10" bind:value={form.hold_ms} /><span class="muted">ms below</span></span></label>
            <label class="field">Rolling raw buffer<span class="input-unit"><input type="number" min="0" max="168" bind:value={form.raw_buffer_hours} /><span class="muted">h</span></span></label>
          </div>
          <label class="check"><input type="checkbox" bind:checked={form.auto_restore} />Restore voltage + board power after a dropout</label>
          <label class="field">Notes<input class="input" type="text" bind:value={form.notes} /></label>
        </details>
        <div class="hint">{estimate}</div>
        <div class="actions">
          <span class="save">
            <input class="input" type="text" placeholder="Preset name" bind:value={presetName} aria-label="Preset name" />
            <button class="btn ghost" type="button" disabled={!presetName.trim()} onclick={savePreset}>Save preset</button>
          </span>
          <button class="btn record" type="submit" disabled={busy}><span class="dot" style="background:#fff"></span>Start recording</button>
        </div>
      </form>
    {/if}
  </div>
  {#if error}<div class="error pad">{error}</div>{/if}
</div>

<style>
  .menu { right: 0; top: calc(100% + 8px); width: 460px; }
  .top { padding: 16px 16px 12px; display: flex; flex-direction: column; gap: 8px; }
  .big { min-height: 46px; font-size: 15px; }
  .center { text-align: center; }
  .sect { border-top: 1px solid var(--line); padding: 12px 16px; display: flex; flex-direction: column; gap: 6px; }
  .title { font-size: 12px; font-weight: 600; color: var(--muted); }
  .preset { display: flex; align-items: center; gap: 4px; }
  .pick { flex: 1 1 0; display: flex; flex-direction: column; align-items: flex-start; gap: 2px; min-height: 44px; padding: 6px 10px; border-radius: 8px; border: 1px solid var(--line); background: transparent; cursor: pointer; text-align: left; }
  .pick:hover { border-color: var(--faint); }
  .pname { font-weight: 600; }
  .link { background: none; border: 0; padding: 4px 0; color: var(--accent); cursor: pointer; text-align: left; font-weight: 500; }
  .form { display: flex; flex-direction: column; gap: 12px; margin-top: 6px; }
  .kinds { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .kind { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--line); background: transparent; cursor: pointer; text-align: left; font-weight: 400; }
  .kind[aria-pressed='true'] { border: 2px solid var(--accent); background: var(--sel); }
  .bat { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; margin: 0; display: flex; flex-direction: column; gap: 10px; }
  .curve { display: grid; grid-template-columns: 140px minmax(0, 1fr); gap: 10px; align-items: center; }
  .curve svg { width: 140px; height: 48px; background: var(--bg); border: 1px solid var(--line); border-radius: 6px; }
  .curve .v { fill: none; stroke: var(--accent); stroke-width: 1.5; }
  .curve .cut { stroke: var(--faint); stroke-dasharray: 3 3; stroke-width: 1; }
  .curve .limit { stroke: var(--rec); stroke-width: 1; }
  .curve .start { stroke: var(--warn); stroke-width: 1; }
  .warnt { color: var(--warn); }
  .three { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
  .two { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; align-items: end; }
  .adv { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; gap: 10px; }
  .adv summary { cursor: pointer; color: var(--muted); font-size: 13px; }
  .adv[open] > :not(summary) { margin-top: 10px; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: space-between; align-items: center; }
  .save { display: flex; gap: 6px; flex: 1 1 220px; }
  .save .input { min-height: 36px; }
  .pad { padding: 0 16px 14px; }
</style>
