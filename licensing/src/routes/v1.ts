/**
 * /v1: what the Plexora client calls. Frozen once shipped -- a released
 * Plexora must keep working against every later deployment.
 *
 *   GET  /v1/health          liveness and whether signing is configured
 *   GET  /v1/keys            the public keys (informational; clients ship theirs)
 *   POST /v1/activate        credential + environment -> certificate
 *   POST /v1/refresh         certificate -> ok | renewed | revoked
 *   POST /v1/deactivate      certificate -> environment released
 *   POST /v1/delegate        cluster certificate -> short job certificate
 *   POST /v1/trial/start     email + trial fingerprint -> key by email
 *
 * No CORS: the caller is the Plexora process, never a web page. Nothing here
 * reads or writes anything about a person's data.
 */
import { Hono } from 'hono';

import { activeKid, checkCertificate, type CertificatePayload, signingConfigured } from '../certs';
import { ipHash, LICENSE_TOKEN_PATTERN, normalizeSeatKey, pepper, SEAT_KEY_PATTERN } from '../crypto';
import type { EnvironmentRow, LicenseRow, SeatRow, TokenRow } from '../db';
import { environmentById, licenseById, seatById } from '../db';
import { DAY, type Env, HOUR, knob, nowSeconds } from '../env';
import { cleanDelegation, cleanName, cleanShort, KINDS, type Kind, noteRefresh, register, release } from '../environments';
import { record } from '../events';
import { ApiError, type AppEnv, int, ok, readJson, str } from '../http';
import { environmentCertificate, jobCertificate, licenseProblem, needsRenewal, seatProblem } from '../licensing';
import { enforce } from '../ratelimit';
import * as seats from '../seats';
import * as tokens from '../tokens';
import { startTrial } from '../trial';

export const v1 = new Hono<AppEnv>();

v1.use('*', async (c, next) => {
  c.set('ipHash', await ipHash(c.env, c.req.raw));
  await next();
});

v1.get('/health', (c) => ok(c, { ok: true, server_time: nowSeconds(), signing: signingConfigured(c.env) }));

v1.get('/keys', (c) => {
  let keys: Record<string, string> = {};
  try {
    keys = JSON.parse(c.env.PUBLIC_KEYS_JSON ?? '{}') as Record<string, string>;
  } catch {
    keys = {};
  }
  return ok(c, { active_kid: activeKid(c.env), keys });
});

const BINDING = /^[0-9a-f]{64}$/;

interface Credential {
  seat: SeatRow;
  license: LicenseRow;
  token: TokenRow | null;
}

async function resolveCredential(env: Env, raw: string, now: number): Promise<Credential> {
  if (LICENSE_TOKEN_PATTERN.test(raw)) {
    const token = await tokens.resolve(env, raw, now);
    const seat = await seatById(env, token.seat_id);
    const problem = seatProblem(seat);
    if (problem) throw problem;
    const license = await licenseById(env, token.license_id);
    const licenceProblem = licenseProblem(license, now);
    if (licenceProblem) throw licenceProblem;
    return { seat: seat!, license: license!, token };
  }
  const key = normalizeSeatKey(raw);
  if (!SEAT_KEY_PATTERN.test(key)) {
    throw new ApiError(401, 'invalid_credential', 'That is not a seat key or licence token.');
  }
  const seat = await seats.bySeatKey(env, key);
  const problem = seatProblem(seat);
  if (problem) throw problem;
  const license = await licenseById(env, seat!.license_id);
  const licenceProblem = licenseProblem(license, now);
  if (licenceProblem) throw licenceProblem;
  return { seat: seat!, license: license!, token: null };
}

