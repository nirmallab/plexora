/**
 * The admin API for provider keys, under /admin/api/ai (admin only, same-origin, JSON):
 *
 *   GET    /keys                    every provider: where its key comes from, its last four, its last check
 *   PUT    /keys/:provider {key}    check it with the provider, then seal and store it
 *   DELETE /keys/:provider          remove the stored key (the Worker secret, if any, serves again)
 *   POST   /keys/:provider/test     check the key the gateway uses now
 *
 * A key is never returned, logged or put in an event: events carry the
 * provider and the last four characters only (ai/keys.ts).
 */
import { Hono } from 'hono';

import { nowSeconds } from '../env';
import { eventStatement } from '../events';
import { ApiError, type AppEnv, ok, readJson } from '../http';
import {
  checkKey, clearKeyCache, hintOf, KEY_SHAPE, keyRow, keysView, recordCheck, sealKey,
} from '../ai/keys';
import { clearListingCache } from '../ai/pricing';
import { isProvider, type Provider, PROVIDERS, SPECS } from '../ai/providers';
import { baseEnv } from '../ai/settings';

export const aiKeysAdmin = new Hono<AppEnv>();

const who = (c: { get: (k: 'admin') => string }) => `admin:${c.get('admin')}`;

function providerOf(value: string | undefined): Provider {
  if (!isProvider(value)) {
    throw new ApiError(400, 'invalid_request', `Unknown provider; one of ${PROVIDERS.join(', ')}.`);
  }
  return value;
}

aiKeysAdmin.get('/keys', async (c) => ok(c, await keysView(c.env, baseEnv(c.env))));

aiKeysAdmin.put('/keys/:provider', async (c) => {
  const now = nowSeconds();
  const provider = providerOf(c.req.param('provider'));
  const body = await readJson(c);
  const key = typeof body.key === 'string' ? body.key.trim() : '';
  if (!KEY_SHAPE.test(key)) {
    throw new ApiError(400, 'invalid_request', 'Paste the whole key: 16 to 512 printable characters, no spaces.');
  }
  if (!c.env.KEY_VAULT_KEY) {
    throw new ApiError(409, 'conflict', 'This Worker has no KEY_VAULT_KEY, so a key cannot be stored sealed. Set it ' +
      `(wrangler secret put KEY_VAULT_KEY), or set the key as a Worker secret (wrangler secret put ${
        SPECS[provider].keyVar}).`);
  }
  const check = await checkKey(c.env, provider, key);
  if (!check.ok && (check.status === 401 || check.status === 403)) {
    throw new ApiError(400, 'invalid_request', `${check.error}. The key was not saved.`);
  }
  const vault = await sealKey(c.env, key);
  if (!vault) throw new ApiError(500, 'internal_error', 'The key could not be sealed.');
  const hint = hintOf(key);
  await c.env.LICENSE_DB.batch([
    c.env.LICENSE_DB.prepare(
      `INSERT INTO ai_provider_keys (provider, vault, hint, updated_at, updated_by, checked_at, check_ok, check_error,
         check_status)
         VALUES (?1, ?2, ?3, ?4, ?5, ?4, ?6, ?7, ?8)
       ON CONFLICT(provider) DO UPDATE SET vault = ?2, hint = ?3, updated_at = ?4, updated_by = ?5, checked_at = ?4,
         check_ok = ?6, check_error = ?7, check_status = ?8`,
    ).bind(provider, vault, hint, now, who(c), check.ok ? 1 : 0, check.error, check.status),
    eventStatement(c.env, now, { actor: who(c), kind: 'ai.key_set', payload: { provider, hint, check_ok: check.ok } }),
  ]);
  clearKeyCache();
  clearListingCache();
  const view = await keysView(c.env, baseEnv(c.env));
  return ok(c, { key: view.keys.find((k) => k.provider === provider), check });
});

aiKeysAdmin.delete('/keys/:provider', async (c) => {
  const now = nowSeconds();
  const provider = providerOf(c.req.param('provider'));
  const row = await keyRow(c.env, provider);
  if (!row?.vault) throw new ApiError(404, 'not_found', `No ${provider} key is set on this page.`);
  await c.env.LICENSE_DB.batch([
    c.env.LICENSE_DB.prepare('DELETE FROM ai_provider_keys WHERE provider = ?1').bind(provider),
    eventStatement(c.env, now, { actor: who(c), kind: 'ai.key_removed', payload: { provider, hint: row.hint } }),
  ]);
  clearKeyCache();
  clearListingCache();
  const view = await keysView(c.env, baseEnv(c.env));
  return ok(c, { key: view.keys.find((k) => k.provider === provider) });
});

aiKeysAdmin.post('/keys/:provider/test', async (c) => {
  const now = nowSeconds();
  const provider = providerOf(c.req.param('provider'));
  const key = c.env[SPECS[provider].keyVar];
  if (typeof key !== 'string' || !key) throw new ApiError(409, 'conflict', `No ${provider} key is set.`);
  const check = await checkKey(c.env, provider, key);
  await c.env.LICENSE_DB.batch([
    recordCheck(c.env, provider, check, now, who(c)),
    eventStatement(c.env, now, { actor: who(c), kind: 'ai.key_checked', payload: { provider, ok: check.ok } }),
  ]);
  return ok(c, { check });
});
