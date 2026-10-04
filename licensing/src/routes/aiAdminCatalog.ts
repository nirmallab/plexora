/**
 * /admin/api/ai: approved models, their provider routes, task assignments,
 * pricing, and the one-shot migration from the v3 route table.
 *
 *   GET    /catalog                              models with routes and live state; providers
 *   GET    /catalog/discover?provider=&q=        a provider's listing, to approve from (a direct provider's
 *                                                needs its key: 409 provider_not_connected without one)
 *   POST   /catalog/import                       {provider, provider_model, id?}: approve from a listing; a
 *                                                model nobody prices is added with its route off, unpriced
 *   POST   /catalog/seed-builtin                 the built-in Anthropic models, where missing
 *   PUT    /catalog/:id                          create or edit an approved model
 *   DELETE /catalog/:id                          never refused: each chain naming it closes up, or inherits
 *   POST   /catalog/:id/routes                   add a provider route (prices given: manual; none found: off)
 *   PATCH  /catalog/:id/routes/:provider         on/off, failover, prices, back to listed prices (`auto`)
 *   DELETE /catalog/:id/routes/:provider
 *   POST   /catalog/:id/routes/:provider/move    {to: 0|1|2}
 *   POST   /catalog/:id/routes/:provider/primary
 *   POST   /pricing/refresh                      {provider?, model_id?}
 *   GET    /pricing/status
 *   GET    /pricing/history?model_id=&provider=&days=
 *   GET    /tasks                                registry, assignments, every row's effective chain
 *   PUT    /tasks/:task                          assign models to a task, module (gating.*) or * (all)
 *   DELETE /tasks/:task?role=                    back to inheriting
 *   POST   /migrate-legacy                       ai_models + ai_routes -> catalogue + assignments
 *   GET    /schema                               schema version, legacy rows, ai_requests columns
 *
 * A body field ending _usd is dollars per 1M tokens (what the forms send);
 * _micro is micro-USD per 1M. Every change is an event.
 */
import { Hono } from 'hono';

import { all, one } from '../db';
import { knob, nowSeconds } from '../env';
import { eventStatement, record } from '../events';
import { ApiError, type App, type AppEnv, int, ok, readJson, str } from '../http';
import { newId } from '../crypto';
import { BENCH, ROUTES, type UnitCosts } from '../ai/catalog';
import {
  defaultPricing, effortFromListing, fieldsFromListing, insertRoute, isUnpriced, MAX_ROUTES, MODEL_ID, modelById,
  type ModelFields, REASONING_UNCONFIRMED, type RemovalOutcome, removeModelStatements, reorderStatements, routesOf,
  seedBuiltinStatements, setEffortStatement, suggestId, UNPRICED_NOTE, unpricedPricing, upsertModel, WIRE_MODEL,
} from '../ai/catalog_store';
import {
  EFFORT_WIRES, type EffortLevel, type EffortWire, isLevel, LEVELS, sortLevels, SPECS_ALLOWED, type StoredProfile,
} from '../ai/effort';
import {
  canList, clearListingCache, type Listing, listing, listsPrices, NotConnected, refreshPricing,
} from '../ai/pricing';
import { admits, isProvider, LABELS, type Provider, PROVIDERS, SPECS } from '../ai/providers';
import {
  type CatalogRouteRow, type CatalogRow, hasTaskRouting, lacks, needsEvaluation, resolve, type TaskRouteRow,
} from '../ai/routing';
import { type LegacyRouteRow, legacyRows } from '../ai/routing_legacy';
import { levelOf, MODULES, moduleOf, PATTERN, patternLabel, TASKS } from '../ai/tasks';
import { catalogView, eligibility, priceHistory, pricingStatus, tasksView } from '../ai/views';

export const aiCatalogAdmin = new Hono<AppEnv>();

function bad(message: string): never {
  throw new ApiError(400, 'invalid_request', message);
}

const who = (c: App) => `admin:${c.get('admin')}`;

const flag = (body: Record<string, unknown>, name: string, fallback: number): number => {
  const v = body[name];
  if (v === undefined || v === null || v === '') return fallback;
  return v === true || v === 1 || v === '1' || v === 'on' || v === 'true' ? 1 : 0;
};

/** A price field as micro-USD per 1M tokens: `<name>_micro`, or `<name>_usd` in dollars (what the form sends). */
function price(body: Record<string, unknown>, name: string): number | null {
  const micro = body[`${name}_micro`];
  if (typeof micro === 'number' && Number.isInteger(micro)) return micro;
  const raw = body[`${name}_usd`];
  const usd = typeof raw === 'string' && raw.trim() !== '' ? Number(raw) : raw;
  return typeof usd === 'number' && Number.isFinite(usd) ? Math.round(usd * 1_000_000) : null;
}

const PRICE_NAMES = ['in', 'cache_read', 'cache_write_5m', 'cache_write_1h', 'out'] as const;

/** Prices from a body: all five (cache prices default to input), or null when none was given. */
function prices(body: Record<string, unknown>): UnitCosts | null {
  const got = Object.fromEntries(PRICE_NAMES.map((n) => [n, price(body, n)])) as Record<string, number | null>;
  if (PRICE_NAMES.every((n) => got[n] === null)) return null;
  if (got.in === null || got.out === null) bad('Give at least the input and output price ($ per 1M tokens).');
  const values = { in: got.in!, out: got.out!, cache_read: got.cache_read ?? got.in!,
    cache_write_5m: got.cache_write_5m ?? got.in!, cache_write_1h: got.cache_write_1h ?? got.cache_write_5m ?? got.in! };
  if (Object.values(values).some((v) => v < 0)) bad('A price cannot be negative.');
  return values;
}

function feeOf(body: Record<string, unknown>, fallback: number): number {
  if (body.fee_bps === undefined || body.fee_bps === null || body.fee_bps === '') return fallback;
  const fee = Number(body.fee_bps);
  if (!Number.isInteger(fee) || fee < 0 || fee > 5000) bad('`fee_bps` is 0-5000.');
  return fee;
}

function providerParam(value: unknown): Provider {
  if (!isProvider(value)) bad(`Name a provider: ${PROVIDERS.join(', ')}.`);
  return value as Provider;
}

async function requireModel(c: App, id: string): Promise<CatalogRow> {
  const model = await modelById(c.env, id);
  if (!model) throw new ApiError(404, 'not_found', `No approved model ${id}.`);
  return model;
}

