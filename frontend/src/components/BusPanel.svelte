<script lang="ts">
  // The logic analyzer's decoded bus traffic on the Live page (a tab next to UART and USB). Messages are grouped
  // per chip (behind an I2C switch: per port), shown as register reads and sets, and repeats fold into one row
  // (lib/busfold.ts). Chip names belong to the board on the bench. Lanes under the chart come later.
  import type { Snippet } from 'svelte';
  import { bus } from '../lib/bus.svelte';
  import { ax, chipStats, every, fold, hx, leaf, leaves, pollEvery, summary, switchesOf, toItems, type Item, type Node } from '../lib/busfold';
  import { devices } from '../lib/devices.svelte';
  import { fmtClock } from '../lib/format';
  import { suggestions } from '../lib/i2cchips';
  import { live } from '../lib/live.svelte';
  import { outside } from '../lib/outside';
  import { place } from '../lib/place';

  let { head }: { head?: Snippet } = $props();

  $effect(() => bus.use());
  $effect(() => { void devices.activeId; void bus.loadChips(); });

  const MAX_ROWS = 2000, MAX_CHILDREN = 400;
  let folded = $state(true);
  let showSwitch = $state(false);
  let hidden = $state<string[]>([]);
  let open = $state<string[]>([]);
  let box = $state<HTMLDivElement>();
  let stick = $state(true);

  const health = $derived(bus.analyzer?.module?.health);
  const v = $derived(bus.analyzer?.values ?? {});
  const wiring = $derived([
    v.i2c_sda !== 'off' && v.i2c_scl !== 'off' ? `I2C on ${v.i2c_sda}/${v.i2c_scl}` : null,
    v.spi_clk !== 'off' ? `SPI on ${[v.spi_clk, v.spi_mosi, v.spi_miso, v.spi_cs].filter((p) => p !== 'off').join('/')}` : null,
  ].filter(Boolean).join(' · ') || 'no bus wired up');

  const msPerIdx = $derived((live.status?.buffer?.sample_period_s ?? 1e-5) * 1000);
  const switches = $derived(switchesOf(bus.events, bus.chips));
  const allItems = $derived(toItems(bus.events, switches, msPerIdx, showSwitch));
  const stats = $derived(chipStats(toItems(bus.events, switches, msPerIdx, false)));
  const shown = $derived(allItems.filter((it) => !hidden.includes(it.key)));
  const rows = $derived<Node[]>(folded ? fold(shown) : shown.map((it) => ({ it })));
  const total = $derived([...stats.values()].reduce((s, c) => s + c.n, 0));
  const mainChips = $derived([...stats].filter(([k]) => k.startsWith('m:') || k === 'spi').sort((a, b) => b[1].n - a[1].n));
  const portChips = $derived([...stats].filter(([k]) => !k.startsWith('m:') && k !== 'spi').sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true })));

  $effect(() => {
    void rows.length;
    if (stick && box) queueMicrotask(() => box && (box.scrollTop = box.scrollHeight));
  });
  function onscroll() {
    if (box) stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 12;
  }

  const clock = (idx: number) => { const w = bus.wallOf(idx); return w == null ? '' : fmtClock(w); };
  const ms = (x: number) => (x >= 1000 ? `${(x / 1000).toFixed(2)} s` : x >= 10 ? `${x.toFixed(0)} ms` : `${x.toFixed(1)} ms`);
  /** A port not known ('?:16'): the name the port chips at that address share, if they all have the same one. */
  const sharedName = (addr: number | null) => {
    const names = new Set(Object.entries(bus.chips).filter(([k]) => /^[0-7]:/.test(k) && Number(k.split(':')[1]) === addr).map(([, c]) => c.name));
    return names.size === 1 ? [...names][0] : undefined;
  };
  const nameOf = (key: string, addr: number | null) =>
    bus.chips[key]?.name || (key.startsWith('?:') ? sharedName(addr) : undefined) ||
    (key.startsWith('m:') && switches.has(addr ?? -1) ? 'I2C switch' : key === 'spi' ? 'SPI' : ax(addr ?? 0));
  const idOf = (n: Node) => `${'loop' in n ? 'L' : 'R'}${leaf(n).key}@${leaf(n).idx}`;
  const toggle = (list: string[], x: string) => (list.includes(x) ? list.filter((y) => y !== x) : [...list, x]);
  const dim = (it: Item) => (it.kind === 'probe' && !it.ok) || it.kind === 'switch';
  const pct = (n: number) => Math.round((100 * n) / Math.max(1, total));

  // naming
  let editing = $state<{ key: string; addr: number; port: string | null } | null>(null);
  let draft = $state('');
  let draftSwitch = $state(false);
  let saveError = $state('');
  function edit(key: string, addr: number, port: string | null) {
    editing = { key, addr, port };
    draft = bus.chips[key]?.name ?? '';
    draftSwitch = switches.has(addr) && key.startsWith('m:');
    saveError = '';
  }
  async function save(name = draft) {
    if (!editing) return;
    const isSwitchAddr = editing.key.startsWith('m:') && editing.addr >= 0x70 && editing.addr <= 0x77;
    try {
      await bus.setChip(editing.key, { name, ...(isSwitchAddr ? { switch: draftSwitch } : {}) });
      editing = null;
    } catch (e) {
      saveError = (e as Error).message;
    }
  }
