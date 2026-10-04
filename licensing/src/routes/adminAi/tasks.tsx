/** @jsxImportSource hono/jsx */
/**
 * /admin/ai/tasks, step 3: which approved model does each task, by module.
 *
 * The first row is All tasks: the general model, with its fallbacks. Each
 * module's default and each task either sets its own chain or inherits (a
 * task its module's default, a module All tasks). A row's model and two
 * fallbacks are three selects that save as soon as one changes; what serves
 * now, from the resolver a call uses, is shown under them. The pencil opens
 * the row's advanced settings (effort, caps, requirements, shadow). Providers
 * do not appear here: a model's provider order is the model's own (Models).
 * Effort is asked for once per row and fitted to each model of the chain
 * (ai/effort.ts); the row shows what each model is actually sent.
 */
import type { Child } from 'hono/jsx';

import { isUnpriced } from '../../ai/catalog_store';
import { LEVELS } from '../../ai/effort';
import { type CatalogRow, specOf, type TaskRouteRow } from '../../ai/routing';
import { moduleLabel, patternLabel, TASKS } from '../../ai/tasks';
import { catalogView, eligibility, summary, type TaskRowView, tasksView, taskUsage } from '../../ai/views';
import { nowSeconds } from '../../env';
import { type App, page } from '../../http';
import { Action, Badge, Field, IconButton, JsonForm, Note, SelectField, Toolbar } from '../../ui/components';
import { ms, tokens, usd } from '../../ui/format';
import { aiShell, API, BASE } from './shared';

const TASKS_PAGE = `${BASE}/tasks`;

const editorId = (pattern: string) => `ed-${pattern === '*' ? 'all' : pattern.replace('.*', '-default').replace('.', '-')}`;

function needs(row: TaskRowView): string {
  if (row.level !== 'task') return '';
  return [row.requires.vision ? 'vision' : null, row.requires.reasoning ? 'reasoning' : null].filter(Boolean).join(' · ')
    || 'text';
}

/** "effort high", "effort auto: medium", "effort xhigh → high": the row's ask, as its primary model takes it. */
function effortOf(a: TaskRouteRow, row: TaskRowView): string | null {
  const spec = specOf(a);
  if (!spec) return null;
  const head = row.effective.source === row.pattern ? row.effective.chain[0]?.effort : undefined;
  // A module or global row's auto is each task's own level, so no one level is shown for it.
  if (spec === 'auto') return `effort auto${head && row.level === 'task' ? `: ${head}` : ''}`;
  return `effort ${head && head !== spec ? head : spec}`;
}

function limitsOf(a: TaskRouteRow | undefined, row: TaskRowView): string {
  if (!a) return '';
  return [effortOf(a, row), a.max_tokens_cap ? `cap ${tokens(a.max_tokens_cap)}` : null,
    a.max_cost_micro ? `≤ ${usd(a.max_cost_micro)}` : null, a.latency_ms ? `≤ ${ms(a.latency_ms)}` : null]
    .filter(Boolean).join(' · ');
}

const tri = (v: number | null | undefined) => (v === null || v === undefined ? 'inherit' : v ? '1' : '0');

/** Why a model cannot be chosen for a row, or null: the API's own rule, and a model with no price yet. */
function whyNot(m: CatalogRow, pattern: string, unpriced: Set<string>): string | null {
  return eligibility(m, pattern) ?? (unpriced.has(m.id) ? 'no price' : null);
}

/** What the blank choice means on a row: what it inherits. */
function blankOf(row: TaskRowView): string {
  if (row.level === 'global') return 'built-in default';
  return `inherit ${row.parent ? patternLabel(row.parent) : 'the default'}`;
}

/**
 * The row's model and its two fallbacks, saved on change. The row's advanced values ride along as hidden fields,
 * because saving a row replaces it whole.
 */
