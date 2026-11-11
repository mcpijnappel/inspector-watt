<script lang="ts">
  // The live page's controls in one place: Board power + voltage, live/pause, record, save.
  import { api } from '../lib/api';
  import { fmtClock, fmtDuration } from '../lib/format';
  import { devices } from '../lib/devices.svelte';
  import { live } from '../lib/live.svelte';
  import RecordMenu from './RecordMenu.svelte';
  import SaveMenu from './SaveMenu.svelte';

  let { saveOpen = $bindable(false) }: { saveOpen?: boolean } = $props();

  const VOLTS_KEY = 'pst.volts';
  const DEFAULT_MV = 3700;

  const st = $derived(live.status);
  const p = $derived(st?.ppk2);
  const session = $derived(st?.session?.active ? st.session.session : undefined);
  const on = $derived(p?.dut_power);
  const maxV = $derived((p?.max_voltage_mv ?? 4200) / 1000);

  let volts = $state(initialVolts());
  let voltsTouched = false;
  let busy = $state(false);
  let error = $state('');
  let recordOpen = $state(false);
  /** Asked before switching off while a session records. */
  let confirmOff = $state(false);
  /** Shown after switching off: live was paused for you. */
  let offNotice = $state(false);

  function initialVolts(): string {
    const s = live.status?.ppk2;
    if (s?.dut_power && s.voltage_set_by_us && s.voltage_mv) return (s.voltage_mv / 1000).toFixed(2);
    return rememberedVolts();
  }

  /** The device's default voltage; without a device the one you last switched on at (default 3.70 V). */
  function rememberedVolts(): string {
    if (devices.active) return (devices.active.voltage_mv / 1000).toFixed(2);
    try {
      const v = localStorage.getItem(VOLTS_KEY);
      if (v) return v;
    } catch { /* storage blocked */ }
    return (DEFAULT_MV / 1000).toFixed(2);
  }

  // While powered: follow the device's voltage (a session start, battery emulation, another browser).
  // While off: show the voltage "Turn on" will use, your remembered one, not whatever a battery
  // emulation left on the PPK2 (that could be 2.8 V). Typing in the field stops both until it's applied.
  $effect(() => {
    const powered = p?.dut_power;
    const mv = p?.voltage_set_by_us ? p.voltage_mv : null;
    if (voltsTouched) return;
    if (powered && mv) volts = (mv / 1000).toFixed(2);
    else if (!powered) volts = rememberedVolts(); // off or unknown: what Turn on will use
  });

  // Nothing comes in while the board is off or measuring is off: the live view is paused then, also when you
  // open the page or something else switches it off (idle timer, battery cutoff, another browser). It goes
  // back to live when power comes back, unless you paused it yourself.
  let autoPaused = false;
  let prevIdle: boolean | null = null;
  $effect(() => {
    if (!p) return;
    const idle = !p.connected || !p.streaming || p.dut_power === false;
    if (idle === prevIdle) return;
    prevIdle = idle;
    if (idle && !live.paused) {
      live.pause();
      autoPaused = true;
    } else if (!idle && autoPaused) {
      autoPaused = false;
      if (live.paused) live.resume();
    }
  });
  function backToLive() {
    offNotice = false;
    autoPaused = false;
    live.resume();
  }

  const wantMv = $derived.by(() => {
    const v = parseFloat(volts.replace(',', '.'));
    return Number.isFinite(v) ? Math.round(v * 1000) : null;
  });
  const voltsValid = $derived(wantMv != null && wantMv >= 800 && wantMv <= maxV * 1000);
  /** Powered, but the field says another voltage: offer "Set". */
  const voltsChanged = $derived(!!on && voltsValid && p?.voltage_mv !== wantMv);

  async function run(fn: () => Promise<unknown>) {
    busy = true;
    error = '';
    try {
      await fn();
      await live.pollStatus();
      return true;
    } catch (e) {
      error = (e as Error).message;
      return false;
    } finally {
      busy = false;
    }
  }

  function rememberVolts() {
    voltsTouched = false;
    try {
      localStorage.setItem(VOLTS_KEY, volts);
    } catch { /* storage blocked */ }
  }

  async function powerOn() {
    if (!voltsValid || wantMv == null) {
      error = `Voltage must be 0.80 – ${maxV.toFixed(2)} V`;
      return;
    }
    offNotice = false;
    const ok = await run(async () => {
      // Set the voltage first: the board never powers up at a voltage this app didn't set.
      if (!p?.voltage_set_by_us || p.voltage_mv !== wantMv || p.mode !== 'source') await api.setSource(wantMv);
      await api.setDut(true);
      if (!p?.streaming) await api.setStream(true);
    });
    if (ok) {
      rememberVolts();
      if (live.paused) live.resume();
    }
  }

  async function applyVolts() {
    if (wantMv == null || !voltsValid) return;
    if (await run(() => api.setSource(wantMv))) rememberVolts();
  }

  function askPowerOff() {
    if (session) confirmOff = true;
    else void powerOff(false);
  }

  async function powerOff(stopRecording: boolean) {
    confirmOff = false;
    const ok = await run(async () => {
      if (stopRecording) await api.stopSession(true);
      else await api.setDut(false);
    });
    if (ok) {
      live.pause();
      offNotice = true;
    }
  }

  async function stopMeasuring() {
    offNotice = false;
    await run(() => api.setStream(false));
  }

  async function stopRecording() {
    await run(() => api.stopSession());
  }

  // Why it's off, when the app (not you) switched it off.
  const offReason = $derived.by(() => {
    const c = p?.dut_changed;
    if (on !== false || !c || c.on) return '';
    if (c.by === 'idle') return `Switched off at ${fmtClock(c.at).slice(0, 5)}: nobody was watching and nothing recorded.`;
    if (c.by === 'session') return `Switched off at ${fmtClock(c.at).slice(0, 5)} when the session stopped.`;
    if (c.by === 'agent') return `Switched off at ${fmtClock(c.at).slice(0, 5)} by an agent (MCP).`;
    return '';
  });
  const idleMin = $derived(Math.round((st?.idle_power_off.after_s ?? 0) / 60));
