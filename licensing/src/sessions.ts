/**
 * Portal sign-in: magic links and sessions. No passwords anywhere.
 *
 *   POST /portal/login {email}  -> a one-time link by email (LOGIN_LINK_MINUTES)
 *   GET  /portal/auth?t=...     -> a page with a button; the GET spends nothing,
 *                                  so a mail scanner that follows links cannot
 *                                  sign itself in or burn the link
 *   POST /portal/auth {t}       -> link spent, session created, cookie set
 *
 * Only SHA-256 hashes of link and session secrets are stored. The cookie is
 * `HttpOnly; SameSite=Lax; Path=/portal`, `Secure` on https, SESSION_DAYS long.
 * The login answer is the same whether or not the address is known, so the
 * form cannot be used to discover who holds a licence.
 */
import type { MiddlewareHandler } from 'hono';
import { deleteCookie, getCookie, setCookie } from 'hono/cookie';

import { canonicalEmail, upsertUser } from './accounts';
import { randomToken, sha256Hex } from './crypto';
import type { UserRow } from './db';
import { one } from './db';
import { baseUrl, DAY, type Env, knob, nowSeconds } from './env';
import { ApiError, type App, type AppEnv, fail, isHttps } from './http';
import * as mail from './email';

export const SESSION_COOKIE = 'plexora_portal';

export async function sendLoginLink(env: Env, email: string, now: number): Promise<void> {
  const canonical = canonicalEmail(email);
  const known = await one<{ id: string }>(env, 'SELECT id FROM users WHERE email_canonical = ?1', canonical);
  if (!known) return; // same answer either way; nothing is sent
  const secret = randomToken(32);
  const minutes = knob(env, 'LOGIN_LINK_MINUTES');
  await env.LICENSE_DB.prepare(
    'INSERT INTO login_links (id, email_canonical, created_at, expires_at) VALUES (?1, ?2, ?3, ?4)',
  ).bind(await sha256Hex(secret), canonical, now, now + minutes * 60).run();
  await mail.send(env, mail.loginMail(email, `${baseUrl(env)}/portal/auth?t=${secret}`, minutes));
}

/** Spend a link; returns the user it signs in, or throws. */
export async function spendLoginLink(env: Env, secret: string, now: number): Promise<UserRow> {
  const id = await sha256Hex(secret);
  const spent = await env.LICENSE_DB.prepare(
    `UPDATE login_links SET used_at = ?2 WHERE id = ?1 AND used_at IS NULL AND expires_at > ?2
     RETURNING email_canonical`,
  ).bind(id, now).first<{ email_canonical: string }>();
  if (!spent) throw new ApiError(401, 'unauthorized', 'That sign-in link has expired or was already used.');
  const user = await one<UserRow>(env, 'SELECT * FROM users WHERE email_canonical = ?1', spent.email_canonical);
  if (!user) throw new ApiError(401, 'unauthorized', 'That sign-in link is no longer valid.');
  return user;
}

export async function startSession(c: App, user: UserRow, now: number): Promise<void> {
  const secret = randomToken(32);
  const days = knob(c.env, 'SESSION_DAYS');
  await c.env.LICENSE_DB.batch([
    c.env.LICENSE_DB.prepare('INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?1, ?2, ?3, ?4)')
      .bind(await sha256Hex(secret), user.id, now, now + days * DAY),
    c.env.LICENSE_DB.prepare('UPDATE users SET last_login_at = ?2 WHERE id = ?1').bind(user.id, now),
  ]);
  setCookie(c, SESSION_COOKIE, secret, { httpOnly: true, secure: isHttps(c), sameSite: 'Lax', path: '/portal',
    maxAge: days * DAY });
}

export async function endSession(c: App): Promise<void> {
  const secret = getCookie(c, SESSION_COOKIE);
  if (secret) await c.env.LICENSE_DB.prepare('DELETE FROM sessions WHERE id = ?1').bind(await sha256Hex(secret)).run();
  deleteCookie(c, SESSION_COOKIE, { path: '/portal', secure: isHttps(c), sameSite: 'Lax' });
}

export async function sessionUser(c: App): Promise<UserRow | null> {
  const secret = getCookie(c, SESSION_COOKIE);
  if (!secret || secret.length > 128) return null;
  return one<UserRow>(c.env,
    `SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?1 AND s.expires_at > ?2`,
    await sha256Hex(secret), nowSeconds());
}

export const requireUser: MiddlewareHandler<AppEnv> = async (c, next) => {
  const user = await sessionUser(c);
  if (!user) {
    if (c.req.method === 'GET' && !c.req.path.startsWith('/portal/api/')) return c.redirect('/portal/login');
    return fail(c, new ApiError(401, 'unauthorized', 'Sign in to the licence portal.'));
  }
  c.set('userId', user.id);
  await next();
};

/** Accept an invitation: the emailed link proves the mailbox. */
export async function acceptInvitation(env: Env, secret: string, now: number): Promise<{
  user: UserRow; account_id: string; license_id: string | null; role: string;
}> {
  const invite = await env.LICENSE_DB.prepare(
    `UPDATE invitations SET accepted_at = ?2 WHERE token_hash = ?1 AND accepted_at IS NULL AND revoked_at IS NULL
       AND expires_at > ?2
     RETURNING account_id, license_id, email_canonical, role`,
  ).bind(await sha256Hex(secret), now).first<{ account_id: string; license_id: string | null;
    email_canonical: string; role: string }>();
  if (!invite) throw new ApiError(401, 'unauthorized', 'That invitation has expired, was revoked, or was already used.');
  const user = await upsertUser(env, invite.email_canonical, now);
  return { user, account_id: invite.account_id, license_id: invite.license_id, role: invite.role };
}
