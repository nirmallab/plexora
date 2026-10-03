/**
 * Row types and the lookups every flow shares.
 */
import type { Env } from './env';
import { knob } from './env';

export interface AccountRow {
  id: string;
  kind: 'individual' | 'organization';
  name: string;
  use_class_default: string;
  billing_provider: string | null;
  billing_customer_ref: string | null;
  created_at: number;
  updated_at: number;
}

export interface UserRow {
  id: string;
  email: string;
  email_canonical: string;
  name: string | null;
  created_at: number;
  last_login_at: number | null;
}

export interface LicenseRow {
  id: string;
  account_id: string;
  plan: 'paid';
  is_trial: number;
  status: 'active' | 'suspended' | 'expired' | 'revoked';
  use_class: string;
  entitlements_json: string;
  seats: number;
  envs_per_seat: number;
  grace_days: number;
  offline_allowed: number;
  offline_max_days: number;
  starts_at: number;
  expires_at: number;
  renewal_state: string;
  notes: string | null;
  created_at: number;
  updated_at: number;
  revoked_at: number | null;
  revoke_reason: string | null;
}

export interface SeatRow {
  id: string;
  license_id: string;
  account_id: string;
  user_id: string | null;
  status: 'active' | 'released' | 'revoked';
  seat_key_hash: string;
  seat_key_hint: string;
  seat_key_vault: string | null;
  entitlements_override_json: string | null;
  envs_per_seat: number | null;
  cooldown_until: number;
  releases: number;
  created_at: number;
  released_at: number | null;
}

export interface EnvironmentRow {
  id: string;
  seat_id: string;
  license_id: string;
  kind: 'desktop' | 'cluster' | 'container-host';
  display_name: string;
  env_binding_hash: string;
  delegation_pubkey: string | null;
  registration_kind: 'online' | 'offline';
  status: 'active' | 'released' | 'revoked';
  platform: string | null;
  scheduler_hint: string | null;
  app_version: string | null;
  created_at: number;
  last_refresh_at: number | null;
  released_at: number | null;
  release_reason: string | null;
}

export interface TokenRow {
  id: string;
  seat_id: string;
  license_id: string;
  token_hash: string;
  token_hint: string;
  scope: 'interactive' | 'hpc' | 'automation' | 'ci';
  label: string;
  created_by: string | null;
  created_at: number;
  expires_at: number;
  revoked_at: number | null;
  last_used_at: number | null;
  use_count: number;
}

export async function one<T>(env: Env, sql: string, ...params: unknown[]): Promise<T | null> {
  return (await env.LICENSE_DB.prepare(sql).bind(...params).first<T>()) ?? null;
}

export async function all<T>(env: Env, sql: string, ...params: unknown[]): Promise<T[]> {
  return (await env.LICENSE_DB.prepare(sql).bind(...params).all<T>()).results ?? [];
}

export const licenseById = (env: Env, id: string) =>
  one<LicenseRow>(env, 'SELECT * FROM licenses WHERE id = ?1', id);
export const seatById = (env: Env, id: string) =>
  one<SeatRow>(env, 'SELECT * FROM seat_assignments WHERE id = ?1', id);
export const environmentById = (env: Env, id: string) =>
  one<EnvironmentRow>(env, 'SELECT * FROM environments WHERE id = ?1', id);
export const accountById = (env: Env, id: string) =>
  one<AccountRow>(env, 'SELECT * FROM accounts WHERE id = ?1', id);
export const userById = (env: Env, id: string) =>
  one<UserRow>(env, 'SELECT * FROM users WHERE id = ?1', id);

export function activeEnvironments(env: Env, seatId: string) {
  return all<EnvironmentRow>(env,
    `SELECT * FROM environments WHERE seat_id = ?1 AND status = 'active' ORDER BY created_at`, seatId);
}

const GRAMMAR = /^[a-z][a-z0-9_]*(:[a-z][a-z0-9_]*)*$/;

/** A JSON list of entitlement strings, validated; anything else is empty. */
export function parseEntitlements(text: string | null | undefined): string[] {
  if (!text) return [];
  try {
    const value = JSON.parse(text) as unknown;
    if (!Array.isArray(value)) return [];
    return [...new Set(value.filter((e): e is string => typeof e === 'string' && GRAMMAR.test(e) && e !== 'free'))].sort();
  } catch {
    return [];
  }
}

export function validEntitlementList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  if (!value.every((e) => typeof e === 'string' && GRAMMAR.test(e) && e !== 'free')) return null;
  return [...new Set(value as string[])].sort();
}

/** What a seat's certificates grant: its override, or its licence's list. */
export function seatEntitlements(license: LicenseRow, seat: SeatRow): string[] {
  return seat.entitlements_override_json !== null
    ? parseEntitlements(seat.entitlements_override_json)
    : parseEntitlements(license.entitlements_json);
}

export function envAllowance(env: Env, license: LicenseRow, seat: SeatRow): number {
  return seat.envs_per_seat ?? license.envs_per_seat ?? knob(env, 'DEFAULT_ENVS_PER_SEAT');
}

export function isUniqueViolation(error: unknown): boolean {
  return /UNIQUE constraint failed/i.test(String((error as Error)?.message ?? error));
}

/** Columns schema.sql gained after a table existed: CREATE IF NOT EXISTS never adds them, so they are added here. */
const LATE_COLUMNS: Array<[string, string, string]> = [
  ['ai_requests', 'task', 'TEXT'],
  ['ai_requests', 'model_id', 'TEXT'],
];
let lateColumnsChecked = false;

/**
 * Add any LATE_COLUMNS a database lacks, once per isolate. A deploy that
 * reaches a database before `npm run db:init` therefore still records every
 * call; two isolates racing to add one column is harmless (the loser's
 * "duplicate column" is ignored).
 */
export async function ensureLateColumns(env: Env): Promise<void> {
  if (lateColumnsChecked) return;
  try {
    for (const table of new Set(LATE_COLUMNS.map(([t]) => t))) {
      const have = new Set((await all<{ name: string }>(env, `PRAGMA table_info(${table})`)).map((c) => c.name));
      if (!have.size) continue;       // no such table yet: schema.sql will create it whole
      for (const [t, column, type] of LATE_COLUMNS) {
        if (t !== table || have.has(column)) continue;
        await env.LICENSE_DB.prepare(`ALTER TABLE ${table} ADD COLUMN ${column} ${type}`).run()
          .catch((error) => { if (!/duplicate column/i.test(String(error))) throw error; });
      }
    }
    lateColumnsChecked = true;
  } catch (error) {
    console.error('ensureLateColumns', error);
  }
}

/** Test seam: check again on the next call. */
export function resetLateColumns(): void {
  lateColumnsChecked = false;
}
