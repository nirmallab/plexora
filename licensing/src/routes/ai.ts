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
 * A call is served by the route table (routing.ts): any of the providers in
 * providers.ts, retried on the same route first, failed over per the route's
 * policy, and optionally shadowed to a candidate route at Plexora's cost.
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
import { ApiError, type AppEnv, type ErrorCode, int, ok, readJson, str } from '../http';
import { licenseProblem, seatProblem } from '../licensing';
import { enforce, hit } from '../ratelimit';
import {
  BENCH, benchPasses, type Capability, CAPABILITIES, capabilitiesFor, costMicro, estimateMicro, FEATURES, type Route,
  ROUTES, type UnitCosts, type Usage, withMarkup,
} from '../ai/catalog';
import {
  aiAccount, balance, balanceView, claimRunCall, credit, finishRun, markupFor, prepare, releaseHold, reserve, runById,
  type RunRow, settleCall, settleRunCall,
} from '../ai/ledger';
import { type Billing, type GatewayClaims, issueToken, verifyBearer } from '../ai/token';
import { buildBody, type CallOptions, callProvider, configured, isProvider, PROVIDERS, SPECS } from '../ai/providers';
import {
  agreement, circuit, circuitKey, force, type ModelCost, namedRoute, preferSticky, recordOutcome, type Resolution,
  resolve, shadowFor, stick, stickUpstream, stickyRouteId, stickyUpstream, unsuitable,
} from '../ai/routing';
import { modulesFor, moduleOf, WIRE_TASK } from '../ai/tasks';
import { rejectsEffort } from '../ai/effort';
import { adapterFor } from '../ai/translate';
import { assess, chainsOf, gather } from '../ai/capacity';
import { tasksView } from '../ai/views';
import { clearSettingsCache, describe as describeSettings, EDITABLE, isEditable, toKnob } from '../ai/settings';
import { classify, type Envelope, sse, userHash } from '../ai/upstream';
import { presented } from './v1';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Ctx = HonoContext<AppEnv, any>;

export const ai = new Hono<AppEnv>();

// -- the admin's switches and limits (src/ai/settings.ts) ----------------------------

/** AI_ENABLED = 0 (the admin page's switch) refuses every token, call and run. */
function requireSwitchedOn(env: AppEnv['Bindings']): void {
  if (knob(env, 'AI_ENABLED') !== 1) {
    throw new ApiError(503, 'ai_disabled', 'Plexora AI is switched off for now.', { retry_after: 300 });
  }
}

interface AccountLimits { calls_per_day: number | null; seat_calls_per_day: number | null }

export const accountLimits = (env: AppEnv['Bindings'], accountId: string) =>
  one<AccountLimits>(env, 'SELECT calls_per_day, seat_calls_per_day FROM ai_account_limits WHERE account_id = ?1',
    accountId);

/** The daily call limits for one account and its seats: its own, else the global knobs; 0 = none. */
export async function dailyLimits(env: AppEnv['Bindings'], accountId: string) {
  const own = await accountLimits(env, accountId);
  return { account: own?.calls_per_day ?? knob(env, 'AI_CALLS_PER_ACCOUNT_PER_DAY'),
    seat: own?.seat_calls_per_day ?? knob(env, 'AI_CALLS_PER_SEAT_PER_DAY') };
}

