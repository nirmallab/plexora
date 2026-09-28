/**
 * Environments: registering one on a seat, and releasing it.
 *
 * The cap -- environments per seat, two by default: a laptop and a cluster --
 * is enforced by ONE conditional INSERT, so two activations racing for the
 * last slot cannot both win it. Re-activating from an environment that is
 * already registered (the same binding) returns that registration: a cluster
 * whose every node shares `$HOME` is one row however many nodes ask.
 *
 * Releases are rate-limited per seat (COOLDOWN_HOURS), which is what stops a
 * two-environment seat being rotated across a lab of twenty machines. Exempt:
 * a seat's first ever release, an environment already stale for
 * STALE_ENV_EXEMPT_DAYS (housekeeping, not rotation), and an owner or admin
 * acting in the portal on purpose.
 */
import { newId } from './crypto';
import type { EnvironmentRow, LicenseRow, SeatRow } from './db';
import { activeEnvironments, envAllowance, environmentById, isUniqueViolation, one } from './db';
import { DAY, type Env, HOUR, knob } from './env';
import { eventStatement } from './events';
import { ApiError } from './http';

export const KINDS = ['desktop', 'cluster', 'container-host'] as const;
export type Kind = (typeof KINDS)[number];

export interface Registration {
  kind: Kind;
  display_name: string;
  binding_hash: string;
  delegation_pubkey: string | null;
  registration_kind: 'online' | 'offline';
  platform: string | null;
  scheduler_hint: string | null;
  app_version: string | null;
}

export function cleanName(value: unknown, fallback: string): string {
  const text = typeof value === 'string' ? value.replace(/[\u0000-\u001f\u007f<>]/g, '').trim() : '';
  return (text || fallback).slice(0, 80);
}

export function cleanShort(value: unknown, max = 40): string | null {
  if (typeof value !== 'string') return null;
  const text = value.replace(/[^\w.+\- ]/g, '').trim();
  return text ? text.slice(0, max) : null;
}

const PUBKEY = /^[A-Za-z0-9_-]{43}$/;

export function cleanDelegation(kind: Kind, value: unknown): string | null {
  if (kind === 'desktop') return null;
  return typeof value === 'string' && PUBKEY.test(value) ? value : null;
}

/** Register (or re-find) an environment. Returns it, and whether it is new. */
export async function register(env: Env, license: LicenseRow, seat: SeatRow, reg: Registration, now: number,
  audit: { actor: string; ip_hash: string | null }): Promise<{ environment: EnvironmentRow; created: boolean }> {
  const existing = await one<EnvironmentRow>(env,
    `SELECT * FROM environments WHERE seat_id = ?1 AND env_binding_hash = ?2 AND status = 'active'`,
    seat.id, reg.binding_hash);
  if (existing) return { environment: await refreshDetails(env, existing, reg), created: false };

  const id = newId('env');
  const allowance = envAllowance(env, license, seat);
  let changes = 0;
  try {
    const result = await env.LICENSE_DB.batch([
      env.LICENSE_DB.prepare(
        `INSERT INTO environments (id, seat_id, license_id, kind, display_name, env_binding_hash,
           delegation_pubkey, registration_kind, status, platform, scheduler_hint, app_version,
           created_at, last_refresh_at)
         SELECT ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'active', ?9, ?10, ?11, ?12, ?12
         WHERE (SELECT COUNT(*) FROM environments WHERE seat_id = ?2 AND status = 'active') < ?13`,
      ).bind(id, seat.id, license.id, reg.kind, reg.display_name, reg.binding_hash, reg.delegation_pubkey,
        reg.registration_kind, reg.platform, reg.scheduler_hint, reg.app_version, now, allowance),
      // Recorded only if the row above went in (same transaction, same guard).
      env.LICENSE_DB.prepare(
        `INSERT INTO events (at, actor, kind, account_id, license_id, seat_id, environment_id, ip_hash, payload)
         SELECT ?1, ?2, 'environment.registered', ?3, ?4, ?5, ?6, ?7, ?8
         WHERE EXISTS (SELECT 1 FROM environments WHERE id = ?6)`,
      ).bind(now, audit.actor, license.account_id, license.id, seat.id, id, audit.ip_hash,
        JSON.stringify({ kind: reg.kind, registration: reg.registration_kind })),
    ]);
    changes = result[0]?.meta.changes ?? 0;
  } catch (error) {
    if (!isUniqueViolation(error)) throw error;
    // The same binding raced itself (two nodes of one cluster activating at
    // once). The winner's row is this environment's registration too.
    const winner = await one<EnvironmentRow>(env,
      `SELECT * FROM environments WHERE seat_id = ?1 AND env_binding_hash = ?2 AND status = 'active'`,
      seat.id, reg.binding_hash);
    if (winner) return { environment: winner, created: false };
    throw error;
  }
  if (changes === 0) {
    const active = await activeEnvironments(env, seat.id);
    throw new ApiError(409, 'seat_env_limit',
      `This seat allows ${allowance} environment${allowance === 1 ? '' : 's'} and all are in use.`, {
        details: {
          allowed: allowance,
          active: active.map((e) => ({ id: e.id, name: e.display_name, kind: e.kind,
            last_seen: e.last_refresh_at ?? e.created_at })),
        },
      });
  }
  const created = await environmentById(env, id);
  if (!created) throw new ApiError(500, 'internal_error', 'The registration was not recorded.');
  return { environment: created, created: true };
}

