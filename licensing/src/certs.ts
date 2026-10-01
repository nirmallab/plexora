/**
 * Sign and check Plexora licence certificates.
 *
 *   PLEXORA1.<kid>.<base64url(payload JSON)>.<base64url(Ed25519 signature)>
 *
 * The signature covers the raw payload bytes. Deliberately not a JWT: no
 * algorithm field to confuse and one format small enough that this signer and
 * the Python verifier (plexora/licensing/certificate.py) are tested against the
 * same bytes (vectors/plexora-license-vectors.json).
 *
 * Canonical payloads -- keys sorted, no whitespace, UTF-8, integers only -- so
 * this and tools/issue_license.py produce identical bytes for identical claims.
 *
 * Ed25519 through WebCrypto, which workerd and Node both provide: no signing
 * library to keep patched. A private key arrives as the 32-byte seed
 * (base64url) that tools/generate_keys.py prints, and is wrapped in the fixed
 * PKCS#8 header WebCrypto wants.
 */
import { base64url, base64urlDecode } from './crypto';
import type { Env } from './env';

export const PREFIX = 'PLEXORA1';
export const AUDIENCE = 'plexora';
export const VERSION = 1;

export type EnvironmentType = 'desktop' | 'cluster' | 'container-host' | 'job';

export interface CertificatePayload {
  v: 1;
  aud: 'plexora';
  kid: string;
  cert_id: string;
  license_id: string;
  account_id: string;
  seat_id: string;
  plan: 'paid';
  trial: boolean;
  use_class: string;
  entitlements: string[];
  environment_id: string | null;
  environment_type: EnvironmentType;
  env_binding: string | null;
  delegation_pubkey: string | null;
  issued_at: number;
  /** When this certificate stops: at most CERT_MAX_DAYS, renewed quietly. */
  expires_at: number;
  /** When the licence itself ends -- the date a user is shown. Absent on
   * certificates issued before it was added; refresh replaces those. */
  license_expires_at?: number;
  grace_days: number;
  offline_until: number | null;
}

/** JSON with sorted keys and no whitespace, matching Python's json.dumps. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'number' && !Number.isInteger(value)) {
      throw new Error('certificate payloads carry integers only');
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  const record = value as Record<string, unknown>;
  const parts = Object.keys(record)
    .filter((key) => record[key] !== undefined)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`);
  return `{${parts.join(',')}}`;
}

const PKCS8_ED25519_PREFIX = Uint8Array.from([
  0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20,
]);

function pkcs8(seed: Uint8Array): Uint8Array {
  if (seed.length !== 32) throw new Error('an Ed25519 seed is 32 bytes');
  const out = new Uint8Array(PKCS8_ED25519_PREFIX.length + 32);
  out.set(PKCS8_ED25519_PREFIX, 0);
  out.set(seed, PKCS8_ED25519_PREFIX.length);
  return out;
}

async function importPrivate(seed: Uint8Array, extractable = false): Promise<CryptoKey> {
  return crypto.subtle.importKey('pkcs8', pkcs8(seed), { name: 'Ed25519' }, extractable, ['sign']);
}

async function importPublic(raw: Uint8Array): Promise<CryptoKey> {
  return crypto.subtle.importKey('raw', raw, { name: 'Ed25519' }, false, ['verify']);
}

/** The public half of a seed, base64url: for PUBLIC_KEYS_JSON and the tools. */
export async function publicKeyOfSeed(seed: Uint8Array): Promise<string> {
  const jwk = (await crypto.subtle.exportKey('jwk', await importPrivate(seed, true))) as JsonWebKey;
  if (!jwk.x) throw new Error('could not derive the public key');
  return jwk.x;
}

/**
 * The private seed for a kid, served only by that kid's own slot.
 *
 * `SIGNING_KEY_<KID>` and nothing else: setting ACTIVE_KID to px2 before the
 * px2 secret exists makes signing fail loudly, instead of signing with px1's
 * key under px2's name -- which every client would reject, dropping the whole
 * fleet to Free at its next renewal.
 */
function seedFor(env: Env, kid: string): Uint8Array | null {
  if (!/^[a-z0-9]{1,16}$/.test(kid)) return null;
  const raw = env[`SIGNING_KEY_${kid.toUpperCase()}`];
  if (typeof raw !== 'string' || !raw) return null;
  try {
    return base64urlDecode(raw.trim());
  } catch {
    return null;
  }
}

export function activeKid(env: Env): string {
  return String(env.ACTIVE_KID || 'px1');
}

export function signingConfigured(env: Env): boolean {
  return seedFor(env, activeKid(env)) !== null;
}

async function publicKeyFor(env: Env, kid: string): Promise<Uint8Array | null> {
  if (env.PUBLIC_KEYS_JSON) {
    try {
      const table = JSON.parse(env.PUBLIC_KEYS_JSON) as Record<string, unknown>;
      const value = table[kid];
      if (typeof value === 'string' && value) return base64urlDecode(value);
    } catch {
      // A malformed var must not make every certificate unverifiable.
    }
  }
  const seed = seedFor(env, kid);
  return seed ? base64urlDecode(await publicKeyOfSeed(seed)) : null;
}

