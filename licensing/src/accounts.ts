/**
 * Accounts, users and memberships.
 *
 * An account is an individual or an organization; users belong to accounts
 * as owner, admin or member. The email address is the only thing identifying
 * a person here, and `email_canonical` is what uniqueness is judged on.
 */
import { newId } from './crypto';
import type { AccountRow, UserRow } from './db';
import { one } from './db';
import type { Env } from './env';

const EMAIL = /^[^\s@<>()[\]\\,;:"]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$/;

export function validEmail(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  return text.length <= 254 && EMAIL.test(text) ? text : null;
}

/**
 * One mailbox, one spelling: lowercased, `+tags` dropped everywhere, and dots
 * dropped where the provider ignores them. What a trial's one-per-mailbox rule
 * is judged on.
 */
export function canonicalEmail(email: string): string {
  const [localRaw = '', domainRaw = ''] = email.trim().toLowerCase().split('@');
  let local = localRaw.split('+')[0] ?? '';
  const domain = domainRaw === 'googlemail.com' ? 'gmail.com' : domainRaw;
  if (domain === 'gmail.com') local = local.replace(/\./g, '');
  return `${local}@${domain}`;
}

export async function findUser(env: Env, email: string): Promise<UserRow | null> {
  return one<UserRow>(env, 'SELECT * FROM users WHERE email_canonical = ?1', canonicalEmail(email));
}

/** The user for an address, created if new. Race-safe on email_canonical. */
export async function upsertUser(env: Env, email: string, now: number): Promise<UserRow> {
  const canonical = canonicalEmail(email);
  await env.LICENSE_DB.prepare(
    `INSERT INTO users (id, email, email_canonical, created_at) VALUES (?1, ?2, ?3, ?4)
     ON CONFLICT(email_canonical) DO NOTHING`,
  ).bind(newId('usr'), email.trim(), canonical, now).run();
  const user = await one<UserRow>(env, 'SELECT * FROM users WHERE email_canonical = ?1', canonical);
  if (!user) throw new Error('user upsert failed');
  return user;
}

export function accountStatement(env: Env, account: AccountRow): D1PreparedStatement {
  return env.LICENSE_DB.prepare(
    `INSERT INTO accounts (id, kind, name, use_class_default, billing_provider, billing_customer_ref,
       created_at, updated_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)`,
  ).bind(account.id, account.kind, account.name, account.use_class_default, account.billing_provider,
    account.billing_customer_ref, account.created_at, account.updated_at);
}

export function memberStatement(env: Env, accountId: string, userId: string, role: string,
  now: number): D1PreparedStatement {
  return env.LICENSE_DB.prepare(
    `INSERT INTO account_members (id, account_id, user_id, role, status, created_at)
     VALUES (?1, ?2, ?3, ?4, 'active', ?5)
     ON CONFLICT DO NOTHING`,
  ).bind(newId('mem'), accountId, userId, role, now);
}

export interface Membership {
  account_id: string;
  role: 'owner' | 'admin' | 'member';
}

export async function membership(env: Env, userId: string, accountId: string): Promise<Membership | null> {
  return one<Membership>(env,
    `SELECT account_id, role FROM account_members WHERE user_id = ?1 AND account_id = ?2 AND status = 'active'`,
    userId, accountId);
}
