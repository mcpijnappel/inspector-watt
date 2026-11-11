// Live data: device status (polled), current (WebSocket, min/max/mean bins) and UART lines.
//
// x values are "stream seconds": PPK2 sample index × 10 µs since the stream started. UART lines carry the
// same sample index, so lines and current share one time axis.
//
// Following: new bins arrive over the WebSocket and the window scrolls.
// Paused: the view is frozen at a moment; window changes, zooming and selections fetch exact data for that
// moment from the capture service's RAM buffer (the last 5 minutes).

import { api, type BufferWindow, type RangeStats, type Status, type UartLine } from './api';

export const PERIOD = 1e-5;
const POINTS = 1500; // bins across the visible window
const MAX_LINES = 2000;
export const BUFFER_S = 300;

export type LiveLine = UartLine & { x: number | null };
export type Range = { a: number; b: number }; // stream seconds

type PpkMsg = { type: 'ppk'; generation: number; start: number; bin_samples: number; min: (number | null)[]; max: (number | null)[]; mean: (number | null)[] };
type UartMsg = { type: 'uart'; lines: UartLine[] };
type StatusMsg = { type: 'status'; ppk2: Status['ppk2']; uart: Status['uart']; usb?: Status['usb']; flash?: Status['flash'] };

const idx = (s: number) => Math.round(s / PERIOD);

class LiveStore {
  status = $state<Status | null>(null);
  statusError = $state<string | null>(null);
  wsOpen = $state(false);
  windowS = $state(loadWindow());
  /** Bumped whenever the chart arrays change (they are plain arrays for speed). */
  version = $state(0);
  lines = $state.raw<LiveLine[]>([]);

  paused = $state(false);
  /** The range shown while paused (stream seconds). */
  view = $state<Range | null>(null);
  zoomDepth = $state(0);
  selection = $state<Range | null>(null);
  selStats = $state<RangeStats | null>(null);
  loading = $state(false);
  message = $state('');

  x: number[] = [];
  mn: (number | null)[] = [];
  mx: (number | null)[] = [];
  avg: (number | null)[] = [];

  private ws: WebSocket | null = null;
  private epoch = 0;
  private gen = -1;
  private uartSeq = -1;
  private retry = 0;
  private zoomStack: Range[] = [];
  private selToken = 0;
  private statusTimer: ReturnType<typeof setInterval> | undefined;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private started = false;

  start() {
    if (this.started) return;
    this.started = true;
    void this.pollStatus();
    this.statusTimer = setInterval(() => void this.pollStatus(), 1000);
    void this.connect();
  }

  stop() {
    clearInterval(this.statusTimer);
    clearTimeout(this.reconnectTimer);
    this.epoch++;
    this.ws?.close();
    this.started = false;
  }

  async pollStatus() {
    try {
      this.status = await api.status(true);
      this.statusError = null;
    } catch (e) {
      this.statusError = (e as Error).message;
    }
  }

  /** Unix time of a stream time, from the newest status (null if unknown). */
  wallOf(s: number): number | null {
    const b = this.status?.buffer;
    if (!b || b.end_wall == null) return null;
    return b.end_wall - (b.end_idx * PERIOD - s);
  }

  /** Oldest stream time still in the RAM buffer. */
  get bufferStart(): number {
    return (this.status?.buffer.start_idx ?? 0) * PERIOD;
  }

  // window, pause, zoom -------------------------------------------------------------------------------------
  setWindow(seconds: number) {
    this.windowS = seconds;
    saveWindow(seconds);
    this.zoomStack = [];
    this.zoomDepth = 0;
    if (this.paused && this.view) void this.loadView({ a: this.view.b - seconds, b: this.view.b });
    else void this.connect();
  }

  pause() {
    if (this.paused) return;
    this.paused = true;
    const n = this.x.length;
    this.view = n ? { a: this.x[0], b: this.x[n - 1] } : null;
  }

  resume() {
    this.paused = false;
    this.view = null;
    this.zoomStack = [];
    this.zoomDepth = 0;
    this.clearSelection();
    void this.connect();
  }

  zoomTo(r: Range) {
    if (r.b - r.a < 20 * PERIOD) r = { a: (r.a + r.b) / 2 - 10 * PERIOD, b: (r.a + r.b) / 2 + 10 * PERIOD };
    this.pause();
    if (this.view) this.zoomStack.push({ ...this.view });
    this.zoomDepth = this.zoomStack.length;
    this.clearSelection();
    void this.loadView(r);
  }

