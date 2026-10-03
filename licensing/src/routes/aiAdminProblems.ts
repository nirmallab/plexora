/**
 * The admin API for what needs attention, under /admin/api/ai (admin only, same-origin, JSON):
 *
 *   GET    /problems                 every open problem, each with its stable key and whether it is dismissed
 *   POST   /problems/:key/dismiss    hide a problem until it changes (its key is gone, or a new one appears)
 *   DELETE /problems/:key/dismiss    show it again
 *   GET    /summary                  providers, models, the general model, fallbacks, tasks and problems in one line
 *
 * A dismissal is kept by the problem's key (ai_dismissals, schema v5) and
 * forgotten once the problem is gone, so one that comes back is shown again.
 */
import { Hono } from 'hono';

import { nowSeconds } from '../env';
import { eventStatement } from '../events';
import { ApiError, type AppEnv, ok } from '../http';
import { PROBLEM_KEY, problems, summary } from '../ai/views';

export const aiProblemsAdmin = new Hono<AppEnv>();

const who = (c: { get: (k: 'admin') => string }) => `admin:${c.get('admin')}`;

/** The key from the path. Hono has already decoded it once; decoding again would turn a `%` in it into another. */
function keyOf(value: string | undefined): string {
  const key = value ?? '';
  if (!PROBLEM_KEY.test(key)) throw new ApiError(400, 'invalid_request', 'That is not a problem key.');
  return key;
}

aiProblemsAdmin.get('/problems', async (c) => {
  const found = await problems(c.env, nowSeconds());
  return ok(c, { problems: found, open: found.filter((p) => !p.dismissed).length,
    dismissed: found.filter((p) => p.dismissed).length });
});

aiProblemsAdmin.post('/problems/:key/dismiss', async (c) => {
  const now = nowSeconds();
  const key = keyOf(c.req.param('key'));
  const found = (await problems(c.env, now, undefined, undefined, { purge: false })).find((p) => p.key === key);
  if (!found) throw new ApiError(404, 'not_found', 'No such problem is open now.');
  await c.env.LICENSE_DB.batch([
    c.env.LICENSE_DB.prepare(
      `INSERT INTO ai_dismissals (key, dismissed_at, dismissed_by, text) VALUES (?1, ?2, ?3, ?4)
       ON CONFLICT(key) DO UPDATE SET dismissed_at = ?2, dismissed_by = ?3, text = ?4`,
    ).bind(key, now, who(c), found.text),
    eventStatement(c.env, now, { actor: who(c), kind: 'ai.problem_dismissed', payload: { key, text: found.text } }),
  ]);
  return ok(c, { key, dismissed: true });
});

aiProblemsAdmin.delete('/problems/:key/dismiss', async (c) => {
  const now = nowSeconds();
  const key = keyOf(c.req.param('key'));
  const result = await c.env.LICENSE_DB.prepare('DELETE FROM ai_dismissals WHERE key = ?1').bind(key).run();
  if (!result.meta.changes) throw new ApiError(404, 'not_found', 'That problem is not dismissed.');
  await eventStatement(c.env, now, { actor: who(c), kind: 'ai.problem_restored', payload: { key } }).run();
  return ok(c, { key, dismissed: false });
});

aiProblemsAdmin.get('/summary', async (c) => ok(c, await summary(c.env, nowSeconds())));
