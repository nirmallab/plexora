/** @jsxImportSource hono/jsx */
/**
 * /admin/ai/providers, step 1: connect the providers Plexora calls.
 *
 * One row per provider: whether it is connected (its key set and accepted),
 * the key's last four, when it was checked, how many models it serves, and
 * its balance. The key form, the price list and any open circuits fold out
 * under the row. A key saved here is checked with the provider, sealed
 * (KEY_VAULT_KEY) and used at once, ahead of any Worker secret of the same
 * provider; removing it hands the provider back to the secret. A key is never
 * shown again, only its last four characters (ai/keys.ts).
 */
import { keysView } from '../../ai/keys';
import { baseEnv } from '../../ai/settings';
import { catalogView, type ProviderState, type ProviderStatusView, providersView, summary } from '../../ai/views';
import { nowSeconds } from '../../env';
import { type App, page } from '../../http';
import { Action, Badge, DefinitionList, Icon, IconButton, JsonForm, Note, Table } from '../../ui/components';
import { ago } from '../../ui/format';
import { aiShell, API, BASE } from './shared';

const PROVIDERS_PAGE = `${BASE}/providers`;

const PLACEHOLDERS: Record<string, string> = {
  anthropic: 'sk-ant-…', openai: 'sk-…', openrouter: 'sk-or-v1-…', orcarouter: 'orca-…', saygm: 'sg-…',
};

const STATE: Record<ProviderState, { tone: 'ok' | 'warn' | 'bad' | 'plain'; label: string }> = {
  connected: { tone: 'ok', label: 'Connected' },
  key_refused: { tone: 'bad', label: 'Key refused' },
  unreachable: { tone: 'warn', label: 'Unreachable' },
  unchecked: { tone: 'plain', label: 'Unchecked' },
  not_connected: { tone: 'plain', label: 'Not connected' },
  off: { tone: 'bad', label: 'Off' },
};

/** "$123.40 left · 20 req / 10s", from whatever the provider exposes. */
function limits(b: Record<string, unknown> | null, r: Record<string, unknown> | null): string {
  const parts: string[] = [];
  if (b?.remaining !== null && b?.remaining !== undefined) parts.push(`$${Number(b.remaining).toFixed(2)} left`);
  else if (b?.usage !== null && b?.usage !== undefined) parts.push(`$${Number(b.usage).toFixed(2)} used`);
  if (b?.free_tier === true) parts.push('free tier');
  // OpenRouter reports -1 for "no request limit".
  if (typeof r?.requests === 'number' && r.requests > 0) parts.push(`${r.requests} req / ${r.interval ?? '?'}`);
  else if (typeof r?.requests === 'number' && r.requests < 0) parts.push('no request limit');
  return parts.join(' · ');
}

function StateCell(props: { p: ProviderStatusView }) {
  const { p } = props;
  const s = STATE[p.state];
  return (
    <>
      <Badge tone={s.tone}>{p.state === 'connected' ? <><Icon name="check" /> {s.label}</> : s.label}</Badge>
      {(p.state === 'key_refused' || p.state === 'unreachable') && p.check?.error
        ? <div class="sub">{p.check.error}</div> : null}
      {p.state === 'off' && p.forced ? <div class="sub">{ago(p.forced.at, nowSeconds())}{p.forced.reason
        ? `: ${p.forced.reason}` : ''}</div> : null}
    </>
  );
}

function KeyCell(props: { p: ProviderStatusView }) {
  const { key } = props.p;
  if (key.source === 'page') {
    return <><span class="keyhint">••••{key.hint}</span>{key.secret ? <div class="sub">over the Worker secret</div> : null}</>;
  }
  if (key.source === 'secret') return <span class="small">Worker secret</span>;
  return <span class="dim">—</span>;
}

