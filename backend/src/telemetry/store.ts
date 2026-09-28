/**
 * Body reading, gzip, and the SQL of an ingest: the batches insert (S1) and
 * the installs upsert (S2). The budget upsert (S3) lives in budget.ts.
 */
import { addDays } from '../env';
import { type CleanBatch, type CleanClient, errorCount, priorityOf } from './schema';

export class BodyTooLarge extends Error {}
export class BodyUnreadable extends Error {}

/**
 * The request body, gunzipped when it says gzip, with both caps enforced
 * *while streaming*: a 64 KB request that inflates to a gigabyte is cancelled
 * at the first chunk past the decompressed cap, never buffered.
 */
export async function readBody(
  request: Request,
  maxCompressed: number,
  maxDecompressed: number,
): Promise<Uint8Array> {
  const body = request.body;
  if (!body) return new Uint8Array();
  let compressed = 0;
  const counter = new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      compressed += chunk.byteLength;
      if (compressed > maxCompressed) controller.error(new BodyTooLarge('compressed'));
      else controller.enqueue(chunk);
    },
  });
  const encoding = (request.headers.get('Content-Encoding') ?? 'identity').toLowerCase().trim();
  let stream: ReadableStream<Uint8Array> = body.pipeThrough(counter);
  if (encoding === 'gzip') stream = stream.pipeThrough(new DecompressionStream('gzip'));
  else if (encoding !== 'identity' && encoding !== '') throw new BodyUnreadable('encoding');

  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxDecompressed) {
        await reader.cancel().catch(() => undefined);
        throw new BodyTooLarge('decompressed');
      }
      chunks.push(value);
    }
  } catch (error) {
    if (error instanceof BodyTooLarge) throw error;
    throw new BodyUnreadable(String(error));
  }
  return concat(chunks, total);
}

export function concat(chunks: Uint8Array[], total?: number): Uint8Array {
  const size = total ?? chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
  const out = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}

