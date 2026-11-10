<script lang="ts">
  // Firmware upload: the DUT's USB port, the upload token, how to set each build system up to flash through the
  // tracker (PlatformIO, PlatformIO over Arduino OTA, ESP-IDF, other), and the last uploads.
  import { api, type FlashJob } from '../../lib/api';
  import { copyText } from '../../lib/copy';
  import { fmtClock } from '../../lib/format';
  import { live } from '../../lib/live.svelte';

  type Way = 'pio' | 'ota' | 'idf' | 'other';
  const WAYS: { id: Way; label: string }[] = [
    { id: 'pio', label: 'PlatformIO' }, { id: 'ota', label: 'PlatformIO OTA' },
    { id: 'idf', label: 'ESP-IDF' }, { id: 'other', label: 'Other' },
  ];
  const WAY_KEY = 'pst.firmware.way';
  const VIA: Record<string, string> = {
    platformio: 'PlatformIO', 'arduino-ota': 'OTA', 'esp-idf': 'ESP-IDF', 'merged image': 'merged image',
    'app image': 'app image', files: 'files',
  };

  let token = $state<string | null>(null);
  let otaPort = $state<number | null>(null);
  let way = $state<Way>(savedWay());
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
extra_scripts = post:pst_flash.py
custom_tracker_url = ${url}
; custom_tracker_on_recording = ask   ; ask | keep | stop | after | cancel
; custom_tracker_when_asleep = wait    ; wait | always | cycle (default: the setting here)
; custom_tracker_full_flash = yes      ; write every image, also those already on the board`);
  const otaIni = $derived(`[env:xiao_c6_ota]
extends = env:seeed_xiao_esp32c6
upload_protocol = espota
upload_port = ${location.hostname}
upload_flags =
  --auth=\${sysenv.PST_FLASH_TOKEN}${otaPort && otaPort !== 3232 ? `\n  --port=${otaPort}` : ''}`);
  const idfCmd = $derived(`idf.py build
python pst_flash.py --url ${url} build/`);
  const otherCmd = $derived(`export PST_URL=${url}
