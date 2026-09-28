/**
 * Seats: a licence's units of use, each assigned to at most one person.
 *
 * The seat count is enforced by one conditional INSERT, like the environment
 * cap. A seat's key is shown once (and emailed); the database keeps its
 * SHA-256, its last four characters, and -- only when KEY_VAULT_KEY is set --
 * an AES-GCM copy the portal can re-display to its holder.
 *
 * Releasing or revoking a seat releases its environments and revokes its
 * tokens in the same batch: nothing outlives the seat it came from, except
 * certificates already issued, which run out on their own.
 */
import { newId, newSeatKey, sha256Hex, vaultOpen, vaultSeal } from './crypto';
import type { LicenseRow, SeatRow } from './db';
import { one, seatById } from './db';
import type { Env } from './env';
import { eventStatement } from './events';
import { ApiError } from './http';

export async function create(env: Env, license: LicenseRow, userId: string | null, now: number,
  actor: string): Promise<{ seat: SeatRow; key: string }> {
  const key = newSeatKey();
  const id = newId('sa');
  const hash = await sha256Hex(key);
  const vault = await vaultSeal(env.KEY_VAULT_KEY, key);
  let changes = 0;
  try {
    const results = await env.LICENSE_DB.batch([
      env.LICENSE_DB.prepare(
        `INSERT INTO seat_assignments (id, license_id, account_id, user_id, status, seat_key_hash, seat_key_hint,
           seat_key_vault, created_at)
         SELECT ?1, ?2, ?3, ?4, 'active', ?5, ?6, ?7, ?8
         WHERE (SELECT COUNT(*) FROM seat_assignments WHERE license_id = ?2 AND status = 'active')
               < (SELECT seats FROM licenses WHERE id = ?2)`,
      ).bind(id, license.id, license.account_id, userId, hash, key.slice(-4), vault, now),
      env.LICENSE_DB.prepare(
        `INSERT INTO events (at, actor, kind, account_id, license_id, seat_id, payload)
         SELECT ?1, ?2, 'seat.created', ?3, ?4, ?5, ?6 WHERE EXISTS (SELECT 1 FROM seat_assignments WHERE id = ?5)`,
      ).bind(now, actor, license.account_id, license.id, id, JSON.stringify({ assigned: userId !== null })),
    ]);
    changes = results[0]?.meta.changes ?? 0;
  } catch (error) {
    if (/UNIQUE constraint failed/i.test(String((error as Error).message))) {
      throw new ApiError(409, 'conflict', 'That person already holds a seat on this account.');
    }
    throw error;
  }
  if (changes === 0) {
    throw new ApiError(409, 'seat_limit', `This licence has ${license.seats} seat${license.seats === 1 ? '' : 's'}, all in use.`);
  }
  const seat = await seatById(env, id);
  if (!seat) throw new ApiError(500, 'internal_error', 'The seat was not recorded.');
  return { seat, key };
}

export async function bySeatKey(env: Env, key: string): Promise<SeatRow | null> {
  return one<SeatRow>(env, 'SELECT * FROM seat_assignments WHERE seat_key_hash = ?1', await sha256Hex(key));
}

/** End a seat: its environments are released and its tokens revoked, atomically. */
export async function end(env: Env, seat: SeatRow, status: 'released' | 'revoked', now: number,
  actor: string): Promise<void> {
  await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare(
      `UPDATE seat_assignments SET status = ?2, released_at = ?3 WHERE id = ?1 AND status = 'active'`,
    ).bind(seat.id, status, now),
    env.LICENSE_DB.prepare(
      `UPDATE environments SET status = ?2, released_at = ?3, release_reason = 'seat_released'
       WHERE seat_id = ?1 AND status = 'active'`,
    ).bind(seat.id, status === 'revoked' ? 'revoked' : 'released', now),
    env.LICENSE_DB.prepare(
      'UPDATE license_tokens SET revoked_at = ?2 WHERE seat_id = ?1 AND revoked_at IS NULL',
    ).bind(seat.id, now),
    eventStatement(env, now, { actor, kind: `seat.${status}`, account_id: seat.account_id,
      license_id: seat.license_id, seat_id: seat.id }),
  ]);
}

/** A new key for a seat; the old one stops working at once. */
export async function rotateKey(env: Env, seat: SeatRow, now: number, actor: string): Promise<string> {
  const key = newSeatKey();
  await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare(
      `UPDATE seat_assignments SET seat_key_hash = ?2, seat_key_hint = ?3, seat_key_vault = ?4 WHERE id = ?1`,
    ).bind(seat.id, await sha256Hex(key), key.slice(-4), await vaultSeal(env.KEY_VAULT_KEY, key)),
    eventStatement(env, now, { actor, kind: 'seat.key_rotated', license_id: seat.license_id, seat_id: seat.id }),
  ]);
  return key;
}

export async function revealKey(env: Env, seat: SeatRow): Promise<string | null> {
  return vaultOpen(env.KEY_VAULT_KEY, seat.seat_key_vault);
}

export async function assign(env: Env, seat: SeatRow, userId: string | null, now: number,
  actor: string): Promise<void> {
  try {
    await env.LICENSE_DB.batch([
      env.LICENSE_DB.prepare('UPDATE seat_assignments SET user_id = ?2 WHERE id = ?1 AND status = \'active\'')
        .bind(seat.id, userId),
      eventStatement(env, now, { actor, kind: 'seat.assigned', license_id: seat.license_id, seat_id: seat.id,
        payload: { assigned: userId !== null } }),
    ]);
  } catch (error) {
    if (/UNIQUE constraint failed/i.test(String((error as Error).message))) {
      throw new ApiError(409, 'conflict', 'That person already holds a seat on this account.');
    }
    throw error;
  }
}
