/**
 * Shared HTTP plumbing: the app type, the ONE error shape, JSON/HTML responses
 * with their security headers, and the same-origin guard in front of every
 * portal and admin mutation.
 *
 * Every non-2xx answer is
 *
 *   {"error": {"code", "message", "retry_after"?, "next_allowed_at"?, "details"?}}
 *
 * with `code` from ERROR_CODES, which plexora/licensing/client.py mirrors as
 * SERVER_CODES. A client that cannot parse an error stays where it is: on the
 * certificate it has, or on Free.
 */
import type { Context, MiddlewareHandler } from 'hono';
import type { ContentfulStatusCode } from 'hono/utils/http-status';

import type { Env } from './env';
import { CLIENT_JS } from './ui/client';
import { STYLES } from './ui/styles';

export type AppEnv = {
  Bindings: Env;
  Variables: {
    admin: string;
    userId: string;
    ipHash: string | null;
  };
};

export type App = Context<AppEnv>;

export const ERROR_CODES = [
  'invalid_request', 'invalid_credential', 'credential_revoked', 'credential_expired',
  'scope_not_allowed', 'license_expired', 'license_revoked', 'license_suspended',
  'seat_env_limit', 'cooldown_active', 'rate_limited', 'trial_already_issued',
  'trial_machine_limit', 'trial_not_available', 'forged_certificate',
  'environment_unknown', 'environment_mismatch', 'not_delegating', 'offline_not_allowed',
  'signing_unavailable', 'not_found', 'unauthorized', 'forbidden', 'seat_limit',
  'conflict', 'internal_error',
  // Plexora AI gateway.
  'invalid_token', 'token_expired', 'ai_not_entitled', 'ai_disabled', 'dev_not_allowed',
  'capability_not_allowed', 'insufficient_credits', 'run_envelope_exceeded', 'run_closed',
  'idempotency_in_progress', 'idempotency_conflict', 'request_too_large',
  'provider_rate_limited', 'provider_unavailable', 'provider_rejected', 'route_not_publishable',
] as const;

export type ErrorCode = (typeof ERROR_CODES)[number];

/** Raised anywhere below a route and turned into the error shape by `onError`. */
export class ApiError extends Error {
  constructor(
    readonly status: ContentfulStatusCode,
    readonly code: ErrorCode,
    message: string,
    readonly extra: { retry_after?: number; next_allowed_at?: number; details?: unknown } = {},
  ) {
    super(message);
  }
}

/** API responses: never cached, never sniffed. */
export const NO_STORE = { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' };

export function fail(c: App, error: ApiError) {
  const headers: Record<string, string> = { ...NO_STORE };
  if (error.extra.retry_after) headers['Retry-After'] = String(error.extra.retry_after);
  return c.json({ error: { code: error.code, message: error.message, ...error.extra } }, error.status, headers);
}

export function ok(c: App, body: unknown, status: ContentfulStatusCode = 200) {
  return c.json(body, status, NO_STORE);
}

export function isHttps(c: App): boolean {
  return new URL(c.req.url).protocol === 'https:';
}

/** A JSON object body, or an ApiError. */
export async function readJson(c: App): Promise<Record<string, unknown>> {
  const body = await c.req.json().catch(() => null);
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw new ApiError(400, 'invalid_request', 'Send a JSON object.');
  }
  return body as Record<string, unknown>;
}

export function str(body: Record<string, unknown>, name: string, max = 256): string | null {
  const value = body[name];
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed && trimmed.length <= max ? trimmed : null;
}

export function int(body: Record<string, unknown>, name: string): number | null {
  const value = body[name];
  return typeof value === 'number' && Number.isInteger(value) ? value : null;
}

let hashes: { script: string; style: string } | null = null;

async function b64sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return btoa(String.fromCharCode(...new Uint8Array(digest)));
}

async function inlineHashes() {
  hashes ??= { script: await b64sha256(CLIENT_JS), style: await b64sha256(STYLES) };
  return hashes;
}

/** A portal or admin page. Nothing loads from anywhere else. */
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
 * CSRF defence for portal and admin mutations: JSON only (a cross-origin form
 * cannot send it without a preflight the browser will not grant), and
 * Sec-Fetch-Site/Origin must say same-origin where the browser sends them.
 */
export const sameOriginGuard: MiddlewareHandler<AppEnv> = async (c, next) => {
  if (c.req.method === 'GET' || c.req.method === 'HEAD') return next();
  const site = c.req.header('Sec-Fetch-Site');
  if (site !== undefined && site !== 'same-origin' && site !== 'none') {
    return fail(c, new ApiError(403, 'forbidden', 'Cross-site requests are not accepted.'));
  }
  const origin = c.req.header('Origin');
  if (origin && origin !== new URL(c.req.url).origin) {
    return fail(c, new ApiError(403, 'forbidden', 'Cross-origin requests are not accepted.'));
  }
  const type = (c.req.header('Content-Type') ?? '').toLowerCase();
  if (c.req.method !== 'DELETE' && !type.startsWith('application/json')) {
    return fail(c, new ApiError(415 as ContentfulStatusCode, 'invalid_request',
      'Send this request with Content-Type: application/json.'));
  }
  return next();
};
