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
