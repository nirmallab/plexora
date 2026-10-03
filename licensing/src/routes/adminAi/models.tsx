/** @jsxImportSource hono/jsx */
/**
 * /admin/ai/models, step 2: the approved models, each with the providers that
 * reach it.
 *
 * A model is one model whoever serves it; its routes are the up to three
 * providers that reach it, tried in order (the chain on its row: the first is
 * the primary, the rest its fallbacks). The row folds out to reorder, switch
 * off or remove a provider, or add one. Making a provider primary moves every
 * task that uses the model at once, and changes no answer. A model is added
 * from any connected provider's list, Anthropic's and OpenAI's included; one
 * nobody prices is added with its route off until it has a price.
 * /models/:id is the advanced page: effort profile, prices by hand, history.
 */
import { BUILTIN_MODELS } from '../../ai/catalog';
import { MAX_ROUTES, suggestId } from '../../ai/catalog_store';
import { EFFORT_WIRES, type EffortWire, LEVELS } from '../../ai/effort';
import { canList, type Listing, listing, NotConnected } from '../../ai/pricing';
import { admits, configured, isProvider, LABELS, type Provider, PROVIDERS, SPECS } from '../../ai/providers';
import { patternLabel } from '../../ai/tasks';
import { catalogView, type EffortView, type ModelView, priceHistory, summary } from '../../ai/views';
import { nowSeconds } from '../../env';
import { ApiError, type App, page } from '../../http';
import {
  Action, Badge, CheckField, DefinitionList, Disclosure, Empty, Field, Icon, IconButton, IconLink, JsonForm, Note,
  Section, SelectField, Seg, Table, Toolbar,
} from '../../ui/components';
import { ago, dateTime, ms, perMillion, tokens } from '../../ui/format';
import {
  Abilities, aiShell, API, BASE, CachePrice, caps, Chain, domId, ModelState, Price, RANK_LABELS, RouteState, sourceLabel,
} from './shared';

const MODELS = `${BASE}/models`;

function filterOf(value: string | undefined): 'all' | 'used' | 'unused' | 'off' {
  return value === 'used' || value === 'unused' || value === 'off' ? value : 'all';
}

