/**
 * Copyright (c) 2026 BioCognia. All rights reserved.
 *
 * Verify a BioCognia certificate in the browser, with the platform's own
 * verifier.
 *
 * The first part is VENDORED from nirmallab/biocognia-platform at eb34d23:
 * `checkSigned`, `canonicalJson` and their constants from
 * packages/shared/src/verify.ts, and `base64url` / `base64urlDecode` / `hex`
 * from packages/shared/src/crypto.ts, with TypeScript's types taken out and
 * nothing else changed. Re-vendor it when those files change;
 * tests/js/license_probe.mjs runs the platform's vectors
 * (vectors/biocognia-vectors.json, the same file the `biocognia` package
 * ships) through it.
 *
 * The second part is the `biocognia` package's own reading of a certificate
 * (biocognia/certificate.py `verify`, biocognia/environment.py
 * `certificate_binds_here`), on the same primitives, so the page names a
 * refusal with the same code Python does (`malformed`, `unknown_key`,
 * `bad_signature`, `wrong_product`, `unsupported_version`, `future_dated`).
 * The platform's three outcomes (valid / forged / unverifiable) are for a
 * server deciding whether to refuse a client; a client wants the reason.
 *
 * `PUBLIC_KEYS` is biocognia/keys.py's table (tests/test_license_page.py holds
 * them equal). The staging kid `bcs1` is trusted by no build.
 *
 * An ES module: services/licenseClient.js imports it, and so does the probe.
 */

// -- vendored: packages/shared/src/crypto.ts ------------------------------------