function ChainForm(props: { row: TaskRowView; models: CatalogRow[]; unpriced: Set<string> }) {
  const { row, models, unpriced } = props;
  const own = row.serve;
  const head = own[0];
  const options = (value: string | undefined, blank: string) => [
    <option value="" selected={value ? undefined : true}>{blank}</option>,
    ...models.map((m) => {
      const why = whyNot(m, row.pattern, unpriced);
      const mine = own.some((a) => a.model_id === m.id);
      return <option value={m.id} selected={m.id === value ? true : undefined}
        disabled={why && !mine ? true : undefined}>{why ? `${m.name} (${why})` : m.name}</option>;
    }),
  ];
  const select = (name: string, label: string, value: string | undefined, blank: string, off: boolean) => (
    <select name={name} aria-label={label} data-keep-empty="" class={value ? undefined : 'blank'}
      disabled={off ? true : undefined}>{options(value, blank)}</select>
  );
  return (
    <form class="chain-form" data-json="" data-autosave="" action={`${API}/tasks/${encodeURIComponent(row.pattern)}`}
      data-method="PUT" data-reload="" data-done={`${patternLabel(row.pattern)} saved.`}
      data-confirm={row.pattern === '*' ? 'Change the general model, which every task without its own uses?' : undefined}>
      {select('primary', 'Primary model', own[0]?.model_id, blankOf(row), false)}
      <span class="sep">›</span>
      {select('fallback_1', 'Fallback 1', own[1]?.model_id, 'no fallback', !own[0])}
      <span class="sep">›</span>
      {select('fallback_2', 'Fallback 2', own[2]?.model_id, 'no fallback', !own[1])}
      {head ? <>
        <input type="hidden" name="effort" value={specOf(head) ?? ''} />
        <input type="hidden" name="max_tokens_cap" value={head.max_tokens_cap ?? ''} data-num="" />
        <input type="hidden" name="max_cost_usd" value={head.max_cost_micro ? String(head.max_cost_micro / 1_000_000) : ''} />
        <input type="hidden" name="latency_ms" value={head.latency_ms ?? ''} data-num="" />
        <input type="hidden" name="requires_vision" value={tri(head.requires_vision)} />
        <input type="hidden" name="requires_reasoning" value={tri(head.requires_reasoning)} />
        <input type="hidden" name="note" value={head.note ?? ''} />
      </> : null}
    </form>
  );
}

/** Under the selects: what serves when the row inherits, and anything wrong with it. */
function Serving(props: { row: TaskRowView }) {
  const { row } = props;
  const e = row.effective;
  const own = row.serve.length > 0 && e.source === row.pattern;
  const passed = e.skipped.filter((s) => s.pattern === row.pattern);
  const lines: Child[] = [];
  if (!own && e.chain.length) {
    lines.push(<span class="inherit">{e.chain.map((l) => l.name).join(' › ')}{e.source
      ? ` · from ${patternLabel(e.source)}` : ''}</span>);
  }
  if (e.level === 'builtin') lines.push(<Badge tone="warn">built-in default</Badge>);
  if (e.level === 'legacy') lines.push(<Badge tone="warn">previous route table</Badge>);
  for (const s of passed) lines.push(<Badge tone="bad">{s.model_id}: {s.reason}</Badge>);
  for (const m of row.mismatch) lines.push(<Badge tone="warn">{m.name}: {m.reason}</Badge>);
  return lines.length ? <div class="sub">{lines.map((l, i) => <>{i ? ' ' : ''}{l}</>)}</div> : null;
}