/** Count one model call against the seat's and the account's day; refuse past either limit. */
async function countDailyCall(env: AppEnv['Bindings'], claims: GatewayClaims, now: number): Promise<void> {
  const limits = await dailyLimits(env, claims.acc);
  const nextDay = now - (now % DAY) + DAY;
  for (const [scope, bucket, subject, limit] of [['person', 'ai-day-seat', claims.seat, limits.seat],
    ['account', 'ai-day-account', claims.acc, limits.account]] as const) {
    if (limit > 0 && !(await hit(env, bucket, subject, limit, DAY, now))) {
      throw new ApiError(429, 'usage_limit_reached', scope === 'person'
        ? `This seat has made its ${limit} Plexora AI calls for today; the limit resets at midnight UTC.`
        : `This account has made its ${limit} Plexora AI calls for today; the limit resets at midnight UTC.`,
      { retry_after: nextDay - now, details: { scope, limit } });
    }
  }
}

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
  requireSwitchedOn(c.env);
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
  const entitlements = seatEntitlements(license!, seat!);
  const caps = capabilitiesFor(entitlements);
  if (!caps.length) throw new ApiError(403, 'ai_not_entitled', 'This licence does not include Plexora AI.');
  const modules = modulesFor(entitlements);
  const account = await aiAccount(c.env, license!.account_id);
  if (account?.mode === 'disabled') throw new ApiError(403, 'ai_disabled', 'Plexora AI is turned off for this account.');
  const appVersion = typeof body.app_version === 'string' ? body.app_version.slice(0, 24) : '';
  const issued = await issueToken(c.env, {
    acc: license!.account_id, usr: seat!.user_id, lic: license!.id, seat: seat!.id, env: environment.id,
    envt: payload.environment_type, caps, mods: modules === 'all' ? ['*'] : [...modules].sort(),
    mode: account?.mode === 'dev' ? 'dev' : 'credits', ver: appVersion,
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
  /** `module.task` (tasks.ts), or null from a client that names none. */
  task: string | null;
  context: Context;
  envelope: Envelope;
  model: string | null;
  textChars: number;
  images: number;
}

const BLOCK_TYPES = new Set(['text', 'image', 'tool_use', 'tool_result']);
const REQUEST_KEYS = new Set(['system', 'messages', 'tools', 'max_tokens', 'stop_sequences', 'output_schema']);
const TOP_KEYS = new Set(['capability', 'task', 'context', 'request', 'model']);

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
  let task: string | null = null;
  if (body.task !== undefined && body.task !== null) {
    if (typeof body.task !== 'string' || !WIRE_TASK.test(body.task)) bad('`task` is module.task, e.g. gating.image_inspection.');
    task = body.task;
  }
  let model: string | null = null;
  if (body.model !== undefined) {
    if (!dev) bad('`model` is accepted only on the dev route; name a `capability`.');
    if (typeof body.model !== 'string' || !/^[A-Za-z0-9._:/-]{1,160}$/.test(body.model)) {
      bad('`model` is `provider/model`, or a catalogued Anthropic model id.');
    }
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
    capability, task, context, model, textChars: tally.chars, images: tally.images,
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
  requireSwitchedOn(env);
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
  // A call that names its task is entitled by module (ai:gating -> gating.*); one that does not, or a token
  // issued before modules were, by its capability class as before.
  const mods = v.task && claims.mods ? claims.mods : null;
  if (mods ? !(mods.includes('*') || mods.includes(moduleOf(v.task!))) : !claims.caps.includes(v.capability)) {
    throw new ApiError(403, 'capability_not_allowed', v.task && mods
      ? 'This licence does not include that Plexora AI module.' : 'This licence does not include that capability.');
  }
  const account = await aiAccount(env, claims.acc);
  if (account?.mode === 'disabled') throw new ApiError(403, 'ai_disabled', 'Plexora AI is turned off for this account.');
  if (dev && account?.mode !== 'dev') {
    throw new ApiError(403, 'dev_not_allowed', 'This account is no longer in dev mode.');
  }
  const billing: Billing = dev ? 'dev' : 'credits';
  const markup = dev ? DEV_MARKUP : markupFor(env, account);
  const feature = v.context.feature;
  const query = { task: v.task, feature, capability: v.capability };
  const stickyId = dev ? null : await stickyRouteId(env, claims.acc, v.context.session_id);
  const resolution = v.model ? await devResolution(env, v.capability, v.model) : await resolve(env, query);
  const costs = resolution.costs;
  let routes = preferSticky(resolution.routes.filter((r) => costs.has(r.id)), stickyId);
  if (!routes.length) {
    throw new ApiError(503, 'provider_unavailable', 'No model is configured for that task.', { retry_after: 60 });
  }
  // A model that cannot take this request's images or tools is not a candidate for it.
  const need = { images: v.images, tools: !!v.envelope.tools?.length };
  const unfit = routes.map((r) => unsuitable(costs.get(r.id)!, need));
  if (unfit.every((reason) => reason !== null)) {
    throw new ApiError(400, 'route_unsupported', `No ${v.task ?? v.capability} model on this route takes ${
      unfit[0] === 'no_vision' ? 'images' : 'tools'}.`, { details: { reasons: unfit } });
  }
  routes = routes.filter((_, i) => unfit[i] === null);
  // The assignment's cost cap, on Plexora's cost (no markup): a route whose high estimate for this request
  // is above it is passed over.
  if (resolution.max_cost_micro !== null) {
    const cap = resolution.max_cost_micro;
    const within = routes.filter((r) => estimateMicro(v.textChars, v.images, Math.min(v.envelope.max_tokens,
      r.max_tokens_cap), costs.get(r.id)!, 10_000) <= cap);
    if (!within.length) {
      throw new ApiError(400, 'route_unsupported', `Every model for ${v.task ?? v.capability} costs more than its cap of $${
        (cap / 1_000_000).toFixed(4)} for a request this large.`, { details: { max_cost_micro: cap } });
    }
    routes = within;
  }

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

  // Money first: a run's call counts against its envelope; any other call
  // holds its estimate, sized on the dearest route it might fail over to.
  let run: RunRow | null = null;
  let holdId: string | null = null;
  let holdMicro = 0;
  try {
    // Inside the try: a refused call frees its idempotency key, and a replayed key never counts twice.
    await countDailyCall(env, claims, now);
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
      const estimate = Math.max(...routes.map((r) => estimateMicro(v.textChars, v.images,
        Math.min(v.envelope.max_tokens, r.max_tokens_cap), costs.get(r.id)!, markup)));
      holdMicro = Math.min(knob(env, 'AI_MAX_RESERVE_MICRO'), Math.max(knob(env, 'AI_MIN_HOLD_MICRO'), estimate));
      holdId = await reserve(env, claims.acc, holdMicro, now, { request_id: requestId });
    }
  } catch (error) {
    await forget();
    throw error;
  }

  const user = await userHash(env, claims.acc, claims.usr);
  const upstreams = new Map<string, string>();
  if (!dev) {
    for (const r of routes.filter((x) => x.provider === 'openrouter')) {
      const pinned = await stickyUpstream(env, claims.acc, v.context.session_id, r.id, now);
      if (pinned) upstreams.set(r.id, pinned);
    }
  }
  const options = { user, cacheKey: await cacheKey(env, claims.acc, v.context.session_id, v.envelope.system),
    upstream: upstreams };
  const base = {
    id: requestId, account_id: claims.acc, user_id: claims.usr, license_id: claims.lic, seat_id: claims.seat,
    environment_id: claims.env, token_jti: claims.jti, billing, run_id: run?.id ?? null,
    session_id: v.context.session_id, feature, agent: v.context.agent,
    workflow: v.context.workflow, attempt: v.context.attempt, app_version: claims.ver, capability: v.capability,
    task: v.task, image_count: v.images, markup_bps: markup, hold_micro: holdMicro, request_bytes: text.length,
    started_at_ms: startedMs, shadow_of: null, shadow_agree: null,
  };

  const connected = await connect(env, routes, v.envelope, options, now, costs);
  if (!connected.ok) {
    const failed = connected;
    if (holdId) await releaseHold(env, claims.acc, holdId, holdMicro, now);
    if (run) await env.LICENSE_DB.prepare('UPDATE ai_runs SET calls = MAX(calls - 1, 0) WHERE id = ?1').bind(run.id).run();
    const route = failed.route ?? routes[0]!;
    await insertRequest(env, { ...base, provider: route.provider, model: route.model, model_id: route.model_id,
      effort: route.effort, route_id: route.id, attempts: failed.attempts, failover: failed.index > 0 ? 1 : 0, status: 'error', failure_class: failed.failure,
      http_status: failed.status, usage_source: 'none', unit: costs.get(route.id)!, usage: null, cost: 0, price: 0,
      charged: 0, first_byte_ms: null, finished_at_ms: Date.now(), stop_reason: null, provider_request_id: null,
      resolved_model: null, reported_cost_micro: null });
    await forget();   // nothing happened upstream: the same key may be retried
    throw new ApiError(failed.http, failed.code, failed.code === 'provider_rejected'
      ? `The provider refused this request: ${failed.detail}` : 'The model provider is not available right now.',
      { retry_after: failed.retryAfter ?? 15 });
  }
  const { route, response: upstream } = connected;
  const unit = costs.get(route.id)!;
  if (!dev && route.id !== stickyId) await stick(env, claims.acc, v.context.session_id, route.id, now);
  const shadow = dev ? null : await shadowFor(env, query,
    v.context.session_id ? `${claims.acc}:${v.context.session_id}` : null, resolution.legacy);
  const shadowRoute = shadow && circuitKey(shadow.route) !== circuitKey(route) ? shadow : null;
  // Which kind of failover served it: another provider of the same model, or the next model.
  const failover = connected.index === 0 ? null : route.model_id === routes[0]!.model_id ? 'provider' : 'model';

  const row = { ...base, provider: route.provider, model: route.model, model_id: route.model_id, route_id: route.id,
    effort: route.effort, attempts: connected.attempts, failover: connected.index > 0 ? 1 : 0 };
  const firstByteMs = Date.now();
  const adapter = adapterFor(SPECS[route.provider].wire, shadowRoute !== null);
  const settle = async () => {
    const m = adapter.result();
    const cost = costMicro(m.usage, unit);
    const price = billing === 'dev' ? cost : withMarkup(cost, markup);
    const finished = nowSeconds();
    let charged = price;
    if (run) charged = await settleRunCall(env, run, price, cost, finished, requestId, claims.usr);
    else await settleCall(env, claims.acc, holdId, holdMicro, price, finished, requestId, claims.usr);
    const status = m.complete ? 'ok' : 'incomplete';
    await insertRequest(env, { ...row, status, failure_class: m.complete ? null : 'stream_cut', http_status: 200,
      usage_source: m.source, unit, usage: m.usage, cost, price, charged, first_byte_ms: firstByteMs,
      finished_at_ms: Date.now(), stop_reason: m.stop_reason, provider_request_id: m.provider_request_id,
      resolved_model: upstream.headers.get('x-orca-resolved-model') ?? m.model,
      reported_cost_micro: m.reported_cost_micro });
    if (!dev && m.upstream && route.provider === 'openrouter') {
      await stickUpstream(env, claims.acc, v.context.session_id, route.id, m.upstream, finished);
    }
    await env.LICENSE_DB.prepare(
      `UPDATE ai_idempotency SET state = ?3, price_micro = ?4 WHERE account_id = ?1 AND key_hash = ?2`,
    ).bind(claims.acc, keyHash, m.complete ? 'settled' : 'failed', charged).run();
    const fresh = run ? await runById(env, run.id) : null;
    return {
      gateway_request_id: requestId, status, usage_source: m.source, usage: m.usage,
      price_micro: price, charged_micro: charged, billing,
      model: route.model_id, provider: route.provider,
      ...(billing === 'dev' ? { cost_micro: cost, provider_model: route.model } : {}),
      run: fresh ? runView(fresh) : null,
      balance: balanceView(await balance(env, claims.acc)),
    };
  };

  // One pump reads the provider, hands the client Anthropic-shaped events,
  // and settles; it keeps going if the client leaves, so a call is billed once.
  const accepted = { gateway_request_id: requestId, provider: route.provider, model: route.model_id,
    capability: v.capability, task: v.task, billing, hold_micro: holdMicro, run_id: run?.id ?? null,
    ...(failover ? { failover } : {}) };
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  let attached = true;
  const send = (bytes: Uint8Array | null) => {
    if (!bytes || !attached) return;
    try {
      controller.enqueue(bytes);
    } catch {
      attached = false;
    }
  };
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
      c.enqueue(sse('plexora.accepted', accepted));
    },
    cancel() {
      attached = false;
    },
  });
  const reader = upstream.body!.getReader();
  const pump = (async () => {
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value) send(adapter.push(value));
      }
    } catch {
      // A cut stream is settled on what was seen.
    }
    send(adapter.end());
    try {
      send(sse('plexora.usage', await settle()));
    } catch (error) {
      console.error('ai settle', error);
      send(sse('plexora.error', { gateway_request_id: requestId, code: 'internal_error', retryable: false }));
    }
    if (attached) {
      try {
        controller.close();
      } catch {
        // The client already went away.
      }
    }
    if (shadowRoute && adapter.result().complete) {
      await shadowCall(env, shadowRoute.route, shadowRoute.cost, v.envelope, options, adapter.text(),
        { ...base, id: newId('req') }, requestId).catch((error) => console.error('ai shadow', error));
    }
  })();
  try {
    c.executionCtx.waitUntil(pump);
  } catch {
    // No execution context (unit tests): the pump runs on its own.
  }
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Plexora-Request-Id': requestId } });
}

