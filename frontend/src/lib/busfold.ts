// Decoded bus messages -> what the Bus tab shows: each message gets its chip (where it sits and its address),
// a kind (register read, register set, command and its answer, presence check) and folds into loops and runs.
//
// - Chip: "m:<address>" on the main bus, "<port>:<address>" behind an I2C switch. A switch's one-byte writes open
//   ports (bit n = port n); a chip seen while the switch has closed all ports is on the main bus, wherever it shows
//   up later. Before the first switch write (or after a long gap) the open port isn't known: a chip that shows up
//   behind a port later in the window is "?:<address>" there, anything else counts as the main bus.
// - Loop: a sequence one chip repeats 3+ times (1-6 messages), then sequences of those (up to 40 steps), so a status
//   poll folds inside a measurement round that folds too.
// - Run: one chip's steps in a row (4 or more) become one row.

import type { BusEvent } from './bus.svelte';

export type Chip = { name?: string; switch?: boolean; type?: string };
/** How an SPI chip's first byte works: register address, with a read/write bit (and maybe an auto-increment bit). */
export type SpiFormat = { read_bit: number; read_when_set?: boolean; inc_bit?: number };
export type Chips = Record<string, Chip>;

export type Item = {
  t: number; // ms
  idx: number; // stream index of the message
  end: number; // stream index where it ended (a command: where its answer ended)
  key: string; // m:68, 0:16, spi
  addr: number | null;
  port: string | null;
  kind: 'probe' | 'reg' | 'set' | 'cmd' | 'write' | 'read' | 'switch' | 'spi';
  ok?: boolean; // probe: answered
  nack: boolean;
  reg?: number[];
  w?: number[];
  r?: number[];
  wait?: number; // cmd: ms between the command and its answer
  text?: string; // switch
  mosi?: number[];
  miso?: number[];
  sig: string;
};
export type Node =
  | { it: Item }
  | { loop: true; k: number; r: number; children: Node[] }
  | { run: true; children: Node[] };

export const hx = (b: number[] = []) => b.map((x) => x.toString(16).toUpperCase().padStart(2, '0')).join(' ');
export const ax = (a: number) => '0x' + a.toString(16).toUpperCase().padStart(2, '0');

/** Addresses that look like an I2C switch: 0x70-0x77 and only ever one-byte writes. */
export function guessSwitches(events: BusEvent[]): Set<number> {
  const seen = new Map<number, boolean>();
  for (const e of events) {
    const a = e.data.addr;
    if (e.kind !== 'i2c' || a == null || a < 0x70 || a > 0x77) continue;
    const p = e.data.parts ?? [];
    const one = p.length === 1 && p[0].rw === 'W' && p[0].bytes.length === 1;
    seen.set(a, (seen.get(a) ?? true) && one);
  }
  return new Set([...seen].filter(([, one]) => one).map(([a]) => a));
}

/** The I2C switches: guessed from the traffic, unless the board's chip list says otherwise (an explicit switch flag,
 * or a chip type from the library: `switchTypes` holds the lower-case names of the switch types). */
export function switchesOf(events: BusEvent[], chips: Chips, switchTypes: Set<string> = new Set()): Set<number> {
  const out = guessSwitches(events);
  for (const [key, c] of Object.entries(chips)) {
    const a = Number(key.split(':')[1]);
    if (!key.startsWith('m:')) continue;
    const isSwitch = c.switch ?? (c.type ? switchTypes.has(c.type.toLowerCase()) : null);
    if (isSwitch == null) continue;
    if (isSwitch) out.add(a); else out.delete(a);
  }
  return out;
}

const isPortWrite = (e: BusEvent, switches: Set<number>) => {
  const p = e.data.parts ?? [];
  return e.kind === 'i2c' && switches.has(e.data.addr ?? -1) && p.length === 1 && p[0].rw === 'W' && p[0].bytes.length === 1;
};

/** Where the switch stands: no port open, a port (or ports) open, or not known (a decode starts or resumes in the
 * middle of things: the port may have been opened just before). */
type SwitchState = { kind: 'unknown' } | { kind: 'closed' } | { kind: 'open'; port: string };
const RESYNC_GAP_MS = 2000; // a gap this long is probably another decode window: the switch state is unknown again

/** Messages -> items. `msPerIdx`: stream index to milliseconds. `showSwitch`: keep the port writes as rows.
 * `spi`: the board's SPI chip's format, so its transfers become register reads and writes. */
