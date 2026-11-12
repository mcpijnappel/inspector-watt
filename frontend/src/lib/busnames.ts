// Names for decoded bus messages: which chip (the board's chip list, else its type from the sensor library, else
// its address), and what the library knows about a message: the register's or command's name and readings from its
// bytes. Used by the Bus panel and the chip lanes (Live, Session, Compare). The api does the same for agents
// (api/src/api/busnames.py): keep the two in step.

import type { Chip, ChipValue } from './api';
import { ax, hx, type Chips, type Item } from './busfold';
import { library, onBus } from './chips.svelte';
import { evaluate, fmtValue } from './formula';

export type Namer = {
  nameOf: (key: string, addr: number | null) => string;
  typeOf: (key: string, addr: number | null) => Chip | undefined;
  known: (it: Item) => { name?: string; values: string[] };
  nameIn: (it: Item) => string | undefined;
  /** One line for a message: "ALS → 412 counts", "measure (high precision) → 21.4 °C · 48.0 %RH". */
  text: (it: Item) => string;
};

/** A port not known ('?:16'): what the port chips at that address share, if they all share it. */
function shared<T>(chips: Chips, addr: number | null, pick: (c: Chips[string]) => T): T | undefined {
  const vals = new Set(Object.entries(chips).filter(([k]) => /^[0-7]:/.test(k) && Number(k.split(':')[1]) === addr).map(([, c]) => pick(c)));
  return vals.size === 1 ? [...vals][0] : undefined;
}

export function namer(chips: Chips, switches: Set<number>): Namer {
  const typeOf = (key: string, addr: number | null): Chip | undefined =>
    key.startsWith('?:') ? library.get(shared(chips, addr, (c) => c.type)) : library.get(chips[key]?.type);
  const nameOf = (key: string, addr: number | null) =>
    chips[key]?.name || (key.startsWith('?:') ? shared(chips, addr, (c) => c.name) : undefined) || typeOf(key, addr)?.name ||
    (key.startsWith('m:') && switches.has(addr ?? -1) ? 'I2C switch' : key === 'spi' ? 'SPI' : ax(addr ?? 0));
  function known(it: Item): { name?: string; values: string[] } {
    // the chip's table for the bus this message was on
    const t = onBus(typeOf(it.key, it.addr), it.key === 'spi' ? 'spi' : 'i2c');
    if (!t) return { values: [] };
    const read = (vals: ChipValue[] | undefined, bytes: number[] | undefined) => (vals ?? []).flatMap((v) => {
      const x = bytes ? evaluate(v.expr, bytes) : null;
      return x == null ? [] : [`${fmtValue(x)}${v.unit ? ` ${v.unit}` : ''}`];
    });
    if ((it.kind === 'reg' || it.kind === 'set') && t.registers) {
      // on SPI only the address bits go over the wire (BME280: 0xF7 is sent as 0x77 / 0xF7): compare those
      const mask = t.bus === 'spi' ? 0xff & ~(t.read_bit ?? 0x80) & ~(t.inc_bit ?? 0) : 0xffff;
      const r = t.registers.find((x) => (x.addr & mask) === it.reg?.[0]);
      return { name: r?.name, values: it.kind === 'reg' ? read(r?.values, it.r) : [] };
    }
    if ((it.kind === 'cmd' || it.kind === 'write') && t.commands) {
      const c = t.commands.find((x) => x.code === it.w?.[0]);
      return { name: c?.name, values: it.kind === 'cmd' ? read(c?.values, it.r) : [] };
    }
    return { values: [] };
  }
  function text(it: Item): string {
    const k = known(it);
    const res = (bytes: number[] | undefined) => (k.values.length ? k.values.join(' · ') : hx(bytes));
    switch (it.kind) {
      case 'switch': return it.text ?? 'switch';
      case 'probe': return it.ok ? 'answers' : 'not found (NACK)';
      case 'reg': return `reads ${k.name ?? hx(it.reg)} → ${res(it.r)}`;
      case 'set': return `sets ${k.name ?? hx(it.reg)} ← ${hx(it.w)}`;
      case 'cmd': return `${k.name ?? 'command ' + hx(it.w)} → ${res(it.r)}`;
      case 'write': return k.name ?? `write ${hx(it.w)}`;
      case 'spi': return [it.mosi?.length ? `MOSI ${hx(it.mosi)}` : '', it.miso?.length ? `MISO ${hx(it.miso)}` : ''].filter(Boolean).join(' · ');
      default: return `read ${hx(it.r)}`;
    }
  }
  return { nameOf, typeOf, known, nameIn: (it) => known(it).name, text };
}