/** The dev route's named model, as the only route: `provider/model` on the wire, or an approved model id
 * (served by its primary provider). Effort and token cap are the capability's built-in ones. */
async function devResolution(env: AppEnv['Bindings'], capability: Capability, named: string): Promise<Resolution> {
  const found = await namedRoute(env, named);
  if (!found) bad(`Unknown model ${named}; name a catalogued one (an approved model id, or provider/model).`);
  const builtin = ROUTES[capability];
  const route = { ...found!.route, effort: builtin.effort, max_tokens_cap: builtin.max_tokens_cap };
  return { routes: [route], costs: new Map([[route.id, found!.cost]]), level: 'dev', pattern: null,
    max_cost_micro: null, latency_ms: null, skipped: [], legacy: false };
}

/** A key for provider-side cache affinity: same account, session and prefix, same key; identifies no one. */
async function cacheKey(env: AppEnv['Bindings'], accountId: string, sessionId: string | null,
  system: unknown): Promise<string> {
  const pepper = env.AI_USER_PEPPER || env.IP_HASH_KEY || 'plexora-ai';
  const prefix = JSON.stringify(system ?? '').slice(0, 4096);
  return (await sha256Hex(`${pepper}|${accountId}|${sessionId ?? ''}|${prefix}`)).slice(0, 32);
}

