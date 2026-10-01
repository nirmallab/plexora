/**
 * /v1/ai: the Plexora AI gateway, and /admin/api/ai: its tracking.
 *
 *   POST /v1/ai/token              environment certificate -> PLXAI1 token (30 min)
 *   POST /v1/ai/messages           one streamed model call, metered and billed
 *   POST /v1/ai/runs               declare a bounded session: quote, hold, envelope
 *   POST /v1/ai/runs/:id/finish    close it; the unspent hold comes back
 *   GET  /v1/ai/runs/:id           a run's totals
 *   GET  /v1/ai/balance            the account's credit
 *   GET  /v1/ai/usage              the account's usage, by day and feature
 *   GET  /v1/ai/requests/:id       one call's metadata
 *   GET  /v1/ai/pricing            the public price table
 *
 *   POST /v1/ai/dev/messages       DEV ROUTE: internal testing, metered at
 *   POST /v1/ai/dev/runs           provider cost (no markup), may name a model;
 *                                  only for accounts an admin put in dev mode
 *
 * The client names a capability class, never a model (except on the dev
 * route), and every field outside a short allowlist is refused, so this is a
 * Plexora feature backend rather than a general model proxy. Billing uses the
 * PROVIDER's usage; a client's numbers are never trusted. Nothing the model
 * saw or said is written anywhere: one metadata row per call.
 */
import { type Context as HonoContext, Hono } from 'hono';

import { ipHash, newId, sha256Hex } from '../crypto';
import { accountById, all, isUniqueViolation, licenseById, one, seatById, seatEntitlements } from '../db';
import { DAY, HOUR, knob, nowSeconds } from '../env';
import { record } from '../events';
import { ApiError, type AppEnv, int, ok, readJson, str } from '../http';
import { licenseProblem, seatProblem } from '../licensing';
import { enforce } from '../ratelimit';
import {
  type Capability, CAPABILITIES, capabilitiesFor, COSTS, costMicro, estimateMicro, FEATURES, ROUTES, withMarkup,
} from '../ai/catalog';
import {
  aiAccount, balance, balanceView, claimRunCall, credit, finishRun, markupFor, prepare, releaseHold, reserve, runById,
  type RunRow, settleCall, settleRunCall,
} from '../ai/ledger';
import { type Billing, type GatewayClaims, issueToken, verifyBearer } from '../ai/token';
import { anthropicBody, callAnthropic, classify, type Envelope, sse, UsageMeter, userHash } from '../ai/upstream';
import { presented } from './v1';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Ctx = HonoContext<AppEnv, any>;

export const ai = new Hono<AppEnv>();

ai.use('*', async (c, next) => {
  c.set('ipHash', await ipHash(c.env, c.req.raw));
  await next();
});

const NAME = /^[a-z][a-z0-9_]{0,31}$/;
const SESSION = /^[A-Za-z0-9_.:-]{1,64}$/;
const IDEMPOTENCY = /^[A-Za-z0-9_.:-]{8,128}$/;
const DEV_MARKUP = 10_000;

// -- token -------------------------------------------------------------------------

ai.post('/token', async (c) => {
  const now = nowSeconds();
  const body = await readJson(c);
  const { payload, environment, bindingOk } = await presented(c, body, c.get('ipHash'), now);
  if (!environment || environment.status !== 'active') {
    throw new ApiError(403, 'environment_unknown', 'That environment is not registered.');
  }
  if (!bindingOk) throw new ApiError(403, 'environment_mismatch', 'That certificate belongs to a different environment.');
  await enforce(c.env, 'ai-token-env', environment.id, knob(c.env, 'AI_TOKENS_PER_ENV_PER_HOUR'), HOUR, now);
  const [license, seat] = await Promise.all([licenseById(c.env, environment.license_id),
    seatById(c.env, environment.seat_id)]);
  const problem = licenseProblem(license, now) ?? seatProblem(seat);
  if (problem) throw problem;
  const caps = capabilitiesFor(seatEntitlements(license!, seat!));
  if (!caps.length) throw new ApiError(403, 'ai_not_entitled', 'This licence does not include Plexora AI.');
  const account = await aiAccount(c.env, license!.account_id);
  if (account?.mode === 'disabled') throw new ApiError(403, 'ai_disabled', 'Plexora AI is turned off for this account.');
  const appVersion = typeof body.app_version === 'string' ? body.app_version.slice(0, 24) : '';
  const issued = await issueToken(c.env, {
    acc: license!.account_id, usr: seat!.user_id, lic: license!.id, seat: seat!.id, env: environment.id,
    envt: payload.environment_type, caps, mode: account?.mode === 'dev' ? 'dev' : 'credits', ver: appVersion,
  }, now);
  return ok(c, { token: issued.token, expires_at: issued.claims.exp, mode: issued.claims.mode, capabilities: caps,
    server_time: now });
});

