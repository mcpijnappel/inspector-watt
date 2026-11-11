// The logic analyzer module on the Live page: is it installed, its decoded bus transactions (polled only while
// the Bus tab is on screen), and "decode the last seconds".

import { api, type ModuleInfo } from './api';
import { live } from './live.svelte';

const MAX_EVENTS = 2000;
const POLL_MS = 1000;
const MODULES_EVERY_MS = 30000;

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

  private seq = 0;
  private users = 0;
  private timer: ReturnType<typeof setInterval> | undefined;
  private modTimer: ReturnType<typeof setInterval> | undefined;

  /** Look for the module now and then (the Live page decides from this whether to show the Bus tab). */
  watch() {
    const load = async () => {
      try {
        const mods = await api.modules();
        this.analyzer = mods.find((m) => m.name === 'analyzer' && m.installed) ?? null;
      } catch { /* the api's own banner shows errors */ }
    };
    void load();
    this.modTimer = setInterval(load, MODULES_EVERY_MS);
    return () => clearInterval(this.modTimer);
  }

  /** The tab is on screen: poll new events until the returned cleanup. */
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

  async decode(seconds = 5) {
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