/** A model on its provider's list; null when the provider has no list or is not connected. */
async function findListed(c: App, provider: Provider, providerModel: string): Promise<Listing | null> {
  if (!canList(c.env, provider)) return null;
  try {
    return (await listing(c.env, provider))?.models.find((m) => m.id === providerModel) ?? null;
  } catch (error) {
    // A direct provider's list only adds facts and a reference price: without it the route still has its list price.
    if (error instanceof NotConnected || SPECS[provider].direct) return null;
    throw new ApiError(503, 'provider_unavailable', `${provider}'s model list could not be read: ${
      String((error as Error)?.message ?? error).slice(0, 200)}`, { retry_after: 30 });
  }
}

/** A 409 for a direct provider whose list needs a key nobody has set. */
function requireListable(c: App, provider: Provider): void {
  if (canList(c.env, provider)) return;
  throw new ApiError(409, 'conflict', new NotConnected(provider).message,
    { details: { reason: 'provider_not_connected', provider } });
}

/** The flash for a route added without a price. */
const unpricedNote = (name: string, provider: Provider) => `${name} is approved, but no price could be found for it ` +
  `on ${LABELS[provider]}; set one on its page before it can serve.`;

// -- the catalogue ---------------------------------------------------------------------

aiCatalogAdmin.get('/catalog', async (c) => ok(c, await catalogView(c.env, nowSeconds())));

aiCatalogAdmin.get('/catalog/discover', async (c) => {
  const provider = providerParam(c.req.query('provider'));
  requireListable(c, provider);
  const got = await listing(c.env, provider).catch((error) => {
    throw new ApiError(503, 'provider_unavailable', `${provider}'s model list could not be read: ${
      String((error as Error)?.message ?? error).slice(0, 200)}`, { retry_after: 30 });
  });
  const q = (c.req.query('q') ?? '').trim().toLowerCase();
  const ids = (await all<{ id: string }>(c.env, 'SELECT id FROM ai_catalog')).map((r) => r.id);
  const routes = await all<{ model_id: string; provider_model: string }>(c.env,
    'SELECT model_id, provider_model FROM ai_catalog_routes WHERE provider = ?1', provider);
  const models = (got?.models ?? [])
    .filter((m) => admits(provider, m.id))
    .filter((m) => !q || m.id.toLowerCase().includes(q) || (m.name ?? '').toLowerCase().includes(q))
    .slice(0, 200)
    .map((m) => ({ ...m, suggested_id: suggestId(m.id, ids),
      catalogued_as: routes.find((r) => r.provider_model === m.id)?.model_id ?? null }));
  return ok(c, { provider, url: got?.url, total: got?.models.length ?? 0, lists_prices: listsPrices(provider), models });
});

/** The first free rank of a model, or a 409 when it has its three routes. */
function freeRank(routes: CatalogRouteRow[], wanted: number | null): number {
  if (routes.length >= MAX_ROUTES) {
    throw new ApiError(409, 'conflict', 'This model has its three provider routes; remove one first.',
      { details: { reason: 'routes_full' } });
  }
  if (wanted !== null && (wanted < 0 || wanted > 2)) bad('`rank` is 0, 1 or 2.');
  return [0, 1, 2].find((n) => !routes.some((r) => r.rank === n))!;
}

/** Add a route at the end of the chain, then move it to `wanted` if that was asked. */
async function addRoute(c: App, modelId: string, fields: Omit<Parameters<typeof insertRoute>[2], 'rank'>,
  wanted: number | null, extra: D1PreparedStatement[] = []): Promise<CatalogRouteRow[]> {
  const now = nowSeconds();
  const existing = await routesOf(c.env, modelId);
  if (existing.some((r) => r.provider === fields.provider)) {
    throw new ApiError(409, 'conflict', `${modelId} already has a ${fields.provider} route; edit that one.`,
      { details: { reason: 'route_exists' } });
  }
  const rank = freeRank(existing, wanted);
  await c.env.LICENSE_DB.batch([...extra, insertRoute(c.env, modelId, { ...fields, rank }, who(c), now),
    eventStatement(c.env, now, { actor: who(c), kind: 'ai.route_added', payload: { model_id: modelId,
      provider: fields.provider, provider_model: fields.provider_model, rank, price_source: fields.price_source } })]);
  if (wanted !== null && wanted < rank) await moveRoute(c, modelId, fields.provider, wanted);
  return routesOf(c.env, modelId);
}

aiCatalogAdmin.post('/catalog/import', async (c) => {
  const now = nowSeconds();
  const body = await readJson(c);
  const provider = providerParam(body.provider);
  const providerModel = str(body, 'provider_model', 160) ?? str(body, 'model', 160);
  if (!providerModel || !WIRE_MODEL.test(providerModel)) bad('Give `provider_model`: the model id on that provider.');
  if (!admits(provider, providerModel!)) bad(`${provider} is admitted for its confidential (-TEE) models only.`);
  requireListable(c, provider);
  const found = await findListed(c, provider, providerModel!);
  if (!found) throw new ApiError(404, 'not_found', `${provider} does not list ${providerModel}.`);
  const ids = (await all<{ id: string }>(c.env, 'SELECT id FROM ai_catalog')).map((r) => r.id);
  const id = str(body, 'id', 64) ?? suggestId(providerModel!, ids);
  if (!MODEL_ID.test(id)) bad('An approved model id is lower case letters, digits, . _ and - (at most 64).');
  const existing = await modelById(c.env, id);
  const listedEffort = existing ? null : effortFromListing(found, providerModel!, id);
  const create = existing ? [] : [upsertModel(c.env, id, fieldsFromListing(found, providerModel!, id), who(c), now),
    ...(listedEffort ? [setEffortStatement(c.env, id, listedEffort, who(c), now)] : [])];
  const priced = defaultPricing(provider, providerModel!, found, now);
  await addRoute(c, id, { provider, provider_model: providerModel!, enabled: priced ? 1 : 0, failover: 'error',
    ...(priced ?? unpricedPricing()), availability: found.available ? 'ok' : 'down', note: priced ? null : UNPRICED_NOTE },
  int(body, 'rank'), create);
  if (!existing) await record(c.env, now, { actor: who(c), kind: 'ai.model_approved', payload: { id, provider, providerModel } });
  const model = await modelById(c.env, id);
  return ok(c, { model, routes: await routesOf(c.env, id), created: !existing, unpriced: !priced,
    ...(priced ? {} : { note: unpricedNote(model?.name ?? id, provider) }) }, 201);
});

