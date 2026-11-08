<script lang="ts">
  // The DUT's USB console on the Live page (next to the UART log): open/close, its output, a key menu the firmware
  // prints (as buttons), and a line to send. Closed, the DUT sees no USB; open, it can't deep-sleep.
  import type { Snippet } from 'svelte';
  import { fmtClock, splitLevel } from '../lib/format';
  import { live } from '../lib/live.svelte';
  import { risky, usb } from '../lib/usb.svelte';

  let { head }: { head?: Snippet } = $props();

  $effect(() => usb.use());

  const u = $derived(live.status?.usb);
  const f = $derived(live.status?.flash);
  const flashing = $derived(!!f && ['preparing', 'waking', 'flashing'].includes(f.status));
  const menu = $derived(u?.wanted ? u.menu : null);
  let confirmKey = $state<string | null>(null);
  let text = $state('');
  let box = $state<HTMLDivElement>();
  let stick = $state(true);

  // A new menu (or none): forget a pending confirmation. Keyed on the menu's time, not the object, which is new
  // with every status update.
  const menuAt = $derived(menu?.at ?? null);
  $effect(() => {
    void menuAt;
    confirmKey = null;
  });

  $effect(() => {
    void usb.lines.length;
    if (stick && box) queueMicrotask(() => box && (box.scrollTop = box.scrollHeight));
  });

  function onscroll() {
    if (box) stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 12;
  }

  function press(key: string, label: string) {
    if (risky(label) && confirmKey !== key) {
      confirmKey = key;
      return;
    }
    confirmKey = null;
    void usb.key(key);
  }

  async function send(e: SubmitEvent) {
    e.preventDefault();
    const t = text.trim();
    if (!t) return;
    await usb.send(t);
    if (!usb.error) text = '';
  }
</script>