function Editor(props: { row: TaskRowView; models: CatalogRow[]; open: boolean; cols: number }) {
  const { row, models } = props;
  const own = row.serve;
  const head = own[0];
  const shadow = row.shadow[0];
  // Saving here keeps the row's chain: its own, or the one it inherits now (which pins it here).
  const chain = own.length ? own.map((a) => a.model_id) : row.effective.chain.map((l) => l.model_id);
  const parent = row.parent ? patternLabel(row.parent) : 'the built-in default';
  return (
    <tr class="editor" id={editorId(row.pattern)} hidden={props.open ? undefined : true}>
      <td colspan={props.cols}>
        <JsonForm action={`${API}/tasks/${encodeURIComponent(row.pattern)}`} method="PUT" submit="Save" reload
          done={`${patternLabel(row.pattern)} saved.`} confirm={row.pattern === '*'
            ? 'Change the settings every task without its own assignment uses?' : undefined}>
          {['primary', 'fallback_1', 'fallback_2'].map((name, i) =>
            <input type="hidden" name={name} value={chain[i] ?? ''} data-keep-empty="" />)}
          {!own.length && chain.length ? <p class="hint">Saving pins the chain this row inherits now
            ({row.effective.chain.map((l) => l.name).join(' › ')}) here.</p> : null}
          <div class="form-grid three">
            <SelectField label="Reasoning effort" name="effort" value={head ? specOf(head) ?? '' : 'auto'} options={[
              { value: '', label: "each model's own default" },
              { value: 'auto', label: row.level === 'task' && TASKS[row.pattern]
                ? `auto: this task's level (${TASKS[row.pattern]!.effort})` : "auto: each task's own level" },
              ...LEVELS.map((l) => ({ value: l, label: l }))]}
              hint="Each model is sent the nearest level it takes, or none." />
            <Field label="Output cap, tokens" name="max_tokens_cap" type="number" num min={1} max={128000}
              value={head?.max_tokens_cap} hint="Empty: the task's own." />
            <Field label="Cost cap per call, $" name="max_cost_usd" type="number" step="any" min={0}
              value={head?.max_cost_micro ? head.max_cost_micro / 1_000_000 : undefined}
              hint="A model whose estimate is above it is passed over." />
            <Field label="Preferred first byte, ms" name="latency_ms" type="number" num min={1}
              value={head?.latency_ms} hint="Slower providers of a model go after faster ones." />
            {row.level === 'task' ? <>
              <SelectField label="Needs vision" name="requires_vision" value={tri(head?.requires_vision)} options={[
                { value: 'inherit', label: `as registered (${row.requires.vision && !row.requires.overridden ? 'yes' : 'no'})` },
                { value: '1', label: 'yes' }, { value: '0', label: 'no' }]} />
              <SelectField label="Needs reasoning" name="requires_reasoning" value={tri(head?.requires_reasoning)} options={[
                { value: 'inherit', label: 'as registered' }, { value: '1', label: 'yes' }, { value: '0', label: 'no' }]} />
            </> : null}
            <SelectField label="Shadow model" name="shadow_model" keepEmpty value={shadow?.model_id ?? ''}
              options={[{ value: '', label: 'none' }, ...models.filter((m) => m.enabled).map((m) => ({ value: m.id,
                label: m.name }))]} hint="Compared on a sample of sessions at Plexora's cost; never billed." />
            <Field label="Shadow share, %" name="shadow_pct" type="number" num min={1} max={100}
              value={shadow?.shadow_pct ?? 5} />
            <Field label="Note" name="note" value={head?.note ?? shadow?.note} wide />
          </div>
        </JsonForm>
        {own.length || row.shadow.length ? (
          <p class="small">
            <Action action={`${API}/tasks/${encodeURIComponent(row.pattern)}`} method="DELETE" tone="ghost" small reload
              icon="undo" label={row.level === 'global' ? 'Back to the built-in default' : `Reset to ${parent}`}
              confirm={`${patternLabel(row.pattern)} will use ${parent} again.`} />
          </p>
        ) : null}
      </td>
    </tr>
  );
}

function Row(props: { row: TaskRowView; models: CatalogRow[]; unpriced: Set<string>;
  usage: { calls: number; cost_micro: number } | undefined; group?: boolean; hit: boolean; open: boolean; label: Child }) {
  const { row } = props;
  const serving = row.serve.length ? row.serve[0] : undefined;
  const g = (cls: string) => (props.group ? `group ${cls}`.trim() : cls || undefined);
  return (
    <tr class={props.hit ? 'hit' : undefined}>
      <td class={props.group ? 'group' : 'task'}>{props.label}</td>
      <td class={g('caps')}>{needs(row)}</td>
      <td class={g('')}><ChainForm row={row} models={props.models} unpriced={props.unpriced} /><Serving row={row} /></td>
      <td class={g('small')}>{limitsOf(serving, row) || <span class="dim">—</span>}
        {row.shadow[0] ? <div class="sub">shadow {row.shadow[0].model_id} · {row.shadow[0].shadow_pct}%</div> : null}</td>
      <td class={g('small right nowrap')}>{props.usage
        ? <>{props.usage.calls.toLocaleString('en')}<div class="sub">{usd(props.usage.cost_micro)}</div></>
        : <span class="dim">—</span>}</td>
      <td class={g('icons')}>
        <IconButton icon="pencil" toggle={`#${editorId(row.pattern)}`} expanded={props.open}
          label={`Advanced settings of ${patternLabel(row.pattern)}`} />
      </td>
    </tr>
  );
}

