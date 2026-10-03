/** @jsxImportSource hono/jsx */
/**
 * /admin/ai/providers: who Plexora can call, whether their keys are set
 * (the keys themselves are on /admin/ai/api), what their price lists said last, and the switch that takes one out of every
 * route at once.
 */
import { catalogView } from '../../ai/views';
import { nowSeconds } from '../../env';
import { type App, page } from '../../http';
import { Action, Badge, Empty, Section, Table } from '../../ui/components';
import { ago } from '../../ui/format';
import { aiShell, API, BASE } from './shared';

const parse = (text: string | null): Record<string, unknown> | null => {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
};

/** "$123.40 left · 20 req / 10s", from whatever the provider exposes. */
function limits(balance: string | null, rate: string | null): string {
  const b = parse(balance);
  const r = parse(rate);
  const parts: string[] = [];
  if (b?.remaining !== null && b?.remaining !== undefined) parts.push(`$${Number(b.remaining).toFixed(2)} left`);
  else if (b?.usage !== null && b?.usage !== undefined) parts.push(`$${Number(b.usage).toFixed(2)} used`);
  if (b?.free_tier === true) parts.push('free tier');
  // OpenRouter reports -1 for "no request limit".
  if (typeof r?.requests === 'number' && r.requests > 0) parts.push(`${r.requests} req / ${r.interval ?? '?'}`);
  else if (typeof r?.requests === 'number' && r.requests < 0) parts.push('no request limit');
  return parts.join(' · ');
}

export async function providersPage(c: App) {
  const now = nowSeconds();
  const view = await catalogView(c.env, now);
  const open = view.circuits.filter((x) => x.route_key.includes(':'));

  return page(c, aiShell(c, `${BASE}/providers`, (
    <>
      <Table class="dense" head={['Provider', 'Key', 'Price list', 'Balance and limits', 'Routes', '']}>
        {view.providers.map((p) => (
          <tr class={p.forced ? 'off' : undefined}>
            <td><b class="mono">{p.provider}</b>
              <div class="sub">{p.direct ? 'direct' : 'aggregator'} · {p.wire}{p.provider === 'saygm'
                ? ' · confidential (TEE) models only' : ''}</div></td>
            <td><a href={`${BASE}/api`}>{p.configured ? <Badge tone="ok">set</Badge> : <Badge>not set</Badge>}</a></td>
            <td class="small">{p.price_api ? (p.status ? <>
              {p.status.ok ? <Badge tone="ok">read</Badge> : <Badge tone="bad">failed</Badge>} {ago(p.status.checked_at, now)}
              <div class="sub">{p.status.ok ? `${p.status.models_seen} models listed${p.status.changes
                ? `, ${p.status.changes} price change${p.status.changes === 1 ? '' : 's'}` : ''}` : p.status.error}</div>
            </> : <span class="dim">not read yet</span>) : <span class="dim">no price API: list prices</span>}</td>
            <td class="small">{p.status ? limits(p.status.balance_json, p.status.rate_limit_json) || <span class="dim">—</span>
              : <span class="dim">—</span>}</td>
            <td class="small">{p.routes ? <a href={`${BASE}/models?provider=${p.provider}`}>{p.routes} route
              {p.routes === 1 ? '' : 's'}</a> : <span class="dim">none</span>}</td>
            <td class="actions">
              {p.price_api ? <Action action={`${API}/pricing/refresh`} body={{ provider: p.provider }} label="Refresh prices"
                tone="ghost" small reload /> : null}
              {' '}{p.forced
                ? <Action action={`${API}/providers/${p.provider}/enable`} label="Switch on" tone="ghost" small reload />
                : <Action action={`${API}/providers/${p.provider}/disable`} body={{ reason: 'admin page' }} label="Switch off"
                  tone="danger" small reload confirm={`Stop every call to ${p.provider}? Its routes are skipped until it is switched on.`} />}
              {p.forced ? <div class="sub">switched off {ago(p.forced.at, now)}{p.forced.reason ? `: ${p.forced.reason}` : ''}</div>
                : null}
            </td>
          </tr>
        ))}
      </Table>
      <p class="hint">A provider without a key is skipped, never called. Keys are added, tested and replaced on
        the <a href={`${BASE}/api`}>API</a> page (or set as Worker secrets). Price lists are read nightly at 03:30 UTC.</p>

      {open.length ? (
        <Section title="Open circuits">
          <Table class="dense" head={['Route', 'State', '']}>
            {open.map((x) => (
              <tr>
                <td class="mono">{x.route_key}</td>
                <td>{x.forced ? <Badge tone="bad">switched off</Badge> : <Badge tone="warn">circuit open</Badge>}
                  <div class="sub">{x.reason ?? `${x.fail} failures`} · {ago(x.updated_at, now)}</div></td>
                <td class="right">{x.forced ? <Action action={`${API}/providers/${encodeURIComponent(x.route_key)}/enable`}
                  label="Switch on" tone="ghost" small reload /> : null}</td>
              </tr>
            ))}
          </Table>
        </Section>
      ) : view.providers.every((p) => !p.forced) ? null : <Empty>No model-level circuit is open.</Empty>}
    </>
  )));
}
