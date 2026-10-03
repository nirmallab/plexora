/**
 * PLXAI1 gateway tokens: what a Plexora client presents on every model call.
 *
 *   PLXAI1.<kid>.<base64url(canonical JSON)>.<base64url(Ed25519 signature)>
 *
 * Issued by POST /v1/ai/token against the environment certificate (the same
 * `presented()` check as /v1/refresh), signed with the licence keys, short
 * (AI_TOKEN_TTL_S, 30 min) and verified offline on each call: no D1 read to
 * authenticate. Revocation is the licence's own state, re-checked at issue,
 * so a revoked seat loses AI within one TTL.
 */
import { activeKid, checkPrefixed, signPrefixed } from '../certs';
import { newId } from '../crypto';
import type { Env } from '../env';
import { knob } from '../env';
import { ApiError } from '../http';
import type { Capability } from './catalog';

export const PREFIX = 'PLXAI1';
export const AUDIENCE = 'plexora-ai';

export type Billing = 'credits' | 'dev';

export interface GatewayClaims {
  v: 1;
  aud: typeof AUDIENCE;
  kid: string;
  jti: string;
  iat: number;
  nbf: number;
  exp: number;
  acc: string;
  usr: string | null;
  lic: string;
  seat: string;
  env: string | null;
  envt: string;
  caps: Capability[];
  /** The modules (tasks.ts) the seat's entitlements unlock, `*` for all; absent from older tokens. */
  mods?: string[];
  /** 'dev' only for accounts an admin put in dev mode: unlocks /v1/ai/dev/*. */
  mode: Billing;
  ver: string;
}

export async function issueToken(env: Env, claims: Omit<GatewayClaims, 'v' | 'aud' | 'kid' | 'jti' | 'iat' | 'nbf' |
  'exp'>, now: number): Promise<{ token: string; claims: GatewayClaims }> {
  const full: GatewayClaims = {
    v: 1, aud: AUDIENCE, kid: activeKid(env), jti: newId('ait'), iat: now, nbf: now - 60,
    exp: now + knob(env, 'AI_TOKEN_TTL_S'), ...claims,
  };
  return { token: await signPrefixed(env, PREFIX, full), claims: full };
}

/** The bearer token of a request, verified, or an ApiError. */
export async function verifyBearer(env: Env, header: string | undefined, now: number): Promise<GatewayClaims> {
  const match = /^Bearer\s+(\S+)$/i.exec(header ?? '');
  if (!match) throw new ApiError(401, 'invalid_token', 'Send a Plexora AI token as `Authorization: Bearer`.');
  const check = await checkPrefixed(env, PREFIX, match[1]);
  if (check.status === 'unverifiable') {
    throw new ApiError(503, 'signing_unavailable', 'This service cannot check that token right now.',
      { retry_after: 60 });
  }
  if (check.status === 'forged') throw new ApiError(401, 'invalid_token', 'This service did not issue that token.');
  const claims = check.payload as unknown as GatewayClaims;
  if (claims.v !== 1 || claims.aud !== AUDIENCE || typeof claims.acc !== 'string' || !Array.isArray(claims.caps)) {
    throw new ApiError(401, 'invalid_token', 'That is not a Plexora AI token.');
  }
  if (!(claims.nbf <= now) || claims.exp - claims.iat > knob(env, 'AI_TOKEN_TTL_S') + 60) {
    throw new ApiError(401, 'invalid_token', 'That token is not valid yet.');
  }
  if (claims.exp <= now) throw new ApiError(401, 'token_expired', 'That token has expired; ask for a new one.');
  return claims;
}
