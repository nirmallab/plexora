/**
 * What a row looks like outside the database. Hashes, vault ciphertext and
 * bindings never leave: not to the portal, not to the admin API.
 */
import type { EnvironmentRow, LicenseRow, SeatRow, TokenRow } from './db';
import { parseEntitlements } from './db';

export function licenseView(row: LicenseRow) {
  return {
    id: row.id, account_id: row.account_id, plan: row.plan, trial: row.is_trial === 1, status: row.status,
    use_class: row.use_class, entitlements: parseEntitlements(row.entitlements_json), seats: row.seats,
    envs_per_seat: row.envs_per_seat, grace_days: row.grace_days, offline_allowed: row.offline_allowed === 1,
    offline_max_days: row.offline_max_days, starts_at: row.starts_at, expires_at: row.expires_at,
    renewal_state: row.renewal_state, notes: row.notes, created_at: row.created_at,
    revoked_at: row.revoked_at, revoke_reason: row.revoke_reason,
  };
}

export function seatView(row: SeatRow & { email?: string | null }) {
  return {
    id: row.id, license_id: row.license_id, status: row.status, user_id: row.user_id, email: row.email ?? null,
    key_hint: `PLEX-****-****-****-${row.seat_key_hint}`, key_revealable: row.seat_key_vault !== null,
    entitlements_override: row.entitlements_override_json === null ? null
      : parseEntitlements(row.entitlements_override_json),
    envs_per_seat: row.envs_per_seat, cooldown_until: row.cooldown_until, created_at: row.created_at,
    released_at: row.released_at,
  };
}

export function environmentView(row: EnvironmentRow) {
  return {
    id: row.id, seat_id: row.seat_id, kind: row.kind, name: row.display_name, status: row.status,
    registration: row.registration_kind, can_delegate: row.delegation_pubkey !== null,
    platform: row.platform, scheduler: row.scheduler_hint, app_version: row.app_version,
    created_at: row.created_at, last_seen: row.last_refresh_at ?? row.created_at, released_at: row.released_at,
    release_reason: row.release_reason,
  };
}

export function tokenView(row: TokenRow) {
  return {
    id: row.id, seat_id: row.seat_id, scope: row.scope, label: row.label, hint: `PLXT1_…${row.token_hint}`,
    created_at: row.created_at, expires_at: row.expires_at, revoked_at: row.revoked_at,
    last_used_at: row.last_used_at, use_count: row.use_count,
  };
}
