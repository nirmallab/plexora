/**
 * Who, if anyone, is administering this request. Three ways in, in order: a
 * verified Cloudflare Access JWT, `Authorization: Bearer ADMIN_TOKEN`
 * (constant-time), and a signed cookie minted from that token at /admin/login
 * for when Access is not in front of the Worker.
 *
 * The cookie is stateless (HMAC over who + expiry under SESSION_KEY), so a
 * sign-in costs no write. `HttpOnly; SameSite=Strict; Path=/admin`, and
 * `Secure` on https.
 */
import type { MiddlewareHandler } from 'hono';
import { deleteCookie, getCookie, setCookie } from 'hono/cookie';

import { verifyAccessJwt } from './access';
import { base64url, base64urlDecode, hmacHex, safeEqual } from './crypto';
import { nowSeconds } from './env';
import { ApiError, type App, type AppEnv, fail, isHttps } from './http';

export const ADMIN_COOKIE = 'plexora_license_admin';
export const ADMIN_SESSION_TTL = 12 * 3600;

function cookieKey(c: App): string | null {
  if (c.env.SESSION_KEY) return `admin:${c.env.SESSION_KEY}`;
  return null;
}

export async function mintAdminCookie(c: App, who: string, now: number): Promise<string | null> {
  const key = cookieKey(c);
  if (!key) return null;
  const body = `${base64url(new TextEncoder().encode(who))}.${now + ADMIN_SESSION_TTL}`;
  return `v1.${body}.${await hmacHex(key, body)}`;
}

async function readAdminCookie(c: App, value: string | undefined, now: number): Promise<string | null> {
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

export function setAdminCookie(c: App, value: string): void {
  setCookie(c, ADMIN_COOKIE, value, { httpOnly: true, secure: isHttps(c), sameSite: 'Strict', path: '/admin',
    maxAge: ADMIN_SESSION_TTL });
}

export function clearAdminCookie(c: App): void {
  deleteCookie(c, ADMIN_COOKIE, { path: '/admin', secure: isHttps(c), sameSite: 'Strict' });
}

export async function checkAdminToken(c: App, token: string): Promise<boolean> {
  return Boolean(c.env.ADMIN_TOKEN) && (await safeEqual(token, c.env.ADMIN_TOKEN!));
}

export async function adminIdentity(c: App): Promise<string | null> {
  const access = await verifyAccessJwt(c.env, c.req.raw);
  if (access) return access;
  const header = c.req.header('Authorization') ?? '';
  if (header.startsWith('Bearer ') && (await checkAdminToken(c, header.slice(7).trim()))) return 'token';
  return readAdminCookie(c, getCookie(c, ADMIN_COOKIE), nowSeconds());
}

/** In front of every /admin route except the sign-in page itself. */
export const requireAdmin: MiddlewareHandler<AppEnv> = async (c, next) => {
  const who = await adminIdentity(c);
  if (!who) {
    if ((c.req.header('Accept') ?? '').includes('text/html') && c.req.method === 'GET') {
      return c.redirect('/admin/login');
    }
    return fail(c, new ApiError(401, 'unauthorized', 'Sign in as an administrator.'));
  }
  c.set('admin', who);
  await next();
};