aiCatalogAdmin.post('/catalog/seed-builtin', async (c) => {
  const now = nowSeconds();
  const statements = await seedBuiltinStatements(c.env, who(c), now);
  if (statements.length) await c.env.LICENSE_DB.batch(statements);
  return ok(c, { seeded: statements.length > 0, catalog: (await catalogView(c.env, now)).models.map((m) => m.id) });
});

function modelFields(body: Record<string, unknown>, current: CatalogRow | null): ModelFields {
  const name = body.name === undefined ? current?.name ?? null : str(body, 'name', 120);
  if (!name) bad('Give the model a `name`.');
  const count = (n: string): number | null => {
    if (body[n] === undefined) return (current as Record<string, any> | null)?.[n] ?? null;
    if (body[n] === null || body[n] === '') return null;
    const v = Number(body[n]);
    if (!Number.isInteger(v) || v < 1 || v > 100_000_000) bad(`\`${n}\` is a number of tokens.`);
    return v;
  };
  const status = body.status === undefined ? current?.status ?? 'active' : body.status;
  if (status !== 'active' && status !== 'preview' && status !== 'deprecated') bad('`status` is active, preview or deprecated.');
  return {
    name: name!, family: body.family === undefined ? current?.family ?? null : str(body, 'family', 60),
    context_window: count('context_window'), max_output: count('max_output'),
    supports_vision: flag(body, 'supports_vision', current?.supports_vision ?? 1),
    supports_tools: flag(body, 'supports_tools', current?.supports_tools ?? 1),
    supports_structured: flag(body, 'supports_structured', current?.supports_structured ?? 1),
    reasoning: flag(body, 'reasoning', current?.reasoning ?? 0), status: status as CatalogRow['status'],
    enabled: flag(body, 'enabled', current?.enabled ?? 1),
    note: answered(body.note === undefined ? current?.note ?? null : str(body, 'note', 500), body),
  };
}

/** Saying whether the model reasons answers the note an import left asking it. */
function answered(note: string | null, body: Record<string, unknown>): string | null {
  return note === REASONING_UNCONFIRMED && body.reasoning !== undefined ? null : note;
}

/**
 * An admin's effort profile for a model: `effort_wire` and `effort_levels` (a list, or one comma-separated
 * string) set it, with an optional `effort_default`; `effort_wire: ""` clears it (back to the provider's list
 * or the built-in profile). Undefined when the body says nothing about effort.
 */
function effortOverride(body: Record<string, unknown>): string | null | undefined {
  if (body.effort_wire === undefined && body.effort_levels === undefined && body.effort_default === undefined) {
    return undefined;
  }
  if (body.effort_wire === '' || body.effort_wire === null) return null;
  const wire = body.effort_wire;
  if (typeof wire !== 'string' || !EFFORT_WIRES.includes(wire as EffortWire)) {
    bad(`\`effort_wire\` is ${EFFORT_WIRES.join(', ')}, or empty to use the built-in profile.`);
  }
  const raw = Array.isArray(body.effort_levels) ? body.effort_levels
    : typeof body.effort_levels === 'string' ? body.effort_levels.split(/[\s,]+/).filter(Boolean) : [];
  const unknown = raw.filter((l) => !isLevel(l));
  if (unknown.length) bad(`Unknown effort level ${unknown.join(', ')}; levels are ${LEVELS.join(', ')}.`);
  const levels = sortLevels(raw as EffortLevel[]);
  if (wire !== 'none' && !levels.length) bad('Give the `effort_levels` the model accepts, or set `effort_wire` to none.');
  const fallback = body.effort_default === undefined || body.effort_default === null || body.effort_default === '';
  if (!fallback && !isLevel(body.effort_default)) bad(`\`effort_default\` is one of ${LEVELS.join(', ')}, or empty.`);
  const stored: StoredProfile = { levels: wire === 'none' ? [] : levels,
    default: fallback ? null : body.effort_default as EffortLevel, wire: wire as EffortWire, source: 'admin' };
  return JSON.stringify(stored);
}

aiCatalogAdmin.put('/catalog/:id', async (c) => {
  const now = nowSeconds();
  const id = c.req.param('id');
  if (!MODEL_ID.test(id)) bad('An approved model id is lower case letters, digits, . _ and - (at most 64).');
  const current = await modelById(c.env, id);
  const body = await readJson(c);
  const fields = modelFields(body, current);
  const effort = effortOverride(body);
  await c.env.LICENSE_DB.batch([upsertModel(c.env, id, fields, who(c), now),
    ...(effort !== undefined ? [setEffortStatement(c.env, id, effort, who(c), now)] : []),
    eventStatement(c.env, now, { actor: who(c), kind: current ? 'ai.model_updated' : 'ai.model_approved',
      payload: { id, ...fields, ...(effort !== undefined ? { effort: effort ? JSON.parse(effort) : null } : {}) } })]);
  return ok(c, { model: await modelById(c.env, id), routes: await routesOf(c.env, id) }, current ? 200 : 201);
});

/** Who serves a pattern now, by the resolver a call uses: a model's name, the built-in default, or the v3 table. */
async function servingNow(c: App, pattern: string): Promise<{ now: string; level: string }> {
  const level = levelOf(pattern);
  const module = pattern === '*' ? null : moduleOf(pattern);
  const capability = TASKS[pattern]?.capability ?? Object.values(TASKS).find((t) => t.module === module)?.capability ??
    'vision_judgement';
  const r = await resolve(c.env, { task: level === 'task' ? pattern : null, feature: module, capability });
  if (r.level === 'builtin') return { now: 'built-in default', level: 'builtin' };
  if (r.level === 'legacy' || r.legacy) return { now: 'the previous route table', level: 'legacy' };
  const id = r.routes[0]?.model_id ?? '';
  return { now: (await modelById(c.env, id))?.name ?? id, level: r.level };
}

/** "Removed Claude Sonnet 5. All tasks now uses Claude Opus 5.5; QC default shadow removed." */
function removalNote(name: string, outcomes: RemovalOutcome[], legacy: boolean): string {
  const parts = outcomes.map((o) => o.outcome === 'promoted' ? `${o.label} now uses ${o.now}`
    : o.outcome === 'inherits' ? `${o.label} inherits (${o.now})` : `${o.label} shadow removed`);
  return `Removed ${name}.${parts.length ? ` ${parts.join('; ')}.` : ''}${legacy
    ? ' No task has a model assigned now, so the previous route table serves again.' : ''}`;
}

