/** @jsxImportSource hono/jsx */
/**
 * /admin/ai: which model answers each kind of Plexora AI call, and changing it.
 *
 * Built around the question "what serves users right now, and how do I move
 * it?": the serving table first, then one form that points every capability at
 * one model, the route table, the model catalogue (with OpenRouter's own prices
 * imported), the providers with their kill switches, and 30 days of use.
 * Every action calls /admin/api/ai; nothing here writes directly.
 */
import { Hono } from 'hono';

import { CAPABILITIES } from '../ai/catalog';
import { configured, PROVIDERS, SPECS } from '../ai/providers';
import { candidates, type RouteRow } from '../ai/routing';
import { describe as describeSettings } from '../ai/settings';
import { all } from '../db';
import { DAY, knob, nowSeconds } from '../env';
import { type AppEnv, page } from '../http';
import {
  Action, Badge, Card, CheckField, Disclosure, Empty, Field, JsonForm, Note, SelectField, Stat, Table,
} from '../ui/components';
import { dateTime, relative } from '../ui/format';
import { actorLabel, shell } from './adminShell';

export const adminAi = new Hono<AppEnv>();

const API = '/admin/api/ai';

const CAPABILITY_LABELS: Record<string, string> = {
  vision_judgement: 'Vision, judgement (gating and QC looks)',
  vision_routine: 'Vision, routine (chat with images)',
  text_routine: 'Text, routine',
  text_reasoning: 'Text, reasoning (chat)',
};

interface ModelRow {
  provider: string; model: string; in_micro: number; cache_read_micro: number; cache_write_5m_micro: number;
  cache_write_1h_micro: number; out_micro: number; fee_bps: number; enabled: number; source_url: string;
  supports_structured: number; supports_tools: number; supports_vision: number; updated_at: number;
  updated_by: string | null;
}

interface CircuitRow { route_key: string; forced: string | null; reason: string | null; open_until: number;
  fail: number; ok: number; updated_at: number }

interface UsageRow { provider: string; model: string; calls: number; failed: number; cost_micro: number;
  charged_micro: number }

/** Dollars per 1M tokens, from micro-USD per 1M. */
function perM(micro: number): string {
  if (!micro) return '0';
  const usd = micro / 1_000_000;
  return usd < 0.01 ? usd.toPrecision(2) : usd.toFixed(2);
}

function usd(micro: number): string {
  return `$${(micro / 1_000_000).toFixed(micro && micro < 10_000 ? 4 : 2)}`;
}

const providerOptions = PROVIDERS.map((p) => ({ value: p, label: p }));