type Connected =
  | { ok: true; route: Route; response: Response; attempts: number; index: number }
  | { ok: false; route: Route | null; attempts: number; index: number; status: number; http: 429 | 503 | 400;
      code: ErrorCode; failure: string; detail: string; retryAfter: number | null };

/**
 * Reach a provider before anything streams. The SAME route is retried on a
 * rate limit or outage (that keeps the prompt cache), and the call moves to
 * the next route only as the route's `failover` allows: by default only once
 * its circuit is open. The request's own fault (a provider 400) never fails over.
 */
async function connect(env: AppEnv['Bindings'], routes: Route[], envelope: Envelope,
  options: CallOptions, now: number, costs: Map<string, ModelCost>): Promise<Connected> {
  const retries = Math.max(0, knob(env, 'AI_UPSTREAM_RETRIES'));
  const backoff = Math.max(0, knob(env, 'AI_RETRY_BACKOFF_MS'));
  let attempts = 0;
  let last: Connected | null = null;
  for (let index = 0; index < routes.length; index++) {
    const route = routes[index]!;
    const unusable = (failure: string): Connected => ({ ok: false, route, attempts, index, status: 503, http: 503,
      code: 'provider_unavailable', failure, detail: '', retryAfter: 15 });
    if (!configured(env, route.provider)) {
      last = unusable('provider_unconfigured');
      continue;
    }
    let state = await circuit(env, route, now);
    if (state.open) {
      last = unusable(state.forced ? 'provider_disabled' : 'circuit_open');
      continue;
    }
    let current = route;
    let body = buildBody(current, envelope, { ...options, structured: costs.get(route.id)?.structured ?? true });
    let opened = false;
    let failed: Extract<Connected, { ok: false }> | null = null;
    for (let attempt = 0; attempt <= retries; attempt++) {
      attempts += 1;
      let response: Response;
      try {
        response = await callProvider(env, current, body, undefined, options.cacheKey);
      } catch {
        response = new Response('upstream unreachable', { status: 503 });
      }
      if (response.ok && response.body) {
        await recordOutcome(env, current, true, now, state);
        return { ok: true, route: current, response, attempts, index };
      }
      const cls = classify(response.status);
      const detail = (await response.text().catch(() => '')).slice(0, 200).replace(/"[^"]{40,}"/g, '"…"');
      // A model that refuses the effort it was fitted to (a stale profile): once more without it, and say so.
      if (current.effort !== null && rejectsEffort(response.status, detail)) {
        await record(env, now, { actor: 'gateway', kind: 'ai.effort_rejected', payload: { route_id: route.id,
          model_id: route.model_id, effort: current.effort, wire: current.effort_wire ?? null, detail } });
        current = { ...current, effort: null };
        body = buildBody(current, envelope, { ...options, structured: costs.get(route.id)?.structured ?? true });
        attempt -= 1;
        continue;
      }
      const retryHeader = Number(response.headers.get('retry-after') ?? '');
      const retryAfter = Number.isFinite(retryHeader) && retryHeader > 0 ? Math.min(Math.ceil(retryHeader), 120) : null;
      if (cls.code !== 'provider_rejected') opened = (await recordOutcome(env, route, false, now, state)) || opened;
      state = { ...state, row: null };
      failed = { ok: false, route, attempts, index, status: response.status, http: cls.http, code: cls.code,
        failure: cls.failure, detail, retryAfter };
      if (!cls.retryable || opened) break;
      if (attempt < retries) {
        const wait = retryAfter !== null && retryAfter <= 5 ? retryAfter * 1000 : backoff * 4 ** attempt;
        if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
      }
    }
    if (!failed) continue;
    last = failed;
    if (failed.code === 'provider_rejected' || route.failover === 'never') return failed;
    // A broken key or account is an outage of that route, whatever its circuit says.
    if (route.failover === 'outage' && !opened && failed.failure !== 'provider_auth') return failed;
  }
  return last ?? { ok: false, route: null, attempts, index: 0, status: 503, http: 503, code: 'provider_unavailable',
    failure: 'no_route', detail: '', retryAfter: 15 };
}