aiCatalogAdmin.delete('/catalog/:id', async (c) => {
  // Never refused; `?force=1` (what older pages sent) changes nothing.
  const now = nowSeconds();
  const model = await requireModel(c, c.req.param('id'));
  const { statements, outcomes } = await removeModelStatements(c.env, model, who(c), now);
  await c.env.LICENSE_DB.batch(statements);
  for (const o of outcomes.filter((x) => x.outcome === 'inherits')) Object.assign(o, await servingNow(c, o.task));
  const taskRouting = await hasTaskRouting(c.env);
  const legacy = !taskRouting && (await legacyRows(c.env)).routes > 0;
  return ok(c, { deleted: model.id, name: model.name, tasks: outcomes,
    unassigned: [...new Set(outcomes.map((o) => o.task))], note: removalNote(model.name, outcomes, legacy) });
});

// -- provider routes -------------------------------------------------------------------

function failoverOf(value: unknown, fallback: CatalogRouteRow['failover']): CatalogRouteRow['failover'] {
  if (value === undefined || value === null || value === '') return fallback;
  if (value !== 'outage' && value !== 'error' && value !== 'never') bad('`failover` is outage, error or never.');
  return value;
}

aiCatalogAdmin.post('/catalog/:id/routes', async (c) => {
  const now = nowSeconds();
  const model = await requireModel(c, c.req.param('id'));
  const body = await readJson(c);
  const provider = providerParam(body.provider);
  const providerModel = str(body, 'provider_model', 160) ?? str(body, 'model', 160);
  if (!providerModel || !WIRE_MODEL.test(providerModel)) bad('Give `provider_model`: the model id on that provider.');
  if (!admits(provider, providerModel!)) bad(`${provider} is admitted for its confidential (-TEE) models only.`);
  const given = prices(body);
  let pricing;
  if (given) {
    const source = str(body, 'source_url', 500);
    if (!source) bad('Give `source_url`: where the price was read.');
    pricing = { prices: given, fee_bps: feeOf(body, 0), price_source: 'manual' as const, priced_at: now,
      source_url: source, extra_json: null };
  } else {
    pricing = defaultPricing(provider, providerModel!, await findListed(c, provider, providerModel!), now);
  }
  // Nobody prices it: added, off, and flagged until it has a price.
  const unpriced = !pricing;
  const routes = await addRoute(c, model.id, { provider, provider_model: providerModel!, enabled: unpriced ? 0 : 1,
    failover: failoverOf(body.failover, 'error'), ...(pricing ?? unpricedPricing()),
    note: str(body, 'note', 500) ?? (unpriced ? UNPRICED_NOTE : null) },
  body.rank === undefined || body.rank === '' ? null : Number(body.rank));
  return ok(c, { model, routes, unpriced, ...(unpriced ? { note: unpricedNote(model.name, provider) } : {}) }, 201);
});

async function requireRoute(c: App, modelId: string, provider: string): Promise<CatalogRouteRow> {
  const row = await one<CatalogRouteRow>(c.env, 'SELECT * FROM ai_catalog_routes WHERE model_id = ?1 AND provider = ?2',
    modelId, provider);
  if (!row) throw new ApiError(404, 'not_found', `${modelId} has no ${provider} route.`);
  return row;
}

aiCatalogAdmin.patch('/catalog/:id/routes/:provider', async (c) => {
  const now = nowSeconds();
  const model = await requireModel(c, c.req.param('id'));
  const row = await requireRoute(c, model.id, c.req.param('provider'));
  const body = await readJson(c);
  const wasUnpriced = isUnpriced(row);
  let enabled = flag(body, 'enabled', row.enabled);
  const failover = failoverOf(body.failover, row.failover);
  let note = body.note === undefined ? row.note : str(body, 'note', 500);
  let pricing = { in: row.in_micro, cache_read: row.cache_read_micro, cache_write_5m: row.cache_write_5m_micro,
    cache_write_1h: row.cache_write_1h_micro, out: row.out_micro };
  let fee = row.fee_bps;
  let source = row.price_source;
  let sourceUrl = row.source_url;
  let pricedAt = row.priced_at;
  const given = prices(body);
  if (given) {
    sourceUrl = str(body, 'source_url', 500) ?? row.source_url;
    if (!sourceUrl) bad('Give `source_url`: where the price was read.');
    pricing = given;
    fee = feeOf(body, row.fee_bps);
    source = 'manual';
    pricedAt = now;
  } else if (body.price_source === 'api' || body.price_source === 'builtin' || body.price_source === 'auto') {
    // Back to the provider's own (or the built-in list) price, read now.
    const fresh = defaultPricing(row.provider as Provider, row.provider_model,
      await findListed(c, row.provider as Provider, row.provider_model), now);
    if (!fresh) bad(`${row.provider} lists no price for ${row.provider_model}, and there is no list price for it.`);
    ({ prices: pricing, fee_bps: fee, price_source: source, priced_at: pricedAt } = fresh!);
    sourceUrl = fresh!.source_url ?? row.source_url;
  } else if (body.fee_bps !== undefined) {
    fee = feeOf(body, row.fee_bps);
  }
  const pricedNow = wasUnpriced && pricedAt !== null;
  if (wasUnpriced && !pricedNow && enabled) bad('Give this route a price before switching it on.');
  if (pricedNow) {
    // Priced at last: on, unless the same request says off, and the waiting note goes.
    if (body.enabled === undefined) enabled = 1;
    if (note === UNPRICED_NOTE) note = null;
  }
  await c.env.LICENSE_DB.batch([
    c.env.LICENSE_DB.prepare(
      `UPDATE ai_catalog_routes SET enabled = ?3, failover = ?4, note = ?5, in_micro = ?6, cache_read_micro = ?7,
         cache_write_5m_micro = ?8, cache_write_1h_micro = ?9, out_micro = ?10, fee_bps = ?11, price_source = ?12,
         source_url = ?13, priced_at = ?14, updated_at = ?15, updated_by = ?16
       WHERE model_id = ?1 AND provider = ?2`,
    ).bind(model.id, row.provider, enabled, failover, note, pricing.in, pricing.cache_read, pricing.cache_write_5m,
      pricing.cache_write_1h, pricing.out, fee, source, sourceUrl, pricedAt, now, who(c)),
    eventStatement(c.env, now, { actor: who(c), kind: 'ai.route_updated', payload: { model_id: model.id,
      provider: row.provider, enabled, failover, price_source: source, ...(given ? { prices: given, fee_bps: fee } : {}) } }),
  ]);
  return ok(c, { model, routes: await routesOf(c.env, model.id),
    ...(pricedNow && enabled ? { note: 'Price set; the route is now on.' } : {}) });
});

