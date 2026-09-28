/**
 * Fixed-window rate limits, counted in D1.
 *
 * In D1 rather than KV on purpose: the free tier allows 1,000 KV writes a day
 * and 100,000 D1 writes, and a limiter that an attacker can exhaust by being
 * limited is not a limiter. One UPSERT per counted request; the daily cron
 * deletes expired windows.
 *
 * These guard against ABUSE only. They are approximate at window edges, which
 * is accepted: every cap that matters for correctness (environments per seat,
 * one trial per mailbox) is enforced by the database itself.
 */
import type { Env } from './env';
import { ApiError } from './http';

export async function hit(env: Env, bucket: string, subject: string | null, limit: number,
  windowSeconds: number, now: number): Promise<boolean> {
  if (!subject || limit <= 0) return true;
  const start = now - (now % windowSeconds);
  const key = `${bucket}:${subject}:${start}`;
  const row = await env.LICENSE_DB.prepare(
    `INSERT INTO rate_limits (key, count, expires_at) VALUES (?1, 1, ?2)
     ON CONFLICT(key) DO UPDATE SET count = count + 1
     RETURNING count`,
  ).bind(key, start + windowSeconds).first<{ count: number }>();
  return (row?.count ?? 1) <= limit;
}

export async function enforce(env: Env, bucket: string, subject: string | null, limit: number,
  windowSeconds: number, now: number): Promise<void> {
  if (!(await hit(env, bucket, subject, limit, windowSeconds, now))) {
    const start = now - (now % windowSeconds);
    throw new ApiError(429, 'rate_limited', 'Too many requests; try again later.',
      { retry_after: Math.max(1, start + windowSeconds - now) });
  }
}
