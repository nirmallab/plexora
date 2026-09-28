/** @jsxImportSource hono/jsx */
/**
 * The shared vocabulary of the admin pages, after the licence admin's
 * (licensing/src/ui/components.tsx). No element carries a style="" attribute
 * and no page writes its own script: the CSP allows exactly one of each,
 * pinned by hash.
 */
import type { Child } from 'hono/jsx';

import { addDays } from '../env';
import type { Filters } from '../telemetry/queries';
import { ShareBar } from './charts';
import { num } from './format';

export type Tone = 'ok' | 'warn' | 'bad' | 'accent' | 'plain';

/** The Plexora mark: a field of view with three cells, as on the website. */
export function Mark() {
  return (
    <svg class="mark" viewBox="0 0 24 24" aria-hidden="true">
      <circle class="ring" cx="12" cy="12" r="10" />
      <circle class="cell" cx="9" cy="10" r="2.2" />
      <circle class="cell dim" cx="15" cy="9" r="1.6" />
      <circle class="cell soft" cx="13.5" cy="15" r="2" />
    </svg>
  );
}

export function PageHead(props: { title: Child; lede?: Child; actions?: Child }) {
  return (
    <div class="page-head">
      <div class="grow">
        <h1>{props.title}</h1>
        {props.lede ? <p class="lede">{props.lede}</p> : null}
      </div>
      {props.actions ? <div class="actions">{props.actions}</div> : null}
    </div>
  );
}

/**
 * A titled card. `flush` lets a table run to the card's edges, which is what
 * most cards here are; `foot` is a one-line summary under it.
 */
export function Card(props: { title?: Child; sub?: Child; actions?: Child; feature?: boolean; flush?: boolean;
  foot?: Child; id?: string; children?: Child }) {
  const cls = ['card', props.feature ? 'feature' : '', props.flush ? 'flush' : ''].filter(Boolean).join(' ');
  return (
    <section class={cls} id={props.id}>
      {props.title || props.actions ? (
        <header>
          <div class="grow">
            {props.title ? <h2>{props.title}</h2> : null}
            {props.sub ? <span class="sub">{props.sub}</span> : null}
          </div>
          {props.actions ? <div class="actions">{props.actions}</div> : null}
        </header>
      ) : null}
      {props.children}
      {props.foot ? <div class="card-foot">{props.foot}</div> : null}
    </section>
  );
}

/** A card that starts folded: reference material nobody needs every visit. */
export function Fold(props: { title: Child; sub?: Child; open?: boolean; children?: Child }) {
  return (
    <details class="card" open={props.open ? true : undefined}>
      <summary>{props.title}{props.sub ? <span class="sub">{props.sub}</span> : null}</summary>
      {props.children}
    </details>
  );
}

export function Stats(props: { children?: Child }) {
  return <div class="stats">{props.children}</div>;
}

export function Stat(props: { label: string; value: Child; sub?: Child; tone?: Tone; text?: boolean }) {
  const tone = props.tone && props.tone !== 'plain' && props.tone !== 'accent' ? ` ${props.tone}` : '';
  return (
    <div class={`stat${tone}`}>
      <span class="label">{props.label}</span>
      <span class={props.text ? 'n text' : 'n'}>{props.value}</span>
      {props.sub ? <span class="sub">{props.sub}</span> : null}
    </div>
  );
}

export function Badge(props: { tone?: Tone; children?: Child }) {
  const tone = props.tone && props.tone !== 'plain' ? ` ${props.tone}` : '';
  return <span class={`badge${tone}`}>{props.children}</span>;
}

export function Empty(props: { children?: Child }) {
  return <div class="empty">{props.children}</div>;
}

export function Note(props: { tone?: 'warn' | 'bad'; children?: Child }) {
  return <div class={props.tone ? `note ${props.tone}` : 'note'}>{props.children}</div>;
}

export interface Column<T> {
  key: string;
  label: Child;
  num?: boolean;
  /** Let a long value wrap instead of widening the table. */
  wrap?: boolean;
  render?: (row: T) => Child;
}