async function bearer(c: { env: AppEnv['Bindings']; req: { header(name: string): string | undefined } },
  now: number): Promise<GatewayClaims> {
  return verifyBearer(c.env, c.req.header('Authorization'), now);
}

// -- validation ----------------------------------------------------------------------

interface Context {
  feature: string | null;
  agent: string | null;
  workflow: string | null;
  session_id: string | null;
  run_id: string | null;
  attempt: number;
}

interface Validated {
  capability: Capability;
  context: Context;
  envelope: Envelope;
  model: string | null;
  textChars: number;
  images: number;
}

const BLOCK_TYPES = new Set(['text', 'image', 'tool_use', 'tool_result']);
const REQUEST_KEYS = new Set(['system', 'messages', 'tools', 'max_tokens', 'stop_sequences', 'output_schema']);
const TOP_KEYS = new Set(['capability', 'context', 'request', 'model']);

function bad(message: string): never {
  throw new ApiError(400, 'invalid_request', message);
}

/** Walk content blocks: count images and text, refuse unknown block types. */
function measure(content: unknown, tally: { chars: number; images: number }, depth = 0): void {
  if (depth > 3) bad('Content is nested too deeply.');
  if (typeof content === 'string') {
    tally.chars += content.length;
    return;
  }
  if (!Array.isArray(content)) bad('Content must be a string or a list of blocks.');
  for (const block of content) {
    if (!block || typeof block !== 'object') bad('Each content block must be an object.');
    const type = (block as Record<string, unknown>).type;
    if (typeof type !== 'string' || !BLOCK_TYPES.has(type)) bad(`Content block type ${String(type)} is not accepted.`);
    const b = block as Record<string, any>;
    if (type === 'text') tally.chars += typeof b.text === 'string' ? b.text.length : 0;
    else if (type === 'image') tally.images += 1;
    else if (type === 'tool_use') tally.chars += JSON.stringify(b.input ?? {}).length;
    else if (type === 'tool_result' && b.content !== undefined) measure(b.content, tally, depth + 1);
  }
}

function validate(body: Record<string, unknown>, dev: boolean, maxImages: number): Validated {
  for (const key of Object.keys(body)) if (!TOP_KEYS.has(key)) bad(`Unknown field \`${key}\`.`);
  const capability = body.capability as Capability;
  if (!(CAPABILITIES as readonly string[]).includes(String(capability))) bad('Name a known `capability`.');
  let model: string | null = null;
  if (body.model !== undefined) {
    if (!dev) bad('`model` is accepted only on the dev route; name a `capability`.');
    if (typeof body.model !== 'string' || !COSTS[body.model]) bad(`Unknown model; known: ${Object.keys(COSTS).join(', ')}.`);
    model = body.model;
  }
  const rawContext = (body.context && typeof body.context === 'object' ? body.context : {}) as Record<string, unknown>;
  const pick = (name: string, pattern: RegExp) => {
    const value = rawContext[name];
    if (value === undefined || value === null) return null;
    if (typeof value !== 'string' || !pattern.test(value)) bad(`context.${name} is not valid.`);
    return value;
  };
  const context: Context = {
    feature: pick('feature', NAME), agent: pick('agent', NAME), workflow: pick('workflow', NAME),
    session_id: pick('session_id', SESSION), run_id: pick('run_id', SESSION),
    attempt: typeof rawContext.attempt === 'number' && Number.isInteger(rawContext.attempt) ?
      Math.max(1, Math.min(rawContext.attempt, 99)) : 1,
  };
  const request = body.request as Record<string, unknown>;
  if (!request || typeof request !== 'object' || Array.isArray(request)) bad('Send the model input as `request`.');
  for (const key of Object.keys(request)) {
    if (!REQUEST_KEYS.has(key)) bad(`\`request.${key}\` is not accepted (model, metadata, thinking and sampling are set by Plexora).`);
  }
  const maxTokens = request.max_tokens;
  if (typeof maxTokens !== 'number' || !Number.isInteger(maxTokens) || maxTokens < 1) bad('`request.max_tokens` must be a positive integer.');
  if (!Array.isArray(request.messages) || !request.messages.length) bad('`request.messages` must be a non-empty list.');
  const tally = { chars: 0, images: 0 };
  if (request.system !== undefined) measure(request.system, tally);
  for (const message of request.messages) {
    if (!message || typeof message !== 'object') bad('Each message must be an object.');
    const m = message as Record<string, unknown>;
    if (m.role !== 'user' && m.role !== 'assistant') bad('A message role is user or assistant.');
    for (const key of Object.keys(m)) if (key !== 'role' && key !== 'content') bad(`Unknown message field \`${key}\`.`);
    measure(m.content, tally);
  }
  if (tally.images > maxImages) bad(`At most ${maxImages} images per request.`);
  if (request.tools !== undefined) {
    if (!Array.isArray(request.tools) || request.tools.length > 64) bad('`request.tools` must be a list of at most 64.');
    tally.chars += JSON.stringify(request.tools).length;
  }
  if (request.output_schema !== undefined &&
      (!request.output_schema || typeof request.output_schema !== 'object' || Array.isArray(request.output_schema))) {
    bad('`request.output_schema` must be a JSON schema object.');
  }
  if (request.stop_sequences !== undefined && (!Array.isArray(request.stop_sequences) ||
      request.stop_sequences.length > 4 || !request.stop_sequences.every((s) => typeof s === 'string'))) {
    bad('`request.stop_sequences` must be at most four strings.');
  }
  return {
    capability, context, model, textChars: tally.chars, images: tally.images,
    envelope: {
      system: request.system, messages: request.messages, tools: request.tools as unknown[] | undefined,
      max_tokens: maxTokens, stop_sequences: request.stop_sequences as string[] | undefined,
      output_schema: request.output_schema as Record<string, unknown> | undefined,
    },
  };
}