export async function modelsPage(c: App) {
  const now = nowSeconds();
  const view = await catalogView(c.env, now);
  const s = await summary(c.env, now, { catalog: view });
  const filter = filterOf(c.req.query('show'));
  const provider = c.req.query('provider');
  const focus = c.req.query('model') ?? null;
  const models = view.models.filter((m) => (filter === 'all' || (filter === 'used' ? m.used_by.length > 0
    : filter === 'unused' ? m.enabled && !m.used_by.length : !m.enabled)) &&
    (!provider || m.routes.some((r) => r.provider === provider)));
  const counts = { used: view.models.filter((m) => m.used_by.length).length,
    unused: view.models.filter((m) => m.enabled && !m.used_by.length).length,
    off: view.models.filter((m) => !m.enabled).length };
  const missingBuiltin = BUILTIN_MODELS.filter((b) => !view.models.some((m) => m.id === b.id));
  const listable = PROVIDERS.filter((p) => canList(c.env, p));
  const add = c.req.query('add');
  const q = c.req.query('q') ?? '';
  const chosen = add && isProvider(add) ? add : listable.find((p) => SPECS[p].direct) ?? listable[0] ?? 'openrouter';
  const cols = 8;

  return page(c, await aiShell(c, MODELS, (
    <>
      {!PROVIDERS.some((p) => configured(c.env, p)) ? (
        <div class="next-step"><Icon name="info" />No provider is connected yet: <a href={`${BASE}/providers`}>connect
          one</a> first, then add its models here.</div>
      ) : view.models.length && !view.models.some((m) => m.used_by.length) ? (
        <div class="next-step"><Icon name="info" />Next: <a href={`${BASE}/tasks`}>choose the general model</a> and
          what each task uses.</div>
      ) : null}
      <Toolbar>
        <span class="grow"><b>{view.models.length}</b> approved model{view.models.length === 1 ? '' : 's'}
          {provider ? <> served by <b>{provider}</b> · <a href={MODELS}>all providers</a></> : null}</span>
        <Seg label="Show" active={`${MODELS}?show=${filter}`} items={[
          [`${MODELS}?show=all`, `All ${view.models.length}`], [`${MODELS}?show=used`, `In use ${counts.used}`],
          [`${MODELS}?show=unused`, `Unused ${counts.unused}`], [`${MODELS}?show=off`, `Disabled ${counts.off}`]]} />
      </Toolbar>
      {models.length === 0 ? <Empty>{view.models.length ? 'No model matches.' : 'No model is approved yet: add one below.'}</Empty> : (
        <Table class="dense tight" head={['Model', 'Abilities', 'Context', 'Providers', 'Price / M', 'State', 'Used by', '']}>
          {models.map((m) => <ModelRow c={c} model={m} now={now} open={focus === m.id} cols={cols} />)}
        </Table>
      )}

      <Section title="Add a model" id="add">
        <form method="get" action={MODELS} class="inline-form">
          <select name="add" aria-label="Provider">
            {PROVIDERS.map((p) => <option value={p} selected={p === chosen ? true : undefined}
              disabled={canList(c.env, p) ? undefined : true}>{LABELS[p]}{canList(c.env, p) ? '' : ' (connect first)'}</option>)}
          </select>
          <input name="q" value={q} placeholder="Search its models: opus, gpt, gemma…" aria-label="Search" />
          <button type="submit" class="ghost tiny">Search</button>
          {missingBuiltin.length ? <Action action={`${API}/catalog/seed-builtin`} label="Add the built-in Claude models"
            icon="plus" tone="ghost" small reload done="Added." /> : null}
        </form>
        {add ? await Results({ c, provider: add, q }) : null}
        <Disclosure summary="Add a model by hand" open={view.models.length === 0 && !add && !listable.length}>
          <JsonForm action={API} template={`${API}/catalog/{id}`} method="PUT" submit="Approve model"
            next={`${MODELS}?model={model.id}`}>
            <div class="form-grid three">
              <Field label="Id" name="id" required placeholder="claude-opus-5-5" hint="Lower case, digits, . _ -" />
              <Field label="Name" name="name" required placeholder="Claude Opus 5.5" />
              <Field label="Family" name="family" placeholder="Claude" />
              <Field label="Context window, tokens" name="context_window" type="number" num min={1} />
              <Field label="Max output, tokens" name="max_output" type="number" num min={1} />
            </div>
            <ModelChecks />
          </JsonForm>
        </Disclosure>
      </Section>
    </>
  ), { summary: s }));
}

function ModelChecks(props: { model?: ModelView }) {
  const m = props.model;
  return (
    <div class="form-grid">
      <CheckField label="Takes images" name="supports_vision" checked={m ? !!m.supports_vision : true} />
      <CheckField label="Calls tools" name="supports_tools" checked={m ? !!m.supports_tools : true} />
      <CheckField label="Native structured output" name="supports_structured" checked={m ? !!m.supports_structured : true}
        hint="Off: the answer schema goes in the prompt instead." />
      <CheckField label="Reasons (extended thinking)" name="reasoning" checked={m ? !!m.reasoning : false}
        hint="Required by the judging tasks." />
    </div>
  );
}

const WIRE_LABELS: Record<EffortWire, string> = {
  effort: 'thinks by default; sent output_config.effort',
  adaptive: 'thinks when asked; sent adaptive thinking and an effort',
  budget: 'sent a thinking token budget, never an effort',
  reasoning: 'sent reasoning.effort (OpenAI-style)',
  none: 'takes no effort; nothing is sent',
};

function sourceOf(e: EffortView) {
  switch (e.source) {
    case 'admin': return <Badge tone="accent">set here</Badge>;
    case 'listing': return <Badge>from the provider's list</Badge>;
    case 'builtin': return <Badge>built-in profile</Badge>;
    case 'generic': return <Badge tone="warn">generic: reasons, levels not known</Badge>;
    default: return <Badge tone="warn">unknown: no effort is sent</Badge>;
  }
}

