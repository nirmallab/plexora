/**
 * Percentiles from histogram bins.
 *
 * Rollups store bands, not percentiles, because bands add across days,
 * versions and installs and percentiles do not. A p95 is interpolated from the
 * summed bins at read time: linear within the bin that holds the rank. The top
 * bin is open-ended; its upper edge is the observed max when known, else twice
 * the last edge.
 */
import { VECTORS } from './schema';

export const MS_EDGES = VECTORS.ms_edges as number[];

export function addBins(a: number[], b: number[]): number[] {
  const out = a.slice();
  for (let i = 0; i < b.length; i += 1) out[i] = (out[i] ?? 0) + (b[i] ?? 0);
  return out;
}

export function percentile(bins: number[], q: number, edges: number[] = MS_EDGES, max?: number | null): number | null {
  const total = bins.reduce((sum, n) => sum + n, 0);
  if (total <= 0) return null;
  const rank = Math.min(Math.max(q, 0), 1) * total;
  let seen = 0;
  for (let i = 0; i < bins.length; i += 1) {
    const n = bins[i] ?? 0;
    if (n > 0 && seen + n >= rank) {
      const lo = i === 0 ? 0 : edges[i - 1]!;
      const last = edges[edges.length - 1]!;
      const hi = i < edges.length ? edges[i]! : Math.max(max ?? 2 * last, last);
      return lo + ((rank - seen) / n) * (hi - lo);
    }
    seen += n;
  }
  return edges[edges.length - 1] ?? null;
}

/** Bins out of a row with b0..b8 columns. */
export function binsOf(row: Record<string, unknown>): number[] {
  return Array.from({ length: VECTORS.hist_bins }, (_, i) => Number(row[`b${i}`] ?? 0));
}
