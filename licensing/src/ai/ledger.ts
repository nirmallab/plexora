/**
 * The AI balance: reserve before a call, settle from the provider's usage
 * after it, grant credit, and run accounting.
 *
 * Race-freedom comes from D1, not from the Worker: a reservation is ONE
 * conditional UPDATE (`... WHERE available >= estimate`), so two calls racing
 * for the last credit cannot both win; a settlement is one batch (one
 * transaction) whose UPDATE reads the pre-update row, so concurrent
 * settlements never lose a write. The ledger legs are written from the same
 * pre-update row inside that batch, so Σ legs always equals the balance.
 *
 * Draw order: the monthly allowance first, then prepaid credit.
 */
import { newId } from '../crypto';
import { all, one } from '../db';
import type { Env } from '../env';
import { knob } from '../env';
import { ApiError } from '../http';

export interface BalanceRow {
  account_id: string;
  prepaid_micro: number;
  allowance_micro: number;
  allowance_period: string | null;
  held_micro: number;
  updated_at: number;
}

export interface AiAccountRow {
  account_id: string;
  mode: 'credits' | 'dev' | 'disabled';
  markup_bps: number | null;
  allowance_micro: number | null;
  notes: string | null;
  updated_at: number;
  updated_by: string | null;
}

export function periodOf(seconds: number): string {
  return new Date(seconds * 1000).toISOString().slice(0, 7);
}

export const aiAccount = (env: Env, accountId: string) =>
  one<AiAccountRow>(env, 'SELECT * FROM ai_accounts WHERE account_id = ?1', accountId);

export function markupFor(env: Env, account: AiAccountRow | null): number {
  return account?.markup_bps ?? knob(env, 'AI_MARKUP_BPS');
}

export async function balance(env: Env, accountId: string): Promise<BalanceRow> {
  return (await one<BalanceRow>(env, 'SELECT * FROM ai_balances WHERE account_id = ?1', accountId)) ??
    { account_id: accountId, prepaid_micro: 0, allowance_micro: 0, allowance_period: null, held_micro: 0,
      updated_at: 0 };
}

export function available(row: BalanceRow): number {
  return row.prepaid_micro + row.allowance_micro - row.held_micro;
}

export function balanceView(row: BalanceRow) {
  return {
    available_micro: available(row), prepaid_micro: row.prepaid_micro, allowance_micro: row.allowance_micro,
    allowance_period: row.allowance_period, held_micro: row.held_micro,
    available_credits: Math.floor(available(row) / 10_000),
  };
}

/** The monthly allowance an account gets: its own setting, else seats x knob. */
async function allowanceFor(env: Env, accountId: string, account: AiAccountRow | null): Promise<number> {
  if (account?.allowance_micro !== null && account?.allowance_micro !== undefined) return account.allowance_micro;
  const perSeat = knob(env, 'AI_ALLOWANCE_PER_SEAT_MICRO');
  if (perSeat <= 0) return 0;
  const row = await one<{ seats: number }>(env,
    `SELECT COALESCE(SUM(seats), 0) AS seats FROM licenses WHERE account_id = ?1 AND status = 'active'`, accountId);
  return perSeat * (row?.seats ?? 0);
}

/**
 * Housekeeping before any reservation: the balance row exists, holds past
 * their expiry are released, and a new month's allowance replaces the old.
 * Each step is idempotent and safe to race.
 */