// -- messages ------------------------------------------------------------------------

async function messages(c: Ctx, dev: boolean) {
  const now = nowSeconds();
  const startedMs = Date.now();
  const env = c.env;
  const claims = await bearer(c, now);
  if (dev && claims.mode !== 'dev') {
    throw new ApiError(403, 'dev_not_allowed', 'The dev route is for internal testing accounts only.');
  }
  await enforce(env, 'ai-calls', claims.jti, knob(env, 'AI_CALLS_PER_MIN'), 60, now);
  const key = c.req.header('Idempotency-Key') ?? '';
  if (!IDEMPOTENCY.test(key)) bad('Send an `Idempotency-Key` header (8-128 of A-Z a-z 0-9 _ . : -).');
  const maxBytes = knob(env, 'AI_MAX_BODY_BYTES');
  const declared = Number(c.req.header('Content-Length') ?? '0');
  if (declared > maxBytes) throw new ApiError(413, 'request_too_large', 'That request is too large.');
  const text = await c.req.text();
  if (text.length > maxBytes) throw new ApiError(413, 'request_too_large', 'That request is too large.');
  let body: Record<string, unknown>;
  try {
    body = JSON.parse(text);
  } catch {
    bad('Send a JSON object.');
  }
  if (!body || typeof body !== 'object' || Array.isArray(body)) bad('Send a JSON object.');
  const v = validate(body, dev, knob(env, 'AI_MAX_IMAGES'));
  if (!claims.caps.includes(v.capability)) {
    throw new ApiError(403, 'capability_not_allowed', 'This licence does not include that capability.');
  }
  const account = await aiAccount(env, claims.acc);
  if (account?.mode === 'disabled') throw new ApiError(403, 'ai_disabled', 'Plexora AI is turned off for this account.');
  if (dev && account?.mode !== 'dev') {
    throw new ApiError(403, 'dev_not_allowed', 'This account is no longer in dev mode.');
  }
  const billing: Billing = dev ? 'dev' : 'credits';
  const route = ROUTES[v.capability];
  const model = v.model ?? route.model;
  const unit = COSTS[model]!;
  const markup = dev ? DEV_MARKUP : markupFor(env, account);

  const keyHash = await sha256Hex(key);
  const requestId = newId('req');
  try {
    await env.LICENSE_DB.prepare(
      `INSERT INTO ai_idempotency (account_id, key_hash, request_id, state, created_at) VALUES (?1, ?2, ?3, 'open', ?4)`,
    ).bind(claims.acc, keyHash, requestId, now).run();
  } catch (error) {
    if (!isUniqueViolation(error)) throw error;
    const seen = await one<{ request_id: string; state: string; price_micro: number | null }>(env,
      'SELECT request_id, state, price_micro FROM ai_idempotency WHERE account_id = ?1 AND key_hash = ?2',
      claims.acc, keyHash);
    if (seen?.state === 'open') {
      throw new ApiError(409, 'idempotency_in_progress', 'A call with that key is still running.',
        { details: { gateway_request_id: seen.request_id } });
    }
    throw new ApiError(409, 'idempotency_conflict', 'A call with that key already happened.',
      { details: { gateway_request_id: seen?.request_id, state: seen?.state, price_micro: seen?.price_micro } });
  }
  const forget = () => env.LICENSE_DB.prepare('DELETE FROM ai_idempotency WHERE account_id = ?1 AND key_hash = ?2')
    .bind(claims.acc, keyHash).run();

  // Money first: a run's call counts against its envelope; any other call holds its estimate.
  let run: RunRow | null = null;
  let holdId: string | null = null;
  let holdMicro = 0;
  try {
    await prepare(env, claims.acc, account, now);
    if (v.context.run_id) {
      run = await runById(env, v.context.run_id);
      if (!run || run.account_id !== claims.acc) throw new ApiError(404, 'not_found', 'No such run.');
      if (run.billing !== billing) bad(`That run is billed as ${run.billing}; call the matching route.`);
      if (run.status !== 'open' || run.expires_at <= now) throw new ApiError(409, 'run_closed', 'That run is closed.');
      if (!(await claimRunCall(env, run.id, claims.acc, now))) {
        throw new ApiError(402, 'run_envelope_exceeded', 'This run has used every call its quote pays for.',
          { details: { run_id: run.id, envelope_calls: run.envelope_calls } });
      }
    } else {
      holdMicro = Math.min(knob(env, 'AI_MAX_RESERVE_MICRO'), Math.max(knob(env, 'AI_MIN_HOLD_MICRO'),
        estimateMicro(v.textChars, v.images, Math.min(v.envelope.max_tokens, route.max_tokens_cap), unit, markup)));
      holdId = await reserve(env, claims.acc, holdMicro, now, { request_id: requestId });
    }
  } catch (error) {
    await forget();
    throw error;
  }

  const user = await userHash(env, claims.acc, claims.usr);
  const row = {
    id: requestId, account_id: claims.acc, user_id: claims.usr, license_id: claims.lic, seat_id: claims.seat,
    environment_id: claims.env, token_jti: claims.jti, billing, run_id: run?.id ?? null,
    session_id: v.context.session_id, feature: v.context.feature, agent: v.context.agent,
    workflow: v.context.workflow, attempt: v.context.attempt, app_version: claims.ver, capability: v.capability,
    provider: route.provider, model, image_count: v.images, markup_bps: markup, hold_micro: holdMicro,
    request_bytes: text.length, started_at_ms: startedMs,
  };

  let upstream: Response;
  try {
    upstream = await callAnthropic(env, anthropicBody(route, model, v.envelope, user));
  } catch {
    upstream = new Response('upstream unreachable', { status: 503 });
  }
  if (!upstream.ok || !upstream.body) {
    const cls = classify(upstream.status);
    const detail = (await upstream.text().catch(() => '')).slice(0, 200).replace(/"[^"]{40,}"/g, '"…"');
    if (holdId) await releaseHold(env, claims.acc, holdId, holdMicro, now);
    if (run) await env.LICENSE_DB.prepare('UPDATE ai_runs SET calls = MAX(calls - 1, 0) WHERE id = ?1').bind(run.id).run();
    await insertRequest(env, { ...row, status: 'error', failure_class: cls.failure, http_status: upstream.status,
      usage_source: 'none', unit, usage: null, cost: 0, price: 0, charged: 0, first_byte_ms: null,
      finished_at_ms: Date.now(), stop_reason: null, provider_request_id: null });
    await forget();   // nothing happened upstream: the same key may be retried
    const retry = Number(upstream.headers.get('retry-after') ?? '');
    throw new ApiError(cls.http, cls.code, cls.code === 'provider_rejected'
      ? `The provider refused this request: ${detail}` : 'The model provider is not available right now.',
      Number.isFinite(retry) && retry > 0 ? { retry_after: Math.min(Math.ceil(retry), 120) } : { retry_after: 15 });
  }

  const firstByteMs = Date.now();
  const [toClient, toMeter] = upstream.body.tee();
  const settlement = (async () => {
    const meter = new UsageMeter();
    const reader = toMeter.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value) meter.push(value);
      }
    } catch {
      // A cut stream is settled on what was seen.
    }
    const m = meter.result();
    const cost = costMicro(m.usage, unit);
    const price = billing === 'dev' ? cost : withMarkup(cost, markup);
    const finished = nowSeconds();
    let charged = price;
    if (run) charged = await settleRunCall(env, run, price, cost, finished, requestId, claims.usr);
    else await settleCall(env, claims.acc, holdId, holdMicro, price, finished, requestId, claims.usr);
    const status = m.complete ? 'ok' : 'incomplete';
    await insertRequest(env, { ...row, status, failure_class: m.complete ? null : 'stream_cut', http_status: 200,
      usage_source: m.source, unit, usage: m.usage, cost, price, charged, first_byte_ms: firstByteMs,
      finished_at_ms: Date.now(), stop_reason: m.stop_reason, provider_request_id: m.provider_request_id });
    await env.LICENSE_DB.prepare(
      `UPDATE ai_idempotency SET state = ?3, price_micro = ?4 WHERE account_id = ?1 AND key_hash = ?2`,
    ).bind(claims.acc, keyHash, m.complete ? 'settled' : 'failed', charged).run();
    const fresh = run ? await runById(env, run.id) : null;
    return {
      gateway_request_id: requestId, status, usage_source: m.source, usage: m.usage,
      price_micro: price, charged_micro: charged, billing,
      ...(billing === 'dev' ? { cost_micro: cost, model } : {}),
      run: fresh ? runView(fresh) : null,
      balance: balanceView(await balance(env, claims.acc)),
    };
  })();
  try {
    c.executionCtx.waitUntil(settlement.catch((error) => console.error('ai settle', error)));
  } catch {
    // No execution context (unit tests): the stream below awaits it anyway.
  }

  const clientReader = toClient.getReader();
  const accepted = { gateway_request_id: requestId, provider: route.provider, model: dev ? model : undefined,
    capability: v.capability, billing, hold_micro: holdMicro, run_id: run?.id ?? null };
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(sse('plexora.accepted', accepted));
    },
    async pull(controller) {
      const { done, value } = await clientReader.read();
      if (!done) {
        if (value) controller.enqueue(value);
        return;
      }
      try {
        controller.enqueue(sse('plexora.usage', await settlement));
      } catch {
        controller.enqueue(sse('plexora.error', { gateway_request_id: requestId, code: 'internal_error',
          retryable: false }));
      }
      controller.close();
    },
    cancel(reason) {
      return clientReader.cancel(reason);
    },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Plexora-Request-Id': requestId } });
}