v1.post('/activate', async (c) => {
  const now = nowSeconds();
  const ip = c.get('ipHash');
  await enforce(c.env, 'activate-ip', ip, knob(c.env, 'ACTIVATE_PER_IP_PER_HOUR'), HOUR, now);
  const body = await readJson(c);
  const raw = str(body, 'credential', 128);
  if (!raw) throw new ApiError(400, 'invalid_request', 'Give a seat key or licence token as `credential`.');
  const described = (body.environment && typeof body.environment === 'object' ? body.environment : {}) as
    Record<string, unknown>;

  const { seat, license, token } = await resolveCredential(c.env, raw, now);

  if (token?.scope === 'ci') {
    // CI never registers anything: a short, unbound job certificate, and the
    // use recorded on the token.
    const issued = await jobCertificate(c.env, { license, seat, now, hours: knob(c.env, 'CI_CERT_HOURS'),
      notAfter: token.expires_at });
    await tokens.noteUse(c.env, token, now);
    return ok(c, { certificate: issued.certificate, server_time: now,
      environment: { id: null, name: 'CI', type: 'job' }, license: licenseSummary(license) });
  }

  const kind = (KINDS as readonly string[]).includes(String(described.kind)) ? described.kind as Kind : null;
  const binding = typeof described.binding === 'string' ? described.binding.toLowerCase() : '';
  if (!kind || !BINDING.test(binding)) {
    throw new ApiError(400, 'invalid_request', 'Describe the environment: kind and binding.');
  }
  if (token && !tokens.SCOPE_KINDS[token.scope].includes(kind)) {
    throw new ApiError(403, 'scope_not_allowed',
      `A ${token.scope} token cannot register a ${kind} environment.`);
  }
  const { environment } = await register(c.env, license, seat, {
    kind,
    display_name: cleanName(described.display_name, kind === 'cluster' ? 'HPC cluster' : 'Computer'),
    binding_hash: await pepper(c.env, binding),
    delegation_pubkey: cleanDelegation(kind, described.delegation_pubkey),
    registration_kind: 'online',
    platform: cleanShort(described.platform),
    scheduler_hint: cleanShort(described.scheduler_hint, 16),
    app_version: cleanShort(described.app_version, 24),
  }, now, { actor: token ? `token:${token.id}` : 'client', ip_hash: ip });
  if (token) await tokens.noteUse(c.env, token, now);

  const issued = await environmentCertificate(c.env, { license, seat, environment, binding, now });
  return ok(c, {
    certificate: issued.certificate,
    server_time: now,
    environment: { id: environment.id, name: environment.display_name, type: environment.kind },
    license: licenseSummary(license),
  });
});

function licenseSummary(license: LicenseRow) {
  return { id: license.id, plan: license.plan, trial: license.is_trial === 1, use_class: license.use_class,
    expires_at: license.expires_at };
}

/** The presented certificate and its environment, checked against the caller's binding. */
export async function presented(c: { env: Env }, body: Record<string, unknown>, ip: string | null,
  now: number): Promise<{ payload: CertificatePayload; environment: EnvironmentRow | null; bindingOk: boolean;
    binding: string }> {
  const check = await checkCertificate(c.env, body.certificate);
  if (check.status === 'unverifiable') {
    throw new ApiError(503, 'signing_unavailable', 'This service cannot check that certificate right now.',
      { retry_after: 3600 });
  }
  if (check.status === 'forged') {
    await enforce(c.env, 'forged-ip', ip, knob(c.env, 'FORGED_PER_IP_PER_HOUR'), HOUR, now);
    await record(c.env, now, { actor: 'client', kind: 'certificate.forged', ip_hash: ip,
      payload: { reason: check.reason } });
    throw new ApiError(403, 'forged_certificate', 'This service did not issue that certificate.');
  }
  const payload = check.payload;
  if (payload.environment_type === 'job' || !payload.environment_id) {
    throw new ApiError(400, 'invalid_request', 'A job certificate is not refreshed; ask for a new one.');
  }
  const binding = typeof body.binding === 'string' ? body.binding.toLowerCase() : '';
  const environment = await environmentById(c.env, payload.environment_id);
  const bindingOk = environment !== null && BINDING.test(binding) &&
    (await pepper(c.env, binding)) === environment.env_binding_hash;
  return { payload, environment, bindingOk, binding };
}