/** How the model takes effort, where that came from, and the form that sets it by hand. */
function EffortSection(props: { model: ModelView }) {
  const m = props.model;
  const e = m.effort;
  const budgets = e.wire === 'budget' && e.budgets ? Object.entries(e.budgets)
    .map(([level, n]) => `${level} ${n ? tokens(n) : 'off'}`).join(' · ') : null;
  return (
    <Section title="Effort" actions={e.source === 'admin' || e.source === 'listing' ? (
      <Action action={`${API}/catalog/${m.id}`} method="PUT" body={{ effort_wire: '' }} tone="ghost" small reload
        label={e.builtin ? 'Use the built-in profile' : 'Clear'} done="Effort profile cleared." />
    ) : null}>
      <DefinitionList items={[
        ['Source', <>{sourceOf(e)}{e.builtin ? <div class="sub">{e.source === 'builtin' ? '' : 'built-in: '}{e.builtin.label},
          checked {e.builtin.verified} · <a href={e.builtin.source} rel="noreferrer noopener">docs</a></div> : null}</>],
        ['Levels', e.levels.length ? e.levels.join(' · ') : 'none'],
        ['When none is sent', e.default === 'none' ? 'no thinking' : e.default ?? 'not known'],
        ['On the wire', <>{WIRE_LABELS[e.wire]}{budgets ? <div class="sub">{budgets}</div> : null}</>],
      ]} />
      <p class="hint">A task asks for one level (or “auto”: its own); this model is sent the nearest level listed here,
        or nothing. Correct it here when the model's maker changes what it takes.</p>
      <Disclosure summary="Set by hand">
        <JsonForm action={`${API}/catalog/${m.id}`} method="PUT" submit="Save effort" reload done="Effort profile saved.">
          <div class="form-grid three">
            <SelectField label="Takes effort as" name="effort_wire" value={e.wire} options={EFFORT_WIRES.map((w) => ({
              value: w, label: `${w}: ${WIRE_LABELS[w]}` }))} />
            <Field label="Levels" name="effort_levels" value={e.levels.join(', ')}
              placeholder="low, medium, high" hint={`Of ${LEVELS.join(', ')}.`} />
            <SelectField label="Its default" name="effort_default" keepEmpty value={e.default ?? ''}
              options={[{ value: '', label: 'not known' }, ...LEVELS.map((l) => ({ value: l, label: l }))]} />
          </div>
        </JsonForm>
      </Disclosure>
    </Section>
  );
}

