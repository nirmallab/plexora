/**
 * The sign-in rate limit: a fixed one-hour window per peppered address, and
 * one for every address together, so neither one machine nor a botnet can
 * guess at ADMIN_TOKEN faster than a few dozen times an hour. Cloudflare
 * Access, when it is in front, is not limited; it never reaches the token.
 *
 * The counters live in `register_quota`, the registration quota's table,
 * under keys no registration can produce (`login:` + 32 hex, `login:*`) and
 * an hour (`YYYY-MM-DDTHH`) in its day column. The quota's retention purge
 * (`day < cutoff`) clears them with the rest, so this needs no schema change.
 *
 * Each counter only moves while it is under its limit, and the address is
 * counted first: an address past its own limit writes nothing and spends
 * nothing of the shared window, so one machine uses up at most its own share.
 */
import { dayOf, ipPepper, knob, type Env } from '../env';
import { addPending } from '../telemetry/budget';
import { hmacHex } from '../telemetry/tokens';

export const GLOBAL_LOGIN_KEY = 'login:*';

export type LoginGate = { ok: true } | { ok: false; status: 429 | 503; retryAfter: number; error: string };

/** The hour a unix time falls in, `YYYY-MM-DDTHH`. */
export function hourOf(seconds: number): string {
  return `${dayOf(seconds)}T${new Date(seconds * 1000).toISOString().slice(11, 13)}`;
}

/** Counts one sign-in attempt, or says why it may not be made. */
export async function loginAttempt(env: Env, address: string | undefined, now: number): Promise<LoginGate> {
  const retryAfter = 3600 - (now % 3600);
  const pepper = ipPepper(env);
  if (!pepper) {
    console.error('admin sign-in refused: IP_HASH_KEY is not set');
    return { ok: false, status: 503, retryAfter, error: 'Admin sign-in is not configured.' };
  }
  const hour = hourOf(now);
  const key = `login:${(await hmacHex(pepper, `login|${hour}|${address ?? 'none'}`)).slice(0, 32)}`;
  const db = env.TELEMETRY_DB;
  const count = (k: string, limit: number) =>
    db
      .prepare(
        `INSERT INTO register_quota (ip_hash, day, n) VALUES (?1, ?2, 1)
         ON CONFLICT(ip_hash, day) DO UPDATE SET n = n + 1 WHERE n < ?3 RETURNING n`,
      )
      .bind(k, hour, limit);
  const windows: [string, number][] = [
    [key, knob(env, 'LOGIN_LIMIT_PER_HOUR')],
    [GLOBAL_LOGIN_KEY, knob(env, 'LOGIN_GLOBAL_LIMIT_PER_HOUR')],
  ];
  for (const [name, limit] of windows) {
    let result: D1Result;
    try {
      result = await count(name, limit).run();
    } catch {
      // Fail closed: the token sign-in is the break-glass path, and a limiter
      // that cannot count is not one. Access and the bearer header still work.
      return { ok: false, status: 503, retryAfter: 60, error: 'Admin sign-in is unavailable right now.' };
    }
    addPending('d1_rows_written', result.meta.rows_written ?? 0);
    addPending('d1_rows_read', result.meta.rows_read ?? 0);
    // No row back: the window was already full and its counter did not move.
    if ((result.results?.length ?? 0) === 0) {
      return { ok: false, status: 429, retryAfter, error: 'Too many sign-in attempts. Try again later.' };
    }
  }
  return { ok: true };
}
