/** @jsxImportSource hono/jsx */
/**
 * The shared vocabulary of the portal and admin pages.
 *
 * Anything that changes something is a `JsonForm` or an `Action`, which render
 * the data- attributes the one inline script listens for (ui/client.ts). No
 * page writes its own script and no element carries a style="" attribute --
 * the CSP allows exactly one script and one stylesheet, both pinned by hash.
 */
import type { Child } from 'hono/jsx';

import type { Tone } from './format';

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

export function Card(props: { title?: Child; sub?: Child; actions?: Child; feature?: boolean; id?: string;
  children?: Child }) {
  return (
    <section class={props.feature ? 'card feature' : 'card'} id={props.id}>
      {props.title || props.actions ? (
        <header>
          <div class="grow">
            {props.title ? <h2>{props.title}</h2> : null}
            {props.sub ? <div class="sub">{props.sub}</div> : null}
          </div>
          {props.actions ? <div class="actions">{props.actions}</div> : null}
        </header>
      ) : null}
      {props.children}
    </section>
  );
}

export function Stat(props: { label: string; value: Child; sub?: Child }) {
  return (
    <div class="stat">
      <span class="label">{props.label}</span>
      <span class="n">{props.value}</span>
      {props.sub ? <div class="sub">{props.sub}</div> : null}
    </div>
  );
}

export function Badge(props: { tone?: Tone; children?: Child }) {
  const tone = props.tone && props.tone !== 'plain' ? ` ${props.tone}` : '';
  return <span class={`badge${tone}`}>{props.children}</span>;
}

export function Table(props: { head: Child[]; right?: number[]; kv?: boolean; class?: string; children?: Child }) {
  const cls = [props.kv ? 'kv' : '', props.class ?? ''].filter(Boolean).join(' ');
  return (
    <div class="scroll">
      <table class={cls || undefined}>
        {props.kv ? null : (
          <thead>
            <tr>{props.head.map((cell, i) => (
              <th class={props.right?.includes(i) ? 'right' : undefined}>{cell}</th>
            ))}</tr>
          </thead>
        )}
        <tbody>{props.children}</tbody>
      </table>
    </div>
  );
}

export function Empty(props: { children?: Child }) {
  return <div class="empty">{props.children}</div>;
}

export function Note(props: { warn?: boolean; children?: Child }) {
  return <div class={props.warn ? 'note warn' : 'note'}>{props.children}</div>;
}

export function Section(props: { title?: Child; actions?: Child; id?: string; children?: Child }) {
  return (
    <div class="section" id={props.id}>
      {props.title || props.actions ? (
        <div class="section-head">
          {props.title ? <h3>{props.title}</h3> : null}
          {props.actions ? <div class="actions">{props.actions}</div> : null}
        </div>
      ) : null}
      {props.children}
    </div>
  );
}

/** A section's own tabs, under the page heading. */
export function SubNav(props: { items: [string, string][]; active: string; label: string }) {
  return (
    <nav class="subnav" aria-label={props.label}>
      {props.items.map(([href, label]) => (
        <a href={href} aria-current={props.active === href ? 'page' : undefined}>{label}</a>
      ))}
    </nav>
  );
}

/** Links that choose one of a few views (7 / 30 / 90 days). */
export function Seg(props: { items: [string, string][]; active: string; label: string }) {
  return (
    <nav class="seg" aria-label={props.label}>
      {props.items.map(([href, label]) => (
        <a href={href} aria-current={props.active === href ? 'true' : undefined}>{label}</a>
      ))}
    </nav>
  );
}

/** A row of label-value facts. */
export function DefinitionList(props: { items: [Child, Child][] }) {
  return (
    <dl class="dl">
      {props.items.map(([term, value]) => (
        <div><dt>{term}</dt><dd>{value}</dd></div>
      ))}
    </dl>
  );
}

/** A bar of counts and controls above a table. */
export function Toolbar(props: { children?: Child }) {
  return <div class="toolbar">{props.children}</div>;
}

/** A folded section, for anything destructive or rarely wanted. */
export function Disclosure(props: { summary: Child; open?: boolean; children?: Child }) {
  return (
    <details class="section" open={props.open ? true : undefined}>
      <summary>{props.summary}</summary>
      {props.children}
    </details>
  );
}

/** A per-row menu of actions. */
export function RowMenu(props: { children?: Child }) {
  return (
    <details class="rowmenu">
      <summary aria-label="Actions">•••</summary>
      <div>{props.children}</div>
    </details>
  );
}

const flag = (on: boolean | undefined) => (on ? '' : undefined);

export interface FieldProps {
  label: string;
  name: string;
  type?: 'text' | 'email' | 'number' | 'password' | 'date';
  value?: string | number | null;
  placeholder?: string;
  hint?: Child;
  required?: boolean;
  /** How the script coerces the value: a number, a comma list, a date. */
  num?: boolean;
  list?: boolean;
  date?: boolean;
  /** Send the field even when empty, so a value can be cleared. */
  keepEmpty?: boolean;
  min?: number;
  max?: number;
  /** "any" lets a number field take decimals (prices). */
  step?: string;
  autocomplete?: string;
  /** Span every column of a .form-grid. */
  wide?: boolean;
  id?: string;
}

export function Field(props: FieldProps) {
  const id = props.id ?? `f-${props.name}`;
  return (
    <div class={props.wide ? 'field wide' : 'field'}>
      <label for={id}>{props.label}</label>
      <input id={id} name={props.name} type={props.type ?? 'text'}
        value={props.value === null || props.value === undefined ? undefined : String(props.value)}
        placeholder={props.placeholder} required={props.required ? true : undefined}
        data-num={flag(props.num)} data-list={flag(props.list)} data-date={flag(props.date)}
        data-keep-empty={flag(props.keepEmpty)} min={props.min} max={props.max} step={props.step} autocomplete={props.autocomplete} />
      {props.hint ? <div class="hint">{props.hint}</div> : null}
    </div>
  );
}

