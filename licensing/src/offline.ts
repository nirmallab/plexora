/**
 * Offline licences: a certificate for an environment that never talks to
 * this service -- an air-gapped workstation, a cluster with no outbound route.
 *
 * The person runs `plexora license fingerprint --out fp.json` there, uploads
 * the report in the portal (or an admin does), and downloads a `.plexora` file
 * to install with `plexora license install`. The report carries the
 * environment's binding (a hash of its random secret), its kind and a name, and
 * nothing else.
 *
 * An offline certificate CANNOT BE RECALLED before `offline_until`. So each
 * one is recorded in offline_grants, bounded by the licence's
 * offline_max_days (itself capped by OFFLINE_MAX_DAYS), never outlives the
 * licence, and registers the environment against the seat's cap like any other.
 */
import { pepper } from './crypto';
import type { LicenseRow, SeatRow } from './db';
import { DAY, type Env, knob } from './env';
import { cleanDelegation, cleanName, cleanShort, KINDS, type Kind, register } from './environments';
import { eventStatement } from './events';
import { ApiError } from './http';
import { environmentCertificate, licenseProblem, seatProblem } from './licensing';
import { newId } from './crypto';

const BINDING = /^[0-9a-f]{64}$/;

export interface OfflineResult {
  file: string;
  filename: string;
  offline_until: number;
  environment_id: string;
  grant_id: string;
}

export async function issueOffline(env: Env, license: LicenseRow, seat: SeatRow,
  report: Record<string, unknown>, days: number | null, actor: string, now: number): Promise<OfflineResult> {
  const problem = licenseProblem(license, now) ?? seatProblem(seat);
  if (problem) throw problem;
  if (license.offline_allowed !== 1) {
    throw new ApiError(403, 'offline_not_allowed', 'This licence does not include offline certificates.');
  }
  if (report.product !== 'plexora' || typeof report.binding !== 'string' || !BINDING.test(report.binding)) {
    throw new ApiError(400, 'invalid_request',
      'That is not a Plexora fingerprint report. Create one with `plexora license fingerprint --out fp.json`.');
  }
  const kind: Kind = (KINDS as readonly string[]).includes(String(report.kind)) ? report.kind as Kind : 'desktop';
  const cap = Math.min(license.offline_max_days, knob(env, 'OFFLINE_MAX_DAYS'));
  const wanted = Math.max(1, Math.min(days ?? knob(env, 'OFFLINE_DEFAULT_DAYS'), cap));
  const offlineUntil = Math.min(now + wanted * DAY, license.expires_at);

  const { environment } = await register(env, license, seat, {
    kind,
    display_name: cleanName(report.display_name, 'Offline computer'),
    binding_hash: await pepper(env, report.binding),
    delegation_pubkey: cleanDelegation(kind, report.delegation_pubkey),
    registration_kind: 'offline',
    platform: cleanShort(report.platform),
    scheduler_hint: cleanShort(report.scheduler_hint, 16),
    app_version: cleanShort(report.plexora_version, 24),
  }, now, { actor, ip_hash: null });

  const { certificate, payload } = await environmentCertificate(env, {
    license, seat, environment, binding: report.binding, now, offlineUntil,
  });
  const grantId = newId('off');
  await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare(
      `INSERT INTO offline_grants (id, license_id, seat_id, environment_id, cert_id, issued_at, offline_until,
         created_by) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)`,
    ).bind(grantId, license.id, seat.id, environment.id, payload.cert_id, now, payload.expires_at, actor),
    eventStatement(env, now, { actor, kind: 'offline.issued', account_id: license.account_id,
      license_id: license.id, seat_id: seat.id, environment_id: environment.id,
      payload: { grant_id: grantId, offline_until: payload.expires_at } }),
  ]);
  const date = (seconds: number) => new Date(seconds * 1000).toISOString().slice(0, 10);
  const until = date(payload.expires_at);
  const file = [
    '# Plexora offline licence',
    `# Environment: ${environment.display_name} (${environment.kind})`,
    `# Licence: ${license.id} (${license.use_class}), valid until ${date(license.expires_at)}`,
    `# This file works until ${until}. It cannot be recalled before then, so keep it private.`,
    '# Install: plexora license install <this file>',
    certificate,
    '',
  ].join('\n');
  return { file, filename: `plexora-${environment.id}.plexora`, offline_until: payload.expires_at,
    environment_id: environment.id, grant_id: grantId };
}
