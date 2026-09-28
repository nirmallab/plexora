/**
 * The single quarter-hourly cron: one slice of one maintenance step per tick.
 *
 * The first tick that finds no maintenance_log rows for D-1 (normally the
 * first after 00:00 UTC; any later tick if that one was missed) enqueues the
 * day's steps. Every tick then runs one bounded slice of the lowest pending
 * `(day, step_order)` and continues from its cursor next tick, because a
 * free-plan cron invocation gets the same 10 ms of CPU as a request.
 *
 * Rerun safety: steps are keyed (day, step) and re-entrant; a `running` row is
 * simply resumed from its cursor (a crashed tick leaves the previous slice's
 * committed cursor behind, never a half-applied slice).
 */
import { addDays, dayOf, type Env, knob, nowSeconds } from '../env';
import { exportDaySlice, exportMonthSlice, monthlyBase, monthOf, partKey, dailyKey, verifyArchive } from './archive';
import {
  type BudgetState,
  cronAllows,
  type Counters,
  forceStop,
  isQuotaError,
  Meter,
  stateFor,
} from './budget';
import { archiveSweepSlice, purgeBatchesSlice, purgeRollupsSlice, reconcileSlice } from './retention';
import { clearStatements, foldStatements, LAST, sliceEnd } from './rollup';

export interface StepRow {
  day: string;
  step: string;
  step_order: number;
  state: string;
  cursor: string | null;
  attempts: number;
  started: number | null;
  finished: number | null;
}

export interface StepContext {
  env: Env;
  meter: Meter;
  now: number;
  today: string;
  row: StepRow;
  budget: BudgetState;
}

export interface StepOutcome {
  state: 'running' | 'done' | 'skipped' | 'failed' | 'pending';
  cursor?: string | null;
  error?: string | null;
  objectKey?: string | null;
  checksum?: string | null;
  /** Committed in the same db.batch() as the log update. */
  statements?: D1PreparedStatement[];
  /** A failed verify: skip the rest of that day's steps. */
  blockDownstream?: boolean;
  /** Re-queued rather than retried: do not count this as a new attempt. */
  keepAttempts?: boolean;
}

/** Step name → order. Only these rows are scheduled; log-only rows use ≥ 1000. */
export const STEP_ORDER: Record<string, number> = {
  fold: 10,
  export: 20,
  verify: 30,
  purge_batches: 40,
  purge_rollups: 50,
  archive_sweep: 60,
  finalize: 70,
  r2_reconcile: 80,
  monthly_export: 90,
  monthly_verify: 95,
};

/** Steps that must not start before another step of the same day finished. */
const DEPENDS_ON: Record<string, string> = { verify: 'export', monthly_verify: 'monthly_export' };

type Runner = (ctx: StepContext) => Promise<StepOutcome>;

const RUNNERS: Record<string, Runner> = {
  fold: foldSlice,
  export: exportDaySlice,
  verify: (ctx) => verifyArchive(ctx, dailyKey(ctx.row.day), 'export'),
  purge_batches: purgeBatchesSlice,
  purge_rollups: purgeRollupsSlice,
  archive_sweep: archiveSweepSlice,
  finalize: finalizeStep,
  r2_reconcile: reconcileSlice,
  monthly_export: exportMonthSlice,
  monthly_verify: (ctx) => verifyArchive(ctx, partKey(monthlyBase(monthOf(ctx.row.day)), 0), 'monthly_export'),
};

// ---------------------------------------------------------------------------
// Steps that live here
// ---------------------------------------------------------------------------

/** One fold slice; the first also clears the day's rollups, atomically with it. */
async function foldSlice(ctx: StepContext): Promise<StepOutcome> {
  const lo = ctx.row.state === 'pending' ? '' : ctx.row.cursor ?? '';
  const hi = await sliceEnd(ctx.meter, ctx.row.day, lo, knob(ctx.env, 'FOLD_SLICE_BATCHES'));
  const statements = lo === '' ? clearStatements(ctx.meter, ctx.row.day) : [];
  statements.push(...foldStatements(ctx.meter, ctx.row.day, lo, hi));
  return { state: hi === LAST ? 'done' : 'running', cursor: hi === LAST ? null : hi, statements };
}