interface RequestRecord {
  id: string; account_id: string; user_id: string | null; license_id: string; seat_id: string;
  environment_id: string | null; token_jti: string; billing: Billing; run_id: string | null; session_id: string | null;
  feature: string | null; agent: string | null; workflow: string | null; attempt: number; app_version: string;
  capability: string; provider: string; model: string; image_count: number; markup_bps: number; hold_micro: number;
  request_bytes: number; started_at_ms: number; status: string; failure_class: string | null; http_status: number;
  usage_source: string; unit: (typeof COSTS)[string]; usage: ReturnType<UsageMeter['result']>['usage'] | null;
  cost: number; price: number; charged: number; first_byte_ms: number | null; finished_at_ms: number;
  stop_reason: string | null; provider_request_id: string | null;
}

async function insertRequest(env: AppEnv['Bindings'], r: RequestRecord): Promise<void> {
  const u = r.usage;
  await env.LICENSE_DB.prepare(
    `INSERT INTO ai_requests (id, account_id, user_id, license_id, seat_id, environment_id, token_jti, billing, run_id,
       session_id, feature, agent, workflow, attempt, app_version, capability, provider, model, provider_request_id,
       status, failure_class, http_status, stop_reason, usage_source, input_uncached, cache_read, cache_write_5m,
       cache_write_1h, output_tokens, image_count, p_in, p_cache_read, p_cache_write_5m, p_cache_write_1h, p_out,
       markup_bps, hold_micro, cost_micro, price_micro, charged_micro, request_bytes, started_at_ms, first_byte_ms,
       finished_at_ms)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23,
       ?24, ?25, ?26, ?27, ?28, ?29, ?30, ?31, ?32, ?33, ?34, ?35, ?36, ?37, ?38, ?39, ?40, ?41, ?42, ?43, ?44)`,
  ).bind(r.id, r.account_id, r.user_id, r.license_id, r.seat_id, r.environment_id, r.token_jti, r.billing, r.run_id,
    r.session_id, r.feature, r.agent, r.workflow, r.attempt, r.app_version, r.capability, r.provider, r.model,
    r.provider_request_id, r.status, r.failure_class, r.http_status, r.stop_reason, r.usage_source,
    u?.input_uncached ?? 0, u?.cache_read ?? 0, u?.cache_write_5m ?? 0, u?.cache_write_1h ?? 0, u?.output_tokens ?? 0,
    r.image_count, r.unit.in, r.unit.cache_read, r.unit.cache_write_5m, r.unit.cache_write_1h, r.unit.out,
    r.markup_bps, r.hold_micro, r.cost, r.price, r.charged, r.request_bytes, r.started_at_ms, r.first_byte_ms,
    r.finished_at_ms).run();
}

