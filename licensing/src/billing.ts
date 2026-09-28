/**
 * `provisionLicense`: the one way a licence comes into existence.
 *
 * Today it is called by admin manual issuance and by the trial flow. A billing
 * provider's webhook calls the same function later -- which is why it takes a
 * provider-agnostic purchase record and knows nothing about Paddle or Stripe.
 */
import { accountStatement, memberStatement, upsertUser } from './accounts';
import { newId } from './crypto';
import type { AccountRow, LicenseRow, SeatRow, UserRow } from './db';
import { accountById, licenseById, validEntitlementList } from './db';
import { DAY, type Env, knob } from './env';
import { eventStatement } from './events';
import { ApiError } from './http';
import * as seats from './seats';

export const USE_CLASSES = ['academic', 'commercial', 'nonprofit', 'government'] as const;
/** What a Paid certificate grants by default. Kept in step with
 *  plexora/licensing/manifest.py PLAN_ENTITLEMENTS by the vectors test. */
export const PAID_ENTITLEMENTS = ['ai'];

export interface ProvisionRequest {
  account_id?: string;
  account?: { kind?: 'individual' | 'organization'; name?: string };
  owner_email: string;
  use_class: string;
  seats: number;
  envs_per_seat?: number;
  days?: number;
  expires_at?: number;
  entitlements?: string[];
  trial?: boolean;
  grace_days?: number;
  offline_allowed?: boolean;
  offline_max_days?: number;
  notes?: string | null;
  assign_owner_seat?: boolean;
  purchase: { provider: string; provider_ref?: string | null; kind: string; amount_cents?: number; currency?: string | null };
}

export interface Provisioned {
  account: AccountRow;
  license: LicenseRow;
  owner: UserRow;
  seat: SeatRow | null;
  seat_key: string | null;
}

export async function provisionLicense(env: Env, request: ProvisionRequest, actor: string,
  now: number): Promise<Provisioned> {
  if (!(USE_CLASSES as readonly string[]).includes(request.use_class)) {
    throw new ApiError(400, 'invalid_request', `use_class must be one of ${USE_CLASSES.join(', ')}.`);
  }
  if (!Number.isInteger(request.seats) || request.seats < 1 || request.seats > 10000) {
    throw new ApiError(400, 'invalid_request', 'seats must be a whole number from 1 to 10000.');
  }
  const expires = request.expires_at ?? (request.days ? now + request.days * DAY : null);
  if (!expires || !Number.isInteger(expires) || expires <= now) {
    throw new ApiError(400, 'invalid_request', 'Give days, or an expires_at in the future.');
  }
  const entitlements = request.entitlements === undefined ? PAID_ENTITLEMENTS
    : validEntitlementList(request.entitlements);
  if (entitlements === null) throw new ApiError(400, 'invalid_request', 'entitlements must be entitlement strings.');
  const envsPerSeat = request.envs_per_seat ?? knob(env, 'DEFAULT_ENVS_PER_SEAT');
  if (!Number.isInteger(envsPerSeat) || envsPerSeat < 1 || envsPerSeat > 100) {
    throw new ApiError(400, 'invalid_request', 'envs_per_seat must be from 1 to 100.');
  }
  const offlineMax = Math.min(request.offline_max_days ?? knob(env, 'OFFLINE_DEFAULT_DAYS'),
    knob(env, 'OFFLINE_MAX_DAYS'));

  const owner = await upsertUser(env, request.owner_email, now);
  let account: AccountRow | null = null;
  const statements: D1PreparedStatement[] = [];
  if (request.account_id) {
    account = await accountById(env, request.account_id);
    if (!account) throw new ApiError(404, 'not_found', 'No such account.');
  } else {
    account = {
      id: newId('acc'),
      kind: request.account?.kind === 'organization' ? 'organization' : 'individual',
      name: (request.account?.name ?? request.owner_email).slice(0, 120),
      use_class_default: request.use_class,
      billing_provider: null,
      billing_customer_ref: null,
      created_at: now,
      updated_at: now,
    };
    statements.push(accountStatement(env, account));
  }
  statements.push(memberStatement(env, account.id, owner.id, 'owner', now));

  const trial = request.trial === true;
  const licenseId = newId('lic');
  statements.push(env.LICENSE_DB.prepare(
    `INSERT INTO licenses (id, account_id, plan, is_trial, status, use_class, entitlements_json, seats,
       envs_per_seat, grace_days, offline_allowed, offline_max_days, starts_at, expires_at, renewal_state, notes,
       created_at, updated_at)
     VALUES (?1, ?2, 'paid', ?3, 'active', ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?11, ?11)`,
  ).bind(licenseId, account.id, trial ? 1 : 0, request.use_class, JSON.stringify(entitlements), request.seats,
    envsPerSeat, trial ? 0 : request.grace_days ?? knob(env, 'DEFAULT_GRACE_DAYS'),
    trial ? 0 : request.offline_allowed === false ? 0 : 1, offlineMax, now, expires,
    trial ? 'none' : 'manual', request.notes ?? null));
  statements.push(env.LICENSE_DB.prepare(
    `INSERT INTO purchases (id, account_id, license_id, provider, provider_ref, kind, amount_cents, currency,
       created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)`,
  ).bind(newId('pur'), account.id, licenseId, request.purchase.provider, request.purchase.provider_ref ?? null,
    request.purchase.kind, request.purchase.amount_cents ?? 0, request.purchase.currency ?? null, now));
  statements.push(eventStatement(env, now, { actor, kind: trial ? 'license.trial_issued' : 'license.issued',
    account_id: account.id, license_id: licenseId,
    payload: { seats: request.seats, expires_at: expires, use_class: request.use_class, entitlements } }));
  await env.LICENSE_DB.batch(statements);

  const license = await licenseById(env, licenseId);
  if (!license) throw new ApiError(500, 'internal_error', 'The licence was not recorded.');
  let seat: SeatRow | null = null;
  let key: string | null = null;
  if (request.assign_owner_seat !== false) {
    ({ seat, key } = await seats.create(env, license, owner.id, now, actor));
  }
  return { account, license, owner, seat, seat_key: key };
}
