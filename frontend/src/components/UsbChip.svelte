<script lang="ts">
  // Top-bar chip for the board's own USB port: asleep / closed / console open / menu waiting / upload waiting /
  // flashing, with a small menu to open or close the console.
  import { fmtClock } from '../lib/format';
  import { live } from '../lib/live.svelte';
  import { outside } from '../lib/outside';
  import { place } from '../lib/place';
  import { router } from '../lib/router.svelte';
  import { usb } from '../lib/usb.svelte';

  let open = $state(false);

  const u = $derived(live.status?.usb);
  const f = $derived(live.status?.flash);
  const flashing = $derived(!!f && ['preparing', 'waking', 'flashing'].includes(f.status));
  const asking = $derived(f?.status === 'waiting_choice');
  const consoleShown = $derived(router.route.name === 'live' && usb.tab === 'usb');

  const label = $derived.by(() => {
    if (!u) return '';
    if (flashing) return f!.status === 'flashing' ? `flashing ${Math.round(f!.progress)} %` : f!.status === 'waking' ? 'waking the board' : 'flashing';
    if (asking) return 'upload waiting';
    if (f?.status === 'waiting_recording') return 'upload queued';
    if (f?.status === 'waiting_wake') return 'upload · waiting for wake';
    if (u.wanted && u.menu && !consoleShown) return 'menu waiting';
    if (u.wanted) return u.present ? 'console open' : 'console open · asleep';
    return u.present ? 'closed' : 'asleep';
  });
  const queued = $derived(f?.status === 'waiting_recording' || f?.status === 'waiting_wake');
  const tone = $derived(flashing || queued ? 'flash' : asking || u?.wanted ? 'warn' : '');
  const pulse = $derived(flashing || asking || (!!u?.wanted && !!u.menu && !consoleShown));
  const last = $derived(f && f.finished ? f : null);

  function showConsole() {
    usb.setTab('usb');
    open = false;
    if (router.route.name !== 'live') location.hash = '#/live';
  }
</script>

{#if u}
  <div class="anchor">
    <button class="chip" class:warn={tone === 'warn'} class:flash={tone === 'flash'} type="button" aria-expanded={open} onclick={() => (open = !open)}
      title="The board's own USB port (console and flashing)">
      <span class="dot" class:warn={tone === 'warn'} class:flash={tone === 'flash'} class:pulse></span>USB · {label}
      {#if flashing && f?.status === 'flashing'}<span class="mini" aria-hidden="true"><i style:width="{f.progress}%"></i></span>{/if}
    </button>
    {#if open}
      <div class="popover menu" role="dialog" aria-label="Board USB" use:place use:outside={() => (open = false)}>
        <div class="hd"><span class="dot" class:warn={u.wanted}></span>Board USB · {u.wanted ? 'console open' : 'closed'}</div>
        <p class="sub">
          {#if u.wanted}
            The board sees USB and won't deep-sleep while the console is open.
          {:else}
            Closed: the board sees no USB and deep-sleeps as usual. The port disappears while it sleeps and comes back on each wake.
          {/if}
        </p>
        {#if usb.error}<p class="error">{usb.error}</p>{/if}
        <div class="acts">
          {#if u.wanted}
            <button class="btn small" type="button" disabled={usb.busy} onclick={() => { void usb.close(); open = false; }}>Close console</button>
            <button class="btn small ghost" type="button" onclick={showConsole}>Show console</button>
          {:else}
            <button class="btn small" type="button" disabled={usb.busy} onclick={() => { void usb.open(); showConsole(); }}>Open console</button>
          {/if}
        </div>
        {#if last}
          <div class="last">
            <span class="lbl">Last upload</span>
            <span class="mono small">{fmtClock(last.finished ?? last.created).slice(0, 5)} · {last.meta.project ?? '?'} {last.meta.version ?? ''} · {last.meta.git ?? ''} · from {last.meta.host ?? '?'}</span>
            <span class="small" class:ok={last.status === 'done'} class:bad={last.status === 'failed'}>
              {last.status === 'done' ? `Flashed in ${Math.round((last.finished ?? last.created) - last.created)} s` : last.status === 'failed' ? `Failed: ${last.error ?? ''}` : 'Cancelled'}
            </span>
          </div>
        {/if}
        <a href="#/settings/firmware" onclick={() => (open = false)}>Firmware upload settings →</a>
      </div>
    {/if}
  </div>
{/if}

<style>
  .anchor { position: relative; }
  .chip.warn { border-color: var(--warn); color: var(--warn); }
  .chip.flash { border-color: var(--flash); color: var(--flash); }
  .mini { width: 38px; height: 4px; border-radius: 2px; background: var(--panel2); overflow: hidden; }
  .mini i { display: block; height: 100%; background: var(--flash); }
  .menu { right: 0; top: calc(100% + 6px); width: 340px; padding: 14px; display: flex; flex-direction: column; gap: 12px; font-size: 13px; }
  .hd { display: flex; align-items: center; gap: 8px; font-weight: 600; }
  .sub { margin: 0; color: var(--muted); line-height: 1.5; }
  .acts { display: flex; gap: 6px; flex-wrap: wrap; }
  .last { border-top: 1px solid var(--line); padding-top: 10px; display: flex; flex-direction: column; gap: 4px; }
  .lbl { font-size: 12px; color: var(--faint); }
  .small { font-size: 12px; }
  .ok { color: var(--ok); }
  .bad { color: var(--rec); }
  .error { margin: 0; }
  a { text-decoration: none; }
</style>