ai.post('/messages', (c) => messages(c, false));
ai.post('/dev/messages', (c) => messages(c, true));

// -- runs ------------------------------------------------------------------------------

function runView(run: RunRow) {
  return {
    run_id: run.id, feature: run.feature, unit: run.unit, units: run.units, billing: run.billing,
    session_id: run.session_id, status: run.status, quote_micro: run.quote_micro,
    quote_credits: Math.ceil(run.quote_micro / 10_000), accrued_micro: run.accrued_micro,
    charged_micro: run.charged_micro, charged_credits: Math.ceil(run.charged_micro / 10_000),
    calls: run.calls, envelope_calls: run.envelope_calls, expires_at: run.expires_at,
    ...(run.billing === 'dev' ? { cost_micro: run.cost_micro } : {}),
  };
}

const RUN_HOURS = 6;

async function startRun(c: Ctx, dev: boolean) {
  const now = nowSeconds();
  const claims = await bearer(c, now);
  if (dev && claims.mode !== 'dev') throw new ApiError(403, 'dev_not_allowed', 'The dev route is for internal testing accounts only.');
  const body = await readJson(c);
  const feature = str(body, 'feature', 32);
  const price = feature ? FEATURES[feature] : undefined;
  if (!feature || !price) bad(`Name a feature: ${Object.keys(FEATURES).join(', ')}.`);
  const units = int(body, 'units');
  if (!units || units < 1 || units > 10_000) bad('`units` must be between 1 and 10000.');
  const session = body.session_id === undefined ? null : str(body, 'session_id', 64);
  if (session !== null && !SESSION.test(session)) bad('`session_id` is not valid.');
  const account = await aiAccount(c.env, claims.acc);
  if (account?.mode === 'disabled') throw new ApiError(403, 'ai_disabled', 'Plexora AI is turned off for this account.');
  if (dev && account?.mode !== 'dev') throw new ApiError(403, 'dev_not_allowed', 'This account is no longer in dev mode.');
  // Credits: the published flat price. Dev: a cap of three times the expected
  // provider cost, charged at cost.
  const quote = dev ? units * price.expected_cost_micro * 3 : units * price.price_micro;
  const envelope = units * price.calls_per_unit + price.base_calls;
  const runId = newId('run');
  const expires = now + RUN_HOURS * HOUR;
  await prepare(c.env, claims.acc, account, now);
  await reserve(c.env, claims.acc, quote, now, { run_id: runId, expires_at: expires });
  await c.env.LICENSE_DB.prepare(
    `INSERT INTO ai_runs (id, account_id, user_id, seat_id, billing, session_id, feature, unit, units, quote_micro,
       hold_micro, envelope_calls, status, started_at, expires_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?10, ?11, 'open', ?12, ?13)`,
  ).bind(runId, claims.acc, claims.usr, claims.seat, dev ? 'dev' : 'credits', session, feature, price.unit, units,
    quote, envelope, now, expires).run();
  const run = (await runById(c.env, runId))!;
  return ok(c, { ...runView(run), balance: balanceView(await balance(c.env, claims.acc)) }, 201);
}

