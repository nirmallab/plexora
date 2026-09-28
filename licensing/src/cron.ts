/**
 * The daily maintenance run (03:30 UTC), in bounded steps:
 *
 *   expire     licences past expiry + grace are marked expired
 *   reminders  30/7/1/0 days before expiry (trials: 7/1/0), each sent once
 *   idle       online environments unseen for IDLE_ENV_RELEASE_DAYS are released
 *   prune      sessions, sign-in links, invitations, rate-limit windows;
 *              audit payloads older than EVENT_PAYLOAD_RETENTION_MONTHS
 *   signals    the review queue (signals.ts)
 *   backup     a gzipped JSON snapshot of D1 to R2 (backup.ts)
 *
 * Every step is independent and catches its own failure, so a broken backup
 * never stops expiry and a mail outage never stops a backup.
 */
import { backupToR2 } from './backup';
import { all } from './db';
import * as mail from './email';
import { baseUrl, DAY, type Env, knob } from './env';
import { eventStatement } from './events';
import { runSignals } from './signals';

export const TASKS = ['expire', 'reminders', 'idle', 'prune', 'signals', 'backup'] as const;
export type Task = (typeof TASKS)[number];

async function expire(env: Env, now: number): Promise<number> {
  const result = await env.LICENSE_DB.prepare(
    `UPDATE licenses SET status = 'expired', updated_at = ?1
     WHERE status = 'active' AND expires_at + grace_days * 86400 < ?1`,
  ).bind(now).run();
  return result.meta.changes ?? 0;
}

const PAID_REMINDERS = [30, 7, 1, 0];
const TRIAL_REMINDERS = [7, 1, 0];

async function reminders(env: Env, now: number): Promise<number> {
  let sent = 0;
  const due = await all<{ id: string; is_trial: number; expires_at: number; email: string }>(env,
    `SELECT l.id, l.is_trial, l.expires_at, u.email
     FROM licenses l
     JOIN account_members m ON m.account_id = l.account_id AND m.role = 'owner' AND m.status = 'active'
     JOIN users u ON u.id = m.user_id
     WHERE l.status = 'active' AND l.renewal_state != 'auto' AND l.expires_at > ?1 - 86400
       AND l.expires_at <= ?1 + 31 * 86400`, now);
  for (const row of due) {
    const days = Math.floor((row.expires_at - now) / DAY);
    // The most urgent reminder that applies. A cron that missed a few days
    // sends the one that is due now, never a backlog of stale ones.
    const schedule = row.is_trial ? TRIAL_REMINDERS : PAID_REMINDERS;
    const threshold = [...schedule].sort((a, b) => a - b).find((d) => d >= days);
    if (threshold === undefined) continue;
    const kind = `expiry_${threshold}`;
    const claimed = await env.LICENSE_DB.prepare(
      'INSERT INTO notices (license_id, kind, sent_at) VALUES (?1, ?2, ?3) ON CONFLICT DO NOTHING',
    ).bind(row.id, kind, now).run();
    if ((claimed.meta.changes ?? 0) === 0) continue;
    await mail.send(env, mail.reminderMail(row.email, kind, row.expires_at, row.is_trial === 1,
      `${baseUrl(env)}/portal`));
    sent += 1;
  }
  return sent;
}

async function releaseIdle(env: Env, now: number): Promise<number> {
  const cutoff = now - knob(env, 'IDLE_ENV_RELEASE_DAYS') * DAY;
  const idle = await all<{ id: string; license_id: string; seat_id: string }>(env,
    `SELECT id, license_id, seat_id FROM environments
     WHERE status = 'active' AND registration_kind = 'online' AND COALESCE(last_refresh_at, created_at) < ?1
     LIMIT 500`, cutoff);
  if (idle.length === 0) return 0;
  const statements: D1PreparedStatement[] = [];
  for (const row of idle) {
    statements.push(env.LICENSE_DB.prepare(
      `UPDATE environments SET status = 'released', released_at = ?2, release_reason = 'idle'
       WHERE id = ?1 AND status = 'active'`).bind(row.id, now));
    statements.push(eventStatement(env, now, { actor: 'cron', kind: 'environment.released',
      license_id: row.license_id, seat_id: row.seat_id, environment_id: row.id, payload: { reason: 'idle' } }));
  }
  await env.LICENSE_DB.batch(statements);
  return idle.length;
}

async function prune(env: Env, now: number): Promise<Record<string, number>> {
  const months = knob(env, 'EVENT_PAYLOAD_RETENTION_MONTHS');
  const results = await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare('DELETE FROM sessions WHERE expires_at < ?1').bind(now),
    env.LICENSE_DB.prepare('DELETE FROM login_links WHERE expires_at < ?1').bind(now - DAY),
    env.LICENSE_DB.prepare('DELETE FROM rate_limits WHERE expires_at < ?1').bind(now),
    env.LICENSE_DB.prepare(
      'DELETE FROM invitations WHERE (accepted_at IS NOT NULL OR revoked_at IS NOT NULL OR expires_at < ?1) AND created_at < ?2',
    ).bind(now, now - 90 * DAY),
    env.LICENSE_DB.prepare(
      `UPDATE events SET payload = NULL WHERE payload IS NOT NULL AND id IN
         (SELECT id FROM events WHERE payload IS NOT NULL AND at < ?1 LIMIT 5000)`,
    ).bind(now - Math.round(months * 30.44 * DAY)),
  ]);
  const names = ['sessions', 'login_links', 'rate_limits', 'invitations', 'event_payloads'];
  return Object.fromEntries(names.map((name, i) => [name, results[i]?.meta.changes ?? 0]));
}

export async function runMaintenance(env: Env, now: number, only?: Task): Promise<Record<string, unknown>> {
  const report: Record<string, unknown> = { at: now };
  const steps: Record<Task, () => Promise<unknown>> = {
    expire: () => expire(env, now),
    reminders: () => reminders(env, now),
    idle: () => releaseIdle(env, now),
    prune: () => prune(env, now),
    signals: () => runSignals(env, now),
    backup: () => backupToR2(env, now),
  };
  for (const task of TASKS) {
    if (only && task !== only) continue;
    try {
      report[task] = await steps[task]();
    } catch (error) {
      console.error('maintenance step failed', task, String(error));
      report[task] = { error: String(error) };
    }
  }
  return report;
}

export async function scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
  ctx.waitUntil(runMaintenance(env, Math.floor(Date.now() / 1000)).then(() => undefined));
}