adminAi.get('/', async (c) => {
  const now = nowSeconds();
  const [routes, models, circuits, usage] = await Promise.all([
    all<RouteRow>(c.env, 'SELECT * FROM ai_routes ORDER BY role, feature, capability, rank'),
    all<ModelRow>(c.env, 'SELECT * FROM ai_models ORDER BY provider, model'),
    all<CircuitRow>(c.env, 'SELECT * FROM ai_circuits WHERE forced IS NOT NULL OR open_until > ?1 ORDER BY route_key',
      now),
    all<UsageRow>(c.env, `SELECT provider, model, COUNT(*) AS calls, SUM(status != 'ok') AS failed,
        SUM(cost_micro) AS cost_micro, SUM(charged_micro) AS charged_micro
      FROM ai_requests WHERE started_at_ms >= ?1 AND billing != 'shadow'
      GROUP BY provider, model ORDER BY calls DESC`, (now - 30 * DAY) * 1000),
  ]);
  const serving = await Promise.all(CAPABILITIES.map(async (cap) => ({ cap, routes: await candidates(c.env, null, cap) })));
  const settings = await describeSettings(c.env);
  const enabled = knob(c.env, 'AI_ENABLED') === 1;
  const unbenched = knob(c.env, 'AI_ALLOW_UNBENCHED_ROUTES') === 1;
  const keyed = new Set(PROVIDERS.filter((p) => configured(c.env, p)));
  const catalogued = new Set(models.map((m) => `${m.provider}/${m.model}`));
  const totals = usage.reduce((t, r) => ({ calls: t.calls + r.calls, cost: t.cost + (r.cost_micro ?? 0),
    charged: t.charged + (r.charged_micro ?? 0) }), { calls: 0, cost: 0, charged: 0 });

  return page(c, shell(c, 'Plexora AI', '/admin/ai', (
    <>
      <Card title={enabled ? 'Plexora AI is on' : 'Plexora AI is switched off'}
        sub={enabled ? 'Licensed seats can fetch tokens, make calls and start runs, within the limits below.'
          : 'Every token, call and run is refused; runs in progress pause, and resume once it is on.'}
        actions={enabled
          ? <Action action={`${API}/settings`} method="PUT" body={{ AI_ENABLED: 0 }} label="Switch AI off" tone="danger"
            confirm="Refuse every Plexora AI call for every account until it is switched on again?" reload />
          : <Action action={`${API}/settings`} method="PUT" body={{ AI_ENABLED: 1 }} label="Switch AI on" reload />}>
        {null}
      </Card>

      <div class="stats">
        <Stat label="Calls, 30 days" value={String(totals.calls)} />
        <Stat label="Provider cost, 30 days" value={usd(totals.cost)} />
        <Stat label="Charged to accounts" value={usd(totals.charged)} />
        <Stat label="Provider keys set" value={`${keyed.size} of ${PROVIDERS.length}`}
          sub={[...keyed].join(', ') || 'none: every call fails'} />
      </div>

      <Card title="Serving now" sub="The model each kind of call goes to first, then its fallbacks, for every feature
        without a route of its own.">
        <Table head={['Capability', 'First', 'Then']}>
          {serving.map(({ cap, routes: rs }) => {
            const [first, ...rest] = rs;
            const builtin = first?.id.startsWith('builtin:');
            return (
              <tr>
                <td>{CAPABILITY_LABELS[cap] ?? cap}<div class="sub mono">{cap}</div></td>
                <td>
                  <span class="mono">{first?.provider}/{first?.model}</span>
                  {builtin ? <div class="sub"><Badge tone="warn">built-in default</Badge> no route published</div> : null}
                  {first && !keyed.has(first.provider) ? <div class="sub"><Badge tone="bad">no {first.provider} key</Badge></div>
                    : null}
                </td>
                <td class="mono small">{rest.map((r) => `${r.provider}/${r.model}`).join(', ') || '—'}</td>
              </tr>
            );
          })}
        </Table>
        {unbenched ? null : (
          <Note warn>This Worker refuses routes to a provider other than Anthropic direct unless they passed the routing
            bench (AI_ALLOW_UNBENCHED_ROUTES = 0).</Note>
        )}
      </Card>

      <Card title="Use one model for everything" sub="Points all four capabilities at one model, at the rank you
        choose: rank 0 serves, rank 1 is the fallback. The model must be in the catalogue below first.">
        <JsonForm action={`${API}/routes/all`} submit="Publish for every capability" done="Routes published." reload>
          <div class="form-grid three">
            <SelectField label="Provider" name="provider" options={providerOptions} value="openrouter" id="all-provider" />
            <Field label="Model" name="model" required placeholder="qwen/qwen3.8-27b:free" id="all-model"
              hint={models.length ? `Catalogued: ${models.slice(0, 4).map((m) => m.model).join(', ')}${models.length > 4
                ? ', …' : ''}` : 'Nothing catalogued yet: import a model below.'} />
            <Field label="Rank" name="rank" type="number" num value={0} min={0} max={99} id="all-rank" />
          </div>
        </JsonForm>
      </Card>

      <Card title="Routes" sub="The published table. Publishing at a feature, capability, role and rank that is taken
        replaces that row.">
        {routes.length === 0 ? <Empty>No routes: every capability uses its built-in Anthropic default.</Empty> : (
          <Table head={['Feature', 'Capability', 'Rank', 'Model', 'State', '']}>
            {routes.map((r) => (
              <tr>
                <td class="mono">{r.feature}</td>
                <td class="mono small">{r.capability}{r.role === 'shadow' ? <div class="sub">shadow, {r.shadow_pct}%</div> : null}</td>
                <td>{r.rank}</td>
                <td><span class="mono">{r.provider}/{r.model}</span>
                  {catalogued.has(`${r.provider}/${r.model}`) || r.provider === 'anthropic' ? null
                    : <div class="sub"><Badge tone="bad">not catalogued</Badge></div>}
                  <div class="sub">failover {r.failover} · {actorLabel(r.updated_by ?? '')} {relative(r.updated_at, now)}</div></td>
                <td>{r.enabled ? <Badge tone="ok">on</Badge> : <Badge>off</Badge>}
                  {r.unbenched ? <div class="sub"><Badge tone="warn">unbenched</Badge></div> : null}</td>
                <td class="nowrap right">
                  <Action action={`${API}/routes/${r.id}`} method="PATCH" body={{ enabled: !r.enabled }}
                    label={r.enabled ? 'Turn off' : 'Turn on'} tone="ghost" small reload />
                  <Action action={`${API}/routes/${r.id}`} method="DELETE" label="Delete" tone="danger" small reload
                    confirm={`Delete the ${r.capability} rank ${r.rank} route to ${r.model}?`} />
                </td>
              </tr>
            ))}
          </Table>
        )}
        <Disclosure summary="Publish one route">
          <JsonForm action={`${API}/routes`} submit="Publish" done="Route published." reload>
            <div class="form-grid three">
              <SelectField label="Capability" name="capability" id="r-cap"
                options={CAPABILITIES.map((cap) => ({ value: cap, label: cap }))} />
              <Field label="Feature" name="feature" value="*" id="r-feature"
                hint="* for every feature, or gating, qc, chat…" />
              <SelectField label="Provider" name="provider" options={providerOptions} value="openrouter" id="r-provider" />
              <Field label="Model" name="model" required id="r-model" />
              <Field label="Rank" name="rank" type="number" num value={0} min={0} max={99} id="r-rank" />
              <SelectField label="Failover" name="failover" id="r-failover" value="outage" options={[
                { value: 'outage', label: 'outage: only when its circuit is open' },
                { value: 'error', label: 'error: after any failed retry' },
                { value: 'never', label: 'never' }]} />
              <SelectField label="Role" name="role" id="r-role" value="serve" options={[
                { value: 'serve', label: 'serve' }, { value: 'shadow', label: 'shadow (compare only, never billed)' }]} />
              <Field label="Shadow %" name="shadow_pct" type="number" num min={0} max={100} id="r-shadow"
                hint="Shadow routes only." />
            </div>
          </JsonForm>
        </Disclosure>
      </Card>

      <Card title="Models" sub="What each model costs Plexora, per 1M tokens. A route can only name a catalogued model;
        accounts pay this times the markup.">
        {models.length === 0 ? <Empty>Nothing catalogued. Anthropic's own models are built in.</Empty> : (
          <Table head={['Model', 'In', 'Cache read', 'Out', 'Fee', 'Supports', 'Updated']} right={[1, 2, 3, 4]}>
            {models.map((m) => (
              <tr>
                <td><span class="mono">{m.provider}/{m.model}</span>
                  {m.enabled ? null : <div class="sub"><Badge>disabled</Badge></div>}
                  <div class="sub small">{m.source_url}</div></td>
                <td class="right">${perM(m.in_micro)}</td>
                <td class="right">${perM(m.cache_read_micro)}</td>
                <td class="right">${perM(m.out_micro)}</td>
                <td class="right">{m.fee_bps ? `${(m.fee_bps / 100).toFixed(1)}%` : '—'}</td>
                <td class="small">{[m.supports_vision ? 'vision' : null, m.supports_tools ? 'tools' : null,
                  m.supports_structured ? 'structured' : null].filter(Boolean).join(', ') || 'text only'}</td>
                <td class="small nowrap">{dateTime(m.updated_at)}</td>
              </tr>
            ))}
          </Table>
        )}
        <Disclosure summary="Import a model from OpenRouter" open={models.length === 0}>
          <JsonForm action={`${API}/models/openrouter/import`} submit="Import" done="Model catalogued." reload inline>
            <Field label="OpenRouter model id" name="model" required placeholder="google/gemma-4-31b-it:free"
              id="i-model" hint="Its prices and what it supports are read from openrouter.ai now; :free models cost $0." />
          </JsonForm>
        </Disclosure>
        <Disclosure summary="Catalogue a model by hand">
          <JsonForm action={API} template={`${API}/models/{provider}/{model}`} method="PUT" submit="Catalogue"
            done="Model catalogued." reload>
            <div class="form-grid three">
              <SelectField label="Provider" name="provider" options={providerOptions} id="m-provider" />
              <Field label="Model id" name="model" required id="m-model" />
              <Field label="Source of the price" name="source_url" required id="m-source"
                placeholder="https://… pricing page" />
              <Field label="Input, $ per 1M" name="in_usd" type="number" num step="any" min={0} required id="m-in" />
              <Field label="Cache read, $ per 1M" name="cache_read_usd" type="number" num step="any" min={0} required
                id="m-cr" />
              <Field label="Output, $ per 1M" name="out_usd" type="number" num step="any" min={0} required id="m-out" />
              <Field label="Cache write 5 min, $ per 1M" name="cache_write_5m_usd" type="number" num step="any" min={0}
                required id="m-cw5" />
              <Field label="Cache write 1 h, $ per 1M" name="cache_write_1h_usd" type="number" num step="any" min={0}
                required id="m-cw1" />
              <Field label="Aggregator fee, basis points" name="fee_bps" type="number" num value={0} min={0} max={5000}
                id="m-fee" hint="OpenRouter: 550." />
            </div>
            <CheckField label="Takes images" name="supports_vision" checked />
            <CheckField label="Calls tools" name="supports_tools" checked />
            <CheckField label="Native structured output" name="supports_structured" checked
              hint="Off: the answer schema is put in the prompt instead." />
          </JsonForm>
        </Disclosure>
      </Card>

      <Card title="Providers" sub="A provider without a key is skipped. Switching one off stops every route to it at
        once; the routes stay published.">
        <Table head={['Provider', 'Kind', 'Key', '']}>
          {PROVIDERS.map((p) => {
            const forced = circuits.find((x) => x.route_key === p && x.forced);
            return (
              <tr>
                <td class="mono">{p}</td>
                <td class="small">{SPECS[p].direct ? 'direct' : 'aggregator'} · {SPECS[p].wire}</td>
                <td>{keyed.has(p) ? <Badge tone="ok">set</Badge> : <Badge>not set</Badge>}
                  {forced ? <div class="sub"><Badge tone="bad">switched off</Badge> {forced.reason ?? ''}</div> : null}</td>
                <td class="right">
                  {forced
                    ? <Action action={`${API}/providers/${p}/enable`} label="Switch on" tone="ghost" small reload />
                    : <Action action={`${API}/providers/${p}/disable`} body={{ reason: 'admin page' }} label="Switch off"
                      tone="danger" small reload confirm={`Stop every call to ${p}?`} />}
                </td>
              </tr>
            );
          })}
        </Table>
        {circuits.filter((x) => x.route_key.includes(':')).length ? (
          <Table head={['Model', 'State', '']}>
            {circuits.filter((x) => x.route_key.includes(':')).map((x) => (
              <tr>
                <td class="mono">{x.route_key}</td>
                <td>{x.forced ? <Badge tone="bad">switched off</Badge> : <Badge tone="warn">circuit open</Badge>}
                  <div class="sub">{x.reason ?? `${x.fail} failures`} · {relative(x.updated_at, now)}</div></td>
                <td class="right">{x.forced
                  ? <Action action={`${API}/providers/${encodeURIComponent(x.route_key)}/enable`} label="Switch on"
                    tone="ghost" small reload /> : null}</td>
              </tr>
            ))}
          </Table>
        ) : null}
        <p class="hint">Provider keys are Worker secrets: <span class="mono">wrangler secret put OPENROUTER_API_KEY</span>.
          They are never shown here or stored in D1.</p>
      </Card>

      <Card title="Limits and settings" sub="Apply within seconds, with no redeploy. An empty field uses the value shown
        in it (wrangler.toml, or the code's default); type a number to override it, clear it to go back.">
        {(['Access', 'Limits', 'Credit', 'Reliability', 'Retention'] as const).map((group) => {
          const rows = settings.filter((x) => x.group === group && x.name !== 'AI_ENABLED');
          if (!rows.length) return null;
          return (
            <Disclosure summary={group} open={group === 'Limits' || group === 'Credit'}>
              <JsonForm action={`${API}/settings`} method="PUT" submit={`Save ${group.toLowerCase()}`} done="Saved."
                reload>
                <div class="form-grid three">
                  {rows.map((x) => x.flag ? (
                    <SelectField label={x.label} name={x.name} id={`s-${x.name}`}
                      value={x.source === 'admin' ? String(x.value) : 'default'}
                      hint={x.help} options={[
                        { value: 'default', label: `Default (${x.fallback ? 'on' : 'off'})` },
                        { value: '1', label: 'On' }, { value: '0', label: 'Off' }]} />
                  ) : (
                    <Field label={`${x.label}${x.unit && !x.label.toLowerCase().includes(x.unit) ? `, ${x.unit}` : ''}`} name={x.name} type="number" step="any"
                      keepEmpty min={x.min} max={x.max} id={`s-${x.name}`}
                      value={x.source === 'admin' ? x.shown : undefined} placeholder={String(x.fallback_shown)}
                      hint={<>{x.help} {x.source === 'admin'
                        ? <Badge tone="accent">set here</Badge> : <span class="muted">({x.source})</span>}</>} />
                  ))}
                </div>
              </JsonForm>
            </Disclosure>
          );
        })}
        <p class="hint">An account's own daily limits are set on its licence page.</p>
      </Card>

      <Card title="Use by model, 30 days">
        {usage.length === 0 ? <Empty>No calls yet.</Empty> : (
          <Table head={['Model', 'Calls', 'Failed', 'Cost', 'Charged']} right={[1, 2, 3, 4]}>
            {usage.map((u) => (
              <tr>
                <td class="mono">{u.provider}/{u.model}</td>
                <td class="right">{u.calls}</td>
                <td class="right">{u.failed}</td>
                <td class="right">{usd(u.cost_micro ?? 0)}</td>
                <td class="right">{usd(u.charged_micro ?? 0)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  ), { lede: <>Which model answers Plexora AI calls, what it costs, and the switches to change it.</> }));
});