function ModelRow(props: { c: App; model: ModelView; now: number; open: boolean; cols: number }) {
  const { c, model: m, cols } = props;
  const primary = m.routes[0];
  const stale = m.routes.some((r) => r.stale);
  const sub = `rt-${domId(m.id)}`;
  const used = m.used_by.length;
  const free = PROVIDERS.filter((p) => !m.routes.some((r) => r.provider === p));
  return (
    <>
      <tr class={[m.enabled ? '' : 'off', props.open ? 'hit' : ''].filter(Boolean).join(' ') || undefined}>
        <td><b>{m.name}</b>
          {m.status !== 'active' ? <> <Badge tone={m.status === 'deprecated' ? 'warn' : 'accent'}>{m.status}</Badge></> : null}
          <div class="sub mono">{m.id}</div></td>
        <td><Abilities m={m} /></td>
        <td class="nowrap small">{tokens(m.context_window)}</td>
        <td><Chain routes={m.routes} /></td>
        <td class="small">{primary && !primary.unpriced ? <Price route={primary} /> : <span class="dim">—</span>}
          {stale ? <> <Badge tone="warn">stale</Badge></> : null}</td>
        <td><ModelState m={m} /></td>
        <td class="small">{used ? <a href={`${BASE}/tasks?model=${m.id}`}>{used} task scope{used === 1 ? '' : 's'}</a>
          : <span class="dim">unused</span>}</td>
        <td class="icons">
          <IconButton icon="chevron-down" toggle={`#${sub}`} expanded={props.open} label={`Providers of ${m.name}`} />
          <IconLink icon="pencil" href={`${MODELS}/${m.id}`} label={`Edit ${m.name}`} />
          <Action action={`${API}/catalog/${m.id}`} method="PUT" body={{ enabled: !m.enabled }} icon="power" iconOnly
            tone={m.enabled ? undefined : 'accent'} label={m.enabled ? `Disable ${m.name}` : `Enable ${m.name}`} reload
            confirm={m.enabled && used ? `Disable ${m.name}? The tasks that use it fall back to their next model.` : undefined} />
          <Action action={`${API}/catalog/${m.id}`} method="DELETE" icon="x" iconOnly tone="danger"
            label={`Remove ${m.name}`} reload confirm={used ? `Remove ${m.name}? ${used} task scope${used === 1
              ? ' uses' : 's use'} it: each moves to its next model, or inherits the general model.`
              : `Remove ${m.name} and its routes?`} />
        </td>
      </tr>
      <tr class="sub" id={sub} hidden={props.open ? undefined : true}>
        <td colspan={cols}>
          {m.routes.length ? (
            <Table class="dense tight" head={['Rank', 'Provider', 'Model id', 'Price / M', 'State', 'p50', '']}>
              {m.routes.map((r, i) => (
                <tr class={r.enabled ? undefined : 'off'}>
                  <td class="rank">{RANK_LABELS[i]}</td>
                  <td>{LABELS[r.provider as Provider] ?? r.provider}<div class="sub">{isProvider(r.provider) &&
                    SPECS[r.provider].direct ? 'direct' : 'aggregator'}</div></td>
                  <td class="mono">{r.provider_model}</td>
                  <td>{r.unpriced ? <><Badge tone="warn">price needed</Badge> <a href={`${MODELS}/${m.id}#prices`}
                    class="small">set</a></> : <><Price route={r} />{r.fee_bps ? <div class="sub">+{(r.fee_bps / 100)
                    .toFixed(1)}% fee</div> : null}</>}</td>
                  <td><RouteState route={r} /></td>
                  <td class="small nowrap">{ms(r.latency_p50_ms)}</td>
                  <td class="icons">
                    {i === 0 ? <span class="icon-btn accent" title="Primary"><Icon name="star" fill /></span>
                      : <Action action={`${API}/catalog/${m.id}/routes/${r.provider}/primary`} icon="star" iconOnly
                        label={`Make ${r.provider} primary`} reload done={`${r.provider} is now primary for ${m.name}.`} />}
                    {i > 0 ? <Action action={`${API}/catalog/${m.id}/routes/${r.provider}/move`} body={{ to: i - 1 }}
                      icon="arrow-up" iconOnly label={`Move ${r.provider} up`} reload /> : null}
                    {i < m.routes.length - 1 ? <Action action={`${API}/catalog/${m.id}/routes/${r.provider}/move`}
                      body={{ to: i + 1 }} icon="arrow-down" iconOnly label={`Move ${r.provider} down`} reload /> : null}
                    {r.unpriced ? null : <Action action={`${API}/catalog/${m.id}/routes/${r.provider}`} method="PATCH"
                      body={{ enabled: !r.enabled }} icon="power" iconOnly tone={r.enabled ? undefined : 'accent'}
                      label={r.enabled ? `Switch the ${r.provider} route off` : `Switch the ${r.provider} route on`} reload />}
                    <Action action={`${API}/catalog/${m.id}/routes/${r.provider}`} method="DELETE" icon="x" iconOnly
                      tone="danger" label={`Remove the ${r.provider} route`} reload
                      confirm={`Remove the ${r.provider} route of ${m.name}?`} />
                  </td>
                </tr>
              ))}
            </Table>
          ) : <p class="small muted">No provider reaches this model yet: add one.</p>}
          {m.routes.length < MAX_ROUTES && free.length ? (
            <JsonForm inline action={`${API}/catalog/${m.id}/routes`} submit="Add provider" reload>
              <select name="provider" aria-label="Provider">
                {free.map((p) => <option value={p} disabled={configured(c.env, p) ? undefined : true}>{LABELS[p]}
                  {configured(c.env, p) ? '' : ' (connect first)'}</option>)}
              </select>
              <input name="provider_model" required aria-label="Model id on that provider"
                placeholder={m.routes[0]?.provider_model ?? m.id} />
              <a href={`${MODELS}/${m.id}#prices`} class="small">prices by hand ›</a>
            </JsonForm>
          ) : null}
          {m.note ? <div class="hint">{m.note}</div> : null}
        </td>
      </tr>
    </>
  );
}