ai.post('/runs', (c) => startRun(c, false));
ai.post('/dev/runs', (c) => startRun(c, true));

async function ownRun(c: Ctx, now: number): Promise<RunRow> {
  const claims = await bearer(c, now);
  const run = await runById(c.env, c.req.param('id') ?? '');
  if (!run || run.account_id !== claims.acc) throw new ApiError(404, 'not_found', 'No such run.');
  return run;
}

ai.get('/runs/:id', async (c) => ok(c, runView(await ownRun(c, nowSeconds()))));

ai.post('/runs/:id/finish', async (c) => {
  const now = nowSeconds();
  const run = await ownRun(c, now);
  await finishRun(c.env, run, now);
  const fresh = (await runById(c.env, run.id))!;
  return ok(c, { ...runView(fresh), balance: balanceView(await balance(c.env, run.account_id)) });
});

// -- balance, usage, pricing ---------------------------------------------------------

ai.get('/balance', async (c) => {
  const now = nowSeconds();
  const claims = await bearer(c, now);
  const account = await aiAccount(c.env, claims.acc);
  await prepare(c.env, claims.acc, account, now);
  return ok(c, { account_id: claims.acc, mode: account?.mode ?? 'credits', ...balanceView(await balance(c.env, claims.acc)) });
});

export async function usageRows(env: AppEnv['Bindings'], accountId: string | null, sinceMs: number,
  userId: string | null = null) {
  return all<Record<string, unknown>>(env,
    `SELECT strftime('%Y-%m-%d', started_at_ms / 1000, 'unixepoch') AS day, COALESCE(feature, '') AS feature, billing,
       COUNT(*) AS calls, SUM(status = 'ok') AS ok, SUM(input_uncached) AS input_uncached, SUM(cache_read) AS cache_read,
       SUM(cache_write_5m + cache_write_1h) AS cache_write, SUM(output_tokens) AS output_tokens,
       SUM(price_micro) AS price_micro, SUM(charged_micro) AS charged_micro,
       SUM(CASE WHEN billing = 'dev' THEN cost_micro ELSE 0 END) AS dev_cost_micro
     FROM ai_requests
     WHERE (?1 IS NULL OR account_id = ?1) AND started_at_ms >= ?2 AND (?3 IS NULL OR user_id = ?3)
     GROUP BY day, feature, billing ORDER BY day DESC, feature`, accountId, sinceMs, userId);
}

