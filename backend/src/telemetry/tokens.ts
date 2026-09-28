/**
 * Install tokens: `PLEXORAT1.<base64url(json {v,kind,tid,iat,exp})>.<base64url(HMAC-SHA256)>`.
 *
 * No secret ships in the client. `/register` exchanges an install id for a
 * token bound to it (`tid`), and every upload must carry a client block whose
 * install_id equals `tid`. HMAC rather than a signature scheme because nothing
 * outside this Worker ever verifies one; the client treats it as opaque.
 *
 * Rotation: verification tries TELEMETRY_HMAC_KEY, then
 * TELEMETRY_HMAC_KEY_PREVIOUS. A token that only the previous key accepts is
 * still honoured, and the response says `rotate: true` so the client
 * re-registers and picks up a token under the current key.
 */
import type { Env } from '../env';

export const TOKEN_PREFIX = 'PLEXORAT1';

export interface TokenPayload {
  v: 1;
  kind: 'telemetry';
  tid: string;
  iat: number;
  exp: number;
}

export interface VerifiedToken {
  payload: TokenPayload;
  /** True when only the previous key verified it. */
  rotate: boolean;
}

const encoder = new TextEncoder();

export function base64url(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function base64urlDecode(text: string): Uint8Array {
  const padded = text.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

// importKey is not free; one CryptoKey per secret per isolate.
const keyCache = new Map<string, Promise<CryptoKey>>();

export function hmacKey(secret: string): Promise<CryptoKey> {
  let key = keyCache.get(secret);
  if (!key) {
    key = crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, [
      'sign',
      'verify',
    ]);
    keyCache.set(secret, key);
  }
  return key;
}

function currentSecret(env: Env): string {
  // Local development only: with no secret set nothing would work at all. A
  // deployed Worker always has the secret (the runbook sets it first).
  return env.TELEMETRY_HMAC_KEY || 'plexora-dev-only-telemetry-key';
}

export async function mintToken(
  env: Env,
  installId: string,
  issuedAt: number,
  ttlSeconds: number,
): Promise<{ token: string; expires: number }> {
  const payload: TokenPayload = {
    v: 1,
    kind: 'telemetry',
    tid: installId,
    iat: issuedAt,
    exp: issuedAt + ttlSeconds,
  };
  const body = base64url(encoder.encode(JSON.stringify(payload)));
  const signature = await crypto.subtle.sign('HMAC', await hmacKey(currentSecret(env)), encoder.encode(body));
  return { token: `${TOKEN_PREFIX}.${body}.${base64url(new Uint8Array(signature))}`, expires: payload.exp };
}

async function verifyWith(secret: string, body: string, signature: Uint8Array): Promise<boolean> {
  try {
    return await crypto.subtle.verify('HMAC', await hmacKey(secret), signature, encoder.encode(body));
  } catch {
    return false;
  }
}

/** The verified payload, or null for anything that is not a valid, unexpired token. */
export async function verifyToken(
  env: Env,
  token: string | null | undefined,
  atSeconds: number,
): Promise<VerifiedToken | null> {
  if (!token || token.length > 1024) return null;
  const parts = token.split('.');
  if (parts.length !== 3 || parts[0] !== TOKEN_PREFIX) return null;
  const [, body, sig] = parts as [string, string, string];

  let signature: Uint8Array;
  try {
    signature = base64urlDecode(sig);
  } catch {
    return null;
  }
  let rotate = false;
  if (!(await verifyWith(currentSecret(env), body, signature))) {
    const previous = env.TELEMETRY_HMAC_KEY_PREVIOUS;
    if (!previous || !(await verifyWith(previous, body, signature))) return null;
    rotate = true;
  }

  try {
    const payload = JSON.parse(new TextDecoder().decode(base64urlDecode(body))) as Partial<TokenPayload>;
    if (payload?.v !== 1 || payload.kind !== 'telemetry') return null;
    if (typeof payload.tid !== 'string' || !/^[0-9a-f]{32}$/.test(payload.tid)) return null;
    if (typeof payload.exp !== 'number' || payload.exp < atSeconds) return null;
    if (typeof payload.iat !== 'number') return null;
    return { payload: payload as TokenPayload, rotate };
  } catch {
    return null;
  }
}

export function bearer(header: string | undefined | null): string | null {
  if (!header) return null;
  const match = /^Bearer\s+(\S+)\s*$/i.exec(header);
  return match ? match[1]! : null;
}

/** Hex SHA-256 of a string or bytes. */
export async function sha256Hex(data: string | Uint8Array | ArrayBuffer): Promise<string> {
  const bytes = typeof data === 'string' ? encoder.encode(data) : data;
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** HMAC-SHA256 hex of `message` under `secret`. */
export async function hmacHex(secret: string, message: string): Promise<string> {
  const mac = await crypto.subtle.sign('HMAC', await hmacKey(secret), encoder.encode(message));
  return [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** Constant-time comparison of two secrets (digest first so lengths match). */
export async function safeEqual(a: string, b: string): Promise<boolean> {
  const [left, right] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(a)),
    crypto.subtle.digest('SHA-256', encoder.encode(b)),
  ]);
  const x = new Uint8Array(left);
  const y = new Uint8Array(right);
  let diff = 0;
  for (let i = 0; i < x.length; i += 1) diff |= x[i]! ^ y[i]!;
  return diff === 0;
}