export function toItems(events: BusEvent[], switches: Set<number>, msPerIdx: number, showSwitch = false, spi?: SpiFormat): Item[] {
  // Pass 1: which addresses are on the main bus (seen while all ports are closed) and which behind a known port.
  const main = new Set<number>(), behind = new Set<number>();
  const walk = (visit: (e: BusEvent, i: number, st: SwitchState) => void) => {
    let st: SwitchState = { kind: 'unknown' }, last = -Infinity;
    events.forEach((e, i) => {
      if ((e.start_idx - last) * msPerIdx > RESYNC_GAP_MS) st = { kind: 'unknown' };
      last = e.start_idx;
      if (isPortWrite(e, switches)) {
        const b = e.data.parts![0].bytes[0];
        st = b === 0 ? { kind: 'closed' } : { kind: 'open', port: [0, 1, 2, 3, 4, 5, 6, 7].filter((k) => b & (1 << k)).join('+') };
      }
      visit(e, i, st);
    });
  };
  walk((e, _i, st) => {
    const a = e.data.addr;
    if (a == null || isPortWrite(e, switches)) return;
    if (st.kind === 'closed') main.add(a);
    else if (st.kind === 'open') behind.add(a);
  });
  /** null: main bus; '?': behind the switch, port not known */
  const portOf = (a: number, st: SwitchState): string | null => {
    if (!switches.size || main.has(a)) return null;
    if (st.kind === 'open') return st.port;
    return st.kind === 'unknown' && behind.has(a) ? '?' : null;
  };

  // Pass 2: the items
  const out: Item[] = [];
  const used = new Set<number>();
  const states: SwitchState[] = [];
  walk((_e, i, st) => { states[i] = st; });
  for (let i = 0; i < events.length; i++) {
    const e = events[i], t = e.start_idx * msPerIdx;
    if (used.has(i)) continue;
    if (e.kind === 'spi') {
      const mosi = e.data.mosi ?? [], miso = e.data.miso ?? [];
      const base = { t, idx: e.start_idx, end: e.end_idx ?? e.start_idx, key: 'spi', addr: null, port: null, nack: false };
      if (spi && mosi.length) {
        // first byte: the register address and read/write bit; the rest is the data (MISO read, MOSI written)
        const bit = spi.read_bit ?? 0x80;
        const set = (mosi[0] & bit) !== 0;
        const read = (spi.read_when_set ?? true) ? set : !set;
        const reg = [mosi[0] & 0xff & ~bit & ~(spi.inc_bit ?? 0)];
        out.push(read ? { ...base, kind: 'reg', reg, r: miso.slice(1), sig: `reg||${hx(reg)}|` }
          : { ...base, kind: 'set', reg, w: mosi.slice(1), sig: `set||${hx(reg)}|${hx(mosi.slice(1))}` });
      } else out.push({ ...base, kind: 'spi', mosi, miso, sig: `spi|${hx(mosi.slice(0, 4))}` });
      continue;
    }
    const a = e.data.addr ?? 0, parts = e.data.parts ?? [], nack = !!e.data.nack, st = states[i];
    if (isPortWrite(e, switches)) {
      if (showSwitch) out.push({ t, idx: e.start_idx, end: e.end_idx ?? e.start_idx, key: `m:${a}`, addr: a, port: null, kind: 'switch', nack, sig: `sw|${parts[0].bytes[0]}`,
        text: st.kind === 'open' ? `open port ${st.port}` : 'all ports off' });
      continue;
    }
    const port = portOf(a, st);
    const base = { t, idx: e.start_idx, end: e.end_idx ?? e.start_idx, key: `${port ?? 'm'}:${a}`, addr: a, port, nack };
    const [p0, p1] = parts;
    let it: Omit<Item, 'sig'>;
    if (!p0) continue;
    if (parts.length === 1 && p0.rw === 'W' && p0.bytes.length === 0) it = { ...base, kind: 'probe', ok: !nack };
    else if (parts.length === 2 && p0.rw === 'W' && p1.rw === 'R') it = { ...base, kind: 'reg', reg: p0.bytes, r: p1.bytes };
    else if (parts.length === 1 && p0.rw === 'W' && p0.bytes.length >= 2) it = { ...base, kind: 'set', reg: [p0.bytes[0]], w: p0.bytes.slice(1) };
    else if (parts.length === 1 && p0.rw === 'W') {
      // A command whose answer is a separate read a little later (SHT4x: FD, then 6 bytes after 10 ms)
      let j = -1;
      for (let n = i + 1; n < Math.min(events.length, i + 6); n++) {
        const x = events[n], xp = x.data.parts ?? [];
        if ((x.start_idx - e.start_idx) * msPerIdx > 100) break;
        if (x.data.addr === a && xp.length === 1 && xp[0].rw === 'R') { j = n; break; }
      }
      if (j >= 0) {
        used.add(j);
        it = { ...base, end: events[j].end_idx ?? events[j].start_idx, kind: 'cmd', w: p0.bytes, r: events[j].data.parts![0].bytes, wait: (events[j].start_idx - e.start_idx) * msPerIdx };
      } else it = { ...base, kind: 'write', w: p0.bytes };
    } else it = { ...base, kind: 'read', r: parts.flatMap((p) => p.bytes) };
    const wrote = it.kind === 'set' || it.kind === 'cmd' || it.kind === 'write' ? it.w : [];
    out.push({ ...it, sig: [it.kind, it.ok, hx(it.reg), hx(wrote)].join('|') } as Item);
  }
  return out;
}

