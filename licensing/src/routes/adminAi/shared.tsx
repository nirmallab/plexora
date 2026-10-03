/** @jsxImportSource hono/jsx */
/**
 * What every Plexora AI admin page shares: the four steps of the workflow
 * (Providers, Models, Tasks, Overview) with a status dot each, the quiet
 * pages beside them (Usage, Settings), the on/off switch (one home, on every
 * page), and how a price, a model's abilities, its provider chain and a
 * route's state read.
 */
import type { Child } from 'hono/jsx';

import type { CatalogRouteRow } from '../../ai/routing';
import { type ModelView, type RouteView, summary as computeSummary, type Summary } from '../../ai/views';
import { knob, nowSeconds } from '../../env';
import type { App } from '../../http';
import { Action, Badge, Note } from '../../ui/components';
import { perMillion } from '../../ui/format';
import { shell } from '../adminShell';

export const API = '/admin/api/ai';
export const BASE = '/admin/ai';

/** The workflow, in the order an admin sets it up. */
export const STEPS: Array<{ n: number; href: string; label: string; key: 'providers' | 'models' | 'tasks' | 'overview' }> = [
  { n: 1, href: `${BASE}/providers`, label: 'Providers', key: 'providers' },
  { n: 2, href: `${BASE}/models`, label: 'Models', key: 'models' },
  { n: 3, href: `${BASE}/tasks`, label: 'Tasks', key: 'tasks' },
  { n: 4, href: BASE, label: 'Overview', key: 'overview' },
];

/** Pages beside the workflow. */
export const ASIDE: [string, string][] = [[`${BASE}/usage`, 'Usage'], [`${BASE}/settings`, 'Settings']];

type Dot = { tone: 'ok' | 'warn' | 'bad' | 'none'; title: string };

/** Each step's dot: green when it is done, amber when it wants a look, red when nothing works yet. */
export function stepStatus(s: Summary): Record<(typeof STEPS)[number]['key'], Dot> {
  const keyed = s.providers.total - (s.providers.by_state.not_connected ?? 0);
  const refused = (s.providers.by_state.key_refused ?? 0) + (s.providers.by_state.unreachable ?? 0);
  return {
    providers: s.providers.connected > 0 ? { tone: refused ? 'warn' : 'ok', title: `${s.providers.connected} of ${
      s.providers.total} connected${refused ? `; ${refused} with a key problem` : ''}` }
      : keyed > 0 ? { tone: 'warn', title: `${keyed} with a key, none checked yet` }
        : { tone: 'bad', title: 'No provider connected' },
    models: s.models.active === 0 ? { tone: 'bad', title: 'No model can serve yet' }
      : s.models.unpriced ? { tone: 'warn', title: `${s.models.unpriced} model${s.models.unpriced === 1 ? '' : 's'
      } need${s.models.unpriced === 1 ? 's' : ''} a price` }
        : { tone: 'ok', title: `${s.models.active} active` },
    tasks: s.general.level === 'builtin' || s.general.level === 'legacy' || !s.general.model
      ? { tone: 'warn', title: 'No general model chosen: the built-in default serves' }
      : s.tasks.issues ? { tone: 'warn', title: `${s.tasks.issues} task row${s.tasks.issues === 1 ? '' : 's'} with an issue` }
        : { tone: 'ok', title: `General model: ${s.general.model}` },
    overview: s.problems.open ? { tone: 'warn', title: `${s.problems.open} open warning${s.problems.open === 1 ? '' : 's'}` }
      : { tone: 'ok', title: 'Nothing needs attention' },
  };
}

export function StepsNav(props: { active: string; summary: Summary }) {
  const dots = stepStatus(props.summary);
  return (
    <nav class="steps" aria-label="Plexora AI">
      {STEPS.map((s) => {
        const dot = dots[s.key];
        return (
          <a href={s.href} aria-current={props.active === s.href ? 'page' : undefined} title={dot.title}>
            <span class="n">{s.n}</span><span>{s.label}</span>
            {dot.tone === 'none' ? null : <span class={`dot ${dot.tone}`} aria-label={dot.title}></span>}
          </a>
        );
      })}
      <span class="aside">
        {ASIDE.map(([href, label]) => <a href={href} aria-current={props.active === href ? 'page' : undefined}>{label}</a>)}
      </span>
    </nav>
  );
}

