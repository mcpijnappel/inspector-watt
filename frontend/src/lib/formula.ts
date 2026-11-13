// The sensor library's value formulas (same language as api/src/api/chips.py checks): numbers, + - * /, brackets and
// byte helpers over the bytes that came back: u8 s8 u16 s16 (big-endian), u16le s16le, u24, u32 u32le; the argument
// is the index of the first byte. Parsed and calculated here, never run as code. null: not a formula, or too few bytes.

const WIDTH: Record<string, number> = { u8: 1, s8: 1, u16: 2, s16: 2, u16le: 2, s16le: 2, u24: 3, u32: 4, u32le: 4 };

function read(fn: string, b: number[], i: number): number | null {
  const w = WIDTH[fn];
  if (w == null || i + w > b.length) return null;
  const x = b.slice(i, i + w);
  switch (fn) {
    case 'u8': return x[0];
    case 's8': return x[0] >= 128 ? x[0] - 256 : x[0];
    case 'u16': return (x[0] << 8) | x[1];
    case 's16': { const v = (x[0] << 8) | x[1]; return v >= 0x8000 ? v - 0x10000 : v; }
    case 'u16le': return x[0] | (x[1] << 8);
    case 's16le': { const v = x[0] | (x[1] << 8); return v >= 0x8000 ? v - 0x10000 : v; }
    case 'u24': return x[0] * 65536 + (x[1] << 8) + x[2];
    case 'u32': return x[0] * 16777216 + ((x[1] << 16) | (x[2] << 8) | x[3]);
    case 'u32le': return x[3] * 16777216 + ((x[2] << 16) | (x[1] << 8) | x[0]);
  }
  return null;
}

export function evaluate(expr: string, bytes: number[]): number | null {
  const toks = expr.match(/\d+(?:\.\d+)?|[a-z][a-z0-9]*|\S/g) ?? [];
  let i = 0;
  const fail = () => { throw new Error('bad formula'); };
  const atom = (): number => {
    const t = toks[i++];
    if (t === '(') { const v = sum(); if (toks[i++] !== ')') fail(); return v; }
    if (t === '-') return -atom();
    if (t != null && /^\d/.test(t)) return parseFloat(t);
    if (t != null && t in WIDTH) {
      if (toks[i++] !== '(') fail();
      const idx = toks[i++];
      if (idx == null || !/^\d+$/.test(idx) || toks[i++] !== ')') fail();
      const v = read(t, bytes, Number(idx));
      if (v == null) throw new Error('too few bytes');
      return v;
    }
    return fail();
  };
  const prod = (): number => { let v = atom(); while (toks[i] === '*' || toks[i] === '/') v = toks[i++] === '*' ? v * atom() : v / atom(); return v; };
  const sum = (): number => { let v = prod(); while (toks[i] === '+' || toks[i] === '-') v = toks[i++] === '+' ? v + prod() : v - prod(); return v; };
  try {
    const v = sum();
    return i === toks.length && Number.isFinite(v) ? v : null;
  } catch {
    return null;
  }
}

/** A reading for a row: 18.3 (1 decimal for small numbers, none for counts). */
export function fmtValue(v: number): string {
  const a = Math.abs(v);
  return a >= 1000 || Number.isInteger(v) ? v.toFixed(0) : a >= 100 ? v.toFixed(1) : v.toFixed(a >= 10 ? 1 : 2);
}

export type FormulaCheck = { ok: true; need: number; value: number | null } | { ok: false; error: string };

/** Check a formula as the API does (same messages as api chips.check_formula) and, when it parses, calculate it for
 *  sample bytes: value null when the sample is too short. need: how many bytes it reads. */
export function check(expr: string, bytes: number[] = []): FormulaCheck {
  const toks: string[] = [];
  for (const m of expr.matchAll(/\s*(?:(\d+(?:\.\d+)?)|([a-z][a-z0-9]*)|(.))/g)) {
    if (m[0] === '') break;
    const t = m[1] ?? m[2] ?? m[3];
    if (t == null || !t.trim()) continue;
    if (m[3] != null && !'+-*/(),'.includes(t)) return { ok: false, error: `not allowed in a formula: '${t}'` };
    toks.push(t);
  }
  if (!expr.trim()) return { ok: false, error: 'empty formula' };
  let i = 0;
  let need = 0;
  let short = false;
  class Bad extends Error {}
  const at = () => (i < toks.length ? `'${toks[i]}'` : "'the end'");
  const take = (want?: string) => {
    if (want != null && toks[i] !== want) throw new Bad(`expected ${want} at ${at()}`);
    return toks[i++];
  };
  const atom = (): number => {
    const t = toks[i];
    if (t === '(') { take(); const v = sum(); take(')'); return v; }
    if (t === '-') { take(); return -atom(); }
    if (t != null && /^\d/.test(t)) { take(); return parseFloat(t); }
    if (t != null && /^[a-z]/.test(t)) {
      if (!(t in WIDTH)) throw new Bad(`unknown function '${t}' (use ${Object.keys(WIDTH).join(', ')})`);
      take();
      take('(');
      const idx = toks[i];
      if (idx == null || !/^\d/.test(idx)) throw new Bad(`expected num at ${at()}`);
      if (!/^\d+$/.test(idx)) throw new Bad('a byte index is a whole number');
      take();
      take(')');
      need = Math.max(need, Number(idx) + WIDTH[t]);
      const v = read(t, bytes, Number(idx));
      if (v == null) { short = true; return 0; }
      return v;
    }
    throw new Bad(`unexpected '${t ?? 'end'}'`);
  };
  const prod = (): number => { let v = atom(); while (toks[i] === '*' || toks[i] === '/') v = take() === '*' ? v * atom() : v / atom(); return v; };
  const sum = (): number => { let v = prod(); while (toks[i] === '+' || toks[i] === '-') v = take() === '+' ? v + prod() : v - prod(); return v; };
  try {
    const v = sum();
    if (i !== toks.length) return { ok: false, error: `unexpected '${toks[i]}'` };
    return { ok: true, need, value: short || !Number.isFinite(v) ? null : v };
  } catch (e) {
    if (e instanceof Bad) return { ok: false, error: e.message };
    throw e;
  }
}

/** "2C 01" or "0x2c,0x01" -> [0x2c, 0x01]; null when a part isn't a byte in hex. */
export function parseBytes(text: string): number[] | null {
  const parts = text.trim().split(/[\s,]+/).filter(Boolean);
  const out = parts.map((p) => (/^(0x)?[0-9a-f]{1,2}$/i.test(p) ? parseInt(p.replace(/^0x/i, ''), 16) : NaN));
  return out.some(Number.isNaN) ? null : out;
}
