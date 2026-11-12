<script lang="ts">
  // The sensor library: chip types shared by every board (how a chip talks on I2C and SPI, so the Bus panel can name
  // what it decodes and turn bytes into readings). Filters and a list on the left, the chosen chip's table on the
  // right. Chips are added and edited in a popup form (ChipForm), with a JSON view and JSON import/export for agents
  // and power users. Built-in tables were checked against their datasheets by the project; agents' tables wait for a
  // person to check them.
  import { api, CHIP_CATEGORIES, CHIP_TAGS, type Chip, type ChipBus, type ChipCategory, type ChipTable, type ChipTag } from '../../lib/api';
  import { CATEGORY_NAMES, TAG_NAMES, hex, toJson } from '../../lib/chipdraft';
  import { library, onBus } from '../../lib/chips.svelte';
  import ChipForm from './ChipForm.svelte';
  import SectionHead from './SectionHead.svelte';

  type Filter = { q: string; bus: '' | 'i2c' | 'spi'; category: '' | ChipCategory; tag: '' | ChipTag; source: '' | Chip['source']; onBoard: boolean };
  const FILTER_KEY = 'pst.library.filter';
  const NO_FILTER: Filter = { q: '', bus: '', category: '', tag: '', source: '', onBoard: false };
  function loadFilter(): Filter {
    try {
      return { ...NO_FILTER, ...JSON.parse(localStorage.getItem(FILTER_KEY) ?? '{}') };
    } catch {
      return { ...NO_FILTER };
    }
  }

  let selected = $state<string | null>(null);
  let filter = $state<Filter>(loadFilter());
  let form = $state<{ chip: Chip | null; start: ChipTable; json?: boolean } | null>(null);
  let error = $state('');
  let busy = $state(false);
  let confirm = $state<'delete' | 'restore' | null>(null);
  let copied = $state(false);
  let busTab = $state<'i2c' | 'spi'>('i2c');

  $effect(() => {
    library.load().catch((e) => (error = (e as Error).message));
  });
  $effect(() => {
    const f = JSON.stringify(filter);
    try {
      localStorage.setItem(FILTER_KEY, f);
    } catch {
      // private window: the filter just isn't remembered
    }
  });

  const filtered = $derived(filter.q || filter.bus || filter.category || filter.tag || filter.source || filter.onBoard);
  const shown = $derived(library.list.filter((c) => {
    const f = filter.q.trim().toLowerCase();
    const addrs = onBus(c, 'i2c')?.addresses ?? [];
    const tags = c.body.tags ?? [];
    return (!filter.bus || !!onBus(c, filter.bus)) && (!filter.category || c.body.category === filter.category) &&
      (!filter.tag || tags.includes(filter.tag)) &&
      (!filter.source || c.source === filter.source) && (!filter.onBoard || c.boards.length > 0) &&
      (!f || c.name.toLowerCase().includes(f) || tags.some((t) => TAG_NAMES[t].toLowerCase().startsWith(f)) || (c.body.kind ?? '').toLowerCase().includes(f) || (c.body.vendor ?? '').toLowerCase().includes(f) ||
        addrs.some((a) => hex(a).toLowerCase() === f || hex(a).toLowerCase() === `0x${f}`));
  }));
  const chip = $derived(shown.find((c) => c.name === selected) ?? shown[0] ?? null);
  const unchecked = $derived(library.list.filter((c) => !c.checked && c.source === 'agent').length);

  const TEMPLATE: ChipTable = {
    name: 'NEWCHIP', vendor: '', kind: '', category: 'sensor', datasheet: '',
    buses: [{ bus: 'i2c', addresses: [0x40], style: 'registers', reg_bytes: 1, byte_order: 'big', registers: [{ addr: 0x00, name: 'CONFIG', rw: 'rw' }] }],
  };

  function edit(c: Chip | null, json = false) {
    form = { chip: c, start: c ? c.body : TEMPLATE, json };
    error = '';
    confirm = null;
  }

  async function saved(c: Chip) {
    form = null;
    await library.load();
    selected = c.name;
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
      confirm = null;
    }
  }

  async function copy(c: Chip) {
    try {
      await navigator.clipboard.writeText(toJson(c.body));
      copied = true;
      setTimeout(() => (copied = false), 1500);
    } catch {
      edit(c, true); // no clipboard: show the JSON to copy by hand
    }
  }

  function exportJson(c: Chip) {
    const url = URL.createObjectURL(new Blob([toJson(c.body) + '\n'], { type: 'application/json' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `${c.name.replace(/[^A-Za-z0-9._-]+/g, '_')}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const styleText = (t: ChipBus) => t.style === 'commands' ? 'commands, then a read' : t.style === 'registers'
    ? `registers, ${t.reg_bytes ?? 1} byte${(t.reg_bytes ?? 1) > 1 ? 's' : ''}${(t.reg_bytes ?? 1) > 1 ? `, ${t.byte_order ?? 'big'}-endian` : ''}` : t.style === 'switch' ? `I2C switch, ${t.ports ?? 8} ports` : 'no table yet';
  const busName = (b: ChipBus) => (b.bus === 'spi' ? 'SPI' : 'I2C');
  const where = (b: ChipBus) => b.bus === 'spi'
    ? `first byte: ${hex(b.read_bit ?? 0x80)} ${b.read_when_set === false ? 'set = write' : 'set = read'}${b.inc_bit ? `, ${hex(b.inc_bit)} auto-increment` : ''}${b.mode != null ? ` · mode ${b.mode}` : ''}${b.max_hz ? ` · up to ${b.max_hz / 1e6} MHz` : ''}`
    : (b.addresses ?? []).map((a) => hex(a)).join(' ');
  const hasTable = (c: Chip) => c.body.buses.some((b) => (b.style ?? 'basic') !== 'basic');
  const changed = (c: Chip) => c.built_in && c.source !== 'built-in';
  const who = (c: Chip) => (c.source === 'agent' ? `an agent${c.agent ? ` (${c.agent})` : ''}` : 'you');
</script>

<div class="s-section">
  <SectionHead title="Sensor library" intro="How each chip and sensor talks, per bus (I2C, SPI), for every board: the Bus panel names registers and commands from it and turns bytes into readings. Built-in tables are checked against their datasheets; agents can fill a table from a datasheet, and you check it here.">
    {#snippet action()}
      <button class="btn" type="button" onclick={() => edit(null, true)}>Import JSON</button>
      <button class="btn primary" type="button" onclick={() => edit(null)}>New chip</button>
    {/snippet}
  </SectionHead>
  {#if error}<div class="s-banner">{error}</div>{/if}
  {#if unchecked}<div class="s-banner warn">{unchecked} table{unchecked === 1 ? '' : 's'} by an agent to check.</div>{/if}

  <section class="panel lib">
    <div class="list">
      <div class="filters">
        <input class="input" type="search" placeholder="Name, what it measures or 0x44" bind:value={filter.q} aria-label="Search the library" />
        <div class="frow">
          <div class="seg" role="group" aria-label="Bus">
            {#each [['', 'All'], ['i2c', 'I2C'], ['spi', 'SPI']] as [v, label] (v)}
              <button type="button" aria-pressed={filter.bus === v} onclick={() => (filter.bus = v as Filter['bus'])}>{label}</button>
            {/each}
          </div>
          <label class="onboard"><input type="checkbox" bind:checked={filter.onBoard} /> On a board</label>
        </div>
        <div class="frow">
          <select class="input" bind:value={filter.tag} aria-label="Measures">
            <option value="">Measures anything</option>
            {#each CHIP_TAGS as t (t)}<option value={t}>{TAG_NAMES[t]}</option>{/each}
          </select>
        </div>
        <div class="frow">
          <select class="input" bind:value={filter.category} aria-label="Type">
            <option value="">Every type</option>
            {#each CHIP_CATEGORIES as c (c)}<option value={c}>{CATEGORY_NAMES[c]}</option>{/each}
          </select>
          <select class="input" bind:value={filter.source} aria-label="Made by">
            <option value="">Anyone</option><option value="built-in">Built in</option><option value="you">By you</option><option value="agent">By an agent</option>
          </select>
        </div>
        <div class="count">
          <span>{filtered ? `${shown.length} of ${library.list.length}` : library.list.length} chips</span>
          {#if filtered}<button class="linkbtn" type="button" onclick={() => (filter = { ...NO_FILTER })}>Clear filters</button>{/if}
        </div>
      </div>
      <div class="items">
        {#each shown as c (c.id)}
          <button type="button" class="item" aria-current={chip?.id === c.id} onclick={() => { selected = c.name; confirm = null; }}>
            <span class="nm">{c.name}{#if c.boards.length}<span class="used" title="On {c.boards.join(', ')}">{c.boards.length}</span>{/if}</span>
            <span class="meta">{c.body.kind || c.body.vendor || '–'} · {c.body.buses.map((b) => b.bus === 'i2c' ? `${(b.addresses ?? []).slice(0, 2).map((a) => hex(a)).join(', ')}${(b.addresses ?? []).length > 2 ? '…' : ''}` : 'SPI').join(' · ')}</span>
            <span class="tags">
              {#if c.source === 'built-in'}<span class="tag">built in</span>
              {:else if changed(c)}<span class="tag">built in, changed</span>{/if}
              {#if c.source === 'agent'}{#if c.checked}<span class="tag ok">checked</span>{:else}<span class="tag warn">to check</span>{/if}<span class="tag agent">agent</span>
              {:else if c.source === 'you' && !c.built_in}<span class="tag">by you</span>{/if}
              {#if !hasTable(c)}<span class="tag dim">no table</span>{/if}
            </span>
          </button>
        {:else}
          <p class="muted small pad">{library.loaded ? (filtered ? 'Nothing matches these filters.' : 'The library is empty.') : 'Loading…'}</p>
        {/each}
      </div>
    </div>

    <div class="detail">
      {#if chip}
        {@const t = chip.body}
        <div class="d-head">
          <h3>{chip.name}</h3>
          <span class="muted">{[t.vendor, t.kind].filter(Boolean).join(' · ')}</span>
          {#if chip.source === 'agent'}{#if chip.checked}<span class="tag ok">checked</span>{:else}<span class="tag warn">not checked</span>{/if}{/if}
        </div>
        <div class="prov">
          <span>
            {#if chip.source === 'built-in'}Built in · checked against {#if t.datasheet_url}<a href={t.datasheet_url} target="_blank" rel="noreferrer">{t.datasheet || 'the datasheet'}</a>{:else}{t.datasheet || 'its datasheet'}{/if}
            {:else if changed(chip)}Built in, changed by {who(chip)}: updates of the built-in table don't reach it.
            {:else if chip.source === 'agent'}By {who(chip)}{t.datasheet ? ` · from “${t.datasheet}”` : ''}.{chip.checked ? '' : ' Compare it with the datasheet, then mark it checked.'}
            {:else}By you{/if}
          </span>
          <span class="spacer"></span>
          {#if chip.source === 'agent'}
            {#if !chip.checked}<button class="btn small primary" type="button" disabled={busy} onclick={() => act(() => api.checkChip(chip.id, true))}>Mark as checked</button>
            {:else}<button class="btn small ghost" type="button" disabled={busy} onclick={() => act(() => api.checkChip(chip.id, false))}>Not checked</button>{/if}
          {/if}
          {#if changed(chip)}<button class="btn small ghost" type="button" onclick={() => (confirm = 'restore')}>Back to built in</button>{/if}
          <button class="btn small ghost" type="button" onclick={() => edit(chip)}>Edit</button>
          <button class="btn small ghost" type="button" onclick={() => copy(chip)}>{copied ? 'Copied' : 'Copy JSON'}</button>
          <button class="btn small ghost" type="button" onclick={() => exportJson(chip)}>Export</button>
          <button class="btn small ghost" type="button" onclick={() => (confirm = 'delete')}>Delete</button>
        </div>
        {#if confirm === 'delete'}
          <div class="s-confirm">Delete {chip.name} from the library?{chip.boards.length ? ` ${chip.boards.join(', ')} keep${chip.boards.length === 1 ? 's' : ''} it by name, without a table.` : ''}{chip.built_in ? ' The built-in table comes back when the tracker restarts.' : ''}
            <button class="btn small danger" type="button" disabled={busy} onclick={() => act(async () => { await api.deleteChip(chip.id); selected = null; })}>Delete</button>
            <button class="btn small ghost" type="button" onclick={() => (confirm = null)}>Cancel</button>
          </div>
        {:else if confirm === 'restore'}
          <div class="s-confirm">Put {chip.name} back to the built-in table? The changes are lost; boards keep using it.
            <button class="btn small primary" type="button" disabled={busy} onclick={() => act(() => api.restoreChip(chip.id))}>Back to built in</button>
            <button class="btn small ghost" type="button" onclick={() => (confirm = null)}>Cancel</button>
          </div>
        {/if}
        {@const buses = t.buses}
        {@const b = buses.find((x) => x.bus === busTab) ?? buses[0]}
        <dl class="facts">
          <div><dt>Buses</dt><dd>{buses.map(busName).join(' and ')}</dd></div>
          <div><dt>Type</dt><dd>{t.category ? CATEGORY_NAMES[t.category] : '–'}</dd></div>
          <div><dt>Measures</dt><dd>{(t.tags ?? []).map((x) => TAG_NAMES[x]).join(', ') || '–'}</dd></div>
          <div><dt>On boards</dt><dd>{chip.boards.join(', ') || '–'}</dd></div>
          {#if chip.source !== 'built-in' && (t.datasheet || t.datasheet_url)}<div><dt>Datasheet</dt><dd>{#if t.datasheet_url}<a href={t.datasheet_url} target="_blank" rel="noreferrer">{t.datasheet || 'link'}</a>{:else}{t.datasheet}{/if}</dd></div>{/if}
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
          {#if b.bank}<div><dt>Register bank</dt><dd class="mono" title={b.bank.about ?? ''}>{hex(b.bank.reg)} bit mask {hex(b.bank.mask)}</dd></div>{/if}
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

{#if form}
  <ChipForm chip={form.chip} start={form.start} json={form.json} onclose={() => (form = null)} onsaved={saved} />
{/if}

<style>
  .lib { display: grid; grid-template-columns: minmax(200px, 260px) minmax(0, 1fr); min-height: 420px; }
  .list { border-right: 1px solid var(--line); display: flex; flex-direction: column; min-width: 0; }
  .filters { padding: 10px; display: flex; flex-direction: column; gap: 8px; border-bottom: 1px solid var(--line); }
  .frow { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }
  .frow select { flex: 1 1 100px; width: auto; min-width: 0; min-height: 32px; font-size: 12.5px; padding: 0 6px; }
  .seg button { padding: 4px 10px; font-size: 12.5px; }
  .seg button[aria-pressed='true'] { background: var(--sel); color: var(--accent); }
  .onboard { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--muted); cursor: pointer; }
  .onboard input { width: 16px; height: 16px; accent-color: var(--accent); }
  .count { display: flex; justify-content: space-between; align-items: center; font-size: 11.5px; color: var(--faint); }
  .count .linkbtn { font-size: 11.5px; }
  .items { overflow: auto; max-height: 560px; }
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
  .prov a, .facts a { color: var(--accent); }
  .small { font-size: 12px; }
  code { font-family: var(--mono); font-size: 11.5px; }
  @media (max-width: 760px) {
    .lib { grid-template-columns: minmax(0, 1fr); }
    .list { border-right: 0; border-bottom: 1px solid var(--line); }
    .items { max-height: 260px; }
  }
</style>
