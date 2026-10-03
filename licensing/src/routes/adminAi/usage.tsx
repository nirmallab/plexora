/** @jsxImportSource hono/jsx */
/**
 * /admin/ai/usage: calls, provider cost and what accounts were charged, by
 * task, model and provider; the rest (accounts, recent calls, shadow
 * agreement, price changes) folded below.
 */
import { DAY, nowSeconds } from '../../env';
import { patternLabel, TASKS } from '../../ai/tasks';
import { priceHistory, usageView } from '../../ai/views';
import { type App, page } from '../../http';
import { Badge, Bars, Disclosure, Empty, Section, Seg, Stat, Table, Toolbar } from '../../ui/components';
import { dateTime, ms, pct, perMillion, usd } from '../../ui/format';
import { shadowReport } from '../ai';
import { aiShell, BASE } from './shared';

const USAGE = `${BASE}/usage`;

/** A task id as a person reads it; a call from an older client shows its feature and capability. */
function taskName(task: string): string {
  if (TASKS[task]) return patternLabel(task);
  const legacy = /^(.*)\.\((.*)\)$/.exec(task);
  return legacy ? `${legacy[1] === '_' ? 'no feature' : legacy[1]} (${legacy[2]}, no task named)` : task;
}

export async function usagePage(c: App) {
  const now = nowSeconds();
  const days = [7, 30, 90].includes(Number(c.req.query('days'))) ? Number(c.req.query('days')) : 30;
  const [u, shadow, prices] = await Promise.all([usageView(c.env, days, now),
    shadowReport(c.env, (now - days * DAY) * 1000), priceHistory(c.env, { days }, now)]);
  const t = u.totals as Record<string, number>;
  const cost = t.cost_micro ?? 0;
  const charged = t.charged_micro ?? 0;
  const dayKeys = Array.from({ length: days }, (_, i) => new Date((now - (days - 1 - i) * DAY) * 1000).toISOString().slice(0, 10));
  const byDay = new Map(u.by_day.map((d) => [d.day, d.cost_micro ?? 0]));
  const compared = shadow.reduce((n, s) => n + (s.compared ?? 0), 0);
  const agreed = shadow.reduce((n, s) => n + (s.agreed ?? 0), 0);

  return page(c, await aiShell(c, USAGE, (
    <>
      <Toolbar>
        <span class="grow">The last <b>{days} days</b>, shadow calls excluded</span>
        <Seg label="Period" active={`${USAGE}?days=${days}`} items={[[`${USAGE}?days=7`, '7 days'],
          [`${USAGE}?days=30`, '30 days'], [`${USAGE}?days=90`, '90 days']]} />
      </Toolbar>
      <div class="stats">
        <Stat label="Calls" value={(t.calls ?? 0).toLocaleString('en')}
          sub={t.calls ? `${t.failed ?? 0} failed · ${t.failovers ?? 0} failed over` : undefined} />
        <Stat label="Provider cost" value={usd(cost)} />
        <Stat label="Charged" value={usd(charged)} />
        <Stat label="Margin" value={usd(charged - cost)} sub={charged ? pct(charged - cost, charged) : undefined} />
        <Stat label="Input from cache" value={pct(t.cache_read ?? 0, t.input_total ?? 0)} />
        <Stat label="Shadow agreement" value={compared ? pct(agreed, compared) : '—'}
          sub={compared ? `${compared} compared` : undefined} />
      </div>
      <Bars values={dayKeys.map((d) => byDay.get(d) ?? 0)} labels={dayKeys.map((d) => `${d}, ${usd(byDay.get(d) ?? 0)}`)}
        label="Provider cost per day" />
      <div class="axis"><span>{dayKeys[0]}</span><span>provider cost per day</span><span>{dayKeys[dayKeys.length - 1]}</span></div>

      <Section title="By task">
        {u.by_task.length === 0 ? <Empty>No calls yet.</Empty> : (
          <Table class="dense" head={['Task', 'Calls', 'Failed', 'Cost', 'Charged']} right={[1, 2, 3, 4]}>
            {u.by_task.map((r) => (
              <tr>
                <td>{taskName(r.task)}<div class="sub mono">{r.task}</div></td>
                <td class="right">{r.calls.toLocaleString('en')}</td>
                <td class="right">{r.failed || '—'}</td>
                <td class="right">{usd(r.cost_micro)}</td>
                <td class="right">{usd(r.charged_micro)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Section>
      <div class="grid two">
        <Section title="By model">
          {u.by_model.length === 0 ? <Empty>No calls yet.</Empty> : (
            <Table class="dense" head={['Model', 'Calls', 'Cost', 'Cache']} right={[1, 2, 3]}>
              {u.by_model.map((r) => (
                <tr>
                  <td class="mono">{r.model_id}</td>
                  <td class="right">{r.calls.toLocaleString('en')}{r.failed ? <div class="sub">{r.failed} failed</div> : null}</td>
                  <td class="right">{usd(r.cost_micro)}</td>
                  <td class="right">{pct(r.cache_read ?? 0, r.input_total ?? 0)}</td>
                </tr>
              ))}
            </Table>
          )}
        </Section>
        <Section title="By provider">
          {u.by_provider.length === 0 ? <Empty>No calls yet.</Empty> : (
            <Table class="dense" head={['Provider', 'Calls', 'Failed', 'Failed over', 'Cost']} right={[1, 2, 3, 4]}>
              {u.by_provider.map((r) => (
                <tr>
                  <td class="mono">{r.provider}</td>
                  <td class="right">{r.calls.toLocaleString('en')}</td>
                  <td class="right">{r.failed || '—'}</td>
                  <td class="right">{r.failovers || '—'}</td>
                  <td class="right">{usd(r.cost_micro)}</td>
                </tr>
              ))}
            </Table>
          )}
        </Section>
      </div>

      <Disclosure summary="By account">
        {u.accounts.length === 0 ? <Empty>No calls yet.</Empty> : (
          <Table class="dense" head={['Account', 'Calls', 'Cost', 'Charged']} right={[1, 2, 3]}>
            {u.accounts.map((a) => (
              <tr>
                <td>{a.account_name ?? a.account_id}<div class="sub mono">{a.account_id}</div></td>
                <td class="right">{a.calls.toLocaleString('en')}</td>
                <td class="right">{usd(a.cost_micro)}</td>
                <td class="right">{usd(a.charged_micro)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Disclosure>
      <Disclosure summary="Recent calls">
        {u.recent.length === 0 ? <Empty>No calls yet.</Empty> : (
          <Table class="dense" head={['When', 'Task', 'Served by', 'State', 'First byte', 'Cost']} right={[4, 5]}>
            {u.recent.map((r) => (
              <tr>
                <td class="nowrap small">{dateTime(Math.floor(r.started_at_ms / 1000))}</td>
                <td class="small">{r.task ? taskName(r.task) : <span class="dim">{r.feature ?? '—'} · {r.capability}</span>}</td>
                <td class="small"><span class="mono">{r.model_id ?? r.model}</span><div class="sub">{r.provider}</div></td>
                <td>{r.status === 'ok' ? <Badge tone="ok">ok</Badge> : <Badge tone="bad">{r.failure_class ?? r.status}</Badge>}
                  {r.failover ? <> <Badge tone="warn">failed over</Badge></> : null}</td>
                <td class="right small">{r.first_byte_ms ? ms(r.first_byte_ms - r.started_at_ms) : '—'}</td>
                <td class="right small">{usd(r.cost_micro)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Disclosure>
      <Disclosure summary="Shadow comparisons">
        {shadow.length === 0 ? <Empty>No shadow calls in this period.</Empty> : (
          <Table class="dense" head={['Candidate', 'For', 'Calls', 'Agreement', 'Cost']} right={[2, 3, 4]}>
            {shadow.map((s) => (
              <tr>
                <td class="mono">{s.model_id ?? s.model}<div class="sub">{s.provider}</div></td>
                <td class="small">{s.task ? taskName(s.task) : `${s.feature ?? '—'} · ${s.capability}`}</td>
                <td class="right">{s.calls}</td>
                <td class="right">{s.agreement === null ? '—' : `${Math.round(s.agreement * 100)}%`}
                  <div class="sub">{s.compared} compared</div></td>
                <td class="right">{usd(s.cost_micro)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Disclosure>
      <Disclosure summary="Price changes">
        {prices.length === 0 ? <Empty>No price changed in this period.</Empty> : (
          <Table class="dense" head={['When', 'Model', 'Provider', 'Change']}>
            {prices.map((h: Record<string, any>) => (
              <tr>
                <td class="nowrap small">{dateTime(h.at)}</td>
                <td><a href={`${BASE}/models/${h.model_id}`} class="mono">{h.model_id}</a></td>
                <td class="mono">{h.provider}</td>
                <td class="small">{(h.changes ?? []).map((x: { field: string; old: number; new: number }) =>
                  `${x.field.replace(/_micro$/, '')} ${x.field === 'fee_bps' ? x.old : `$${perMillion(x.old)}`} → ${
                    x.field === 'fee_bps' ? x.new : `$${perMillion(x.new)}`}`).join(' · ')}</td>
              </tr>
            ))}
          </Table>
        )}
      </Disclosure>
    </>
  )));
}