/**
 * Duplicate a served call to a shadow candidate, at Plexora's cost: metered
 * and recorded with billing 'shadow' and whether it reached the same
 * decision, never charged, its answer never returned or kept.
 */
async function shadowCall(env: AppEnv['Bindings'], route: Route, unit: ModelCost, envelope: Envelope,
  options: CallOptions, servedText: string, base: ShadowBase,
  servedId: string): Promise<void> {
  if (!configured(env, route.provider) || (await circuit(env, route, nowSeconds())).open) return;
  if (unsuitable(unit, { images: base.image_count, tools: !!envelope.tools?.length })) return;
  const shadowBase = { ...base, billing: 'shadow' as const, run_id: null, markup_bps: 10_000, hold_micro: 0,
    provider: route.provider, model: route.model, model_id: route.model_id, effort: route.effort, route_id: route.id,
    attempts: 1,
    failover: 0, shadow_of: servedId };
  const body = buildBody(route, envelope, { ...options, structured: unit.structured });
  let response = new Response('', { status: 503 });
  // One more try on a dropped connection: a pooled keep-alive socket the provider already closed.
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      response = await callProvider(env, route, body, undefined, options.cacheKey);
      break;
    } catch (error) {
      console.error('ai shadow: upstream unreachable', error);
    }
  }
  if (!response.ok || !response.body) {
    await recordOutcome(env, route, false, nowSeconds(), null);
    await insertRequest(env, { ...shadowBase, status: 'error', failure_class: classify(response.status).failure,
      http_status: response.status, usage_source: 'none', unit, usage: null, cost: 0, price: 0, charged: 0,
      first_byte_ms: null, finished_at_ms: Date.now(), stop_reason: null, provider_request_id: null,
      resolved_model: null, reported_cost_micro: null, shadow_agree: null });
    return;
  }
  const firstByte = Date.now();
  const adapter = adapterFor(SPECS[route.provider].wire, true);
  const reader = response.body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) adapter.push(value);
    }
  } catch {
    // Settled on what was seen.
  }
  adapter.end();
  const m = adapter.result();
  await insertRequest(env, { ...shadowBase, status: m.complete ? 'ok' : 'incomplete',
    failure_class: m.complete ? null : 'stream_cut', http_status: 200, usage_source: m.source, unit, usage: m.usage,
    cost: costMicro(m.usage, unit), price: 0, charged: 0, first_byte_ms: firstByte, finished_at_ms: Date.now(),
    stop_reason: m.stop_reason, provider_request_id: m.provider_request_id,
    resolved_model: response.headers.get('x-orca-resolved-model') ?? m.model, reported_cost_micro: m.reported_cost_micro,
    shadow_agree: m.complete ? agreement(servedText, adapter.text()) : null });
}

