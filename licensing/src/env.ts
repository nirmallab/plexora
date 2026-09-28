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

  PUBLIC_BASE_URL?: string;
  PUBLIC_KEYS_JSON?: string;
  ACTIVE_KID?: string;
  ACCESS_TEAM_DOMAIN?: string;
  ACCESS_AUD?: string;
  MAIL_FROM?: string;
  SUPPORT_EMAIL?: string;
  TRIAL_BLOCKED_DOMAINS?: string;

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
