/**
 * The audit log: who did what to which licence, seat or environment, when.
 *
 * One row per state change -- a registration, a release, a revocation, a
 * trial, a token minted. Deliberately NOT one row per routine refresh: those
 * record at most one row a week per environment, when the refresh's own
 * conditional `last_refresh_at` update happens, which is what the
 * simultaneous-use signal reads.
 */
import type { Env } from './env';

export interface EventFields {
  actor: string;
  kind: string;
  account_id?: string | null;
  license_id?: string | null;
  seat_id?: string | null;
  environment_id?: string | null;
  ip_hash?: string | null;
  payload?: Record<string, unknown> | null;
}

export function eventStatement(env: Env, at: number, event: EventFields): D1PreparedStatement {
  return env.LICENSE_DB.prepare(
    `INSERT INTO events (at, actor, kind, account_id, license_id, seat_id, environment_id, ip_hash, payload)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)`,
  ).bind(at, event.actor, event.kind, event.account_id ?? null, event.license_id ?? null,
    event.seat_id ?? null, event.environment_id ?? null, event.ip_hash ?? null,
    event.payload ? JSON.stringify(event.payload) : null);
}

export async function record(env: Env, at: number, event: EventFields): Promise<void> {
  await eventStatement(env, at, event).run();
}
