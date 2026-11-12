<script lang="ts">
  // The sensor library: chip types shared by every board (how a chip talks on I2C, so the Bus panel can name what it
  // decodes and turn bytes into readings). A list on the left, the chosen chip's table on the right. Tables are edited
  // as JSON (hex numbers allowed, e.g. "0x44"); agents fill them through MCP and you check them here.
  import { api, type Chip, type ChipBus, type ChipTable } from '../../lib/api';
  import { library, onBus } from '../../lib/chips.svelte';
  import SectionHead from './SectionHead.svelte';

  let selected = $state<string | null>(null);
  let filter = $state('');
  let editing = $state<'new' | number | null>(null);
  let draft = $state('');
  let error = $state('');
  let busy = $state(false);
  let confirmDelete = $state(false);
  let copied = $state(false);

  $effect(() => {
    library.load().catch((e) => (error = (e as Error).message));
  });

  const shown = $derived(library.list.filter((c) => {
    const f = filter.trim().toLowerCase();
    const addrs = onBus(c, 'i2c')?.addresses ?? [];
    return !f || ((f === 'spi' || f === 'i2c') && !!onBus(c, f)) || c.name.toLowerCase().includes(f) || (c.body.kind ?? '').toLowerCase().includes(f) ||
      (c.body.vendor ?? '').toLowerCase().includes(f) || addrs.some((a) => hex(a).toLowerCase() === f || hex(a).toLowerCase() === `0x${f}`);
  }));
  const chip = $derived(library.list.find((c) => c.name === selected) ?? shown[0] ?? null);
  const unchecked = $derived(library.list.filter((c) => !c.checked && c.source === 'agent').length);

  const hex = (n: number, w = 2) => '0x' + n.toString(16).toUpperCase().padStart(w, '0');
  const KEYS_HEX = new Set(['addr', 'code', 'reg', 'mask', 'read_bit', 'inc_bit']);

  /** The table as JSON to edit: addresses and register/command numbers in hex, which people and datasheets use. */
  function toJson(t: ChipTable): string {
    return JSON.stringify(t, (k, v) => (KEYS_HEX.has(k) && typeof v === 'number' ? hex(v) : k === 'addresses' && Array.isArray(v) ? v.map((a: number) => hex(a)) : v), 2);
  }
  /** JSON back to a table: "0x44" strings become numbers. */
  function fromJson(text: string): ChipTable {
    return JSON.parse(text, (k, v) => (typeof v === 'string' && /^0x[0-9a-f]+$/i.test(v) && (KEYS_HEX.has(k) || /^\d+$/.test(k)) ? parseInt(v, 16) : v));
  }

  const TEMPLATE: ChipTable = {
    name: 'NEWCHIP', vendor: '', kind: '', datasheet: '',
    buses: [
      { bus: 'i2c', addresses: [0x40], style: 'registers', reg_bytes: 1, byte_order: 'big', registers: [{ addr: 0x00, name: 'CONFIG', rw: 'rw', about: '' }] },
      { bus: 'spi', read_bit: 0x80, read_when_set: true, style: 'registers', registers: [{ addr: 0x00, name: 'CONFIG', rw: 'rw', about: '' }] },
    ],
  };
  let busTab = $state<'i2c' | 'spi'>('i2c');

  function startEdit(c: Chip | null) {
    draft = toJson(c ? c.body : TEMPLATE);
    editing = c ? c.id : 'new';
    error = '';
    confirmDelete = false;
  }

  async function save() {
    let t: ChipTable;
    try {
      t = fromJson(draft);
    } catch (e) {
      error = `Not valid JSON: ${(e as Error).message}`;
      return;
    }
    busy = true;
    error = '';
    try {
      const saved = editing === 'new' ? await api.createChip(t) : await api.updateChip(editing as number, t);
      await library.load();
      selected = saved.name;
      editing = null;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  async function act(f: () => Promise<unknown>) {
    busy = true;
    error = '';
    try {
      await f();
      await library.load();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  async function copy(c: Chip) {
    try {
      await navigator.clipboard.writeText(toJson(c.body));
      copied = true;
      setTimeout(() => (copied = false), 1500);
    } catch {
      startEdit(c); // no clipboard: show the JSON to copy by hand
    }
  }

  const styleText = (t: ChipBus) => t.style === 'commands' ? 'commands, then a read' : t.style === 'registers'
    ? `registers, ${t.reg_bytes ?? 1} byte${(t.reg_bytes ?? 1) > 1 ? 's' : ''}${(t.reg_bytes ?? 1) > 1 ? `, ${t.byte_order ?? 'big'}-endian` : ''}` : t.style === 'switch' ? `I2C switch, ${t.ports ?? 8} ports` : 'no table yet';
  const busName = (b: ChipBus) => (b.bus === 'spi' ? 'SPI' : 'I2C');
  const where = (b: ChipBus) => b.bus === 'spi'
    ? `first byte: ${hex(b.read_bit ?? 0x80)} ${b.read_when_set === false ? 'set = write' : 'set = read'}${b.inc_bit ? `, ${hex(b.inc_bit)} auto-increment` : ''}${b.mode != null ? ` · mode ${b.mode}` : ''}`
    : (b.addresses ?? []).map((a) => hex(a)).join(' ');
  const hasTable = (c: Chip) => c.body.buses.some((b) => (b.style ?? 'basic') !== 'basic');
  const source = (c: Chip) => (c.source === 'agent' ? `by agent${c.agent ? ` (${c.agent})` : ''}` : c.source === 'built-in' ? 'built in' : 'by you');
</script>

<div class="s-section">
  <SectionHead title="Sensor library" intro="How each chip and sensor talks, per bus (I2C, SPI), for every board: the Bus panel names registers and commands from it and turns bytes into readings. Agents can fill a table from a datasheet; you check it here.">
    {#snippet action()}<button class="btn primary" type="button" disabled={editing === 'new'} onclick={() => startEdit(null)}>New chip</button>{/snippet}
  </SectionHead>
  {#if error && editing == null}<div class="s-banner">{error}</div>{/if}
  {#if unchecked}<div class="s-banner warn">{unchecked} table{unchecked === 1 ? '' : 's'} by an agent to check.</div>{/if}

  <section class="panel lib">
    <div class="list">
      <input class="input search" type="search" placeholder="Name, kind, 0x44 or SPI" bind:value={filter} aria-label="Filter the library" />
      <div class="items">
        {#each shown as c (c.id)}
          <button type="button" class="item" aria-current={chip?.id === c.id} onclick={() => { selected = c.name; editing = null; confirmDelete = false; }}>
            <span class="nm">{c.name}{#if c.boards.length}<span class="used" title="On {c.boards.join(', ')}">{c.boards.length}</span>{/if}</span>
            <span class="meta">{c.body.kind || c.body.vendor || '–'} · {c.body.buses.map((b) => b.bus === 'i2c' ? `${(b.addresses ?? []).slice(0, 2).map((a) => hex(a)).join(', ')}${(b.addresses ?? []).length > 2 ? '…' : ''}` : 'SPI').join(' · ')}</span>
            <span class="tags">
              {#if c.checked}<span class="tag ok">checked</span>{:else if c.source === 'agent'}<span class="tag warn">to check</span>{/if}
              {#if c.source === 'agent'}<span class="tag agent">agent</span>{:else if c.source === 'built-in'}<span class="tag">built in</span>{/if}
              {#if !hasTable(c)}<span class="tag dim">no table</span>{/if}
            </span>
          </button>
        {:else}
          <p class="muted small pad">{library.loaded ? 'Nothing matches.' : 'Loading…'}</p>
        {/each}
      </div>
    </div>

    <div class="detail">
      {#if editing != null}
        <h3>{editing === 'new' ? 'New chip' : `Edit ${chip?.name}`}</h3>
        <p class="muted small">The table as JSON. Numbers may be hex ("0x44"). Formulas: numbers, + − × ÷, brackets and u8 s8 u16 s16 u16le s16le u24 u32 u32le(index), e.g. <code>-45 + 175 * u16(0) / 65535</code>.</p>
        <textarea class="input json" bind:value={draft} spellcheck="false" aria-label="Chip table as JSON"></textarea>
        {#if error}<div class="error">{error}</div>{/if}
        <div class="actions">
          <button class="btn ghost" type="button" onclick={() => { editing = null; error = ''; }}>Cancel</button>
          <button class="btn primary" type="button" disabled={busy} onclick={save}>{editing === 'new' ? 'Add to the library' : 'Save'}</button>
        </div>
      {:else if chip}
        {@const t = chip.body}
        <div class="d-head">
          <h3>{chip.name}</h3>
          <span class="muted">{[t.vendor, t.kind].filter(Boolean).join(' · ')}</span>
          {#if chip.checked}<span class="tag ok">checked</span>{:else}<span class="tag warn">not checked</span>{/if}
        </div>
        <div class="prov">
          <span>{source(chip)}{t.datasheet ? ` · from “${t.datasheet}”` : ''}{chip.checked ? '' : chip.source === 'agent' ? '. Compare it with the datasheet, then mark it checked.' : ''}</span>
          <span class="spacer"></span>
          {#if !chip.checked}<button class="btn small primary" type="button" disabled={busy} onclick={() => act(() => api.checkChip(chip.id, true))}>Mark as checked</button>
          {:else}<button class="btn small ghost" type="button" disabled={busy} onclick={() => act(() => api.checkChip(chip.id, false))}>Not checked</button>{/if}
          <button class="btn small ghost" type="button" onclick={() => startEdit(chip)}>Edit</button>
          <button class="btn small ghost" type="button" onclick={() => copy(chip)}>{copied ? 'Copied' : 'Copy JSON'}</button>
          <button class="btn small ghost" type="button" onclick={() => (confirmDelete = true)}>Delete</button>
        </div>
        {#if confirmDelete}
          <div class="s-confirm">Delete {chip.name} from the library?{chip.boards.length ? ` ${chip.boards.join(', ')} keep${chip.boards.length === 1 ? 's' : ''} it by name, without a table.` : ''}
            <button class="btn small danger" type="button" disabled={busy} onclick={() => act(async () => { await api.deleteChip(chip.id); selected = null; confirmDelete = false; })}>Delete</button>
            <button class="btn small ghost" type="button" onclick={() => (confirmDelete = false)}>Cancel</button>
          </div>
        {/if}
        {@const buses = t.buses}
        {@const b = buses.find((x) => x.bus === busTab) ?? buses[0]}
        <dl class="facts">
          <div><dt>Buses</dt><dd>{buses.map(busName).join(' and ')}</dd></div>
          <div><dt>On boards</dt><dd>{chip.boards.join(', ') || '–'}</dd></div>
        </dl>
        {#if t.notes}<p class="small muted notes">{t.notes}</p>{/if}
        {#if buses.length > 1}
          <div class="bustabs" role="tablist" aria-label="Bus">
            {#each buses as x (x.bus)}<button type="button" role="tab" aria-selected={x === b} onclick={() => (busTab = x.bus)}>{busName(x)}</button>{/each}
          </div>
        {/if}
        <dl class="facts">
          <div><dt>{b.bus === 'spi' ? 'SPI' : 'I2C addresses'}</dt><dd class="mono">{where(b)}</dd></div>
          <div><dt>Style</dt><dd>{styleText(b)}</dd></div>
          {#if b.bank}<div><dt>Register bank</dt><dd class="mono">{hex(b.bank.reg)} bit mask {hex(b.bank.mask)}</dd></div>{/if}
        </dl>
        {#if b.style === 'registers' && b.registers?.length}
          <div class="s-tablewrap"><table class="tbl">
            <thead><tr><th>Addr</th><th>Name</th><th>R/W</th><th>What it is</th><th>Readings</th></tr></thead>
            <tbody>{#each b.registers as r (r.addr)}<tr>
              <td class="mono">{hex(r.addr)}</td><td class="mono">{r.name}</td><td class="mono">{r.rw ?? 'rw'}</td>
              <td>{r.about ?? ''}{#if r.bytes}<span class="fld">{r.bytes} bytes</span>{/if}{#if r.fields}<span class="fld">{r.fields}</span>{/if}</td>
              <td>{#each r.values ?? [] as v (v.name)}<span class="vv">{v.name}{v.unit ? ` (${v.unit})` : ''}</span><span class="fld">{v.expr}</span>{:else}<span class="faint">–</span>{/each}</td>
            </tr>{/each}</tbody>
          </table></div>
        {:else if b.style === 'commands' && b.commands?.length}
          <div class="s-tablewrap"><table class="tbl">
            <thead><tr><th>Code</th><th>Name</th><th>Answer</th><th>Wait</th><th>Readings</th></tr></thead>
            <tbody>{#each b.commands as c (c.code)}<tr>
              <td class="mono">{hex(c.code)}</td><td>{c.name}{#if c.about}<span class="fld">{c.about}</span>{/if}</td>
              <td class="mono">{c.reply ?? 0} bytes</td><td class="mono">{c.wait_ms != null ? `${c.wait_ms} ms` : '–'}</td>
              <td>{#each c.values ?? [] as v (v.name)}<span class="vv">{v.name}{v.unit ? ` (${v.unit})` : ''}</span><span class="fld">{v.expr}</span>{:else}<span class="faint">–</span>{/each}</td>
            </tr>{/each}</tbody>
          </table></div>
        {:else if b.style === 'switch'}
          <p class="small muted">One control byte: bit n opens port n (0–{(b.ports ?? 8) - 1}), 0x00 closes them all. A chip seen while a port is open belongs to that port.</p>
        {:else}
          <p class="small muted">No {busName(b)} register or command table yet. An agent can fill it from the datasheet (MCP tool <code>chip_save</code>, with "Agents may edit the sensor library" on in Settings → Agents), or Edit it here.</p>
        {/if}
      {/if}
    </div>
  </section>
</div>

<style>
  .lib { display: grid; grid-template-columns: minmax(200px, 260px) minmax(0, 1fr); min-height: 420px; }
  .list { border-right: 1px solid var(--line); display: flex; flex-direction: column; min-width: 0; }
  .search { margin: 10px; width: auto; }
  .items { overflow: auto; max-height: 640px; }
  .item { all: unset; box-sizing: border-box; width: 100%; cursor: pointer; padding: 8px 14px; border-top: 1px solid var(--line); display: flex; flex-direction: column; gap: 2px; }
  .item:hover { background: var(--panel2); }
  .item[aria-current='true'] { background: var(--sel); }
  .item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
  .nm { font-weight: 600; font-size: 13.5px; display: flex; align-items: center; gap: 6px; }
  .used { font-size: 10.5px; font-weight: 600; min-width: 16px; height: 16px; border-radius: 8px; padding: 0 4px; display: inline-grid; place-items: center; background: var(--sel); color: var(--accent); }
  .meta { font-size: 11.5px; color: var(--faint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tags { display: flex; gap: 4px; flex-wrap: wrap; }
  .tag { font-size: 10.5px; padding: 0 6px; border-radius: 999px; background: var(--panel2); color: var(--muted); }
  .tag.ok { background: var(--ok-bg); color: var(--ok); }
  .tag.warn { background: var(--warn-bg); color: var(--warn); }
  .tag.agent { background: var(--flash-bg); color: var(--flash); }
  .tag.dim { color: var(--faint); }
  .pad { padding: 10px 14px; }
  .detail { padding: 14px 18px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  h3 { margin: 0; font-size: 17px; }
  .d-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 12px; }
  .prov { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 8px; background: var(--panel2); font-size: 12.5px; color: var(--muted); }
  .bustabs { display: flex; gap: 4px 18px; border-bottom: 1px solid var(--line); }
  .bustabs button { padding: 4px 0 6px; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px; background: none; color: var(--muted); font-size: 13px; cursor: pointer; }
  .bustabs button[aria-selected='true'] { border-bottom-color: var(--accent); color: var(--text); font-weight: 600; }
  .facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px 16px; margin: 0; font-size: 13px; }
  .facts div { display: flex; flex-direction: column; gap: 2px; }
  dt { color: var(--faint); font-size: 11.5px; }
  dd { margin: 0; }
  .notes { margin: 0; white-space: pre-wrap; }
  .tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  .tbl th { text-align: left; font-weight: 500; color: var(--faint); font-size: 11.5px; padding: 6px 10px; border-bottom: 1px solid var(--line); }
  .tbl td { padding: 5px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
  .tbl tr:last-child td { border-bottom: 0; }
  .fld { display: block; color: var(--faint); font-family: var(--mono); font-size: 11.5px; }
  .vv { display: block; }
  .json { min-height: 380px; font-family: var(--mono); font-size: 12px; padding: 10px; resize: vertical; white-space: pre; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; }
  .small { font-size: 12px; }
  code { font-family: var(--mono); font-size: 11.5px; }
  @media (max-width: 760px) {
    .lib { grid-template-columns: minmax(0, 1fr); }
    .list { border-right: 0; border-bottom: 1px solid var(--line); }
    .items { max-height: 260px; }
  }
</style>
