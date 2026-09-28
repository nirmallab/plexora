/**
 * Shared HTTP plumbing: the app type, JSON/HTML responses with their security
 * headers, and the same-origin guard in front of every admin mutation.
 */
import type { Context, MiddlewareHandler } from 'hono';
import type { ContentfulStatusCode } from 'hono/utils/http-status';

import type { Env } from './env';
import type { Meter } from './telemetry/budget';
import { CLIENT_JS } from './ui/client';
import { STYLES } from './ui/styles';

export type AppEnv = {
  Bindings: Env;
  Variables: {
    /** Who the admin is; set by the /admin auth middleware. */
    admin: string;
    /** Meters an admin request's D1 use into budget_daily. */
    meter: Meter;
  };
};

export type App = Context<AppEnv>;

/** API responses: never cached, never sniffed. */
export const NO_STORE = { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' };

export function jsonError(c: App, status: ContentfulStatusCode, error: string, headers: Record<string, string> = {}) {
  return c.json({ error }, status, { ...NO_STORE, ...headers });
}

export function wantsHtml(c: App): boolean {
  return (c.req.header('Accept') ?? '').includes('text/html');
}

/** `wrangler dev` is plain http, where browsers drop `Secure` cookies. */
export function isHttps(c: App): boolean {
  return new URL(c.req.url).protocol === 'https:';
}

let hashes: { script: string; style: string } | null = null;

async function b64sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return btoa(String.fromCharCode(...new Uint8Array(digest)));
}

/** CSP hashes of the one inline script and stylesheet; constants, so once per isolate. */
async function inlineHashes() {
  hashes ??= { script: await b64sha256(CLIENT_JS), style: await b64sha256(STYLES) };
  return hashes;
}

/**
 * Renders an admin page. Nothing loads from anywhere: `default-src 'none'`,
 * the inline script and stylesheet pinned by hash, charts are inline SVG.
 */
export async function page(c: App, node: string | Promise<string>, status: ContentfulStatusCode = 200) {
  const { script, style } = await inlineHashes();
  return c.html(`<!doctype html>${await node}`, status, {
    ...NO_STORE,
    'Referrer-Policy': 'same-origin',
    'X-Frame-Options': 'DENY',
    'Content-Security-Policy': [
      "default-src 'none'",
      `style-src 'sha256-${style}'`,
      `script-src 'sha256-${script}'`,
      "img-src 'self' data:",
      "connect-src 'self'",
      "form-action 'self'",
      "base-uri 'none'",
      "frame-ancestors 'none'",
    ].join('; '),
  });
}

/**
 * CSRF defence for admin mutations. Every mutation is a `fetch` sending JSON,
 * which a cross-origin form cannot produce without a preflight the browser
 * will not grant; `Sec-Fetch-Site`, where the browser sends it, must say
 * same-origin. Scripts using a bearer token send no Sec-Fetch-Site and pass.
 */
export const sameOriginGuard: MiddlewareHandler<AppEnv> = async (c, next) => {
  if (c.req.method === 'GET' || c.req.method === 'HEAD') return next();
  const site = c.req.header('Sec-Fetch-Site');
  if (site !== undefined && site !== 'same-origin' && site !== 'none') {
    return jsonError(c, 403, 'Cross-site requests are not accepted.');
  }
  const origin = c.req.header('Origin');
  if (origin && origin !== new URL(c.req.url).origin) {
    return jsonError(c, 403, 'Cross-origin requests are not accepted.');
  }
  const type = (c.req.header('Content-Type') ?? '').toLowerCase();
  // DELETE usually has no body; everything else must declare JSON.
  if (c.req.method !== 'DELETE' && !type.startsWith('application/json')) {
    return jsonError(c, 415, 'Send this request with Content-Type: application/json.');
  }
  return next();
};

/** A JSON object body, or an empty object. Handlers validate fields anyway. */
export async function readJson(c: App): Promise<Record<string, unknown>> {
  const body = await c.req.json().catch(() => null);
  return body !== null && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : {};
}

/** Cloudflare's own country code; the IP it came from is never stored. */
export function countryOf(request: Request): string | null {
  const country = (request as { cf?: { country?: unknown } }).cf?.country;
  return typeof country === 'string' && /^[A-Z]{2}$/.test(country) ? country : null;
}