interface RequestRecord {
  id: string; account_id: string; user_id: string | null; license_id: string; seat_id: string;
  environment_id: string | null; token_jti: string; billing: Billing | 'shadow'; run_id: string | null;
  session_id: string | null; feature: string | null; agent: string | null; workflow: string | null; attempt: number;
  app_version: string; capability: string; task: string | null; provider: string; model: string;
  model_id: string | null; route_id: string; attempts: number; effort?: string | null;
  failover: number; image_count: number; markup_bps: number; hold_micro: number; request_bytes: number;
  started_at_ms: number; status: string; failure_class: string | null; http_status: number; usage_source: string;
  unit: UnitCosts; usage: Usage | null; cost: number; price: number; charged: number; first_byte_ms: number | null;
  finished_at_ms: number; stop_reason: string | null; provider_request_id: string | null;
  resolved_model: string | null; reported_cost_micro: number | null; shadow_of: string | null;
  shadow_agree: number | null;
}

type ShadowBase = Pick<RequestRecord, 'id' | 'account_id' | 'user_id' | 'license_id' | 'seat_id' | 'environment_id' |
  'token_jti' | 'session_id' | 'feature' | 'agent' | 'workflow' | 'attempt' | 'app_version' | 'capability' |
  'task' | 'image_count' | 'request_bytes' | 'started_at_ms'>;

async function insertRequest(env: AppEnv['Bindings'], r: RequestRecord): Promise<void> {
  const u = r.usage;
  await env.LICENSE_DB.prepare(
    `INSERT INTO ai_requests (id, account_id, user_id, license_id, seat_id, environment_id, token_jti, billing, run_id,
       session_id, feature, agent, workflow, attempt, app_version, capability, provider, model, provider_request_id,
       status, failure_class, http_status, stop_reason, usage_source, input_uncached, cache_read, cache_write_5m,
       cache_write_1h, output_tokens, image_count, p_in, p_cache_read, p_cache_write_5m, p_cache_write_1h, p_out,
       markup_bps, hold_micro, cost_micro, price_micro, charged_micro, request_bytes, started_at_ms, first_byte_ms,
       finished_at_ms, route_id, attempts, failover, resolved_model, reported_cost_micro, shadow_of, shadow_agree,
       task, model_id, effort)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23,
       ?24, ?25, ?26, ?27, ?28, ?29, ?30, ?31, ?32, ?33, ?34, ?35, ?36, ?37, ?38, ?39, ?40, ?41, ?42, ?43, ?44, ?45,
       ?46, ?47, ?48, ?49, ?50, ?51, ?52, ?53, ?54)`,
  ).bind(r.id, r.account_id, r.user_id, r.license_id, r.seat_id, r.environment_id, r.token_jti, r.billing, r.run_id,
    r.session_id, r.feature, r.agent, r.workflow, r.attempt, r.app_version, r.capability, r.provider, r.model,
    r.provider_request_id, r.status, r.failure_class, r.http_status, r.stop_reason, r.usage_source,
    u?.input_uncached ?? 0, u?.cache_read ?? 0, u?.cache_write_5m ?? 0, u?.cache_write_1h ?? 0, u?.output_tokens ?? 0,
    r.image_count, r.unit.in, r.unit.cache_read, r.unit.cache_write_5m, r.unit.cache_write_1h, r.unit.out,
    r.markup_bps, r.hold_micro, r.cost, r.price, r.charged, r.request_bytes, r.started_at_ms, r.first_byte_ms,
    r.finished_at_ms, r.route_id, r.attempts, r.failover, r.resolved_model, r.reported_cost_micro, r.shadow_of,
    r.shadow_agree, r.task, r.model_id, r.effort ?? null).run();
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
  requireSwitchedOn(c.env);
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
       AND billing != 'shadow'
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
     FROM ai_requests WHERE id = ?1 AND account_id = ?2 AND billing != 'shadow'`, c.req.param('id'), claims.acc);
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

/** How close each limit is (src/ai/capacity.ts): the admin page's Capacity card, as JSON. */
aiAdmin.get('/capacity', async (c) => {
  const usage = await gather(c.env, Date.now(), chainsOf((await tasksView(c.env)).rows));
  return ok(c, { ...assess(usage), usage });
});

aiAdmin.get('/usage', async (c) => {
  const now = nowSeconds();
  const days = Math.max(1, Math.min(Number(c.req.query('days') ?? '30') || 30, 400));
  const since = (now - days * DAY) * 1000;
  const [accounts, models, tasks] = await Promise.all([
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
    all<Record<string, unknown>>(c.env,
      `SELECT COALESCE(task, COALESCE(feature, '_') || '.(' || capability || ')') AS task, model_id, provider, model,
         COUNT(*) AS calls, SUM(status != 'ok') AS failed, SUM(cost_micro) AS cost_micro,
         SUM(charged_micro) AS charged_micro
       FROM ai_requests WHERE started_at_ms >= ?1 AND billing != 'shadow'
       GROUP BY 1, model_id, provider, model ORDER BY calls DESC`, since),
  ]);
  const totals = accounts.reduce<Record<string, number>>((t, r) => {
    for (const k of ['calls', 'cost_micro', 'charged_micro']) t[k] = (t[k] ?? 0) + Number(r[k] ?? 0);
    return t;
  }, {});
  return ok(c, { days, totals, accounts, models, tasks });
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

// -- admin: the routing bench, shadow agreement, providers ----------------------------
//
// Approved models, provider routes, task assignments and pricing are in aiAdminCatalog.ts.
//
//   POST   /evaluations                  record a routing-bench result; the gateway decides `passed`
//   GET    /evaluations
//   GET    /shadow                       shadow agreement and cost per candidate
//   GET    /providers                    keys present, circuits, kill switches
//   GET    /capacity                     each plan, database and provider limit, graded ok / watch / act
//   POST   /providers/:key/disable       kill switch: `openai` or `openai:<model>`
//   POST   /providers/:key/enable

/**
 * A routing-bench result, for a module and an approved model (`model_id`).
 * The older shape -- `provider` and the model id on its wire -- is accepted
 * too and recorded against the approved model that route belongs to.
 */
aiAdmin.post('/evaluations', async (c) => {
  const now = nowSeconds();
  const body = await readJson(c);
  const feature = str(body, 'feature', 32);
  if (!feature || (feature !== '*' && !NAME.test(feature))) bad('Name the `feature` (module) the model was benched for.');
  const capability = body.capability === undefined ? '*' : str(body, 'capability', 32);
  if (!capability || (capability !== '*' && !(CAPABILITIES as readonly string[]).includes(capability))) {
    bad('`capability` is a known capability or *.');
  }
  let modelId = body.model_id === undefined ? null : str(body, 'model_id', 64);
  const provider = body.provider ?? null;
  if (provider !== null && !isProvider(provider)) bad(`\`provider\` is one of ${PROVIDERS.join(', ')}.`);
  if (!modelId) {
    const wire = str(body, 'model', 160);
    if (!wire) bad('Name the approved `model_id` (or `provider` and `model`).');
    const route = provider ? await one<{ model_id: string }>(c.env,
      'SELECT model_id FROM ai_catalog_routes WHERE provider = ?1 AND provider_model = ?2', provider, wire)
      .catch(() => null) : null;
    modelId = route?.model_id ?? wire!;
  }
  const metrics = body.metrics;
  if (!metrics || typeof metrics !== 'object' || Array.isArray(metrics)) bad('Send `metrics` as an object of numbers.');
  const bench = BENCH[feature] ?? BENCH['*']!;
  const benchVersion = str(body, 'bench_version', 40) ?? bench.version;
  const verdict = benchPasses(bench, metrics as Record<string, unknown>);
  const misses = benchVersion === bench.version ? verdict.misses : [`bench ${benchVersion} is not ${bench.version}`,
    ...verdict.misses];
  const passed = misses.length === 0 ? 1 : 0;
  const datasets = Array.isArray(body.dataset_ids) ? body.dataset_ids.filter((d) => typeof d === 'string').slice(0, 200) : [];
  const who = `admin:${c.get('admin')}`;
  const row = await one<{ id: number }>(c.env,
    `INSERT INTO ai_route_evaluations (feature, capability, provider, model, bench_version, plexora_version,
       dataset_ids_json, metrics_json, passed, misses_json, report_url, evaluated_at, evaluated_by)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13) RETURNING id`,
    feature, capability, provider ?? '*', modelId, benchVersion, str(body, 'plexora_version', 40),
    JSON.stringify(datasets), JSON.stringify(metrics), passed, JSON.stringify(misses), str(body, 'report_url', 500),
    now, who);
  await record(c.env, now, { actor: who, kind: 'ai.route_evaluated', payload: { id: row?.id, feature, model_id: modelId,
    provider, passed } });
  return ok(c, { id: row?.id, model_id: modelId, passed: passed === 1, misses, bench_version: benchVersion,
    required: bench }, 201);
});