aiCatalogAdmin.delete('/catalog/:id/routes/:provider', async (c) => {
  const now = nowSeconds();
  const model = await requireModel(c, c.req.param('id'));
  const row = await requireRoute(c, model.id, c.req.param('provider'));
  const rest = (await routesOf(c.env, model.id)).filter((r) => r.provider !== row.provider);
  await c.env.LICENSE_DB.batch([...reorderStatements(c.env, model.id, rest, who(c), now),
    eventStatement(c.env, now, { actor: who(c), kind: 'ai.route_removed', payload: { model_id: model.id,
      provider: row.provider, provider_model: row.provider_model } })]);
  return ok(c, { model, routes: await routesOf(c.env, model.id) });
});

async function moveRoute(c: App, modelId: string, provider: string, to: number): Promise<CatalogRouteRow[]> {
  const now = nowSeconds();
  const routes = await routesOf(c.env, modelId);
  const from = routes.findIndex((r) => r.provider === provider);
  if (from < 0) throw new ApiError(404, 'not_found', `${modelId} has no ${provider} route.`);
  if (!Number.isInteger(to) || to < 0 || to >= routes.length) bad(`\`to\` is 0-${routes.length - 1}.`);
  if (from === to) return routes;
  const ordered = [...routes];
  const [moved] = ordered.splice(from, 1);
  ordered.splice(to, 0, moved!);
  await c.env.LICENSE_DB.batch([...reorderStatements(c.env, modelId, ordered, who(c), now),
    eventStatement(c.env, now, { actor: who(c), kind: 'ai.routes_reordered', payload: { model_id: modelId,
      order: ordered.map((r) => r.provider) } })]);
  return routesOf(c.env, modelId);
}

aiCatalogAdmin.post('/catalog/:id/routes/:provider/move', async (c) => {
  const model = await requireModel(c, c.req.param('id'));
  const body = await readJson(c);
  const to = Number(body.to);
  return ok(c, { model, routes: await moveRoute(c, model.id, c.req.param('provider'), to) });
});

aiCatalogAdmin.post('/catalog/:id/routes/:provider/primary', async (c) => {
  const model = await requireModel(c, c.req.param('id'));
  return ok(c, { model, routes: await moveRoute(c, model.id, c.req.param('provider'), 0) });
});

// -- pricing ---------------------------------------------------------------------------

/** "openrouter: 466 models listed, 3 routes checked. No price changed." The flash after Refresh prices. */
function refreshNote(report: Awaited<ReturnType<typeof refreshPricing>>, changes: number): string {
  const parts = Object.entries(report).map(([p, r]) => r.ok
    ? `${p}: ${r.models_seen ? `${r.models_seen} models listed, ` : ''}${r.routes_updated} route${r.routes_updated === 1 ? '' : 's'} checked`
    : `${p}: failed (${r.error ?? 'no reason given'})`);
  const tail = changes ? `${changes} price${changes === 1 ? '' : 's'} changed.` : 'No price changed.';
  return parts.length ? `${parts.join('; ')}. ${tail}` : `Nothing to refresh. ${tail}`;
}

aiCatalogAdmin.post('/pricing/refresh', async (c) => {
  const now = nowSeconds();
  const body = await readJson(c).catch(() => ({} as Record<string, unknown>));
  const provider = body.provider === undefined || body.provider === '' ? undefined : providerParam(body.provider);
  const modelId = str(body, 'model_id', 64) ?? undefined;
  clearListingCache();
  const report = await refreshPricing(c.env, now, { provider, model_id: modelId }, who(c));
  const changes = Object.values(report).reduce((n, r) => n + r.changes, 0);
  return ok(c, { report, changes, note: refreshNote(report, changes) });
});

aiCatalogAdmin.get('/pricing/status', async (c) => ok(c, await pricingStatus(c.env, nowSeconds())));

aiCatalogAdmin.get('/pricing/history', async (c) => {
  const days = Math.max(1, Math.min(Number(c.req.query('days') ?? '90') || 90, 800));
  return ok(c, { days, changes: await priceHistory(c.env, { model_id: c.req.query('model_id') ?? null,
    provider: c.req.query('provider') ?? null, days }, nowSeconds()) });
});

// -- task assignments ------------------------------------------------------------------

aiCatalogAdmin.get('/tasks', async (c) => ok(c, await tasksView(c.env)));

function patternParam(value: string): string {
  const pattern = decodeURIComponent(value);
  if (!PATTERN.test(pattern)) bad('A task is module.task, module.* or *.');
  const level = levelOf(pattern);
  if (level === 'task' && !TASKS[pattern]) bad(`${pattern} is not a task Plexora knows; assign it to ${moduleOf(pattern)}.* instead.`);
  if (level === 'module' && !MODULES.some((m) => m.id === moduleOf(pattern))) bad(`No module ${moduleOf(pattern)}.`);
  return pattern;
}

/** The ordered model ids a body names: `models` [..], or primary / fallback_1 / fallback_2; null when neither. */
function chainOf(body: Record<string, unknown>): string[] | null {
  if (Array.isArray(body.models)) return body.models.map((m) => String(m).trim()).filter(Boolean);
  if (!('primary' in body) && !('fallback_1' in body) && !('fallback_2' in body)) return null;
  return ['primary', 'fallback_1', 'fallback_2'].map((k) => (typeof body[k] === 'string' ? (body[k] as string).trim() : ''))
    .filter(Boolean);
}

/** A tri-state from a form: true/false, or null for "the registry's". */
function tri(value: unknown): number | null {
  if (value === undefined || value === null || value === '' || value === 'inherit') return null;
  return value === true || value === 1 || value === '1' || value === 'yes' ? 1 : 0;
}

function optionalCount(body: Record<string, unknown>, name: string, max: number): number | null {
  const raw = body[name];
  if (raw === undefined || raw === null || raw === '') return null;
  const v = Number(raw);
  if (!Number.isInteger(v) || v < 1 || v > max) bad(`\`${name}\` is 1-${max}, or empty.`);
  return v;
}