export async function prepare(env: Env, accountId: string, account: AiAccountRow | null, now: number): Promise<void> {
  const period = periodOf(now);
  await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare(
      `INSERT OR IGNORE INTO ai_balances (account_id, prepaid_micro, allowance_micro, allowance_period, held_micro,
         updated_at) VALUES (?1, 0, 0, NULL, 0, ?2)`).bind(accountId, now),
    env.LICENSE_DB.prepare(
      `UPDATE ai_balances SET held_micro = MAX(held_micro - (SELECT COALESCE(SUM(micro), 0) FROM ai_holds
         WHERE account_id = ?1 AND expires_at < ?2), 0), updated_at = ?2 WHERE account_id = ?1
         AND EXISTS (SELECT 1 FROM ai_holds WHERE account_id = ?1 AND expires_at < ?2)`).bind(accountId, now),
    env.LICENSE_DB.prepare('DELETE FROM ai_holds WHERE account_id = ?1 AND expires_at < ?2').bind(accountId, now),
  ]);
  const current = await balance(env, accountId);
  if (current.allowance_period === period) return;
  const grant = await allowanceFor(env, accountId, account);
  const journal = `allowance:${accountId}:${period}`;
  // The legs and the swap are one batch, guarded on the old period, so two
  // racing first-calls-of-the-month post it once.
  await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare(
      `INSERT OR IGNORE INTO ai_ledger (journal_id, account_id, at, kind, bucket, amount_micro, ref_type, ref_id, actor)
       SELECT ?1 || ':expiry', account_id, ?2, 'allowance_expiry', 'allowance', -allowance_micro, 'period',
         allowance_period, 'gateway' FROM ai_balances
       WHERE account_id = ?3 AND allowance_micro > 0 AND COALESCE(allowance_period, '') != ?4`,
    ).bind(journal, now, accountId, period),
    env.LICENSE_DB.prepare(
      `INSERT OR IGNORE INTO ai_ledger (journal_id, account_id, at, kind, bucket, amount_micro, ref_type, ref_id, actor)
       SELECT ?1, account_id, ?2, 'allowance_grant', 'allowance', ?5, 'period', ?4, 'gateway' FROM ai_balances
       WHERE account_id = ?3 AND ?5 > 0 AND COALESCE(allowance_period, '') != ?4`,
    ).bind(journal, now, accountId, period, grant),
    env.LICENSE_DB.prepare(
      `UPDATE ai_balances SET allowance_micro = ?3, allowance_period = ?2, updated_at = ?4
       WHERE account_id = ?1 AND COALESCE(allowance_period, '') != ?2`,
    ).bind(accountId, period, grant, now),
  ]);
}

/** Reserve `micro` for one call or run, or throw 402 insufficient_credits. */
export async function reserve(env: Env, accountId: string, micro: number, now: number,
  link: { request_id?: string; run_id?: string; expires_at?: number }): Promise<string> {
  const result = await env.LICENSE_DB.prepare(
    `UPDATE ai_balances SET held_micro = held_micro + ?2, updated_at = ?3
     WHERE account_id = ?1 AND prepaid_micro + allowance_micro - held_micro >= ?2`,
  ).bind(accountId, micro, now).run();
  if ((result.meta.changes ?? 0) !== 1) {
    const row = await balance(env, accountId);
    throw new ApiError(402, 'insufficient_credits', 'Not enough Plexora AI credits for this call.',
      { details: { required_micro: micro, ...balanceView(row) } });
  }
  const holdId = newId('hold');
  await env.LICENSE_DB.prepare(
    `INSERT INTO ai_holds (id, account_id, request_id, run_id, micro, created_at, expires_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)`,
  ).bind(holdId, accountId, link.request_id ?? null, link.run_id ?? null, micro, now,
    link.expires_at ?? now + knob(env, 'AI_HOLD_TTL_S')).run();
  return holdId;
}

/**
 * Statements that take `charge` from the balance (allowance first) and drop
 * what is held by `release` -- a number, or for a run the charge bounded by
 * what the run still holds. The legs read the row before the UPDATE runs.
 */