export async function signPayload(seed: Uint8Array, payload: unknown): Promise<{ body: Uint8Array; signature: Uint8Array }> {
  const body = new TextEncoder().encode(canonicalJson(payload));
  const signature = new Uint8Array(await crypto.subtle.sign({ name: 'Ed25519' }, await importPrivate(seed), body));
  return { body, signature };
}

export async function signCertificate(env: Env, payload: CertificatePayload): Promise<string> {
  const kid = payload.kid;
  const seed = seedFor(env, kid);
  if (!seed) throw new Error(`no signing key configured for kid '${kid}'`);
  const { body, signature } = await signPayload(seed, payload);
  return `${PREFIX}.${kid}.${base64url(body)}.${base64url(signature)}`;
}

/**
 * The three outcomes of checking a certificate, kept apart on purpose.
 *
 *   valid         the signature is ours and the claims are these
 *   forged        we hold the key and it does not verify, or it is not even
 *                 the right shape, or not for this product
 *   unverifiable  no key material for that kid, so we cannot say -- and must
 *                 NOT refuse: an operator who has not set PUBLIC_KEYS_JSON
 *                 after a rotation would otherwise take the fleet offline
 */
export type CertificateCheck =
  | { status: 'valid'; payload: CertificatePayload }
  | { status: 'forged'; reason: string }
  | { status: 'unverifiable'; kid: string };

export async function checkCertificate(env: Env, text: unknown): Promise<CertificateCheck> {
  if (typeof text !== 'string' || text.length > 16384) return { status: 'forged', reason: 'not a certificate' };
  const parts = text.trim().split('.');
  if (parts.length !== 4 || parts[0] !== PREFIX) return { status: 'forged', reason: 'not a certificate' };
  const [, kid, bodyText, signatureText] = parts as [string, string, string, string];
  if (!kid) return { status: 'forged', reason: 'names no signing key' };

  let publicKey: Uint8Array | null = null;
  try {
    publicKey = await publicKeyFor(env, kid);
  } catch {
    publicKey = null;
  }
  if (!publicKey) return { status: 'unverifiable', kid };

  let body: Uint8Array;
  let signature: Uint8Array;
  try {
    body = base64urlDecode(bodyText);
    signature = base64urlDecode(signatureText);
  } catch {
    return { status: 'forged', reason: 'unreadable' };
  }
  let ok = false;
  try {
    ok = await crypto.subtle.verify({ name: 'Ed25519' }, await importPublic(publicKey), signature, body);
  } catch {
    ok = false;
  }
  if (!ok) return { status: 'forged', reason: 'signature does not verify' };

  let payload: CertificatePayload;
  try {
    payload = JSON.parse(new TextDecoder().decode(body)) as CertificatePayload;
  } catch {
    return { status: 'forged', reason: 'payload is not JSON' };
  }
  if (!payload || typeof payload !== 'object') return { status: 'forged', reason: 'payload is not an object' };
  if (payload.kid !== kid) return { status: 'forged', reason: 'names two signing keys' };
  if (payload.aud !== AUDIENCE) return { status: 'forged', reason: 'not a Plexora certificate' };
  if (payload.v !== VERSION) return { status: 'forged', reason: 'unsupported version' };
  return { status: 'valid', payload };
}

/**
 * The same signed format under another prefix and audience -- `PLXAI1` gateway
 * tokens (src/ai/token.ts). Same keys, same kid rotation, same canonical JSON.
 */
export async function signPrefixed(env: Env, prefix: string, payload: { kid: string }): Promise<string> {
  const seed = seedFor(env, payload.kid);
  if (!seed) throw new Error(`no signing key configured for kid '${payload.kid}'`);
  const { body, signature } = await signPayload(seed, payload);
  return `${prefix}.${payload.kid}.${base64url(body)}.${base64url(signature)}`;
}

export type PrefixedCheck =
  | { status: 'valid'; payload: Record<string, unknown> }
  | { status: 'forged'; reason: string }
  | { status: 'unverifiable'; kid: string };

export async function checkPrefixed(env: Env, prefix: string, text: unknown): Promise<PrefixedCheck> {
  if (typeof text !== 'string' || text.length > 8192) return { status: 'forged', reason: 'not a token' };
  const parts = text.trim().split('.');
  if (parts.length !== 4 || parts[0] !== prefix) return { status: 'forged', reason: 'not a token' };
  const [, kid, bodyText, signatureText] = parts as [string, string, string, string];
  let publicKey: Uint8Array | null = null;
  try {
    publicKey = kid ? await publicKeyFor(env, kid) : null;
  } catch {
    publicKey = null;
  }
  if (!publicKey) return { status: 'unverifiable', kid };
  let verified = false;
  let body: Uint8Array;
  try {
    body = base64urlDecode(bodyText);
    verified = await crypto.subtle.verify({ name: 'Ed25519' }, await importPublic(publicKey),
      base64urlDecode(signatureText), body);
  } catch {
    return { status: 'forged', reason: 'unreadable' };
  }
  if (!verified) return { status: 'forged', reason: 'signature does not verify' };
  try {
    const payload = JSON.parse(new TextDecoder().decode(body)) as Record<string, unknown>;
    if (!payload || typeof payload !== 'object' || payload.kid !== kid) {
      return { status: 'forged', reason: 'malformed payload' };
    }
    return { status: 'valid', payload };
  } catch {
    return { status: 'forged', reason: 'payload is not JSON' };
  }
}