export async function providersPage(c: App) {
  const now = nowSeconds();
  const catalog = await catalogView(c.env, now);
  const [providers, keys] = await Promise.all([providersView(c.env, now, catalog), keysView(c.env, baseEnv(c.env))]);
  const s = await summary(c.env, now, { catalog, providers });
  const keyed = providers.filter((p) => p.key.source !== 'none');
  const cols = 7;

  return page(c, await aiShell(c, PROVIDERS_PAGE, (
    <>
      {keys.vault ? null : <Note warn>This Worker has no <span class="mono">KEY_VAULT_KEY</span>, so keys cannot be
        stored here, only as Worker secrets (<span class="mono">wrangler secret put KEY_VAULT_KEY</span>, base64 of 32
        random bytes).</Note>}
      {keyed.length === 0 ? (
        <div class="next-step"><Icon name="info" />Connect a provider: paste its API key with the <Icon name="key" />
          button. Then add models.</div>
      ) : s.models.total === 0 ? (
        <div class="next-step"><Icon name="info" />Next: <a href={`${BASE}/models`}>add models</a> from a connected
          provider.</div>
      ) : null}
      <Table class="dense tight" head={['Provider', 'State', 'Key', 'Checked', 'Models', 'Balance · limits', '']}>
        {providers.map((p) => {
          const keyId = `key-${p.provider}`;
          const detailId = `pd-${p.provider}`;
          const open = p.listing && !p.listing.ok;
          return (
            <>
              <tr class={p.forced ? 'off' : undefined}>
                <td><b>{p.label}</b><div class="sub">{p.direct ? 'direct' : 'aggregator'}{p.provider === 'saygm'
                  ? ' · TEE models only' : ''}</div></td>
                <td><StateCell p={p} /></td>
                <td><KeyCell p={p} /></td>
                <td class="small nowrap">{p.check ? ago(p.check.at, now) : <span class="dim">never</span>}</td>
                <td class="small">{p.models ? <a href={`${BASE}/models?provider=${p.provider}`}>{p.models} model
                  {p.models === 1 ? '' : 's'}</a> : <span class="dim">none</span>}</td>
                <td class="small">{limits(p.balance, p.rate_limit) || <span class="dim">—</span>}</td>
                <td class="icons">
                  {keys.vault ? <IconButton icon="key" toggle={`#${keyId}`} expanded={false}
                    label={p.key.source === 'page' ? `Replace ${p.provider} key` : `Connect ${p.provider}: paste its API key`} />
                    : null}
                  {p.key.source !== 'none' ? <Action action={`${API}/keys/${p.provider}/test`} icon="refresh" iconOnly
                    label={`Test ${p.provider} key`} reload done={`${p.label}: checked.`} /> : null}
                  {p.forced
                    ? <Action action={`${API}/providers/${p.provider}/enable`} icon="power" iconOnly tone="accent"
                      label={`Switch ${p.provider} on`} reload done={`${p.label} is on.`} />
                    : <Action action={`${API}/providers/${p.provider}/disable`} body={{ reason: 'admin page' }} icon="power"
                      iconOnly label={`Switch ${p.provider} off`} reload
                      confirm={`Stop every call to ${p.provider}? Its routes are skipped until it is switched on.`} />}
                  {p.key.source === 'page' ? <Action action={`${API}/keys/${p.provider}`} method="DELETE" icon="x"
                    iconOnly tone="danger" label={`Remove ${p.provider} key`} reload done={`${p.label} key removed.`}
                    confirm={p.key.secret ? `Remove the ${p.provider} key set here? The Worker secret serves again.`
                      : `Remove the ${p.provider} key? Its routes are skipped until a key is set.`} /> : null}
                  <IconButton icon="chevron-down" toggle={`#${detailId}`} expanded={!!open} label={`Details of ${p.provider}`} />
                </td>
              </tr>
              {keys.vault ? (
                <tr class="sub" id={keyId} hidden>
                  <td colspan={cols}>
                    <JsonForm inline action={`${API}/keys/${p.provider}`} method="PUT"
                      submit={p.key.source === 'page' ? 'Replace key' : 'Connect'} reload
                      done={`${p.label} key saved and accepted.`}
                      confirm={p.key.source === 'page' ? `Replace the ${p.provider} key set here?` : undefined}>
                      <input name="key" type="password" autocomplete="new-password" required
                        aria-label={`${p.provider} API key`} placeholder={PLACEHOLDERS[p.provider]} />
                    </JsonForm>
                    <div class="hint">Checked with {p.label} first (nothing is generated or billed), sealed, then used by
                      the next call.</div>
                  </td>
                </tr>
              ) : null}
              <tr class="sub" id={detailId} hidden={open ? undefined : true}>
                <td colspan={cols}>
                  <DefinitionList items={[
                    ['Wire', p.wire],
                    ['Key variable', <span class="mono">{p.key.key_var}</span>],
                    ['Model list', !p.can_list ? <span class="dim">needs its key</span> : p.listing ? <>
                      {p.listing.ok ? `${p.listing.models_seen} models` : <Badge tone="bad">failed</Badge>}
                      {' '}{ago(p.listing.at, now)}{p.listing.error ? <div class="sub">{p.listing.error}</div> : null}
                    </> : <span class="dim">not read yet</span>],
                    ['Prices', p.lists_prices ? 'from its list, nightly' : p.provider === 'anthropic'
                      ? 'list prices, else OpenRouter\'s listing' : 'OpenRouter\'s listing, else set by hand'],
                    ['Routes', String(p.routes)],
                  ]} />
                  {p.can_list ? <Action action={`${API}/pricing/refresh`} body={{ provider: p.provider }} icon="refresh"
                    label="Refresh prices" tone="ghost" small reload /> : null}
                  {catalog.circuits.filter((x) => x.route_key.startsWith(`${p.provider}:`)).length ? (
                    <Table class="dense tight" head={['Open circuit', 'State', '']}>
                      {catalog.circuits.filter((x) => x.route_key.startsWith(`${p.provider}:`)).map((x) => (
                        <tr>
                          <td class="mono">{x.route_key}</td>
                          <td>{x.forced ? <Badge tone="bad">switched off</Badge> : <Badge tone="warn">circuit open</Badge>}
                            <div class="sub">{x.reason ?? `${x.fail} failures`} · {ago(x.updated_at, now)}</div></td>
                          <td class="icons">{x.forced ? <Action icon="power" iconOnly tone="accent" reload
                            action={`${API}/providers/${encodeURIComponent(x.route_key)}/enable`}
                            label={`Switch ${x.route_key} on`} /> : null}</td>
                        </tr>
                      ))}
                    </Table>
                  ) : null}
                </td>
              </tr>
            </>
          );
        })}
      </Table>
      <p class="hint">A provider without a key is skipped, never called. Price lists are read nightly at 03:30 UTC;
        Worker secrets (<span class="mono">wrangler secret put ORCAROUTER_API_KEY</span>) work too.</p>
    </>
  ), { summary: s }));
}