/** A passing evaluation for this model on the module's current bench, if there is one. */
async function passingEvaluation(c: App, pattern: string, modelId: string, given: number | null): Promise<number | null> {
  const feature = pattern === '*' ? '*' : moduleOf(pattern);
  const bench = BENCH[feature] ?? BENCH['*']!;
  if (given !== null) {
    const e = await one<{ feature: string; model: string; bench_version: string; passed: number }>(c.env,
      'SELECT feature, model, bench_version, passed FROM ai_route_evaluations WHERE id = ?1', given);
    if (!e) throw new ApiError(409, 'route_not_publishable', 'No such evaluation.');
    if (e.feature !== feature || e.model !== modelId) {
      throw new ApiError(409, 'route_not_publishable', `That evaluation is for ${e.feature} / ${e.model}.`);
    }
    if (e.bench_version !== bench.version) {
      throw new ApiError(409, 'route_not_publishable', `That evaluation ran on bench ${e.bench_version}; ${feature} is on ${bench.version}.`);
    }
    if (!e.passed) throw new ApiError(409, 'route_not_publishable', 'That evaluation did not pass the bench.');
    return given;
  }
  const latest = await one<{ id: number }>(c.env,
    `SELECT id FROM ai_route_evaluations WHERE feature = ?1 AND model = ?2 AND bench_version = ?3 AND passed = 1
     ORDER BY evaluated_at DESC LIMIT 1`, feature, modelId, bench.version);
  return latest?.id ?? null;
}

aiCatalogAdmin.put('/tasks/:task', async (c) => {
  const now = nowSeconds();
  const pattern = patternParam(c.req.param('task'));
  const body = await readJson(c);
  const chain = chainOf(body);
  const shadowGiven = 'shadow_model' in body || Array.isArray(body.shadow);
  if (chain === null && !shadowGiven) bad('Give the models: `primary` (and `fallback_1`, `fallback_2`) or `models`.');
  if (chain && chain.length > 3) bad('A task takes a primary model and at most two fallbacks.');
  if (chain && new Set(chain).size !== chain.length) bad('The same model is named twice.');
  // `auto` is the task's own level, fitted to each model of the chain; empty (or `default`) sends nothing.
  // Left out: what the row asks now, and `auto` for a new row -- not each model's own default, which is
  // what every production task had been left on unasked (2026-10-03, AI_DEPLOY.md).
  let given: unknown = body.effort === null || body.effort === '' ? 'default' : body.effort;
  if (given === undefined) {
    const current = await one<{ effort_spec: string | null }>(c.env,
      `SELECT effort_spec FROM ai_task_routes WHERE task = ?1 AND role = 'serve' ORDER BY rank LIMIT 1`, pattern);
    given = current ? current.effort_spec ?? 'default' : 'auto';
  }
  if (typeof given !== 'string' || !SPECS_ALLOWED.includes(given)) {
    bad(`\`effort\` is auto, ${LEVELS.join(', ')}, or empty for the model's own default.`);
  }
  const effort = given === 'default' ? null : given as string;
  const cap = optionalCount(body, 'max_tokens_cap', 128_000);
  const latency = optionalCount(body, 'latency_ms', 600_000);
  const costUsd = body.max_cost_usd === undefined || body.max_cost_usd === '' || body.max_cost_usd === null ? null
    : Number(body.max_cost_usd);
  const costMicro = int(body, 'max_cost_micro') ?? (costUsd === null ? null : Math.round(costUsd * 1_000_000));
  if (costMicro !== null && (!Number.isFinite(costMicro) || costMicro <= 0)) bad('The cost cap is a positive amount, or empty.');
  const level = levelOf(pattern);
  // A task's requirements may be overridden only on the task itself.
  const reqVision = level === 'task' ? tri(body.requires_vision) : null;
  const reqReasoning = level === 'task' ? tri(body.requires_reasoning) : null;
  const evaluationId = body.evaluation_id === undefined || body.evaluation_id === null || body.evaluation_id === ''
    ? null : Number(body.evaluation_id);
  const note = str(body, 'note', 500);
  const unbenchedOk = knob(c.env, 'AI_ALLOW_UNBENCHED_ROUTES') === 1;

  const statements: D1PreparedStatement[] = [];
  const checked: Array<{ model_id: string; unbenched: number; evaluation_id: number | null }> = [];
  if (chain) {
    for (const [rank, id] of chain.entries()) {
      const model = await modelById(c.env, id);
      if (!model) throw new ApiError(409, 'route_not_publishable', `${id} is not an approved model.`);
      const why = level !== 'task' ? eligibility(model, pattern) : !model.enabled ? 'disabled' : lacks(model, {
        vision: reqVision === null ? TASKS[pattern]!.requires.vision : !!reqVision,
        reasoning: reqReasoning === null ? TASKS[pattern]!.requires.reasoning : !!reqReasoning });
      if (why) {
        throw new ApiError(409, 'route_not_publishable', `${model.name} cannot serve ${patternLabel(pattern)} (${why}).`,
          { details: { reason: 'assignment_invalid', model_id: id, why } });
      }
      const own = await routesOf(c.env, id);
      if (own.length && own.every(isUnpriced)) {
        throw new ApiError(409, 'route_not_publishable', `${model.name} has no priced provider route; set a price on ` +
          'its page.', { details: { reason: 'assignment_invalid', model_id: id, why: 'unpriced' } });
      }
      const routes = own.filter((r) => r.enabled);
      if (!routes.length) {
        throw new ApiError(409, 'route_not_publishable', `${model.name} has no provider route switched on.`,
          { details: { reason: 'assignment_invalid', model_id: id, why: 'no route' } });
      }
      const primary = routes[0]!.provider as Provider;
      let evaluation: number | null = null;
      if (needsEvaluation({ provider: isProvider(primary) ? primary : null, pattern, role: 'serve' })) {
        evaluation = await passingEvaluation(c, pattern, id, rank === 0 ? evaluationId : null);
        if (evaluation === null && !unbenchedOk) {
          throw new ApiError(409, 'route_not_publishable', `${model.name} is served through ${primary}; serving ${
            patternLabel(pattern)} with it needs a passing routing-bench evaluation for ${pattern === '*' ? 'the default'
            : moduleOf(pattern)}.`, { details: { reason: 'needs_evaluation', model_id: id } });
        }
      }
      checked.push({ model_id: id, evaluation_id: evaluation, unbenched: evaluation === null &&
        needsEvaluation({ provider: isProvider(primary) ? primary : null, pattern, role: 'serve' }) ? 1 : 0 });
    }
    statements.push(c.env.LICENSE_DB.prepare(`DELETE FROM ai_task_routes WHERE task = ?1 AND role = 'serve'`).bind(pattern));
    for (const [rank, x] of checked.entries()) {
      statements.push(c.env.LICENSE_DB.prepare(
        `INSERT INTO ai_task_routes (id, task, role, rank, model_id, effort_spec, max_tokens_cap, requires_vision,
           requires_reasoning, max_cost_micro, latency_ms, shadow_pct, unbenched, evaluation_id, enabled, note,
           updated_at, updated_by)
         VALUES (?1, ?2, 'serve', ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 0, ?11, ?12, 1, ?13, ?14, ?15)`,
      ).bind(newId('tr'), pattern, rank, x.model_id, effort, cap, reqVision, reqReasoning, costMicro, latency,
        x.unbenched, x.evaluation_id, note, now, who(c)));
    }
  }
  if (shadowGiven) {
    const shadowId = typeof body.shadow_model === 'string' ? body.shadow_model.trim() : '';
    statements.push(c.env.LICENSE_DB.prepare(`DELETE FROM ai_task_routes WHERE task = ?1 AND role = 'shadow'`).bind(pattern));
    if (shadowId) {
      const model = await modelById(c.env, shadowId);
      if (!model?.enabled) throw new ApiError(409, 'route_not_publishable', `${shadowId} is not an enabled approved model.`);
      const pct = body.shadow_pct === undefined || body.shadow_pct === '' ? 5 : Number(body.shadow_pct);
      if (!Number.isInteger(pct) || pct < 1 || pct > 100) bad('`shadow_pct` is 1-100.');
      statements.push(c.env.LICENSE_DB.prepare(
        `INSERT INTO ai_task_routes (id, task, role, rank, model_id, shadow_pct, enabled, note, updated_at, updated_by)
         VALUES (?1, ?2, 'shadow', 0, ?3, ?4, 1, ?5, ?6, ?7)`,
      ).bind(newId('tr'), pattern, shadowId, pct, note, now, who(c)));
    }
  }
  statements.push(eventStatement(c.env, now, { actor: who(c), kind: 'ai.task_assigned', payload: { task: pattern,
    models: chain, effort, max_tokens_cap: cap, max_cost_micro: costMicro, latency_ms: latency,
    requires_vision: reqVision, requires_reasoning: reqReasoning, shadow: shadowGiven ? body.shadow_model ?? null : undefined } }));
  await c.env.LICENSE_DB.batch(statements);
  return ok(c, { task: pattern, assignments: await all<TaskRouteRow>(c.env,
    'SELECT * FROM ai_task_routes WHERE task = ?1 ORDER BY role, rank', pattern) });
});

