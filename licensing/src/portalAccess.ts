/**
 * Who may do what in the portal.
 *
 *   owner, admin   everything on their account's licences: every seat,
 *                  environment and token, invitations, and removing an
 *                  environment without the seat's cooldown
 *   member         their own seat: its key, its environments, its tokens,
 *                  its offline licences
 *
 * Every portal API route resolves its subject through here, so a seat id from
 * someone else's account is a 404, never a leak.
 */
import type { EnvironmentRow, LicenseRow, SeatRow } from './db';
import { all, environmentById, licenseById, one, seatById } from './db';
import type { Env } from './env';
import { ApiError } from './http';

export interface LicenseAccess {
  license: LicenseRow;
  account_name: string;
  role: 'owner' | 'admin' | 'member';
  manage: boolean;
  seat: SeatRow | null;
}

export async function licensesFor(env: Env, userId: string): Promise<LicenseAccess[]> {
  const rows = await all<LicenseRow & { account_name: string; role: 'owner' | 'admin' | 'member' }>(env,
    `SELECT l.*, a.name AS account_name, m.role FROM account_members m
     JOIN accounts a ON a.id = m.account_id JOIN licenses l ON l.account_id = m.account_id
     WHERE m.user_id = ?1 AND m.status = 'active' ORDER BY l.expires_at DESC`, userId);
  const out: LicenseAccess[] = [];
  for (const row of rows) {
    const seat = await one<SeatRow>(env,
      `SELECT * FROM seat_assignments WHERE license_id = ?1 AND user_id = ?2 AND status = 'active'`, row.id, userId);
    const manage = row.role === 'owner' || row.role === 'admin';
    if (!manage && !seat) continue;
    const { account_name, role, ...license } = row;
    out.push({ license: license as LicenseRow, account_name, role, manage, seat });
  }
  return out;
}

export async function licenseAccess(env: Env, userId: string, licenseId: string): Promise<LicenseAccess> {
  const found = (await licensesFor(env, userId)).find((a) => a.license.id === licenseId);
  if (!found) throw new ApiError(404, 'not_found', 'No such licence.');
  return found;
}

export async function seatAccess(env: Env, userId: string, seatId: string): Promise<{
  seat: SeatRow; license: LicenseRow; manage: boolean; own: boolean;
}> {
  const seat = await seatById(env, seatId);
  if (!seat) throw new ApiError(404, 'not_found', 'No such seat.');
  const license = await licenseById(env, seat.license_id);
  const role = await one<{ role: string }>(env,
    `SELECT role FROM account_members WHERE user_id = ?1 AND account_id = ?2 AND status = 'active'`,
    userId, seat.account_id);
  const manage = role?.role === 'owner' || role?.role === 'admin';
  const own = seat.user_id === userId;
  if (!license || (!manage && !own)) throw new ApiError(404, 'not_found', 'No such seat.');
  return { seat, license, manage, own };
}

export async function environmentAccess(env: Env, userId: string, environmentId: string): Promise<{
  environment: EnvironmentRow; seat: SeatRow; license: LicenseRow; manage: boolean; own: boolean;
}> {
  const environment = await environmentById(env, environmentId);
  if (!environment) throw new ApiError(404, 'not_found', 'No such environment.');
  const access = await seatAccess(env, userId, environment.seat_id).catch(() => null);
  if (!access) throw new ApiError(404, 'not_found', 'No such environment.');
  return { environment, ...access };
}
