/**
 * Small, dependency-free crypto helpers on WebCrypto: encodings, hashes,
 * HMACs, constant-time comparison, identifiers and the two credential shapes.
 *
 *   seat key       PLEX-XXXX-XXXX-XXXX-XXXX   emailed; typed by a person
 *   licence token  PLXT1_<43 base64url>       for automation; pasted into env
 *
 * Both are stored as SHA-256 only. Neither can be recovered from the database;
 * a seat key is re-displayable only through the optional AES-GCM vault.
 */

export function base64url(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function base64urlDecode(text: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]*$/.test(text)) throw new Error('not base64url');
  const padded = text.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

export function hex(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  return Array.from(view, (b) => b.toString(16).padStart(2, '0')).join('');
}

const encoder = new TextEncoder();

export async function sha256Hex(text: string): Promise<string> {
  return hex(await crypto.subtle.digest('SHA-256', encoder.encode(text)));
}

export async function hmacHex(key: string, message: string): Promise<string> {
  const cryptoKey = await crypto.subtle.importKey('raw', encoder.encode(key), { name: 'HMAC', hash: 'SHA-256' },
    false, ['sign']);
  return hex(await crypto.subtle.sign('HMAC', cryptoKey, encoder.encode(message)));
}

/** Constant-time string comparison (by HMAC-ing both under a random key). */
export async function safeEqual(a: string, b: string): Promise<boolean> {
  const key = base64url(crypto.getRandomValues(new Uint8Array(16)));
  const [x, y] = await Promise.all([hmacHex(key, a), hmacHex(key, b)]);
  let diff = 0;
  for (let i = 0; i < x.length; i += 1) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0 && x.length === y.length;
}

export function randomToken(bytes = 32): string {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

/** `acc_1f3a...`: a prefixed random id, 16 hex characters of entropy. */
export function newId(prefix: string): string {
  return `${prefix}_${hex(crypto.getRandomValues(new Uint8Array(8)))}`;
}

// Crockford-ish: no 0/O, 1/I/L, so a key read aloud or copied by hand survives.
const SEAT_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';

export const SEAT_KEY_PATTERN = /^PLEX-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$/;
export const LICENSE_TOKEN_PATTERN = /^PLXT1_[A-Za-z0-9_-]{43}$/;

export function newSeatKey(): string {
  const groups: string[] = [];
  const random = crypto.getRandomValues(new Uint8Array(16));
  for (let g = 0; g < 4; g += 1) {
    let group = '';
    for (let i = 0; i < 4; i += 1) group += SEAT_ALPHABET[random[g * 4 + i]! % SEAT_ALPHABET.length];
    groups.push(group);
  }
  return `PLEX-${groups.join('-')}`;
}

export function newLicenseToken(): string {
  return `PLXT1_${randomToken(32)}`;
}

/** Seat keys are case- and dash-tolerant when a person types one. */
export function normalizeSeatKey(text: string): string {
  const compact = text.toUpperCase().replace(/[^A-Z0-9]/g, '');
  if (!compact.startsWith('PLEX') || compact.length !== 20) return text.trim();
  const body = compact.slice(4);
  return `PLEX-${body.slice(0, 4)}-${body.slice(4, 8)}-${body.slice(8, 12)}-${body.slice(12, 16)}`;
}

/** The binding pepper applied to a client's environment binding. */
export async function pepper(env: { FP_PEPPER?: string }, value: string): Promise<string> {
  if (!env.FP_PEPPER) throw new Error('FP_PEPPER is not configured');
  return hmacHex(env.FP_PEPPER, value);
}

/** The IP of a request, peppered; never stored raw. */
export async function ipHash(env: { IP_HASH_KEY?: string }, request: Request): Promise<string | null> {
  const ip = request.headers.get('CF-Connecting-IP');
  if (!ip || !env.IP_HASH_KEY) return null;
  return (await hmacHex(env.IP_HASH_KEY, ip)).slice(0, 32);
}

/** AES-GCM for the optional seat-key vault. `KEY_VAULT_KEY` is base64 of 32 bytes. */
async function vaultKey(secret: string): Promise<CryptoKey> {
  const raw = Uint8Array.from(atob(secret), (c) => c.charCodeAt(0));
  return crypto.subtle.importKey('raw', raw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
}

export async function vaultSeal(secret: string | undefined, plain: string): Promise<string | null> {
  if (!secret) return null;
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const sealed = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, await vaultKey(secret), encoder.encode(plain));
  return `v1.${base64url(iv)}.${base64url(new Uint8Array(sealed))}`;
}

export async function vaultOpen(secret: string | undefined, sealed: string | null): Promise<string | null> {
  if (!secret || !sealed) return null;
  const [version, ivText, body] = sealed.split('.');
  if (version !== 'v1' || !ivText || !body) return null;
  try {
    const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: base64urlDecode(ivText) },
      await vaultKey(secret), base64urlDecode(body));
    return new TextDecoder().decode(plain);
  } catch {
    return null;
  }
}