<section class="panel log" aria-label="USB console">
  <div class="panel-head">
    <h2>Serial log</h2>
    {@render head?.()}
    <span class="spacer"></span>
    {#if u?.wanted}<button class="btn small" type="button" disabled={usb.busy} onclick={() => usb.close()}>Close USB</button>{/if}
  </div>

  {#if !u}
    <div class="closed"><p class="muted">This capture service has no USB port configured.</p></div>
  {:else if !u.wanted && !usb.lines.length}
    <div class="closed">
      <h3>The DUT's USB console</h3>
      <p>The port stays closed, so the DUT sees no USB and deep-sleeps normally. Open it to catch the debug menu on the next boot and to send commands. While it's open the DUT <strong>won't deep-sleep</strong>, which changes what you measure.</p>
      <dl class="facts">
        <dt>Port</dt><dd>{u.port.replace('/host-dev/', '/dev/')} · {u.present ? 'there now' : 'not there (asleep, off or unplugged)'}</dd>
        <dt>Saved</dt><dd>not with recordings: it repeats the UART log</dd>
      </dl>
      {#if usb.error}<p class="error">{usb.error}</p>{/if}
      <div><button class="btn" type="button" disabled={usb.busy} onclick={() => usb.open()}>Open USB console</button></div>
    </div>
  {:else}
    {#if flashing}
      <div class="note flashing">Flashing: the console lets go of the port{u.wanted ? ' and opens again for the new firmware' : ''}.</div>
    {:else if u.wanted}
      <div class="note open"><span class="dot warn"></span><span><strong>Open:</strong> the DUT can't deep-sleep. It sleeps again ~30 s after you close.{u.present ? '' : ' Waiting for it to wake up.'}</span></div>
    {/if}
    <div class="lines mono" bind:this={box} {onscroll}>
      {#each usb.lines as l, i (i)}
        {#if l.note}
          <div class="row note-row"><span class="msg">{l.text}</span></div>
        {:else if l.sent}
          <div class="row sent"><span class="t">{fmtClock(l.wall)}</span><span class="lv">›</span><span class="msg">sent "{l.text}"</span></div>
        {:else}
          {@const r = splitLevel(l.text)}
          <div class="row"><span class="t">{fmtClock(l.wall)}</span><span class="lv lv-{r.level || 'x'}">{r.level || '·'}</span><span class="msg">{r.msg}</span></div>
        {/if}
      {/each}
    </div>
    {#if menu}
      <div class="menucard" role="group" aria-label={menu.title}>
        <div class="mhead"><span class="mtitle">{menu.title}</span><span class="prompt">The DUT is waiting for a key · {menu.prompt}</span></div>
        <div class="keys">
          {#each menu.options as o, i (o.key)}
            <button class="key" class:first={i === 0} class:risky={risky(o.label)} type="button" disabled={usb.busy} onclick={() => press(o.key, o.label)}>
              <kbd>{o.key}</kbd><span>{o.label}</span>
            </button>
          {/each}
        </div>
        {#if confirmKey}
          {@const o = menu.options.find((x) => x.key === confirmKey)}
          <div class="confirm" role="alert">
            <span><strong>{o?.label}?</strong> This changes what's stored on the DUT.</span>
            <span class="spacer"></span>
            <button class="btn small danger" type="button" onclick={() => o && press(o.key, o.label)}>Send {confirmKey}</button>
            <button class="btn small ghost" type="button" onclick={() => (confirmKey = null)}>Cancel</button>
          </div>
        {/if}
      </div>
    {/if}
    {#if usb.error}<div class="note err">{usb.error}</div>{/if}
    <form class="send" onsubmit={send}>
      <input type="text" bind:value={text} placeholder={u.wanted ? 'Send a line (Enter adds a newline)' : 'Open the console to send'} disabled={!u.wanted || !u.connected}
        autocomplete="off" aria-label="Line to send over USB" />
      <button class="btn small" type="submit" disabled={!u.wanted || !u.connected || usb.busy || !text.trim()}>Send</button>
    </form>
    <div class="foot">
      <span>USB console · not saved with recordings</span>
      {#if !u.wanted}<button class="btn small" type="button" disabled={usb.busy} onclick={() => usb.open()}>Open again</button>{/if}
      {#if !stick}<button type="button" class="btn small" onclick={() => { stick = true; if (box) box.scrollTop = box.scrollHeight; }}>Jump to newest</button>{/if}
    </div>
  {/if}
</section>

<style>
  .log { display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
  .closed { flex: 1 1 0; display: flex; flex-direction: column; justify-content: center; gap: 12px; padding: 24px 22px; }
  .closed h3 { margin: 0; font-size: 15px; }
  .closed p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.55; max-width: 46ch; }
  .facts { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 14px; font-size: 12.5px; margin: 0; }
  .facts dt { color: var(--faint); }
  .facts dd { margin: 0; font-family: var(--mono); font-size: 12px; overflow-wrap: anywhere; }
  .note { flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; padding: 8px 14px; border-bottom: 1px solid var(--line); font-size: 12.5px; }
  .note.open { background: var(--warn-bg); flex-wrap: nowrap; }
  .note.open > span:last-child { flex: 1 1 0; min-width: 0; }
  .note.flashing { background: var(--flash-bg); }
  .note.err { color: var(--rec); border-bottom: 0; border-top: 1px solid var(--line); }
  .lines { flex: 1 1 0; min-height: 120px; overflow: auto; padding: 6px 0; font-size: 12px; line-height: 1.5; }
  .row { display: flex; gap: 10px; padding: 1px 14px; }
  .t { color: var(--faint); flex: 0 0 auto; }
  .lv { flex: 0 0 12px; font-weight: 600; color: var(--faint); }
  .lv-I { color: var(--muted); }
  .lv-W { color: var(--warn); }
  .lv-E { color: var(--rec); }
  .msg { min-width: 0; white-space: pre-wrap; word-break: break-word; }
  .sent .lv, .sent .msg { color: var(--accent); }
  .note-row .msg { color: var(--faint); font-style: italic; font-family: var(--font); }
  /* Compact, in the flow under the log: the keys wrap side by side, so the log keeps most of the room. */
  .menucard { flex: 0 0 auto; border-top: 1px solid var(--warn); background: var(--warn-bg); padding: 8px 14px 10px; display: flex; flex-direction: column; gap: 6px; }
  .mhead { display: flex; align-items: baseline; gap: 4px 10px; flex-wrap: wrap; min-width: 0; }
  .mtitle { font-family: var(--mono); font-weight: 600; font-size: 12px; letter-spacing: 0.04em; }
  .prompt { font-size: 12px; color: var(--muted); }
  .keys { display: flex; flex-wrap: wrap; gap: 6px; }
  .key { display: inline-flex; align-items: center; gap: 8px; max-width: 100%; text-align: left; padding: 3px 10px 3px 4px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel); color: var(--text); cursor: pointer; font-size: 12.5px; min-height: 32px; }
  .key span { min-width: 0; overflow-wrap: anywhere; }
  .key:hover { border-color: var(--faint); }
  .key:disabled { opacity: 0.6; cursor: default; }
  .key kbd { flex: 0 0 auto; font-family: var(--mono); font-size: 12px; font-weight: 600; min-width: 22px; height: 22px; display: inline-grid; place-items: center; border-radius: 5px; border: 1px solid var(--line); background: var(--bg); }
  .key.first { border-color: var(--accent); }
  .key.risky { color: var(--rec); }
  .key.risky kbd { border-color: var(--rec); }
  .confirm { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px; background: var(--rec-bg); font-size: 12.5px; }
  .btn.danger { border-color: var(--rec); color: var(--rec); background: transparent; }
  .send { flex: 0 0 auto; display: flex; gap: 8px; padding: 8px 14px; border-top: 1px solid var(--line); }
  .send input { flex: 1 1 0; min-width: 0; min-height: 34px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); color: var(--text); font-family: var(--mono); font-size: 12px; }
  .foot { flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; min-height: 40px; padding: 4px 14px; border-top: 1px solid var(--line); font-size: 12px; color: var(--muted); }
  .foot span { flex: 1 1 auto; min-width: 0; }
</style>