aiCatalogAdmin.delete('/tasks/:task', async (c) => {
  const now = nowSeconds();
  const pattern = decodeURIComponent(c.req.param('task'));
  if (!PATTERN.test(pattern)) bad('A task is module.task, module.* or *.');
  const role = c.req.query('role') ?? null;
  if (role !== null && role !== 'serve' && role !== 'shadow') bad('`role` is serve or shadow.');
  const result = await c.env.LICENSE_DB.prepare('DELETE FROM ai_task_routes WHERE task = ?1 AND (?2 IS NULL OR role = ?2)')
    .bind(pattern, role).run();
  await record(c.env, now, { actor: who(c), kind: 'ai.task_reset', payload: { task: pattern, role } });
  return ok(c, { task: pattern, removed: result.meta.changes ?? 0 });
});

// -- the migration from the v3 route table ---------------------------------------------

interface LegacyModelRow {
  provider: string; model: string; in_micro: number; cache_read_micro: number; cache_write_5m_micro: number;
  cache_write_1h_micro: number; out_micro: number; fee_bps: number; supports_structured: number;
  supports_tools: number; supports_vision: number; enabled: number; source_url: string | null; note: string | null;
}

/**
 * Carry the v3 configuration over, without changing what serves: every
 * catalogued (provider, model) becomes a route of an approved model (two
 * providers of one model become one model with two routes), the default
 * chain becomes the `*` assignment, and any task whose legacy chain differs
 * from it gets its own. Refused once assignments exist, unless forced.
 */