ai.get('/usage', async (c) => {
  const now = nowSeconds();
  const claims = await bearer(c, now);
  const days = Math.max(1, Math.min(Number(c.req.query('days') ?? '30') || 30, 400));
  const mine = c.req.query('mine') === '1';
  return ok(c, { account_id: claims.acc, days,
    rows: await usageRows(c.env, claims.acc, (now - days * DAY) * 1000, mine ? claims.usr : null) });
});

ai.get('/requests/:id', async (c) => {
  const claims = await bearer(c, nowSeconds());
  const row = await one<Record<string, unknown>>(c.env,
    `SELECT id, billing, run_id, session_id, feature, agent, capability, status, failure_class, stop_reason, usage_source,
       input_uncached, cache_read, cache_write_5m, cache_write_1h, output_tokens, image_count, price_micro, charged_micro,
       started_at_ms, first_byte_ms, finished_at_ms
     FROM ai_requests WHERE id = ?1 AND account_id = ?2`, c.req.param('id'), claims.acc);
  if (!row) throw new ApiError(404, 'not_found', 'No such request.');
  return ok(c, row);
});

ai.get('/pricing', (c) => ok(c, {
  credit_micro: 10_000,
  features: Object.fromEntries(Object.entries(FEATURES).map(([name, f]) =>
    [name, { unit: f.unit, credits: Math.ceil(f.price_micro / 10_000), price_micro: f.price_micro }])),
  capabilities: CAPABILITIES,
  metered_markup_bps: knob(c.env, 'AI_MARKUP_BPS'),
}));

// -- admin: tracking ------------------------------------------------------------------

export const aiAdmin = new Hono<AppEnv>();

aiAdmin.get('/usage', async (c) => {
  const now = nowSeconds();
  const days = Math.max(1, Math.min(Number(c.req.query('days') ?? '30') || 30, 400));
  const since = (now - days * DAY) * 1000;
  const [accounts, models] = await Promise.all([
    all<Record<string, unknown>>(c.env,
      `SELECT r.account_id, a.name AS account_name, r.billing, COUNT(*) AS calls, SUM(r.status = 'ok') AS ok,
         SUM(r.input_uncached) AS input_uncached, SUM(r.cache_read) AS cache_read,
         SUM(r.cache_write_5m + r.cache_write_1h) AS cache_write, SUM(r.output_tokens) AS output_tokens,
         SUM(r.cost_micro) AS cost_micro, SUM(r.price_micro) AS price_micro, SUM(r.charged_micro) AS charged_micro
       FROM ai_requests r LEFT JOIN accounts a ON a.id = r.account_id
       WHERE r.started_at_ms >= ?1 GROUP BY r.account_id, r.billing ORDER BY cost_micro DESC LIMIT 500`, since),
    all<Record<string, unknown>>(c.env,
      `SELECT provider, model, billing, COUNT(*) AS calls, SUM(cost_micro) AS cost_micro,
         SUM(charged_micro) AS charged_micro, SUM(cache_read) AS cache_read,
         SUM(input_uncached + cache_read + cache_write_5m + cache_write_1h) AS input_total
       FROM ai_requests WHERE started_at_ms >= ?1 GROUP BY provider, model, billing`, since),
  ]);
  const totals = accounts.reduce<Record<string, number>>((t, r) => {
    for (const k of ['calls', 'cost_micro', 'charged_micro']) t[k] = (t[k] ?? 0) + Number(r[k] ?? 0);
    return t;
  }, {});
  return ok(c, { days, totals, accounts, models });
});

aiAdmin.get('/requests', async (c) => {
  const limit = Math.max(1, Math.min(Number(c.req.query('limit') ?? '100') || 100, 1000));
  const account = c.req.query('account_id') ?? null;
  return ok(c, { requests: await all(c.env,
    `SELECT * FROM ai_requests WHERE (?1 IS NULL OR account_id = ?1) ORDER BY started_at_ms DESC LIMIT ?2`,
    account, limit) });
});