/** A Plexora AI page: the heading, the switch, the steps, and an off banner while it is off. */
export async function aiShell(c: App, active: string, body: Child, opts: { lede?: Child; summary?: Summary } = {}) {
  const on = knob(c.env, 'AI_ENABLED') === 1;
  const s = opts.summary ?? await computeSummary(c.env, nowSeconds());
  const label = [...STEPS.map((x) => [x.href, x.label] as [string, string]), ...ASIDE]
    .find(([href]) => href === active)?.[1] ?? 'Plexora AI';
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
  ), { actions: power, lede: opts.lede, docTitle: `${label} · Plexora AI`,
    sub: <StepsNav active={active} summary={s} /> });
}

/** An id usable in a `#selector`: a model id may hold a dot. */
export const domId = (id: string) => id.replace(/[^A-Za-z0-9_-]/g, '-');

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

const ABILITIES: Array<[string, string, string]> = [
  ['V', 'supports_vision', 'takes images'], ['T', 'supports_tools', 'calls tools'],
  ['S', 'supports_structured', 'native structured output'], ['R', 'reasoning', 'reasons (extended thinking)'],
];

/** A model's abilities as four letters: V T S R, struck through where it lacks one. */
export function Abilities(props: { m: { supports_vision: number; supports_tools: number; supports_structured: number;
  reasoning: number } | { vision: boolean | null; tools: boolean | null; structured: boolean | null;
  reasoning: boolean | null } }) {
  const m = props.m as Record<string, unknown>;
  const has = (key: string) => {
    const short = key.replace(/^supports_/, '');
    const v = key in m ? m[key] : m[short];
    return v === null || v === undefined ? null : !!v;
  };
  return (
    <span class="abil" aria-label={ABILITIES.map(([, key, words]) => `${has(key) === false ? 'no: ' : ''}${words}`)
      .join(', ')}>
      {ABILITIES.map(([letter, key, words]) => {
        const v = has(key);
        return <span class={v === false ? 'no' : undefined} title={v === null ? `${words}: not known`
          : v ? words : `does not: ${words}`}>{letter}</span>;
      })}
    </span>
  );
}

const RANK_WORDS = ['primary', 'fallback 1', 'fallback 2'];

/** A model's providers in the order they are tried: "orcarouter › anthropic › openrouter". */
export function Chain(props: { routes: RouteView[] }) {
  if (!props.routes.length) return <Badge tone="bad">no route</Badge>;
  return (
    <span class="chain">
      {props.routes.map((r, i) => {
        const down = r.circuit === 'forced' || !r.configured || r.unpriced;
        const cls = [i === 0 ? 'p' : '', !r.enabled ? 'off' : down ? 'bad' : ''].filter(Boolean).join(' ');
        const why = r.unpriced ? 'needs a price' : !r.enabled ? 'off' : r.circuit === 'forced' ? 'provider switched off'
          : !r.configured ? 'no key' : r.circuit === 'open' ? 'circuit open' : r.availability;
        return <>{i ? <span class="sep">›</span> : null}<span class={cls || undefined}
          title={`${RANK_WORDS[i]}: ${r.provider_model} (${why})`}>{r.provider}</span></>;
      })}
    </span>
  );
}

const serves = (r: RouteView) => r.enabled && r.configured && r.circuit !== 'forced' && !r.unpriced;

/** One word for whether a model can serve, and if not, why. */
export function ModelState(props: { m: ModelView }) {
  const { m } = props;
  if (!m.enabled) return <Badge>disabled</Badge>;
  if (!m.routes.length) return <Badge tone="bad">no route</Badge>;
  if (m.routes.every((r) => r.unpriced)) return <Badge tone="warn">price needed</Badge>;
  if (!m.routes.some((r) => r.configured)) return <Badge tone="bad">no key</Badge>;
  if (!m.routes.some(serves)) return <Badge tone="bad">off</Badge>;
  if (!serves(m.routes[0]!) || m.routes[0]!.circuit === 'open') return <Badge tone="warn">on fallback</Badge>;
  return <Badge tone="ok">ready</Badge>;
}

/** A route's live state: switched off, circuit, availability, no key. */
export function RouteState(props: { route: RouteView }) {
  const r = props.route;
  if (r.unpriced) return <Badge tone="warn">price needed</Badge>;
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
  source === 'api' ? 'listed price' : source === 'builtin' ? 'list price' : 'set by hand';