export const leaf = (n: Node): Item => ('it' in n ? n.it : leaf(n.children[0]));
export const leaves = (n: Node): Item[] => ('it' in n ? [n.it] : n.children.flatMap(leaves));
const sigOf = (n: Node): string => ('it' in n ? n.it.sig : 'loop' in n ? `L[${n.children.slice(0, n.k).map(sigOf).join(',')}]` : 'R');

function foldLoops(nodes: Node[], maxK: number): Node[] {
  const byChip = new Map<string, number[]>();
  nodes.forEach((n, i) => {
    const k = leaf(n).key;
    (byChip.get(k) ?? byChip.set(k, []).get(k)!).push(i);
  });
  const sigs = nodes.map(sigOf), at = new Map<number, Node>(), skip = new Set<number>();
  for (const idx of byChip.values()) {
    let i = 0;
    while (i < idx.length) {
      let best: { k: number; r: number } | null = null;
      for (let k = 1; k <= maxK && i + 3 * k <= idx.length; k++) {
        let r = 1;
        const same = (rep: number) => { for (let j = 0; j < k; j++) if (sigs[idx[i + rep * k + j]] !== sigs[idx[i + j]]) return false; return true; };
        while (i + (r + 1) * k <= idx.length && same(r)) r++;
        if (r >= 3 && (!best || r * k > best.r * best.k)) best = { k, r };
      }
      if (!best) { i++; continue; }
      const span = idx.slice(i, i + best.r * best.k);
      at.set(span[0], { loop: true, k: best.k, r: best.r, children: span.map((j) => nodes[j]) });
      for (const j of span.slice(1)) skip.add(j);
      i += best.r * best.k;
    }
  }
  const out: Node[] = [];
  nodes.forEach((n, i) => { if (at.has(i)) out.push(at.get(i)!); else if (!skip.has(i)) out.push(n); });
  return out;
}

function runs(nodes: Node[]): Node[] {
  const out: Node[] = [];
  for (const n of nodes) {
    const last = out[out.length - 1];
    if (last && leaf(last).key === leaf(n).key) {
      if ('run' in last) last.children.push(n);
      else out[out.length - 1] = { run: true, children: [last, n] };
    } else out.push(n);
  }
  return out.flatMap((n) => ('run' in n && n.children.length < 4 ? n.children : [n]));
}

export const fold = (items: Item[]): Node[] => runs(foldLoops(foldLoops(items.map((it) => ({ it })), 6), 40));

/** The usual spacing of a loop's rounds, in ms. */
export function every(n: Extract<Node, { loop: true }>): number {
  const starts = n.children.filter((_, i) => i % n.k === 0).map((c) => leaf(c).t);
  const gaps = starts.slice(1).map((x, i) => x - starts[i]).sort((x, y) => x - y);
  return gaps[Math.floor(gaps.length / 2)] ?? 0;
}

/** A fast poll: a loop every < 20 ms with 100+ messages. The fastest one inside, or null. */
export function pollEvery(n: Node): number | null {
  if ('it' in n) return null;
  let best: number | null = null;
  if ('loop' in n) {
    const e = every(n);
    if (e < 20 && leaves(n).length >= 100) best = e;
  }
  for (const c of n.children) {
    const e = pollEvery(c);
    if (e != null && (best == null || e < best)) best = e;
  }
  return best;
}

/** "reads 00, 03 · sets 00, 03": what a group of a chip's steps does. `name`: a register's or command's name from the
 * sensor library, when it has one ("reads STATUS2 · sets CFG0"). */
export function summary(its: Item[], name: (it: Item) => string | undefined = () => undefined): string[] {
  const reads = new Set<string>(), sets = new Set<string>(), other = new Set<string>();
  for (const it of its) {
    if (it.kind === 'reg') reads.add(name(it) ?? hx(it.reg));
    else if (it.kind === 'set') sets.add(name(it) ?? hx(it.reg));
    else if (it.kind === 'cmd' || it.kind === 'write') other.add(name(it) ?? 'command ' + hx(it.w));
    else if (it.kind === 'probe') other.add(it.ok ? 'answers' : 'not found');
    else if (it.kind === 'read') other.add('read');
    else if (it.kind === 'spi') other.add('transfers');
    else other.add(it.text ?? it.kind);
  }
  const list = (s: Set<string>, n = 6) => [...s].slice(0, n).join(', ') + (s.size > n ? ` +${s.size - n}` : '');
  return [other.size ? list(other, 3) : '', reads.size ? 'reads ' + list(reads) : '', sets.size ? 'sets ' + list(sets) : ''].filter(Boolean);
}

/** Per chip: how many messages, and whether it ever answered. */
export function chipStats(items: Item[]): Map<string, { n: number; ok: boolean; addr: number | null; port: string | null }> {
  const out = new Map<string, { n: number; ok: boolean; addr: number | null; port: string | null }>();
  for (const it of items) {
    const s = out.get(it.key) ?? out.set(it.key, { n: 0, ok: false, addr: it.addr, port: it.port }).get(it.key)!;
    s.n++;
    if (!(it.kind === 'probe' && !it.ok)) s.ok = true;
  }
  return out;
}
