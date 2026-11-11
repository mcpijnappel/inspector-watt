<script lang="ts">
  // Firmware upload: the DUT's USB port and the last uploads first; then how to set each build system up to flash
  // through the tracker (PlatformIO, PlatformIO over Arduino OTA, ESP-IDF, other), starting with the upload token;
  // last, what happens when an upload comes in while the DUT sleeps.
  import { api, type FlashJob } from '../../lib/api';
  import { copyText } from '../../lib/copy';
  import { devPath, fmtClock } from '../../lib/format';
  import { live } from '../../lib/live.svelte';
  import SectionHead from './SectionHead.svelte';

  type Way = 'pio' | 'ota' | 'idf' | 'other';
  type Asleep = 'wait' | 'always' | 'cycle';
  const WAYS: { id: Way; label: string }[] = [
    { id: 'pio', label: 'PlatformIO' }, { id: 'ota', label: 'PlatformIO OTA' },
    { id: 'idf', label: 'ESP-IDF' }, { id: 'other', label: 'Other' },
  ];
  const WAY_KEY = 'pst.firmware.way';
  const OS_KEY = 'pst.firmware.os';
  const URL_KEY = 'pst.firmware.url';
  const VIA: Record<string, string> = {
    platformio: 'PlatformIO', 'arduino-ota': 'OTA', 'esp-idf': 'ESP-IDF', 'merged image': 'merged image',
    'app image': 'app image', files: 'files',
  };
  const ASLEEP_HINT: Record<Asleep, string> = {
    wait: "Its sleep cycle stays as it is. If it hasn't woken after the wait, the PPK2 power-cycles it.",
    always: 'However long it sleeps. Slow with long sleep intervals.',
    cycle: 'Fastest: the PPK2 switches it off and on, then the upload starts.',
  };

  let token = $state<string | null>(null);
  let otaPort = $state<number | null>(null);
  let way = $state<Way>(saved<Way>(WAY_KEY, ['pio', 'ota', 'idf', 'other'], 'pio'));
  let os = $state<'sh' | 'ps'>(saved<'sh' | 'ps'>(OS_KEY, ['sh', 'ps'], 'sh'));
  let jobs = $state<FlashJob[]>([]);
  let error = $state('');
  let busy = $state(false);
  let showToken = $state(false);
  let confirmNew = $state(false);
  let copied = $state('');
  let whenAsleep = $state<Asleep>('wait');
  let wakeWait = $state(90);
  let lanUrl = $state(saved<string>(URL_KEY, null, ''));

  const u = $derived(live.status?.usb);
  // Opened on the tracker itself: the build PC can't use this address, so ask for the one it can.
  const local = /^(localhost|127\.\d+\.\d+\.\d+|\[::1\])$/.test(location.hostname);
  const url = $derived(local ? lanUrl.trim().replace(/\/+$/, '') || `http://TRACKER-ADDRESS${location.port ? `:${location.port}` : ''}` : `${location.protocol}//${location.host}`);
  const host = $derived(url.replace(/^\w+:\/\//, '').replace(/[:/].*$/, ''));
  // OTA can't go through a reverse proxy: it needs the tracker's own address on the network
  const proxied = $derived(!local && (location.protocol === 'https:' || !/^(\d+\.){3}\d+$|\.local$/.test(location.hostname)));
  const masked = (t: string) => `${t.slice(0, 4)}${'•'.repeat(16)}${t.slice(-4)}`;
  const shown = $derived(token ? (showToken ? token : masked(token)) : '');
  const exportLine = (t: string) => (os === 'sh' ? `export PST_FLASH_TOKEN=${t}` : `$env:PST_FLASH_TOKEN="${t}"`);

  const ini = $derived(`[env:xiao_c6_tracker]
extends = env:seeed_xiao_esp32c6
upload_protocol = custom
extra_scripts = post:pst_flash.py
custom_tracker_url = ${url}`);
  const otaIni = $derived(`[env:xiao_c6_ota]
extends = env:seeed_xiao_esp32c6
upload_protocol = espota
upload_port = ${host}
upload_flags =
  --auth=\${sysenv.PST_FLASH_TOKEN}${otaPort && otaPort !== 3232 ? `\n  --port=${otaPort}` : ''}`);
  const idfCmd = $derived(`idf.py build
python pst_flash.py --url ${url} build/`);
  const otherCmd = $derived(`${os === 'sh' ? 'export PST_URL=' : '$env:PST_URL="'}${url}${os === 'sh' ? '' : '"'}
python pst_flash.py build/esp32.esp32.XIAO_ESP32C6/   # arduino-cli compile --export-binaries
python pst_flash.py firmware.merged.bin              # esptool merge-bin, espflash
python pst_flash.py firmware.bin                     # an app image alone
python pst_flash.py 0x0 boot.bin 0x10000 app.bin`);
  const upload = 'pio run -e xiao_c6_tracker -t upload';
  const otaUpload = 'pio run -e xiao_c6_ota -t upload';

  // Same words as the USB chip in the top bar.
  const usbState = $derived(!u ? 'no port configured' : u.held ? 'flashing' : u.wanted ? (u.present ? 'console open' : 'console open · asleep') : u.present ? 'closed' : 'asleep');
  const policy = $derived(whenAsleep === 'wait' ? `wait ${wakeWait} s, then power-cycle` : whenAsleep === 'always' ? 'wait for its next wake' : 'power-cycle right away');

  function saved<T extends string>(key: string, allowed: readonly T[] | null, fallback: T): T {
    try {
      const v = localStorage.getItem(key);
      return v != null && (!allowed || allowed.includes(v as T)) ? (v as T) : fallback;
    } catch {
      return fallback;
    }
  }
  function keep(key: string, v: string) {
    try {
      localStorage.setItem(key, v);
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

  async function saveAsleep(mode: Asleep, seconds: number) {
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
  function toAsleep() {
    document.getElementById('fw-asleep')?.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' });
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

{#snippet copyBtn(text: string, what: string)}
  <button class="btn small" type="button" onclick={() => copy(text, what)}>{copied === what ? 'Copied' : 'Copy'}</button>
{/snippet}

<div class="s-section">
  <SectionHead title="Firmware upload" intro="Flash the DUT from any build system on your network, over its USB port while the PPK2 powers it." />
  {#if error}<div class="s-banner">{error}</div>{/if}

  <section class="panel">
    <div class="s-card top">
      <div class="s-status">
        <span class="dot" class:ok={u?.present} class:warn={!!u?.wanted} class:flash={!!u?.held}></span>
        <strong class="state">USB port: {usbState}</strong>
        {#if u}<span class="muted small">{devPath(u.port)}{u.wanted ? ' · the DUT stays awake while it is open' : u.present ? ' · DUT awake, port kept closed' : ' · shows up on each wake, kept closed'}</span>{/if}
        <span class="spacer"></span>
        <span class="small muted">When asleep: {policy} · <button class="linkbtn" type="button" onclick={toAsleep}>Change</button></span>
      </div>
    </div>
    {#if jobs.length}
      <div class="s-tablewrap">
        <table class="s-table">
          <thead><tr><th>Time</th><th>From</th><th>Version</th><th>Result</th><th class="num">Took</th><th class="note">Note</th></tr></thead>
          <tbody>
            {#each jobs as j (j.id)}
              <tr>
                <td class="mono">{fmtClock(j.created).slice(0, 5)}</td>
                <td>{j.meta.host ?? '?'}{#if j.meta.source}<span class="sub">{VIA[j.meta.source] ?? j.meta.source}</span>{/if}</td>
                <td class="mono">{j.meta.version ?? '–'}{#if j.meta.git}<span class="sub">{j.meta.git}</span>{/if}</td>
                <td><span class="res"><span class="dot" class:ok={j.status === 'done'} class:bad={j.status === 'failed'} class:flash={!j.finished}></span>{result(j)}</span></td>
                <td class="mono num">{took(j)}</td>
                <td class="muted note">{note(j)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <div class="s-foot"><span>Last {jobs.length} since the capture service started</span></div>
    {:else}
      <p class="s-empty">No uploads since the capture service started.</p>
    {/if}
  </section>

  <h3 class="s-sub">Set up your build</h3>
  <section class="panel s-card">
    <div class="seg ways" role="group" aria-label="Build system">
      {#each WAYS as w (w.id)}
        <button type="button" aria-pressed={way === w.id} onclick={() => { way = w.id; keep(WAY_KEY, w.id); }}>{w.label}</button>
      {/each}
    </div>
    {#if local}
      <label class="addr small">
        <span>You opened this page on the tracker itself ({location.hostname}). The address your build PC uses:</span>
        <input class="input" type="text" placeholder="http://192.168.1.50{location.port ? `:${location.port}` : ''}" bind:value={lanUrl}
          oninput={() => keep(URL_KEY, lanUrl.trim())} aria-label="Tracker address on the network" />
      </label>
    {/if}

    <ol class="steps">
      <li>
        <div class="s-label">Set the upload token on the machine you build on</div>
        {#if token}
          <div class="tokenrow">
            <code class="s-token">{shown}</code>
            <button class="btn small ghost" type="button" onclick={() => (showToken = !showToken)}>{showToken ? 'Hide' : 'Show'}</button>
            <button class="btn small ghost" type="button" onclick={() => token && copy(token, 'token')}>{copied === 'token' ? 'Copied' : 'Copy'}</button>
            {#if confirmNew}
              <span class="confirm small">Build machines then need the new token.
                <button class="btn small" type="button" disabled={busy} onclick={newToken}>New token</button>
                <button class="btn small ghost" type="button" onclick={() => (confirmNew = false)}>Cancel</button>
              </span>
            {:else}
              <button class="btn small ghost" type="button" onclick={() => (confirmNew = true)}>New token…</button>
            {/if}
          </div>
          <div class="s-status">
            <div class="seg tiny" role="group" aria-label="Shell">
              <button type="button" aria-pressed={os === 'sh'} onclick={() => { os = 'sh'; keep(OS_KEY, 'sh'); }}>Linux / macOS</button>
              <button type="button" aria-pressed={os === 'ps'} onclick={() => { os = 'ps'; keep(OS_KEY, 'ps'); }}>Windows</button>
            </div>
            {#if way === 'ota'}<span class="small faint">OTA uses it as the password.</span>{/if}
          </div>
          <div class="s-cmd"><code>{exportLine(shown)}</code>{@render copyBtn(exportLine(token), 'export')}</div>
        {:else}
          <p class="muted small">No token yet.</p>
        {/if}
      </li>

      {#if way !== 'ota'}
        <li>
          <div class="s-label">{way === 'pio' ? 'Put pst_flash.py next to platformio.ini' : 'Download pst_flash.py'}</div>
          <div class="dl"><a class="btn small primary" href="/downloads/pst_flash.py" download="pst_flash.py">Download pst_flash.py</a>
            <span class="small faint">One file, Python 3.8+, nothing to install. Also in the repository: <span class="mono">tools/pst_flash.py</span></span></div>
        </li>
      {/if}

      {#if way === 'pio'}
        <li>
          <div class="s-label">Add an environment to platformio.ini</div>
          <div class="s-cmd"><pre>{ini}</pre>{@render copyBtn(ini, 'ini')}</div>
          <p class="faint small"><span class="mono">extends</span> names the environment you build now; change it for another board.</p>
        </li>
        <li>
          <div class="s-label">Upload</div>
          <div class="s-cmd"><code>{upload}</code>{@render copyBtn(upload, 'up')}</div>
          <p class="faint small">While a recording runs, it asks what to do with it, in the terminal and here.</p>
        </li>
      {:else if way === 'ota'}
        <li>
          <div class="s-label">Add an environment to platformio.ini (no script needed)</div>
          <div class="s-cmd"><pre>{otaIni}</pre>{@render copyBtn(otaIni, 'ota')}</div>
          {#if proxied}<p class="warnline small">OTA doesn't go through a proxy: put the tracker's own network address in <span class="mono">upload_port</span>.</p>{/if}
        </li>
        <li>
          <div class="s-label">Upload</div>
          <div class="s-cmd"><code>{otaUpload}</code>{@render copyBtn(otaUpload, 'otaup')}</div>
          {#if otaPort === null}<p class="warnline small">The OTA endpoint is switched off on this tracker (<span class="mono">OTA_PORT=0</span>).</p>{/if}
          <p class="faint small">Only the app goes over (UDP {otaPort ?? 3232}, local network only); your PC must accept the tracker's connection back. A running recording is asked about here, not in the terminal.</p>
        </li>
      {:else if way === 'idf'}
        <li>
          <div class="s-label">Build and upload</div>
          <div class="s-cmd"><pre>{idfCmd}</pre>{@render copyBtn(idfCmd, 'idf')}</div>
          <p class="faint small">Reads <span class="mono">build/flasher_args.json</span> for the images, offsets and chip.</p>
        </li>
      {:else}
        <li>
          <div class="s-label">Upload what your build made</div>
          <div class="s-cmd"><pre>{otherCmd}</pre>{@render copyBtn(otherCmd, 'other')}</div>
          <p class="faint small">A merged image is split along its partition table, so the board keeps its saved settings (NVS). <span class="mono">--help</span> lists the options.</p>
        </li>
      {/if}
    </ol>

    <details class="s-fold">
      <summary>Per-upload options</summary>
      <div class="inner">
        <div class="s-tablewrap">
          <table class="s-table opts">
            <thead><tr><th>Option</th><th>platformio.ini</th><th>pst_flash.py</th><th>Values</th></tr></thead>
            <tbody>
              <tr><td>While recording</td><td class="mono">custom_tracker_on_recording</td><td class="mono">--on-recording</td><td class="mono">ask · keep · stop · after · cancel</td></tr>
              <tr><td>While asleep</td><td class="mono">custom_tracker_when_asleep</td><td class="mono">--when-asleep</td><td class="mono">wait · always · cycle</td></tr>
              <tr><td>Write everything</td><td class="mono">custom_tracker_full_flash = yes</td><td class="mono">--full-flash</td><td class="muted">also images already on the board</td></tr>
            </tbody>
          </table>
        </div>
        <p class="faint small">Images with the same checksum as the board's aren't written again: usually the bootloader and partition table, or all of it for an unchanged build.</p>
      </div>
    </details>
  </section>

  <h3 class="s-sub" id="fw-asleep">When an upload comes in while the DUT sleeps</h3>
  <section class="panel s-card">
    <div class="choices" role="radiogroup" aria-label="When the DUT sleeps">
      <label class="choice"><input type="radio" name="asleep" value="wait" checked={whenAsleep === 'wait'} disabled={busy} onchange={() => saveAsleep('wait', wakeWait)} />
        Wait for its next wake, power-cycle after
        <span class="nowrap"><input class="secs" type="number" min="5" max="3600" step="5" bind:value={wakeWait} disabled={busy || whenAsleep !== 'wait'}
          onchange={() => saveAsleep('wait', wakeWait)} aria-label="Seconds to wait for a wake" /> s</span></label>
      <label class="choice"><input type="radio" name="asleep" value="always" checked={whenAsleep === 'always'} disabled={busy} onchange={() => saveAsleep('always', wakeWait)} />
        Wait for its next wake</label>
      <label class="choice"><input type="radio" name="asleep" value="cycle" checked={whenAsleep === 'cycle'} disabled={busy} onchange={() => saveAsleep('cycle', wakeWait)} />
        Power-cycle right away</label>
    </div>
    <p class="faint small">{ASLEEP_HINT[whenAsleep]} Power-cycling needs the voltage set by the tracker (switch the DUT on once); the banner can always power-cycle or cancel.</p>
  </section>
</div>

<style>
  .top { padding-bottom: 12px; }
  .state { font-weight: 500; }
  .ways { align-self: flex-start; flex-wrap: wrap; }
  .seg.tiny button { padding: 3px 9px; font-size: 12px; }
  .addr { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px; background: var(--warn-bg); }
  .addr .input { min-height: 30px; width: 240px; max-width: 100%; font-family: var(--mono); font-size: 12px; }
  .steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 16px; counter-reset: step; }
  .steps > li { position: relative; padding-left: 34px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
  .steps > li::before { counter-increment: step; content: counter(step); position: absolute; left: 0; top: -2px; width: 22px; height: 22px; border-radius: 50%; background: var(--panel2); color: var(--muted); display: grid; place-items: center; font-size: 12px; font-weight: 600; }
  .tokenrow { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .confirm { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px; }
  .dl { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
  .warnline { color: var(--warn); }
  .small .mono { white-space: nowrap; }
  .res { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
  td.note { min-width: 180px; }
  .opts td { white-space: nowrap; }
  .choices { display: flex; flex-direction: column; gap: 10px; }
  .choice { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 8px; font-size: 13px; cursor: pointer; }
  .choice input[type='radio'] { width: 16px; height: 16px; margin: 0 2px 0 0; accent-color: var(--accent); }
  .nowrap { white-space: nowrap; }
  .secs { width: 70px; min-height: 28px; padding: 0 6px; border-radius: 6px; border: 1px solid var(--line); background: var(--bg); color: var(--text); font-family: var(--mono); font-size: 12px; }
  @media (max-width: 640px) {
    th.note, td.note { display: none; }
    .steps > li { padding-left: 30px; }
  }
</style>
