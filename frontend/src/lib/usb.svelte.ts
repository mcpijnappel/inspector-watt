// The board's own USB console: its lines (polled only while the console is on screen), open/close, send, menu keys.
// Status (present, open, menu) comes with the live status poll.

import { api, type UartLine } from './api';
import { live } from './live.svelte';

const MAX_LINES = 3000;
const POLL_MS = 400;
const TAB_KEY = 'pst.log.tab';

/** Keys whose label says they wipe or reset something ask before they're sent. */
export const risky = (label: string) => /clear|reset|erase|factory|format|wipe/i.test(label);

export type ConsoleLine = UartLine & { sent?: boolean; note?: boolean };

export type LogTab = 'uart' | 'usb' | 'bus';

function loadTab(): LogTab {
  try {
    const t = localStorage.getItem(TAB_KEY);
    return t === 'usb' || t === 'bus' ? t : 'uart';
  } catch {
    return 'uart';
  }
}

class UsbConsole {
  lines = $state.raw<ConsoleLine[]>([]);
  /** Which log the Live page shows (bus: the logic analyzer's decoded traffic). */
  tab = $state<LogTab>(loadTab());
  error = $state('');
  busy = $state(false);

  private seq = -1;
  private users = 0;
  private timer: ReturnType<typeof setInterval> | undefined;
  private loaded = false;

  get status() {
    return live.status?.usb;
  }

  setTab(t: LogTab) {
    this.tab = t;
    try {
      localStorage.setItem(TAB_KEY, t);
    } catch { /* storage blocked */ }
  }

  /** The console is on screen: poll its lines until release(). */
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
      // The first time: only the last lines (the capture keeps a few thousand).
      if (!this.loaded) {
        const total = this.status?.lines ?? 0;
        this.seq = Math.max(-1, total - 1 - 500);
        this.loaded = true;
      }
      const fresh = await api.usbLines(this.seq, 1000);
      if (!fresh.length) return;
      this.seq = fresh[fresh.length - 1].seq;
      this.append(fresh);
    } catch {
      /* the status poll shows when the api is unreachable */
    }
  }

  private append(add: ConsoleLine[]) {
    const all = [...this.lines, ...add];
    this.lines = all.length > MAX_LINES ? all.slice(all.length - MAX_LINES) : all;
  }

  private note(text: string, sent = false) {
    this.append([{ seq: -1, wall: Date.now() / 1000, sample_idx: null, text, note: !sent, sent }]);
  }

  private async run(fn: () => Promise<unknown>) {
    this.busy = true;
    this.error = '';
    try {
      await fn();
      await live.pollStatus();
      return true;
    } catch (e) {
      this.error = (e as Error).message;
      return false;
    } finally {
      this.busy = false;
    }
  }

  async open() {
    if (await this.run(() => api.usbOpen(true)))
      this.note(this.status?.present ? 'Console open.' : 'Console open. Waiting for the board to wake up (it shows up on its next wake).');
  }

  async close() {
    if (await this.run(() => api.usbOpen(false))) this.note('Console closed. The board sleeps again within ~30 s.');
  }

  async send(text: string) {
    if (await this.run(() => api.usbSend(text))) this.note(text, true);
  }

  async key(k: string) {
    if (await this.run(() => api.usbKey(k))) this.note(k, true);
  }
}

export const usb = new UsbConsole();