  zoomOut() {
    const prev = this.zoomStack.pop();
    this.zoomDepth = this.zoomStack.length;
    this.clearSelection();
    if (prev) void this.loadView(prev);
  }

  private async loadView(r: Range) {
    const a = Math.max(r.a, this.bufferStart);
    if (r.b <= a) {
      this.message = 'That part is no longer in the live buffer (it keeps the last 5 minutes).';
      return;
    }
    this.loading = true;
    try {
      const w = await api.window(idx(a), idx(r.b), POINTS);
      this.view = { a, b: r.b };
      this.x = [];
      this.mn = [];
      this.mx = [];
      this.avg = [];
      this.appendWindow(w, false);
      this.message = a > r.a ? 'Part of this range is no longer in the live buffer.' : '';
    } catch (e) {
      this.message = (e as Error).message;
    } finally {
      this.loading = false;
    }
  }

  // selection ----------------------------------------------------------------------------------------------
  async select(r: Range) {
    this.pause();
    this.selection = r;
    this.selStats = null;
    const token = ++this.selToken; // $state wraps r in a proxy, so compare tokens, not objects
    try {
      const s = await api.rangeStats(idx(r.a), idx(r.b));
      if (token === this.selToken) this.selStats = s;
    } catch (e) {
      this.message = (e as Error).message;
    }
  }

  clearSelection() {
    this.selToken++;
    this.selection = null;
    this.selStats = null;
  }

  // 1 cycle ------------------------------------------------------------------------------------------------
  /** Find the wake-to-wake period in the buffer and show exactly one cycle. */
  async showOneCycle(thresholdA: number) {
    this.message = '';
    const st = this.status ?? (await api.status());
    const end = st.buffer.end_idx;
    const start = Math.max(st.buffer.start_idx, end - BUFFER_S / PERIOD);
    const w = await api.window(start, end, 3000);
    const mean = w.mean ?? w.current ?? [];
    const k = w.bin_samples;
    const starts: number[] = [];
    let below = true;
    for (let i = 0; i < mean.length; i++) {
      const v = mean[i];
      if (v == null) continue;
      if (below && v > thresholdA) {
        const t = (w.start + i * k) * PERIOD;
        if (!starts.length || t - starts[starts.length - 1] > 1) starts.push(t);
        below = false;
      } else if (v <= thresholdA) below = true;
    }
    if (starts.length < 2) {
      this.message = `No repeating wake above ${thresholdA * 1e3} mA in the last ${BUFFER_S / 60} minutes.`;
      return;
    }
    const period = starts[starts.length - 1] - starts[starts.length - 2];
    // Show the last complete cycle, starting just before its wake.
    const a = starts[starts.length - 2] - Math.min(1, period * 0.02);
    if (this.paused) void this.loadView({ a, b: a + period });
    else {
      this.windowS = Math.ceil(period * 1.02);
      saveWindow(this.windowS);
      void this.connect();
    }
    this.message = `One cycle: ${period.toFixed(1)} s from wake to wake.`;
  }

  // streaming ---------------------------------------------------------------------------------------------
  refresh() {
    void this.connect();
  }

  private binSamples() {
    return Math.max(1, Math.round(this.windowS / PERIOD / POINTS));
  }

  private clear() {
    this.x = [];
    this.mn = [];
    this.mx = [];
    this.avg = [];
    this.version++;
  }

