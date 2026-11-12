// The logic analyzer module on the Live page: is it installed, its decoded bus transactions (polled only while
// the Bus panel is open), "decode the last seconds", and the row the mouse is on (shaded on the chart).

import { api, type ModuleInfo } from './api';
import type { Chip, Chips } from './busfold';
import { devices } from './devices.svelte';
import { live } from './live.svelte';

const MAX_EVENTS = 6000; // a decode sends up to 5000
const POLL_MS = 1000;
const MODULES_EVERY_MS = 30000;
const WAITING_EVERY_MS = 5000; // installed and on, but no analyzer streaming (yet)
const LIVE_RENEW_MS = 10000; // live decoding is a 30 s lease in the module: renewed while a page shows the bus
const LIVE_KEY = 'pst.bus.live';

export type BusEvent = {
  seq: number;
  module: string;
  kind: 'i2c' | 'spi' | string;
  start_idx: number;
  end_idx: number | null;
  data: {
    label: string;
    addr?: number;
    parts?: { rw: 'R' | 'W'; addr: number; bytes: number[] }[];
    nack?: boolean;
    mosi?: number[];
    miso?: number[];
  };
};

export type BusSource = {
  events: BusEvent[];
  chips: Chips;
  msPerIdx: number;
  /** The time shown on a row, from its index. */
  clock: (idx: number) => string;
  /** In the head, where Live shows the analyzer's state. */
  status: string;
  /** In the bar: how many messages, and from where. */
  summary?: string;
  /** No messages: why (nothing decoded here, the analyzer was off, ...). */
  empty: string;
  /** Name a chip on the board the messages came from; none: they can't be named here. */
  setChip?: (key: string, chip: Chip) => Promise<void>;
  board?: string;
  /** The row under the mouse: its span in indexes, for the chart. */
  onhover?: (h: { a: number; b: number; label: string } | null) => void;
};

export type DecodeResult = { from: number; to: number; samples: number; decode_s: number; found: number; truncated: boolean };

class Bus {
  /** The analyzer module, if installed (null: not installed or not known yet). The sensor library's settings show
   * only with it. */
  analyzer = $state<ModuleInfo | null>(null);
  /** The module list was asked for at least once (until then analyzer null says nothing). */
  modulesKnown = $state(false);
  events = $state.raw<BusEvent[]>([]);
  busy = $state(false);
  error = $state('');
  last = $state<DecodeResult | null>(null);
  /** Chip names of the board on the bench (its id: chipsOf). */
  chips = $state<Chips>({});
  chipsOf = $state<number | null>(null);
  /** The bus row under the mouse: its span in stream indexes and a label, shaded on the current chart. */
  hover = $state<{ a: number; b: number; label: string } | null>(null);

  /** Decode live (the newest second, every second) while a page shows the bus; off by default, kept in the browser. */
  live = $state(((): boolean => { try { return localStorage.getItem(LIVE_KEY) === '1'; } catch { return false; } })());

  private seq = 0;
  private users = 0;
  private liveTimer: ReturnType<typeof setInterval> | undefined;
  private timer: ReturnType<typeof setInterval> | undefined;
  private modTimer: ReturnType<typeof setTimeout> | undefined;
  private watchers = 0;
  private stopWatch: (() => void) | undefined;

  /** Look for the module now and then while a page needs it (the Live page for the Bus panel, Settings for the sensor
   * library); more often while it has no analyzer to read, so plugging one in shows within seconds. */
  watch() {
    if (this.watchers++ === 0) {
      let stop = false;
      const load = async () => {
        try {
          const mods = await api.modules();
          this.analyzer = mods.find((m) => m.name === 'analyzer' && m.installed) ?? null;
        } catch { /* the api's own banner shows errors */ }
        this.modulesKnown = true;
        if (stop) return;
        const waiting = this.analyzer != null && this.analyzer.values?.enabled !== false && !this.analyzer.module?.health?.ok;
        this.modTimer = setTimeout(load, waiting ? WAITING_EVERY_MS : MODULES_EVERY_MS);
      };
      void load();
      this.stopWatch = () => { stop = true; clearTimeout(this.modTimer); };
    }
    return () => {
      if (--this.watchers === 0) this.stopWatch?.();
    };
  }

  /** The panel is open: poll new events until the returned cleanup. */
  use() {
    if (this.users++ === 0) {
      void this.poll();
      this.timer = setInterval(() => void this.poll(), POLL_MS);
      this.syncLive();
    }
    return () => {
      if (--this.users === 0) {
        clearInterval(this.timer);
        this.syncLive();
      }
    };
  }

  setLive(on: boolean) {
    this.live = on;
    try { localStorage.setItem(LIVE_KEY, on ? '1' : '0'); } catch { /* private window */ }
    if (!on) void api.analyzerLive(false).catch(() => {});
    this.syncLive();
  }

  /** Keep the module's live lease while it's wanted and a page shows the bus; it runs out by itself otherwise. */
  private syncLive() {
    clearInterval(this.liveTimer);
    this.liveTimer = undefined;
    if (!this.live || this.users === 0) return;
    const renew = () => void api.analyzerLive(true).catch(() => {});
    renew();
    this.liveTimer = setInterval(renew, LIVE_RENEW_MS);
  }

  private async poll() {
    try {
      const fresh = await api.moduleLiveEvents<BusEvent>('analyzer', this.seq);
      if (fresh.length) {
        this.seq = fresh[fresh.length - 1].seq;
        this.events = [...this.events, ...fresh].slice(-MAX_EVENTS);
      }
    } catch { /* not installed (any more) or not reachable: the tab says so */ }
  }

  async decode(seconds = 30) {
    this.busy = true;
    this.error = '';
    try {
      this.last = await api.analyzerDecode<DecodeResult>(seconds);
      await this.poll();
    } catch (e) {
      this.error = (e as Error).message;
    } finally {
      this.busy = false;
    }
  }

  /** Load the chip names of the board on the bench, once per board. */
  async loadChips() {
    const id = devices.activeId;
    if (id == null || id === this.chipsOf) return;
    try {
      this.chips = await api.busChips(id);
      this.chipsOf = id;
    } catch { /* shown as plain addresses */ }
  }

  /** Name a chip, give it a type from the sensor library or mark it as an I2C switch, for the board on the bench
   * (nothing left: the chip comes off the board's list). */
  async setChip(key: string, chip: Chip) {
    const id = devices.activeId;
    if (id == null) throw new Error('Pick the board on the bench first (top bar)');
    const next = { ...this.chips };
    const name = (chip.name ?? '').trim();
    const c = { ...(name ? { name } : {}), ...(chip.type ? { type: chip.type } : {}), ...(chip.switch != null ? { switch: chip.switch } : {}) };
    if (Object.keys(c).length) next[key] = c;
    else delete next[key];
    this.chips = await api.setBusChips(id, next);
    this.chipsOf = id;
  }

  clear() {
    this.events = [];
    this.last = null;
  }

  /** Unix time of a stream index, from the live buffer's clock. */
  wallOf(idx: number): number | null {
    const b = live.status?.buffer;
    return !b || b.end_wall == null ? null : b.end_wall - (b.end_idx - idx) * b.sample_period_s;
  }
}

export const bus = new Bus();
