/**
 * The client-facing routes: /v1/telemetry/{register,events,config}.
 *
 * Written to one rule: nothing a Plexora user does can be made worse by this
 * endpoint being slow, unreachable or wrong. A 202 is sent only after the
 * durable write, because the client deletes its copy on 202; every refusal
 * the client should retry (429, 503) says when.
 *
 * Checks run cheapest first, and the body is not read until the token, the
 * content type, the declared length and the budget have all said yes.
 */
import { Hono } from 'hono';

import { dayOf, knob, nowSeconds, secondsToMidnight } from '../env';
import { type AppEnv, countryOf, NO_STORE } from '../http';
import {
  addPending,
  budgetStatement,
  type BudgetState,
  configFor,
  type Counters,
  currentState,
  forceStop,
  ingestPolicy,
  isQuotaError,
  noteCounters,
  retryAfterMidnight,
} from './budget';
import { type CleanBatch, cleanBatch, cleanClient, SCHEMA_VERSION } from './schema';
import {
  BodyTooLarge,
  buildLine,
  duplicateCheck,
  gzip,
  insertBatch,
  insertedProbe,
  readBody,
  type StoredBatch,
  toBase64,
  upsertInstall,
} from './store';
import { bearer, hmacHex, mintToken, verifyToken } from './tokens';

export const telemetry = new Hono<AppEnv>();

const REGISTER_MAX_BYTES = 4096;

type Reply = Record<string, unknown>;

function reply(body: Reply, status: 200 | 202 | 400 | 401 | 403 | 413 | 415 | 429 | 503, headers: Record<string, string> = {}) {
  return Response.json(body, { status, headers: { ...NO_STORE, ...headers } });
}

async function stopped(env: AppEnv['Bindings'], now: number, version?: string | null) {
  addPending('refused_503');
  return reply(
    { error: 'Telemetry is paused for today.', config: await configFor(env, 'stop', version, now) },
    503,
    { 'Retry-After': String(retryAfterMidnight(now)) },
  );
}

// ---------------------------------------------------------------------------
// POST /v1/telemetry/register
// ---------------------------------------------------------------------------

