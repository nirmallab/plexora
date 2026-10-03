/** @jsxImportSource hono/jsx */
/**
 * /admin/ai/routing: which approved model does each task, by module.
 *
 * Every row shows what serves it NOW (from the resolver a call uses): its own
 * assignment, or the module default or global default it inherits. Editing a
 * row opens its editor in place; "Reset" makes it inherit again. Providers do
 * not appear here: a model's provider order is the model's own (Models page).
 */
import type { Child } from 'hono/jsx';

import { type CatalogRow, type TaskRouteRow } from '../../ai/routing';
import { moduleLabel, patternLabel } from '../../ai/tasks';
import { eligibility, type TaskRowView, tasksView, taskUsage } from '../../ai/views';
import { nowSeconds } from '../../env';
import { type App, page } from '../../http';
import { Action, Badge, Disclosure, Field, JsonForm, Note, SelectField, Toolbar } from '../../ui/components';
import { ms, tokens, usd } from '../../ui/format';
import { aiShell, API, BASE } from './shared';

const ROUTING = `${BASE}/routing`;

const editorId = (pattern: string) => `ed-${pattern === '*' ? 'all' : pattern.replace('.*', '-default').replace('.', '-')}`;

function needs(row: TaskRowView): string {
  if (row.level !== 'task') return '';
  return [row.requires.vision ? 'vision' : null, row.requires.reasoning ? 'reasoning' : null].filter(Boolean).join(' · ')
    || 'text';
}

function limitsOf(a: TaskRouteRow | undefined): string {
  if (!a) return '';
  return [a.effort ? `effort ${a.effort}` : null, a.max_tokens_cap ? `cap ${tokens(a.max_tokens_cap)}` : null,
    a.max_cost_micro ? `≤ ${usd(a.max_cost_micro)}` : null, a.latency_ms ? `≤ ${ms(a.latency_ms)}` : null]
    .filter(Boolean).join(' · ');
}

function ModelCell(props: { row: TaskRowView }) {
  const { row } = props;
  const e = row.effective;
  const first = e.chain[0];
  const own = row.serve.length > 0 && e.source === row.pattern;
  const passed = e.skipped.filter((s) => s.pattern === row.pattern);
  return (
    <>
      {own ? <><span class="set-here">{first?.name}</span> {row.level === 'global' ? null : <Badge tone="accent">set here</Badge>}</>
        : first ? <span class="inherit">{first.name}</span> : <span class="dim">—</span>}
      {!own && e.source ? <div class="sub">from {patternLabel(e.source)}</div> : null}
      {e.level === 'builtin' ? <div class="sub"><Badge tone="warn">built-in default</Badge></div> : null}
      {e.level === 'legacy' ? <div class="sub"><Badge tone="warn">previous route table</Badge></div> : null}
      {passed.map((s) => <div class="sub"><Badge tone="bad">{s.model_id}: {s.reason}</Badge></div>)}
      {row.mismatch.map((m) => <div class="sub"><Badge tone="warn">{m.name}: {m.reason}</Badge></div>)}
    </>
  );
}

function Editor(props: { row: TaskRowView; models: CatalogRow[]; open: boolean; cols: number }) {
  const { row, models } = props;
  const own = row.serve;
  const head = own[0];
  const shadow = row.shadow[0];
  const choices = (blank: string) => [{ value: '', label: blank }, ...models.map((m) => {
    const why = eligibility(m, row.pattern);
    return { value: m.id, label: why ? `${m.name} (${why})` : m.name, disabled: !!why && !own.some((a) => a.model_id === m.id) };
  })];
  const parent = row.parent ? patternLabel(row.parent) : 'the built-in default';
  const tri = (v: number | null | undefined) => (v === null || v === undefined ? 'inherit' : v ? '1' : '0');
  return (
    <tr class="editor" id={editorId(row.pattern)} hidden={props.open ? undefined : true}>
      <td colspan={props.cols}>
        <JsonForm action={`${API}/tasks/${encodeURIComponent(row.pattern)}`} method="PUT" submit="Save" reload
          done={`${patternLabel(row.pattern)} saved.`} confirm={row.pattern === '*'
            ? 'Change the model every task without its own assignment uses?' : undefined}>
          <div class="form-grid three">
            <SelectField label="Primary model" name="primary" keepEmpty value={own[0]?.model_id ?? ''}
              options={choices(row.level === 'global' ? 'built-in default' : `inherit from ${parent}`)} />
            <SelectField label="Fallback 1" name="fallback_1" keepEmpty value={own[1]?.model_id ?? ''} options={choices('none')} />
            <SelectField label="Fallback 2" name="fallback_2" keepEmpty value={own[2]?.model_id ?? ''} options={choices('none')} />
          </div>
          <Disclosure summary="Advanced" open={!!(head && (head.effort || head.max_tokens_cap || head.max_cost_micro ||
            head.latency_ms || head.requires_vision !== null || head.requires_reasoning !== null)) || !!shadow}>
            <div class="form-grid three">
              <SelectField label="Reasoning effort" name="effort" value={head?.effort ?? ''} options={[
                { value: '', label: "the model's default" }, { value: 'low', label: 'low' }, { value: 'medium', label: 'medium' },
                { value: 'high', label: 'high' }]} />
              <Field label="Output cap, tokens" name="max_tokens_cap" type="number" num min={1} max={128000}
                value={head?.max_tokens_cap} hint="Empty: the task's own." />
              <Field label="Cost cap per call, $" name="max_cost_usd" type="number" step="any" min={0}
                value={head?.max_cost_micro ? head.max_cost_micro / 1_000_000 : undefined}
                hint="A model whose estimate for a call is above it is passed over." />
              <Field label="Preferred first byte, ms" name="latency_ms" type="number" num min={1}
                value={head?.latency_ms} hint="Slower providers of a model are tried after faster ones." />
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
          </Disclosure>
        </JsonForm>
        {own.length || row.shadow.length ? (
          <p class="small">
            <Action action={`${API}/tasks/${encodeURIComponent(row.pattern)}`} method="DELETE" tone="ghost" small reload
              label={row.level === 'global' ? 'Back to the built-in default' : `Reset to ${parent}`}
              confirm={`${patternLabel(row.pattern)} will use ${parent} again.`} />
          </p>
        ) : null}
      </td>
    </tr>
  );
}

