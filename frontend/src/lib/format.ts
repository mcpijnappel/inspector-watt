// Number and time formatting shared by all screens.

function sig(v: number): string {
  const a = Math.abs(v);
  return v.toFixed(a >= 100 ? 0 : a >= 10 ? 1 : 2);
}

/** Current in amps -> "1.58 mA", "312 µA", "0.42 A". */
export function fmtAmps(a: number | null | undefined): string {
  if (a == null || !Number.isFinite(a)) return '–';
  const x = Math.abs(a);
  if (x < 1e-6) return `${sig(a * 1e9)} nA`;
  if (x < 1e-3) return `${sig(a * 1e6)} µA`;
  if (x < 1) return `${sig(a * 1e3)} mA`;
  return `${sig(a)} A`;
}

/** Short axis label for amps (no decimals where possible). */
export function fmtAmpsAxis(a: number): string {
  const x = Math.abs(a);
  if (x === 0) return '0';
  if (x < 1e-6) return `${+(a * 1e9).toPrecision(3)} nA`;
  if (x < 1e-3) return `${+(a * 1e6).toPrecision(3)} µA`;
  if (x < 1) return `${+(a * 1e3).toPrecision(3)} mA`;
  return `${+a.toPrecision(3)} A`;
}

/** Seconds relative to now -> "now", "−20 s", "−2:30". */
export function fmtRel(s: number, zero = 'now'): string {
  if (Math.abs(s) < 5e-7) return zero;
  const a = -s;
  if (a < 0.001) return `−${+(a * 1e6).toFixed(1)} µs`;
  if (a < 1) return `−${+(a * 1e3).toFixed(a < 0.01 ? 2 : 1)} ms`;
  if (a < 10) return `−${+a.toFixed(1)} s`;
  if (a < 60) return `−${a.toFixed(0)} s`;
  const m = Math.floor(a / 60);
  return `−${m}:${String(Math.round(a - m * 60)).padStart(2, '0')}`;
}

/** Duration -> "32:45" or "1:02:03". */
export function fmtDuration(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return '–';
  const t = Math.max(0, Math.floor(s));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = t % 60;
  const mm = String(m).padStart(h ? 2 : 1, '0');
  return h ? `${h}:${mm}:${String(sec).padStart(2, '0')}` : `${mm}:${String(sec).padStart(2, '0')}`;
}

/** Unix time -> local "18:12:45.123". */
export function fmtClock(unix: number): string {
  const d = new Date(unix * 1000);
  const p = (n: number, w = 2) => String(n).padStart(w, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

/** Hours -> "≈ 4.6 days" / "≈ 18 h". */
export function fmtLife(hours: number): string {
  if (!Number.isFinite(hours) || hours <= 0) return '–';
  return hours >= 48 ? `≈ ${(hours / 24).toFixed(1)} days` : `≈ ${hours.toFixed(0)} h`;
}

export function fmtBytes(b: number): string {
  if (b >= 1e9) return `${(b / 1e9).toFixed(2)} GB`;
  if (b >= 1e6) return `${(b / 1e6).toFixed(0)} MB`;
  return `${(b / 1e3).toFixed(0)} kB`;
}

/** Default session name, e.g. "detail 31 Oct 19:40". */
export function autoName(kind: string, d = new Date()): string {
  const mon = d.toLocaleString('en-GB', { month: 'short' });
  return `${kind} ${d.getDate()} ${mon} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** Log level from an ESP-IDF/Arduino style line "[I] ..." / "I (123) tag: ...". */
export function splitLevel(text: string): { level: string; msg: string } {
  const m = text.match(/^\[(V|D|I|W|E)\]\s?(.*)$/) ?? text.match(/^(V|D|I|W|E) \(\d+\) (.*)$/);
  return m ? { level: m[1], msg: m[2] } : { level: '', msg: text };
}

/** Session time in seconds -> "32:07.665" / "1:02:03.5"; decimals follow the visible span. */
export function fmtSessionTime(s: number, span = 60): string {
  const dec = span < 0.01 ? 5 : span < 0.1 ? 4 : span < 1 ? 3 : span < 20 ? 2 : span < 120 ? 1 : 0;
  const neg = s < 0;
  let t = Math.abs(s);
  const h = Math.floor(t / 3600);
  t -= h * 3600;
  const m = Math.floor(t / 60);
  const sec = t - m * 60;
  let secStr = sec.toFixed(dec);
  if (Number(secStr) >= 60) secStr = (59.999999).toFixed(dec);
  const [i, f] = secStr.split('.');
  const ss = i.padStart(2, '0') + (f ? '.' + f : '');
  return (neg ? '−' : '') + (h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`);
}

/** Local date + time, e.g. "31 Oct 17:40". */
export function fmtStarted(iso: string): string {
  const d = new Date(iso);
  const mon = d.toLocaleString('en-GB', { month: 'short' });
  return `${d.getDate()} ${mon} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** Charge in coulomb -> "33.3 C" / "608 mC". */
export function fmtCharge(c: number | null | undefined): string {
  if (c == null || !Number.isFinite(c)) return '–';
  if (Math.abs(c) >= 1) return `${c.toFixed(c >= 100 ? 0 : 1)} C`;
  const mc = c * 1e3;
  return `${mc.toFixed(mc >= 100 ? 0 : mc >= 10 ? 1 : 2)} mC`;
}

/** Span in seconds -> "8.439 s", "162.6 ms", "1 h 0 min". */
export function fmtSpan(s: number): string {
  if (s < 0.001) return `${(s * 1e6).toFixed(0)} µs`;
  if (s < 1) return `${(s * 1e3).toFixed(s < 0.01 ? 2 : 1)} ms`;
  if (s < 60) return `${s.toFixed(s < 10 ? 3 : 1)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}
