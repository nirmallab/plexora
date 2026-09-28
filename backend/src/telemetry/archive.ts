/**
 * Daily and monthly archives in R2, and their verification.
 *
 * Daily export needs no compression CPU: every batch row already carries its
 * JSONL line as one gzip member (`events_gz`, base64), and concatenated gzip
 * members are a valid gzip stream (zcat, Python's gzip and node's zlib read
 * them as one; note workerd's DecompressionStream stops after the first). A segment is a memcpy of ≤ EXPORT_SEGMENT_BYTES
 * plus one SHA-256. Row order (install_id, id) and segment boundaries are
 * deterministic, so a re-put after a lost D1 update is byte-identical.
 *
 * Verify is read-only (HEAD per part: size and customMetadata.sha256 must
 * match the archives row). Nothing downstream of a failed verify runs for
 * that day, and purge never deletes a day whose archive is not verified.
 */
import { addDays, knob } from '../env';
import type { StepContext, StepOutcome } from './cron';
import { fromBase64Into, gzip } from './store';
import { sha256Hex } from './tokens';

export interface Part {
  key: string;
  bytes: number;
  sha256: string;
  records: number;
}

export interface ArchiveRow {
  key: string;
  kind: 'daily' | 'monthly' | 'orphan';
  period: string;
  segments: number;
  parts_json: string;
  bytes: number;
  records: number;
  sha256: string | null;
  created: number;
  verified_at: number | null;
  expires: number | null;
  deleted_at: number | null;
}

/** Decoded length of a base64 string, without decoding it. */
function base64Length(text: string): number {
  const padding = text.endsWith('==') ? 2 : text.endsWith('=') ? 1 : 0;
  return (text.length / 4) * 3 - padding;
}

export function dailyBase(day: string): string {
  return `archives/daily/${day.slice(0, 4)}/${day.slice(5, 7)}/daily-${day}`;
}

export function monthlyBase(month: string): string {
  return `archives/monthly/monthly-${month}`;
}

/** Part 0 is `<base>.jsonl.gz`; later parts `<base>.0001.jsonl.gz` and so on. */
export function partKey(base: string, part: number): string {
  return part === 0 ? `${base}.jsonl.gz` : `${base}.${String(part).padStart(4, '0')}.jsonl.gz`;
}

export function dailyKey(day: string): string {
  return partKey(dailyBase(day), 0);
}

/** SUM(bytes) of undeleted archives: the R2 storage figure every cap uses. */
export async function r2Bytes(ctx: Pick<StepContext, 'meter'>): Promise<number> {
  const row = await ctx.meter.first<{ total: number | null }>(
    ctx.meter.prepare('SELECT SUM(bytes) AS total FROM archives WHERE deleted_at IS NULL'),
  );
  return Number(row?.total ?? 0);
}

interface ExportCursor {
  install: string;
  id: string;
  part: number;
}

function parseCursor(raw: string | null): ExportCursor {
  if (!raw) return { install: '', id: '', part: 0 };
  try {
    const value = JSON.parse(raw) as ExportCursor;
    return { install: String(value.install ?? ''), id: String(value.id ?? ''), part: Number(value.part ?? 0) };
  } catch {
    return { install: '', id: '', part: 0 };
  }
}

async function putPart(ctx: StepContext, key: string, bytes: Uint8Array, meta: Record<string, string>): Promise<string> {
  const sha = await sha256Hex(bytes);
  await ctx.env.ARCHIVES.put(key, bytes, {
    httpMetadata: { contentType: 'application/x-ndjson', contentEncoding: 'gzip' },
    customMetadata: { ...meta, sha256: sha },
  });
  ctx.meter.r2Puts += 1;
  ctx.meter.r2Bytes += bytes.byteLength;
  return sha;
}

/**
 * Upserts the archives row for part `part` in the same db.batch as the cursor.
 * Part 0 replaces the row (a re-export starts clean); later parts append.
 */
function archiveStatements(
  ctx: StepContext,
  key: string,
  kind: 'daily' | 'monthly',
  period: string,
  partIndex: number,
  part: Part,
  expires: number,
): D1PreparedStatement {
  if (partIndex === 0) {
    return ctx.meter
      .prepare(
        `INSERT INTO archives (key, kind, period, segments, parts_json, bytes, records, sha256, created, verified_at, expires, deleted_at)
         VALUES (?1, ?2, ?3, 1, ?4, ?5, ?6, ?7, ?8, NULL, ?9, NULL)
         ON CONFLICT(key) DO UPDATE SET segments = 1, parts_json = excluded.parts_json, bytes = excluded.bytes,
           records = excluded.records, sha256 = excluded.sha256, created = excluded.created,
           verified_at = NULL, expires = excluded.expires, deleted_at = NULL`,
      )
      .bind(key, kind, period, JSON.stringify([part]), part.bytes, part.records, part.sha256, ctx.now, expires);
  }
  return ctx.meter
    .prepare(
      `UPDATE archives SET segments = segments + 1, parts_json = json_insert(parts_json, '$[#]', json(?2)),
         bytes = bytes + ?3, records = records + ?4, sha256 = NULL WHERE key = ?1`,
    )
    .bind(key, JSON.stringify(part), part.bytes, part.records);
}

