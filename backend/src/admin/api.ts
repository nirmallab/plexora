/**
 * /admin/api/*: downloads and mutations. Mutations sit behind the same-origin
 * guard (routes.ts) and take JSON only. Archives are streamed through admin
 * auth; the bucket is never public and no presigned URL is ever minted.
 */
import { Hono } from 'hono';

import { addDays, dayOf, knob, nowSeconds } from '../env';
import { type AppEnv, jsonError, NO_STORE, readJson } from '../http';
import type { Part } from '../telemetry/archive';
import { Meter, resetConfigCache } from '../telemetry/budget';
import { STEP_ORDER, tick } from '../telemetry/cron';
import { backups, budget } from '../telemetry/queries';
import { MONTHLY_TABLES } from '../telemetry/archive';
import { fromBase64, gzip } from '../telemetry/store';

export const api = new Hono<AppEnv>();

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

api.get('/budget', async (c) => c.json(await budget(c.var.meter, c.env, nowSeconds()), 200, NO_STORE));

/** The raw cleaned batches of a day range, as one multi-member .jsonl.gz. */
api.get('/export', async (c) => {
  const from = c.req.query('from') ?? '';
  const to = c.req.query('to') ?? from;
  if (!DAY_RE.test(from) || !DAY_RE.test(to) || from > to) return jsonError(c, 400, 'from and to must be YYYY-MM-DD.');
  if (addDays(from, knob(c.env, 'EXPORT_MAX_DAYS') - 1) < to) {
    return jsonError(c, 400, `At most ${knob(c.env, 'EXPORT_MAX_DAYS')} days per export.`);
  }
  const env = c.env;
  const meter = new Meter(env.TELEMETRY_DB);
  const page = knob(env, 'EXPORT_PAGE_ROWS');
  let cursor = { day: from, install: '', id: '' };
  let finished = false;
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (finished) return;
      const rows = await meter.all<{ day: string; install_id: string; id: string; events_gz: string }>(
        meter
          .prepare(
            `SELECT day, install_id, id, events_gz FROM batches
             WHERE (day, install_id, id) > (?1, ?2, ?3) AND day >= ?1 AND day <= ?4
             ORDER BY day, install_id, id LIMIT ?5`,
          )
          .bind(cursor.day, cursor.install, cursor.id, to, page),
      );
      for (const row of rows) controller.enqueue(fromBase64(row.events_gz));
      const last = rows[rows.length - 1];
      if (last) cursor = { day: last.day, install: last.install_id, id: last.id };
      if (rows.length < page) {
        finished = true;
        controller.close();
        await meter.flush(env, nowSeconds());
      }
    },
  });
  return new Response(stream, {
    headers: {
      ...NO_STORE,
      'Content-Type': 'application/gzip',
      'Content-Disposition': `attachment; filename="plexora-batches-${from}-${to}.jsonl.gz"`,
    },
  });
});

