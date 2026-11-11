<script lang="ts">
  // The logic analyzer's decoded bus traffic on the Live page (a tab next to UART and USB): the latest I2C and SPI
  // transactions, and "Decode the last 5 s". Lanes under the chart come later, for Live, Session and Compare.
  import type { Snippet } from 'svelte';
  import { bus, type BusEvent } from '../lib/bus.svelte';
  import { fmtClock } from '../lib/format';

  let { head }: { head?: Snippet } = $props();

  $effect(() => bus.use());

  let box = $state<HTMLDivElement>();
  let stick = $state(true);
  const health = $derived(bus.analyzer?.module?.health);
  const v = $derived(bus.analyzer?.values ?? {});
  const wiring = $derived([
    v.i2c_sda !== 'off' && v.i2c_scl !== 'off' ? `I2C on ${v.i2c_sda}/${v.i2c_scl}` : null,
    v.spi_clk !== 'off' ? `SPI on ${[v.spi_clk, v.spi_mosi, v.spi_miso, v.spi_cs].filter((p) => p !== 'off').join('/')}` : null,
  ].filter(Boolean).join(' · ') || 'no bus wired up');

  $effect(() => {
    void bus.events.length;
    if (stick && box) queueMicrotask(() => box && (box.scrollTop = box.scrollHeight));
  });
  function onscroll() {
    if (box) stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 12;
  }

  const hex = (b: number[] = []) => b.map((x) => x.toString(16).toUpperCase().padStart(2, '0')).join(' ');
  function detail(e: BusEvent): string {
    if (e.kind === 'spi') return [e.data.mosi?.length ? `MOSI ${hex(e.data.mosi)}` : '', e.data.miso?.length ? `MISO ${hex(e.data.miso)}` : ''].filter(Boolean).join(' · ');
    return (e.data.parts ?? []).map((p) => `${p.rw} ${hex(p.bytes) || '–'}`).join(' · ');
  }
  function time(e: BusEvent): string {
    const w = bus.wallOf(e.start_idx);
    return w == null ? '' : fmtClock(w);
  }
</script>

<section class="panel log" aria-label="Bus traffic">
  <div class="panel-head">
    <h2>Serial log</h2>
    {@render head?.()}
    <span class="spacer"></span>
    {#if bus.events.length}<button class="btn small ghost" type="button" onclick={() => bus.clear()}>Clear</button>{/if}
    <button class="btn small" type="button" disabled={bus.busy || !health?.ok} onclick={() => bus.decode(5)}
      title={health?.ok ? 'Decode the last 5 seconds of the analyzer\'s buffer' : health?.detail ?? ''}>{bus.busy ? 'Decoding…' : 'Decode last 5 s'}</button>
  </div>

  <div class="note" class:bad={!health?.ok}>
    <span class="dot" class:ok={health?.ok}></span>
    <span>{health?.detail ?? 'Logic analyzer'} · {wiring} · <a href="#/settings/modules">settings</a></span>
  </div>
  {#if bus.error}<div class="note err">{bus.error}</div>{/if}
  {#if bus.last}
    <div class="note">Decoded {(bus.last.to - bus.last.from).toFixed(1)} s in {bus.last.decode_s} s: {bus.last.found} transaction{bus.last.found === 1 ? '' : 's'}{bus.last.truncated ? ' (only the first shown: a pin may be noisy, check the wiring)' : ''}</div>
  {/if}

  {#if !bus.events.length}
    <div class="closed">
      <h3>Bus traffic</h3>
      <p>I2C and SPI on the board's pins, decoded by the logic analyzer. <strong>Decode last 5 s</strong> looks at what just happened; during recordings each wake will be decoded by itself, and the transactions are kept with the session.</p>
    </div>
  {:else}
    <div class="lines mono" bind:this={box} {onscroll}>
      {#each bus.events as e (e.seq)}
        <div class="row" class:nack={e.data.nack}>
          <span class="t">{time(e)}</span>
          <span class="k k-{e.kind}">{e.kind.toUpperCase()}</span>
          <span class="lbl">{e.data.label}</span>
          <span class="msg">{detail(e)}</span>
        </div>
      {/each}
    </div>
  {/if}
</section>

<style>
  .log { display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
  .note { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; padding: 7px 14px; border-bottom: 1px solid var(--line); font-size: 12.5px; color: var(--muted); }
  .note.bad { background: var(--warn-bg); color: var(--text); }
  .note.err { color: var(--rec); }
  .closed { flex: 1 1 0; display: flex; flex-direction: column; justify-content: center; gap: 10px; padding: 24px 22px; }
  .closed h3 { margin: 0; font-size: 15px; }
  .closed p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.55; max-width: 46ch; }
  .lines { flex: 1 1 0; min-height: 120px; overflow: auto; padding: 6px 0; font-size: 12px; line-height: 1.5; }
  .row { display: flex; gap: 10px; padding: 1px 14px; align-items: baseline; }
  .row.nack .lbl { color: var(--warn); }
  .t { color: var(--faint); flex: 0 0 auto; }
  .k { flex: 0 0 auto; font-size: 10.5px; font-weight: 600; padding: 0 5px; border-radius: 4px; background: var(--panel2); color: var(--muted); }
  .k-i2c { color: var(--flash); background: var(--flash-bg); }
  .k-spi { color: var(--accent); background: var(--sel); }
  .lbl { flex: 0 0 auto; font-weight: 600; }
  .msg { min-width: 0; color: var(--muted); overflow-wrap: anywhere; }
</style>
