/**
 * Exact-origin CORS for the one kind of web page that calls this Worker: Plexora's website, whose Python runs in a
 * worker beside the page and sends the same anonymous counts a desktop install sends (plexora/browser/telemetry.py),
 * with the same uploader -- register, then gzipped batches under the install token -- through the page's fetch. Every
 * other caller is a Plexora process, sends no Origin, and is answered exactly as before.
 *
 * The rules are biocognia-platform's (packages/shared/src/cors.ts), copied rather than shared: this Worker is
 * deliberately separate from the platform's (wrangler.toml), and the two repositories do not depend on each other.
 *
 *   - An origin is admitted only when it is in WEB_ORIGINS, or in the Worker's extra list (`TELEMETRY_WEB_ORIGINS`:
 *     exact origins for staging or development, separated by spaces or commas; production sets none). No wildcard,
 *     no suffix match, and an origin that is not admitted is never echoed back.
 *   - A preflight (OPTIONS with Origin and Access-Control-Request-Method) ends here, so no handler, rate limit or D1
 *     read runs for one. From an admitted origin it is 204 with that origin, the route's methods and OPTIONS, the
 *     request headers the uploader sends (TELEMETRY_ALLOW_HEADERS), a two-hour max age (Chrome's cap) and
 *     Vary: Origin. From any other origin it is 204 with no Access-Control-Allow-* header, so the browser refuses.
 *   - Every other response to an admitted origin carries Access-Control-Allow-Origin: <origin>, Vary: Origin and
 *     Access-Control-Expose-Headers: Retry-After (the uploader waits what a 429 or 503 says). The handlers here
 *     return raw Responses; Hono's c.header on a finished response makes it again with the headers added.
 *   - Never Access-Control-Allow-Credentials: the page sends no cookie, and the install token is a header.
 */
import type { Context, MiddlewareHandler } from 'hono';

/** The web pages admitted everywhere, production included: Plexora's website and its preview build. */
export const WEB_ORIGINS: readonly string[] = ['https://viewer.plexoraapp.com', 'https://plexora-viewer-preview.pages.dev'];

/**
 * The request headers the uploader sends (plexora/telemetry/uploader.py `_post`): the install token, the JSON body's
 * type and its gzip. Never a User-Agent: plexora/browser/fetch.py drops it, as a page may not set one.
 */
export const TELEMETRY_ALLOW_HEADERS = 'Authorization, Content-Type, Content-Encoding';
/** Response headers a page may read beyond the safelisted ones: how long to wait after a 429 or 503. */
export const CORS_EXPOSE_HEADERS = 'Retry-After';
/** How long a browser may cache a preflight answer, in seconds: two hours is Chrome's cap (Firefox's is a day). */
export const CORS_MAX_AGE = 7200;

/**
 * `value` if it is an origin exactly as a browser sends one -- scheme, host and a non-default port, nothing else, in
 * lower case -- on https, or on http for this machine (localhost, 127.0.0.1); otherwise null.
 */
export function exactOrigin(value: string): string | null {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  // The URL parser takes `*` in a host name, and a browser never sends one: refuse it, so nothing reads as a wildcard.
  if (url.origin !== value || !/^[a-z0-9.-]+$/.test(url.hostname)) return null;
  const loopback = url.protocol === 'http:' && (url.hostname === 'localhost' || url.hostname === '127.0.0.1');
  return url.protocol === 'https:' || loopback ? value : null;
}

/** The Worker's extra origins, from a var holding exact origins separated by spaces or commas; anything else is dropped. */
export function extraOrigins(value: unknown): string[] {
  if (typeof value !== 'string') return [];
  return value.split(/[\s,]+/).filter(Boolean).map(exactOrigin).filter((origin): origin is string => origin !== null);
}

/** The request's Origin when it is admitted, else null. */
export function admittedOrigin(origin: string | null | undefined, extra: readonly string[] = []): string | null {
  if (!origin) return null;
  return WEB_ORIGINS.includes(origin) || extra.includes(origin) ? origin : null;
}

/** Whether a request is a CORS preflight: OPTIONS with an Origin and an Access-Control-Request-Method. */
export function isPreflight(request: Request): boolean {
  return request.method === 'OPTIONS' && request.headers.has('Origin') &&
    request.headers.has('Access-Control-Request-Method');
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyContext = Context<any>;

/**
 * The middleware for one route: `methods` are what the route answers (POST, or GET). Mount it on the route's exact
 * path, before the route's handler. The extra origins are read from `TELEMETRY_WEB_ORIGINS`.
 */
export function cors(options: { methods: readonly string[] }): MiddlewareHandler {
  const methods = [...options.methods, 'OPTIONS'].join(', ');
  return async (c: AnyContext, next) => {
    const origin = admittedOrigin(c.req.header('Origin'), extraOrigins(c.env?.TELEMETRY_WEB_ORIGINS));
    if (isPreflight(c.req.raw)) {
      const headers: Record<string, string> = { Vary: 'Origin' };
      if (origin) {
        headers['Access-Control-Allow-Origin'] = origin;
        headers['Access-Control-Allow-Methods'] = methods;
        headers['Access-Control-Allow-Headers'] = TELEMETRY_ALLOW_HEADERS;
        headers['Access-Control-Max-Age'] = String(CORS_MAX_AGE);
      }
      return c.body(null, 204, headers);
    }
    await next();
    if (!origin) return;
    c.header('Access-Control-Allow-Origin', origin);
    c.header('Access-Control-Expose-Headers', CORS_EXPOSE_HEADERS);
    c.header('Vary', 'Origin', { append: true });
  };
}