python pst_flash.py build/esp32.esp32.XIAO_ESP32C6/   # arduino-cli compile --export-binaries
python pst_flash.py firmware.merged.bin              # esptool merge-bin, espflash
python pst_flash.py firmware.bin                     # an app image alone
python pst_flash.py 0x0 boot.bin 0x10000 app.bin`);
  // OTA can't go through a reverse proxy: it needs the tracker's own address on the network
  const proxied = $derived(location.protocol === 'https:' || !/^(\d+\.){3}\d+$|\.local$|^localhost$/.test(location.hostname));
  const masked = (t: string) => `${t.slice(0, 4)}${'•'.repeat(16)}${t.slice(-4)}`;
  const shown = $derived(token ? (showToken ? token : masked(token)) : '');
  const upload = 'pio run -e xiao_c6_tracker -t upload';
  const otaUpload = 'pio run -e xiao_c6_ota -t upload';

  function savedWay(): Way {
    try {
      const v = localStorage.getItem(WAY_KEY);
      return WAYS.some((w) => w.id === v) ? (v as Way) : 'pio';
    } catch {
      return 'pio';
    }
  }
  function pickWay(w: Way) {
    way = w;
    try {
      localStorage.setItem(WAY_KEY, w);
    } catch { /* private window: just not remembered */ }
  }

  async function load() {
    try {
      const m = await api.measurement();
      whenAsleep = m.flash_when_asleep ?? 'wait';
      wakeWait = Math.round(m.flash_wake_wait_s ?? 90);
      const f = await api.flashSettings();
      token = f.token;
      otaPort = f.ota_port;
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
    if (j.meta.source === 'arduino-ota' && j.status === 'done') parts.push('app only');
    if (j.status === 'done' && j.full_flash) parts.push('full flash');
    else if (j.status === 'done' && skipped) parts.push(`${skipped} of ${j.images.length} images already on the board`);
    return parts.join(' · ');
  }
</script>

<div class="section">
  <h2 class="title">Firmware upload</h2>
  <p class="intro muted">Flash the DUT from any build system on the network. The tracker writes it over the DUT's own USB port (through an adapter that blocks the 5 V) while the PPK2 keeps powering it. Agents (MCP) can't flash.</p>
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
      <p class="faint small">Power-cycling needs the voltage set by the tracker (switch the DUT on once). An upload can override it (<span class="mono">custom_tracker_when_asleep</span>, <span class="mono">--when-asleep</span>).</p>
    </div>
  </section>

  <section class="panel card">
    <div class="block first">
      <div class="label">Upload token</div>
      {#if token}
        <div class="tokenrow">
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
        <p class="faint small">Set it on the machine you upload from (Linux / macOS above, Windows PowerShell below). Every way below uses it; OTA uploads use it as the password. Anyone who can open this web UI can see it, like the MCP token.</p>
      {:else}
        <p class="muted small">No token yet.</p>
      {/if}
    </div>
  </section>

  <section class="panel card">
    <div class="block first">
      <div class="label">Set up your build</div>
      <div class="seg ways" role="group" aria-label="Build system">
        {#each WAYS as w (w.id)}
          <button type="button" aria-pressed={way === w.id} onclick={() => pickWay(w.id)}>{w.label}</button>
        {/each}
      </div>
    </div>

    {#if way !== 'ota'}
      <div class="block">
        <div class="label">1 · {way === 'pio' ? 'Put pst_flash.py next to platformio.ini' : 'Download pst_flash.py'}</div>
        <div class="acts"><a class="btn small primary" href="/downloads/pst_flash.py" download="pst_flash.py">Download pst_flash.py</a></div>
        <p class="faint small">One file, Python 3.8 or newer, nothing to install. It's also in this tracker's repository: <span class="mono">tools/pst_flash.py</span>.</p>
      </div>
    {/if}

    {#if way === 'pio'}
      <div class="block">
        <div class="label">2 · Add an environment to platformio.ini</div>
        <div class="cmd"><pre>{ini}</pre><button class="btn small" type="button" onclick={() => copy(ini, 'ini')}>{copied === 'ini' ? 'Copied' : 'Copy'}</button></div>
        <p class="faint small"><span class="mono">extends</span> names the environment you build now; change it for another board.</p>
      </div>
      <div class="block">
        <div class="label">3 · Upload</div>
        <div class="cmd"><code>{upload}</code><button class="btn small" type="button" onclick={() => copy(upload, 'up')}>{copied === 'up' ? 'Copied' : 'Copy'}</button></div>
        <p class="faint small">Writes the bootloader, partition table and app. While a recording runs, it asks what to do with it, in the terminal and here.</p>
      </div>
    {:else if way === 'ota'}
      <div class="block">
        <div class="label">1 · Add an environment to platformio.ini (no script needed)</div>
        <div class="cmd"><pre>{otaIni}</pre><button class="btn small" type="button" onclick={() => copy(otaIni, 'ota')}>{copied === 'ota' ? 'Copied' : 'Copy'}</button></div>
        {#if proxied}
          <p class="warnline small">This page comes through a proxy or a name: put the tracker's own address on the network in <span class="mono">upload_port</span>. OTA doesn't go through a proxy.</p>
        {/if}
      </div>
      <div class="block">
        <div class="label">2 · Upload</div>
        <div class="cmd"><code>{otaUpload}</code><button class="btn small" type="button" onclick={() => copy(otaUpload, 'otaup')}>{copied === 'otaup' ? 'Copied' : 'Copy'}</button></div>
        {#if otaPort === null}
          <p class="warnline small">The OTA endpoint is switched off on this tracker (<span class="mono">OTA_PORT=0</span>).</p>
        {/if}
        <p class="faint small">The tracker answers like an Arduino OTA device: only the app goes over (the tracker reads the partition table from the chip). A running recording is asked about here in the web UI, not in the terminal. Local network only (UDP {otaPort ?? 3232}), and your machine must accept the tracker's connection back.</p>
      </div>
    {:else if way === 'idf'}
      <div class="block">
        <div class="label">2 · Build and upload</div>
        <div class="cmd"><pre>{idfCmd}</pre><button class="btn small" type="button" onclick={() => copy(idfCmd, 'idf')}>{copied === 'idf' ? 'Copied' : 'Copy'}</button></div>
        <p class="faint small">Reads <span class="mono">build/flasher_args.json</span> (images, offsets, chip, flash settings), and the project name and version from <span class="mono">project_description.json</span>.</p>
      </div>
    {:else}
      <div class="block">
        <div class="label">2 · Upload what your build made</div>
        <div class="cmd"><pre>{otherCmd}</pre><button class="btn small" type="button" onclick={() => copy(otherCmd, 'other')}>{copied === 'other' ? 'Copied' : 'Copy'}</button></div>
        <p class="faint small">The chip comes from the image. A merged image is split along its partition table, so the board keeps its saved settings (NVS). <span class="mono">python pst_flash.py --help</span> lists the options.</p>
      </div>
    {/if}

    <p class="faint small">Images already on the board (same checksum) aren't written again: usually the bootloader and partition table, or the whole build when it's unchanged. Per upload: <span class="mono">custom_tracker_on_recording</span>, <span class="mono">_when_asleep</span>, <span class="mono">_full_flash</span> in platformio.ini, or <span class="mono">--on-recording</span>, <span class="mono">--when-asleep</span>, <span class="mono">--full-flash</span>.</p>
  </section>

  <h3 class="sub">Last uploads</h3>
  <section class="panel">
    {#if jobs.length}
      <div class="tablewrap">
        <table>
          <thead><tr><th>Time</th><th>From · via</th><th>Version</th><th>Git</th><th>Result</th><th>Took</th><th>Note</th></tr></thead>
          <tbody>
            {#each jobs as j (j.id)}
              <tr>
                <td class="mono">{fmtClock(j.created).slice(0, 5)}</td>
                <td>{j.meta.host ?? '?'}{j.meta.source ? ` · ${VIA[j.meta.source] ?? j.meta.source}` : ''}</td>
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
  .ways { align-self: flex-start; flex-wrap: wrap; }
  .warnline { color: var(--warn); }
  .small .mono { white-space: nowrap; }
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
