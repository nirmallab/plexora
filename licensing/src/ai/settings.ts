/**
 * Plexora AI settings an administrator changes from /admin/ai, without a
 * redeploy.
 *
 * A row in `ai_settings` overrides the `[vars]` value of the same knob, which
 * overrides the code default (env.ts DEFAULTS). `withSettings` lays the rows
 * over the Worker's env once per request, so every `knob()` call -- the
 * gateway, the ledger, routing, the cron -- sees them with no change of its
 * own. Rows are cached per isolate for AI_SETTINGS_CACHE_S; a save clears this
 * isolate's cache, others catch up within that window.
 *
 * Only the knobs in EDITABLE can be set this way: licence terms, signing and
 * anything security-bearing stay in wrangler.toml and secrets.
 */
import { DEFAULTS, type Env, type Knob, knob } from '../env';

/** The Settings page's sections, in order. */
export const GROUPS = ['Routing', 'Limits', 'Credit', 'Reliability', 'Capacity', 'Retention', 'Access'] as const;
export type SettingsGroup = (typeof GROUPS)[number];

export interface Editable {
  label: string;
  group: SettingsGroup;
  help: string;
  /** What one displayed unit is in knob units (credits: 10000 micro-USD). */
  scale?: number;
  unit?: string;
  min: number;
  max: number;
  /** A 0/1 switch, shown as on/off. */
  flag?: boolean;
}

export const EDITABLE: Partial<Record<Knob, Editable>> = {
  AI_ENABLED: { label: 'Plexora AI is on', group: 'Access', flag: true, min: 0, max: 1,
    help: 'Off refuses every token, call and run for every account (they pause, and resume once it is on).' },
  AI_ALLOW_UNBENCHED_ROUTES: { label: 'Any approved model may serve', group: 'Routing', flag: true, min: 0, max: 1,
    help: 'Off: assigning a model reached through an aggregator needs a passing routing-bench evaluation.' },
  AI_PRICE_REFRESH: { label: 'Refresh prices nightly', group: 'Routing', flag: true, min: 0, max: 1,
    help: 'Reads each aggregator\'s model list at 03:30 UTC: prices, fees, availability, context windows.' },
  AI_PRICE_STALE_HOURS: { label: 'Prices are stale after', group: 'Routing', unit: 'hours', min: 1, max: 720,
    help: 'A listed price not confirmed for this long is flagged on the Overview and Models pages.' },
  AI_CALLS_PER_SEAT_PER_DAY: { label: 'Calls per person per day', group: 'Limits', unit: 'calls', min: 0,
    max: 1_000_000, help: 'Model calls one seat (one person) may make per UTC day. 0 = no limit.' },
  AI_CALLS_PER_ACCOUNT_PER_DAY: { label: 'Calls per account per day', group: 'Limits', unit: 'calls', min: 0,
    max: 10_000_000, help: 'Model calls a whole account may make per UTC day, all seats together. 0 = no limit.' },
  AI_CALLS_PER_MIN: { label: 'Calls per minute per token', group: 'Limits', unit: 'calls', min: 1, max: 10_000,
    help: 'A burst limit on one 30-minute gateway token.' },
  AI_TOKENS_PER_ENV_PER_HOUR: { label: 'Tokens per environment per hour', group: 'Limits', unit: 'tokens', min: 1,
    max: 10_000, help: 'How often one installation may fetch a gateway token.' },
  AI_MAX_IMAGES: { label: 'Images per call', group: 'Limits', unit: 'images', min: 0, max: 100,
    help: 'The most images one model call may carry.' },
  AI_MAX_BODY_BYTES: { label: 'Request size per call', group: 'Limits', unit: 'MB', scale: 1_000_000, min: 0.1, max: 20,
    help: 'The largest request body the gateway accepts.' },
  AI_ALLOWANCE_PER_SEAT_MICRO: { label: 'Monthly allowance per seat', group: 'Credit', unit: 'credits', scale: 10_000,
    min: 0, max: 1_000_000, help: 'Credit included every month for each active seat (1 credit = $0.01 of metered use). '
      + 'A run reserves its quote up front, so a seat with neither credit nor allowance cannot start one.' },
  AI_MARKUP_BPS: { label: 'Markup on provider cost', group: 'Credit', unit: '× cost', scale: 10_000, min: 1, max: 10,
    help: 'What accounts pay per unit of provider cost (2 = twice the cost). An account can have its own.' },
  AI_MAX_RESERVE_MICRO: { label: 'Largest hold per call', group: 'Credit', unit: 'credits', scale: 10_000, min: 0.1,
    max: 100_000, help: 'The most credit one call may hold while it runs.' },
  AI_MIN_HOLD_MICRO: { label: 'Smallest hold per call', group: 'Credit', unit: 'credits', scale: 10_000, min: 0,
    max: 1_000, help: 'Held even for a call estimated at nothing (a free model).' },
  AI_UPSTREAM_RETRIES: { label: 'Retries on the same model', group: 'Reliability', unit: 'retries', min: 0, max: 5,
    help: 'Retries on a provider 429 or 5xx before failover is considered.' },
  AI_RETRY_BACKOFF_MS: { label: 'Retry backoff', group: 'Reliability', unit: 'ms', min: 0, max: 10_000,
    help: 'The first wait between retries; it doubles.' },
  AI_CIRCUIT_MIN_FAILURES: { label: 'Failures that open a circuit', group: 'Reliability', unit: 'failures', min: 1,
    max: 1_000, help: 'Within the window, and at least half the calls, before calls fail over.' },
  AI_CIRCUIT_WINDOW_S: { label: 'Circuit window', group: 'Reliability', unit: 's', min: 5, max: 3_600,
    help: 'How far back failures count.' },
  AI_CIRCUIT_OPEN_S: { label: 'Circuit open for', group: 'Reliability', unit: 's', min: 5, max: 3_600,
    help: 'How long an open circuit sends calls to the next rank before one probe.' },
  AI_CLOUDFLARE_PAID: { label: 'Cloudflare account is on Workers Paid', group: 'Capacity', flag: true, min: 0, max: 1,
    help: 'Grades the capacity monitor against Paid\'s monthly inclusions instead of Free\'s daily limits.' },
  AI_PROVIDER_RPM: { label: 'Provider limit per minute', group: 'Capacity', unit: 'calls', min: 0, max: 10_000_000,
    help: 'The serving provider\'s written requests-per-minute limit for Plexora\'s key (every user shares it). 0 = not known.' },
  AI_PROVIDER_RPD: { label: 'Provider limit per day', group: 'Capacity', unit: 'calls', min: 0, max: 1_000_000_000,
    help: 'The serving provider\'s written requests-per-day limit for Plexora\'s key. 0 = not known.' },
  AI_REQUEST_RETENTION_DAYS: { label: 'Keep call records for', group: 'Retention', unit: 'days', min: 30, max: 3_650,
    help: 'Rows in ai_requests older than this are pruned nightly.' },
};

