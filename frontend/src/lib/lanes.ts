// Chip lanes under the current: the bus messages on the chart's time axis. One lane per bus (I2C, SPI) with a tick
// per message in its chip's colour; a bus opens into one lane per chip, and a pinned chip keeps its own lane.

import type { BusEvent } from './bus.svelte';
import { switchesOf, toItems, type Chips, type Item } from './busfold';
import { namer, type Namer } from './busnames';
import { library, onBus } from './chips.svelte';

const MS_PER_IDX = 0.01; // the PPK2's 100 kS/s: stream and session indexes alike
const PREF = 'pst.lanes';

export type LaneMark = { x: number; x2: number; nack: boolean; text: string; color: string; who: string };
/** One chip's messages. side: Compare's A or B. */
export type Lane = { chip: string; bus: 'i2c' | 'spi'; label: string; title: string; color: string; marks: LaneMark[]; side?: 'a' | 'b' };
/** What's drawn: a bus row (all its chips) or a chip row. */
export type Row = { key: string; kind: 'bus' | 'chip'; bus: 'i2c' | 'spi'; chip?: string; label: string; title: string; side?: 'a' | 'b'; marks: LaneMark[]; first: boolean; count?: number };
/** The chart's x scale and where its plot area is (CSS pixels from the chart's left edge). */
export type ChartScale = { min: number; max: number; left: number; width: number };
/** A stretch the analyzer decoded, or couldn't (missed: why), in chart x. */
export type LaneWindow = { a: number; b: number; missed?: string };

// Chip colours: apart from each other and from the NACK red, readable on light and dark
export const CHIP_COLORS = ['#8250df', '#0969da', '#1a7f37', '#d4760a', '#bf3989', '#0a8a9c', '#7d6200', '#5a6ad6', '#a0522d', '#6e7781'];

/** Lanes shown under the charts (on unless switched off; kept in the browser). */
export function lanesPref(): boolean {
  try { return localStorage.getItem(PREF) !== '0'; } catch { return true; }
}
export function setLanesPref(on: boolean) {
  try { localStorage.setItem(PREF, on ? '1' : '0'); } catch { /* private window */ }
}

/** Chips in the order the Bus panel shows them: the main bus by how much it talks, then the switch ports, SPI last. */
function order(items: Item[]): string[] {
  const n = new Map<string, number>();
  for (const it of items) n.set(it.key, (n.get(it.key) ?? 0) + 1);
  const keys = [...n.keys()];
  const main = keys.filter((k) => k.startsWith('m:')).sort((a, b) => n.get(b)! - n.get(a)!);
  const ports = keys.filter((k) => !k.startsWith('m:') && k !== 'spi').sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  return [...main, ...ports, ...keys.filter((k) => k === 'spi')];
}

// behind the switch the port goes first, so it stays readable when the name is cut short
const label = (nm: Namer, it: Item) => (it.port != null ? `${it.port}│` : '') + nm.nameOf(it.key, it.addr);
const title = (nm: Namer, it: Item) => `${nm.nameOf(it.key, it.addr)}${it.addr != null ? ` (${it.port != null ? `port ${it.port}, ` : ''}0x${it.addr.toString(16).toUpperCase().padStart(2, '0')})` : ''}`;

/** xOf: chart x of a message's stream (or session) index. colorOf: a chip's colour (the same in A and B). */
export function lanesOf(items: Item[], nm: Namer, xOf: (idx: number) => number, colorOf: (chip: string, i: number) => string): Lane[] {
  const by = new Map<string, Item[]>();
  for (const it of items) (by.get(it.key) ?? by.set(it.key, []).get(it.key)!).push(it);
  return order(items).map((chip, i) => {
    const its = by.get(chip)!, color = colorOf(chip, i), who = title(nm, its[0]);
    return {
      chip, bus: chip === 'spi' ? 'spi' : 'i2c', color, label: label(nm, its[0]), title: who,
      marks: its.map((it) => ({ x: xOf(it.idx), x2: xOf(it.end), nack: it.nack || (it.kind === 'probe' && !it.ok), text: nm.text(it), color, who })),
    };
  });
}

/** Messages (as the analyzer sends them) -> lanes, named from the board's chips and the sensor library. */
export function lanesFor(events: BusEvent[], chips: Chips, xOf: (idx: number) => number,
  colorOf: (chip: string, i: number) => string = (_c, i) => CHIP_COLORS[i % CHIP_COLORS.length]): Lane[] {
  if (!events.length) return [];
  const switches = switchesOf(events, chips, library.switchTypes);
  const spi = onBus(library.get(chips.spi?.type), 'spi') as { read_bit: number } | undefined;
  return lanesOf(toItems(events, switches, MS_PER_IDX, false, spi), namer(chips, switches), xOf, colorOf);
}

/** Compare: A's and B's lanes, a chip keeping one colour on both sides. */
export function pairLanes(events: [BusEvent[], BusEvent[]], chips: [Chips, Chips], xOf: [(idx: number) => number, (idx: number) => number]): Lane[] {
  const colors = new Map<string, string>();
  const colorOf = (chip: string) => colors.get(chip) ?? colors.set(chip, CHIP_COLORS[colors.size % CHIP_COLORS.length]).get(chip)!;
  const a = lanesFor(events[0], chips[0], xOf[0], colorOf).map((l) => ({ ...l, side: 'a' as const }));
  const b = lanesFor(events[1], chips[1], xOf[1], colorOf).map((l) => ({ ...l, side: 'b' as const }));
  return [...a, ...b];
}

const BUS_NAME = { i2c: 'I2C', spi: 'SPI' };

/** What to draw: per bus (I2C first) a bus row per side, then its chips' rows when the bus is open, or only the
 * pinned ones. A chip's A and B rows sit together. */
export function rows(lanes: Lane[], open: Set<string>, pinned: Set<string>): Row[] {
  const out: Row[] = [];
  for (const bus of ['i2c', 'spi'] as const) {
    const ls = lanes.filter((l) => l.bus === bus);
    if (!ls.length) continue;
    const sides = [...new Set(ls.map((l) => l.side))];
    const chipCount = new Set(ls.map((l) => l.chip)).size;
    sides.forEach((side, i) => out.push({
      key: `bus|${bus}|${side ?? ''}`, kind: 'bus', bus, side, first: i === 0, count: chipCount,
      label: i === 0 ? BUS_NAME[bus] : '', title: `${BUS_NAME[bus]}${side ? ` · ${side.toUpperCase()}` : ''}: ${chipCount} chip${chipCount === 1 ? '' : 's'}`,
      marks: ls.filter((l) => l.side === side).flatMap((l) => l.marks).sort((x, y) => x.x - y.x),
    }));
    const chips = [...new Set(ls.map((l) => l.chip))].filter((c) => open.has(bus) || pinned.has(c));
    for (const chip of chips) {
      ls.filter((l) => l.chip === chip).forEach((l, i) => out.push({
        key: `chip|${chip}|${l.side ?? ''}`, kind: 'chip', bus, chip, side: l.side, first: i === 0,
        label: i === 0 ? l.label : '', title: l.title, marks: l.marks,
      }));
    }
  }
  return out;
}