async function drain(stream: ReadableStream<Uint8Array>): Promise<Uint8Array> {
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** One gzip member. Native CompressionStream, ~1 ms for a 3 KB line. */
export async function gzip(data: Uint8Array | string): Promise<Uint8Array> {
  const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data;
  return drain(new Blob([bytes]).stream().pipeThrough(new CompressionStream('gzip')));
}

/** Base64 of bytes, chunked so a large array never blows the argument limit. */
export function toBase64(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

/** Decodes base64 into `out` at `offset`; returns the bytes written. */
export function fromBase64Into(text: string, out: Uint8Array, offset: number): number {
  const binary = atob(text);
  for (let i = 0; i < binary.length; i += 1) out[offset + i] = binary.charCodeAt(i);
  return binary.length;
}

export function fromBase64(text: string): Uint8Array {
  const out = new Uint8Array(Math.floor((text.length * 3) / 4));
  return out.subarray(0, fromBase64Into(text, out, 0));
}

export async function gunzip(data: Uint8Array | ArrayBuffer): Promise<Uint8Array> {
  return drain(new Blob([data]).stream().pipeThrough(new DecompressionStream('gzip')));
}

// ---------------------------------------------------------------------------
// The stored line
// ---------------------------------------------------------------------------

export interface StoredBatch {
  day: string;
  received: number;
  country: string | null;
  line: string;
  eventsJson: string;
  events: number;
  errors: number;
  prioMax: number;
  hasSession: boolean;
}

/**
 * The JSONL line archived for this upload. Built only from cleaned values, so
 * nothing the vectors do not allow can reach D1 or R2.
 */
export function buildLine(batch: CleanBatch, day: string, received: number, country: string | null): StoredBatch {
  const c = batch.client;
  const eventsJson = JSON.stringify(batch.events);
  const prioMax = batch.events.reduce((max, event) => Math.max(max, priorityOf(event.type)), 0);
  const hasSession = batch.events.some((event) => event.type === 'session.summary');
  const line = JSON.stringify({
    schema: batch.schema,
    day,
    received,
    install_id: c.install_id,
    session_id: c.session_id ?? null,
    id: batch.batch_id,
    version: c.plexora_version ?? null,
    mode: c.mode ?? null,
    launch_mode: c.launch_mode ?? null,
    deployment: c.deployment ?? null,
    os: c.os ?? null,
    country,
    priority_max: prioMax,
    client: c,
    events: batch.events,
  });
  return {
    day,
    received,
    country,
    line,
    eventsJson,
    events: batch.events.length,
    errors: errorCount(batch.events),
    prioMax,
    hasSession,
  };
}

/** The days a batch id is deduplicated over: today and DEDUPE_DAYS before it. */
export function dedupeDays(day: string, dedupeDays: number): string[] {
  const days: string[] = [];
  for (let back = 0; back <= dedupeDays; back += 1) days.push(addDays(day, -back));
  return days;
}

/**
 * S1: the batch row, inserted only when the id is new over the dedupe window
 * and the install is under its hourly and daily caps. `meta.changes === 0`
 * means duplicate or rate-limited; the caller tells them apart.
 */
export function insertBatch(
  db: D1Database,
  batch: CleanBatch,
  stored: StoredBatch,
  gz: string,
  limits: { dedupeDays: number; perHour: number; perDay: number },
): D1PreparedStatement {
  const c = batch.client;
  const days = dedupeDays(stored.day, limits.dedupeDays);
  const binds: unknown[] = [
    stored.day,
    c.install_id,
    batch.batch_id,
    stored.received,
    c.session_id ?? null,
    c.plexora_version ?? null,
    c.python ?? null,
    c.os ?? null,
    c.arch ?? null,
    c.launch_mode ?? null,
    c.deployment ?? null,
    c.scheduler ?? null,
    c.install_kind ?? null,
    c.mode ?? null,
    stored.country,
    stored.events,
    stored.errors,
    stored.line.length,
    stored.prioMax,
    stored.hasSession ? 1 : 0,
    stored.eventsJson,
    gz,
    addDays(stored.day, -1),
    limits.perHour,
    limits.perDay,
  ];
  const dayList = days.map((d) => {
    binds.push(d);
    return `?${binds.length}`;
  });
  const values = Array.from({ length: 22 }, (_, i) => `?${i + 1}`).join(', ');
  return db
    .prepare(
      `INSERT INTO batches (day, install_id, id, received, session_id, version, python, os, arch,
         launch_mode, deployment, scheduler, install_kind, mode, country, events, errors, bytes,
         priority_max, has_session, events_json, events_gz)
       SELECT ${values}
       WHERE NOT EXISTS (SELECT 1 FROM batches WHERE day IN (${dayList.join(', ')}) AND install_id = ?2 AND id = ?3)
         AND (SELECT COUNT(*) FROM batches WHERE day IN (?1, ?23) AND install_id = ?2 AND received > ?4 - 3600) < ?24
         AND (SELECT COUNT(*) FROM batches WHERE day = ?1 AND install_id = ?2) < ?25`,
    )
    .bind(...binds);
}

/** The probe budget.ts uses to see whether S1 landed, in the same db.batch(). */
export function insertedProbe(batch: CleanBatch, stored: StoredBatch) {
  return {
    sql: 'SELECT COUNT(*) AS n FROM batches WHERE day = ?1 AND install_id = ?2 AND id = ?3 AND received = ?4',
    binds: [stored.day, batch.client.install_id, batch.batch_id, stored.received],
  };
}

/** A duplicate is a batch id already stored over the window; anything else is the rate limit. */
export function duplicateCheck(db: D1Database, batch: CleanBatch, day: string, window: number) {
  const days = dedupeDays(day, window);
  return db
    .prepare(
      `SELECT 1 AS hit FROM batches WHERE day IN (${days.map((_, i) => `?${i + 3}`).join(', ')})
         AND install_id = ?1 AND id = ?2 LIMIT 1`,
    )
    .bind(batch.client.install_id, batch.batch_id, ...days);
}

const INSTALL_COLUMNS = [
  'version',
  'python',
  'os',
  'arch',
  'launch_mode',
  'deployment',
  'scheduler',
  'install_kind',
  'mode',
  'country',
  // The plan tier (free/paid/trial) -- the reserved installs column, now
  // filled. A word, never who holds the licence.
  'license_tier',
] as const;

function installValues(c: Partial<CleanClient>, country: string | null): (string | null)[] {
  return [
    c.plexora_version ?? null,
    c.python ?? null,
    c.os ?? null,
    c.arch ?? null,
    c.launch_mode ?? null,
    c.deployment ?? null,
    c.scheduler ?? null,
    c.install_kind ?? null,
    c.mode ?? null,
    country,
    typeof c.license_tier === 'string' ? c.license_tier : null,
  ];
}

const INSTALL_UPDATE = INSTALL_COLUMNS.map((col) => `${col} = COALESCE(excluded.${col}, installs.${col})`).join(', ');

/**
 * The installs upsert, guarded by `whereSql` (S2 only runs when S1 landed;
 * /register only when the address is under its quota). First_seen is kept.
 */
export function upsertInstall(
  db: D1Database,
  installId: string,
  client: Partial<CleanClient>,
  country: string | null,
  now: number,
  whereSql: string,
  whereBinds: unknown[],
): D1PreparedStatement {
  const offset = 3 + INSTALL_COLUMNS.length;
  const where = whereSql.replace(/\?(\d+)/g, (_, n: string) => `?${Number(n) + offset}`);
  return db
    .prepare(
      `INSERT INTO installs (install_id, first_seen, last_seen, ${INSTALL_COLUMNS.join(', ')})
       SELECT ?1, ?2, ?3, ${INSTALL_COLUMNS.map((_, i) => `?${i + 4}`).join(', ')}
       WHERE ${where}
       ON CONFLICT(install_id) DO UPDATE SET last_seen = excluded.last_seen, ${INSTALL_UPDATE}`,
    )
    .bind(installId, now, now, ...installValues(client, country), ...whereBinds);
}