const PRICE_FROM: Record<NonNullable<Listing['price_from']>, string> = {
  listing: 'listed', builtin: 'list price', reference: 'OpenRouter reference',
};

/** A provider's listing, searched, each row one click from approved. */
async function Results(props: { c: App; provider: string; q: string }) {
  const { c, provider, q } = props;
  if (!isProvider(provider)) return <Note warn>Choose a provider.</Note>;
  if (!canList(c.env, provider)) {
    return <Note warn>{new NotConnected(provider).message} <a href={`${BASE}/providers`}>Connect {LABELS[provider]}</a>.</Note>;
  }
  let got;
  try {
    got = await listing(c.env, provider);
  } catch (error) {
    return <Note warn>{LABELS[provider]}'s model list could not be read: {String((error as Error)?.message ?? error)}</Note>;
  }
  const view = await catalogView(c.env, nowSeconds());
  const ids = view.models.map((m) => m.id);
  const needle = q.trim().toLowerCase();
  const found = (got?.models ?? []).filter((m) => admits(provider, m.id) &&
    (!needle || m.id.toLowerCase().includes(needle) || (m.name ?? '').toLowerCase().includes(needle)));
  const shown = found.slice(0, 40);
  const here = (id: string) => view.models.find((m) => m.routes.some((r) => r.provider === provider && r.provider_model === id));
  return (
    <div class="section">
      <div class="section-head"><h3>{LABELS[provider]}: {found.length} match{found.length === 1 ? '' : 'es'}
        {found.length > shown.length ? `, first ${shown.length}` : ''}</h3>
        <IconLink icon="x" href={MODELS} label="Close the results" /></div>
      {shown.length === 0 ? <Empty>Nothing matches “{q}”.</Empty> : (
        <Table class="dense tight" head={['Model', 'Price / M', 'Context', 'Abilities', '']}>
          {shown.map((m) => {
            const existing = here(m.id);
            const suggested = suggestId(m.id, ids);
            return (
              <tr>
                <td><span class="mono">{m.id}</span>{m.name ? <div class="sub">{m.name}</div> : null}</td>
                <td class="price">{m.prices ? <>{m.prices.in || m.prices.out
                  ? `$${perMillion(m.prices.in)} / $${perMillion(m.prices.out)}` : 'free'}
                  <div class="sub">{m.price_from ? PRICE_FROM[m.price_from] : ''}{m.fee_bps
                    ? ` · +${(m.fee_bps / 100).toFixed(1)}% fee` : ''}</div></>
                  : <Badge tone="warn">price needed</Badge>}</td>
                <td class="small">{tokens(m.context_window)}</td>
                <td><Abilities m={m} /></td>
                <td class="icons">{existing ? <a href={`${MODELS}?model=${existing.id}`} class="small">{existing.name}</a> : (
                  <Action action={`${API}/catalog/import`} body={{ provider, provider_model: m.id }} icon="plus" iconOnly
                    tone="accent" label={ids.includes(suggested) ? `Add ${m.id} as a route of ${suggested}` : `Add ${m.id}`}
                    next={`${MODELS}?model={model.id}`} />
                )}</td>
              </tr>
            );
          })}
        </Table>
      )}
    </div>
  );
}

// -- one model -------------------------------------------------------------------------

