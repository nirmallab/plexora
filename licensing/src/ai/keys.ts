/**
 * Provider API keys entered on /admin/ai/providers, beside (and over) the Worker's
 * secrets.
 *
 * Workers Free allows 64 variables and secrets per Worker, and a secret takes
 * a deploy to change; a key here takes neither. Each is sealed with the
 * seat-key vault (AES-GCM under KEY_VAULT_KEY, crypto.ts), so D1 never holds
 * one in clear, and the nightly backup leaves the table out (backup.ts).
 * Without KEY_VAULT_KEY nothing can be stored, and keys stay Worker secrets.
 *
 * They reach the gateway through `withSettings` (settings.ts), which lays
 * them over the env under each provider's key variable: every
 * `env[SPECS[p].keyVar]` read -- calls, `configured()`, balances -- sees a key
 * set here before the Worker secret, and the secret again once it is removed.
 * A key is never sent back to the browser; the page shows its last four
 * characters and whether the provider accepted it.
 */
import { vaultOpen, vaultSeal } from '../crypto';
import { all, one } from '../db';
import { type Env, knob } from '../env';
import { type Provider, providerFetch, PROVIDERS, SPECS } from './providers';

export interface KeyRow {
  provider: Provider;
  /** The sealed key; null for a row that only records a check of the Worker secret. */
  vault: string | null;
  hint: string | null;
  updated_at: number;
  updated_by: string | null;
  checked_at: number | null;
  check_ok: number | null;
  check_error: string | null;
  /** The HTTP status the last check got (0: unreachable); a late column, so absent on old rows. */
  check_status?: number | null;
}

/** A key as a provider issues one: printable, no spaces. */
export const KEY_SHAPE = /^[\x21-\x7e]{16,512}$/;

let cache: { at: number; keys: Record<string, string> } | null = null;

export function clearKeyCache(): void {
  cache = null;
}

/** The keys set here, opened, by the env variable each provider's key is read from. */
export async function storedKeys(env: Env): Promise<Record<string, string>> {
  const ttl = knob(env, 'AI_SETTINGS_CACHE_S') * 1000;
  if (cache && ttl > 0 && Date.now() - cache.at < ttl) return cache.keys;
  const keys: Record<string, string> = {};
  if (env.KEY_VAULT_KEY) {
    let rows: KeyRow[] = [];
    try {
      rows = await all<KeyRow>(env, 'SELECT * FROM ai_provider_keys WHERE vault IS NOT NULL');
    } catch {
      rows = [];   // before the schema has the table
    }
    for (const row of rows) {
      if (!(PROVIDERS as readonly string[]).includes(row.provider)) continue;
      const key = await vaultOpen(env.KEY_VAULT_KEY, row.vault);
      if (key) keys[SPECS[row.provider].keyVar] = key;
      else console.error(`ai keys: the stored ${row.provider} key does not open with this KEY_VAULT_KEY`);
    }
  }
  cache = { at: Date.now(), keys };
  return keys;
}

export async function keyRow(env: Env, provider: Provider): Promise<KeyRow | null> {
  return one<KeyRow>(env, 'SELECT * FROM ai_provider_keys WHERE provider = ?1', provider).catch(() => null);
}

export function sealKey(env: Env, key: string): Promise<string | null> {
  return vaultSeal(env.KEY_VAULT_KEY, key);
}

export const hintOf = (key: string) => key.slice(-4);

export interface CheckResult {
  ok: boolean;
  status: number;
  error: string | null;
}

/**
 * Whether a provider accepts a key, for free: an empty request to the endpoint
 * the gateway calls. A provider checks the key before the request, so a
 * refused key answers 401 or 403 and an accepted one a complaint about the
 * empty request (400, 422) -- nothing is generated and nothing is billed.
 */
export async function checkKey(env: Env, provider: Provider, key: string): Promise<CheckResult> {
  const spec = SPECS[provider];
  const base = String(env[spec.baseVar] || spec.base).replace(/\/$/, '');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await providerFetch(`${base}${spec.path}`, { method: 'POST', signal: controller.signal,
      headers: { 'content-type': 'application/json', accept: 'application/json', ...spec.headers(key) }, body: '{}' });
    const text = (await response.text().catch(() => '')).slice(0, 300);
    if (response.status === 401 || response.status === 403) {
      return { ok: false, status: response.status, error: `${provider} refused the key (${response.status})${
        messageOf(text) ? `: ${messageOf(text)}` : ''}` };
    }
    if (response.status >= 500) {
      return { ok: false, status: response.status, error: `${provider} answered ${response.status}; try again later.` };
    }
    return { ok: true, status: response.status, error: null };
  } catch (error) {
    return { ok: false, status: 0, error: `${provider} could not be reached: ${String((error as Error)?.message ?? error)}` };
  } finally {
    clearTimeout(timer);
  }
}

/** A provider's error message, without echoing anything key-shaped back. */
function messageOf(text: string): string {
  let message = text;
  try {
    const parsed = JSON.parse(text);
    message = String(parsed?.error?.message ?? parsed?.message ?? parsed?.error ?? '');
  } catch {
    // plain text
  }
  return message.replace(/[A-Za-z0-9_\-]{24,}/g, '…').slice(0, 160);
}

export function recordCheck(env: Env, provider: Provider, result: CheckResult, now: number,
  who: string): D1PreparedStatement {
  return env.LICENSE_DB.prepare(
    `INSERT INTO ai_provider_keys (provider, vault, hint, updated_at, updated_by, checked_at, check_ok, check_error,
       check_status)
       VALUES (?1, NULL, NULL, ?2, ?3, ?2, ?4, ?5, ?6)
     ON CONFLICT(provider) DO UPDATE SET checked_at = ?2, check_ok = ?4, check_error = ?5, check_status = ?6`,
  ).bind(provider, now, who, result.ok ? 1 : 0, result.error, result.status);
}

export interface KeyView {
  provider: Provider;
  key_var: string;
  /** Where the key the gateway uses comes from: this page, a Worker secret, or nowhere. */
  source: 'page' | 'secret' | 'none';
  hint: string | null;
  /** A Worker secret is set too (it serves again once the page's key is removed). */
  secret: boolean;
  updated_at: number | null;
  updated_by: string | null;
  check: { at: number; ok: boolean; error: string | null; status: number | null } | null;
}

/** Every provider's key, as the Providers page shows it. `base` is the env without the page's keys laid over it. */
export async function keysView(env: Env, base: Env): Promise<{ vault: boolean; keys: KeyView[] }> {
  const rows = await all<KeyRow>(env, 'SELECT * FROM ai_provider_keys').catch(() => [] as KeyRow[]);
  const keys = PROVIDERS.map((provider): KeyView => {
    const row = rows.find((r) => r.provider === provider);
    const keyVar = SPECS[provider].keyVar;
    const secret = typeof base[keyVar] === 'string' && String(base[keyVar]).length > 0;
    const page = !!row?.vault;
    return { provider, key_var: keyVar, source: page ? 'page' : secret ? 'secret' : 'none',
      hint: page ? row!.hint : null, secret, updated_at: page ? row!.updated_at : null,
      updated_by: page ? row!.updated_by : null,
      check: row?.checked_at ? { at: row.checked_at, ok: !!row.check_ok, error: row.check_error,
        status: row.check_status ?? null } : null };
  });
  return { vault: !!env.KEY_VAULT_KEY, keys };
}
