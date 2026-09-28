/**
 * Licence tokens: opaque bearer credentials for automation.
 *
 *   PLXT1_<43 base64url>
 *
 * Stored as SHA-256 only and shown once. A token's only power is asking this
 * service for a certificate on behalf of its seat, within its scope:
 *
 *   interactive  a person's desktop or cluster login, activated by env var
 *   hpc          registers a cluster or container host (never a desktop)
 *   automation   any environment kind; for provisioning scripts
 *   ci           NEVER registers anything: each use gets a CI_CERT_HOURS job
 *                certificate, and the use is recorded on the token
 *
 * Bounded (TOKEN_MAX_TTL_DAYS, and never past the licence), revocable,
 * recorded (`last_used_at`, `use_count`).
 */
import { newId, newLicenseToken, sha256Hex } from './crypto';
import type { LicenseRow, SeatRow, TokenRow } from './db';
import { one } from './db';
import { DAY, type Env, knob } from './env';
import { eventStatement } from './events';
import { ApiError } from './http';

export const SCOPES = ['interactive', 'hpc', 'automation', 'ci'] as const;
export type Scope = (typeof SCOPES)[number];

/** Which environment kinds a scope may register. `ci` registers none. */
export const SCOPE_KINDS: Record<Scope, readonly string[]> = {
  interactive: ['desktop', 'cluster'],
  hpc: ['cluster', 'container-host'],
  automation: ['desktop', 'cluster', 'container-host'],
  ci: [],
};

export async function mint(env: Env, options: {
  license: LicenseRow;
  seat: SeatRow;
  scope: Scope;
  label: string;
  ttlDays?: number | null;
  createdBy: string;
  now: number;
}): Promise<{ token: string; row: TokenRow }> {
  const { license, seat, now } = options;
  const max = knob(env, 'TOKEN_MAX_TTL_DAYS');
  const days = Math.max(1, Math.min(options.ttlDays ?? knob(env, 'TOKEN_DEFAULT_TTL_DAYS'), max));
  const expires = Math.min(now + days * DAY, license.expires_at);
  const token = newLicenseToken();
  const row: TokenRow = {
    id: newId('tok'), seat_id: seat.id, license_id: license.id, token_hash: await sha256Hex(token),
    token_hint: token.slice(-4), scope: options.scope, label: options.label.slice(0, 80),
    created_by: options.createdBy, created_at: now, expires_at: expires, revoked_at: null,
    last_used_at: null, use_count: 0,
  };
  await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare(
      `INSERT INTO license_tokens (id, seat_id, license_id, token_hash, token_hint, scope, label, created_by,
         created_at, expires_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)`,
    ).bind(row.id, row.seat_id, row.license_id, row.token_hash, row.token_hint, row.scope, row.label,
      row.created_by, row.created_at, row.expires_at),
    eventStatement(env, now, { actor: options.createdBy, kind: 'token.minted', license_id: license.id,
      seat_id: seat.id, payload: { token_id: row.id, scope: row.scope, expires_at: expires } }),
  ]);
  return { token, row };
}

/** The token row for a presented token, or the refusal. */
export async function resolve(env: Env, token: string, now: number): Promise<TokenRow> {
  const row = await one<TokenRow>(env, 'SELECT * FROM license_tokens WHERE token_hash = ?1', await sha256Hex(token));
  if (!row) throw new ApiError(401, 'invalid_credential', 'That licence token is not recognised.');
  if (row.revoked_at !== null) {
    await eventStatement(env, now, { actor: 'client', kind: 'token.revoked_use', license_id: row.license_id,
      seat_id: row.seat_id, payload: { token_id: row.id } }).run();
    throw new ApiError(403, 'credential_revoked', 'That licence token has been revoked.');
  }
  if (row.expires_at <= now) throw new ApiError(403, 'credential_expired', 'That licence token has expired.');
  return row;
}

export async function noteUse(env: Env, row: TokenRow, now: number): Promise<void> {
  await env.LICENSE_DB.prepare(
    'UPDATE license_tokens SET last_used_at = ?2, use_count = use_count + 1 WHERE id = ?1',
  ).bind(row.id, now).run();
}

export async function revoke(env: Env, id: string, actor: string, now: number): Promise<boolean> {
  const row = await one<TokenRow>(env, 'SELECT * FROM license_tokens WHERE id = ?1', id);
  if (!row || row.revoked_at !== null) return false;
  await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare('UPDATE license_tokens SET revoked_at = ?2 WHERE id = ?1 AND revoked_at IS NULL')
      .bind(id, now),
    eventStatement(env, now, { actor, kind: 'token.revoked', license_id: row.license_id, seat_id: row.seat_id,
      payload: { token_id: id } }),
  ]);
  return true;
}