export function isEditable(name: string): name is Knob {
  return Object.prototype.hasOwnProperty.call(EDITABLE, name);
}

/** The knob value for one displayed value: credits to micro-USD, MB to bytes, multiplier to basis points. */
export function toKnob(name: Knob, shown: number): number {
  return Math.round(shown * (EDITABLE[name]?.scale ?? 1));
}

export function toShown(name: Knob, value: number): number {
  const scale = EDITABLE[name]?.scale ?? 1;
  return Math.round((value / scale) * 10_000) / 10_000;
}

type Rows = Record<string, string>;
let cache: { at: number; rows: Rows } | null = null;
const BASE = Symbol('ai-settings-base');

export function clearSettingsCache(): void {
  cache = null;
}

async function rows(env: Env): Promise<Rows> {
  const ttl = knob(env, 'AI_SETTINGS_CACHE_S') * 1000;
  if (cache && ttl > 0 && Date.now() - cache.at < ttl) return cache.rows;
  let found: Rows = {};
  try {
    const result = await env.LICENSE_DB.prepare('SELECT name, value FROM ai_settings').all<{ name: string; value: string }>();
    found = Object.fromEntries((result.results ?? []).filter((r) => isEditable(r.name)).map((r) => [r.name, r.value]));
  } catch {
    found = {};    // before the schema has the table: nothing overridden
  }
  cache = { at: Date.now(), rows: found };
  return found;
}

/** The env with the admin's settings over its `[vars]`; the same object when none are set. */
export async function withSettings(env: Env): Promise<Env> {
  const set = await rows(env);
  if (!Object.keys(set).length) return env;
  const merged = Object.create(env) as Env & { [BASE]?: Env };
  Object.assign(merged, set);
  merged[BASE] = env;
  return merged;
}

/** Each editable knob: its effective value and where it comes from. */
export async function describe(env: Env) {
  const base = (env as Env & { [BASE]?: Env })[BASE] ?? env;
  const set = await rows(env);
  return (Object.keys(EDITABLE) as Knob[]).map((name) => {
    const meta = EDITABLE[name]!;
    const fallback = knob(base, name);
    const value = knob(env, name);
    const source = name in set ? 'admin' : (typeof base[name] === 'string' && base[name] !== '' ? 'wrangler' : 'default');
    return { name, ...meta, value, shown: toShown(name, value), fallback, fallback_shown: toShown(name, fallback),
      code_default: DEFAULTS[name], source };
  });
}