export function base64url(bytes) {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function base64urlDecode(text) {
  if (!/^[A-Za-z0-9_-]*$/.test(text)) throw new Error('not base64url');
  const padded = text.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

export function hex(bytes) {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  return Array.from(view, (b) => b.toString(16).padStart(2, '0')).join('');
}

// -- vendored: packages/shared/src/verify.ts --------------------------------------

export const CERT_PREFIX = 'BIOC1';
export const AI_TOKEN_PREFIX = 'BIOCAI1';
export const JOB_PREFIX = 'BIOCD1';
export const VERSION = 1;

/** JSON with sorted keys and no whitespace, matching Python's json.dumps. */
export function canonicalJson(value) {
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'number' && !Number.isInteger(value)) {
      throw new Error('signed payloads carry integers only');
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  const record = value;
  const parts = Object.keys(record)
    .filter((key) => record[key] !== undefined)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`);
  return `{${parts.join(',')}}`;
}

async function importPublic(raw) {
  return crypto.subtle.importKey('raw', raw, { name: 'Ed25519' }, false, ['verify']);
}

/**
 * The three outcomes, kept apart on purpose.
 *
 *   valid         the signature is ours and the claims are these
 *   forged        we hold the key and it does not verify, or it is not even
 *                 the right shape, or not for this audience
 *   unverifiable  no key for that kid: we cannot say, and must NOT refuse a
 *                 client over it (a rotation the operator has not finished)
 */
export async function checkSigned(keys, prefix, text, options = {}) {
  if (typeof text !== 'string' || text.length > (options.maxLength ?? 16384)) {
    return { status: 'forged', reason: 'not a signed artefact' };
  }
  const parts = text.trim().split('.');
  if (parts.length !== 4 || parts[0] !== prefix) return { status: 'forged', reason: 'wrong format' };
  const [, kid, bodyText, signatureText] = parts;
  if (!kid) return { status: 'forged', reason: 'names no signing key' };

  let publicKey = null;
  try {
    publicKey = keys[kid] ? base64urlDecode(keys[kid]) : options.fallback ? await options.fallback(kid) : null;
  } catch {
    publicKey = null;
  }
  if (!publicKey) return { status: 'unverifiable', kid };

  let body;
  let ok = false;
  try {
    body = base64urlDecode(bodyText);
    ok = await crypto.subtle.verify({ name: 'Ed25519' }, await importPublic(publicKey),
      base64urlDecode(signatureText), body);
  } catch {
    return { status: 'forged', reason: 'unreadable' };
  }
  if (!ok) return { status: 'forged', reason: 'signature does not verify' };

  let payload;
  try {
    payload = JSON.parse(new TextDecoder().decode(body));
  } catch {
    return { status: 'forged', reason: 'payload is not JSON' };
  }
  if (!payload || typeof payload !== 'object') return { status: 'forged', reason: 'payload is not an object' };
  if (payload.kid !== kid) return { status: 'forged', reason: 'names two signing keys' };
  if (payload.v !== VERSION) return { status: 'forged', reason: 'unsupported version' };
  const audience = options.audience;
  if (audience !== undefined) {
    const okAudience = typeof audience === 'string' ? payload.aud === audience : audience(payload.aud);
    if (!okAudience) return { status: 'forged', reason: 'not for this audience' };
  }
  return { status: 'valid', payload };
}

// -- the client's reading (biocognia/certificate.py, biocognia/environment.py) ----

/** biocognia/keys.py PUBLIC_KEYS: kid -> base64url raw Ed25519 public key. */
export const PUBLIC_KEYS = Object.freeze({
  bc1: 'TIwPRL4zrhfy0uxavk5oEE-ghfF4_e6sWPA3F8qGA2Q',
  bc2: 'M25UvdgFzazAzLV7QFjam_D0QsespteU_bBOuNlGuHw',
});

/** A certificate's kinds: `env` (one device), `job` and `ci` (unbound, short). */
export const TYPES = Object.freeze(['env', 'job', 'ci']);
/** A certificate dated this far ahead is a clock problem worth naming. */
export const MAX_CLOCK_SKEW_SECONDS = 300;
/** Far larger than any real certificate (under 2 KB). */
export const MAX_LENGTH = 16 * 1024;
/** `bind` forms, told apart by `bv`: the binding itself, or sha256 of it. */
export const BIND_RAW = 1;
export const BIND_COMMITMENT = 2;

/** A refusal, with the code biocognia/errors.py gives it. */
export class CertificateError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

export function looksLike(text) {
  return typeof text === 'string' && text.trim().startsWith(`${CERT_PREFIX}.`);
}

function decodeField(text, what) {
  if (typeof text !== 'string' || !text || /[+/=]/.test(text)) {
    throw new CertificateError('malformed', `the certificate has an unreadable ${what}`);
  }
  try {
    return base64urlDecode(text);
  } catch {
    throw new CertificateError('malformed', `the certificate has an unreadable ${what}`);
  }
}

/** `{kid, payload, signature, signed}` of a certificate, unverified. */
export function parse(certificate) {
  if (typeof certificate !== 'string') throw new CertificateError('malformed', 'a certificate is a string');
  const text = certificate.trim();
  if (text.length > MAX_LENGTH) throw new CertificateError('malformed', 'the certificate is too long to be one');
  const parts = text.split('.');
  if (parts.length !== 4) {
    throw new CertificateError('malformed', `a certificate has four dot-separated fields; this has ${parts.length}`);
  }
  const [prefix, kid, payloadText, signatureText] = parts;
  if (prefix !== CERT_PREFIX) {
    throw new CertificateError('malformed', `unknown certificate format ${JSON.stringify(prefix)}`);
  }
  if (!kid) throw new CertificateError('malformed', 'the certificate names no signing key');
  const signed = decodeField(payloadText, 'payload');
  const signature = decodeField(signatureText, 'signature');
  let payload;
  try {
    payload = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(signed));
  } catch {
    throw new CertificateError('malformed', 'the certificate payload is not JSON');
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new CertificateError('malformed', 'the certificate payload is not an object');
  }
  return { kid, payload, signature, signed };
}

/** biocognia/certificate.py `check_claims`: what every kind shares. */
export function checkClaims(payload, { audience, kid = null, now = null } = {}) {
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
  if (!TYPES.includes(typ) && payload.kind !== 'job') {
    throw new CertificateError('malformed', `unknown certificate type ${JSON.stringify(typ)}`);
  }
  if ('ent' in payload && !Array.isArray(payload.ent)) {
    throw new CertificateError('malformed', "the certificate's entitlements are not a list");
  }
  if ('lim' in payload && (payload.lim === null || typeof payload.lim !== 'object' || Array.isArray(payload.lim))) {
    throw new CertificateError('malformed', "the certificate's limits are not an object");
  }
  const moment = now === null ? Date.now() / 1000 : now;
  const issued = payload.iat;
  if (typeof issued === 'number' && issued > moment + MAX_CLOCK_SKEW_SECONDS) {
    throw new CertificateError('future_dated', "the certificate is dated in the future; check this machine's clock");
  }
}

/**
 * The payload of `certificate`, once its signature and claims check out
 * (biocognia/certificate.py `verify`); throws a `CertificateError` otherwise.
 * Expiry is not checked here: an expired certificate is still authentic.
 */
export async function verify(certificate, { audience, now = null, keys = PUBLIC_KEYS } = {}) {
  const { kid, payload, signature, signed } = parse(certificate);
  const encoded = Object.prototype.hasOwnProperty.call(keys, kid) ? keys[kid] : undefined;
  if (encoded === undefined) {
    throw new CertificateError('unknown_key', `this certificate was signed with key ${JSON.stringify(kid)}, which this version does not trust`);
  }
  let raw;
  try {
    raw = base64urlDecode(encoded);
  } catch {
    raw = null;
  }
  if (!raw || raw.length !== 32) {
    throw new CertificateError('unknown_key', `the trusted key ${JSON.stringify(kid)} is not a valid public key`);
  }
  let ok;
  try {
    ok = await crypto.subtle.verify({ name: 'Ed25519' }, await importPublic(raw), signature, signed);
  } catch (error) {
    // A browser without Ed25519 in WebCrypto cannot say: Python in the
    // worker verifies the same file with its own library.
    if (error && (error.name === 'NotSupportedError' || /algorithm/i.test(String(error.message)))) {
      throw new CertificateError('unverifiable', 'this browser cannot check Ed25519 signatures');
    }
    ok = false;
  }
  if (!ok) {
    throw new CertificateError('bad_signature', 'the certificate signature does not verify; it has been altered or truncated');
  }
  checkClaims(payload, { audience, kid, now });
  return payload;
}

const encoder = new TextEncoder();

/** The SHA-256 of `text`'s bytes, in hex. */
export async function sha256Hex(text) {
  return hex(await crypto.subtle.digest('SHA-256', encoder.encode(text)));
}

/** What the platform is told of a device secret: sha256(secret), hex (environment.binding_of). */
export function bindingOf(secret) {
  return sha256Hex(secret);
}

/** What a `bv: 2` certificate carries: sha256 of the binding's hex (environment.commitment_of). */
export function commitmentOf(bindingHex) {
  return sha256Hex(bindingHex);
}

/**
 * Whether a certificate's `bind` names the device whose secret is `secret`,
 * in the form its `bv` says (environment.certificate_binds_here). No secret,
 * no bound certificate.
 */
export async function bindsHere(payload, secret) {
  const claimed = payload && payload.bind;
  if (typeof claimed !== 'string' || !claimed) return false;
  if (typeof secret !== 'string' || secret.length < 32) return false;
  const own = await bindingOf(secret);
  const version = Object.prototype.hasOwnProperty.call(payload, 'bv') ? payload.bv : BIND_RAW;
  if (typeof version === 'boolean') return false;
  let expected;
  if (version === null || version === BIND_RAW) expected = own;
  else if (version === BIND_COMMITMENT) expected = await commitmentOf(own);
  else return false;
  const given = claimed.trim().toLowerCase();
  if (given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i += 1) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}
