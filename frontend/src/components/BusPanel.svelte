<script lang="ts">
  // The logic analyzer's decoded bus traffic on the Live page, in a panel under the current chart (so the UART log
  // stays visible next to it). Messages are grouped per chip (behind an I2C switch: per port), shown as register
  // reads and sets, and repeats fold into one row (lib/busfold.ts). Chip names belong to the board on the bench.
  // The row under the mouse is shaded on the chart. Height and folded state are kept in the browser.
  // Without `source` it's the Live one (the analyzer's latest messages, "Decode last 30 s"); with it, it shows
  // messages from elsewhere (a session's stored ones) and names chips on that board.
  import { bus, type BusSource } from '../lib/bus.svelte';
  import { ax, chipStats, every, fold, hx, leaf, leaves, pollEvery, summary, switchesOf, toItems, type Item, type Node } from '../lib/busfold';
  import { namer } from '../lib/busnames';
  import { library, onBus } from '../lib/chips.svelte';
  import { devices } from '../lib/devices.svelte';
  import { fmtClock } from '../lib/format';
  import { live } from '../lib/live.svelte';
  import { outside } from '../lib/outside';
  import { place } from '../lib/place';

  let { source }: { source?: BusSource } = $props();
  const events = $derived(source ? source.events : bus.events);
  const chips = $derived(source ? source.chips : bus.chips);
  const setHover = (h: { a: number; b: number; label: string } | null) => (source ? source.onhover?.(h) : (bus.hover = h));

  const H_KEY = 'pst.bus.height', FOLD_KEY = 'pst.bus.folded';
  const MIN_H = 100, MAX_H = 900;
  const stored = (k: string) => { try { return localStorage.getItem(k); } catch { return null; } };
  const store = (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* private window */ } };
  let height = $state(Math.min(MAX_H, Math.max(MIN_H, Number(stored(H_KEY)) || 280)));
  let collapsed = $state(stored(FOLD_KEY) === '1');

  $effect(() => (shut || source ? undefined : bus.use()));
  $effect(() => { void devices.activeId; if (!source) void bus.loadChips(); });
  $effect(() => library.ensure());

  const MAX_ROWS = 2000, MAX_CHILDREN = 400;
  let folded = $state(true);
  let showSwitch = $state(false);
  let hidden = $state<string[]>([]);
  let open = $state<string[]>([]);
  let box = $state<HTMLDivElement>();
  let stick = $state(true);

  const health = $derived(bus.analyzer?.module?.health);
  // No analyzer to read (unplugged, starting): just the header bar with the reason, unless there's traffic to look at
  const idle = $derived(!source && !health?.ok && !events.length);
  const shut = $derived(collapsed || idle);
  const v = $derived(bus.analyzer?.values ?? {});
  const wiring = $derived([
    v.i2c_sda !== 'off' && v.i2c_scl !== 'off' ? `I2C on ${v.i2c_sda}/${v.i2c_scl}` : null,
    v.spi_clk !== 'off' ? `SPI on ${[v.spi_clk, v.spi_mosi, v.spi_miso, v.spi_cs].filter((p) => p !== 'off').join('/')}` : null,
  ].filter(Boolean).join(' · ') || 'no bus wired up');

  const msPerIdx = $derived(source ? source.msPerIdx : (live.status?.buffer?.sample_period_s ?? 1e-5) * 1000);
  const switches = $derived(switchesOf(events, chips, library.switchTypes));
  // the board's SPI chip: with a table from the library its transfers read as register reads and writes
  const spiFormat = $derived(onBus(library.get(chips.spi?.type), 'spi') as { read_bit: number } | undefined);
  const allItems = $derived(toItems(events, switches, msPerIdx, showSwitch, spiFormat));
  const stats = $derived(chipStats(toItems(events, switches, msPerIdx, false, spiFormat)));
  const shown = $derived(allItems.filter((it) => !hidden.includes(it.key)));
  const rows = $derived<Node[]>(folded ? fold(shown) : shown.map((it) => ({ it })));
  const total = $derived([...stats.values()].reduce((s, c) => s + c.n, 0));
  type Stat = { n: number; ok: boolean; addr: number | null; port: string | null; missing?: boolean };
  // The board's chips that weren't in this decode: shown as "not seen" (it may be asleep, or not answering).
  const expected = $derived<[string, Stat][]>(events.length ? Object.keys(chips)
    .filter((k) => !stats.has(k) && !(k.startsWith('m:') && switches.has(Number(k.split(':')[1]))))
    .map((k) => { const [p, a] = k.split(':'); return [k, { n: 0, ok: false, addr: k === 'spi' ? null : Number(a), port: p === 'm' || k === 'spi' ? null : p, missing: true }]; }) : []);
  const allStats = $derived<[string, Stat][]>([...stats, ...expected]);
  const mainChips = $derived(allStats.filter(([k]) => k.startsWith('m:') || k === 'spi').sort((a, b) => b[1].n - a[1].n));
  const portChips = $derived(allStats.filter(([k]) => !k.startsWith('m:') && k !== 'spi').sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true })));

  $effect(() => {
    void rows.length;
    if (stick && box) queueMicrotask(() => box && (box.scrollTop = box.scrollHeight));
  });
  function onscroll() {
    if (box) stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 12;
  }

  const clock = (idx: number) => { if (source) return source.clock(idx); const w = bus.wallOf(idx); return w == null ? '' : fmtClock(w); };
  const ms = (x: number) => (x >= 1000 ? `${(x / 1000).toFixed(2)} s` : x >= 10 ? `${x.toFixed(0)} ms` : `${x.toFixed(1)} ms`);
  const names = $derived(namer(chips, switches));
  const nameOf = (key: string, addr: number | null) => names.nameOf(key, addr);
  const known = (it: Item) => names.known(it);
  const nameIn = (it: Item) => names.nameIn(it);
  const idOf = (n: Node) => `${'loop' in n ? 'L' : 'R'}${leaf(n).key}@${leaf(n).idx}`;
  const toggle = (list: string[], x: string) => (list.includes(x) ? list.filter((y) => y !== x) : [...list, x]);
  const dim = (it: Item) => (it.kind === 'probe' && !it.ok) || it.kind === 'switch';
  const pct = (n: number) => Math.round((100 * n) / Math.max(1, total));

  // height: drag the bar on top (or arrow keys on it)
  function grab(e: PointerEvent) {
    const el = e.currentTarget as HTMLElement, y0 = e.clientY, h0 = height;
    el.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => { height = Math.min(MAX_H, Math.max(MIN_H, h0 - (ev.clientY - y0))); };
    const up = () => { el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up); store(H_KEY, String(Math.round(height))); };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
  }
  function nudge(e: KeyboardEvent) {
    const d = e.key === 'ArrowUp' ? 40 : e.key === 'ArrowDown' ? -40 : 0;
    if (!d) return;
    e.preventDefault();
    height = Math.min(MAX_H, Math.max(MIN_H, height + d));
    store(H_KEY, String(height));
  }
  // the row under the mouse: shaded on the chart
  let hovered: { a: number; b: number; label: string } | null = null;
  function hoverRow(e: Event) {
    const r = (e.target as HTMLElement).closest<HTMLElement>('[data-a]');
    if (!r) return;
    const a = Number(r.dataset.a), b = Number(r.dataset.b);
    if (hovered?.a !== a || hovered?.b !== b) setHover((hovered = { a, b, label: r.dataset.label ?? '' }));
  }

  // naming
  let editing = $state<{ key: string; addr: number | null; port: string | null } | null>(null);
  let draft = $state('');
  let draftType = $state('');
  let draftSwitch = $state(false);
  let saveError = $state('');
  function edit(key: string, addr: number | null, port: string | null) {
    editing = { key, addr, port };
    draft = chips[key]?.name ?? '';
    draftType = chips[key]?.type ?? '';
    draftSwitch = addr != null && switches.has(addr) && key.startsWith('m:');
    saveError = '';
  }
  async function save(name = draft) {
    if (!editing) return;
    const typed = library.get(draftType);
    // a switch by its library type needs no flag; otherwise the checkbox says it (0x70-0x77 only)
    const isSwitchAddr = editing.key.startsWith('m:') && editing.addr != null && editing.addr >= 0x70 && editing.addr <= 0x77 && !typed;
    try {
      await (source ? source.setChip ?? (async () => {}) : bus.setChip.bind(bus))(editing.key, { name, type: draftType || undefined, ...(isSwitchAddr ? { switch: draftSwitch } : {}) });
      editing = null;
    } catch (e) {
      saveError = (e as Error).message;
    }
  }