/** One rollup table as gzipped CSV, capped at CSV_MAX_ROWS. */
api.get('/rollups/:name', async (c) => {
  const match = /^([a-z_]+)\.csv\.gz$/.exec(c.req.param('name'));
  const spec = match ? MONTHLY_TABLES.find((t) => t.table === match[1]) : undefined;
  if (!spec) return jsonError(c, 404, 'Unknown rollup table.');
  const today = dayOf(nowSeconds());
  const from = DAY_RE.test(c.req.query('from') ?? '') ? c.req.query('from')! : addDays(today, -30);
  const to = DAY_RE.test(c.req.query('to') ?? '') ? c.req.query('to')! : today;
  const cap = knob(c.env, 'CSV_MAX_ROWS');
  const rows = await c.var.meter.all<Record<string, unknown>>(
    c.var.meter
      .prepare(`SELECT * FROM ${spec.table} WHERE day >= ?1 AND day <= ?2 ORDER BY ${spec.pk.join(', ')} LIMIT ?3`)
      .bind(from, to, cap + 1),
  );
  const truncated = rows.length > cap;
  const columns = Object.keys(rows[0] ?? Object.fromEntries(spec.pk.map((k) => [k, ''])));
  const cell = (value: unknown) => {
    const text = value === null || value === undefined ? '' : String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const lines = [columns.join(','), ...rows.slice(0, cap).map((row) => columns.map((k) => cell(row[k])).join(','))];
  return new Response(await gzip(`${lines.join('\n')}\n`), {
    headers: {
      ...NO_STORE,
      'Content-Type': 'application/gzip',
      'Content-Disposition': `attachment; filename="${spec.table}-${from}-${to}.csv.gz"`,
      'X-Truncated': truncated ? 'true' : 'false',
    },
  });
});

api.get('/backups', async (c) => c.json(await backups(c.var.meter), 200, NO_STORE));

function backupKey(path: string): string | null {
  const key = decodeURIComponent(path.replace(/^.*?\/admin\/api\/backups\//, ''));
  return /^archives\/[0-9A-Za-z/_.-]+$/.test(key) && !key.includes('..') ? key : null;
}

api.get('/backups/*', async (c) => {
  const key = backupKey(c.req.path);
  if (!key) return jsonError(c, 400, 'Not an archive key.');
  const object = await c.env.ARCHIVES.get(key);
  c.var.meter.r2Gets += 1;
  if (!object) return jsonError(c, 404, 'No such archive.');
  return new Response(object.body, {
    headers: {
      ...NO_STORE,
      'Content-Type': 'application/gzip',
      'Content-Disposition': `attachment; filename="${key.split('/').pop()}"`,
    },
  });
});

/** Deletes every part of an archive; its verified_at is cleared so purge waits for a fresh export. */
api.delete('/backups/*', async (c) => {
  const key = backupKey(c.req.path);
  if (!key) return jsonError(c, 400, 'Not an archive key.');
  const row = await c.var.meter.first<{ parts_json: string }>(
    c.var.meter.prepare('SELECT parts_json FROM archives WHERE key = ?1 AND deleted_at IS NULL').bind(key),
  );
  if (!row) return jsonError(c, 404, 'No such archive.');
  const parts = JSON.parse(row.parts_json) as Part[];
  await c.env.ARCHIVES.delete(parts.map((part) => part.key));
  await c.var.meter.run(
    c.var.meter.prepare('UPDATE archives SET deleted_at = ?2, verified_at = NULL WHERE key = ?1').bind(key, nowSeconds()),
  );
  return c.json({ ok: true, deleted: parts.length }, 200, NO_STORE);
});

/** Per-version (or '*') client config: level ceiling, interval, sample, 24 h kill switch. */
api.post('/client-config', async (c) => {
  const body = await readJson(c);
  const version = typeof body.version === 'string' ? body.version : '';
  if (!/^(?:\*|[0-9A-Za-z.+-]{1,48})$/.test(version)) return jsonError(c, 400, "version must be a version string or '*'.");
  const level = body.level_max ?? null;
  if (level !== null && level !== 'anonymous' && level !== 'diagnostics') return jsonError(c, 400, 'level_max must be anonymous or diagnostics.');
  const interval = body.upload_interval_s ?? null;
  if (interval !== null && !(Number.isInteger(interval) && (interval as number) >= 60 && (interval as number) <= 7 * 86400)) {
    return jsonError(c, 400, 'upload_interval_s must be an integer from 60 to 604800.');
  }
  const sample = body.sample ?? null;
  if (sample !== null && !(typeof sample === 'number' && sample >= 0 && sample <= 1)) return jsonError(c, 400, 'sample must be 0..1.');
  const note = typeof body.note === 'string' ? body.note.slice(0, 200) : null;
  const now = nowSeconds();
  // A kill switch lasts a day and must be renewed: a forgotten one recovers.
  const disabledUntil = body.disabled === true ? now + 86400 : 0;
  await c.var.meter.run(
    c.var.meter
      .prepare(
        `INSERT INTO client_config (version, level_max, upload_interval_s, sample, disabled_until, note, updated_at, updated_by)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)
         ON CONFLICT(version) DO UPDATE SET level_max = excluded.level_max, upload_interval_s = excluded.upload_interval_s,
           sample = excluded.sample, disabled_until = excluded.disabled_until, note = excluded.note,
           updated_at = excluded.updated_at, updated_by = excluded.updated_by`,
      )
      .bind(version, level, interval, sample, disabledUntil, note, now, c.var.admin),
  );
  resetConfigCache();
  return c.json({ ok: true, version, disabled_until: disabledUntil }, 200, NO_STORE);
});

api.post('/errors/:fp/mute', async (c) => {
  const fp = c.req.param('fp');
  if (!/^[0-9a-f]{8}(?:[0-9a-f]{8})?$/.test(fp)) return jsonError(c, 400, 'Not a fingerprint.');
  const body = await readJson(c);
  const muted = body.muted !== false;
  const result = await c.var.meter.run(
    c.var.meter
      .prepare('UPDATE error_fingerprints SET muted_at = ?2, muted_by = ?3, notes = COALESCE(?4, notes) WHERE fp = ?1')
      .bind(fp, muted ? nowSeconds() : null, muted ? c.var.admin : null, typeof body.notes === 'string' ? body.notes.slice(0, 500) : null),
  );
  if (!result.meta.changes) return jsonError(c, 404, 'Unknown fingerprint.');
  return c.json({ ok: true, fp, muted }, 200, NO_STORE);
});

/** Nulls identity columns and deletes detail rows (bounded to each table's retention window). */
api.post('/erase/:install_id', async (c) => {
  const id = c.req.param('install_id');
  if (!/^[0-9a-f]{32}$/.test(id)) return jsonError(c, 400, 'Not an install id.');
  const now = nowSeconds();
  const today = dayOf(now);
  const results = await c.var.meter.batch([
    c.var.meter
      .prepare('DELETE FROM batches WHERE day >= ?1 AND install_id = ?2')
      .bind(addDays(today, -knob(c.env, 'BATCH_RETENTION_HARD_DAYS') - 5), id),
    c.var.meter
      .prepare('DELETE FROM daily_installs WHERE day >= ?1 AND install_id = ?2')
      .bind(addDays(today, -knob(c.env, 'DAILY_INSTALLS_RETENTION_DAYS') - 5), id),
    c.var.meter
      .prepare(
        `UPDATE installs SET erased_at = ?2, version = NULL, python = NULL, os = NULL, arch = NULL, launch_mode = NULL,
           deployment = NULL, scheduler = NULL, install_kind = NULL, mode = NULL, country = NULL,
           machine_id_hash = NULL, license_id = NULL, license_tier = NULL WHERE install_id = ?1`,
      )
      .bind(id, now),
  ]);
  return c.json(
    { ok: true, batches: results[0]?.meta.changes ?? 0, daily_installs: results[1]?.meta.changes ?? 0, install: results[2]?.meta.changes ?? 0 },
    200,
    NO_STORE,
  );
});

/** One maintenance tick now, optionally limited to one step. */
api.post('/maintenance/run', async (c) => {
  const body = await readJson(c);
  const step = typeof body.step === 'string' ? body.step : undefined;
  if (step !== undefined && !(step in STEP_ORDER)) return jsonError(c, 400, 'Unknown step.');
  return c.json(await tick(c.env, nowSeconds(), step), 200, NO_STORE);
});