export async function tasksPage(c: App) {
  const now = nowSeconds();
  const [view, usage, catalog] = await Promise.all([tasksView(c.env), taskUsage(c.env, now), catalogView(c.env, now)]);
  const s = await summary(c.env, now, { catalog, tasks: view });
  const editing = c.req.query('edit') ?? null;
  const highlight = c.req.query('model') ?? null;
  const models = view.models.filter((m) => m.enabled || view.rows.some((r) => r.serve.some((a) => a.model_id === m.id)));
  const unpriced = new Set(catalog.models.filter((m) => m.routes.length && m.routes.every(isUnpriced)).map((m) => m.id));
  const global = view.rows.find((r) => r.pattern === '*')!;
  const ownCount = view.rows.filter((r) => r.level !== 'global' && r.serve.length).length;
  const taskCount = view.rows.filter((r) => r.level === 'task').length;
  const cols = 6;
  const hit = (row: TaskRowView) => !!highlight && row.effective.chain.some((l) => l.model_id === highlight);
  const rowsFor = (row: TaskRowView, label: Child, group = false) => [
    <Row row={row} models={models} unpriced={unpriced} usage={usage.get(row.pattern)} group={group} hit={hit(row)}
      open={editing === row.pattern} label={label} />,
    <Editor row={row} models={models} open={editing === row.pattern} cols={cols} />,
  ];
  const [first, ...rest] = global.effective.chain;

  return page(c, await aiShell(c, TASKS_PAGE, (
    <>
      {view.task_routing ? null : <Note warn>No task has a model yet, so every call is served by
        {global.effective.level === 'legacy' ? ' the previous route table' : ' the built-in defaults'}. Choosing the
        general model below switches Plexora to task routing{global.effective.level === 'legacy'
          ? ', so migrate first (Settings)' : ''}.</Note>}
      {!catalog.models.length ? (
        <div class="next-step">No model is approved yet: <a href={`${BASE}/models`}>add models</a> first.</div>
      ) : null}
      <Toolbar>
        <span class="grow">General model <b>{first?.name ?? 'built-in default'}</b>
          {rest.length ? <> › {rest.map((l) => l.name).join(' › ')}</> : null} · {taskCount} tasks · {ownCount} set on
          their own</span>
        {highlight ? <span>Where <b class="mono">{highlight}</b> serves · <a href={TASKS_PAGE}>clear</a></span> : null}
      </Toolbar>
      <div class="scroll">
        <table class="dense tree">
          <thead>
            <tr>{['Task', 'Needs', 'Model › fallbacks', 'Limits', '30 days', ''].map((h, i) => (
              <th class={i === 4 ? 'right' : undefined}>{h}</th>))}</tr>
          </thead>
          <tbody>{rowsFor(global, <b>All tasks</b>, true)}</tbody>
          {view.modules.map((m) => {
            const moduleRow = view.rows.find((r) => r.pattern === `${m.id}.*`)!;
            return (
              <tbody>
                {rowsFor(moduleRow, <b>{moduleLabel(m.id)}</b>, true)}
                {m.tasks.map((t) => {
                  const row = view.rows.find((r) => r.pattern === t.id)!;
                  return rowsFor(row, <>{t.label}<div class="sub">{t.blurb}</div></>);
                })}
              </tbody>
            );
          })}
        </table>
      </div>
      {view.rows.filter((r) => r.level === 'task' && !view.modules.some((m) => m.tasks.some((t) => t.id === r.pattern)))
        .map((r) => (
          <Note warn>{r.pattern} is assigned but is no longer a task Plexora names.
            {' '}<Action action={`${API}/tasks/${encodeURIComponent(r.pattern)}`} method="DELETE" icon="x" iconOnly
              tone="danger" label={`Remove the ${r.pattern} assignment`} reload /></Note>
        ))}
      <p class="hint">Blank inherits; a model marked “(no vision)”, “(no reasoning)” or “(no price)” cannot serve that
        row until fixed on Models. The pencil holds effort, caps, requirements and shadow comparisons.</p>
    </>
  ), { summary: s }));
}
