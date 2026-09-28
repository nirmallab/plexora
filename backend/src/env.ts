/**
 * Bindings, secrets and the `[vars]` knobs.
 *
 * Every knob is a string in wrangler.toml and is parsed here, with the default
 * the plan specifies, so a missing or garbled var degrades to a sane value
 * rather than to NaN arithmetic in the budget monitor.
 */
export interface Env {
  TELEMETRY_DB: D1Database;
  ARCHIVES: R2Bucket;

  TELEMETRY_HMAC_KEY?: string;
  TELEMETRY_HMAC_KEY_PREVIOUS?: string;
  IP_HASH_KEY?: string;
  ADMIN_TOKEN?: string;
  ADMIN_COOKIE_KEY?: string;

  PUBLIC_BASE_URL?: string;
  ACCESS_TEAM_DOMAIN?: string;
  ACCESS_AUD?: string;

  [knob: string]: unknown;
}

export const DEFAULTS = {
  D1_WRITE_BUDGET: 40000,
  D1_READ_BUDGET: 2000000,
  WORKER_REQUEST_BUDGET: 40000,
  R2_CLASS_A_BUDGET: 300000,
  R2_CLASS_B_BUDGET: 3000000,
  BUDGET_WARN: 0.5,
  BUDGET_AGGREGATE: 0.7,
  BUDGET_REDUCE: 0.85,
  BUDGET_CRITICAL: 0.95,
  BUDGET_STOP: 1.0,
  BUDGET_CACHE_S: 60,
  CONFIG_CACHE_S: 60,
  MAX_COMPRESSED_BYTES: 65536,
  MAX_DECOMPRESSED_BYTES: 524288,
  MAX_CLEAN_BYTES: 131072,
  MAX_EVENTS: 200,
  REGISTER_LIMIT_PER_DAY: 200,
  EVENTS_PER_INSTALL_PER_HOUR: 12,
  EVENTS_PER_INSTALL_PER_DAY: 96,
  DEDUPE_DAYS: 3,
  UPLOAD_INTERVAL_S: 3600,
  INSTALL_TOKEN_TTL_DAYS: 365,
  FOLD_SLICE_BATCHES: 2000,
  EXPORT_SEGMENT_BYTES: 1048576,
  EXPORT_PAGE_ROWS: 500,
  MONTHLY_SLICE_ROWS: 3000,
  PURGE_SLICE_ROWS: 5000,
  SWEEP_PAGE_KEYS: 500,
  REOPEN_DAYS: 3,
  EXPORT_MAX_ATTEMPTS: 3,
  MAINTENANCE_LOOKBACK_DAYS: 7,
  BATCH_RETENTION_DAYS: 30,
  BATCH_RETENTION_HARD_DAYS: 45,
  ERROR_DAILY_RETENTION_DAYS: 60,
  ROLLUP_RETENTION_DAYS: 365,
  DAILY_INSTALLS_RETENTION_DAYS: 400,
  ARCHIVE_RETENTION_DAILY_DAYS: 180,
  ARCHIVE_RETENTION_MONTHLY_DAYS: 365,
  ARCHIVE_MIN_DAYS: 30,
  INSTALL_INACTIVE_ERASE_DAYS: 730,
  REGISTER_QUOTA_RETENTION_DAYS: 7,
  MAINTENANCE_LOG_RETENTION_DAYS: 400,
  R2_TARGET_BYTES: 5e9,
  R2_WARN_BYTES: 6e9,
  R2_AGGRESSIVE_BYTES: 8e9,
  R2_HARD_BYTES: 9.5e9,
  CSV_MAX_ROWS: 10000,
  EXPORT_MAX_DAYS: 7,
} as const;

export type Knob = keyof typeof DEFAULTS;

/** A numeric `[vars]` value, or its default when absent or not a number. */
export function knob(env: Env, name: Knob): number {
  const raw = env[name];
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw;
  if (typeof raw === 'string' && raw.trim() !== '') {
    const parsed = Number(raw);
    if (Number.isFinite(parsed)) return parsed;
  }
  return DEFAULTS[name];
}

/** Every knob with its effective value, for the budget page. */
export function knobs(env: Env): Record<Knob, number> {
  const out = {} as Record<Knob, number>;
  for (const name of Object.keys(DEFAULTS) as Knob[]) out[name] = knob(env, name);
  return out;
}

export function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

/** The UTC day of a unix time, `YYYY-MM-DD`. */
export function dayOf(seconds: number): string {
  return new Date(seconds * 1000).toISOString().slice(0, 10);
}

/** `day` shifted by `delta` days. */
export function addDays(day: string, delta: number): string {
  return new Date(Date.parse(`${day}T00:00:00Z`) + delta * 86400_000).toISOString().slice(0, 10);
}

/** Seconds until the next UTC midnight. */
export function secondsToMidnight(seconds: number): number {
  return 86400 - (seconds % 86400);
}
