// The sensor library's chip form works on a draft: the table with numbers as the text people type ("0x44"), rows with
// a key for moving them around, and sample bytes for checking formulas. toDraft / fromDraft convert, and fromDraft
// says what's wrong in words. toJson / fromJson are the JSON view (hex for addresses and codes), for agents and power
// users, import and export.
import { CHIP_TAGS, type ChipBus, type ChipCategory, type ChipCommand, type ChipRegister, type ChipTable, type ChipTag, type ChipValue } from './api';

export const CATEGORY_NAMES: Record<ChipCategory, string> = {
  sensor: 'Sensor', switch: 'I2C switch', memory: 'Memory', clock: 'Clock', display: 'Display',
  converter: 'Converter or monitor', gpio: 'GPIO expander', other: 'Other',
};

export const TAG_NAMES: Record<ChipTag, string> = {
  temperature: 'Temperature', humidity: 'Humidity', pressure: 'Pressure', gas: 'Gas', co2: 'CO2', light: 'Light',
  spectral: 'Spectral', uv: 'UV', infrared: 'Infrared', acceleration: 'Acceleration', rotation: 'Rotation',
  magnetic: 'Magnetic field', distance: 'Distance', current: 'Current', voltage: 'Voltage', power: 'Power',
  battery: 'Battery level', analog: 'Analog input',
};

export const hex = (n: number, w = 2) => '0x' + n.toString(16).toUpperCase().padStart(w, '0');

/** "0x44", "44h" or "68" -> 68; null when it isn't a whole number. */
export function num(text: string): number | null {
  const t = text.trim();
  if (/^0x[0-9a-f]+$/i.test(t)) return parseInt(t.slice(2), 16);
  if (/^[0-9a-f]+h$/i.test(t)) return parseInt(t.slice(0, -1), 16);
  if (/^\d+$/.test(t)) return Number(t);
  return null;
}

let nextKey = 1;
const key = () => nextKey++;

export type DValue = { name: string; unit: string; expr: string };
export type DRow = { key: number; addr: string; name: string; rw: 'r' | 'w' | 'rw'; about: string; fields: string; bytes: string; values: DValue[]; sample: string };
export type DCmd = { key: number; code: string; name: string; reply: string; wait: string; about: string; values: DValue[]; sample: string };
export type DBus = {
  bus: 'i2c' | 'spi'; style: NonNullable<ChipBus['style']>; addresses: number[]; ports: string;
  read_bit: string; read_when_set: boolean; inc_bit: string; mode: string; max_mhz: string;
  reg_bytes: 1 | 2; byte_order: 'big' | 'little'; bank: { reg: string; mask: string; about: string } | null;
  registers: DRow[]; commands: DCmd[];
};
export type Draft = { name: string; vendor: string; kind: string; category: ChipCategory | ''; tags: ChipTag[]; datasheet: string; datasheet_url: string; notes: string; buses: DBus[] };

const dvalues = (v: ChipValue[] | undefined): DValue[] => (v ?? []).map((x) => ({ name: x.name, unit: x.unit ?? '', expr: x.expr }));

export function newRow(addr = 0): DRow {
  return { key: key(), addr: hex(addr), name: '', rw: 'rw', about: '', fields: '', bytes: '', values: [], sample: '' };
}
export function newCmd(code = 0): DCmd {
  return { key: key(), code: hex(code), name: '', reply: '0', wait: '', about: '', values: [], sample: '' };
}

export function draftBus(b: ChipBus): DBus {
  return {
    bus: b.bus, style: b.style ?? 'basic', addresses: [...(b.addresses ?? [])], ports: b.ports != null ? String(b.ports) : '',
    read_bit: hex(b.read_bit ?? 0x80), read_when_set: b.read_when_set ?? true, inc_bit: b.inc_bit != null ? hex(b.inc_bit) : '',
    mode: b.mode != null ? String(b.mode) : '', max_mhz: b.max_hz != null ? String(b.max_hz / 1e6) : '',
    reg_bytes: b.reg_bytes ?? 1, byte_order: b.byte_order ?? 'big',
    bank: b.bank ? { reg: hex(b.bank.reg), mask: hex(b.bank.mask), about: b.bank.about ?? '' } : null,
    registers: (b.registers ?? []).map((r) => ({ key: key(), addr: hex(r.addr), name: r.name, rw: r.rw ?? 'rw', about: r.about ?? '', fields: r.fields ?? '',
      bytes: r.bytes != null ? String(r.bytes) : '', values: dvalues(r.values), sample: '' })),
    commands: (b.commands ?? []).map((c) => ({ key: key(), code: hex(c.code), name: c.name, reply: String(c.reply ?? 0), wait: c.wait_ms != null ? String(c.wait_ms) : '',
      about: c.about ?? '', values: dvalues(c.values), sample: '' })),
  };
}