  private async connect() {
    const epoch = ++this.epoch;
    clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
    this.wsOpen = false;
    try {
      await this.backfill(epoch);
    } catch {
      /* api not reachable yet: the WebSocket retry below keeps trying */
    }
    if (epoch !== this.epoch) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/live?bin_us=${this.binSamples() * 10}`);
    this.ws = ws;
    ws.onopen = () => {
      if (epoch !== this.epoch) return;
      this.wsOpen = true;
      this.retry = 0;
      void this.catchUpUart(epoch);
    };
    ws.onmessage = (ev) => {
      if (epoch === this.epoch) this.onMessage(JSON.parse(ev.data));
    };
    ws.onclose = () => {
      if (epoch !== this.epoch) return;
      this.wsOpen = false;
      const delay = Math.min(5000, 500 * 2 ** this.retry++);
      this.reconnectTimer = setTimeout(() => void this.connect(), delay);
    };
  }

  private async backfill(epoch: number) {
    const st = await api.status();
    if (epoch !== this.epoch) return;
    this.status = st;
    if (!this.paused) this.clear();
    this.gen = st.buffer.generation;
    const end = st.buffer.end_idx;
    const start = Math.max(st.buffer.start_idx, end - Math.round(this.windowS / PERIOD));
    // Paused before anything was loaded (opened while the board is off): show the last data, frozen.
    const frozenEmpty = this.paused && !this.x.length;
    if ((!this.paused || frozenEmpty) && end - start > 100) {
      const w = await api.window(start, end, POINTS);
      if (epoch !== this.epoch || (this.paused && !frozenEmpty)) return;
      this.appendWindow(w, true);
      if (this.paused && this.x.length) this.view = { a: this.x[0], b: this.x[this.x.length - 1] };
    }
    const lines = await api.uartLines(Math.max(-1, st.uart.lines - 1 - 400), 400);
    if (epoch !== this.epoch) return;
    this.uartSeq = -1;
    this.lines = [];
    this.addLines(lines);
  }

  private async catchUpUart(epoch: number) {
    const lines = await api.uartLines(this.uartSeq, 1000).catch(() => []);
    if (epoch === this.epoch) this.addLines(lines);
  }

  private appendWindow(w: BufferWindow, trim: boolean) {
    const k = w.bin_samples;
    const mean = w.current ?? w.mean ?? [];
    const lo = w.current ?? w.min ?? [];
    const hi = w.current ?? w.max ?? [];
    for (let i = 0; i < mean.length; i++) {
      this.x.push((w.start + i * k) * PERIOD);
      this.avg.push(mean[i]);
      this.mn.push(lo[i]);
      this.mx.push(hi[i]);
    }
    if (trim) this.trim();
    this.version++;
  }

  private onMessage(m: PpkMsg | UartMsg | StatusMsg) {
    if (m.type === 'ppk') {
      if (this.paused) return; // the frozen view doesn't move
      if (m.generation !== this.gen) {
        this.gen = m.generation;
        this.clear();
      }
      for (let i = 0; i < m.mean.length; i++) {
        this.x.push((m.start + i * m.bin_samples) * PERIOD);
        this.avg.push(m.mean[i]);
        this.mn.push(m.min[i]);
        this.mx.push(m.max[i]);
      }
      this.trim();
      this.version++;
    } else if (m.type === 'uart') {
      this.addLines(m.lines);
    } else if (m.type === 'status' && this.status) {
      this.status = { ...this.status, ppk2: m.ppk2, uart: m.uart, usb: m.usb ?? this.status.usb, flash: m.flash === undefined ? this.status.flash : m.flash };
    }
  }

  private trim() {
    const n = this.x.length;
    if (!n) return;
    const cutoff = this.x[n - 1] - this.windowS;
    let i = 0;
    while (i < n && this.x[i] < cutoff) i++;
    if (i > 0) {
      this.x.splice(0, i);
      this.mn.splice(0, i);
      this.mx.splice(0, i);
      this.avg.splice(0, i);
    }
  }

  private addLines(lines: UartLine[]) {
    const fresh = lines.filter((l) => l.seq > this.uartSeq);
    if (!fresh.length) return;
    this.uartSeq = fresh[fresh.length - 1].seq;
    const add = fresh.map((l) => ({ ...l, x: l.sample_idx == null ? null : l.sample_idx * PERIOD }));
    const all = this.lines.concat(add);
    this.lines = all.length > MAX_LINES ? all.slice(all.length - MAX_LINES) : all;
  }
}

function loadWindow(): number {
  try {
    const v = Number(localStorage.getItem('liveWindow'));
    if (v > 0 && v <= BUFFER_S) return v;
  } catch {
    /* storage blocked */
  }
  return 60;
}

function saveWindow(v: number) {
  try {
    localStorage.setItem('liveWindow', String(v));
  } catch {
    /* storage blocked */
  }
}

export const live = new LiveStore();

/** Mean of the non-null values in a[from..]. */
export function meanFrom(a: (number | null)[], from: number): number | null {
  let s = 0;
  let n = 0;
  for (let i = Math.max(0, from); i < a.length; i++) {
    const v = a[i];
    if (v != null) {
      s += v;
      n++;
    }
  }
  return n ? s / n : null;
}

/** Index of the last x <= t (binary search), or -1. */
export function indexAt(x: number[], t: number): number {
  let lo = 0;
  let hi = x.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (x[mid] <= t) {
      ans = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return ans;
}
