// Battery chemistries (from the capture service, loaded once) and helpers for their curves.
import { api, type BatteryForm, type Chemistry } from './api';

let cache: Promise<Chemistry[]> | null = null;
export function loadChemistries(): Promise<Chemistry[]> {
  cache ??= api.chemistries().catch((e) => {
    cache = null;
    throw e;
  });
  return cache;
}

/** Pack voltage (mV) at a state of charge (%). */
export function socToMv(c: Chemistry, soc: number, cells: number): number {
  const pts = c.curve;
  if (soc >= 100) return pts[pts.length - 1][1] * cells;
  if (soc <= 0) return pts[0][1] * cells;
  for (let i = 1; i < pts.length; i++) {
    const [s1, v1] = pts[i];
    const [s0, v0] = pts[i - 1];
    if (soc <= s1) return (v0 + ((v1 - v0) * (soc - s0)) / (s1 - s0)) * cells;
  }
  return pts[pts.length - 1][1] * cells;
}

/** Highest whole state of charge whose voltage stays at or under `maxMv`. */
export function maxStartSoc(c: Chemistry, cells: number, maxMv: number): number {
  for (let s = 100; s > 0; s--) if (socToMv(c, s, cells) <= maxMv) return s;
  return 1;
}

/** Short description, e.g. "NiMH AA ×3 2000 mAh". */
export function batteryLabel(b: BatteryForm, chems: Chemistry[]): string {
  const c = chems.find((x) => x.id === (b.chemistry ?? 'lipo'));
  const name = c ? c.label.replace(/ \(.*\)$/, '') : (b.chemistry ?? 'LiPo');
  return `${name}${(b.cells ?? 1) > 1 ? ` ×${b.cells}` : ''}`;
}