</script>

{#snippet who(it: Item)}
  <span class="who" title="{it.addr != null ? ax(it.addr) : 'SPI'}{it.port === '?' ? ' behind the switch: the decode started after its port was opened' : it.port != null ? ` on switch port ${it.port}` : ''}">{nameOf(it.key, it.addr)}{#if it.port != null}<span class="pn">{it.port}</span>{/if}</span>
{/snippet}

{#snippet step(it: Item)}
  {#if it.kind === 'switch'}{it.text}
  {:else if it.kind === 'probe'}{it.ok ? 'answers' : 'not found'}
  {:else if it.kind === 'reg'}<span class="w">{hx(it.reg)}</span><span class="op">→</span>{hx(it.r)}
  {:else if it.kind === 'set'}<span class="w">{hx(it.reg)}</span><span class="op">←</span><span class="w">{hx(it.w)}</span>
  {:else if it.kind === 'cmd'}<span class="w">{hx(it.w)}</span><span class="op">→</span>{hx(it.r)}
  {:else if it.kind === 'write'}write <span class="w">{hx(it.w)}</span>
  {:else if it.kind === 'spi'}{#if it.mosi?.length}MOSI <span class="w">{hx(it.mosi)}</span>{/if}{#if it.mosi?.length && it.miso?.length}<span class="sep">·</span>{/if}{#if it.miso?.length}MISO {hx(it.miso)}{/if}
  {:else}read {hx(it.r)}{/if}
{/snippet}

{#snippet brief(n: Node)}
  {#if 'it' in n}{@render step(n.it)}
  {:else if 'loop' in n}({#each n.children.slice(-n.k).slice(0, 3) as c, i}{#if i}<span class="sep">·</span>{/if}{@render brief(c)}{/each}{n.k > 3 ? ` +${n.k - 3}` : ''}) ×{n.r}
  {:else}{summary(leaves(n)).join(' · ')}{/if}
{/snippet}

{#snippet line(it: Item)}
  <div class="row" class:dim={dim(it)}>
    <span class="t"><span class="tw"></span>{clock(it.idx)}</span>
    {@render who(it)}
    <span class="what" class:bad={it.nack && it.kind !== 'probe'}>{@render step(it)}{#if it.nack && it.kind !== 'probe'} · refused (NACK){/if}</span>
    <span class="side">{it.kind === 'cmd' && it.wait != null ? `+${ms(it.wait)}` : ''}</span>
  </div>
{/snippet}

{#snippet node(n: Node)}
  {#if 'it' in n}
    {@render line(n.it)}
  {:else}
    {@const its = leaves(n)}
    {@const first = its[0]}
    {@const id = idOf(n)}
    {@const isOpen = open.includes(id)}
    {@const span = its[its.length - 1].t - first.t}
    {@const poll = pollEvery(n)}
    <button type="button" class="row grp" class:dim={dim(first)} aria-expanded={isOpen} onclick={() => (open = toggle(open, id))}>
      <span class="t"><span class="tw">{isOpen ? '▾' : '▸'}</span>{clock(first.idx)}</span>
      {@render who(first)}
      <span class="what">
        {#if 'loop' in n && n.k <= 4}
          {#each n.children.slice(-n.k) as c, i}{#if i}<span class="sep">·</span>{/if}{@render brief(c)}{/each}
        {:else if 'loop' in n}
          <span class="w">{leaves({ run: true, children: n.children.slice(-n.k) }).length} steps each</span><span class="sep">·</span>{summary(leaves({ run: true, children: n.children.slice(-n.k) })).join(' · ')}
        {:else}
          <span class="w">{its.length} steps</span><span class="sep">·</span>{summary(its).join(' · ')}
        {/if}
      </span>
      <span class="side">
        {#if 'loop' in n}
          <span class="rep" class:hot={poll != null && every(n) === poll}>×{n.r}</span> every {ms(every(n))} · {ms(span)}
        {:else}
          {#if poll != null}<span class="rep hot">polls every {ms(poll)}</span> · {/if}{ms(span)}
        {/if}
      </span>
    </button>
    {#if isOpen}
      <div class="sub">
        {#each n.children.slice(0, MAX_CHILDREN) as c, i (i)}{@render node(c)}{/each}
        {#if n.children.length > MAX_CHILDREN}<div class="more">… {n.children.length - MAX_CHILDREN} more</div>{/if}
      </div>
    {/if}
  {/if}
{/snippet}

{#snippet chip(key: string, addr: number | null, port: string | null, n: number | null, ok: boolean)}
  <span class="anchor">
    <span class="bchip" class:off={hidden.includes(key)} class:missing={!ok}>
      <button type="button" class="tog" title="{hidden.includes(key) ? 'Show' : 'Hide'} this chip's messages" onclick={() => (hidden = toggle(hidden, key))}>
        {#if port != null}<span class="pn">{port}</span>{:else}<span class="dot" class:ok></span>{/if}
        <span class="nm">{nameOf(key, addr)}</span>
        {#if addr != null && port == null && bus.chips[key]?.name}<span class="a">{ax(addr)}</span>{/if}
        {#if n != null}<span class="n" class:hog={pct(n) >= 50}>{ok ? n : 'not found'}{ok && pct(n) >= 20 ? ` · ${pct(n)} %` : ''}</span>{/if}
      </button>
      {#if addr != null && port !== '?'}<button type="button" class="ed" title="Name this chip" aria-label="Name {ax(addr)}" onclick={() => edit(key, addr, port)}>✎</button>{/if}
    </span>
    {#if editing?.key === key}
      <form class="popover menu" use:place use:outside={() => (editing = null)} onsubmit={(e) => { e.preventDefault(); void save(); }}>
        <label class="field" for="chip-name">Name for {ax(editing.addr)} {editing.port != null ? `on switch port ${editing.port}` : 'on the main bus'}
          <!-- svelte-ignore a11y_autofocus -->
          <input id="chip-name" class="input" bind:value={draft} placeholder={ax(editing.addr)} maxlength="40" autofocus />
        </label>
        {#if suggestions(editing.addr).length}
          <div class="sugg">{#each suggestions(editing.addr) as s (s)}<button type="button" class="chip" onclick={() => save(s)}>{s}</button>{/each}</div>
        {/if}
        {#if editing.port == null && editing.addr >= 0x70 && editing.addr <= 0x77}
          <label class="check"><input type="checkbox" bind:checked={draftSwitch} /> I2C switch: its writes open ports</label>
        {/if}
        <p class="hint">Common chips at {ax(editing.addr)}. Kept with {devices.active?.name ?? 'the board on the bench'}.</p>
        {#if saveError}<p class="error">{saveError}</p>{/if}
        <div class="actions">
          <button class="btn small ghost" type="button" onclick={() => (editing = null)}>Cancel</button>
          <button class="btn small primary" type="submit" disabled={devices.activeId == null}>Save</button>
        </div>
      </form>
    {/if}
  </span>
{/snippet}

<section class="panel log" aria-label="Bus traffic">
  <div class="panel-head">
    <h2>Serial log</h2>
    {@render head?.()}
    <span class="spacer"></span>
    {#if bus.events.length}<button class="btn small ghost" type="button" onclick={() => { bus.clear(); open = []; }}>Clear</button>{/if}
    <button class="btn small" type="button" disabled={bus.busy || !health?.ok} onclick={() => bus.decode(5)}
      title={health?.ok ? 'Decode the last 5 seconds of the analyzer\'s buffer' : health?.detail ?? ''}>{bus.busy ? 'Decoding…' : 'Decode last 5 s'}</button>
  </div>

  <div class="note" class:bad={!health?.ok}>
    <span class="dot" class:ok={health?.ok}></span>
    <span>{health?.detail ?? 'Logic analyzer'} · {wiring} · <a href="#/settings/modules">settings</a></span>
  </div>
  {#if bus.error}<div class="note err">{bus.error}</div>{/if}
  {#if bus.last}
    <div class="note">Decoded {(bus.last.to - bus.last.from).toFixed(1)} s in {bus.last.decode_s} s: {bus.last.found} message{bus.last.found === 1 ? '' : 's'}{folded && bus.events.length ? ` → ${rows.length} row${rows.length === 1 ? '' : 's'}` : ''}{bus.last.truncated ? ' (only the first shown: a pin may be noisy, check the wiring)' : ''}</div>
  {/if}

  {#if !bus.events.length}
    <div class="closed">
      <h3>Bus traffic</h3>
      <p>I2C and SPI on the board's pins, decoded by the logic analyzer. <strong>Decode last 5 s</strong> looks at what just happened; during recordings each wake will be decoded by itself, and the transactions are kept with the session.</p>
    </div>
  {:else}
    <div class="devs">
      {#each mainChips as [key, s] (key)}
        {#if !(key.startsWith('m:') && switches.has(s.addr ?? -1))}{@render chip(key, s.addr, null, s.n, s.ok)}{/if}
      {/each}
      {#each [...switches] as a (a)}
        <span class="sw">
          {@render chip(`m:${a}`, a, null, null, true)}
          {#each portChips as [key, s] (key)}{@render chip(key, s.addr, s.port, s.n, s.ok)}{/each}
        </span>
      {/each}
    </div>
    <div class="opts">
      <label class="opt"><input type="checkbox" bind:checked={folded} /> Fold loops and runs</label>
      {#if switches.size}<label class="opt"><input type="checkbox" bind:checked={showSwitch} /> Show switch port writes</label>{/if}
      {#if devices.activeId == null}<span class="hint">Pick the board on the bench to name chips</span>{/if}
    </div>
    <div class="lines mono" bind:this={box} {onscroll}>
      {#each rows.slice(0, MAX_ROWS) as n, i (i)}{@render node(n)}{/each}
      {#if rows.length > MAX_ROWS}<div class="more">… and {rows.length - MAX_ROWS} more rows</div>{/if}
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

  .devs { flex: 0 0 auto; display: flex; flex-wrap: wrap; gap: 5px; align-items: center; padding: 7px 14px; border-bottom: 1px solid var(--line); }
  .anchor { position: relative; display: inline-flex; }
  .bchip { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 999px; background: var(--panel2); font-size: 12px; line-height: 1.3; }
  .bchip.missing { border-style: dashed; background: transparent; }
  .bchip.off { opacity: 0.45; }
  .bchip button { border: 0; background: transparent; color: var(--text); cursor: pointer; font: inherit; display: inline-flex; align-items: center; gap: 6px; }
  .bchip .tog { padding: 2px 3px 2px 7px; gap: 5px; border-radius: 999px 0 0 999px; }
  .bchip .ed { padding: 2px 7px 2px 3px; color: var(--faint); border-radius: 0 999px 999px 0; }
  .bchip .ed:hover { color: var(--accent); }
  .bchip .a, .bchip .n { font-family: var(--mono); font-size: 11px; color: var(--faint); }
  .bchip .n.hog { color: var(--warn); }
  .bchip.missing .nm, .bchip.missing .n { color: var(--muted); }
  .sw { display: inline-flex; flex-wrap: wrap; gap: 4px; align-items: center; padding: 2px; border: 1px solid var(--line); border-radius: 13px; }
  .sw > .anchor:first-child .bchip { background: transparent; border-color: transparent; }
  .pn { font-family: var(--mono); font-size: 10.5px; min-width: 16px; height: 16px; border-radius: 50%; display: inline-grid; place-items: center; background: var(--line); color: var(--text); }
  .menu { left: 0; top: calc(100% + 6px); width: 290px; padding: 14px; display: flex; flex-direction: column; gap: 10px; font-size: 13px; }
  .sugg { display: flex; flex-wrap: wrap; gap: 6px; }
  .sugg .chip { min-height: 26px; padding: 2px 9px; }
  .actions { display: flex; gap: 8px; justify-content: flex-end; }
  .menu p { margin: 0; }

  .opts { flex: 0 0 auto; display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; padding: 6px 14px; border-bottom: 1px solid var(--line); font-size: 12px; color: var(--muted); }
  .opt { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
  .opt input { margin: 0; accent-color: var(--accent); }

  .lines { flex: 1 1 0; min-height: 120px; overflow: auto; padding: 6px 0; font-size: 12px; line-height: 1.5; container-type: inline-size; }
  .row { display: grid; grid-template-columns: auto minmax(70px, 130px) minmax(0, 1fr) auto; gap: 10px; padding: 1px 14px; align-items: baseline; width: 100%; text-align: left; }
  button.row { border: 0; background: transparent; color: inherit; font: inherit; cursor: pointer; }
  button.row:hover { background: var(--panel2); }
  button.row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
  .row.dim, .row.dim .who { color: var(--faint); font-weight: 400; }
  .t { color: var(--faint); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .tw { display: inline-block; width: 12px; }
  .who { font-family: var(--font); font-size: 12.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .who .pn { margin-left: 5px; vertical-align: 1px; }
  .what { min-width: 0; color: var(--muted); overflow-wrap: anywhere; }
  .what.bad { color: var(--warn); }
  .w { color: var(--text); }
  .op { color: var(--faint); padding: 0 3px; }
  .sep { color: var(--line); padding: 0 6px; }
  .side { color: var(--faint); font-size: 11px; white-space: nowrap; font-variant-numeric: tabular-nums; text-align: right; }
  .rep { font-family: var(--font); font-size: 11px; font-weight: 600; padding: 0 6px; border-radius: 999px; background: var(--sel); color: var(--accent); }
  .rep.hot { background: var(--warn-bg); color: var(--warn); }
  .sub { margin-left: 20px; padding-left: 4px; border-left: 2px solid var(--line); max-height: 320px; overflow: auto; }
  .more { color: var(--faint); padding: 2px 14px; font-family: var(--font); }
  /* A narrow log column (next to the chart): what the row does goes on its own line under time and chip */
  @container (max-width: 620px) {
    .row { grid-template-columns: auto minmax(0, 1fr) auto; row-gap: 0; padding-top: 2px; padding-bottom: 2px; }
    .row .side { grid-column: 3; grid-row: 1; }
    .row .what { grid-column: 1 / -1; grid-row: 2; padding-left: 12px; }
  }
</style>