/** One segment of a day's export. */
export async function exportDaySlice(ctx: StepContext): Promise<StepOutcome> {
  const day = ctx.row.day;
  if ((await r2Bytes(ctx)) >= knob(ctx.env, 'R2_HARD_BYTES')) {
    return { state: 'skipped', error: 'R2 at HARD: export skipped' };
  }
  const cursor = parseCursor(ctx.row.cursor);
  const segmentCap = knob(ctx.env, 'EXPORT_SEGMENT_BYTES');
  const pageRows = knob(ctx.env, 'EXPORT_PAGE_ROWS');

  // Base64 text per row; decoded once, straight into the segment buffer.
  const texts: string[] = [];
  let bytes = 0;
  let records = 0;
  let exhausted = false;
  let last = { install: cursor.install, id: cursor.id };
  pages: for (;;) {
    const rows = await ctx.meter.all<{ install_id: string; id: string; events_gz: string }>(
      ctx.meter
        .prepare(
          `SELECT install_id, id, events_gz FROM batches WHERE day = ?1 AND (install_id, id) > (?2, ?3)
           ORDER BY install_id, id LIMIT ?4`,
        )
        .bind(day, last.install, last.id, pageRows),
    );
    for (let i = 0; i < rows.length; i += 1) {
      const row = rows[i]!;
      texts.push(row.events_gz);
      bytes += base64Length(row.events_gz);
      records += 1;
      last = { install: row.install_id, id: row.id };
      if (bytes >= segmentCap) {
        exhausted = i === rows.length - 1 && rows.length < pageRows;
        break pages;
      }
    }
    if (rows.length < pageRows) {
      exhausted = true;
      break;
    }
  }
  // A cap-sized segment that happened to end on the last row: the next slice
  // will find nothing and finish the step without writing a part.
  if (records === 0) {
    if (cursor.part === 0) return { state: 'skipped', error: 'no batches for this day' };
    return { state: 'done', statements: [] };
  }

  const base = dailyBase(day);
  const key = partKey(base, cursor.part);
  const body = new Uint8Array(bytes);
  let offset = 0;
  for (const text of texts) offset += fromBase64Into(text, body, offset);
  const sha = await putPart(ctx, key, body, { day, segment: String(cursor.part), records: String(records) });
  const part: Part = { key, bytes, sha256: sha, records };
  const next: ExportCursor = { install: last.install, id: last.id, part: cursor.part + 1 };
  return {
    state: exhausted ? 'done' : 'running',
    cursor: JSON.stringify(next),
    objectKey: dailyKey(day),
    checksum: sha,
    statements: [
      archiveStatements(ctx, dailyKey(day), 'daily', day, cursor.part, part, ctx.now + knob(ctx.env, 'ARCHIVE_RETENTION_DAILY_DAYS') * 86400),
    ],
  };
}

/**
 * HEAD every part of an archive. On any mismatch the export is re-queued
 * (≤ EXPORT_MAX_ATTEMPTS) or, past that, the day's downstream steps are skipped.
 */
export async function verifyArchive(ctx: StepContext, key: string, exportStep: string): Promise<StepOutcome> {
  const archive = await ctx.meter.first<ArchiveRow>(
    ctx.meter.prepare('SELECT * FROM archives WHERE key = ?1 AND deleted_at IS NULL').bind(key),
  );
  if (!archive) return { state: 'skipped', error: 'no archive to verify' };
  const parts = JSON.parse(archive.parts_json) as Part[];
  let problem: string | null = null;
  for (const part of parts) {
    const head = await ctx.env.ARCHIVES.head(part.key);
    ctx.meter.r2Gets += 1;
    if (!head) problem = `missing ${part.key}`;
    else if (head.size !== part.bytes) problem = `size mismatch on ${part.key}`;
    else if (head.customMetadata?.sha256 !== part.sha256) problem = `checksum mismatch on ${part.key}`;
    if (problem) break;
  }
  if (!problem) {
    return {
      state: 'done',
      checksum: archive.sha256 ?? parts[0]?.sha256 ?? null,
      objectKey: key,
      statements: [ctx.meter.prepare('UPDATE archives SET verified_at = ?2 WHERE key = ?1').bind(key, ctx.now)],
    };
  }

  const exportRow = await ctx.meter.first<{ attempts: number }>(
    ctx.meter.prepare('SELECT attempts FROM maintenance_log WHERE day = ?1 AND step = ?2').bind(ctx.row.day, exportStep),
  );
  const statements = [ctx.meter.prepare('UPDATE archives SET verified_at = NULL WHERE key = ?1').bind(key)];
  if ((exportRow?.attempts ?? 0) < knob(ctx.env, 'EXPORT_MAX_ATTEMPTS')) {
    // Re-queue the export from scratch; this verify runs again after it.
    statements.push(
      ctx.meter
        .prepare(`UPDATE maintenance_log SET state = 'pending', cursor = NULL, error = ?3 WHERE day = ?1 AND step = ?2`)
        .bind(ctx.row.day, exportStep, problem),
    );
    return { state: 'pending', error: problem, statements, keepAttempts: true };
  }
  return { state: 'failed', error: problem, statements, blockDownstream: true };
}