aiAdmin.get('/evaluations', async (c) => {
  const feature = c.req.query('feature') ?? null;
  return ok(c, { evaluations: await all(c.env,
    `SELECT * FROM ai_route_evaluations WHERE (?1 IS NULL OR feature = ?1) ORDER BY evaluated_at DESC LIMIT 200`,
    feature), bench: BENCH });
});

aiAdmin.get('/shadow', async (c) => {
  const now = nowSeconds();
  const days = Math.max(1, Math.min(Number(c.req.query('days') ?? '30') || 30, 400));
  return ok(c, { days, candidates: await shadowReport(c.env, (now - days * DAY) * 1000) });
});

export function shadowReport(env: AppEnv['Bindings'], sinceMs: number) {
  return all<Record<string, any>>(env,
    `SELECT feature, capability, task, model_id, provider, model, route_id, COUNT(*) AS calls,
       COUNT(DISTINCT session_id) AS sessions, SUM(status = 'ok') AS ok, SUM(shadow_agree IS NOT NULL) AS compared,
       SUM(shadow_agree = 1) AS agreed,
       ROUND(1.0 * SUM(shadow_agree = 1) / NULLIF(SUM(shadow_agree IS NOT NULL), 0), 4) AS agreement,
       SUM(cost_micro) AS cost_micro
     FROM ai_requests WHERE billing = 'shadow' AND started_at_ms >= ?1
     GROUP BY feature, capability, task, model_id, provider, model, route_id ORDER BY calls DESC`, sinceMs);
}