v1.post('/refresh', async (c) => {
  const now = nowSeconds();
  const ip = c.get('ipHash');
  const body = await readJson(c);
  const { payload, environment, bindingOk, binding } = await presented(c, body, ip, now);
  if (!environment) return ok(c, { status: 'revoked', reason: 'environment_unknown', server_time: now });
  if (!bindingOk) {
    throw new ApiError(403, 'environment_mismatch', 'That certificate belongs to a different environment.');
  }
  const [license, seat] = await Promise.all([licenseById(c.env, environment.license_id),
    seatById(c.env, environment.seat_id)]);
  if (!license || license.status === 'revoked') {
    return ok(c, { status: 'revoked', reason: 'license_revoked', server_time: now });
  }
  if (license.status === 'suspended') {
    return ok(c, { status: 'revoked', reason: 'license_suspended', server_time: now });
  }
  if (!seat || seat.status !== 'active' || environment.status !== 'active') {
    return ok(c, { status: 'revoked', reason: 'environment_released', server_time: now });
  }
  if (Array.isArray(body.flags) && body.flags.includes('clock_rollback')) {
    await record(c.env, now, { actor: 'client', kind: 'client.clock_rollback', license_id: license.id,
      seat_id: seat.id, environment_id: environment.id, ip_hash: ip });
  }
  await noteRefresh(c.env, environment, now, ip);
  if (license.status === 'active' && license.expires_at > now &&
      needsRenewal(c.env, payload, license, seat, environment, now)) {
    const issued = await environmentCertificate(c.env, { license, seat, environment, binding, now });
    return ok(c, { status: 'renewed', certificate: issued.certificate, server_time: now });
  }
  return ok(c, { status: 'ok', server_time: now });
});

v1.post('/deactivate', async (c) => {
  const now = nowSeconds();
  const ip = c.get('ipHash');
  const body = await readJson(c);
  const { environment, bindingOk } = await presented(c, body, ip, now);
  if (!environment) throw new ApiError(404, 'environment_unknown', 'This service does not know that environment.');
  if (!bindingOk) {
    throw new ApiError(403, 'environment_mismatch', 'That certificate belongs to a different environment.');
  }
  if (environment.status !== 'active') return ok(c, { status: 'released', server_time: now });
  const seat = await seatById(c.env, environment.seat_id);
  if (!seat) throw new ApiError(404, 'environment_unknown', 'This service does not know that environment.');
  const result = await release(c.env, environment, seat, { now, reason: 'user', actor: 'client', ip_hash: ip });
  return ok(c, { status: 'released', next_allowed_at: result.next_allowed_at, server_time: now });
});

v1.post('/delegate', async (c) => {
  const now = nowSeconds();
  const ip = c.get('ipHash');
  const body = await readJson(c);
  const { payload, environment, bindingOk } = await presented(c, body, ip, now);
  if (!environment || environment.status !== 'active') {
    throw new ApiError(403, 'environment_unknown', 'That environment is not registered.');
  }
  if (!bindingOk) {
    throw new ApiError(403, 'environment_mismatch', 'That certificate belongs to a different environment.');
  }
  if (environment.kind === 'desktop') {
    throw new ApiError(403, 'not_delegating', 'Only a registered cluster or container host can lend its licence to jobs.');
  }
  await enforce(c.env, 'delegate-env', environment.id, knob(c.env, 'DELEGATE_PER_ENV_PER_DAY'), DAY, now);
  const [license, seat] = await Promise.all([licenseById(c.env, environment.license_id),
    seatById(c.env, environment.seat_id)]);
  const problem = licenseProblem(license, now) ?? seatProblem(seat);
  if (problem) throw problem;
  const hours = int(body, 'ttl_hours') ?? knob(c.env, 'JOB_CERT_DEFAULT_HOURS');
  const issued = await jobCertificate(c.env, { license: license!, seat: seat!, now, hours,
    environmentId: environment.id, notAfter: payload.expires_at });
  return ok(c, { certificate: issued.certificate, expires_at: issued.payload.expires_at, server_time: now });
});

v1.post('/trial/start', async (c) => {
  const now = nowSeconds();
  const body = await readJson(c);
  return ok(c, await startTrial(c.env, body, c.get('ipHash'), now), 202);
});
