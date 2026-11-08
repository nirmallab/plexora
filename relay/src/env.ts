/**
 * The relay's bindings and its `[vars]` (wrangler.toml), read the same way everywhere. Every var is a string; a missing
 * or unreadable one falls back to the default docs/internal/WEB_REMOTE_RELAY.md gives it.
 */
import type { Room } from './room';

export interface Env {
  ROOMS: DurableObjectNamespace<Room>;
  /** Page origins, exact, separated by commas or spaces. Default: the website. */
  ALLOWED_ORIGINS?: string;
  /** TCP ports `/v1/ssh` may open, separated by commas or spaces. Default: 22. */
  SSH_PORTS?: string;
  /** Days a room with no sockets keeps its record. Default: 7. */
  ROOM_IDLE_DAYS?: string;
  /** Where the free/licensed switch is read. Empty: always free. */
  FEATURES_URL?: string;
  /** "1" lets `/v1/ssh` reach loopback and private addresses. Development only (`.dev.vars`); never in wrangler.toml. */
  ALLOW_PRIVATE_HOSTS?: string;
  /** `/v1/ssh` opens per client address (ratelimit.ts). Absent: no limit. */
  SSH_LIMIT?: RateLimit;
  /** Rooms created per client address (ratelimit.ts). Absent: no limit. */
  ROOM_CREATE_LIMIT?: RateLimit;
}

export const DEFAULT_ORIGINS: readonly string[] = ['https://viewer.plexoraapp.com'];
export const DEFAULT_SSH_PORTS: readonly number[] = [22];
export const DEFAULT_ROOM_IDLE_DAYS = 7;

/**
 * `value` if it is an origin exactly as a browser sends one -- scheme, host and a non-default port, nothing else, in
 * lower case -- on https, or on http for this machine (localhost, 127.0.0.1); otherwise null. The rule of
 * backend/src/cors.ts `exactOrigin`, plus one development form: `http://localhost:*` / `http://127.0.0.1:*` admits
 * that loopback host on any port, so `.dev.vars` need not know the port a local website runs on.
 */
export function exactOrigin(value: string): string | null {
  const loopbackAnyPort = /^http:\/\/(localhost|127\.0\.0\.1):\*$/.exec(value);
  if (loopbackAnyPort) return value;
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

/** ALLOWED_ORIGINS as a list; entries that are not exact origins are dropped. Unset means the default. */
export function allowedOrigins(env: Env): string[] {
  if (typeof env.ALLOWED_ORIGINS !== 'string') return [...DEFAULT_ORIGINS];
  return env.ALLOWED_ORIGINS.split(/[\s,]+/).filter(Boolean).map(exactOrigin).filter((o): o is string => o !== null);
}

/** The request's Origin when ALLOWED_ORIGINS admits it, else null. Never a suffix or prefix match. */
export function admittedOrigin(origin: string | null, env: Env): string | null {
  if (!origin) return null;
  for (const allowed of allowedOrigins(env)) {
    if (allowed === origin) return origin;
    if (allowed.endsWith(':*')) {
      const host = allowed.slice(0, -2);
      const rest = origin.startsWith(`${host}:`) ? origin.slice(host.length + 1) : null;
      if (rest !== null && /^[1-9][0-9]{0,4}$/.test(rest) && Number(rest) <= 65535) return origin;
      if (origin === host) return origin;
    }
  }
  return null;
}

/** SSH_PORTS as a list of valid TCP ports. Unset means the default; an entry that is not a port is dropped. */
export function sshPorts(env: Env): number[] {
  if (typeof env.SSH_PORTS !== 'string') return [...DEFAULT_SSH_PORTS];
  return env.SSH_PORTS.split(/[\s,]+/).filter((p) => /^[1-9][0-9]{0,4}$/.test(p)).map(Number).filter((p) => p <= 65535);
}

/** ROOM_IDLE_DAYS in milliseconds. A value that is not a positive number means the default. */
export function roomIdleMs(env: Env): number {
  const days = Number(env.ROOM_IDLE_DAYS);
  return (Number.isFinite(days) && days > 0 ? days : DEFAULT_ROOM_IDLE_DAYS) * 86_400_000;
}

export function allowPrivateHosts(env: Env): boolean {
  return env.ALLOW_PRIVATE_HOSTS === '1';
}
