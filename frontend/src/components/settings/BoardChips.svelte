<script lang="ts">
  // A board's chips on its I2C bus (Settings → Devices & sensors): where each sits (main bus or a port of an I2C switch), its
  // address, its type from the sensor library and its name on this board. The Bus panel uses this to name what it
  // decodes; agents can set it too when Settings → Agents allows them.
  import { api, type BoardChip, type Device } from '../../lib/api';
  import { library, onBus } from '../../lib/chips.svelte';
  import { bus } from '../../lib/bus.svelte';

  let { device, others }: { device: Device; others: Device[] } = $props();

  type Row = { port: string; addr: string; type: string; name: string; switch?: boolean };
  let rows = $state<Row[]>([]);
  let loaded = $state(false);
  let dirty = $state(false);
  let error = $state('');
  let busy = $state(false);
  let copyFrom = $state('');

  const hex = (n: number) => '0x' + n.toString(16).toUpperCase().padStart(2, '0');
  const PORTS = ['m', '0', '1', '2', '3', '4', '5', '6', '7', 'spi'];

  function toRows(chips: Record<string, BoardChip>): Row[] {
    return Object.entries(chips)
      .map(([k, c]) => { const [port, a] = k.split(':'); return { port, addr: k === 'spi' ? '' : hex(Number(a)), type: c.type ?? '', name: c.name ?? '', switch: c.switch }; })
      .sort((a, b) => (a.port === 'm' ? -1 : 0) - (b.port === 'm' ? -1 : 0) || a.port.localeCompare(b.port) || a.addr.localeCompare(b.addr));
  }

  async function load() {
    try {
      library.ensure();
      rows = toRows(await api.busChips(device.id));
      loaded = true;
      dirty = false;
    } catch (e) {
      error = (e as Error).message;
    }
  }
  $effect(() => { void device.id; void load(); });

  function parseAddr(s: string): number | null {
    const t = s.trim().toLowerCase();
    const n = t.startsWith('0x') ? parseInt(t.slice(2), 16) : /^\d+$/.test(t) ? parseInt(t, 10) : NaN;
    return Number.isInteger(n) && n >= 0x03 && n <= 0x77 ? n : null;
  }

  async function save() {
    const out: Record<string, BoardChip> = {};
    for (const r of rows) {
      if (r.port === 'spi') {
        if (out.spi) return void (error = 'One SPI chip per board for now (the analyzer has one chip-select input)');
        out.spi = { ...(r.name.trim() ? { name: r.name.trim() } : {}), ...(r.type ? { type: r.type } : {}) };
        continue;
      }
      const a = parseAddr(r.addr);
      if (a == null) return void (error = `${r.addr || '(empty)'} is not an I2C address (0x03 to 0x77)`);
      const key = `${r.port}:${a}`;
      if (out[key]) return void (error = `Two chips at ${hex(a)}${r.port === 'm' ? ' on the main bus' : ` on port ${r.port}`}`);
      out[key] = { ...(r.name.trim() ? { name: r.name.trim() } : {}), ...(r.type ? { type: r.type } : {}), ...(r.switch != null ? { switch: r.switch } : {}) };
    }
    busy = true;
    error = '';
    try {
      rows = toRows(await api.setBusChips(device.id, out));
      dirty = false;
      if (device.id === bus.chipsOf) bus.chipsOf = null; // the Bus panel loads the new list
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  async function copy() {
    const from = others.find((d) => String(d.id) === copyFrom);
    if (!from) return;
    try {
      rows = toRows(await api.busChips(from.id));
      dirty = true;
      copyFrom = '';
    } catch (e) {
      error = (e as Error).message;
    }
  }

  function setType(r: Row, t: string) {
    r.type = t;
    const c = library.get(t);
    const first = onBus(c, 'i2c')?.addresses?.[0];
    if (r.port !== 'spi' && parseAddr(r.addr) == null && first != null) r.addr = hex(first);
    dirty = true;
  }
  const typeChoices = (r: Row) => {
    const a = parseAddr(r.addr);
    const fit = r.port === 'spi' ? library.spi : a == null ? [] : library.at(a);
    return { fit, rest: library.list.filter((c) => !fit.includes(c)) };
  };
</script>

<details class="chips" open={rows.length > 0 && rows.length < 12}>
  <summary>Chips on its I2C and SPI bus <span class="muted">({loaded ? rows.length : '…'})</span></summary>
  <div class="inner">
    {#if rows.length}
      <div class="s-tablewrap">
        <table>
          <thead><tr><th>Where</th><th>Address</th><th>Type (sensor library)</th><th>Name on this board</th><th></th></tr></thead>
          <tbody>
            {#each rows as r, i (i)}
              {@const ch = typeChoices(r)}
              {@const t = library.get(r.type)}
              <tr>
                <td><select class="input sm" bind:value={r.port} onchange={() => (dirty = true)} aria-label="Where">
                  {#each PORTS as p (p)}<option value={p}>{p === 'm' ? 'I2C main bus' : p === 'spi' ? 'SPI (chip select)' : `I2C switch port ${p}`}</option>{/each}
                </select></td>
                <td>{#if r.port === 'spi'}<span class="faint small">–</span>{:else}<input class="input sm mono addr" bind:value={r.addr} oninput={() => (dirty = true)} aria-label="Address" />{/if}</td>
                <td>
                  <select class="input sm" value={r.type} onchange={(e) => setType(r, (e.target as HTMLSelectElement).value)} aria-label="Type">
                    <option value="">–</option>
                    {#if ch.fit.length}<optgroup label={r.port === 'spi' ? 'SPI chips' : 'Answers at this address'}>{#each ch.fit as c (c.id)}<option value={c.name}>{c.name}</option>{/each}</optgroup>{/if}
                    <optgroup label="All chips">{#each ch.rest as c (c.id)}<option value={c.name}>{c.name}</option>{/each}</optgroup>
                    {#if r.type && !t}<option value={r.type}>{r.type} (not in the library)</option>{/if}
                  </select>
                  {#if t && !t.checked}<span class="tag warn" title="This table isn't checked yet">not checked</span>{/if}
                </td>
                <td><input class="input sm" bind:value={r.name} placeholder={r.type || 'name'} maxlength="40" oninput={() => (dirty = true)} aria-label="Name" /></td>
                <td><button class="btn small ghost" type="button" aria-label="Remove" onclick={() => { rows.splice(i, 1); dirty = true; }}>×</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else if loaded}
      <p class="muted small">No chips yet. Add the ones on this board so the Bus panel can name them, or let an agent do it from the schematic.</p>
    {/if}
    {#if error}<div class="error">{error}</div>{/if}
    <div class="bar">
      <button class="btn small" type="button" onclick={() => { rows.push({ port: 'm', addr: '', type: '', name: '' }); dirty = true; }}>Add chip</button>
      {#if others.length}
        <select class="input sm" bind:value={copyFrom} aria-label="Copy the chips of another board">
          <option value="">Copy from another board…</option>
          {#each others as d (d.id)}<option value={String(d.id)}>{d.name}</option>{/each}
        </select>
        {#if copyFrom}<button class="btn small ghost" type="button" onclick={copy}>Copy</button>{/if}
      {/if}
      <span class="spacer"></span>
      {#if dirty}
        <button class="btn small ghost" type="button" onclick={load}>Undo</button>
        <button class="btn small primary" type="button" disabled={busy} onclick={save}>Save chips</button>
      {/if}
    </div>
  </div>
</details>

<style>
  .chips { border-top: 1px solid var(--line); padding-top: 8px; }
  summary { cursor: pointer; font-size: 13px; font-weight: 600; }
  .inner { display: flex; flex-direction: column; gap: 8px; padding-top: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th { text-align: left; font-weight: 500; color: var(--faint); font-size: 11.5px; padding: 4px 6px; }
  td { padding: 3px 6px; vertical-align: middle; white-space: nowrap; }
  .sm { min-height: 30px; padding: 0 8px; font-size: 12.5px; width: auto; }
  .addr { width: 72px; }
  .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .tag { font-size: 10.5px; padding: 0 6px; border-radius: 999px; margin-left: 6px; }
  .tag.warn { background: var(--warn-bg); color: var(--warn); }
  .small { font-size: 12px; }
  p { margin: 0; }
</style>