// ---------------------------------------------------------------------------
// Monthly archive of the rollup tables
// ---------------------------------------------------------------------------

/** Rollup tables and their primary keys, for keyset pagination. */
export const MONTHLY_TABLES: { table: string; pk: string[] }[] = [
  { table: 'daily_usage', pk: ['day', 'version', 'launch_mode', 'deployment', 'os'] },
  { table: 'version_usage', pk: ['day', 'version'] },
  { table: 'plugin_usage', pk: ['day', 'version', 'plugin'] },
  { table: 'feature_usage', pk: ['day', 'version', 'plugin', 'feature'] },
  { table: 'function_usage', pk: ['day', 'version', 'source', 'fn'] },
  { table: 'capability_usage', pk: ['day', 'version', 'capability', 'owner'] },
  { table: 'capability_transitions', pk: ['day', 'version', 'from_cap', 'to_cap'] },
  { table: 'modality_usage', pk: ['day', 'version', 'modality', 'image_kind', 'run_format', 'table_kind'] },
  { table: 'data_scale_usage', pk: ['day', 'version', 'dim', 'band'] },
  { table: 'performance_rollups', pk: ['day', 'version', 'metric', 'dims_key'] },
  { table: 'error_daily', pk: ['day', 'version', 'fp'] },
  { table: 'daily_installs', pk: ['day', 'install_id'] },
];

interface MonthlyCursor {
  t: number;
  after: unknown[] | null;
  part: number;
}

/** `YYYY-MM` of the month a monthly step covers (the month containing its day). */
export function monthOf(day: string): string {
  return day.slice(0, 7);
}

export function monthRange(month: string): { from: string; to: string } {
  const from = `${month}-01`;
  const next = addDays(`${month}-28`, 7).slice(0, 7);
  return { from, to: addDays(`${next}-01`, -1) };
}

/** One part of a month's rollup export: up to MONTHLY_SLICE_ROWS rows, gzipped. */
export async function exportMonthSlice(ctx: StepContext): Promise<StepOutcome> {
  if ((await r2Bytes(ctx)) >= knob(ctx.env, 'R2_HARD_BYTES')) {
    return { state: 'skipped', error: 'R2 at HARD: monthly export skipped' };
  }
  const month = monthOf(ctx.row.day);
  const { from, to } = monthRange(month);
  let cursor: MonthlyCursor = { t: 0, after: null, part: 0 };
  if (ctx.row.cursor) {
    try {
      cursor = JSON.parse(ctx.row.cursor) as MonthlyCursor;
    } catch {
      /* start over */
    }
  }
  const limit = knob(ctx.env, 'MONTHLY_SLICE_ROWS');
  const lines: string[] = [];
  while (cursor.t < MONTHLY_TABLES.length && lines.length < limit) {
    const { table, pk } = MONTHLY_TABLES[cursor.t]!;
    const want = limit - lines.length;
    const after = cursor.after ? ` AND (${pk.join(', ')}) > (${pk.map((_, i) => `?${i + 4}`).join(', ')})` : '';
    const rows = await ctx.meter.all<Record<string, unknown>>(
      ctx.meter
        .prepare(`SELECT * FROM ${table} WHERE day >= ?1 AND day <= ?2${after} ORDER BY ${pk.join(', ')} LIMIT ?3`)
        .bind(from, to, want, ...(cursor.after ?? [])),
    );
    for (const row of rows) lines.push(JSON.stringify({ table, ...row }));
    const lastRow = rows[rows.length - 1];
    cursor =
      rows.length < want || !lastRow
        ? { t: cursor.t + 1, after: null, part: cursor.part }
        : { ...cursor, after: pk.map((column) => lastRow[column]) };
  }
  const exhausted = cursor.t >= MONTHLY_TABLES.length;
  if (lines.length === 0) {
    return cursor.part === 0 ? { state: 'skipped', error: 'no rollups for this month' } : { state: 'done', statements: [] };
  }
  const base = monthlyBase(month);
  const key = partKey(base, cursor.part);
  const body = await gzip(`${lines.join('\n')}\n`);
  const sha = await putPart(ctx, key, body, { month, segment: String(cursor.part), records: String(lines.length) });
  const part: Part = { key, bytes: body.byteLength, sha256: sha, records: lines.length };
  const archiveKey = partKey(base, 0);
  return {
    state: exhausted ? 'done' : 'running',
    cursor: JSON.stringify({ ...cursor, part: cursor.part + 1 }),
    objectKey: archiveKey,
    checksum: sha,
    statements: [
      archiveStatements(ctx, archiveKey, 'monthly', month, cursor.part, part, ctx.now + knob(ctx.env, 'ARCHIVE_RETENTION_MONTHLY_DAYS') * 86400),
    ],
  };
}
