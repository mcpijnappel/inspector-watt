export type ChartCursor = { x: number; title: string; text: string } | null;
/** A rule being created from a log line, inserted at position insertAt. */
export type DraftRule = { rule: import('./api').Rule; insertAt: number };

/** One line of a compare chart (x = seconds on the shared axis). */
export type Series = { x: number[]; y: (number | null)[] };

/** A coloured time band drawn behind the chart (phases). */
export type ChartBand = { a: number; b: number; color: string };
