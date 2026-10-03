/** @jsxImportSource hono/jsx */
/**
 * What every Plexora AI admin page shares: the section's tabs, the on/off
 * switch (one home, on every page), and how a price, a model's abilities and
 * a route's state read.
 */
import type { Child } from 'hono/jsx';

import type { CatalogRouteRow } from '../../ai/routing';
import type { RouteView } from '../../ai/views';
import { knob } from '../../env';
import type { App } from '../../http';
import { Action, Badge, Note, SubNav } from '../../ui/components';
import { perMillion } from '../../ui/format';
import { shell } from '../adminShell';

export const API = '/admin/api/ai';
export const BASE = '/admin/ai';

export const PAGES: [string, string][] = [
  [BASE, 'Overview'],
  [`${BASE}/models`, 'Models'],
  [`${BASE}/providers`, 'Providers'],
  [`${BASE}/routing`, 'Task routing'],
  [`${BASE}/usage`, 'Usage & cost'],
  [`${BASE}/settings`, 'Settings'],
  [`${BASE}/api`, 'API'],
];

/** A Plexora AI page: the heading, the switch, the tabs, and an off banner while it is off. */
export function aiShell(c: App, active: string, body: Child, opts: { lede?: Child } = {}) {
  const on = knob(c.env, 'AI_ENABLED') === 1;
  const page = PAGES.find(([href]) => href === active)?.[1] ?? 'Plexora AI';
  const power = on ? (
    <>
      <span class="power on">On</span>
      <Action action={`${API}/settings`} method="PUT" body={{ AI_ENABLED: 0 }} label="Switch off" tone="danger" small
        reload confirm="Refuse every Plexora AI call for every account until it is switched on again?" />
    </>
  ) : (
    <>
      <span class="power">Off</span>
      <Action action={`${API}/settings`} method="PUT" body={{ AI_ENABLED: 1 }} label="Switch AI on" small reload />
    </>
  );
  return shell(c, 'Plexora AI', '/admin/ai', (
    <div class="ai-page">
      {on ? null : <Note warn>Plexora AI is switched off: every token, call and run is refused. Runs in progress pause,
        and resume once it is on.</Note>}
      {body}
    </div>
  ), { actions: power, lede: opts.lede, docTitle: `${page} · Plexora AI`,
    sub: <SubNav items={PAGES} active={active} label="Plexora AI" /> });
}

/** "$4.00 / $20.00" per 1M tokens, in / out. */
export function Price(props: { route: Pick<CatalogRouteRow, 'in_micro' | 'out_micro'>; unit?: boolean }) {
  const free = !props.route.in_micro && !props.route.out_micro;
  return (
    <span class="price">
      {free ? 'free' : `$${perMillion(props.route.in_micro)} / $${perMillion(props.route.out_micro)}`}
      {props.unit && !free ? <span class="unit"> per M</span> : null}
    </span>
  );
}

export function CachePrice(props: { route: CatalogRouteRow }) {
  const r = props.route;
  if (!r.in_micro && !r.out_micro) return null;
  return <div class="sub">cache read ${perMillion(r.cache_read_micro)} · write ${perMillion(r.cache_write_5m_micro)}
    {r.cache_write_1h_micro !== r.cache_write_5m_micro ? ` (1 h $${perMillion(r.cache_write_1h_micro)})` : ''}</div>;
}

export function caps(m: { supports_vision: number; supports_tools: number; supports_structured: number;
  reasoning: number }): string {
  return [m.supports_vision ? 'vision' : null, m.supports_tools ? 'tools' : null,
    m.supports_structured ? 'structured' : null, m.reasoning ? 'reasoning' : null].filter(Boolean).join(' · ') ||
    'text only';
}

/** A route's live state: switched off, circuit, availability, no key. */
export function RouteState(props: { route: RouteView }) {
  const r = props.route;
  if (!r.enabled) return <Badge>off</Badge>;
  if (r.circuit === 'forced') return <Badge tone="bad">switched off</Badge>;
  if (!r.configured) return <Badge tone="bad">no key</Badge>;
  if (r.circuit === 'open') return <Badge tone="warn">circuit open</Badge>;
  if (r.availability === 'down') return <Badge tone="bad">down</Badge>;
  if (r.availability === 'degraded') return <Badge tone="warn">degraded</Badge>;
  if (r.availability === 'ok') return <Badge tone="ok">up</Badge>;
  return <Badge>unchecked</Badge>;
}

export const RANK_LABELS = ['Primary', 'Fallback 1', 'Fallback 2'];

export const sourceLabel = (source: CatalogRouteRow['price_source']) =>
  source === 'api' ? 'provider API' : source === 'builtin' ? 'list price' : 'set by hand';