export async function modelPage(c: App) {
  const now = nowSeconds();
  const id = c.req.param('id') ?? '';
  const view = await catalogView(c.env, now);
  const m = view.models.find((x) => x.id === id);
  if (!m) throw new ApiError(404, 'not_found', `No approved model ${id}.`);
  const history = await priceHistory(c.env, { model_id: m.id, days: 365 }, now);
  const free = PROVIDERS.filter((p) => !m.routes.some((r) => r.provider === p));

  return page(c, await aiShell(c, MODELS, (
    <>
      <div class="section-head">
        <h2>{m.name} <span class="mono muted small">{m.id}</span>
          {m.status !== 'active' ? <> <Badge tone={m.status === 'deprecated' ? 'warn' : 'accent'}>{m.status}</Badge></> : null}
          {m.enabled ? null : <> <Badge>disabled</Badge></>}</h2>
        <div class="actions">
          <a href={`${MODELS}?model=${m.id}`} class="small">All models</a>
          <Action action={`${API}/catalog/${m.id}`} method="PUT" body={{ enabled: !m.enabled }} icon="power" iconOnly
            tone={m.enabled ? undefined : 'accent'} reload label={m.enabled ? `Disable ${m.name}` : `Enable ${m.name}`}
            confirm={m.enabled && m.used_by.length ? `Disable ${m.name}? The tasks that use it fall back to their next model.`
              : undefined} />
          <Action action={`${API}/catalog/${m.id}`} method="DELETE" icon="x" iconOnly tone="danger" label={`Remove ${m.name}`}
            next={MODELS} confirm={m.used_by.length ? `Remove ${m.name}? ${m.used_by.map(patternLabel).join(', ')
            } move to their next model, or inherit the general model.` : `Remove ${m.name} and its routes?`} />
        </div>
      </div>
      <DefinitionList items={[
        ['Family', m.family ?? '—'],
        ['Context', `${tokens(m.context_window)} in · ${tokens(m.max_output)} out`],
        ['Abilities', caps(m)],
        ['Used by', m.used_by.length ? <>{m.used_by.map((p, i) => <>{i ? ' · ' : ''}<a
          href={`${BASE}/tasks?edit=${encodeURIComponent(p)}`}>{patternLabel(p)}</a></>)}</> : 'no task yet'],
      ]} />

      <EffortSection model={m} />

      <Section title="Provider routes" actions={m.routes.some((r) => r.price_source !== 'manual') ? (
        <Action action={`${API}/pricing/refresh`} body={{ model_id: m.id }} icon="refresh" label="Refresh prices"
          tone="ghost" small reload />
      ) : null}>
        <p class="hint">Tried in this order. A failure on one moves the call to the next provider of the same model;
          only when every route is out does a task fall back to its next model.</p>
        <Table class="dense" head={['Rank', 'Provider', 'Model id', 'Price', 'Fee', 'State', 'p50', 'Priced', '']}>
          {m.routes.map((r, i) => (
            <tr class={r.enabled ? undefined : 'off'}>
              <td class="rank">{RANK_LABELS[i]}</td>
              <td><span class="mono">{r.provider}</span>
                <div class="sub">{isProvider(r.provider) && SPECS[r.provider].direct ? 'direct' : 'aggregator'}</div></td>
              <td class="mono">{r.provider_model}</td>
              <td>{r.unpriced ? <Badge tone="warn">price needed</Badge> : <><Price route={r} unit /><CachePrice route={r} /></>}</td>
              <td>{r.fee_bps ? `+${(r.fee_bps / 100).toFixed(1)}%` : '—'}</td>
              <td><RouteState route={r} />{r.failover !== 'error' ? <div class="sub">fails over on {r.failover}</div> : null}</td>
              <td class="nowrap">{ms(r.latency_p50_ms)}</td>
              <td class="small nowrap">{r.unpriced ? <span class="dim">never</span> : ago(r.priced_at, now)}
                {r.stale ? <> <Badge tone="warn">stale</Badge></> : null}
                <div class="sub">{r.unpriced ? 'no price found' : sourceLabel(r.price_source)}</div></td>
              <td class="icons">
                {i === 0 ? <span class="icon-btn accent" title="Primary"><Icon name="star" fill /></span>
                  : <Action action={`${API}/catalog/${m.id}/routes/${r.provider}/primary`} icon="star" iconOnly
                    label={`Make ${r.provider} primary`} reload done={`${r.provider} is now primary for ${m.name}.`} />}
                {i > 0 ? <Action action={`${API}/catalog/${m.id}/routes/${r.provider}/move`} body={{ to: i - 1 }}
                  icon="arrow-up" iconOnly label={`Move ${r.provider} up`} reload /> : null}
                {i < m.routes.length - 1 ? <Action action={`${API}/catalog/${m.id}/routes/${r.provider}/move`}
                  body={{ to: i + 1 }} icon="arrow-down" iconOnly label={`Move ${r.provider} down`} reload /> : null}
                {r.unpriced ? null : <Action action={`${API}/catalog/${m.id}/routes/${r.provider}`} method="PATCH"
                  body={{ enabled: !r.enabled }} icon="power" iconOnly tone={r.enabled ? undefined : 'accent'} reload
                  label={r.enabled ? `Switch the ${r.provider} route off` : `Switch the ${r.provider} route on`} />}
                <Action action={`${API}/catalog/${m.id}/routes/${r.provider}`} method="DELETE" icon="x" iconOnly
                  tone="danger" label={`Remove the ${r.provider} route`} reload
                  confirm={`Remove the ${r.provider} route of ${m.name}?`} />
              </td>
            </tr>
          ))}
          {m.routes.length === 0 ? <tr><td colSpan={9}><Empty>No provider route yet: add one below.</Empty></td></tr> : null}
        </Table>
        {m.routes.length < MAX_ROUTES ? (
          <Disclosure summary={`Add ${m.routes.length ? `fallback ${m.routes.length}` : 'the primary route'}`}
            open={m.routes.length === 0}>
            <JsonForm action={`${API}/catalog/${m.id}/routes`} submit="Add route" reload done="Route added.">
              <div class="form-grid three">
                <SelectField label="Provider" name="provider" options={free.map((p) => ({ value: p,
                  label: `${LABELS[p]}${SPECS[p].direct ? ' (direct)' : ''}${configured(c.env, p) ? '' : ' (connect first)'}`,
                  disabled: !configured(c.env, p) }))} />
                <Field label="Model id on that provider" name="provider_model" required
                  placeholder={m.routes[0]?.provider_model ?? m.id} />
                <SelectField label="Fails over" name="failover" value="error" options={[
                  { value: 'error', label: 'after any failed retry' }, { value: 'outage', label: 'only on an outage' },
                  { value: 'never', label: 'never' }]} />
              </div>
              <Disclosure summary="Prices, for a provider without a price list">
                <p class="hint">Leave empty to use the provider's listed price, Anthropic's list price, or OpenRouter's
                  listing of the same model; failing all three the route is added off until priced. Prices typed here
                  are kept as they are.</p>
                <PriceFields />
              </Disclosure>
            </JsonForm>
          </Disclosure>
        ) : null}
      </Section>

      <div id="prices">
      <Disclosure summary="Prices: detail, by hand, history" open={m.routes.some((r) => r.unpriced)}>
        {m.routes.map((r) => (
          <div class="section">
            <div class="section-head"><h3>{r.provider} · {r.unpriced ? 'no price yet' : sourceLabel(r.price_source)}</h3>
              {r.price_source === 'manual' && isProvider(r.provider) && canList(c.env, r.provider) ? (
                <Action action={`${API}/catalog/${m.id}/routes/${r.provider}`} method="PATCH"
                  body={{ price_source: 'auto' }} icon="refresh" tone="ghost" small reload label="Use the listed price" />
              ) : null}</div>
            <p class="small">Input ${perMillion(r.in_micro)} · cache read ${perMillion(r.cache_read_micro)} · cache write
              ${perMillion(r.cache_write_5m_micro)} (5 min) / ${perMillion(r.cache_write_1h_micro)} (1 h) · output
              ${perMillion(r.out_micro)} per 1M tokens{r.fee_bps ? `, plus a ${(r.fee_bps / 100).toFixed(1)}% fee` : ''}.
              {r.source_url ? <> Source: <a href={r.source_url} rel="noreferrer noopener">{r.source_url}</a>.</> : null}</p>
            {r.extra && Object.keys(r.extra).length ? <p class="hint mono">{JSON.stringify(r.extra).slice(0, 400)}</p> : null}
            <Disclosure summary="Set this price by hand" open={r.unpriced}>
              <JsonForm action={`${API}/catalog/${m.id}/routes/${r.provider}`} method="PATCH" submit="Save price" reload
                done="Price saved. It will not be refreshed until set back to the provider's.">
                <PriceFields route={r} />
              </JsonForm>
            </Disclosure>
          </div>
        ))}
        <Section title="Price changes">
          {history.length === 0 ? <Empty>No price change recorded.</Empty> : (
            <Table class="dense" head={['When', 'Provider', 'Change', 'By']}>
              {history.map((h: Record<string, any>) => (
                <tr>
                  <td class="nowrap small">{dateTime(h.at)}</td>
                  <td class="mono">{h.provider}</td>
                  <td class="small">{(h.changes ?? []).map((x: { field: string; old: number; new: number }) =>
                    `${x.field.replace(/_micro$/, '')} ${x.field === 'fee_bps' ? x.old : `$${perMillion(x.old)}`} → ${
                      x.field === 'fee_bps' ? x.new : `$${perMillion(x.new)}`}`).join(' · ')}</td>
                  <td class="small">{String(h.actor ?? '').replace(/^admin:/, '')}</td>
                </tr>
              ))}
            </Table>
          )}
        </Section>
      </Disclosure>
      </div>

      <Disclosure summary="Edit model">
        <JsonForm action={`${API}/catalog/${m.id}`} method="PUT" submit="Save model" reload done="Saved.">
          <div class="form-grid three">
            <Field label="Name" name="name" value={m.name} required />
            <Field label="Family" name="family" value={m.family} keepEmpty />
            <SelectField label="Status" name="status" value={m.status} options={[
              { value: 'active', label: 'active' }, { value: 'preview', label: 'preview' },
              { value: 'deprecated', label: 'deprecated' }]} />
            <Field label="Context window, tokens" name="context_window" type="number" num min={1} value={m.context_window} />
            <Field label="Max output, tokens" name="max_output" type="number" num min={1} value={m.max_output} />
            <Field label="Note" name="note" value={m.note} keepEmpty />
          </div>
          <ModelChecks model={m} />
        </JsonForm>
      </Disclosure>

    </>
  )));
}

