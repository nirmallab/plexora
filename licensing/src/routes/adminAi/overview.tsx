/** @jsxImportSource hono/jsx */
/**
 * /admin/ai, step 4: is anything wrong, and what serves now.
 *
 * The warnings first, each dismissible until it changes (and back with Undo
 * under "dismissed"); then one line summing up the four steps, each cell a
 * link to its page; what each scope goes to; and the capacity card.
 */
import { assess, chainsOf, gather } from '../../ai/capacity';
import { moduleLabel } from '../../ai/tasks';
import { catalogView, type Problem, problems, providersView, summary, tasksView, usageView } from '../../ai/views';
import { nowSeconds } from '../../env';
import { type App, page } from '../../http';
import { Action, Badge, Icon, Section, Table } from '../../ui/components';
import { usd } from '../../ui/format';
import { CapacityCard } from './capacity';
import { aiShell, API, BASE } from './shared';

function ProblemLine(props: { p: Problem }) {
  const { p } = props;
  const key = encodeURIComponent(p.key);
  return (
    <li class={p.tone === 'bad' ? 'bad' : undefined}>
      <Icon name={p.tone === 'bad' ? 'alert' : 'info'} />
      <span class="grow">{p.text}</span>
      {!p.dismissed && p.fix ? <Action action={p.fix.action} body={p.fix.body} label={p.fix.label} tone="ghost" small reload />
        : null}
      {p.href ? <a href={p.href}>Open</a> : null}
      {p.dismissed
        ? <Action action={`${API}/problems/${key}/dismiss`} method="DELETE" label="Undo" tone="ghost" small reload
          done="Shown again." />
        : <Action action={`${API}/problems/${key}/dismiss`} icon="x" iconOnly label="Dismiss this warning" reload
          done="Dismissed. It comes back if it changes." />}
    </li>
  );
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;

export async function overviewPage(c: App) {
  const now = nowSeconds();
  const [catalog, tasks, usage] = await Promise.all([catalogView(c.env, now), tasksView(c.env),
    usageView(c.env, 30, now)]);
  const capacity = await gather(c.env, now * 1000, chainsOf(tasks.rows)).then(assess);
  const [found, providers] = await Promise.all([problems(c.env, now, catalog, tasks, { capacity }),
    providersView(c.env, now, catalog)]);
  const s = await summary(c.env, now, { catalog, tasks, providers, problems: found });
  const showDismissed = c.req.query('dismissed') === '1';
  const open = found.filter((p) => !p.dismissed);
  const dismissed = found.filter((p) => p.dismissed);
  const t = usage.totals as Record<string, number>;
  const rowOf = (pattern: string) => tasks.rows.find((r) => r.pattern === pattern)!;
  const scopes = ['*', ...tasks.modules.map((m) => `${m.id}.*`)];
  const states = Object.entries(s.providers.by_state).filter(([state]) => state !== 'connected')
    .map(([state, n]) => `${n} ${state.replace('_', ' ')}`).join(' · ');
  const degraded = s.fallback.open_circuits + s.fallback.forced.length;

  return page(c, await aiShell(c, BASE, (
    <>
      {open.length ? (
        <ul class="problems" aria-label="Warnings">{open.map((p) => <ProblemLine p={p} />)}</ul>
      ) : <p class="all-clear">Nothing needs attention.</p>}
      {dismissed.length ? (
        showDismissed ? <>
          <ul class="problems dismissed" aria-label="Dismissed warnings">{dismissed.map((p) => <ProblemLine p={p} />)}</ul>
          <p class="problems-foot">{dismissed.length} dismissed · <a href={BASE}>hide</a></p>
        </> : <p class="problems-foot">{dismissed.length} dismissed · <a href={`${BASE}?dismissed=1`}>show</a></p>
      ) : null}

      <div class="strip">
        <a href={`${BASE}/providers`}><span class="k">Providers</span>
          <span class="v">{s.providers.connected}/{s.providers.total} connected</span>
          <span class="s">{states || 'all connected'}</span></a>
        <a href={`${BASE}/models`}><span class="k">Models</span>
          <span class="v">{s.models.active} active</span>
          <span class="s">{s.models.unused} unused{s.models.unpriced ? ` · ${s.models.unpriced} need a price` : ''}</span></a>
        <a href={`${BASE}/tasks`}><span class="k">General model</span>
          <span class="v">{s.general.model ?? 'built-in default'}</span>
          <span class="s">{s.general.chain.length > 1 ? `then ${s.general.chain.slice(1).join(', ')}` : 'no fallback'}</span></a>
        <a href={`${BASE}/models`}><span class="k">Fallback</span>
          <span class="v">{degraded ? 'degraded' : 'ok'}</span>
          <span class="s">{[s.fallback.open_circuits ? plural(s.fallback.open_circuits, 'open circuit') : null,
            s.fallback.forced.length ? `${s.fallback.forced.join(', ')} off` : null,
            s.fallback.routes_without_fallback ? `${s.fallback.routes_without_fallback} without a fallback` : null]
            .filter(Boolean).join(' · ') || 'every chain has one'}</span></a>
        <a href={`${BASE}/tasks`}><span class="k">Tasks</span>
          <span class="v">{s.tasks.issues ? plural(s.tasks.issues, 'issue') : 'no issues'}</span>
          <span class="s">{s.tasks.own} of {s.tasks.total} set on their own</span></a>
        <a href={`${BASE}/usage`}><span class="k">30 days</span>
          <span class="v">{plural(t.calls ?? 0, 'call')}</span>
          <span class="s">cost {usd(t.cost_micro ?? 0)} · charged {usd(t.charged_micro ?? 0)}</span></a>
      </div>

      <Section title="Serving now" actions={<a href={`${BASE}/tasks`} class="small">Tasks ›</a>}>
        <Table head={['Scope', 'Model › fallbacks', 'Set on their own']} class="dense tight">
          {scopes.map((pattern) => {
            const row = rowOf(pattern);
            const module = pattern === '*' ? null : pattern.slice(0, -2);
            const differ = module ? tasks.rows.filter((r) => r.level === 'task' && r.pattern.startsWith(`${module}.`) &&
              r.serve.length).length : tasks.rows.filter((r) => r.level !== 'global' && r.serve.length).length;
            const names = row.effective.chain.map((l) => l.name);
            return (
              <tr>
                <td>{module ? moduleLabel(module) : <b>All tasks</b>}</td>
                <td>{row.serve.length || pattern === '*' ? <span class="set-here">{names[0] ?? '—'}</span>
                  : <span class="inherit">{names[0] ?? '—'}</span>}
                  {names.length > 1 ? <span class="small muted"> › {names.slice(1).join(' › ')}</span> : null}
                  {row.effective.level === 'builtin' ? <> <Badge tone="warn">built-in</Badge></> : null}
                  {row.effective.level === 'legacy' ? <> <Badge tone="warn">route table</Badge></> : null}</td>
                <td class="small">{differ || <span class="dim">—</span>}</td>
              </tr>
            );
          })}
        </Table>
      </Section>

      <CapacityCard a={capacity} />
    </>
  ), { summary: s }));
}