/** The day's final budget state, recorded on its own budget_daily row. */
async function finalizeStep(ctx: StepContext): Promise<StepOutcome> {
  const row = await ctx.meter.first<Counters>(
    ctx.meter
      .prepare('SELECT requests, d1_rows_read, d1_rows_written FROM budget_daily WHERE day = ?1')
      .bind(ctx.row.day),
  );
  if (!row) return { state: 'done' };
  return {
    state: 'done',
    statements: [
      ctx.meter
        .prepare('UPDATE budget_daily SET state = ?2, state_changed_at = ?3 WHERE day = ?1')
        .bind(ctx.row.day, stateFor(ctx.env, row), ctx.now),
    ],
  };
}

// ---------------------------------------------------------------------------
// Scheduling
// ---------------------------------------------------------------------------

function insertStep(meter: Meter, day: string, step: string) {
  return meter
    .prepare(
      `INSERT INTO maintenance_log (day, step, step_order, state) VALUES (?1, ?2, ?3, 'pending')
       ON CONFLICT(day, step) DO NOTHING`,
    )
    .bind(day, step, STEP_ORDER[step]!);
}

/**
 * Enqueues D-1 when its fold row is missing: one point read per tick. Also
 * reopens D-2..D-REOPEN_DAYS when a batch arrived after their fold finished
 * (only possible when a fold ran early, e.g. a manual maintenance run).
 */
export async function enqueue(meter: Meter, env: Env, today: string): Promise<boolean> {
  const yesterday = addDays(today, -1);
  const existing = await meter.first(
    meter.prepare(`SELECT 1 AS hit FROM maintenance_log WHERE day = ?1 AND step = 'fold'`).bind(yesterday),
  );
  if (existing) return false;

  const steps = ['fold', 'export', 'verify', 'purge_batches', 'purge_rollups', 'archive_sweep', 'finalize'];
  if (new Date(`${today}T00:00:00Z`).getUTCDay() === 0) steps.push('r2_reconcile');
  // On the 1st, D-1 is the last day of the previous month: its monthly archive.
  if (today.endsWith('-01')) steps.push('monthly_export', 'monthly_verify');
  const statements = steps.map((step) => insertStep(meter, yesterday, step));

  for (let back = 2; back <= knob(env, 'REOPEN_DAYS'); back += 1) {
    const day = addDays(today, -back);
    statements.push(
      meter
        .prepare(
          `UPDATE maintenance_log SET state = 'pending', cursor = NULL, attempts = 0
           WHERE day = ?1 AND step IN ('fold', 'export', 'verify') AND state = 'done'
             AND (SELECT MAX(received) FROM batches WHERE day = ?1) >
                 (SELECT finished FROM maintenance_log WHERE day = ?1 AND step = 'fold')`,
        )
        .bind(day),
    );
  }
  await meter.batch(statements);
  return true;
}

const FINISHED = new Set(['done', 'skipped']);

/**
 * The next runnable step: lowest (day, step_order) among pending/running rows
 * in the lookback window whose earlier same-day steps are finished -- or are
 * merely deferred by the budget state and are not this step's dependency.
 * `only` (an admin run) picks that step's oldest pending row regardless of order.
 */
export function pickStep(rows: StepRow[], budget: BudgetState, only?: string): StepRow | null {
  const byDay = new Map<string, StepRow[]>();
  for (const row of rows) {
    if (!(row.step in STEP_ORDER)) continue;
    const list = byDay.get(row.day) ?? [];
    list.push(row);
    byDay.set(row.day, list);
  }
  for (const day of [...byDay.keys()].sort()) {
    const steps = byDay.get(day)!.sort((a, b) => a.step_order - b.step_order);
    for (const row of steps) {
      if (row.state !== 'pending' && row.state !== 'running') continue;
      if (only && row.step !== only) continue;
      if (!cronAllows(budget, row.step)) continue;
      // An explicit admin run of one step skips the ordering, not the step's
      // own safety checks (purge still demands a verified archive).
      if (only) return row;
      const blocked = steps.some(
        (earlier) =>
          earlier.step_order < row.step_order &&
          !FINISHED.has(earlier.state) &&
          (cronAllows(budget, earlier.step) || DEPENDS_ON[row.step] === earlier.step),
      );
      if (!blocked) return row;
    }
  }
  return null;
}

export interface TickResult {
  state: BudgetState;
  enqueued: boolean;
  step: string | null;
  day: string | null;
  outcome: StepOutcome['state'] | null;
  error?: string;
}

