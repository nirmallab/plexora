/**
 * The website's licence switch and the certificate check behind it (docs/internal/WEB_REMOTE_RELAY.md, "Licence
 * switch").
 *
 * The switch is read from FEATURES_URL (`{"remote_servers":{"website":"free"|"licensed","local":…}}`), kept 60 s per
 * isolate, and when it cannot be read the last value this isolate saw stands, else "free". An empty FEATURES_URL is
 * always free.
 *
 * When the switch is "licensed", `/v1/ssh` and page room sockets present `l.<certificate>`: a BIOC1 certificate, the
 * one the website holds. It is checked the way the website checks it, by a port of
 * plexora/client/src/js/services/licenseVerify.js (itself the platform's packages/shared/src/verify.ts and the
 * `biocognia` package's reading of a certificate): Ed25519 over the payload, a trusted kid, `aud` plexora, `v` 1, a known
 * type, not dated in the future. Then expiry the way licenseClient.js `stateForPayload`/`applyExpiry` reads it: the
 * earlier of `exp` and `offline_until`, plus `grace_days`; a certificate in grace is still licensed there, so here too.
 *
 * What the relay cannot check: the device binding (`bind` names the browser's own secret, which never leaves it) and
 * revocation (the platform's, read by the page). A certificate copied out of one browser therefore opens the relay for
 * another until it expires; the page's own check is the one that binds.
 */
import { LICENCE_REQUIRED } from './protocol';

/** biocognia/keys.py PUBLIC_KEYS, as licenseVerify.js holds it: kid -> base64url raw Ed25519 public key. */
export const PUBLIC_KEYS: Readonly<Record<string, string>> = Object.freeze({
  bc1: 'TIwPRL4zrhfy0uxavk5oEE-ghfF4_e6sWPA3F8qGA2Q',
  bc2: 'M25UvdgFzazAzLV7QFjam_D0QsespteU_bBOuNlGuHw',
});

export const PRODUCT = 'plexora';
export const CERT_PREFIX = 'BIOC1';
export const VERSION = 1;
export const TYPES: readonly string[] = ['env', 'job', 'ci'];
export const MAX_CLOCK_SKEW_SECONDS = 300;
export const MAX_LENGTH = 16 * 1024;

export type WebsiteSwitch = 'free' | 'licensed';

// -- the switch -----------------------------------------------------------------------------------------------------

export const FEATURES_CACHE_MS = 60_000;
const FEATURES_TIMEOUT_MS = 5_000;

interface Cached {
  value: WebsiteSwitch;
  at: number;
}

/**
 * Per isolate, per URL: the last value read and when it was read (or last tried). Concurrent misses each read: a
 * promise shared across requests would hang every waiter if the request that started it were cancelled.
 */
const cache = new Map<string, Cached>();

/** For tests: forget every switch value this isolate has seen. */
export function resetFeaturesCache(): void {
  cache.clear();
}

function readSwitch(body: unknown): WebsiteSwitch | null {
  if (!body || typeof body !== 'object') return null;
  const servers = (body as { remote_servers?: unknown }).remote_servers;
  if (!servers || typeof servers !== 'object') return null;
  const website = (servers as { website?: unknown }).website;
  return website === 'free' || website === 'licensed' ? website : null;
}

/** "free" or "licensed" for the website, as FEATURES_URL says now (or said within the last 60 s). */
export async function websiteSwitch(featuresUrl: string | undefined, now = Date.now()): Promise<WebsiteSwitch> {
  const url = (featuresUrl ?? '').trim();
  if (!url) return 'free';
  const held = cache.get(url);
  if (held && now - held.at < FEATURES_CACHE_MS) return held.value;
  let value: WebsiteSwitch | null = null;
  try {
    const response = await fetch(url, {
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(FEATURES_TIMEOUT_MS),
    });
    if (response.ok) value = readSwitch(await response.json());
    else await response.body?.cancel();
  } catch {
    value = null;
  }
  // Unreadable: the last value seen stands, else free. Either way it is not asked again for 60 s.
  const settled = value ?? cache.get(url)?.value ?? 'free';
  cache.set(url, { value: settled, at: now });
  return settled;
}

// -- the certificate --------------------------------------------------------------------------------------------------

export class CertificateError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
  }
}

function base64urlDecode(text: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]*$/.test(text)) throw new Error('not base64url');
  const padded = text.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function decodeField(text: string | undefined, what: string): Uint8Array {
  if (typeof text !== 'string' || !text || /[+/=]/.test(text)) {
    throw new CertificateError('malformed', `the certificate has an unreadable ${what}`);
  }
  try {
    return base64urlDecode(text);
  } catch {
    throw new CertificateError('malformed', `the certificate has an unreadable ${what}`);
  }
}

export type Payload = Record<string, unknown>;

/** licenseVerify.js `parse`: `{kid, payload, signature, signed}`, unverified. */
export function parse(certificate: string): { kid: string; payload: Payload; signature: Uint8Array; signed: Uint8Array } {
  if (typeof certificate !== 'string') throw new CertificateError('malformed', 'a certificate is a string');
  const text = certificate.trim();
  if (text.length > MAX_LENGTH) throw new CertificateError('malformed', 'the certificate is too long to be one');
  const parts = text.split('.');
  if (parts.length !== 4) {
    throw new CertificateError('malformed', `a certificate has four dot-separated fields; this has ${parts.length}`);
  }
  const [prefix, kid, payloadText, signatureText] = parts;
  if (prefix !== CERT_PREFIX) throw new CertificateError('malformed', `unknown certificate format ${JSON.stringify(prefix)}`);
  if (!kid) throw new CertificateError('malformed', 'the certificate names no signing key');
  const signed = decodeField(payloadText, 'payload');
  const signature = decodeField(signatureText, 'signature');
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(signed));
  } catch {
    throw new CertificateError('malformed', 'the certificate payload is not JSON');
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new CertificateError('malformed', 'the certificate payload is not an object');
  }
  return { kid, payload: payload as Payload, signature, signed };
}