export function Table<T extends Record<string, unknown>>(props: { columns: Column<T>[]; rows: T[]; empty?: string;
  rowClass?: (row: T) => string; capped?: boolean }) {
  if (props.rows.length === 0) return <Empty>{props.empty ?? 'Nothing in this range.'}</Empty>;
  return (
    <div class={props.capped ? 'scroll capped' : 'scroll'}>
      <table>
        <thead>
          <tr>
            {props.columns.map((column) => (
              <th class={column.num ? 'num' : undefined}>{column.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {props.rows.map((row) => (
            <tr class={props.rowClass?.(row) || undefined}>
              {props.columns.map((column) => (
                <td class={column.num ? 'num' : column.wrap ? 'wrap' : undefined}>
                  {column.render ? column.render(row) : String(row[column.key] ?? '–')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Name, count and share of the total, with a bar against the largest. */
export function SplitList(props: { rows: { name: string; value: number }[]; empty?: string; limit?: number }) {
  const rows = props.rows.slice(0, props.limit ?? 8);
  if (rows.length === 0) return <Empty>{props.empty ?? 'Nothing in this range.'}</Empty>;
  const total = props.rows.reduce((sum, row) => sum + row.value, 0);
  const peak = Math.max(1, ...rows.map((row) => row.value));
  const rest = props.rows.length - rows.length;
  return (
    <ul class="split-list">
      {rows.map((row) => (
        <li>
          <div class="row-line">
            <span class="name">{row.name}</span>
            <span class="v"><b>{num(row.value)}</b> · {total ? Math.round((row.value / total) * 100) : 0}%</span>
          </div>
          <ShareBar value={row.value} max={peak} />
        </li>
      ))}
      {rest > 0 ? <li class="muted small">and {rest} more</li> : null}
    </ul>
  );
}

/** A range preset link, which keeps every other filter. */
function preset(filters: Filters, days: number, keep: Record<string, string>) {
  const params = new URLSearchParams();
  params.set('from', addDays(filters.to, -(days - 1)));
  params.set('to', filters.to);
  for (const key of ['version', 'launch_mode', 'deployment'] as const) {
    if (filters[key]) params.set(key, filters[key]!);
  }
  for (const [key, value] of Object.entries(keep)) if (value) params.set(key, value);
  return `?${params.toString()}`;
}

/**
 * The shared GET filter bar; the server validates every field again. The
 * presets end at the current `to` day, so they move a chosen window rather
 * than jumping back to today.
 */
export function FilterForm(props: { filters: Filters; extra?: Child; keep?: Record<string, string>;
  fields?: ('version' | 'launch_mode' | 'deployment')[] }) {
  const f = props.filters;
  const fields = props.fields ?? ['version', 'launch_mode', 'deployment'];
  const span = Math.round((Date.parse(`${f.to}T00:00:00Z`) - Date.parse(`${f.from}T00:00:00Z`)) / 86400000) + 1;
  const labels: Record<string, string> = { version: 'Version', launch_mode: 'Launch mode', deployment: 'Deployment' };
  return (
    <div class="card filter-card">
      <form class="filters" method="get">
        <div class="field date">
          <label for="f-from">From</label>
          <input id="f-from" type="date" name="from" value={f.from} />
        </div>
        <div class="field date">
          <label for="f-to">To</label>
          <input id="f-to" type="date" name="to" value={f.to} />
        </div>
        {fields.map((name) => (
          <div class="field">
            <label for={`f-${name}`}>{labels[name]}</label>
            <input id={`f-${name}`} name={name} value={f[name] ?? ''} placeholder="any" />
          </div>
        ))}
        {props.extra}
        <div class="go">
          <button type="submit">Apply</button>
          <a class="button ghost" href="?">Reset</a>
        </div>
        <div class="presets" aria-label="Range">
          {[7, 30, 90, 365].map((days) => (
            <a href={preset(f, days, props.keep ?? {})} aria-current={span === days ? 'true' : undefined}>
              {days === 365 ? '1y' : `${days}d`}
            </a>
          ))}
        </div>
      </form>
    </div>
  );
}
