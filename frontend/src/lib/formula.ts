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