telemetry.post('/register', async (c) => {
  const env = c.env;
  const now = nowSeconds();
  const length = Number(c.req.header('Content-Length') ?? '0');
  if (length > REGISTER_MAX_BYTES) return reply({ error: 'Registration body too large.' }, 413);

  const state = await currentState(env, now);
  if (state === 'stop') return stopped(env, now);

  let body: unknown;
  try {
    const bytes = await readBody(c.req.raw, REGISTER_MAX_BYTES, REGISTER_MAX_BYTES);
    body = JSON.parse(new TextDecoder().decode(bytes));
  } catch (error) {
    return reply({ error: error instanceof BodyTooLarge ? 'Registration body too large.' : 'Could not read the body.' }, error instanceof BodyTooLarge ? 413 : 400);
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return reply({ error: 'Expected a JSON object.' }, 400);
  const record = body as Record<string, unknown>;
  if (record.schema !== SCHEMA_VERSION) return reply({ error: `Expected schema ${SCHEMA_VERSION}.` }, 400);
  const installId = record.install_id;
  if (typeof installId !== 'string' || !/^[0-9a-f]{32}$/.test(installId)) {
    return reply({ error: 'install_id must be 32 lowercase hex characters.' }, 400);
  }
  const client = cleanClient(record.client ?? {});
  if (!client || ('install_id' in client && client.install_id !== installId)) {
    return reply({ error: 'The client block does not match the schema.' }, 400);
  }

  const day = dayOf(now);
  const country = countryOf(c.req.raw);
  // Peppered, and keyed by day: the stored hash cannot be linked across days
  // or reversed without the pepper, and the address itself is never stored.
  const address = c.req.header('CF-Connecting-IP') ?? 'none';
  const ipHash = (await hmacHex(env.IP_HASH_KEY || 'plexora-dev-only-pepper', `${day}|${address}`)).slice(0, 32);
  const limit = knob(env, 'REGISTER_LIMIT_PER_DAY');

  let quota: number;
  try {
    const db = env.TELEMETRY_DB;
    const results = await db.batch([
      db
        .prepare(
          `INSERT INTO register_quota (ip_hash, day, n) VALUES (?1, ?2, 1)
           ON CONFLICT(ip_hash, day) DO UPDATE SET n = n + 1 RETURNING n`,
        )
        .bind(ipHash, day),
      upsertInstall(db, installId, client, country, now, '(SELECT n FROM register_quota WHERE ip_hash = ?1 AND day = ?2) <= ?3', [
        ipHash,
        day,
        limit,
      ]),
      budgetStatement(env, day, { register_requests: 1, d1_rows_written: 3 }),
    ]);
    for (const result of results) addPending('d1_rows_read', result.meta.rows_read ?? 0);
    quota = Number((results[0]?.results?.[0] as { n?: number } | undefined)?.n ?? 0);
    noteCounters(env, day, results[2]?.results?.[0] as Counters | undefined, now);
  } catch (error) {
    if (isQuotaError(error)) {
      forceStop(now);
      return stopped(env, now);
    }
    return reply({ error: 'Could not register right now.', config: await configFor(env, state, null, now) }, 503, {
      'Retry-After': '900',
    });
  }

  const config = await configFor(env, state, (client as { plexora_version?: string }).plexora_version, now);
  if (quota > limit) {
    return reply({ error: 'Too many registrations from this address today.', config }, 429, {
      'Retry-After': String(secondsToMidnight(now)),
    });
  }
  const minted = await mintToken(env, installId, now, knob(env, 'INSTALL_TOKEN_TTL_DAYS') * 86400);
  return reply({ schema: SCHEMA_VERSION, install_token: minted.token, expires: minted.expires, config }, 200);
});

// ---------------------------------------------------------------------------
// POST /v1/telemetry/events
// ---------------------------------------------------------------------------

telemetry.post('/events', async (c) => {
  const env = c.env;
  const now = nowSeconds();

  const token = await verifyToken(env, bearer(c.req.header('Authorization')), now);
  if (!token) return reply({ error: 'A valid install token is required.' }, 401);

  const type = (c.req.header('Content-Type') ?? '').toLowerCase();
  if (!type.startsWith('application/json')) return reply({ error: 'Content-Type must be application/json.' }, 415);

  const maxCompressed = knob(env, 'MAX_COMPRESSED_BYTES');
  const declared = Number(c.req.header('Content-Length') ?? '0');
  if (Number.isFinite(declared) && declared > maxCompressed) return reply({ error: 'Batch too large.' }, 413);

  const state = await currentState(env, now);
  // Body deliberately unread: at stop, even the gunzip is work we do not do.
  if (state === 'stop') return stopped(env, now);

  let parsed: unknown;
  let bytesIn = 0;
  try {
    const bytes = await readBody(c.req.raw, maxCompressed, knob(env, 'MAX_DECOMPRESSED_BYTES'));
    bytesIn = bytes.byteLength;
    parsed = JSON.parse(new TextDecoder().decode(bytes));
  } catch (error) {
    if (error instanceof BodyTooLarge) return reply({ error: 'Batch too large.' }, 413);
    // Includes the RangeError JSON.parse throws on absurd nesting.
    return reply({ error: 'Could not read the batch.' }, 400);
  }

  const policy = ingestPolicy(state);
  const cleaned = cleanBatch(parsed, { ...policy, maxEvents: knob(env, 'MAX_EVENTS') });
  if (!cleaned.ok) return reply({ error: cleaned.error }, 400);
  const batch = cleaned.batch;
  // The token names the installation; a batch claiming another is not stored
  // under a name it cannot prove.
  if (batch.client.install_id !== token.payload.tid) {
    return reply({ error: 'The batch does not belong to this install token.' }, 403);
  }

  const version = batch.client.plexora_version ?? null;
  const day = dayOf(now);
  const stored = buildLine(batch, day, now, countryOf(c.req.raw));
  if (stored.line.length > knob(env, 'MAX_CLEAN_BYTES')) return reply({ error: 'Batch too large once cleaned.' }, 413);

  const base = {
    schema: SCHEMA_VERSION,
    rejected: batch.rejected,
    dropped: batch.dropped,
    rotate: token.rotate,
  };

  const db = env.TELEMETRY_DB;
  if (batch.events.length === 0) {
    // Nothing survived cleaning: nothing to store, and the client may delete it.
    addPending('bytes_in', bytesIn);
    addPending('dropped_events', batch.dropped);
    return reply({ ...base, accepted: 0, duplicate: false, config: await configFor(env, state, version, now) }, 202);
  }

  const gz = toBase64(await gzip(`${stored.line}\n`));
  const statements = ingestStatements(env, batch, stored, gz, bytesIn);

  let inserted: boolean;
  try {
    const results = await db.batch(statements);
    for (const result of results) addPending('d1_rows_read', result.meta.rows_read ?? 0);
    inserted = (results[0]?.meta.changes ?? 0) > 0;
    noteCounters(env, day, results[results.length - 1]?.results?.[0] as Counters | undefined, now);
  } catch (error) {
    return d1Failure(env, error, now, version);
  }

  const config = await configFor(env, state, version, now);
  if (inserted) return reply({ ...base, accepted: batch.events.length, duplicate: false, config }, 202);

  // Rare: one point lookup to tell a retry from a flood.
  let duplicate = false;
  try {
    const hit = await duplicateCheck(db, batch, day, knob(env, 'DEDUPE_DAYS')).all();
    addPending('d1_rows_read', hit.meta.rows_read ?? 0);
    duplicate = hit.results.length > 0;
  } catch {
    duplicate = false;
  }
  if (duplicate) {
    addPending('duplicates');
    return reply({ ...base, accepted: 0, duplicate: true, config }, 202);
  }
  addPending('rate_limited');
  return reply({ error: 'Too many uploads from this install.', config }, 429, { 'Retry-After': '3600' });
});

/**
 * The one db.batch() of an ingest: S1 batches insert, S2 installs upsert (only
 * with a session.summary), S3 budget upsert. Exported so a test can run these
 * exact statements and sum their `meta.rows_written`.
 */
export function ingestStatements(
  env: AppEnv['Bindings'],
  batch: CleanBatch,
  stored: StoredBatch,
  gz: string,
  bytesIn: number,
): D1PreparedStatement[] {
  const db = env.TELEMETRY_DB;
  const probe = insertedProbe(batch, stored);
  const statements = [
    insertBatch(db, batch, stored, gz, {
      dedupeDays: knob(env, 'DEDUPE_DAYS'),
      perHour: knob(env, 'EVENTS_PER_INSTALL_PER_HOUR'),
      perDay: knob(env, 'EVENTS_PER_INSTALL_PER_DAY'),
    }),
  ];
  if (stored.hasSession) {
    statements.push(
      upsertInstall(db, batch.client.install_id, batch.client, stored.country, stored.received, `EXISTS (${probe.sql})`, probe.binds),
    );
  }
  // Writes: S3's own row always; S1 (and S2) only when the insert landed.
  statements.push(
    budgetStatement(env, stored.day, { bytes_in: bytesIn, dropped_events: batch.dropped, d1_rows_written: 1 }, undefined, {
      ...probe,
      perInsert: { batches: 1, events: stored.events, d1_rows_written: stored.hasSession ? 2 : 1 },
    }),
  );
  return statements;
}

async function d1Failure(env: AppEnv['Bindings'], error: unknown, now: number, version: string | null) {
  if (isQuotaError(error)) {
    forceStop(now);
    return stopped(env, now, version);
  }
  const state: BudgetState = 'ok';
  return reply({ error: 'Could not store the batch.', config: await configFor(env, state, version, now) }, 503, {
    'Retry-After': '900',
  });
}

// ---------------------------------------------------------------------------
// GET /v1/telemetry/config
// ---------------------------------------------------------------------------

telemetry.get('/config', async (c) => {
  const now = nowSeconds();
  const token = await verifyToken(c.env, bearer(c.req.header('Authorization')), now);
  if (!token) return reply({ error: 'A valid install token is required.' }, 401);
  const version = c.req.query('version');
  const safeVersion = version && /^[0-9A-Za-z.+-]{1,48}$/.test(version) ? version : null;
  const state = await currentState(c.env, now);
  return reply({ schema: SCHEMA_VERSION, config: await configFor(c.env, state, safeVersion, now) }, 200);
});