aiAdmin.get('/accounts/:id', async (c) => {
  const now = nowSeconds();
  const accountId = c.req.param('id');
  const owner = await accountById(c.env, accountId);
  if (!owner) throw new ApiError(404, 'not_found', 'No such account.');
  const settings = await aiAccount(c.env, accountId);
  const [usage, runs, ledger, recent] = await Promise.all([
    usageRows(c.env, accountId, (now - 30 * DAY) * 1000),
    all(c.env, 'SELECT * FROM ai_runs WHERE account_id = ?1 ORDER BY started_at DESC LIMIT 20', accountId),
    all(c.env, 'SELECT * FROM ai_ledger WHERE account_id = ?1 ORDER BY id DESC LIMIT 50', accountId),
    all(c.env, `SELECT id, billing, feature, capability, model, status, cost_micro, price_micro, charged_micro,
        input_uncached, cache_read, cache_write_5m, output_tokens, started_at_ms FROM ai_requests
        WHERE account_id = ?1 ORDER BY started_at_ms DESC LIMIT 50`, accountId),
  ]);
  return ok(c, {
    account: { id: owner.id, name: owner.name },
    settings: { mode: settings?.mode ?? 'credits', markup_bps: settings?.markup_bps ?? null,
      allowance_micro: settings?.allowance_micro ?? null, notes: settings?.notes ?? null },
    balance: balanceView(await balance(c.env, accountId)), usage, runs, ledger, recent,
  });
});

aiAdmin.patch('/accounts/:id', async (c) => {
  const now = nowSeconds();
  const accountId = c.req.param('id');
  if (!(await accountById(c.env, accountId))) throw new ApiError(404, 'not_found', 'No such account.');
  const body = await readJson(c);
  const current = await aiAccount(c.env, accountId);
  const mode = body.mode === undefined ? current?.mode ?? 'credits' : body.mode;
  if (mode !== 'credits' && mode !== 'dev' && mode !== 'disabled') bad('`mode` is credits, dev or disabled.');
  const markup = body.markup_bps === undefined ? current?.markup_bps ?? null
    : body.markup_bps === null ? null : int(body, 'markup_bps');
  if (markup !== null && (markup === undefined || markup < 10_000 || markup > 100_000)) {
    bad('`markup_bps` is null or 10000-100000 (1x-10x).');
  }
  const allowance = body.allowance_micro === undefined ? current?.allowance_micro ?? null
    : body.allowance_micro === null ? null : int(body, 'allowance_micro');
  if (allowance !== null && (allowance === undefined || allowance < 0)) bad('`allowance_micro` is null or >= 0.');
  const notes = body.notes === undefined ? current?.notes ?? null : str(body, 'notes', 2000);
  const who = `admin:${c.get('admin')}`;
  await c.env.LICENSE_DB.prepare(
    `INSERT INTO ai_accounts (account_id, mode, markup_bps, allowance_micro, notes, updated_at, updated_by)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
     ON CONFLICT(account_id) DO UPDATE SET mode = ?2, markup_bps = ?3, allowance_micro = ?4, notes = ?5,
       updated_at = ?6, updated_by = ?7`,
  ).bind(accountId, mode, markup, allowance, notes, now, who).run();
  await record(c.env, now, { actor: who, kind: 'ai.account_updated', account_id: accountId,
    payload: { mode, markup_bps: markup, allowance_micro: allowance } });
  return ok(c, { account_id: accountId, mode, markup_bps: markup, allowance_micro: allowance, notes });
});

aiAdmin.post('/accounts/:id/credit', async (c) => {
  const now = nowSeconds();
  const accountId = c.req.param('id');
  if (!(await accountById(c.env, accountId))) throw new ApiError(404, 'not_found', 'No such account.');
  const body = await readJson(c);
  const credits = int(body, 'credits');
  const micro = int(body, 'amount_micro') ?? (credits !== null ? credits * 10_000 : null);
  if (micro === null || micro === 0 || Math.abs(micro) > 100_000_000_000) bad('Give `credits` or `amount_micro` (non-zero).');
  const kind = body.kind === undefined ? 'grant' : body.kind;
  if (kind !== 'grant' && kind !== 'purchase' && kind !== 'adjustment' && kind !== 'refund') {
    bad('`kind` is grant, purchase, adjustment or refund.');
  }
  if (micro < 0 && kind !== 'adjustment' && kind !== 'refund') bad('Only an adjustment or refund may be negative.');
  const who = `admin:${c.get('admin')}`;
  const posted = await credit(c.env, accountId, { micro, kind, journal_id: str(body, 'journal_id', 80), actor: who,
    note: str(body, 'note', 500), ref_type: 'admin' }, now);
  if (posted.posted) {
    await record(c.env, now, { actor: who, kind: 'ai.credit', account_id: accountId,
      payload: { micro, kind, note: str(body, 'note', 500) } });
  }
  return ok(c, { posted: posted.posted, balance: balanceView(await balance(c.env, accountId)) }, posted.posted ? 201 : 200);
});