function Row(props: { row: TaskRowView; usage: { calls: number; cost_micro: number } | undefined; group?: boolean;
  hit: boolean; label: Child }) {
  const { row } = props;
  const rest = row.effective.chain.slice(1);
  const serving = row.serve.length ? row.serve[0] : undefined;
  const cls = [props.hit ? 'hit' : ''].filter(Boolean).join(' ');
  return (
    <tr class={cls || undefined}>
      <td class={props.group ? 'group' : 'task'}>{props.label}</td>
      <td class={props.group ? 'group caps' : 'caps'}>{needs(row)}</td>
      <td class={props.group ? 'group' : undefined}><ModelCell row={row} /></td>
      <td class={props.group ? 'group small' : 'small'}>{rest.map((l) => l.name).join(', ') || <span class="dim">—</span>}</td>
      <td class={props.group ? 'group small' : 'small'}>{limitsOf(serving) || <span class="dim">—</span>}
        {row.shadow[0] ? <div class="sub">shadow {row.shadow[0].model_id} · {row.shadow[0].shadow_pct}%</div> : null}</td>
      <td class={props.group ? 'group small right nowrap' : 'small right nowrap'}>{props.usage
        ? <>{props.usage.calls.toLocaleString('en')}<div class="sub">{usd(props.usage.cost_micro)}</div></> : <span class="dim">—</span>}</td>
      <td class={props.group ? 'group right' : 'right'}>
        <button type="button" class="ghost tiny" data-toggle={`#${editorId(row.pattern)}`} aria-expanded="false"
          aria-controls={editorId(row.pattern)}>Edit</button>
      </td>
    </tr>
  );
}

export async function routingPage(c: App) {
  const now = nowSeconds();
  const [view, usage] = await Promise.all([tasksView(c.env), taskUsage(c.env, now)]);
  const editing = c.req.query('edit') ?? null;
  const highlight = c.req.query('model') ?? null;
  const models = view.models.filter((m) => m.enabled || view.rows.some((r) => r.serve.some((a) => a.model_id === m.id)));
  const global = view.rows.find((r) => r.pattern === '*')!;
  const ownCount = view.rows.filter((r) => r.level !== 'global' && r.serve.length).length;
  const taskCount = view.rows.filter((r) => r.level === 'task').length;
  const cols = 7;
  const hit = (row: TaskRowView) => !!highlight && row.effective.chain.some((l) => l.model_id === highlight);
  const rowsFor = (row: TaskRowView, label: Child, group = false) => [
    <Row row={row} usage={usage.get(row.pattern)} group={group} hit={hit(row)} label={label} />,
    <Editor row={row} models={models} open={editing === row.pattern} cols={cols} />,
  ];
  const [first, ...rest] = global.effective.chain;

  return page(c, aiShell(c, ROUTING, (
    <>
      {view.task_routing ? null : <Note warn>No task has an assignment yet, so every call is still served by
        {global.effective.level === 'legacy' ? ' the previous route table' : ' the built-in defaults'}. Saving any row
        switches Plexora to task routing{global.effective.level === 'legacy' ? ', so migrate first (Settings)' : ''}.</Note>}
      <Toolbar>
        <span class="grow">Every task uses <b>{first?.name ?? 'the built-in default'}</b>
          {rest.length ? <> → {rest.map((l) => l.name).join(' → ')}</> : null} unless its module or the task says
          otherwise · {taskCount} tasks · {ownCount} set on their own</span>
        {highlight ? <span>Showing where <b class="mono">{highlight}</b> serves · <a href={ROUTING}>clear</a></span> : null}
      </Toolbar>
      <div class="scroll">
        <table class="dense tree">
          <thead>
            <tr>{['Task', 'Needs', 'Model', 'Fallbacks', 'Limits', '30 days', ''].map((h, i) => (
              <th class={i === 5 ? 'right' : undefined}>{h}</th>))}</tr>
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
            <Action action={`${API}/tasks/${encodeURIComponent(r.pattern)}`} method="DELETE" label="Remove" tone="ghost"
              small reload /></Note>
        ))}
      <p class="hint">A model offered as “(no vision)” or “(no reasoning)” cannot do what that task needs; its abilities
        are set on its page. Effort, caps and shadow comparisons are under Advanced.</p>
    </>
  )));
}
