/**
 * Bindings, secrets and the `[vars]` knobs.
 *
 * Every knob is a string in wrangler.toml and is parsed here, with its
 * default, so a missing or garbled var degrades to a sane value rather than to
 * NaN arithmetic on an expiry date.
 */
export interface Env {
  LICENSE_DB: D1Database;
  BACKUPS: R2Bucket;

  SIGNING_KEY_PX1?: string;
  SIGNING_KEY_PX2?: string;
  FP_PEPPER?: string;
  IP_HASH_KEY?: string;
  ADMIN_TOKEN?: string;
  SESSION_KEY?: string;
  KEY_VAULT_KEY?: string;
  RESEND_API_KEY?: string;
  /** Plexora AI: the provider key the gateway calls with, and the pepper for
   * the end-user id sent upstream. Never in the client, never in D1. */
  ANTHROPIC_API_KEY?: string;
  AI_USER_PEPPER?: string;
  /** Other providers the route table may name (src/ai/providers.ts). A route
   * whose provider has no key is skipped, never called. */
  OPENAI_API_KEY?: string;
  OPENROUTER_API_KEY?: string;
  ORCAROUTER_API_KEY?: string;
  SAYGM_API_KEY?: string;

  PUBLIC_BASE_URL?: string;
  PUBLIC_KEYS_JSON?: string;
  ACTIVE_KID?: string;
  ACCESS_TEAM_DOMAIN?: string;
  ACCESS_AUD?: string;
  MAIL_FROM?: string;
  SUPPORT_EMAIL?: string;
  TRIAL_BLOCKED_DOMAINS?: string;
  /** Test and staging seam; production leaves it unset. */
  ANTHROPIC_BASE_URL?: string;
  OPENAI_BASE_URL?: string;
  OPENROUTER_BASE_URL?: string;
  ORCAROUTER_BASE_URL?: string;
  SAYGM_BASE_URL?: string;

  [knob: string]: unknown;
}

export const DEFAULTS = {
  CERT_MAX_DAYS: 90,
  CERT_RENEW_WINDOW_DAYS: 21,
  REFRESH_WRITE_INTERVAL_DAYS: 7,
  DEFAULT_GRACE_DAYS: 14,
  JOB_CERT_MAX_DAYS: 7,
  JOB_CERT_DEFAULT_HOURS: 48,
  CI_CERT_HOURS: 24,
  DEFAULT_ENVS_PER_SEAT: 2,
  COOLDOWN_HOURS: 48,
  STALE_ENV_EXEMPT_DAYS: 30,
  IDLE_ENV_RELEASE_DAYS: 180,
  OFFLINE_DEFAULT_DAYS: 180,
  OFFLINE_MAX_DAYS: 366,
  TOKEN_DEFAULT_TTL_DAYS: 90,
  TOKEN_MAX_TTL_DAYS: 365,
  TRIAL_DAYS: 30,
  TRIAL_MAX_PER_FP: 2,
  TRIAL_PER_IP_PER_DAY: 5,
  ACTIVATE_PER_IP_PER_HOUR: 30,
  DELEGATE_PER_ENV_PER_DAY: 200,
  LOGIN_PER_IP_PER_HOUR: 10,
  FORGED_PER_IP_PER_HOUR: 20,
  SESSION_DAYS: 7,
  LOGIN_LINK_MINUTES: 30,
  INVITE_DAYS: 14,
  EVENT_PAYLOAD_RETENTION_MONTHS: 24,
  BACKUP_DAILY_DAYS: 90,
  SIGNAL_ENV_OVERUSE_FACTOR: 2,
  SIGNAL_CHURN_RELEASES: 4,
  SIGNAL_FORGED_PER_IP: 5,
  SIGNAL_IPS_PER_ENV: 3,
  // Plexora AI gateway (src/ai/).
  AI_TOKEN_TTL_S: 1800,
  AI_TOKENS_PER_ENV_PER_HOUR: 60,
  AI_CALLS_PER_MIN: 120,
  AI_HOLD_TTL_S: 600,
  AI_MAX_RESERVE_MICRO: 600000,
  AI_MIN_HOLD_MICRO: 1000,
  AI_MARKUP_BPS: 20000,
  AI_MAX_BODY_BYTES: 4000000,
  AI_MAX_IMAGES: 24,
  AI_ALLOWANCE_PER_SEAT_MICRO: 0,
  AI_IDEMPOTENCY_TTL_S: 86400,
  AI_REQUEST_RETENTION_DAYS: 400,
  AI_UPSTREAM_RETRIES: 2,
  AI_RETRY_BACKOFF_MS: 250,
  AI_CIRCUIT_WINDOW_S: 30,
  AI_CIRCUIT_MIN_FAILURES: 5,
  AI_CIRCUIT_OPEN_S: 20,
  AI_STICKY_RETENTION_DAYS: 2,
  // 1 lets a route be published without a passing routing-bench evaluation.
  // For a local pipeline test on free models ONLY; production leaves it 0.
  AI_ALLOW_UNBENCHED_ROUTES: 0,
  // 0 refuses every AI token, call and run (the admin page's switch).
  AI_ENABLED: 1,
  // Model calls per UTC day for one seat / one account; 0 = no limit. An
  // account may have its own (ai_account_limits).
  AI_CALLS_PER_SEAT_PER_DAY: 0,
  AI_CALLS_PER_ACCOUNT_PER_DAY: 0,
  // How long an isolate reuses the admin's ai_settings rows (src/ai/settings.ts).
  AI_SETTINGS_CACHE_S: 15,
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

export const DAY = 86400;
export const HOUR = 3600;

/** Test seam: route tests move time without touching Date. */
let offsetSeconds = 0;
export function setClockOffset(seconds: number): void {
  offsetSeconds = seconds;
}

export function nowSeconds(): number {
  return Math.floor(Date.now() / 1000) + offsetSeconds;
}

export function dayOf(seconds: number): string {
  return new Date(seconds * 1000).toISOString().slice(0, 10);
}

export function baseUrl(env: Env): string {
  return String(env.PUBLIC_BASE_URL ?? '').replace(/\/$/, '');
}
