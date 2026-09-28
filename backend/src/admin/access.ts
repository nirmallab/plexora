/**
 * Cloudflare Access, verified rather than trusted.
 *
 * Access puts a signed JWT on every request it lets through. The header
 * `Cf-Access-Authenticated-User-Email` is an ordinary header that anything
 * reaching the Worker by another path could set, so it is never read; the
 * RS256 signature is checked against the team's published keys, with iss,
 * aud and exp, every time.
 */
import type { Env } from '../env';
import { base64urlDecode } from '../telemetry/tokens';

interface Jwk extends JsonWebKey {
  kid?: string;
}

const JWKS_TTL_S = 3600;
let cache: { issuer: string; keys: Jwk[]; fetchedAt: number } | null = null;

/** Test seam: the module cache would otherwise leak between cases. */
export function resetAccessCache(): void {
  cache = null;
}

export function accessConfigured(env: Env): boolean {
  return Boolean(env.ACCESS_TEAM_DOMAIN && env.ACCESS_AUD);
}

function issuerFor(env: Env): string {
  return `https://${String(env.ACCESS_TEAM_DOMAIN ?? '').replace(/^https?:\/\//, '').replace(/\/$/, '')}`;
}

async function jwks(env: Env, force = false): Promise<Jwk[]> {
  const issuer = issuerFor(env);
  const fresh = cache !== null && cache.issuer === issuer && Date.now() / 1000 - cache.fetchedAt < JWKS_TTL_S;
  if (fresh && !force) return cache!.keys;
  try {
    const response = await fetch(`${issuer}/cdn-cgi/access/certs`);
    if (!response.ok) return fresh ? cache!.keys : [];
    const document = (await response.json()) as { keys?: Jwk[] };
    cache = { issuer, keys: document.keys ?? [], fetchedAt: Date.now() / 1000 };
    return cache.keys;
  } catch {
    return fresh ? cache!.keys : [];
  }
}

async function keyFor(env: Env, kid: string): Promise<CryptoKey | null> {
  let jwk = (await jwks(env)).find((candidate) => candidate.kid === kid);
  // An unknown kid usually means Access rotated; refetch once, never more, or
  // a bad token becomes a way to hammer the certs endpoint.
  if (!jwk) jwk = (await jwks(env, true)).find((candidate) => candidate.kid === kid);
  if (!jwk) return null;
  return crypto.subtle
    .importKey('jwk', jwk, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify'])
    .catch(() => null);
}

function segment(text: string): Record<string, unknown> | null {
  try {
    return JSON.parse(new TextDecoder().decode(base64urlDecode(text))) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** The Access identity (email), or null for every kind of failure alike. */
export async function verifyAccessJwt(env: Env, request: Request): Promise<string | null> {
  if (!accessConfigured(env)) return null;
  const cookie = /(?:^|;\s*)CF_Authorization=([^;]+)/.exec(request.headers.get('Cookie') ?? '');
  const raw = request.headers.get('Cf-Access-Jwt-Assertion') ?? cookie?.[1];
  if (!raw) return null;
  const parts = raw.split('.');
  if (parts.length !== 3) return null;
  const [head, body, sig] = parts as [string, string, string];

  const header = segment(head);
  // RS256 only, before anything else: `alg: none` and HMAC confusion die here.
  if (!header || header.alg !== 'RS256' || typeof header.kid !== 'string') return null;
  const key = await keyFor(env, header.kid);
  if (!key) return null;
  let signature: Uint8Array;
  try {
    signature = base64urlDecode(sig);
  } catch {
    return null;
  }
  const ok = await crypto.subtle
    .verify('RSASSA-PKCS1-v1_5', key, signature, new TextEncoder().encode(`${head}.${body}`))
    .catch(() => false);
  if (!ok) return null;

  const payload = segment(body);
  if (!payload) return null;
  const now = Math.floor(Date.now() / 1000);
  if (typeof payload.exp !== 'number' || payload.exp <= now) return null;
  if (typeof payload.nbf === 'number' && payload.nbf > now + 60) return null;
  if (payload.iss !== issuerFor(env)) return null;
  const audience = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!audience.includes(env.ACCESS_AUD)) return null;
  return typeof payload.email === 'string' ? payload.email : null;
}