aiCatalogAdmin.post('/migrate-legacy', async (c) => {
  const now = nowSeconds();
  const body = await readJson(c).catch(() => ({} as Record<string, unknown>));
  if ((await hasTaskRouting(c.env)) && body.force !== true) {
    throw new ApiError(409, 'conflict', 'Task routing is already in use; nothing to migrate.',
      { details: { reason: 'already_migrated' } });
  }
  const actor = who(c);
  const seed = await seedBuiltinStatements(c.env, actor, now);
  if (seed.length) await c.env.LICENSE_DB.batch(seed);
  const legacyModels = await all<LegacyModelRow>(c.env, 'SELECT * FROM ai_models ORDER BY provider, model').catch(() => []);
  const legacyRoutes = await all<LegacyRouteRow>(c.env,
    'SELECT * FROM ai_routes WHERE enabled = 1 ORDER BY feature, capability, role, rank').catch(() => []);
  // provider/model -> approved model id
  const idOf = new Map<string, string>();
  const approve = async (provider: Provider, wire: string, legacy: LegacyModelRow | null) => {
    const key = `${provider}/${wire}`;
    if (idOf.has(key)) return idOf.get(key)!;
    const existingRoute = await one<{ model_id: string }>(c.env,
      'SELECT model_id FROM ai_catalog_routes WHERE provider = ?1 AND provider_model = ?2', provider, wire);
    if (existingRoute) {
      idOf.set(key, existingRoute.model_id);
      return existingRoute.model_id;
    }
    const ids = (await all<{ id: string }>(c.env, 'SELECT id FROM ai_catalog')).map((r) => r.id);
    const id = suggestId(wire, ids);
    // What the provider lists for it (best effort): its name, context and whether it reasons. The abilities
    // an admin catalogued it with win.
    let found: Listing | null = null;
    try {
      found = canList(c.env, provider) ? (await listing(c.env, provider))?.models.find((m) => m.id === wire) ?? null
        : null;
    } catch {
      found = null;
    }
    if (!(await modelById(c.env, id))) {
      const base = found ? fieldsFromListing(found, wire, id) : { name: wire.split('/').pop() ?? wire,
        family: wire.includes('/') ? wire.split('/')[0]! : null, context_window: null, max_output: null,
        supports_vision: 1, supports_tools: 1, supports_structured: 1, reasoning: 0, status: 'active' as const,
        enabled: 1, note: null };
      await upsertModel(c.env, id, { ...base, supports_vision: legacy?.supports_vision ?? base.supports_vision,
        supports_tools: legacy?.supports_tools ?? base.supports_tools,
        supports_structured: legacy?.supports_structured ?? base.supports_structured, enabled: legacy?.enabled ?? 1,
        note: 'migrated from the v3 route table' }, actor, now).run();
    }
    const routes = await routesOf(c.env, id);
    if (!routes.some((r) => r.provider === provider) && routes.length < MAX_ROUTES) {
      // A price imported from OpenRouter's list stays the provider's (refreshed nightly); one typed by hand
      // stays typed.
      const listed = found !== null && provider === 'openrouter' &&
        !!legacy?.source_url?.includes('openrouter.ai/api/v1/models');
      const unit = legacy ? { in: legacy.in_micro, cache_read: legacy.cache_read_micro,
        cache_write_5m: legacy.cache_write_5m_micro, cache_write_1h: legacy.cache_write_1h_micro, out: legacy.out_micro }
        : defaultPricing(provider, wire, null, now)?.prices;
      if (unit) {
        await insertRoute(c.env, id, { provider, provider_model: wire, rank: freeRank(routes, null), enabled: 1,
          failover: 'error', prices: unit, fee_bps: legacy?.fee_bps ?? 0, extra_json: null,
          price_source: listed ? 'api' : legacy ? 'manual' : 'builtin', priced_at: now,
          source_url: legacy?.source_url ?? null, note: legacy?.note ?? null }, actor, now).run();
      }
    }
    idOf.set(key, id);
    return id;
  };
  for (const m of legacyModels) if (isProvider(m.provider)) await approve(m.provider, m.model, m);
  for (const r of legacyRoutes) {
    if (isProvider(r.provider)) await approve(r.provider, r.model, legacyModels.find((m) => m.provider === r.provider &&
      m.model === r.model) ?? null);
  }

  // The legacy chain each (feature, capability) resolved to, as approved model ids in order.
  const chainFor = (feature: string, capability: string, role: 'serve' | 'shadow'): LegacyRouteRow[] => {
    const own = legacyRoutes.filter((r) => r.role === role && r.capability === capability && r.feature === feature);
    if (own.length || feature === '*') return own;
    return legacyRoutes.filter((r) => r.role === role && r.capability === capability && r.feature === '*');
  };
  const idsOf = (rows: LegacyRouteRow[]) => [...new Set(rows.filter((r) => isProvider(r.provider))
    .map((r) => idOf.get(`${r.provider}/${r.model}`)!).filter(Boolean))].slice(0, 3);
  const builtinIds = (capability: string) => [ROUTES[capability as keyof typeof ROUTES]?.model].filter(Boolean) as string[];

  const writes: D1PreparedStatement[] = [];
  const assigned: Record<string, string[]> = {};
  const assign = (pattern: string, rows: LegacyRouteRow[], ids: string[]) => {
    const head = rows[0];
    ids.forEach((id, rank) => writes.push(c.env.LICENSE_DB.prepare(
      `INSERT INTO ai_task_routes (id, task, role, rank, model_id, effort, max_tokens_cap, shadow_pct, unbenched,
         evaluation_id, enabled, note, updated_at, updated_by)
       VALUES (?1, ?2, 'serve', ?3, ?4, ?5, ?6, 0, ?7, NULL, 1, 'migrated from the v3 route table', ?8, ?9)`,
    ).bind(newId('tr'), pattern, rank, id, head?.effort ?? null, head?.max_tokens_cap ?? null, head?.unbenched ?? 0,
      now, actor)));
    assigned[pattern] = ids;
  };
  writes.push(c.env.LICENSE_DB.prepare('DELETE FROM ai_task_routes'));
  // The default: what a gating or QC look resolved to with no feature of its own.
  const defaultCapability = ['vision_judgement', 'vision_routine', 'text_reasoning', 'text_routine']
    .find((cap) => chainFor('*', cap, 'serve').length) ?? 'vision_judgement';
  const globalRows = chainFor('*', defaultCapability, 'serve');
  const globalIds = globalRows.length ? idsOf(globalRows) : builtinIds(defaultCapability);
  assign('*', globalRows, globalIds);
  for (const task of Object.values(TASKS)) {
    const rows = chainFor(task.module, task.capability, 'serve');
    const ids = rows.length ? idsOf(rows) : builtinIds(task.capability);
    if (ids.join() !== globalIds.join() && ids.length) assign(task.id, rows, ids);
  }
  for (const r of legacyRoutes.filter((x) => x.role === 'shadow' && isProvider(x.provider))) {
    const pattern = r.feature === '*' ? '*' : `${r.feature}.*`;
    if (!PATTERN.test(pattern)) continue;
    writes.push(c.env.LICENSE_DB.prepare(
      `INSERT OR IGNORE INTO ai_task_routes (id, task, role, rank, model_id, shadow_pct, enabled, note, updated_at,
         updated_by) VALUES (?1, ?2, 'shadow', 0, ?3, ?4, 1, 'migrated from the v3 route table', ?5, ?6)`,
    ).bind(newId('tr'), pattern, idOf.get(`${r.provider}/${r.model}`), r.shadow_pct, now, actor));
  }
  writes.push(eventStatement(c.env, now, { actor, kind: 'ai.legacy_migrated', payload: { assigned,
    models: [...new Set(idOf.values())], legacy: await legacyRows(c.env) } }));
  await c.env.LICENSE_DB.batch(writes);
  return ok(c, { assigned, models: [...new Set(idOf.values())] });
});

aiCatalogAdmin.get('/schema', async (c) => {
  const version = await one<{ v: number }>(c.env, 'SELECT MAX(version) AS v FROM schema_migrations');
  const columns = (await all<{ name: string }>(c.env, 'PRAGMA table_info(ai_requests)')).map((r) => r.name);
  const has = async (table: string, column: string) => (await all<{ name: string }>(c.env, `PRAGMA table_info(${table})`))
    .some((r) => r.name === column);
  const legacy = await legacyRows(c.env);
  const taskRouting = await hasTaskRouting(c.env);
  return ok(c, { schema_version: version?.v ?? null, task_routing: taskRouting,
    legacy: { ...legacy, serving: !taskRouting && legacy.routes > 0 },
    ai_requests_has_task_columns: columns.includes('task') && columns.includes('model_id'),
    effort_columns: columns.includes('effort') && await has('ai_catalog', 'effort_json') &&
      await has('ai_task_routes', 'effort_spec'),
    provider_keys_table: (await all(c.env, `SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ai_provider_keys'`))
      .length > 0,
    dismissals_table: (await all(c.env, `SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'ai_dismissals'`))
      .length > 0,
    providers: PROVIDERS.map((p) => ({ provider: p, direct: SPECS[p].direct, lists_prices: listsPrices(p),
      can_list: canList(c.env, p) })) });
});