/** One cron tick (also POST /admin/api/maintenance/run). */
export async function tick(env: Env, now = nowSeconds(), only?: string): Promise<TickResult> {
  const meter = new Meter(env.TELEMETRY_DB);
  const today = dayOf(now);
  let budget: BudgetState = 'ok';
  try {
    const row = await meter.first<Counters>(
      meter.prepare('SELECT requests, d1_rows_read, d1_rows_written FROM budget_daily WHERE day = ?1').bind(today),
    );
    budget = row ? stateFor(env, row) : 'ok';
  } catch (error) {
    if (isQuotaError(error)) forceStop(now);
    return { state: 'stop', enqueued: false, step: null, day: null, outcome: null, error: String(error) };
  }
  // At stop the tick reads its row and returns: no writes at all.
  if (budget === 'stop') return { state: budget, enqueued: false, step: null, day: null, outcome: null };

  const result: TickResult = { state: budget, enqueued: false, step: null, day: null, outcome: null };
  try {
    result.enqueued = await enqueue(meter, env, today);
    const rows = await meter.all<StepRow>(
      meter
        .prepare(
          `SELECT day, step, step_order, state, cursor, attempts, started, finished FROM maintenance_log
           WHERE day >= ?1 AND step_order < 1000 ORDER BY day, step_order`,
        )
        .bind(addDays(today, -knob(env, 'MAINTENANCE_LOOKBACK_DAYS'))),
    );
    const row = pickStep(rows, budget, only);
    if (row) {
      result.step = row.step;
      result.day = row.day;
      const outcome = await runStep({ env, meter, now, today, row, budget });
      result.outcome = outcome.state;
      if (outcome.error) result.error = outcome.error;
    }
  } catch (error) {
    if (isQuotaError(error)) forceStop(now);
    result.error = String(error);
  }
  await meter.flush(env, now, { cron_ticks: 1, requests: 1 }, budget);
  return result;
}

async function runStep(ctx: StepContext): Promise<StepOutcome> {
  const { row, meter } = ctx;
  let outcome: StepOutcome;
  try {
    outcome = await RUNNERS[row.step]!(ctx);
  } catch (error) {
    // The slice did not commit; its cursor did not move. Record why and let
    // the next tick try again.
    outcome = { state: row.state === 'running' ? 'running' : 'pending', error: String(error), statements: [] };
    await meter.run(
      meter
        .prepare('UPDATE maintenance_log SET error = ?3 WHERE day = ?1 AND step = ?2')
        .bind(row.day, row.step, String(error).slice(0, 500)),
    );
    return outcome;
  }

  const finished = outcome.state === 'done' || outcome.state === 'skipped' || outcome.state === 'failed';
  const newAttempt = row.state === 'pending' && !outcome.keepAttempts;
  const cursor = outcome.cursor === undefined ? row.cursor : outcome.cursor;
  const statements = [...(outcome.statements ?? [])];
  statements.push(
    meter
      .prepare(
        `UPDATE maintenance_log SET state = ?3, cursor = ?4, attempts = attempts + ?5,
           started = COALESCE(started, ?6), finished = ?7, error = ?8,
           object_key = COALESCE(?9, object_key), checksum = COALESCE(?10, checksum),
           rows_read = rows_read + ?11, rows_written = rows_written + ?12
         WHERE day = ?1 AND step = ?2`,
      )
      .bind(
        row.day,
        row.step,
        outcome.state,
        finished ? null : cursor,
        newAttempt ? 1 : 0,
        ctx.now,
        finished ? ctx.now : null,
        outcome.error ?? null,
        outcome.objectKey ?? null,
        outcome.checksum ?? null,
        meter.reads,
        meter.writes,
      ),
  );
  if (outcome.blockDownstream) {
    statements.push(
      meter
        .prepare(
          `UPDATE maintenance_log SET state = 'skipped', error = 'upstream step failed', finished = ?3
           WHERE day = ?1 AND step_order > ?2 AND step_order < 1000 AND state IN ('pending', 'running')`,
        )
        .bind(row.day, row.step_order, ctx.now),
    );
  }
  await meter.batch(statements);
  return outcome;
}

export const scheduled: ExportedHandlerScheduledHandler<Env> = async (controller, env, ctx) => {
  ctx.waitUntil(tick(env, Math.floor(controller.scheduledTime / 1000)).then(() => undefined));
};
