/**
 * Stored numbers as something a person reads at a glance. Dates are ISO
 * everywhere: the audience is international, and 03/04 is two different days.
 */
import type { LicenseRow } from '../db';

export type Tone = 'ok' | 'warn' | 'bad' | 'accent' | 'plain';

const DAY = 86400;

export function date(seconds: number | null | undefined): string {
  if (!seconds) return '—';
  return new Date(seconds * 1000).toISOString().slice(0, 10);
}

export function dateTime(seconds: number | null | undefined): string {
  if (!seconds) return '—';
  return `${new Date(seconds * 1000).toISOString().slice(0, 16).replace('T', ' ')}Z`;
}

/** "in 12 days" / "3 days ago" / "today". */
export function relative(seconds: number | null | undefined, now: number): string {
  if (!seconds) return '';
  const days = Math.round((seconds - now) / DAY);
  if (days === 0) return 'today';
  if (days > 0) return days === 1 ? 'tomorrow' : `in ${days} days`;
  return days === -1 ? 'yesterday' : `${-days} days ago`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * What a licence is today, which is not always its status column: a row stays
 * `active` until the nightly sweep, and one past its end still works for its
 * grace days.
 */
export function licenceState(license: Pick<LicenseRow, 'status' | 'expires_at' | 'grace_days' | 'is_trial'>,
  now: number): { label: string; tone: Tone; detail: string } {
  if (license.status === 'revoked') {
    return { label: 'Revoked', tone: 'bad', detail: 'Every environment drops to Free at its next check.' };
  }
  if (license.status === 'suspended') {
    return { label: 'Suspended', tone: 'bad',
      detail: 'Paused: nothing new is issued, and refreshes answer revoked. Reactivating restores it at once.' };
  }
  const trial = license.is_trial === 1;
  if (license.expires_at <= now || license.status === 'expired') {
    const graceEnd = license.expires_at + license.grace_days * DAY;
    if (graceEnd > now) {
      return { label: 'In grace', tone: 'warn',
        detail: `Ended ${relative(license.expires_at, now)}. Paid keeps working until ${date(graceEnd)}.` };
    }
    return { label: trial ? 'Trial ended' : 'Expired', tone: 'bad',
      detail: `Ended ${relative(license.expires_at, now)}. Plexora is back on Free; everything made with Paid stays.` };
  }
  const days = Math.ceil((license.expires_at - now) / DAY);
  if (days <= 30) {
    return { label: trial ? 'Trial ending' : 'Expiring soon', tone: 'warn',
      detail: `Ends ${relative(license.expires_at, now)}, on ${date(license.expires_at)}.` };
  }
  return { label: trial ? 'Trial' : 'Active', tone: trial ? 'accent' : 'ok',
    detail: `Valid until ${date(license.expires_at)}.` };
}

export function statusTone(status: string): Tone {
  if (['active', 'ok', 'paid_active'].includes(status)) return 'ok';
  if (['suspended', 'expired', 'invited'].includes(status)) return 'warn';
  if (['released'].includes(status)) return 'plain';
  return 'bad';
}

export const KIND_LABELS: Record<string, string> = {
  desktop: 'Computer', cluster: 'HPC cluster', 'container-host': 'Container host',
};

/** "3 computers, 1 HPC cluster". */
export function kindCounts(rows: { kind: string; n: number }[]): string {
  const nouns: Record<string, [string, string]> = {
    desktop: ['computer', 'computers'], cluster: ['HPC cluster', 'HPC clusters'],
    'container-host': ['container host', 'container hosts'],
  };
  return rows.map((row) => {
    const [one, many] = nouns[row.kind] ?? [row.kind, row.kind];
    return plural(row.n, one, many);
  }).join(', ');
}

/** Micro-USD as dollars: "$212.40", "$0.0042" for small sums. */
export function usd(micro: number | null | undefined): string {
  const n = micro ?? 0;
  return `$${(n / 1_000_000).toFixed(n && Math.abs(n) < 10_000 ? 4 : 2)}`;
}

/** A unit price in micro-USD per 1M tokens as dollars: "4.00", "0.13", "0.0075", "0". */
export function perMillion(micro: number | null | undefined): string {
  const n = micro ?? 0;
  if (!n) return '0';
  const dollars = n / 1_000_000;
  return dollars >= 0.01 ? dollars.toFixed(2) : String(Number(dollars.toPrecision(2)));
}

/** "just now", "12 min ago", "3 h ago", "4 days ago". */
export function ago(seconds: number | null | undefined, now: number): string {
  if (!seconds) return 'never';
  const s = Math.max(0, now - seconds);
  if (s < 90) return 'just now';
  if (s < 90 * 60) return `${Math.round(s / 60)} min ago`;
  if (s < 36 * 3600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / DAY)} days ago`;
}

/** A token count at a glance: "200k", "1M", "128k". */
export function tokens(n: number | null | undefined): string {
  if (!n) return '—';
  if (n >= 1_000_000) return `${Number((n / 1_000_000).toFixed(1))}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

/** Milliseconds as "820 ms" or "1.9 s". */
export function ms(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  return n < 1000 ? `${Math.round(n)} ms` : `${(n / 1000).toFixed(1)} s`;
}

/** A share, as "42%". */
export function pct(part: number, whole: number): string {
  return whole > 0 ? `${Math.round((100 * part) / whole)}%` : '—';
}
