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

export type DecodeResult = { from: number; to: number; samples: number; decode_s: number; found: number; truncated: boolean };

class Bus {
  /** The analyzer module, if installed (null: not installed or not known yet). */
  analyzer = $state<ModuleInfo | null>(null);
  events = $state.raw<BusEvent[]>([]);
  busy = $state(false);
  error = $state('');
  last = $state<DecodeResult | null>(null);
  /** Chip names of the board on the bench (its id: chipsOf). */
  chips = $state<Chips>({});
  chipsOf = $state<number | null>(null);
  /** The bus row under the mouse: its span in stream indexes and a label, shaded on the current chart. */
  hover = $state<{ a: number; b: number; label: string } | null>(null);

  private seq = 0;
  private users = 0;
  private timer: ReturnType<typeof setInterval> | undefined;
  private modTimer: ReturnType<typeof setTimeout> | undefined;

  /** Look for the module now and then (the Live page decides from this whether to show the Bus panel); more often
   * while it has no analyzer to read, so plugging one in shows within seconds. */
  watch() {
    let stop = false;
    const load = async () => {
      try {
        const mods = await api.modules();
        this.analyzer = mods.find((m) => m.name === 'analyzer' && m.installed) ?? null;
      } catch { /* the api's own banner shows errors */ }
      if (stop) return;
      const waiting = this.analyzer != null && this.analyzer.values?.enabled !== false && !this.analyzer.module?.health?.ok;
      this.modTimer = setTimeout(load, waiting ? WAITING_EVERY_MS : MODULES_EVERY_MS);
    };
    void load();
    return () => { stop = true; clearTimeout(this.modTimer); };
  }

  /** The panel is open: poll new events until the returned cleanup. */
  use() {
    if (this.users++ === 0) {
      void this.poll();
      this.timer = setInterval(() => void this.poll(), POLL_MS);
    }
    return () => {
      if (--this.users === 0) clearInterval(this.timer);
    };
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

  /** Name a chip (empty name: forget it) or mark it as an I2C switch, for the board on the bench. */
  async setChip(key: string, chip: Chip) {
    const id = devices.activeId;
    if (id == null) throw new Error('Pick the board on the bench first (top bar)');
    const next = { ...this.chips };
    const name = (chip.name ?? '').trim();
    if (name || chip.switch != null) next[key] = { ...(name ? { name } : {}), ...(chip.switch != null ? { switch: chip.switch } : {}) };
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