function PriceFields(props: { route?: { in_micro: number; cache_read_micro: number; cache_write_5m_micro: number;
  cache_write_1h_micro: number; out_micro: number; fee_bps: number; source_url: string | null } }) {
  const r = props.route;
  const v = (micro: number | undefined) => (micro === undefined ? undefined : micro / 1_000_000);
  return (
    <div class="form-grid three">
      <Field label="Input, $ per 1M" name="in_usd" type="number" step="any" min={0} num value={v(r?.in_micro)} />
      <Field label="Output, $ per 1M" name="out_usd" type="number" step="any" min={0} num value={v(r?.out_micro)} />
      <Field label="Cache read, $ per 1M" name="cache_read_usd" type="number" step="any" min={0} num
        value={v(r?.cache_read_micro)} hint="Empty: the input price." />
      <Field label="Cache write 5 min, $ per 1M" name="cache_write_5m_usd" type="number" step="any" min={0} num
        value={v(r?.cache_write_5m_micro)} />
      <Field label="Cache write 1 h, $ per 1M" name="cache_write_1h_usd" type="number" step="any" min={0} num
        value={v(r?.cache_write_1h_micro)} />
      <Field label="Fee, basis points" name="fee_bps" type="number" min={0} max={5000} num value={r?.fee_bps}
        hint="OpenRouter: 550." />
      <Field label="Where the price was read" name="source_url" value={r?.source_url} wide
        placeholder="https://… the provider's pricing page" />
    </div>
  );
}
