<script lang="ts">
  // Add or edit a chip in the sensor library, as a popup: name and datasheet on top, a tab per bus (I2C, SPI) with its
  // settings and its register or command table (rows can be added, moved and removed; a row opens for its bits and
  // readings, each formula checked as you type). Form | JSON switches to the raw table for agents and power users.
  import { api, CHIP_CATEGORIES, CHIP_TAGS, type Chip, type ChipTable } from '../../lib/api';
  import { addBus, CATEGORY_NAMES, TAG_NAMES, fromDraft, fromJson, hex, newCmd, newRow, num, toDraft, toJson, type DBus, type DValue, type Draft } from '../../lib/chipdraft';
  import { check, fmtValue, parseBytes } from '../../lib/formula';

  let { chip = null, start, json: asJson = false, onclose, onsaved }: {
    chip?: Chip | null; // null: a new chip
    start: ChipTable; // what the form opens with (the chip or a template)
    json?: boolean; // open in the JSON view (importing: paste or load a file)
    onclose: () => void;
    onsaved: (c: Chip) => void;
  } = $props();

  // svelte-ignore state_referenced_locally
  let draft = $state<Draft>(toDraft(start));
  // svelte-ignore state_referenced_locally
  const initial = JSON.stringify(fromDraft(toDraft(start)).table);
  // svelte-ignore state_referenced_locally
  let mode = $state<'form' | 'json'>(asJson ? 'json' : 'form');
  // svelte-ignore state_referenced_locally
  let json = $state(asJson ? toJson(start) : '');
  let tab = $state(0);
  let open = $state<number | null>(null); // the row (key) showing its bits and readings
  let addrText = $state('');
  let error = $state('');
  let errors = $state<string[]>([]);
  let busy = $state(false);
  let leaving = $state(false);
  let shownJson = ''; // the JSON the form was turned into: unchanged, back to Form keeps open rows and samples
  let dragKey = $state<number | null>(null);

  const bus = $derived<DBus | undefined>(draft.buses[tab] ?? draft.buses[0]);
  const missing = $derived((['i2c', 'spi'] as const).filter((b) => !draft.buses.some((x) => x.bus === b)));
  const BUS = (b: 'i2c' | 'spi') => (b === 'spi' ? 'SPI' : 'I2C');

  function current(): ChipTable | null {
    if (mode === 'json') {
      try {
        return fromJson(json);
      } catch (e) {
        error = (e as Error).message;
        return null;
      }
    }
    const r = fromDraft(draft);
    errors = r.errors;
    return r.errors.length ? null : r.table;
  }
  const dirty = () => {
    try {
      return JSON.stringify(mode === 'json' ? fromJson(json) : fromDraft(draft).table) !== initial;
    } catch {
      return true;
    }
  };

  function setMode(m: 'form' | 'json') {
    if (m === mode) return;
    error = '';
    if (m === 'json') {
      json = toJson(fromDraft(draft).table); // what's there, even with gaps: fix them in either view
      shownJson = json;
      errors = [];
    } else if (json !== shownJson) {
      try {
        draft = toDraft(fromJson(json));
        tab = 0;
      } catch (e) {
        error = (e as Error).message;
        return;
      }
    }
    mode = m;
  }

  async function loadFile(e: Event) {
    const f = (e.currentTarget as HTMLInputElement).files?.[0];
    if (!f) return;
    json = await f.text();
    error = '';
  }

  async function save() {
    error = '';
    const t = current();
    if (!t) return;
    busy = true;
    try {
      onsaved(chip ? await api.updateChip(chip.id, t) : await api.createChip(t));
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  function close() {
    if (busy) return;
    if (!leaving && dirty()) leaving = true;
    else onclose();
  }
  function onkey(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  }

  // addresses as pills: type one (0x44 or 68) and press Enter, comma or space
  function addAddr() {
    const b = bus;
    if (!b) return;
    const parts = addrText.split(/[\s,]+/).filter(Boolean);
    const bad = parts.filter((p) => { const n = num(p); return n == null || n < 0x03 || n > 0x77; });
    for (const p of parts) {
      const n = num(p);
      if (n != null && n >= 0x03 && n <= 0x77 && !b.addresses.includes(n)) b.addresses = [...b.addresses, n].sort((x, y) => x - y);
    }
    addrText = bad.join(' ');
  }
  function addrKey(e: KeyboardEvent) {
    if (e.key === 'Enter' || e.key === ',' || e.key === ' ') {
      e.preventDefault();
      addAddr();
    }
  }

  function newBus(b: 'i2c' | 'spi') {
    draft.buses = [...draft.buses, addBus(draft, b)];
    tab = draft.buses.length - 1;
  }
  function removeBus(i: number) {
    draft.buses = draft.buses.filter((_, j) => j !== i);
    tab = 0;
  }

  // rows: add (next free address), move, remove
  function addRow() {
    const b = bus!;
    if (b.style === 'commands') {
      const used = new Set(b.commands.map((c) => num(c.code)));
      let c = 0;
      while (used.has(c)) c++;
      const r = newCmd(c);
      b.commands = [...b.commands, r];
      open = r.key;
    } else {
      const used = new Set(b.registers.map((r) => num(r.addr)));
      let a = b.registers.length ? Math.max(...b.registers.map((r) => num(r.addr) ?? 0)) + 1 : 0;
      while (used.has(a)) a++;
      const r = newRow(a);
      b.registers = [...b.registers, r];
      open = r.key;
    }
  }
  function move<T>(list: T[], from: number, to: number): T[] {
    if (to < 0 || to >= list.length || from === to) return list;
    const out = [...list];
    const [x] = out.splice(from, 1);
    out.splice(to, 0, x);
    return out;
  }
  function moveRow(from: number, to: number) {
    const b = bus!;
    if (b.style === 'commands') b.commands = move(b.commands, from, to);
    else b.registers = move(b.registers, from, to);
  }
  function removeRow(i: number) {
    const b = bus!;
    if (b.style === 'commands') b.commands = b.commands.filter((_, j) => j !== i);
    else b.registers = b.registers.filter((_, j) => j !== i);
  }
  function sortRows() {
    const b = bus!;
    if (b.style === 'commands') b.commands = [...b.commands].sort((x, y) => (num(x.code) ?? 0) - (num(y.code) ?? 0));
    else b.registers = [...b.registers].sort((x, y) => (num(x.addr) ?? 0) - (num(y.addr) ?? 0));
  }
  function drop(i: number) {
    const b = bus!;
    const list: { key: number }[] = b.style === 'commands' ? b.commands : b.registers;
    const from = list.findIndex((r) => r.key === dragKey);
    if (from >= 0) moveRow(from, i);
    dragKey = null;
  }

  const rows = $derived(bus ? (bus.style === 'commands' ? bus.commands : bus.style === 'registers' ? bus.registers : []) : []);
  const hidden = $derived(bus ? (bus.style === 'commands' ? bus.registers.length : bus.style === 'registers' ? bus.commands.length : bus.registers.length + bus.commands.length) : 0);
  const badNum = (t: string, max = 0xffff) => { const n = num(t); return n == null || n > max; };

  function result(v: DValue, sample: string) {
    const bytes = parseBytes(sample);
    if (bytes == null) return { cls: 'bad', text: 'the sample is hex bytes, e.g. 66 66 93' };
    const r = check(v.expr, bytes);
    if (!r.ok) return { cls: 'bad', text: r.error };
    const reads = `reads ${r.need} byte${r.need === 1 ? '' : 's'}`;
    if (!bytes.length) return { cls: 'ok', text: `${reads}; add sample bytes to see a value` };
    if (r.value == null) return { cls: 'bad', text: `${reads}, the sample has ${bytes.length}` };
    return { cls: 'ok', value: `${fmtValue(r.value)}${v.unit ? ` ${v.unit}` : ''}`, text: `for the sample, ${reads}` };
  }
</script>

<svelte:window onkeydown={onkey} />

<div class="backdrop" role="presentation" onpointerdown={(e) => e.target === e.currentTarget && close()}>
  <div class="dialog panel" role="dialog" aria-modal="true" aria-label={chip ? `Edit ${chip.name}` : 'New chip'}>
    <div class="panel-head">
      <h2>{chip ? `Edit ${chip.name}` : 'New chip'}</h2>
      <span class="spacer"></span>
      <div class="seg" role="group" aria-label="Edit as">
        <button type="button" aria-pressed={mode === 'form'} onclick={() => setMode('form')}>Form</button>
        <button type="button" aria-pressed={mode === 'json'} onclick={() => setMode('json')}>JSON</button>
      </div>
      <button class="btn small ghost icon" type="button" aria-label="Close" onclick={close}>×</button>
    </div>

    <div class="body">
      {#if mode === 'json'}
        <div class="jsonbar">
          <p class="hint-line">The chip as JSON, as agents write it (MCP <code>chip_save</code>). Numbers may be hex ("0x44"). Paste a table, or load an exported file; Form shows it before you save.</p>
          <label class="btn small ghost file">Load a file<input type="file" accept=".json,application/json" onchange={loadFile} /></label>
        </div>
        <textarea class="input json" bind:value={json} spellcheck="false" aria-label="Chip as JSON"></textarea>
      {:else}
        <div class="grid">
          <label class="f"><span>Name</span><input class="input" bind:value={draft.name} placeholder="e.g. SHT4x" /></label>
          <label class="f"><span>Vendor</span><input class="input" bind:value={draft.vendor} /></label>
          <label class="f"><span>Type</span>
            <select class="input" bind:value={draft.category}>
              <option value="">–</option>
              {#each CHIP_CATEGORIES as c (c)}<option value={c}>{CATEGORY_NAMES[c]}</option>{/each}
            </select>
          </label>
          <label class="f"><span>What it is</span><input class="input" bind:value={draft.kind} placeholder="e.g. Ambient light" /></label>
          <label class="f"><span>Datasheet (name and revision)</span><input class="input" bind:value={draft.datasheet} placeholder="e.g. VEML7700, document 84286, rev. 1.8" /></label>
          <label class="f"><span>Datasheet link</span><input class="input" bind:value={draft.datasheet_url} placeholder="https://…" class:bad={!!draft.datasheet_url.trim() && !/^https?:\/\/\S+$/.test(draft.datasheet_url.trim())} /></label>
          <div class="f wide"><span>Measures (pick every one that fits; the library's filter and search use them)</span>
            <div class="tagpick" role="group" aria-label="Measures">
              {#each CHIP_TAGS as t (t)}
                <button type="button" class="tagbtn" aria-pressed={draft.tags.includes(t)} onclick={() => (draft.tags = draft.tags.includes(t) ? draft.tags.filter((x) => x !== t) : [...draft.tags, t])}>{TAG_NAMES[t]}</button>
              {/each}
            </div>
          </div>
          <label class="f wide"><span>Notes</span><textarea class="input notes" bind:value={draft.notes} placeholder="What the Bus panel or the next person should know"></textarea></label>
        </div>

        <div class="tabs" role="tablist" aria-label="Bus">
          {#each draft.buses as b, i (b.bus)}
            <button type="button" class="t" role="tab" aria-selected={bus === b} onclick={() => { tab = i; open = null; }}>{BUS(b.bus)}</button>
          {/each}
          <span class="spacer"></span>
          {#each missing as m (m)}<button type="button" class="btn small ghost" onclick={() => newBus(m)}>Add {BUS(m)}</button>{/each}
          {#if bus && draft.buses.length > 1}<button type="button" class="btn small ghost" onclick={() => removeBus(tab)}>Remove {BUS(bus.bus)}</button>{/if}
        </div>

        {#if bus}
          <div class="grid">
            {#if bus.bus === 'i2c'}
              <div class="f"><span>Addresses (7-bit)</span>
                <div class="addrs">
                  {#each bus.addresses as a (a)}<span class="pill mono">{hex(a)}<button type="button" aria-label="Remove {hex(a)}" onclick={() => (bus.addresses = bus.addresses.filter((x) => x !== a))}>×</button></span>{/each}
                  <input class="input mono addr" bind:value={addrText} onkeydown={addrKey} onblur={addAddr} placeholder="0x44" aria-label="Add an address" />
                </div>
              </div>
            {:else}
              <label class="f"><span>Read/write bit of the first byte</span><input class="input mono" bind:value={bus.read_bit} class:bad={badNum(bus.read_bit, 0xff)} /></label>
              <label class="f"><span>That bit set means</span>
                <select class="input" bind:value={bus.read_when_set}><option value={true}>read</option><option value={false}>write</option></select>
              </label>
              <label class="f"><span>Auto-increment bit</span><input class="input mono" bind:value={bus.inc_bit} placeholder="none (ST: 0x40)" class:bad={!!bus.inc_bit.trim() && badNum(bus.inc_bit, 0xff)} /></label>
              <label class="f"><span>SPI mode</span>
                <select class="input" bind:value={bus.mode}><option value="">any / not set</option>{#each ['0', '1', '2', '3'] as m (m)}<option value={m}>{m}</option>{/each}</select>
              </label>
              <label class="f"><span>Highest clock (MHz)</span><input class="input mono" bind:value={bus.max_mhz} placeholder="e.g. 10" /></label>
            {/if}
            <label class="f"><span>How it talks</span>
              <select class="input" bind:value={bus.style}>
                <option value="registers">Registers</option>
                <option value="commands">Commands, then a read</option>
                {#if bus.bus === 'i2c'}<option value="switch">I2C switch</option>{/if}
                <option value="basic">Only that it's there (no table)</option>
              </select>
            </label>
            {#if bus.style === 'registers'}
              <label class="f"><span>Register data width</span>
                <select class="input" bind:value={bus.reg_bytes}><option value={1}>1 byte</option><option value={2}>2 bytes</option></select>
              </label>
              <label class="f"><span>Byte order</span>
                <select class="input" bind:value={bus.byte_order}><option value="big">Big-endian (high byte first)</option><option value="little">Little-endian (low byte first)</option></select>
              </label>
            {:else if bus.style === 'switch'}
              <label class="f"><span>Ports</span><input class="input mono" bind:value={bus.ports} placeholder="8" /></label>
            {/if}
          </div>

          {#if bus.style === 'registers'}
            <details class="more" open={!!bus.bank}>
              <summary>Register bank or page (only for chips that switch banks, e.g. AS7343)</summary>
              {#if bus.bank}
                <div class="grid">
                  <label class="f"><span>Bank register</span><input class="input mono" bind:value={bus.bank.reg} class:bad={badNum(bus.bank.reg, 0xff)} /></label>
                  <label class="f"><span>Bit mask</span><input class="input mono" bind:value={bus.bank.mask} class:bad={badNum(bus.bank.mask, 0xff)} /></label>
                  <label class="f wide2"><span>Which bank holds what</span><input class="input" bind:value={bus.bank.about} /></label>
                </div>
                <button class="linkbtn" type="button" onclick={() => (bus.bank = null)}>No register bank</button>
              {:else}
                <button class="btn small" type="button" onclick={() => (bus.bank = { reg: '0x00', mask: '0x01', about: '' })}>Add a register bank</button>
              {/if}
            </details>
          {/if}

          {#if bus.style === 'registers' || bus.style === 'commands'}
            {@const cmds = bus.style === 'commands'}
            <div class="s-tablewrap"><table class="rtbl">
              <thead><tr>
                <th class="c-grip"></th>
                {#if cmds}<th>Code</th><th>Name</th><th>Answer</th><th>Wait (ms)</th><th>What it is</th>
                {:else}<th>Address</th><th>Name</th><th>R/W</th><th>What it is</th><th>Bytes</th>{/if}
                <th></th><th></th>
              </tr></thead>
              <tbody>
                {#each rows as r, i (r.key)}
                  {@const isOpen = open === r.key}
                  <tr class:openrow={isOpen} class:dragging={dragKey === r.key} ondragover={(e) => e.preventDefault()} ondrop={() => drop(i)}>
                    <td class="c-grip"><span class="grip" role="button" tabindex="-1" draggable="true" title="Drag to move" aria-label="Drag to move" ondragstart={(e) => { dragKey = r.key; e.dataTransfer?.setData('text/plain', String(r.key)); }} ondragend={() => (dragKey = null)}>⋮⋮</span></td>
                    {#if 'code' in r}
                      <td class="c-addr"><input class="input mono" bind:value={r.code} class:bad={badNum(r.code)} aria-label="Command code" /></td>
                      <td class="c-name"><input class="input" bind:value={r.name} aria-label="Name" /></td>
                      <td class="c-n"><input class="input mono" bind:value={r.reply} aria-label="Answer bytes" /></td>
                      <td class="c-n"><input class="input mono" bind:value={r.wait} aria-label="Wait in ms" /></td>
                      <td><input class="input" bind:value={r.about} aria-label="What it is" /></td>
                    {:else}
                      <td class="c-addr"><input class="input mono" bind:value={r.addr} class:bad={badNum(r.addr)} aria-label="Register address" /></td>
                      <td class="c-name"><input class="input mono" bind:value={r.name} aria-label="Name" /></td>
                      <td class="c-rw"><select class="input" bind:value={r.rw} aria-label="Read or write"><option value="r">r</option><option value="w">w</option><option value="rw">rw</option></select></td>
                      <td><input class="input" bind:value={r.about} aria-label="What it is" /></td>
                      <td class="c-n"><input class="input mono" bind:value={r.bytes} placeholder={String(bus.reg_bytes)} aria-label="Bytes a read returns" /></td>
                    {/if}
                    <td class="c-open"><button class="linkbtn" type="button" aria-expanded={isOpen} onclick={() => (open = isOpen ? null : r.key)}>{r.values.length ? `${r.values.length} reading${r.values.length > 1 ? 's' : ''}` : cmds ? 'readings' : 'bits, readings'} {isOpen ? '▴' : '▾'}</button></td>
                    <td class="c-act">
                      <button class="iconbtn" type="button" title="Move up" aria-label="Move up" disabled={i === 0} onclick={() => moveRow(i, i - 1)}>↑</button>
                      <button class="iconbtn" type="button" title="Move down" aria-label="Move down" disabled={i === rows.length - 1} onclick={() => moveRow(i, i + 1)}>↓</button>
                      <button class="iconbtn" type="button" title="Remove" aria-label="Remove" onclick={() => removeRow(i)}>×</button>
                    </td>
                  </tr>
                  {#if isOpen}
                    <tr class="openrow"><td></td><td colspan="7">
                      <div class="sub">
                        {#if !('code' in r)}
                          <label class="f"><span>Bits (what each field means)</span><input class="input mono" bind:value={r.fields} placeholder="bits 12:11 gain · 0 shut down" /></label>
                        {/if}
                        <div class="samp"><span>Sample bytes to check the formulas</span>
                          <input class="input mono" bind:value={r.sample} placeholder={cmds ? '66 66 93 80 00 A2' : '2C 01'} aria-label="Sample bytes" />
                          <span class="faint">hex, as the chip sends them</span>
                        </div>
                        {#each r.values as v, j (j)}
                          {@const res = result(v, r.sample)}
                          <div class="reading">
                            <label class="f"><span>Reading</span><input class="input" bind:value={v.name} placeholder="temperature" /></label>
                            <label class="f"><span>Unit</span><input class="input" bind:value={v.unit} placeholder="°C" /></label>
                            <label class="f fx"><span>Formula</span><input class="input mono" bind:value={v.expr} spellcheck="false" placeholder="-45 + 175 * u16(0) / 65535" /></label>
                            <div class="f chk"><span>Check</span><div class="res {res.cls}" aria-live="polite">{#if res.value}<b class="mono">{res.value}</b>{/if}<span>{res.text}</span></div></div>
                            <button class="iconbtn rm" type="button" aria-label="Remove this reading" onclick={() => (r.values = r.values.filter((_, k) => k !== j))}>×</button>
                          </div>
                        {/each}
                        <div class="subfoot">
                          <button class="btn small" type="button" disabled={r.values.length >= 8} onclick={() => (r.values = [...r.values, { name: '', unit: '', expr: '' }])}>Add a reading</button>
                          <span class="faint small">Formulas: numbers, + − × ÷, brackets and u8 s8 u16 s16 u16le s16le u24 u32 u32le(first byte), e.g. <code>u16le(0) * 0.0042</code></span>
                        </div>
                      </div>
                    </td></tr>
                  {/if}
                {:else}
                  <tr><td colspan="8" class="empty">No {cmds ? 'commands' : 'registers'} yet.</td></tr>
                {/each}
              </tbody>
            </table></div>
            <div class="rowbar">
              <button class="btn small" type="button" onclick={addRow}>{cmds ? 'Add a command' : 'Add a register'}</button>
              <button class="btn small ghost" type="button" disabled={rows.length < 2} onclick={sortRows}>Sort by {cmds ? 'code' : 'address'}</button>
            </div>
          {:else if bus.style === 'switch'}
            <p class="hint-line">An I2C switch: its one-byte writes open ports (bit n opens port n). The Bus panel puts the chips it sees behind the open port.</p>
          {:else}
            <p class="hint-line">Only that it's there on {BUS(bus.bus)}: the Bus panel names it by its address. Pick Registers or Commands to give it a table.</p>
          {/if}
          {#if hidden}<p class="hint-line warn">{hidden} {bus.style === 'commands' ? 'register' : bus.style === 'registers' ? 'command' : 'row'}{hidden === 1 ? '' : 's'} from another style won't be saved unless you switch back.</p>{/if}
        {/if}
      {/if}
    </div>

    <div class="foot">
      {#if leaving}
        <span class="warnmsg">Close without saving your changes?</span>
        <span class="spacer"></span>
        <button class="btn ghost" type="button" onclick={() => (leaving = false)}>Keep editing</button>
        <button class="btn danger" type="button" onclick={onclose}>Discard</button>
      {:else}
        <div class="msgs" class:none={!error && !errors.length}>
          {#if error}<div class="error">{error}</div>{/if}
          {#each errors.slice(0, 4) as e (e)}<div class="error">{e}</div>{/each}
          {#if errors.length > 4}<div class="error">and {errors.length - 4} more</div>{/if}
        </div>
        <span class="spacer"></span>
        <button class="btn ghost" type="button" onclick={close}>Cancel</button>
        <button class="btn primary" type="button" disabled={busy} onclick={save}>{chip ? 'Save' : 'Add to the library'}</button>
      {/if}
    </div>
  </div>
</div>

<style>
  .backdrop { position: fixed; inset: 0; z-index: 100; background: rgba(0, 0, 0, 0.45); display: flex; align-items: flex-start; justify-content: center; padding: 4vh 12px; }
  .dialog { width: min(1000px, 100%); max-height: 92vh; display: flex; flex-direction: column; box-shadow: var(--shadow); }
  .panel-head h2 { font-size: 16px; }
  .body { overflow: auto; padding: 14px 16px; display: flex; flex-direction: column; gap: 14px; min-height: 0; }
  .body > * { flex-shrink: 0; } /* the body scrolls; the register table must not shrink into its own scroll box */
  .foot { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 10px 16px; border-top: 1px solid var(--line); }
  .msgs { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 1 260px; }
  .msgs .error { font-size: 12.5px; }
  .msgs.none { display: none; }
  .warnmsg { font-size: 13px; }
  .btn.danger { background: var(--rec-strong); border-color: var(--rec-strong); color: #fff; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px 14px; }
  .f { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .f > span { font-size: 11.5px; color: var(--faint); }
  .f.wide { grid-column: 1 / -1; }
  .f.wide2 { grid-column: span 2; }
  .input.bad { border-color: var(--rec); }
  .tagpick { display: flex; flex-wrap: wrap; gap: 6px; }
  .tagbtn { font: inherit; font-size: 12px; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--line); background: transparent; color: var(--muted); cursor: pointer; }
  .tagbtn:hover { border-color: var(--faint); }
  .tagbtn[aria-pressed='true'] { background: var(--sel); border-color: var(--accent); color: var(--accent); font-weight: 600; }
  .notes { min-height: 54px; padding: 8px 10px; resize: vertical; }
  .jsonbar { display: flex; align-items: flex-start; gap: 10px; }
  .jsonbar p { flex: 1; }
  .file { position: relative; overflow: hidden; }
  .file input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
  .json { min-height: 420px; padding: 10px; font-family: var(--mono); font-size: 12px; white-space: pre; resize: vertical; }
  .mono { font-family: var(--mono); }
  .tabs { display: flex; align-items: flex-end; gap: 4px 18px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .tabs .t { padding: 4px 0 6px; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px; background: none; color: var(--muted); font-size: 13px; cursor: pointer; }
  .tabs .t[aria-selected='true'] { border-bottom-color: var(--accent); color: var(--text); font-weight: 600; }
  .tabs .btn { margin-bottom: 4px; }
  .addrs { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; min-height: 38px; }
  .pill { display: inline-flex; align-items: center; gap: 2px; font-size: 12px; padding: 2px 2px 2px 8px; border-radius: 999px; background: var(--panel2); }
  .pill button { border: 0; background: none; color: var(--faint); cursor: pointer; font-size: 14px; line-height: 1; padding: 0 5px; }
  .pill button:hover { color: var(--text); }
  .addr { width: 84px; min-height: 30px; }
  .more summary { cursor: pointer; font-size: 12.5px; color: var(--muted); }
  .more[open] { display: flex; flex-direction: column; gap: 8px; }
  .rtbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  .rtbl th { text-align: left; font-weight: 500; color: var(--faint); font-size: 11.5px; padding: 4px 4px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  .rtbl td { padding: 3px 4px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  .rtbl .input { min-height: 30px; padding: 0 7px; font-size: 12.5px; }
  .rtbl .c-grip { width: 18px; }
  .rtbl .c-addr { width: 82px; }
  .rtbl .c-name { width: 150px; }
  .rtbl .c-rw { width: 62px; }
  .rtbl .c-n { width: 70px; }
  .rtbl .c-open, .rtbl .c-act { white-space: nowrap; width: 1%; }
  .rtbl td:nth-child(6) { min-width: 160px; }
  tr.openrow td { background: var(--panel2); }
  tr.dragging td { opacity: 0.5; }
  .grip { color: var(--faint); cursor: grab; user-select: none; letter-spacing: -2px; padding: 0 2px; }
  .iconbtn { border: 0; background: none; color: var(--faint); cursor: pointer; font-size: 14px; padding: 2px 5px; border-radius: 4px; }
  .iconbtn:hover:not(:disabled) { color: var(--text); background: var(--panel2); }
  .iconbtn:disabled { opacity: 0.35; cursor: default; }
  .linkbtn { border: 0; background: none; color: var(--accent); cursor: pointer; padding: 0; font: inherit; font-size: 12px; }
  .empty { color: var(--faint); padding: 10px 6px; }
  .sub { display: flex; flex-direction: column; gap: 10px; padding: 8px 2px 10px; }
  .samp { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 12px; color: var(--muted); }
  .samp .input { width: 230px; min-height: 30px; }
  .reading { display: grid; grid-template-columns: 140px 80px minmax(200px, 1fr) minmax(180px, 240px) 26px; gap: 6px 8px; align-items: start; }
  .res { font-size: 12px; padding: 5px 8px; border-radius: 7px; min-height: 30px; display: flex; flex-direction: column; gap: 1px; }
  .res.ok { background: var(--ok-bg); color: var(--ok); }
  .res.bad { background: var(--rec-bg); color: var(--rec); }
  .rm { margin-top: 20px; }
  .subfoot { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
  .rowbar { display: flex; gap: 8px; flex-wrap: wrap; }
  .hint-line { margin: 0; font-size: 12.5px; color: var(--muted); }
  .hint-line.warn { color: var(--warn); }
  .faint { color: var(--faint); }
  .small { font-size: 11.5px; }
  code { font-family: var(--mono); font-size: 11.5px; }
  @media (max-width: 700px) {
    .backdrop { padding: 0; }
    .dialog { max-height: 100vh; height: 100%; border-radius: 0; }
    .reading { grid-template-columns: 1fr 1fr; }
    .reading .fx, .reading .chk { grid-column: 1 / -1; }
    .f.wide2 { grid-column: 1 / -1; }
  }
</style>
