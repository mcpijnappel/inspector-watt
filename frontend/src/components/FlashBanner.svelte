<script lang="ts">
  // Under the top bar, on every page: a firmware upload that needs an answer (a recording is running), one that
  // is flashing, and how it ended. Uploads come from PlatformIO on another machine.
  import { api, type FlashChoice, type FlashJob } from '../lib/api';
  import { live } from '../lib/live.svelte';

  const ANSWER_S = 300; // the capture service cancels after this long without an answer
  const SHOW_DONE_S = 90;

  const f = $derived(live.status?.flash ?? null);
  const now = $derived(live.status?.time ?? Date.now() / 1000);
  let dismissed = $state<number | null>(null);
  let showLog = $state(false);
  let log = $state<string[]>([]);
  let logFor = -1;
  let busy = $state(false);
  let error = $state('');

  const active = (j: FlashJob) => ['queued', 'preparing', 'waking', 'flashing'].includes(j.status);
  const waiting = (j: FlashJob) => j.status === 'waiting_recording' || j.status === 'waiting_wake';
  const visible = $derived(!!f && f.id !== dismissed && (f.status === 'waiting_choice' || waiting(f) || active(f) || (f.finished != null && now - f.finished < SHOW_DONE_S)));
  const cycleIn = $derived(f?.wait_until ? Math.max(0, Math.round(f.wait_until - now)) : 0);
  const what = $derived(f ? [f.meta.project ?? 'firmware', f.meta.version].filter(Boolean).join(' ') : '');
  const left = $derived(f ? Math.min(ANSWER_S, Math.max(0, Math.round(ANSWER_S - (now - f.created)))) : 0);
  const took = $derived(f?.finished ? Math.round(f.finished - f.created) : null);
  const mb = $derived(f ? `${(f.bytes / 1e6).toFixed(1)} MB` : '');

  // The log: fetched while it's open, only the lines not seen yet.
  $effect(() => {
    if (!showLog || !f) return;
    if (logFor !== f.id) {
      logFor = f.id;
      log = [];
    }
    let stop = false;
    const id = f.id;
    const pull = async () => {
      try {
        const j = await api.flashJob(id, log.length);
        if (!stop && j.log?.length) log = [...log, ...j.log];
      } catch { /* shown by the status poll */ }
    };
    void pull();
    const t = setInterval(pull, 700);
    return () => {
      stop = true;
      clearInterval(t);
    };
  });

  let logBox = $state<HTMLPreElement>();
  $effect(() => {
    void log.length;
    if (logBox) logBox.scrollTop = logBox.scrollHeight;
  });

  const mmss = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

  async function answer(choice: FlashChoice) {
    if (f) await act(() => api.flashAnswer(f!.id, choice));
  }

  async function act(fn: () => Promise<unknown>) {
    busy = true;
    error = '';
    try {
      await fn();
      await live.pollStatus();
    } catch (e) {
      error = (e as Error).message; // e.g. answered in the terminal meanwhile
    } finally {
      busy = false;
    }
  }
</script>