aiAdmin.get('/providers', async (c) => {
  const circuits = await all<Record<string, unknown>>(c.env, 'SELECT * FROM ai_circuits ORDER BY route_key');
  return ok(c, { providers: PROVIDERS.map((p) => ({ provider: p, wire: SPECS[p].wire, direct: SPECS[p].direct,
    configured: configured(c.env, p) })), circuits, server_time: nowSeconds() });
});

async function killSwitch(c: Ctx, open: boolean) {
  const now = nowSeconds();
  const key = c.req.param('key') ?? '';
  const provider = key.split(':')[0];
  if (!isProvider(provider)) bad(`Name a provider (${PROVIDERS.join(', ')}) or provider:model.`);
  const body = open ? await readJson(c).catch(() => ({} as Record<string, unknown>)) : {};
  const reason = str(body, 'reason', 300);
  await force(c.env, key, open, reason, now);
  await record(c.env, now, { actor: `admin:${c.get('admin')}`, kind: open ? 'ai.provider_disabled' : 'ai.provider_enabled',
    payload: { key, reason } });
  return ok(c, await one(c.env, 'SELECT * FROM ai_circuits WHERE route_key = ?1', key));
}

// -- admin: settings and limits ------------------------------------------------------
//
//   GET  /settings               every editable AI knob: value, where it comes from, its fallback
//   PUT  /settings               {NAME: value | null | "default"} in displayed units (credits, MB, x cost);
//                                null or "default" removes the override
//   PATCH /accounts/:id/limits   {calls_per_day, seat_calls_per_day}: this account's own daily limits (null = global)

aiAdmin.get('/settings', async (c) => ok(c, { settings: await describeSettings(c.env) }));

aiAdmin.put('/settings', async (c) => {
  const now = nowSeconds();
  const body = await readJson(c);
  const who = `admin:${c.get('admin')}`;
  const writes: D1PreparedStatement[] = [];
  const changed: Record<string, number | null> = {};
  for (const [name, raw] of Object.entries(body)) {
    if (!isEditable(name)) bad(`${name} is not a setting this page can change.`);
    const meta = EDITABLE[name]!;
    if (raw === null || raw === '' || raw === 'default') {
      writes.push(c.env.LICENSE_DB.prepare('DELETE FROM ai_settings WHERE name = ?1').bind(name));
      changed[name] = null;
      continue;
    }
    const shown = typeof raw === 'boolean' ? (raw ? 1 : 0) : Number(raw);
    if (!Number.isFinite(shown) || shown < meta.min || shown > meta.max) {
      bad(`${meta.label} is ${meta.min}-${meta.max}${meta.unit ? ` ${meta.unit}` : ''}.`);
    }
    const value = toKnob(name, shown);
    writes.push(c.env.LICENSE_DB.prepare(
      `INSERT INTO ai_settings (name, value, updated_at, updated_by) VALUES (?1, ?2, ?3, ?4)
       ON CONFLICT(name) DO UPDATE SET value = ?2, updated_at = ?3, updated_by = ?4`).bind(name, String(value), now, who));
    changed[name] = value;
  }
  if (!writes.length) bad('Name at least one setting.');
  await c.env.LICENSE_DB.batch(writes);
  clearSettingsCache();
  await record(c.env, now, { actor: who, kind: 'ai.settings_changed', payload: changed });
  return ok(c, { changed, note: 'Applies at once here, and within seconds everywhere.' });
});

aiAdmin.patch('/accounts/:id/limits', async (c) => {
  const now = nowSeconds();
  const accountId = c.req.param('id');
  if (!(await accountById(c.env, accountId))) throw new ApiError(404, 'not_found', 'No such account.');
  const body = await readJson(c);
  const current = await accountLimits(c.env, accountId);
  const pick = (name: 'calls_per_day' | 'seat_calls_per_day') => {
    if (!(name in body)) return current?.[name] ?? null;
    const raw = body[name];
    if (raw === null || raw === '' || raw === 'default') return null;
    const n = Number(raw);
    if (!Number.isInteger(n) || n < 0 || n > 10_000_000) bad(`\`${name}\` is a whole number of calls, 0 for no limit.`);
    return n;
  };
  const limits = { calls_per_day: pick('calls_per_day'), seat_calls_per_day: pick('seat_calls_per_day') };
  const who = `admin:${c.get('admin')}`;
  await c.env.LICENSE_DB.prepare(
    `INSERT INTO ai_account_limits (account_id, calls_per_day, seat_calls_per_day, updated_at, updated_by)
     VALUES (?1, ?2, ?3, ?4, ?5)
     ON CONFLICT(account_id) DO UPDATE SET calls_per_day = ?2, seat_calls_per_day = ?3, updated_at = ?4, updated_by = ?5`,
  ).bind(accountId, limits.calls_per_day, limits.seat_calls_per_day, now, who).run();
  await record(c.env, now, { actor: who, kind: 'ai.account_limits', account_id: accountId, payload: limits });
  return ok(c, { limits, effective: await dailyLimits(c.env, accountId) });
});

aiAdmin.post('/providers/:key/disable', (c) => killSwitch(c, true));
aiAdmin.post('/providers/:key/enable', (c) => killSwitch(c, false));