export function SelectField(props: { label: string; name: string;
  options: { value: string; label: string; disabled?: boolean }[]; value?: string | null; hint?: Child; wide?: boolean;
  id?: string; keepEmpty?: boolean }) {
  const id = props.id ?? `f-${props.name}`;
  return (
    <div class={props.wide ? 'field wide' : 'field'}>
      <label for={id}>{props.label}</label>
      <select id={id} name={props.name} data-keep-empty={flag(props.keepEmpty)}>
        {props.options.map((option) => (
          <option value={option.value} selected={option.value === props.value ? true : undefined}
            disabled={option.disabled ? true : undefined}>{option.label}</option>
        ))}
      </select>
      {props.hint ? <div class="hint">{props.hint}</div> : null}
    </div>
  );
}

export function TextareaField(props: { label: string; name: string; value?: string | null; hint?: Child;
  rows?: number; placeholder?: string; keepEmpty?: boolean; jsonText?: boolean; wide?: boolean; id?: string }) {
  const id = props.id ?? `f-${props.name}`;
  return (
    <div class={props.wide ? 'field wide' : 'field'}>
      <label for={id}>{props.label}</label>
      <textarea id={id} name={props.name} rows={props.rows ?? 3} placeholder={props.placeholder}
        data-keep-empty={flag(props.keepEmpty)} data-json-text={flag(props.jsonText)}>{props.value ?? ''}</textarea>
      {props.hint ? <div class="hint">{props.hint}</div> : null}
    </div>
  );
}

export function CheckField(props: { label: Child; name: string; checked?: boolean; hint?: Child }) {
  return (
    <label class="check">
      <input type="checkbox" name={props.name} checked={props.checked ? true : undefined} />
      <span>{props.label}{props.hint ? <div class="hint">{props.hint}</div> : null}</span>
    </label>
  );
}

export function FileField(props: { label: string; name: string; accept?: string; hint?: Child; required?: boolean;
  id?: string }) {
  const id = props.id ?? `f-${props.name}`;
  return (
    <div class="field">
      <label for={id}>{props.label}</label>
      <input id={id} type="file" name={props.name} data-file-json="" accept={props.accept}
        required={props.required ? true : undefined} />
      {props.hint ? <div class="hint">{props.hint}</div> : null}
    </div>
  );
}

type Tones = 'primary' | 'ghost' | 'danger';

interface SendProps {
  method?: 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  confirm?: string;
  /** Message on success. */
  done?: string;
  reload?: boolean;
  next?: string;
  /** Comma-separated response fields to show once, e.g. "seat.key". */
  reveal?: string;
  /** Where to show them (a Reveal's id, with '#'); #reveal by default. */
  revealInto?: string;
  download?: boolean;
}

function sendAttrs(props: SendProps) {
  return {
    'data-method': props.method && props.method !== 'POST' ? props.method : undefined,
    'data-confirm': props.confirm,
    'data-done': props.done,
    'data-reload': flag(props.reload),
    'data-next': props.next,
    'data-reveal': props.reveal,
    'data-reveal-into': props.revealInto,
    'data-download': flag(props.download),
  };
}

function toneClass(tone: Tones | undefined, small?: boolean): string | undefined {
  const classes = [tone === 'ghost' ? 'ghost' : tone === 'danger' ? 'danger' : '', small ? 'tiny' : '']
    .filter(Boolean).join(' ');
  return classes || undefined;
}

export function JsonForm(props: SendProps & {
  action: string;
  /** "/x/{name}/y": the address is filled from that field at submit. */
  template?: string;
  submit: string;
  tone?: Tones;
  small?: boolean;
  inline?: boolean;
  wideSubmit?: boolean;
  children?: Child;
}) {
  const button = toneClass(props.tone, props.small ?? props.inline);
  return (
    <form class={props.inline ? 'inline-form' : undefined} data-json="" action={props.action}
      data-template={props.template} {...sendAttrs(props)}>
      {props.children}
      <div class="actions">
        <button type="submit" class={props.wideSubmit ? `${button ?? ''} wide`.trim() : button}>{props.submit}</button>
      </div>
    </form>
  );
}

export function Action(props: SendProps & { action: string; label: string; body?: unknown; tone?: Tones;
  small?: boolean }) {
  return (
    <button type="button" class={toneClass(props.tone, props.small)} data-action={props.action}
      data-body={props.body === undefined ? undefined : JSON.stringify(props.body)} {...sendAttrs(props)}>
      {props.label}
    </button>
  );
}

/** Where a one-time value appears (see `reveal`). */
export function Reveal(props: { id: string }) {
  return <div id={props.id} aria-live="polite" hidden></div>;
}

/**
 * A count per day as bars. SVG geometry attributes rather than inline styles,
 * which the CSP would refuse: no chart library, nothing to load.
 */
export function Bars(props: { values: number[]; labels: string[]; label: string }) {
  const peak = Math.max(1, ...props.values);
  const width = 10;
  return (
    <svg class="bars" viewBox={`0 0 ${props.values.length * width} 64`} preserveAspectRatio="none" role="img"
      aria-label={props.label}>
      {props.values.map((n, i) => {
        const h = Math.max(2, Math.round((n / peak) * 62));
        return (
          <rect x={String(i * width + 1)} y={String(64 - h)} width={String(width - 2)} height={String(h)} rx="1"
            class={n > 0 ? 'on' : undefined}>
            <title>{`${props.labels[i]}: ${n}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}