function drawStatements(env: Env, accountId: string, charge: number, release: number | { run_id: string },
  now: number, journal: string, ref: { type: string; id: string; user_id: string | null }): D1PreparedStatement[] {
  const statements: D1PreparedStatement[] = [];
  if (charge > 0) {
    statements.push(
      env.LICENSE_DB.prepare(
        `INSERT OR IGNORE INTO ai_ledger (journal_id, account_id, at, kind, bucket, amount_micro, ref_type, ref_id,
           user_id, actor)
         SELECT ?1, account_id, ?2, 'settle', 'allowance', -MIN(allowance_micro, ?3), ?4, ?5, ?6, 'gateway'
         FROM ai_balances WHERE account_id = ?7 AND MIN(allowance_micro, ?3) > 0`,
      ).bind(journal, now, charge, ref.type, ref.id, ref.user_id, accountId),
      env.LICENSE_DB.prepare(
        `INSERT OR IGNORE INTO ai_ledger (journal_id, account_id, at, kind, bucket, amount_micro, ref_type, ref_id,
           user_id, actor)
         SELECT ?1, account_id, ?2, 'settle', 'prepaid', -(?3 - allowance_micro), ?4, ?5, ?6, 'gateway'
         FROM ai_balances WHERE account_id = ?7 AND ?3 > allowance_micro`,
      ).bind(journal, now, charge, ref.type, ref.id, ref.user_id, accountId),
    );
  }
  statements.push(typeof release === 'number'
    ? env.LICENSE_DB.prepare(
      `UPDATE ai_balances SET
         prepaid_micro = prepaid_micro - MAX(?2 - allowance_micro, 0),
         allowance_micro = MAX(allowance_micro - ?2, 0),
         held_micro = MAX(held_micro - ?3, 0),
         updated_at = ?4
       WHERE account_id = ?1`,
    ).bind(accountId, charge, release, now)
    : env.LICENSE_DB.prepare(
      `UPDATE ai_balances SET
         prepaid_micro = prepaid_micro - MAX(?2 - allowance_micro, 0),
         allowance_micro = MAX(allowance_micro - ?2, 0),
         held_micro = MAX(held_micro - MIN(?2, (SELECT COALESCE(SUM(micro), 0) FROM ai_holds WHERE run_id = ?3)), 0),
         updated_at = ?4
       WHERE account_id = ?1`,
    ).bind(accountId, charge, release.run_id, now));
  if (typeof release !== 'number') {
    statements.push(env.LICENSE_DB.prepare('UPDATE ai_holds SET micro = MAX(micro - ?2, 0) WHERE run_id = ?1')
      .bind(release.run_id, charge));
  }
  return statements;
}

/** Settle a single call: charge its price, drop its hold. */
export async function settleCall(env: Env, accountId: string, holdId: string | null, holdMicro: number,
  charge: number, now: number, requestId: string, userId: string | null): Promise<void> {
  const statements = drawStatements(env, accountId, charge, holdId ? holdMicro : 0, now, `settle:${requestId}`,
    { type: 'request', id: requestId, user_id: userId });
  if (holdId) statements.unshift(env.LICENSE_DB.prepare('DELETE FROM ai_holds WHERE id = ?1').bind(holdId));
  await env.LICENSE_DB.batch(statements);
}

/** Give a hold back without charging anything (the call never reached a provider). */
export async function releaseHold(env: Env, accountId: string, holdId: string, micro: number, now: number) {
  await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare('DELETE FROM ai_holds WHERE id = ?1').bind(holdId),
    env.LICENSE_DB.prepare(
      'UPDATE ai_balances SET held_micro = MAX(held_micro - ?2, 0), updated_at = ?3 WHERE account_id = ?1',
    ).bind(accountId, micro, now),
  ]);
}

/** Credit an account (a purchase, an admin grant, an adjustment, a refund). */
export async function credit(env: Env, accountId: string, input: { micro: number; kind: 'purchase' | 'grant' |
  'adjustment' | 'refund'; journal_id?: string | null; actor: string; note?: string | null; ref_type?: string | null;
  ref_id?: string | null }, now: number): Promise<{ posted: boolean }> {
  const journal = input.journal_id || newId('jrn');
  await env.LICENSE_DB.prepare(
    `INSERT OR IGNORE INTO ai_balances (account_id, prepaid_micro, allowance_micro, allowance_period, held_micro,
       updated_at) VALUES (?1, 0, 0, NULL, 0, ?2)`).bind(accountId, now).run();
  // One transaction: the balance moves only when the journal is new, then the
  // leg is written, so a retried credit posts exactly once.
  const results = await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare(
      `UPDATE ai_balances SET prepaid_micro = prepaid_micro + ?2, updated_at = ?3
       WHERE account_id = ?1 AND NOT EXISTS (SELECT 1 FROM ai_ledger WHERE journal_id = ?4 AND bucket = 'prepaid')`,
    ).bind(accountId, input.micro, now, journal),
    env.LICENSE_DB.prepare(
      `INSERT OR IGNORE INTO ai_ledger (journal_id, account_id, at, kind, bucket, amount_micro, ref_type, ref_id, actor,
         note) VALUES (?1, ?2, ?3, ?4, 'prepaid', ?5, ?6, ?7, ?8, ?9)`,
    ).bind(journal, accountId, now, input.kind, input.micro, input.ref_type ?? null, input.ref_id ?? null,
      input.actor, input.note ?? null),
  ]);
  return { posted: (results[0]?.meta.changes ?? 0) === 1 };
}

