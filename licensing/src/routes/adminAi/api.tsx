/** @jsxImportSource hono/jsx */
/**
 * /admin/ai/api: the API keys Plexora calls each provider with.
 *
 * A key saved here is checked with the provider, sealed (KEY_VAULT_KEY) and
 * used at once, ahead of any Worker secret of the same provider; removing it
 * hands the provider back to the secret. A key is never shown again, only its
 * last four characters (ai/keys.ts).
 */
import { keysView, type KeyView } from '../../ai/keys';
import { SPECS } from '../../ai/providers';
import { baseEnv } from '../../ai/settings';
import { nowSeconds } from '../../env';
import { type App, page } from '../../http';
import { Action, Badge, JsonForm, Note, RowMenu, Table } from '../../ui/components';
import { ago } from '../../ui/format';
import { aiShell, API, BASE } from './shared';

const PLACEHOLDERS: Record<string, string> = {
  anthropic: 'sk-ant-…', openai: 'sk-…', openrouter: 'sk-or-v1-…', orcarouter: 'orca-…', saygm: 'sg-…',
};

function KeyBadge(props: { k: KeyView }) {
  const { k } = props;
  if (k.source === 'page') return <><Badge tone="ok">set here</Badge> <span class="mono">••••{k.hint}</span></>;
  if (k.source === 'secret') return <Badge tone="accent">Worker secret</Badge>;
  return <Badge>none</Badge>;
}

function CheckCell(props: { k: KeyView; now: number }) {
  const { check } = props.k;
  if (!check) return <span class="dim">not checked</span>;
  return (
    <>
      {check.ok ? <Badge tone="ok">accepted</Badge> : <Badge tone="bad">failed</Badge>} {ago(check.at, props.now)}
      {check.error ? <div class="sub">{check.error}</div> : null}
    </>
  );
}

export async function apiPage(c: App) {
  const now = nowSeconds();
  const view = await keysView(c.env, baseEnv(c.env));

  return page(c, aiShell(c, `${BASE}/api`, (
    <>
      {view.vault ? null : <Note warn>This Worker has no <span class="mono">KEY_VAULT_KEY</span>, so keys cannot be
        stored here; the table shows the Worker secrets only. Set one with <span class="mono">wrangler secret put
        KEY_VAULT_KEY</span> (base64 of 32 random bytes) to save keys on this page.</Note>}
      <Table class="dense" head={['Provider', 'Key', 'Last check', 'Set', 'New key', '']}>
        {view.keys.map((k) => (
          <tr>
            <td><b class="mono">{k.provider}</b>
              <div class="sub">{SPECS[k.provider].direct ? 'direct' : 'aggregator'} · <span class="mono">{k.key_var}</span></div></td>
            <td><KeyBadge k={k} />{k.source === 'page' && k.secret
              ? <div class="sub">overrides the Worker secret</div> : null}</td>
            <td class="small"><CheckCell k={k} now={now} /></td>
            <td class="small">{k.updated_at ? <>{ago(k.updated_at, now)}<div class="sub">{k.updated_by}</div></>
              : <span class="dim">—</span>}</td>
            <td>{view.vault ? (
              <JsonForm inline action={`${API}/keys/${k.provider}`} method="PUT" submit={k.source === 'page' ? 'Replace' : 'Save'}
                reload done={`${k.provider} key saved and accepted.`}
                confirm={k.source === 'page' ? `Replace the ${k.provider} key set here?` : undefined}>
                <input name="key" type="password" autocomplete="new-password" required aria-label={`${k.provider} API key`}
                  placeholder={PLACEHOLDERS[k.provider]} />
              </JsonForm>
            ) : <span class="dim">—</span>}</td>
            <td class="right">{k.source === 'none' ? null : (
              <RowMenu>
                <Action action={`${API}/keys/${k.provider}/test`} label="Test" tone="ghost" small reload
                  done={`${k.provider}: checked.`} />
                {k.source === 'page' ? <Action action={`${API}/keys/${k.provider}`} method="DELETE" label="Remove"
                  tone="danger" small reload done={`${k.provider} key removed.`}
                  confirm={k.secret ? `Remove the ${k.provider} key set here? The Worker secret serves again.`
                    : `Remove the ${k.provider} key? Its routes are skipped until a key is set.`} /> : null}
              </RowMenu>
            )}</td>
          </tr>
        ))}
      </Table>

      <p class="hint">A key is checked with its provider before it is saved (an empty request: nothing is generated
        or billed), sealed, and used by the next call. It is never shown again; only its last four characters are.
        A provider without any key is skipped, never called. Worker secrets
        (<span class="mono">wrangler secret put ORCAROUTER_API_KEY</span>) still work, and serve whenever no key is
        set here. Set a spend limit with the provider as well.</p>
    </>
  )));
}
