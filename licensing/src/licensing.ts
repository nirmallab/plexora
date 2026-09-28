/**
 * Licences and the certificates they produce.
 *
 * A certificate's life is bounded three ways: CERT_MAX_DAYS from issue, the
 * licence's own expiry, and -- for an offline certificate -- its offline window.
 * That bound is also the bound on revocation for an environment that never
 * comes back online; it is why certificates are short and renewed quietly.
 */
import { activeKid, type CertificatePayload, type EnvironmentType, signCertificate, signingConfigured } from './certs';
import { newId } from './crypto';
import type { EnvironmentRow, LicenseRow, SeatRow } from './db';
import { seatEntitlements } from './db';
import { DAY, type Env, HOUR, knob } from './env';
import { ApiError } from './http';

/** The refusal for a licence that cannot issue anything, or null. */
export function licenseProblem(license: LicenseRow | null, now: number): ApiError | null {
  if (!license) return new ApiError(404, 'not_found', 'Licence not found.');
  if (license.status === 'revoked') return new ApiError(403, 'license_revoked', 'This licence has been revoked.');
  if (license.status === 'suspended') return new ApiError(403, 'license_suspended', 'This licence is suspended.');
  if (license.status === 'expired' || license.expires_at <= now) {
    return new ApiError(403, 'license_expired', 'This licence has expired.');
  }
  return null;
}

export function seatProblem(seat: SeatRow | null): ApiError | null {
  if (!seat) return new ApiError(401, 'invalid_credential', 'That seat key is not recognised.');
  if (seat.status !== 'active') return new ApiError(403, 'credential_revoked', 'That seat is no longer active.');
  return null;
}

function requireSigning(env: Env): void {
  if (!signingConfigured(env)) {
    throw new ApiError(503, 'signing_unavailable', 'The licence service cannot issue certificates right now.',
      { retry_after: 3600 });
  }
}

interface IssueOptions {
  license: LicenseRow;
  seat: SeatRow;
  now: number;
}

function basePayload(env: Env, { license, seat, now }: IssueOptions, type: EnvironmentType,
  expires: number): CertificatePayload {
  return {
    v: 1,
    aud: 'plexora',
    kid: activeKid(env),
    cert_id: newId('crt'),
    license_id: license.id,
    account_id: license.account_id,
    seat_id: seat.id,
    plan: 'paid',
    trial: license.is_trial === 1,
    use_class: license.use_class,
    entitlements: seatEntitlements(license, seat),
    environment_id: null,
    environment_type: type,
    env_binding: null,
    delegation_pubkey: null,
    issued_at: now,
    expires_at: Math.min(expires, license.expires_at),
    license_expires_at: license.expires_at,
    grace_days: license.is_trial === 1 ? 0 : license.grace_days,
    offline_until: null,
  };
}

/** An environment's certificate. `binding` is the client's (unpeppered) binding. */
export async function environmentCertificate(env: Env, options: IssueOptions & {
  environment: EnvironmentRow;
  binding: string;
  offlineUntil?: number | null;
}): Promise<{ certificate: string; payload: CertificatePayload }> {
  requireSigning(env);
  const { environment, binding, now } = options;
  const offline = options.offlineUntil ?? null;
  const expires = offline ?? now + knob(env, 'CERT_MAX_DAYS') * DAY;
  const payload = basePayload(env, options, environment.kind, expires);
  payload.environment_id = environment.id;
  payload.env_binding = binding;
  payload.delegation_pubkey = environment.kind === 'desktop' ? null : environment.delegation_pubkey;
  if (offline !== null) payload.offline_until = payload.expires_at;
  return { certificate: await signCertificate(env, payload), payload };
}

/**
 * A certificate for a job: unbound, no grace, short. Issued for CI tokens
 * (CI_CERT_HOURS, no registration) and by /v1/delegate for a registered
 * cluster that cannot mint one itself.
 */
export async function jobCertificate(env: Env, options: IssueOptions & {
  hours: number;
  environmentId?: string | null;
  notAfter?: number;
  entitlements?: string[];
}): Promise<{ certificate: string; payload: CertificatePayload }> {
  requireSigning(env);
  const maxHours = knob(env, 'JOB_CERT_MAX_DAYS') * 24;
  const hours = Math.max(1, Math.min(options.hours, maxHours));
  const payload = basePayload(env, options, 'job', options.now + hours * HOUR);
  if (options.notAfter !== undefined) payload.expires_at = Math.min(payload.expires_at, options.notAfter);
  payload.environment_id = options.environmentId ?? null;
  payload.grace_days = 0;
  if (options.entitlements) {
    const granted = new Set(payload.entitlements);
    payload.entitlements = options.entitlements.filter((e) =>
      [...granted].some((g) => e === g || e.startsWith(`${g}:`)));
  }
  return { certificate: await signCertificate(env, payload), payload };
}

/**
 * Whether a presented certificate should be replaced with a fresh one: close
 * to its end with more licence left, or no longer saying what the licence says.
 */
export function needsRenewal(env: Env, presented: CertificatePayload, license: LicenseRow, seat: SeatRow,
  environment: EnvironmentRow, now: number): boolean {
  const desired = Math.min(now + knob(env, 'CERT_MAX_DAYS') * DAY, license.expires_at);
  if (presented.expires_at - now < knob(env, 'CERT_RENEW_WINDOW_DAYS') * DAY && desired > presented.expires_at) {
    return true;
  }
  const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
  if (!same([...(presented.entitlements ?? [])].sort(), seatEntitlements(license, seat))) return true;
  if (presented.trial !== (license.is_trial === 1)) return true;
  if (presented.use_class !== license.use_class) return true;
  if (presented.expires_at > license.expires_at) return true; // the licence was shortened
  // Extended, or a certificate from before the claim existed: the date the
  // user sees must be the licence's.
  if (presented.license_expires_at !== license.expires_at) return true;
  const wantedDelegation = environment.kind === 'desktop' ? null : environment.delegation_pubkey;
  if ((presented.delegation_pubkey ?? null) !== wantedDelegation) return true;
  return false;
}