export function toDraft(t: ChipTable): Draft {
  return { name: t.name, vendor: t.vendor ?? '', kind: t.kind ?? '', category: t.category ?? '', tags: [...(t.tags ?? [])], datasheet: t.datasheet ?? '',
    datasheet_url: t.datasheet_url ?? '', notes: t.notes ?? '', buses: t.buses.map(draftBus) };
}

/** A second bus starts as a copy of the first one's table (most chips have the same registers on both). */
export function addBus(d: Draft, bus: 'i2c' | 'spi'): DBus {
  const from = d.buses[0];
  const b = from ? draftBus(fromBus(from, [])) : draftBus({ bus });
  b.bus = bus;
  if (bus === 'spi' && b.style === 'switch') b.style = 'basic';
  if (bus === 'i2c' && !b.addresses.length) b.addresses = [];
  return b;
}

function fromValues(vals: DValue[], where: string, errors: string[]): ChipValue[] | undefined {
  const out = vals.map((v, i) => {
    if (!v.name.trim()) errors.push(`${where}: reading ${i + 1} needs a name`);
    if (!v.expr.trim()) errors.push(`${where}: reading ${v.name || i + 1} needs a formula`);
    return { name: v.name.trim(), ...(v.unit.trim() ? { unit: v.unit.trim() } : {}), expr: v.expr.trim() };
  });
  return out.length ? out : undefined;
}

function fromBus(b: DBus, errors: string[]): ChipBus {
  const B = b.bus.toUpperCase();
  const out: ChipBus = { bus: b.bus, style: b.style };
  if (b.bus === 'i2c') {
    if (!b.addresses.length) errors.push('I2C: add at least one address');
    out.addresses = [...b.addresses];
    if (b.style === 'switch') {
      const p = num(b.ports || '8');
      if (p == null || p < 2 || p > 8) errors.push('I2C: a switch has 2 to 8 ports');
      else out.ports = p;
    }
  } else {
    const rb = num(b.read_bit);
    if (rb == null || rb < 1 || rb > 0xff) errors.push('SPI: the read/write bit is a number from 0x01 to 0xFF, e.g. 0x80');
    else out.read_bit = rb;
    out.read_when_set = b.read_when_set;
    if (b.inc_bit.trim()) {
      const ib = num(b.inc_bit);
      if (ib == null || ib < 1 || ib > 0xff) errors.push('SPI: the auto-increment bit is a number from 0x01 to 0xFF, or empty');
      else out.inc_bit = ib;
    }
    if (b.mode !== '') out.mode = Number(b.mode);
    if (b.max_mhz.trim()) {
      const mhz = Number(b.max_mhz);
      if (!(mhz > 0 && mhz <= 200)) errors.push('SPI: the highest clock is in MHz, e.g. 10');
      else out.max_hz = Math.round(mhz * 1e6);
    }
  }
  if (b.style === 'registers') {
    out.reg_bytes = b.reg_bytes;
    out.byte_order = b.byte_order;
    if (b.bank) {
      const reg = num(b.bank.reg), mask = num(b.bank.mask);
      if (reg == null || reg > 0xff || mask == null || mask < 1 || mask > 0xff) errors.push(`${B}: the register bank needs a register and a bit mask (0x01-0xFF)`);
      else out.bank = { reg, mask, ...(b.bank.about.trim() ? { about: b.bank.about.trim() } : {}) };
    }
  }
  if (b.style === 'registers') {
    const seen = new Set<number>();
    out.registers = b.registers.map((r, i): ChipRegister => {
      const where = `${B} register ${r.name || i + 1}`;
      const addr = num(r.addr);
      if (addr == null || addr > 0xffff) errors.push(`${where}: the address isn't a number (e.g. 0x04)`);
      else if (seen.has(addr)) errors.push(`${B}: register ${hex(addr)} is listed twice`);
      else seen.add(addr);
      if (!r.name.trim()) errors.push(`${B} register ${r.addr}: needs a name`);
      const bytes = r.bytes.trim() ? num(r.bytes) : null;
      if (r.bytes.trim() && (bytes == null || bytes < 1 || bytes > 64)) errors.push(`${where}: bytes is 1 to 64, or empty`);
      return { addr: addr ?? 0, name: r.name.trim(), rw: r.rw, ...(r.about.trim() ? { about: r.about.trim() } : {}),
        ...(r.fields.trim() ? { fields: r.fields.trim() } : {}), ...(bytes != null ? { bytes } : {}),
        ...(r.values.length ? { values: fromValues(r.values, where, errors) } : {}) };
    });
  }
  if (b.style === 'commands') {
    const seen = new Set<number>();
    out.commands = b.commands.map((c, i): ChipCommand => {
      const where = `${B} command ${c.name || i + 1}`;
      const code = num(c.code);
      if (code == null || code > 0xffff) errors.push(`${where}: the code isn't a number (e.g. 0xFD)`);
      else if (seen.has(code)) errors.push(`${B}: command ${hex(code)} is listed twice`);
      else seen.add(code);
      if (!c.name.trim()) errors.push(`${B} command ${c.code}: needs a name`);
      const reply = num(c.reply || '0');
      if (reply == null || reply > 64) errors.push(`${where}: the answer is 0 to 64 bytes`);
      const wait = c.wait.trim() ? Number(c.wait) : null;
      if (wait != null && !(wait >= 0 && wait <= 10000)) errors.push(`${where}: the wait is 0 to 10000 ms`);
      return { code: code ?? 0, name: c.name.trim(), reply: reply ?? 0, ...(wait != null ? { wait_ms: wait } : {}),
        ...(c.about.trim() ? { about: c.about.trim() } : {}), ...(c.values.length ? { values: fromValues(c.values, where, errors) } : {}) };
    });
  }
  return out;
}