</script>

{#snippet who(it: Item)}
  <span class="who" title="{it.addr != null ? ax(it.addr) : 'SPI'}{it.port === '?' ? ' behind the switch: the decode started after its port was opened' : it.port != null ? ` on switch port ${it.port}` : ''}">{nameOf(it.key, it.addr)}{#if it.port != null}<span class="pn">{it.port}</span>{/if}</span>
{/snippet}

{#snippet named(code: number[] | undefined, name: string | undefined)}
  {#if name}<span class="rn">{name}</span><span class="rx">{hx(code)}</span>{:else}<span class="w">{hx(code)}</span>{/if}
{/snippet}

{#snippet step(it: Item)}
  {@const k = known(it)}
  {#if it.kind === 'switch'}{it.text}
  {:else if it.kind === 'probe'}{it.ok ? 'answers' : 'not found'}
  {:else if it.kind === 'reg'}{@render named(it.reg, k.name)}<span class="op">→</span>{#if k.values.length}<span class="val">{k.values.join(' · ')}</span><span class="rx">{hx(it.r)}</span>{:else}{hx(it.r)}{/if}
  {:else if it.kind === 'set'}{@render named(it.reg, k.name)}<span class="op">←</span><span class="w">{hx(it.w)}</span>
  {:else if it.kind === 'cmd'}{@render named(it.w, k.name)}<span class="op">→</span>{#if k.values.length}<span class="val">{k.values.join(' · ')}</span><span class="rx">{hx(it.r)}</span>{:else}{hx(it.r)}{/if}
  {:else if it.kind === 'write'}{#if k.name}<span class="rn">{k.name}</span><span class="rx">{hx(it.w)}</span>{:else}write <span class="w">{hx(it.w)}</span>{/if}
  {:else if it.kind === 'spi'}{#if it.mosi?.length}MOSI <span class="w">{hx(it.mosi)}</span>{/if}{#if it.mosi?.length && it.miso?.length}<span class="sep">·</span>{/if}{#if it.miso?.length}MISO {hx(it.miso)}{/if}
  {:else}read {hx(it.r)}{/if}
{/snippet}

{#snippet brief(n: Node)}
  {#if 'it' in n}{@render step(n.it)}
  {:else if 'loop' in n}({#each n.children.slice(-n.k).slice(0, 3) as c, i}{#if i}<span class="sep">·</span>{/if}{@render brief(c)}{/each}{n.k > 3 ? ` +${n.k - 3}` : ''}) ×{n.r}
  {:else}{summary(leaves(n), nameIn).join(' · ')}{/if}
{/snippet}

{#snippet line(it: Item)}
  <div class="row" class:dim={dim(it)} data-a={it.idx} data-b={it.idx} data-label={nameOf(it.key, it.addr)}>
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
    <button type="button" class="row grp" class:dim={dim(first)} aria-expanded={isOpen} onclick={() => (open = toggle(open, id))}
      data-a={first.idx} data-b={its[its.length - 1].idx} data-label={nameOf(first.key, first.addr)}>
      <span class="t"><span class="tw">{isOpen ? '▾' : '▸'}</span>{clock(first.idx)}</span>
      {@render who(first)}
      <span class="what">
        {#if 'loop' in n && n.k <= 4}
          {#each n.children.slice(-n.k) as c, i}{#if i}<span class="sep">·</span>{/if}{@render brief(c)}{/each}
        {:else if 'loop' in n}
          <span class="w">{leaves({ run: true, children: n.children.slice(-n.k) }).length} steps each</span><span class="sep">·</span>{summary(leaves({ run: true, children: n.children.slice(-n.k) }), nameIn).join(' · ')}
        {:else}
          <span class="w">{its.length} steps</span><span class="sep">·</span>{summary(its, nameIn).join(' · ')}
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
        {#if addr != null && port == null && chips[key]?.name}<span class="a">{ax(addr)}</span>{/if}
        {#if n != null}<span class="n" class:hog={pct(n) >= 50}>{n === 0 && !ok ? 'not seen' : ok ? n : 'not found'}{ok && pct(n) >= 20 ? ` · ${pct(n)} %` : ''}</span>{/if}
      </button>
      {#if (addr != null || key === 'spi') && port !== '?' && (!source || source.setChip)}<button type="button" class="ed" title="Name this chip" aria-label="Name {addr != null ? ax(addr) : 'the SPI chip'}" onclick={() => edit(key, addr, port)}>✎</button>{/if}
    </span>
    {#if editing?.key === key}
      {@const a = editing.addr}
      {@const fits = a == null ? library.spi : library.at(a)}
      <form class="popover menu" use:place use:outside={() => (editing = null)} onsubmit={(e) => { e.preventDefault(); void save(); }}>
        <p class="ttl">{a == null ? 'SPI chip on the analyzer\'s chip select' : `${ax(a)} ${editing.port != null ? `on switch port ${editing.port}` : 'on the main bus'}`}</p>
        <label class="field" for="chip-type">Type, from the sensor library
          <select id="chip-type" class="input" bind:value={draftType}>
            <option value="">–</option>
            {#if fits.length}<optgroup label={a == null ? 'SPI chips' : `Answers at ${ax(a)}`}>{#each fits as c (c.id)}<option value={c.name}>{c.name}{c.body.kind ? ` · ${c.body.kind}` : ''}</option>{/each}</optgroup>{/if}
            <optgroup label="All chips">{#each library.list.filter((c) => !fits.includes(c)) as c (c.id)}<option value={c.name}>{c.name}</option>{/each}</optgroup>
          </select>
        </label>
        <label class="field" for="chip-name">Name on this board
          <!-- svelte-ignore a11y_autofocus -->
          <input id="chip-name" class="input" bind:value={draft} placeholder={draftType || (a == null ? 'SPI' : ax(a))} maxlength="40" autofocus />
        </label>
        {#if editing.port == null && a != null && a >= 0x70 && a <= 0x77 && !library.get(draftType)}
          <label class="check"><input type="checkbox" bind:checked={draftSwitch} /> I2C switch: its writes open ports</label>
        {/if}
        <p class="hint">Kept with {source ? source.board ?? 'this board' : devices.active?.name ?? 'the board on the bench'} (Settings → Devices & sensors). Tables: <a href="#/settings/library">Sensor library</a>.</p>
        {#if saveError}<p class="error">{saveError}</p>{/if}
        <div class="actions">
          <button class="btn small ghost" type="button" onclick={() => (editing = null)}>Cancel</button>
          <button class="btn small primary" type="submit" disabled={!source && devices.activeId == null}>Save</button>
        </div>
      </form>
    {/if}
  </span>
{/snippet}

<section class="panel bus" class:collapsed={shut} aria-label="Bus traffic">
  {#if !shut}
    <button class="grip" type="button" aria-label="Height of the bus panel: drag, or arrow keys up and down" title="Drag to change the height"
      onpointerdown={grab} onkeydown={nudge}></button>
  {/if}
  <div class="panel-head">
    {#if !idle}<button class="btn small ghost icon" type="button" aria-expanded={!collapsed} aria-label={collapsed ? 'Open the bus panel' : 'Fold the bus panel'}
      title={collapsed ? 'Open' : 'Fold to this bar'} onclick={() => { collapsed = !collapsed; store(FOLD_KEY, collapsed ? '1' : '0'); }}>{collapsed ? '▸' : '▾'}</button>{/if}
    <h2>Bus</h2>
    {#if source}
      <span class="state">{source.status}</span>
    {:else}
      <span class="state" title={health?.detail ?? ''}><span class="dot" class:ok={health?.ok} class:warn={!health?.ok}></span>{health?.ok ? wiring : health?.detail ?? 'starting'} · <a href="#/settings/modules">settings</a></span>
    {/if}
    {#if collapsed && events.length}<span class="faint small">{events.length} messages</span>{/if}
    <span class="spacer"></span>
    {#if !source && !collapsed && events.length}<button class="btn small ghost" type="button" onclick={() => { bus.clear(); open = []; }}>Clear</button>{/if}
    {#if !source}<label class="opt live" title={health?.ok ? 'Decode the newest second, every second, while this page is open (about 1-2 s behind). Off: only when you press Decode.' : health?.detail ?? ''}>
      <input type="checkbox" checked={bus.live} disabled={!health?.ok} onchange={(e) => { bus.setLive((e.target as HTMLInputElement).checked); if (bus.live) collapsed = false; }} /> Live</label>{/if}
    {#if !source}<div class="decode">
      <button class="btn small" type="button" disabled={bus.busy || !health?.ok} onclick={() => { collapsed = false; void bus.decode(30); }}
        title={health?.ok ? 'Decode everything the analyzer kept: the last 30 seconds' : health?.detail ?? ''}>{bus.busy ? 'Decoding…' : 'Decode last 30 s'}</button>
      <button class="btn small ghost" type="button" disabled={bus.busy || !health?.ok} onclick={() => { collapsed = false; void bus.decode(5); }}
        title="Decode only the last 5 seconds (quicker)">5 s</button>
    </div>{/if}
  </div>

  {#if !shut}
    {#if !source && bus.error}<div class="note err">{bus.error}</div>{/if}

    {#if !events.length}
      <div class="closed" style:height="{height}px">
        {#if source}<p>{source.empty}</p>
        {:else}<p>I2C and SPI on the board's pins, decoded by the logic analyzer. <strong>Decode last 30 s</strong> looks at what just happened, <strong>Live</strong> keeps decoding while this page is open; during a recording each wake is decoded by itself (Settings → Modules: Decode in recordings), and the messages are kept with the session.</p>{/if}
      </div>
    {:else}
      <div class="bar">
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
          {#if source?.summary}<span>{source.summary}{folded ? ` → ${rows.length} rows` : ''}</span>{/if}
          {#if !source && bus.last}<span>{(bus.last.to - bus.last.from).toFixed(0)} s: {bus.last.found} messages{folded ? ` → ${rows.length} rows` : ''}{bus.last.truncated ? ' (cut off: a pin may be noisy, check the wiring)' : ''}</span>{/if}
          <label class="opt"><input type="checkbox" bind:checked={folded} /> Fold loops and runs</label>
          {#if switches.size}<label class="opt"><input type="checkbox" bind:checked={showSwitch} /> Switch port writes</label>{/if}
          {#if !source && devices.activeId == null}<span class="hint">Pick the board on the bench to name chips</span>{/if}
          {#if source && !source.setChip}<span class="hint">This recording has no board: chips can't be named here</span>{/if}
        </div>
      </div>
      <!-- The shading on the chart is extra: hover, or focus a folded row with the keyboard -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div class="lines mono" style:height="{height}px" bind:this={box} {onscroll} onmouseover={hoverRow} onfocusin={hoverRow}
        onmouseleave={() => setHover((hovered = null))} onfocusout={() => setHover((hovered = null))}>
        {#each rows.slice(0, MAX_ROWS) as n, i (i)}{@render node(n)}{/each}
        {#if rows.length > MAX_ROWS}<div class="more">… and {rows.length - MAX_ROWS} more rows</div>{/if}
      </div>
    {/if}
  {/if}
</section>

<style>
  .bus { position: relative; display: flex; flex-direction: column; min-width: 0; }
  .grip { position: absolute; left: 0; right: 0; top: 0; height: 9px; cursor: ns-resize; display: grid; place-items: center; z-index: 2; touch-action: none; border: 0; padding: 0; background: transparent; }
  .grip::after { content: ''; width: 44px; height: 4px; border-radius: 2px; background: var(--line); }
  .grip:hover::after, .grip:focus-visible::after { background: var(--accent); }
  .grip:focus-visible { outline: none; }
  .state { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); min-width: 0; }
  .small { font-size: 12px; }
  .decode { display: inline-flex; gap: 4px; }
  .opt.live { font-size: 12px; color: var(--muted); }
  .bar { flex: 0 0 auto; display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; justify-content: space-between; padding: 7px 14px; border-bottom: 1px solid var(--line); }
  .note { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; padding: 7px 14px; border-bottom: 1px solid var(--line); font-size: 12.5px; color: var(--muted); }
  .note.err { color: var(--rec); }
  .closed { display: flex; flex-direction: column; justify-content: center; gap: 10px; padding: 16px 22px; max-height: 160px; }
  .closed p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.55; max-width: 46ch; }

  .devs { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; min-width: 0; }
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
  .ttl { margin: 0; font-weight: 600; font-family: var(--mono); font-size: 12.5px; }
  .rn { color: var(--text); font-family: var(--font); font-size: 12px; }
  .rx { color: var(--faint); font-size: 11px; margin-left: 4px; }
  .val { color: var(--flash); font-family: var(--font); }
  .actions { display: flex; gap: 8px; justify-content: flex-end; }
  .menu p { margin: 0; }

  .opts { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; font-size: 12px; color: var(--muted); }
  .opt { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
  .opt input { margin: 0; accent-color: var(--accent); }

  .lines { flex: none; overflow: auto; padding: 6px 0; font-size: 12px; line-height: 1.5; container-type: inline-size; }
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