/** Keep a re-registration's details current, writing only when they changed. */
async function refreshDetails(env: Env, row: EnvironmentRow, reg: Registration): Promise<EnvironmentRow> {
  const delegation = reg.kind === 'desktop' ? null : reg.delegation_pubkey ?? row.delegation_pubkey;
  const changed = row.kind !== reg.kind || row.delegation_pubkey !== delegation ||
    (reg.app_version !== null && row.app_version !== reg.app_version);
  if (!changed) return row;
  await env.LICENSE_DB.prepare(
    `UPDATE environments SET kind = ?2, delegation_pubkey = ?3, app_version = COALESCE(?4, app_version)
     WHERE id = ?1`,
  ).bind(row.id, reg.kind, delegation, reg.app_version).run();
  return { ...row, kind: reg.kind, delegation_pubkey: delegation, app_version: reg.app_version ?? row.app_version };
}

export interface ReleaseOptions {
  now: number;
  reason: 'user' | 'portal' | 'admin' | 'idle' | 'seat_released';
  actor: string;
  ip_hash?: string | null;
  /** An owner/admin in the portal, or an admin: no cooldown. */
  override?: boolean;
}

/** Release an environment. Returns when the seat may next release one. */
export async function release(env: Env, environment: EnvironmentRow, seat: SeatRow,
  options: ReleaseOptions): Promise<{ released: boolean; next_allowed_at: number | null }> {
  if (environment.status !== 'active') return { released: false, next_allowed_at: null };
  const { now } = options;
  const lastSeen = environment.last_refresh_at ?? environment.created_at;
  const stale = now - lastSeen > knob(env, 'STALE_ENV_EXEMPT_DAYS') * DAY;
  const exempt = options.override === true || stale || options.reason === 'idle' ||
    options.reason === 'seat_released';
  if (!exempt && seat.releases > 0 && seat.cooldown_until > now) {
    throw new ApiError(429, 'cooldown_active',
      'Environments on this seat were changed recently. Remove this one from the portal, or try again later.',
      { next_allowed_at: seat.cooldown_until, retry_after: seat.cooldown_until - now });
  }
  const cooldown = exempt ? seat.cooldown_until : now + knob(env, 'COOLDOWN_HOURS') * HOUR;
  const results = await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare(
      `UPDATE environments SET status = 'released', released_at = ?2, release_reason = ?3
       WHERE id = ?1 AND status = 'active'`,
    ).bind(environment.id, now, options.reason),
    env.LICENSE_DB.prepare(
      `UPDATE seat_assignments SET releases = releases + 1, cooldown_until = MAX(cooldown_until, ?2)
       WHERE id = ?1`,
    ).bind(seat.id, cooldown),
    eventStatement(env, now, {
      actor: options.actor, kind: 'environment.released', license_id: environment.license_id,
      seat_id: seat.id, environment_id: environment.id, ip_hash: options.ip_hash ?? null,
      payload: { reason: options.reason, exempt },
    }),
  ]);
  return { released: (results[0]?.meta.changes ?? 0) > 0, next_allowed_at: exempt ? null : cooldown };
}

/**
 * The weekly `last_refresh_at` write, and the one place a refresh writes at
 * all: a conditional UPDATE that changes nothing inside the interval.
 */
export async function noteRefresh(env: Env, environment: EnvironmentRow, now: number,
  ip_hash: string | null): Promise<boolean> {
  const interval = knob(env, 'REFRESH_WRITE_INTERVAL_DAYS') * DAY;
  const result = await env.LICENSE_DB.prepare(
    `UPDATE environments SET last_refresh_at = ?2
     WHERE id = ?1 AND (last_refresh_at IS NULL OR last_refresh_at <= ?3)`,
  ).bind(environment.id, now, now - interval).run();
  if ((result.meta.changes ?? 0) === 0) return false;
  await eventStatement(env, now, {
    actor: 'client', kind: 'environment.refreshed', license_id: environment.license_id,
    seat_id: environment.seat_id, environment_id: environment.id, ip_hash,
  }).run();
  return true;
}
