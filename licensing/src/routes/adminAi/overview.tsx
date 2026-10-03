/** @jsxImportSource hono/jsx */
/**
 * /admin/ai: is anything wrong, what does it cost, and what serves now.
 * Read only; every problem links to the page that fixes it.
 */
import { catalogView, problems, tasksView, usageView } from '../../ai/views';
import { moduleLabel } from '../../ai/tasks';
import { nowSeconds } from '../../env';
import { type App, page } from '../../http';
import { Action, Badge, Card, Stat, Table } from '../../ui/components';
import { ago, pct, usd } from '../../ui/format';
import { aiShell, BASE } from './shared';

export async function overviewPage(c: App) {
  const now = nowSeconds();
  const [catalog, tasks, usage] = await Promise.all([catalogView(c.env, now), tasksView(c.env),
    usageView(c.env, 30, now)]);
  const found = await problems(c.env, now, catalog, tasks);
  const t = usage.totals as Record<string, number>;
  const cost = t.cost_micro ?? 0;
  const charged = t.charged_micro ?? 0;
  const rowOf = (pattern: string) => tasks.rows.find((r) => r.pattern === pattern)!;
  const scopes = ['*', ...tasks.modules.map((m) => `${m.id}.*`)];

  return page(c, aiShell(c, BASE, (
    <>
      {found.length ? (
        <ul class="problems" aria-label="Problems">
          {found.map((p) => (
            <li class={p.tone === 'bad' ? 'bad' : undefined}>
              <span class="grow">{p.text}</span>
              {p.fix ? <Action action={p.fix.action} body={p.fix.body} label={p.fix.label} tone="ghost" small reload /> : null}
              {p.href ? <a href={p.href}>Open</a> : null}
            </li>
          ))}
        </ul>
      ) : <p class="all-clear">Nothing needs attention.</p>}

      <div class="stats">
        <Stat label="Calls, 30 days" value={(t.calls ?? 0).toLocaleString('en')}
          sub={t.failed ? `${t.failed} failed` : undefined} />
        <Stat label="Provider cost" value={usd(cost)} />
        <Stat label="Charged" value={usd(charged)} />
        <Stat label="Margin" value={usd(charged - cost)} sub={charged ? pct(charged - cost, charged) : undefined} />
      </div>

      <div class="grid two">
        <Card title="Serving now" sub="The model each scope goes to first. A task set on its own is counted under Differ."
          actions={<a href={`${BASE}/routing`}>Task routing</a>}>
          <Table head={['Scope', 'Primary', 'Then', 'Differ']} class="dense">
            {scopes.map((pattern) => {
              const row = rowOf(pattern);
              const [first, ...rest] = row.effective.chain;
              const module = pattern === '*' ? null : pattern.slice(0, -2);
              const differ = module ? tasks.rows.filter((r) => r.level === 'task' && r.pattern.startsWith(`${module}.`) &&
                r.serve.length).length : tasks.rows.filter((r) => r.level !== 'global' && r.serve.length).length;
              return (
                <tr>
                  <td>{module ? moduleLabel(module) : <b>All tasks</b>}</td>
                  <td>{row.serve.length || pattern === '*' ? <span class="set-here">{first?.name ?? '—'}</span>
                    : <span class="inherit">{first?.name ?? '—'}</span>}
                    {row.effective.level === 'builtin' ? <div class="sub"><Badge tone="warn">built-in</Badge></div> : null}
                    {row.effective.level === 'legacy' ? <div class="sub"><Badge tone="warn">route table</Badge></div> : null}</td>
                  <td class="small">{rest.map((l) => l.name).join(', ') || '—'}</td>
                  <td class="right">{differ || '—'}</td>
                </tr>
              );
            })}
          </Table>
        </Card>
        <Card title="Providers" actions={<a href={`${BASE}/providers`}>Providers</a>}>
          <Table head={['Provider', 'Key', 'State', 'Prices']} class="dense">
            {catalog.providers.map((p) => (
              <tr>
                <td class="mono">{p.provider}</td>
                <td>{p.configured ? <Badge tone="ok">set</Badge> : <span class="dim">none</span>}</td>
                <td>{p.forced ? <Badge tone="bad">switched off</Badge> : p.status && !p.status.ok
                  ? <Badge tone="warn">list failed</Badge> : p.routes ? <span class="small">{p.routes} route{p.routes === 1 ? '' : 's'}</span>
                    : <span class="dim">unused</span>}</td>
                <td class="small">{p.price_api ? (p.status ? ago(p.status.checked_at, now) : 'not read yet')
                  : <span class="dim">list prices</span>}</td>
              </tr>
            ))}
          </Table>
        </Card>
      </div>
    </>
  )));
}
