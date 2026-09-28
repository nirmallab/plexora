/**
 * Misuse signals: a nightly review queue for a PERSON to look at.
 *
 * Never read on a request path, never the cause of an automatic block, ban or
 * revocation. A signal is a question ("is this seat being shared across a
 * lab?"), and the answer is often innocent -- a reinstall, a new laptop, a
 * cluster behind a NAT pool. Usage volume is deliberately NOT a signal: using
 * Plexora a lot is the point.
 *
 *   env_overuse      a seat that registered more than allowance x FACTOR
 *                    environments in 30 days
 *   release_churn    a seat that released SIGNAL_CHURN_RELEASES+ in 30 days
 *   forged_certs     an IP hash presenting SIGNAL_FORGED_PER_IP+ forged
 *                    certificates in 7 days
 *   revoked_token    a revoked token still being presented (3+ in 7 days)
 *   clock_rollback   an environment reporting rollback 3+ times in 30 days
 *   trial_fp_reuse   a machine refused a trial for its fingerprint
 *   shared_env       SIGNAL_IPS_PER_ENV+ distinct IP hashes refreshing one
 *                    environment in 7 days -- weak by construction, because
 *                    a refresh writes at most once a week; that is the price
 *                    of refreshes costing nothing, and it is accepted
 */
import { all } from './db';
import { DAY, type Env, knob } from './env';

interface Candidate {
  kind: string;
  subject: string;
  detail: Record<string, unknown>;
}

export async function runSignals(env: Env, now: number): Promise<number> {
  const month = now - 30 * DAY;
  const week = now - 7 * DAY;
  const candidates: Candidate[] = [];

  const overuse = await all<{ seat_id: string; n: number; allowance: number }>(env,
    `SELECT e.seat_id, COUNT(*) AS n, COALESCE(s.envs_per_seat, l.envs_per_seat) AS allowance
     FROM events ev JOIN environments e ON e.id = ev.environment_id
     JOIN seat_assignments s ON s.id = e.seat_id JOIN licenses l ON l.id = e.license_id
     WHERE ev.kind = 'environment.registered' AND ev.at > ?1
     GROUP BY e.seat_id HAVING COUNT(*) > COALESCE(s.envs_per_seat, l.envs_per_seat) * ?2`,
    month, knob(env, 'SIGNAL_ENV_OVERUSE_FACTOR'));
  for (const row of overuse) {
    candidates.push({ kind: 'env_overuse', subject: row.seat_id, detail: { registrations_30d: row.n,
      allowance: row.allowance } });
  }

  const churn = await all<{ seat_id: string; n: number }>(env,
    `SELECT seat_id, COUNT(*) AS n FROM events WHERE kind = 'environment.released' AND at > ?1
       AND actor != 'cron' AND seat_id IS NOT NULL
     GROUP BY seat_id HAVING COUNT(*) >= ?2`, month, knob(env, 'SIGNAL_CHURN_RELEASES'));
  for (const row of churn) candidates.push({ kind: 'release_churn', subject: row.seat_id, detail: { releases_30d: row.n } });

  const forged = await all<{ ip_hash: string; n: number }>(env,
    `SELECT ip_hash, COUNT(*) AS n FROM events WHERE kind = 'certificate.forged' AND at > ?1 AND ip_hash IS NOT NULL
     GROUP BY ip_hash HAVING COUNT(*) >= ?2`, week, knob(env, 'SIGNAL_FORGED_PER_IP'));
  for (const row of forged) candidates.push({ kind: 'forged_certs', subject: row.ip_hash, detail: { forged_7d: row.n } });

  const revokedUse = await all<{ token_id: string; n: number }>(env,
    `SELECT json_extract(payload, '$.token_id') AS token_id, COUNT(*) AS n FROM events
     WHERE kind = 'token.revoked_use' AND at > ?1 GROUP BY token_id HAVING COUNT(*) >= 3`, week);
  for (const row of revokedUse) {
    if (row.token_id) candidates.push({ kind: 'revoked_token', subject: row.token_id, detail: { uses_7d: row.n } });
  }

  const rollback = await all<{ environment_id: string; n: number }>(env,
    `SELECT environment_id, COUNT(*) AS n FROM events WHERE kind = 'client.clock_rollback' AND at > ?1
       AND environment_id IS NOT NULL GROUP BY environment_id HAVING COUNT(*) >= 3`, month);
  for (const row of rollback) {
    candidates.push({ kind: 'clock_rollback', subject: row.environment_id, detail: { reports_30d: row.n } });
  }

  const trialReuse = await all<{ fp: string; n: number }>(env,
    `SELECT json_extract(payload, '$.fingerprint_hash') AS fp, COUNT(*) AS n FROM events
     WHERE kind = 'trial.refused_machine' AND at > ?1 GROUP BY fp`, month);
  for (const row of trialReuse) {
    if (row.fp) candidates.push({ kind: 'trial_fp_reuse', subject: row.fp, detail: { refusals_30d: row.n } });
  }

  const shared = await all<{ environment_id: string; n: number }>(env,
    `SELECT environment_id, COUNT(DISTINCT ip_hash) AS n FROM events
     WHERE kind = 'environment.refreshed' AND at > ?1 AND ip_hash IS NOT NULL AND environment_id IS NOT NULL
     GROUP BY environment_id HAVING COUNT(DISTINCT ip_hash) >= ?2`, week, knob(env, 'SIGNAL_IPS_PER_ENV'));
  for (const row of shared) {
    candidates.push({ kind: 'shared_env', subject: row.environment_id, detail: { ip_hashes_7d: row.n } });
  }

  if (candidates.length === 0) return 0;
  const results = await env.LICENSE_DB.batch(candidates.map((c) => env.LICENSE_DB.prepare(
    `INSERT INTO signals (kind, subject, detail, created_at) VALUES (?1, ?2, ?3, ?4) ON CONFLICT DO NOTHING`,
  ).bind(c.kind, c.subject, JSON.stringify(c.detail), now)));
  return results.reduce((sum, r) => sum + (r.meta.changes ?? 0), 0);
}