{#if visible && f}
  {@const asking = f.status === 'waiting_choice'}
  <div class="banner" class:ask={asking} class:prog={active(f) || waiting(f)} class:done={f.status === 'done'} class:bad={f.status === 'failed'} class:cancel={f.status === 'cancelled'}
    role={asking ? 'alertdialog' : 'status'} aria-label="Firmware upload">
    <svg class="ic" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round" aria-hidden="true"><path d="M13 2L4 14h7l-1 8 9-12h-7z"></path></svg>
    <div class="what">
      {#if asking}
        <span class="t">Upload from {f.meta.host ?? 'another machine'}: {what} · <span class="mono">{f.meta.git ?? ''}</span></span>
        <span class="s">Recording <strong>{f.question?.session_name}</strong> is running. Flashing reboots the DUT.</span>
      {:else if f.status === 'waiting_recording'}
        <span class="t">Upload queued: {what} · <span class="mono">{f.meta.git ?? ''}</span></span>
        <span class="s">Flashes when recording <strong>{f.waiting_for}</strong> stops · from {f.meta.host ?? '?'} · a newer upload replaces it</span>
      {:else if f.status === 'waiting_wake'}
        <span class="t">Waiting for the DUT to wake up: {what} · <span class="mono">{f.meta.git ?? ''}</span></span>
        <span class="s">It's asleep; flashes on its next wake{f.wait_until ? `, or power-cycles it in ${mmss(cycleIn)}` : ' (no power-cycle)'} · from {f.meta.host ?? '?'}</span>
      {:else if active(f)}
        <span class="t">Flashing {what} · <span class="mono">{f.meta.git ?? ''}</span>{f.status === 'flashing' ? ` · ${Math.round(f.progress)} %` : f.status === 'waking' ? ' · waking the DUT' : ''}</span>
        <span class="s">From {f.meta.host ?? '?'} · {f.images.length} images, {mb}{f.power_cycled ? ' · it was asleep: power-cycled it first' : ''}{f.choice === 'stop' ? ' · recording stopped' : f.session_id ? ' · marked in the recording' : ''}</span>
      {:else if f.status === 'done'}
        <span class="t">Flashed {what} · <span class="mono">{f.meta.git ?? ''}</span>{took != null ? ` in ${took} s` : ''}</span>
        <span class="s">The DUT is booting the new firmware{f.session_id ? ' · marked in the recording' : ''}{f.choice === 'stop' ? ' · the recording was stopped first' : ''}.</span>
      {:else if f.status === 'failed'}
        <span class="t">Flashing {what} failed</span>
        <span class="s">{f.error}</span>
      {:else}
        <span class="t">Upload from {f.meta.host ?? 'another machine'} cancelled</span>
        <span class="s">{f.error ?? 'The terminal there says so too.'}</span>
      {/if}
    </div>
    <span class="spacer"></span>
    {#if asking}
      <div class="acts">
        <button class="btn small primary" type="button" disabled={busy} onclick={() => answer('keep')}>Keep recording, mark the flash</button>
        <button class="btn small" type="button" disabled={busy} onclick={() => answer('stop')}>Stop recording, then flash</button>
        <button class="btn small" type="button" disabled={busy} onclick={() => answer('after')}>Flash when it stops</button>
        <button class="btn small ghost" type="button" disabled={busy} onclick={() => answer('cancel')}>Cancel upload</button>
      </div>
      <span class="faint small full">Or answer in the terminal on {f.meta.host ?? 'that machine'}. Cancels by itself in {Math.floor(left / 60)}:{String(left % 60).padStart(2, '0')}.</span>
    {:else if waiting(f)}
      <div class="acts">
        {#if f.status === 'waiting_wake'}<button class="btn small" type="button" disabled={busy} onclick={() => act(() => api.flashNow(f.id))}>Power-cycle now</button>{/if}
        <button class="btn small ghost" type="button" disabled={busy} onclick={() => act(() => api.flashCancel(f.id))}>Cancel upload</button>
        <button class="btn small ghost" type="button" onclick={() => (showLog = !showLog)}>{showLog ? 'Hide log' : 'Show log'}</button>
      </div>
    {:else}
      {#if active(f) && f.status === 'flashing'}<span class="progress" aria-hidden="true"><i style:width="{f.progress}%"></i></span>{/if}
      <button class="btn small ghost" type="button" onclick={() => (showLog = !showLog)}>{showLog ? 'Hide log' : 'Show log'}</button>
      {#if !active(f)}<button class="btn small ghost icon" type="button" aria-label="Close" onclick={() => { dismissed = f.id; showLog = false; }}>×</button>{/if}
    {/if}
    {#if error}<span class="error full">{error}</span>{/if}
    {#if showLog && !asking}<pre class="log" bind:this={logBox}>{log.join('\n') || 'Loading…'}</pre>{/if}
  </div>
{/if}

<style>
  .banner { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; padding: 10px 24px; border-bottom: 1px solid var(--line); font-size: 13px; background: var(--panel2); }
  .banner.ask { background: var(--warn-bg); }
  .banner.ask .ic { color: var(--warn); }
  .banner.prog { background: var(--flash-bg); }
  .banner.prog .ic { color: var(--flash); }
  .banner.done { background: var(--ok-bg); }
  .banner.done .ic { color: var(--ok); }
  .banner.bad { background: var(--rec-bg); }
  .banner.bad .ic, .banner.bad .t { color: var(--rec); }
  .ic { flex: 0 0 auto; color: var(--muted); }
  .what { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .t { font-weight: 600; }
  .s { color: var(--muted); overflow-wrap: anywhere; }
  .acts { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .full { flex-basis: 100%; }
  .small { font-size: 12px; }
  .progress { width: 180px; height: 6px; border-radius: 3px; background: var(--panel); overflow: hidden; }
  .progress i { display: block; height: 100%; background: var(--flash); transition: width 0.3s; }
  .log { flex-basis: 100%; max-height: 240px; overflow: auto; margin: 0; padding: 8px 12px; border-radius: 8px; background: var(--panel); border: 1px solid var(--line);
    font-family: var(--mono); font-size: 11.5px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; color: var(--muted); }
  @media (max-width: 720px) {
    .banner { padding: 10px 12px; }
    .progress { width: 120px; }
  }
</style>