/** licenseVerify.js `checkClaims`: what every kind shares. */
export function checkClaims(payload: Payload, options: { audience: string; kid?: string | null; now: number }): void {
  const { audience, kid = null, now } = options;
  if (kid !== null && payload.kid !== kid) {
    throw new CertificateError('bad_signature', 'the certificate names two different signing keys');
  }
  if (payload.aud !== audience) {
    throw new CertificateError('wrong_product', `this certificate is for ${JSON.stringify(payload.aud)}, not ${JSON.stringify(audience)}`);
  }
  if (payload.v !== VERSION) {
    throw new CertificateError('unsupported_version', `certificate version ${JSON.stringify(payload.v)} is not supported by this version`);
  }
  const typ = payload.typ === undefined ? 'env' : payload.typ;
  if (!TYPES.includes(typ as string) && payload.kind !== 'job') {
    throw new CertificateError('malformed', `unknown certificate type ${JSON.stringify(typ)}`);
  }
  if ('ent' in payload && !Array.isArray(payload.ent)) {
    throw new CertificateError('malformed', "the certificate's entitlements are not a list");
  }
  if ('lim' in payload && (payload.lim === null || typeof payload.lim !== 'object' || Array.isArray(payload.lim))) {
    throw new CertificateError('malformed', "the certificate's limits are not an object");
  }
  if (typeof payload.iat === 'number' && payload.iat > now + MAX_CLOCK_SKEW_SECONDS) {
    throw new CertificateError('future_dated', "the certificate is dated in the future; check this machine's clock");
  }
}

/**
 * licenseVerify.js `verify`: the payload once signature and claims check out, else a CertificateError. Expiry is not
 * checked here (an expired certificate is still authentic); `licensed` adds it.
 */
export async function verify(
  certificate: string,
  options: { audience: string; now: number; keys?: Readonly<Record<string, string>> },
): Promise<Payload> {
  const keys = options.keys ?? PUBLIC_KEYS;
  const { kid, payload, signature, signed } = parse(certificate);
  const encoded = Object.prototype.hasOwnProperty.call(keys, kid) ? keys[kid] : undefined;
  if (encoded === undefined) {
    throw new CertificateError('unknown_key', `this certificate was signed with key ${JSON.stringify(kid)}, which this version does not trust`);
  }
  let raw: Uint8Array | null;
  try {
    raw = base64urlDecode(encoded);
  } catch {
    raw = null;
  }
  if (!raw || raw.length !== 32) {
    throw new CertificateError('unknown_key', `the trusted key ${JSON.stringify(kid)} is not a valid public key`);
  }
  let ok = false;
  try {
    const key = await crypto.subtle.importKey('raw', raw, { name: 'Ed25519' }, false, ['verify']);
    ok = await crypto.subtle.verify({ name: 'Ed25519' }, key, signature, signed);
  } catch {
    ok = false;
  }
  if (!ok) {
    throw new CertificateError('bad_signature', 'the certificate signature does not verify; it has been altered or truncated');
  }
  checkClaims(payload, { audience: options.audience, kid, now: options.now });
  return payload;
}

function number(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * When the certificate stops being a licence, in seconds since the epoch, or null for never: licenseClient.js
 * `stateForPayload` (the earlier of `exp` and `offline_until`) plus its grace (`grace_days`), after which `applyExpiry`
 * calls it expired.
 */
export function licensedUntil(payload: Payload): number | null {
  let expires = number(payload.exp);
  const offlineUntil = number(payload.offline_until);
  if (offlineUntil !== null) expires = expires === null ? offlineUntil : Math.min(expires, offlineUntil);
  if (expires === null) return null;
  const graceDays = number(payload.grace_days);
  return expires + (graceDays !== null && graceDays > 0 ? graceDays * 86_400 : 0);
}

let trustedKeys: Readonly<Record<string, string>> = PUBLIC_KEYS;

/** For tests only: trust these keys instead of the platform's (the vectors' `vt1`). `null` restores the platform's. */
export function setTrustedKeysForTests(keys: Readonly<Record<string, string>> | null): void {
  trustedKeys = keys ?? PUBLIC_KEYS;
}

export type LicenceCheck = { ok: true } | { ok: false; code: string; message: string };

/** Whether `certificate` is a Plexora licence now: it verifies, is for plexora, and has not expired. */
export async function checkLicence(certificate: string | null, now = Date.now() / 1000): Promise<LicenceCheck> {
  if (!certificate) return { ok: false, code: 'no_license', message: 'no licence certificate was presented' };
  try {
    const payload = await verify(certificate, { audience: PRODUCT, now, keys: trustedKeys });
    const until = licensedUntil(payload);
    if (until !== null && now > until) return { ok: false, code: 'expired', message: 'the licence has expired' };
    return { ok: true };
  } catch (error) {
    if (error instanceof CertificateError) return { ok: false, code: error.code, message: error.message };
    return { ok: false, code: 'malformed', message: 'the certificate could not be read' };
  }
}

/**
 * The gate for a website socket: null when it may go on, else the refusal reason. Reads the switch; when it is
 * "licensed" the certificate must check out.
 */
export async function licenceRefusal(featuresUrl: string | undefined, certificate: string | null): Promise<string | null> {
  if ((await websiteSwitch(featuresUrl)) !== 'licensed') return null;
  const result = await checkLicence(certificate);
  return result.ok ? null : LICENCE_REQUIRED;
}