// -- runs ------------------------------------------------------------------------

export interface RunRow {
  id: string;
  account_id: string;
  user_id: string | null;
  seat_id: string | null;
  billing: 'credits' | 'dev';
  session_id: string | null;
  feature: string;
  unit: string;
  units: number;
  quote_micro: number;
  hold_micro: number;
  accrued_micro: number;
  charged_micro: number;
  cost_micro: number;
  envelope_calls: number;
  calls: number;
  status: 'open' | 'finished' | 'expired';
  started_at: number;
  expires_at: number;
  closed_at: number | null;
}

export const runById = (env: Env, id: string) => one<RunRow>(env, 'SELECT * FROM ai_runs WHERE id = ?1', id);

/** Count a call against a run's envelope, atomically; false when it is spent or closed. */
export async function claimRunCall(env: Env, runId: string, accountId: string, now: number): Promise<boolean> {
  const result = await env.LICENSE_DB.prepare(
    `UPDATE ai_runs SET calls = calls + 1 WHERE id = ?1 AND account_id = ?2 AND status = 'open'
       AND expires_at > ?3 AND calls < envelope_calls`,
  ).bind(runId, accountId, now).run();
  return (result.meta.changes ?? 0) === 1;
}

/**
 * Settle a call inside a run: accrue its price, and charge only what takes the
 * run's total to min(accrued, quote), out of the run's own hold. The charge is
 * computed by ONE UPDATE from the run's current totals (SQLite evaluates every
 * SET expression against the old row), so concurrent calls of a run compose
 * and the run can never be charged past its quote.
 */
export async function settleRunCall(env: Env, run: RunRow, price: number, cost: number, now: number,
  requestId: string, userId: string | null): Promise<number> {
  const row = await env.LICENSE_DB.prepare(
    `UPDATE ai_runs SET
       last_charge_micro = MAX(0, MIN(accrued_micro + ?2, quote_micro) - charged_micro),
       charged_micro = MAX(charged_micro, MIN(accrued_micro + ?2, quote_micro)),
       hold_micro = MAX(hold_micro - MAX(0, MIN(accrued_micro + ?2, quote_micro) - charged_micro), 0),
       accrued_micro = accrued_micro + ?2,
       cost_micro = cost_micro + ?3
     WHERE id = ?1 RETURNING last_charge_micro`,
  ).bind(run.id, price, cost).first<{ last_charge_micro: number }>();
  const charge = row?.last_charge_micro ?? 0;
  if (charge > 0) {
    await env.LICENSE_DB.batch(drawStatements(env, run.account_id, charge, { run_id: run.id }, now,
      `settle:${requestId}`, { type: 'request', id: requestId, user_id: userId }));
  }
  return charge;
}

/** Close a run and give back whatever of its hold it did not spend -- in one transaction. */
export async function finishRun(env: Env, run: RunRow, now: number, status: 'finished' | 'expired' = 'finished') {
  await env.LICENSE_DB.batch([
    env.LICENSE_DB.prepare(
      `UPDATE ai_balances SET held_micro = MAX(held_micro - (SELECT COALESCE(SUM(micro), 0) FROM ai_holds
         WHERE run_id = ?1), 0), updated_at = ?3
       WHERE account_id = ?2 AND EXISTS (SELECT 1 FROM ai_runs WHERE id = ?1 AND status = 'open')`,
    ).bind(run.id, run.account_id, now),
    env.LICENSE_DB.prepare(
      `DELETE FROM ai_holds WHERE run_id = ?1 AND EXISTS (SELECT 1 FROM ai_runs WHERE id = ?1 AND status = 'open')`,
    ).bind(run.id),
    env.LICENSE_DB.prepare(
      `UPDATE ai_runs SET status = ?2, closed_at = ?3, hold_micro = 0 WHERE id = ?1 AND status = 'open'`,
    ).bind(run.id, status, now),
  ]);
}

/** Runs past their expiry, closed by the daily cron (their holds already lapsed). */
export async function expireRuns(env: Env, now: number): Promise<number> {
  const runs = await all<RunRow>(env,
    `SELECT * FROM ai_runs WHERE status = 'open' AND expires_at < ?1 LIMIT 500`, now);
  for (const run of runs) await finishRun(env, run, now, 'expired');
  return runs.length;
}
