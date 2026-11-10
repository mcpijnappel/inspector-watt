<script lang="ts">
  // Firmware upload: the DUT's USB port, how to set PlatformIO up to flash through the tracker, the upload token,
  // and the last uploads.
  import { api, type FlashJob } from '../../lib/api';
  import { copyText } from '../../lib/copy';
  import { fmtClock } from '../../lib/format';
  import { live } from '../../lib/live.svelte';

  let token = $state<string | null>(null);
  let jobs = $state<FlashJob[]>([]);
  let error = $state('');
  let busy = $state(false);
  let showToken = $state(false);
  let confirmNew = $state(false);
  let copied = $state('');
  let whenAsleep = $state<'wait' | 'always' | 'cycle'>('wait');
  let wakeWait = $state(90);

  const u = $derived(live.status?.usb);
  const url = $derived(`${location.protocol}//${location.host}`); // https behind a reverse proxy
  const ini = $derived(`[env:xiao_c6_tracker]
extends = env:seeed_xiao_esp32c6
upload_protocol = custom
extra_scripts = post:tracker_upload.py
custom_tracker_url = ${url}
; custom_tracker_on_recording = ask   ; ask | keep | stop | after | cancel
; custom_tracker_when_asleep = wait    ; wait | always | cycle (default: the setting here)`);
  const masked = (t: string) => `${t.slice(0, 4)}${'•'.repeat(16)}${t.slice(-4)}`;
  const shown = $derived(token ? (showToken ? token : masked(token)) : '');
  const upload = 'pio run -e xiao_c6_tracker -t upload';

  async function load() {
    try {
      const m = await api.measurement();
      whenAsleep = m.flash_when_asleep ?? 'wait';
      wakeWait = Math.round(m.flash_wake_wait_s ?? 90);
      token = (await api.flashSettings()).token;
      jobs = await api.flashJobs();
      error = '';
    } catch (e) {
      error = (e as Error).message;
    }
  }
  $effect(() => {
    void load();
    const t = setInterval(async () => {
      try {
        jobs = await api.flashJobs();
      } catch { /* the banner above shows errors */ }
    }, 5000);
    return () => clearInterval(t);
  });

  async function newToken() {
    confirmNew = false;
    busy = true;
    try {
      token = (await api.newFlashToken()).token;
      showToken = true;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  async function saveAsleep(mode: 'wait' | 'always' | 'cycle', seconds: number) {
    busy = true;
    try {
      const m = await api.setMeasurement({ flash_when_asleep: mode, flash_wake_wait_s: Math.min(3600, Math.max(5, seconds || 90)) });
      whenAsleep = m.flash_when_asleep;
      wakeWait = Math.round(m.flash_wake_wait_s);
      error = '';
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  async function copy(text: string, what: string) {
    copied = (await copyText(text)) ? what : '';
    if (copied) setTimeout(() => (copied = copied === what ? '' : copied), 2000);
  }

  const took = (j: FlashJob) => (j.finished ? `${Math.round(j.finished - j.created)} s` : '…');
  function result(j: FlashJob): string {
    if (j.status === 'done') return 'flashed';
    if (j.status === 'waiting_choice') return 'waiting for an answer';
    if (j.status === 'waiting_recording') return 'queued: after the recording';
    if (j.status === 'waiting_wake') return j.wait_until ? 'waiting for the DUT to wake' : 'waiting for the DUT to wake (no power-cycle)';
    return j.status;
  }
  function note(j: FlashJob): string {
    if (j.status === 'failed' || j.status === 'cancelled') return j.error ?? '';
    const parts = [];
    if (j.power_cycled) parts.push('power-cycled first');
    if (j.choice === 'stop') parts.push('recording stopped');
    else if (j.session_id) parts.push(`marked in session ${j.session_id}`);
    const skipped = j.images.filter((im) => im.skipped).length;
    if (j.status === 'done' && j.full_flash) parts.push('full flash');
    else if (j.status === 'done' && skipped) parts.push(`${skipped} of ${j.images.length} images already on the board`);
    return parts.join(' · ');
  }
</script>

<div class="section">
  <h2 class="title">Firmware upload</h2>
  <p class="intro muted">Flash the DUT from PlatformIO on any machine on the network. The tracker runs esptool on the DUT's own USB port (through an adapter that blocks the 5 V) while the PPK2 keeps powering it. Agents (MCP) can't flash.</p>
  {#if error}<div class="banner bad">{error}</div>{/if}

  <section class="panel card">
    <div class="block first">
      <div class="status">
        <span class="dot" class:ok={u?.present} class:warn={!!u?.wanted}></span>
        <strong>{!u ? 'No USB port configured' : u.present ? 'DUT USB there' : 'DUT USB not there right now'}</strong>
        {#if u}<span class="muted small">{u.port.replace('/host-dev/', '/dev/')}{u.present ? '' : ' · normal while the DUT sleeps; it shows up on each wake'}</span>{/if}
      </div>
      <p class="faint small">Kept closed: the DUT sees no USB and sleeps as usual.</p>
    </div>

    <div class="block">
      <div class="label">When an upload comes in while the DUT sleeps</div>
      <label class="radio"><input type="radio" name="asleep" value="wait" checked={whenAsleep === 'wait'} disabled={busy}
          onchange={() => saveAsleep('wait', wakeWait)} />
        <span><span>Wait for its next wake, power-cycle it after
          <input class="secs" type="number" min="5" max="3600" step="5" bind:value={wakeWait} disabled={busy || whenAsleep !== 'wait'}
            onchange={() => saveAsleep('wait', wakeWait)} aria-label="Seconds to wait for a wake" /> s if it doesn't wake</span>
          <span class="faint small">Its sleep cycle stays as it is; the banner can power-cycle it sooner.</span></span>
      </label>
      <label class="radio"><input type="radio" name="asleep" value="always" checked={whenAsleep === 'always'} disabled={busy}
          onchange={() => saveAsleep('always', wakeWait)} />
        <span><span>Always wait for its next wake, never power-cycle</span>
          <span class="faint small">However long it sleeps; the banner can still power-cycle it or cancel.</span></span>
      </label>
      <label class="radio"><input type="radio" name="asleep" value="cycle" checked={whenAsleep === 'cycle'} disabled={busy}
          onchange={() => saveAsleep('cycle', wakeWait)} />
        <span><span>Power-cycle it right away with the PPK2</span> <span class="faint small">Fastest.</span></span>
      </label>
      <p class="faint small">Power-cycling needs the voltage set by the tracker (switch the DUT on once). <span class="mono">custom_tracker_when_asleep</span> in platformio.ini overrides this per upload.</p>
    </div>

    <div class="block">
      <div class="label">1 · Put tracker_upload.py in the firmware project</div>
      <div class="acts"><a class="btn small primary" href="/downloads/tracker_upload.py" download="tracker_upload.py">Download tracker_upload.py</a></div>
      <p class="faint small">Save it next to platformio.ini. It's also in this tracker's repository: <span class="mono">platformio/tracker_upload.py</span>.</p>
    </div>

    <div class="block">
      <div class="label">2 · Add an environment to platformio.ini</div>
      <div class="cmd"><pre>{ini}</pre><button class="btn small" type="button" onclick={() => copy(ini, 'ini')}>{copied === 'ini' ? 'Copied' : 'Copy'}</button></div>
      <p class="faint small"><span class="mono">extends</span> names the environment you build now; change it for another board.</p>
    </div>

    <div class="block">
      <div class="label">3 · Set the upload token on that machine</div>
      {#if token}
        <div class="tokenrow">
          <span class="muted small">Token</span>
          <code class="token">{shown}</code>
          <button class="btn small ghost" type="button" onclick={() => (showToken = !showToken)}>{showToken ? 'Hide' : 'Show'}</button>
          <button class="btn small ghost" type="button" onclick={() => token && copy(token, 'token')}>{copied === 'token' ? 'Copied' : 'Copy'}</button>
          {#if confirmNew}
            <span class="confirm small">Machines you upload from then need the new token.
              <button class="btn small" type="button" disabled={busy} onclick={newToken}>New token</button>
              <button class="btn small ghost" type="button" onclick={() => (confirmNew = false)}>Cancel</button>
            </span>
          {:else}
            <button class="btn small ghost" type="button" onclick={() => (confirmNew = true)}>New token…</button>
          {/if}
        </div>
        <div class="cmd"><code>export PST_FLASH_TOKEN={shown}</code><button class="btn small" type="button" onclick={() => token && copy(`export PST_FLASH_TOKEN=${token}`, 'export')}>{copied === 'export' ? 'Copied' : 'Copy'}</button></div>
        <div class="cmd"><code>$env:PST_FLASH_TOKEN="{shown}"</code><button class="btn small" type="button" onclick={() => token && copy(`$env:PST_FLASH_TOKEN="${token}"`, 'ps')}>{copied === 'ps' ? 'Copied' : 'Copy'}</button></div>
        <p class="faint small">Linux / macOS above, Windows PowerShell below. Anyone who can open this web UI can see the token, like the MCP token.</p>
      {:else}
        <p class="muted small">No token yet.</p>
      {/if}
    </div>

    <div class="block">
      <div class="label">4 · Upload</div>
      <div class="cmd"><code>{upload}</code><button class="btn small" type="button" onclick={() => copy(upload, 'up')}>{copied === 'up' ? 'Copied' : 'Copy'}</button></div>
      <p class="faint small">While a recording runs, the upload asks what to do with it, in the terminal and here in the web UI.</p>
      <p class="faint small">Images already on the board (same checksum) aren't written again: usually the bootloader and partition table, or the whole build when it's unchanged. <span class="mono">custom_tracker_full_flash = yes</span> writes everything.</p>
    </div>
  </section>

  <h3 class="sub">Last uploads</h3>
  <section class="panel">
    {#if jobs.length}
      <div class="tablewrap">
        <table>
          <thead><tr><th>Time</th><th>From</th><th>Version</th><th>Git</th><th>Result</th><th>Took</th><th>Note</th></tr></thead>
          <tbody>
            {#each jobs as j (j.id)}
              <tr>
                <td class="mono">{fmtClock(j.created).slice(0, 5)}</td>
                <td>{j.meta.host ?? '?'}</td>
                <td class="mono">{j.meta.version ?? '–'}</td>
                <td class="mono">{j.meta.git ?? '–'}</td>
                <td><span class="res"><span class="dot" class:ok={j.status === 'done'} class:bad={j.status === 'failed'} class:flash={!j.finished}></span>{result(j)}</span></td>
                <td class="mono">{took(j)}</td>
                <td class="muted note">{note(j)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <p class="empty muted">No uploads since the tracker started.</p>
    {/if}
  </section>
  <p class="faint small">Kept since the capture service last started (the last 10).</p>
</div>

<style>
  .section { display: flex; flex-direction: column; gap: 12px; }
  .title { margin: 0; font-size: 18px; }
  .sub { margin: 8px 0 0; font-size: 15px; }
  .intro { margin: 0; font-size: 13px; max-width: 80ch; line-height: 1.55; }
  .small { font-size: 12px; }
  p { margin: 0; }
  .banner { padding: 10px 14px; border-radius: 10px; }
  .banner.bad { border: 1px solid var(--rec); color: var(--rec); background: var(--rec-bg); }
  .card { padding: 14px 16px; display: flex; flex-direction: column; gap: 14px; }
  .block { display: flex; flex-direction: column; gap: 8px; padding-top: 12px; border-top: 1px solid var(--line); }
  .block.first { border-top: 0; padding-top: 0; }
  .status { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .label { font-weight: 600; font-size: 13px; }
  .cmd { display: flex; align-items: flex-start; gap: 8px; min-width: 0; }
  .cmd code, .cmd pre { flex: 1; min-width: 0; margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--panel2); font-family: var(--mono); font-size: 12px; overflow-x: auto; white-space: pre; }
  .acts { display: flex; flex-wrap: wrap; gap: 8px; }
  .acts a { text-decoration: none; }
  .radio { display: flex; align-items: flex-start; gap: 10px; font-size: 13px; cursor: pointer; }
  .radio input[type='radio'] { width: 16px; height: 16px; margin-top: 2px; accent-color: var(--accent); }
  .radio > span { display: flex; flex-direction: column; gap: 2px; }
  .secs { width: 70px; min-height: 28px; padding: 0 6px; border-radius: 6px; border: 1px solid var(--line); background: var(--bg); color: var(--text); font-family: var(--mono); font-size: 12px; }
  .tokenrow { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .token { font-family: var(--mono); font-size: 12px; padding: 4px 8px; border-radius: 6px; background: var(--panel2); overflow-wrap: anywhere; }
  .confirm { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  .tablewrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { text-align: left; padding: 8px 12px; border-top: 1px solid var(--line); white-space: nowrap; }
  th { font-weight: 500; color: var(--faint); font-size: 12px; border-top: 0; }
  td.mono { font-family: var(--mono); font-size: 12px; font-variant-numeric: tabular-nums; }
  td.note { white-space: normal; min-width: 180px; }
  .res { display: inline-flex; align-items: center; gap: 6px; }
  .empty { padding: 14px 16px; font-size: 13px; }
</style>
