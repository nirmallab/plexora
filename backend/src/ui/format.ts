/** Number and time formatting shared by the pages. */
export function num(value: unknown): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return '–';
  return Math.round(n).toLocaleString('en-US');
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '–';
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '–';
  return `${(value * 100).toFixed(digits)}%`;
}

export function bytes(value: unknown): string {
  let n = Number(value ?? 0);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  while (n >= 1000 && unit < units.length - 1) {
    n /= 1000;
    unit += 1;
  }
  return `${n.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function when(seconds: unknown): string {
  const n = Number(seconds);
  if (!seconds || !Number.isFinite(n)) return '–';
  return new Date(n * 1000).toISOString().replace('T', ' ').slice(0, 16);
}

/** An ISO day from epoch seconds. */
export function day(seconds: unknown): string {
  const n = Number(seconds);
  if (!seconds || !Number.isFinite(n)) return '–';
  return new Date(n * 1000).toISOString().slice(0, 10);
}

/** "channels_band" → "Channels"; "rows_band" → "Rows". */
export function dimLabel(dim: string): string {
  const text = dim.replace(/_band$/, '').replace(/[._]/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** A ratio as a whole percentage when it is large, one decimal when it is small. */
export function share(part: number, whole: number): string {
  if (!whole) return '–';
  const value = (part / whole) * 100;
  return `${value >= 10 || value === 0 ? Math.round(value) : value.toFixed(1)}%`;
}

export function daysIn(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000) + 1;
}
