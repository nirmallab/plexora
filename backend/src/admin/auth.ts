/**
 * Who, if anyone, is administering this request. Three ways in, in order:
 * a verified Cloudflare Access JWT, `Authorization: Bearer ADMIN_TOKEN`
 * (constant-time), and a signed session cookie minted from that token at
 * /admin/login for the days Access is not in front of the Worker.
 *
 * The cookie is stateless (HMAC over who + expiry), so a sign-in costs no D1
 * or KV write. `HttpOnly; SameSite=Lax; Path=/admin`, and `Secure` on https.
 */
import { deleteCookie, getCookie, setCookie } from 'hono/cookie';

import { type App, isHttps } from '../http';
import { base64url, base64urlDecode, hmacHex, safeEqual } from '../telemetry/tokens';
import { verifyAccessJwt } from './access';

export const ADMIN_COOKIE = 'plexora_admin';
export const ADMIN_SESSION_TTL = 12 * 3600;

function cookieKey(c: App): string | null {
  if (c.env.ADMIN_COOKIE_KEY) return c.env.ADMIN_COOKIE_KEY;
  return c.env.ADMIN_TOKEN ? `plexora-admin-cookie:${c.env.ADMIN_TOKEN}` : null;
}

export async function mintSessionCookie(c: App, who: string, now: number): Promise<string | null> {
  const key = cookieKey(c);
  if (!key) return null;
  const body = `${base64url(new TextEncoder().encode(who))}.${now + ADMIN_SESSION_TTL}`;
  return `v1.${body}.${await hmacHex(key, body)}`;
}

async function readSessionCookie(c: App, value: string | undefined, now: number): Promise<string | null> {
  const key = cookieKey(c);
  if (!key || !value) return null;
  const match = /^v1\.([\w-]+)\.(\d+)\.([0-9a-f]{64})$/.exec(value);
  if (!match) return null;
  const [, who, exp, mac] = match as unknown as [string, string, string, string];
  if (Number(exp) < now) return null;
  if (!(await safeEqual(await hmacHex(key, `${who}.${exp}`), mac))) return null;
  try {
    return new TextDecoder().decode(base64urlDecode(who));
  } catch {
    return null;
  }
}

export function setSessionCookie(c: App, value: string): void {
  setCookie(c, ADMIN_COOKIE, value, {
    httpOnly: true,
    secure: isHttps(c),
    sameSite: 'Lax',
    path: '/admin',
    maxAge: ADMIN_SESSION_TTL,
  });
}

export function clearSessionCookie(c: App): void {
  deleteCookie(c, ADMIN_COOKIE, { path: '/admin', secure: isHttps(c), sameSite: 'Lax' });
}

export async function checkAdminToken(c: App, token: string): Promise<boolean> {
  return Boolean(c.env.ADMIN_TOKEN) && (await safeEqual(token, c.env.ADMIN_TOKEN!));
}

export async function adminIdentity(c: App, now: number): Promise<string | null> {
  const access = await verifyAccessJwt(c.env, c.req.raw);
  if (access) return access;
  const header = c.req.header('Authorization') ?? '';
  if (header.startsWith('Bearer ') && (await checkAdminToken(c, header.slice(7).trim()))) return 'token';
  return readSessionCookie(c, getCookie(c, ADMIN_COOKIE), now);
}