/** The draft as a table, and what's wrong with it (empty: ready to save; the API checks the rest). Only the table
 *  that fits each bus's style is kept (registers or commands). */
export function fromDraft(d: Draft): { table: ChipTable; errors: string[] } {
  const errors: string[] = [];
  if (!d.name.trim()) errors.push('The chip needs a name');
  else if (!/^[A-Za-z0-9][A-Za-z0-9 ._+/-]*$/.test(d.name.trim())) errors.push('The name may hold letters, digits, spaces and . _ + / -');
  if (d.datasheet_url.trim() && !/^https?:\/\/\S+$/.test(d.datasheet_url.trim())) errors.push('The datasheet link starts with https://');
  if (!d.buses.length) errors.push('Add a bus (I2C or SPI)');
  const table: ChipTable = {
    name: d.name.trim(), ...(d.vendor.trim() ? { vendor: d.vendor.trim() } : {}), ...(d.kind.trim() ? { kind: d.kind.trim() } : {}),
    ...(d.category ? { category: d.category } : {}), ...(d.tags.length ? { tags: CHIP_TAGS.filter((x) => d.tags.includes(x)) } : {}), buses: d.buses.map((b) => fromBus(b, errors)),
    ...(d.datasheet.trim() ? { datasheet: d.datasheet.trim() } : {}), ...(d.datasheet_url.trim() ? { datasheet_url: d.datasheet_url.trim() } : {}),
    ...(d.notes.trim() ? { notes: d.notes.trim() } : {}),
  };
  return { table, errors };
}

const KEYS_HEX = new Set(['addr', 'code', 'reg', 'mask', 'read_bit', 'inc_bit']);

/** The table as JSON: addresses and register/command numbers in hex, as people and datasheets write them. */
export function toJson(t: ChipTable): string {
  return JSON.stringify(t, (k, v) => (KEYS_HEX.has(k) && typeof v === 'number' ? hex(v) : k === 'addresses' && Array.isArray(v) ? v.map((a: number) => hex(a)) : v), 2);
}

/** JSON back to a table: "0x44" strings become numbers. Throws with a readable message. */
export function fromJson(text: string): ChipTable {
  let t: unknown;
  try {
    t = JSON.parse(text, (k, v) => (typeof v === 'string' && /^0x[0-9a-f]+$/i.test(v) && (KEYS_HEX.has(k) || /^\d+$/.test(k)) ? parseInt(v, 16) : v));
  } catch (e) {
    throw new Error(`Not valid JSON: ${(e as Error).message}`);
  }
  const c = t as ChipTable;
  if (!c || typeof c !== 'object' || typeof c.name !== 'string' || !Array.isArray(c.buses)) throw new Error('Not a chip table: it needs a "name" and a "buses" list');
  return c;
}