</script>

<section class="strip panel" aria-label="Controls">
  <div class="group">
    {#if on}
      <button class="btn power on" type="button" disabled={busy || !p?.connected} onclick={askPowerOff} title="Switch the board off">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M12 3v9M6.3 6.3a8 8 0 1011.4 0"></path></svg>
        On · {((p?.voltage_mv ?? 0) / 1000).toFixed(2)} V
      </button>
    {:else}
      <button class="btn power" type="button" disabled={busy || !p?.connected || !voltsValid} onclick={powerOn} title="Set the voltage and power the board">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M12 3v9M6.3 6.3a8 8 0 1011.4 0"></path></svg>
        {on == null ? 'Power ?' : 'Off'} · Turn on
      </button>
      {#if on == null}
        <button class="btn ghost" type="button" disabled={busy || !p?.connected} onclick={askPowerOff} title="State unknown after a restart: make sure the board is off">Off</button>
      {/if}
    {/if}
    <form class="volts" onsubmit={(e) => { e.preventDefault(); if (on) applyVolts(); else powerOn(); }}>
      <span class="input-unit" class:bad={!voltsValid}>
        <input type="text" inputmode="decimal" bind:value={volts} oninput={() => (voltsTouched = true)} aria-label="Board voltage in volts" size="4"
          disabled={!!session?.battery} title={session?.battery ? 'Battery emulation sets the voltage while this session records' : undefined} />
        <span class="muted">V</span>
      </span>
      {#if voltsChanged}<button class="btn small" type="submit" disabled={busy}>Set</button>{/if}
    </form>
  </div>

  <span class="sep" aria-hidden="true"></span>

  <div class="group">
    {#if live.paused}
      <button class="btn" type="button" onclick={backToLive}><span class="dot ok"></span>Back to live</button>
    {:else}
      <button class="btn ghost" type="button" onclick={() => { autoPaused = false; live.pause(); }}>
        <span class="dot" class:ok={live.wsOpen}></span>{live.wsOpen ? 'Live · Pause' : 'Connecting…'}
      </button>
    {/if}
  </div>

  <span class="sep" aria-hidden="true"></span>

  <div class="group">
    {#if session}
      <span class="recpill" title="Recording session #{session.id}"><span class="dot rec"></span>REC {fmtDuration(session.duration_s)}</span>
      <button class="btn" type="button" disabled={busy} onclick={stopRecording}>Stop</button>
    {:else}
      <div class="anchor">
        <button class="btn record" type="button" aria-expanded={recordOpen} onclick={() => (recordOpen = !recordOpen)}>
          <span class="dot" style="background:#fff"></span>Record
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><path d="M6 9l6 6 6-6"></path></svg>
        </button>
        {#if recordOpen}<RecordMenu onclose={() => (recordOpen = false)} />{/if}
      </div>
    {/if}
    <div class="anchor">
      <button class="btn ghost" type="button" aria-expanded={saveOpen} onclick={() => (saveOpen = !saveOpen)}>Save…</button>
      {#if saveOpen}<SaveMenu onclose={() => (saveOpen = false)} />{/if}
    </div>
  </div>

  {#if confirmOff}
    <div class="note warn" role="alertdialog" aria-label="Power off while recording">
      <span>Session <strong>{session?.name}</strong> is recording. Stop it too?</span>
      <span class="spacer"></span>
      <button class="btn small" type="button" onclick={() => powerOff(true)}>Stop recording + power off</button>
      <button class="btn small ghost" type="button" onclick={() => powerOff(false)}>Power off, keep recording</button>
      <button class="btn small ghost" type="button" onclick={() => (confirmOff = false)}>Cancel</button>
    </div>
  {:else if offNotice && on === false}
    <div class="note" aria-live="polite">
      <span>Board off · live view paused.</span>
      <span class="spacer"></span>
      <button class="btn small ghost" type="button" disabled={busy} onclick={stopMeasuring}>Stop measuring too</button>
      <button class="btn small ghost" type="button" onclick={backToLive}>Back to live</button>
      <button class="btn small ghost icon" type="button" aria-label="Close" onclick={() => (offNotice = false)}>×</button>
    </div>
  {:else if offReason}
    <div class="note faint">{offReason}</div>
  {:else if on && idleMin && !session}
    <div class="note faint">Switches off by itself after {idleMin} min with no browser open and nothing recording.</div>
  {/if}
  {#if error}<div class="note error">{error}</div>{/if}
</section>

<style>
  .strip { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; padding: 10px 14px; }
  .group { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .sep { width: 1px; align-self: stretch; background: var(--line); }
  .anchor { position: relative; }
  .power { gap: 8px; font-weight: 600; min-width: 150px; justify-content: center; }
  .power.on { background: var(--ok); border-color: var(--ok); color: #fff; }
  .volts { display: flex; align-items: center; gap: 6px; }
  .volts .input-unit { width: 92px; min-height: 36px; }
  .input-unit.bad { border-color: var(--rec); }
  .recpill { display: inline-flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: 999px; background: var(--rec-bg); color: var(--rec); font-weight: 600; font-size: 13px; white-space: nowrap; }
  .note { flex-basis: 100%; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 13px; }
  .note.warn { padding: 8px 10px; border-radius: 8px; background: var(--rec-bg); }
  .note:not(.warn):not(.faint):not(.error) { padding: 6px 10px; border-radius: 8px; background: var(--panel2); }
  .spacer { flex: 1 1 auto; }
  @media (max-width: 720px) {
    .sep { display: none; }
    .power { min-width: 0; }
  }
</style>
