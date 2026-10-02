/**
 * Daily backups of D1 to R2, gzipped.
 *
 *   backups/daily/YYYY-MM-DD.json.gz    kept BACKUP_DAILY_DAYS
 *   backups/monthly/YYYY-MM.json.gz     the first of each month, kept for good
 *
 * Every table listed in sqlite_master except `sessions`, `login_links` and
 * `rate_limits`, which are secrets or noise and are rebuilt by use. A snapshot
 * of this service's scale is kilobytes to a few megabytes; paging keeps any one
 * query small regardless. Restore is `wrangler d1 execute` from a script that
 * reads the JSON -- see README.md.
 */
import { dayOf, type Env, knob, DAY } from './env';

const SKIP = new Set(['sessions', 'login_links', 'rate_limits', 'sqlite_sequence', 'd1_migrations', 'ai_holds',
  'ai_idempotency', 'ai_sticky', 'ai_sticky_upstream', 'ai_circuits']);
const PAGE = 1000;

async function gzip(text: string): Promise<Uint8Array> {
  const stream = new Blob([new TextEncoder().encode(text)]).stream().pipeThrough(new CompressionStream('gzip'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

export async function snapshot(env: Env, now: number): Promise<Record<string, unknown>> {
  const tables = (await env.LICENSE_DB.prepare(
    `SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name`,
  ).all<{ name: string }>()).results.map((r) => r.name)
    // `_cf_*` are D1's own bookkeeping tables, which a Worker may not read.
    .filter((name) => !SKIP.has(name) && !name.startsWith('sqlite_') && !name.startsWith('_cf_'));
  const out: Record<string, unknown[]> = {};
  for (const table of tables) {
    const rows: unknown[] = [];
    for (let offset = 0; ; offset += PAGE) {
      const page = (await env.LICENSE_DB.prepare(`SELECT * FROM "${table}" LIMIT ?1 OFFSET ?2`)
        .bind(PAGE, offset).all()).results;
      rows.push(...page);
      if (page.length < PAGE) break;
    }
    out[table] = rows;
  }
  return { schema: 1, product: 'plexora-licensing', taken_at: now, tables: out };
}

export async function backupToR2(env: Env, now: number): Promise<Record<string, unknown>> {
  const day = dayOf(now);
  const body = await gzip(JSON.stringify(await snapshot(env, now)));
  await env.BACKUPS.put(`backups/daily/${day}.json.gz`, body, {
    httpMetadata: { contentType: 'application/json', contentEncoding: 'gzip' },
  });
  let monthly = false;
  if (day.endsWith('-01')) {
    await env.BACKUPS.put(`backups/monthly/${day.slice(0, 7)}.json.gz`, body, {
      httpMetadata: { contentType: 'application/json', contentEncoding: 'gzip' },
    });
    monthly = true;
  }
  // Prune old dailies, a page at a time.
  const cutoff = dayOf(now - knob(env, 'BACKUP_DAILY_DAYS') * DAY);
  let pruned = 0;
  let cursor: string | undefined;
  do {
    const listing = await env.BACKUPS.list({ prefix: 'backups/daily/', cursor, limit: 500 });
    const old = listing.objects.map((o) => o.key)
      .filter((key) => key.slice('backups/daily/'.length, 'backups/daily/'.length + 10) < cutoff);
    if (old.length > 0) {
      await env.BACKUPS.delete(old);
      pruned += old.length;
    }
    cursor = listing.truncated ? listing.cursor : undefined;
  } while (cursor);
  return { key: `backups/daily/${day}.json.gz`, bytes: body.byteLength, monthly, pruned };
}
