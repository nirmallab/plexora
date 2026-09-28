/**
 * Trials: 30 days of Paid, one per mailbox, at most TRIAL_MAX_PER_FP per
 * machine, with the seat key sent by email (which is what proves the mailbox).
 *
 * The one-per-mailbox rule is the UNIQUE index on trials.email_canonical, so
 * two racing requests cannot both win. A trial is an ordinary paid licence with
 * is_trial = 1 and no grace; when it ends the next refresh says so and Plexora
 * is Free again, with everything made during the trial intact.
 *
 * Refusing a NEW trial is the strongest automatic action anywhere in this
 * service. Nothing bans, nothing revokes on suspicion.
 */
import { canonicalEmail, findUser, validEmail } from './accounts';
import { newId, pepper } from './crypto';
import { one } from './db';
import { baseUrl, DAY, type Env, knob } from './env';
import { record } from './events';
import { ApiError } from './http';
import { provisionLicense } from './billing';
import { enforce } from './ratelimit';
import * as mail from './email';

/** A small, deliberately conservative list; extend with TRIAL_BLOCKED_DOMAINS. */
const DISPOSABLE = new Set([
  'mailinator.com', 'guerrillamail.com', '10minutemail.com', 'tempmail.com', 'temp-mail.org',
  'yopmail.com', 'trashmail.com', 'sharklasers.com', 'getnada.com', 'dispostable.com',
  'maildrop.cc', 'throwawaymail.com', 'fakeinbox.com', 'mintemail.com', 'mohmal.com',
]);

function blocked(env: Env, domain: string): boolean {
  if (DISPOSABLE.has(domain)) return true;
  const extra = String(env.TRIAL_BLOCKED_DOMAINS ?? '').split(',').map((d) => d.trim().toLowerCase()).filter(Boolean);
  return extra.includes(domain);
}

const FINGERPRINT = /^[0-9a-f]{64}$/;

export async function startTrial(env: Env, body: Record<string, unknown>, ipHash: string | null,
  now: number): Promise<{ status: 'sent'; message: string }> {
  const email = validEmail(body.email);
  const fingerprint = typeof body.fingerprint === 'string' ? body.fingerprint.trim().toLowerCase() : '';
  if (!email) throw new ApiError(400, 'invalid_request', 'Give a valid email address.');
  if (!FINGERPRINT.test(fingerprint)) {
    throw new ApiError(400, 'invalid_request',
      'Start the trial from Plexora (Settings > License, or `plexora license trial`) so it can include this machine\'s fingerprint.');
  }
  await enforce(env, 'trial-ip', ipHash, knob(env, 'TRIAL_PER_IP_PER_DAY'), DAY, now);
  const canonical = canonicalEmail(email);
  const domain = canonical.split('@')[1] ?? '';
  if (blocked(env, domain)) {
    throw new ApiError(403, 'trial_not_available', 'Trials need a permanent email address.');
  }
  const fpHash = await pepper(env, fingerprint);
  const priorOnMachine = await one<{ n: number }>(env,
    'SELECT COUNT(*) AS n FROM trials WHERE fingerprint_hash = ?1', fpHash);
  if ((priorOnMachine?.n ?? 0) >= knob(env, 'TRIAL_MAX_PER_FP')) {
    await record(env, now, { actor: 'trial', kind: 'trial.refused_machine', ip_hash: ipHash,
      payload: { fingerprint_hash: fpHash.slice(0, 16) } });
    throw new ApiError(409, 'trial_machine_limit', 'This machine has already had the trials it is allowed.');
  }
  const user = await findUser(env, email);
  if (user) {
    const paid = await one<{ n: number }>(env,
      `SELECT COUNT(*) AS n FROM account_members m JOIN licenses l ON l.account_id = m.account_id
       WHERE m.user_id = ?1 AND m.status = 'active' AND l.is_trial = 0 AND l.status = 'active' AND l.expires_at > ?2`,
      user.id, now);
    if ((paid?.n ?? 0) > 0) {
      throw new ApiError(409, 'trial_not_available', 'This address already has a Paid licence.');
    }
  }

  // The gate: one row per mailbox, atomically.
  const trialId = newId('tri');
  try {
    await env.LICENSE_DB.prepare(
      `INSERT INTO trials (id, email_canonical, fingerprint_hash, ip_hash, created_at) VALUES (?1, ?2, ?3, ?4, ?5)`,
    ).bind(trialId, canonical, fpHash, ipHash, now).run();
  } catch (error) {
    if (/UNIQUE constraint failed/i.test(String((error as Error).message))) {
      throw new ApiError(409, 'trial_already_issued', 'A trial has already been issued to that email address.');
    }
    throw error;
  }

  let provisioned;
  try {
    provisioned = await provisionLicense(env, {
      owner_email: email, use_class: 'academic', seats: 1, days: knob(env, 'TRIAL_DAYS'), trial: true,
      account: { kind: 'individual', name: email },
      purchase: { provider: 'trial', kind: 'trial', amount_cents: 0 },
    }, 'trial', now);
  } catch (error) {
    // Give the mailbox its chance back: the trial did not happen.
    await env.LICENSE_DB.prepare('DELETE FROM trials WHERE id = ?1').bind(trialId).run();
    throw error;
  }
  await env.LICENSE_DB.prepare('UPDATE trials SET license_id = ?2 WHERE id = ?1')
    .bind(trialId, provisioned.license.id).run();
  if (provisioned.seat_key) {
    await mail.send(env, mail.seatKeyMail(email, provisioned.seat_key, {
      trial: true, expires_at: provisioned.license.expires_at, portal: `${baseUrl(env)}/portal`,
    }));
  }
  return { status: 'sent', message: `A trial key is on its way to ${email}. Activate it in Plexora.` };
}
