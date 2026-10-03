import { SELF } from 'cloudflare:test';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { clearListingCache } from '../../src/ai/pricing';
import { setUpstreamFetch } from '../../src/ai/providers';
import anthropic from './fixtures/anthropic_models.json';
import openai from './fixtures/openai_models.json';
import openrouter from './fixtures/openrouter_models.json';
import orcarouter from './fixtures/orcarouter_models.json';
import { admin, BASE } from './helpers';

/**
 * The Plexora AI admin: four steps (Providers, Models, Tasks, Overview) and
 * two quiet pages (Usage, Settings). Each renders, keeps the CSP's rules (no
 * inline style, one script), and shows what it is for.
 */

const HEADERS = { Authorization: 'Bearer test-admin', Accept: 'text/html' };

async function html(path: string) {
  const response = await SELF.fetch(`${BASE}${path}`, { headers: HEADERS, redirect: 'manual' });
  const text = await response.text();
  expect(response.status, `${path}: ${text.slice(0, 300)}`).toBe(200);
  expect(response.headers.get('Content-Security-Policy'), path).toContain("style-src 'sha256-");
  expect(text, path).not.toMatch(/\sstyle="/);
  expect(text.match(/<script/g), path).toHaveLength(1);
  return text;
}

beforeEach(() => {
  clearListingCache();
  setUpstreamFetch(async (url) => {
    const body = url.includes('openrouter.ai') ? openrouter : url.includes('orcarouter') ? orcarouter
      : url.includes('api.anthropic.com') ? anthropic : url.includes('api.openai.com') ? openai : null;
    return body ? new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
      : new Response('no', { status: 503 });
  });
});
afterEach(() => setUpstreamFetch(null));

/** A catalogue like the owner's example: Claude Opus 5.5 on OrcaRouter, then Anthropic, then OpenRouter. */
async function seed() {
  await admin('POST', '/ai/catalog/seed-builtin');
  await admin('POST', '/ai/catalog/import', { provider: 'orcarouter', provider_model: 'anthropic/claude-opus-5.5' });
  await admin('POST', '/ai/catalog/import', { provider: 'openrouter', provider_model: 'anthropic/claude-opus-5.5' });
  await admin('POST', '/ai/catalog/claude-opus-5-5/routes/orcarouter/primary');
  await admin('PUT', '/ai/settings', { AI_ALLOW_UNBENCHED_ROUTES: 1 });
  await admin('PUT', '/ai/tasks/*', { primary: 'claude-sonnet-5', fallback_1: 'claude-opus-5-5' });
  await admin('PUT', '/ai/tasks/gating.threshold_evaluation', { primary: 'claude-opus-5-5', effort: 'high',
    max_cost_usd: 0.05 });
}

const PAGES = ['/admin/ai', '/admin/ai?dismissed=1', '/admin/ai/providers', '/admin/ai/models', '/admin/ai/tasks',
  '/admin/ai/usage', '/admin/ai/usage?days=7', '/admin/ai/settings', '/admin/ai/models?add=openrouter&q=opus',
  '/admin/ai/models?add=anthropic'];

describe('Plexora AI admin pages', () => {
  it('every page renders soundly, empty and configured, under the four steps', async () => {
    for (const path of PAGES) await html(path);
    await seed();
    for (const path of [...PAGES, '/admin/ai/models/claude-opus-5-5', '/admin/ai/models?model=claude-opus-5-5',
      '/admin/ai/models?show=used', '/admin/ai/tasks?edit=gating.*', '/admin/ai/tasks?model=claude-opus-5-5']) {
      const text = await html(path);
      expect(text, path).toContain('class="steps"');
      const steps = ['Providers', 'Models', 'Tasks', 'Overview'].map((label) => text.indexOf(`>${label}</span>`));
      expect(steps.every((i) => i > 0), path).toBe(true);
      expect(steps, path).toEqual([...steps].sort((a, b) => a - b));
      for (const n of [1, 2, 3, 4]) expect(text, path).toContain(`<span class="n">${n}</span>`);
      expect(text, path).toContain('>Usage</a>');
      expect(text, path).toContain('>Settings</a>');
    }
    expect(await html('/admin/ai/tasks')).toMatch(/href="\/admin\/ai\/tasks" aria-current="page"/);
  });

  it('sends the old addresses to their new pages', async () => {
    const routing = await SELF.fetch(`${BASE}/admin/ai/routing?edit=gating.*`, { headers: HEADERS, redirect: 'manual' });
    expect(routing.status).toBe(301);
    expect(routing.headers.get('Location')).toBe('/admin/ai/tasks?edit=gating.*');
    const api = await SELF.fetch(`${BASE}/admin/ai/api`, { headers: HEADERS, redirect: 'manual' });
    expect(api.status).toBe(301);
    expect(api.headers.get('Location')).toBe('/admin/ai/providers');
  });

  it('providers: state, key form folded under the row, and icon actions', async () => {
    await seed();
    await admin('POST', '/ai/providers/orcarouter/disable', { reason: 'incident' });
    const text = await html('/admin/ai/providers');
    expect(text).toContain('Unchecked');
    expect(text).toContain('>Off</span>');
    expect(text).toMatch(/id="key-anthropic" hidden/);
    expect(text).toContain('name="key" type="password"');
    expect(text).toContain('aria-label="Switch orcarouter on"');
    expect(text).toContain('aria-label="Test anthropic key"');
    expect(text).toContain('aria-label="Details of saygm"');
    // Every provider's list can be read in tests (every key is set): one Refresh each.
    expect((text.match(/Refresh prices/g) ?? []).length).toBe(5);
  });

  it("models: each row's provider chain in order, folded out to reorder; remove is always offered", async () => {
    await seed();
    const text = await html('/admin/ai/models');
    const row = text.slice(text.indexOf('<b>Claude Opus 5.5</b>'));
    expect(row.indexOf('>orcarouter</span>')).toBeLessThan(row.indexOf('>anthropic</span>'));
    expect(row.indexOf('>anthropic</span>')).toBeLessThan(row.indexOf('>openrouter</span>'));
    expect(text).toMatch(/id="rt-claude-opus-5-5" hidden/);
    expect(text).toContain('aria-label="Make anthropic primary"');
    expect(text).toContain('aria-label="Remove Claude Opus 5.5"');
    expect(await html('/admin/ai/models?model=claude-opus-5-5')).not.toMatch(/id="rt-claude-opus-5-5" hidden/);
    const search = await html('/admin/ai/models?add=anthropic&q=opus');
    expect(search).toContain('data-next="/admin/ai/models?model={model.id}"');
    expect(search).toContain('claude-opus-4-1-20250805');
    expect(search).toContain('OpenRouter reference');
    expect(search).toContain('list price');
  });

  it("a model's page shows its provider chain in order, with the switches to reorder it", async () => {
    await seed();
    const text = await html('/admin/ai/models/claude-opus-5-5');
    const order = ['Primary', 'Fallback 1', 'Fallback 2'].map((label) => text.indexOf(`>${label}</td>`));
    expect(order.every((i) => i > 0)).toBe(true);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    // OrcaRouter first, as reordered; Anthropic's list price; OpenRouter's fee.
    expect(text.indexOf('orcarouter</span>')).toBeLessThan(text.indexOf('anthropic</span>'));
    expect(text).toContain('aria-label="Make anthropic primary"');
    expect(text).toContain('list price');
    expect(text).toContain('+5.5%');
    expect(text).toContain('$4.00 / $20.00');
    expect(text).toContain('id="prices"');
    expect((await SELF.fetch(`${BASE}/admin/ai/models/nope`, { headers: HEADERS })).status).toBe(404);
  });

  it('tasks: three selects per row that save on change, what each inherits, and the advanced editor', async () => {
    await seed();
    const text = await html('/admin/ai/tasks');
    for (const label of ['Threshold evaluation', 'Image inspection', 'Final QC review', 'Conversation turn']) {
      expect(text).toContain(label);
    }
    expect(text).toContain('class="chain-form" data-json="" data-autosave=""');
    expect(text).toContain('data-method="PUT"');
    expect(text).toContain('aria-label="Primary model"');
    expect(text).toContain('class="inherit">');
    expect(text).toContain('from All tasks');
    expect(text).toContain('effort high');
    // The row's advanced values ride along with its selects, since saving replaces the row.
    const threshold = text.slice(text.indexOf('/admin/api/ai/tasks/gating.threshold_evaluation'));
    const form = threshold.slice(0, threshold.indexOf('</form>'));
    expect(form).toContain('name="effort" value="high"');
    expect(form).toContain('name="max_cost_usd" value="0.05"');
    expect(text).toContain('data-toggle="#ed-gating-threshold_evaluation"');
    // Editors are hidden until opened, except the one the URL names.
    expect(text).toMatch(/id="ed-gating-threshold_evaluation" hidden/);
    const opened = await html('/admin/ai/tasks?edit=gating.threshold_evaluation');
    expect(opened).not.toMatch(/id="ed-gating-threshold_evaluation" hidden/);
    // A model that cannot do a task is offered, but not selectable, with the reason.
    await admin('PUT', '/ai/catalog/text-only', { name: 'Text Only', supports_vision: false });
    expect(await html('/admin/ai/tasks')).toMatch(/<option value="text-only" disabled="">Text Only \(no vision\)<\/option>/);
  });

  it('the overview: warnings that dismiss and come back, the summary, what serves; the switch on every page', async () => {
    await seed();
    await admin('POST', '/ai/providers/orcarouter/disable', { reason: 'incident' });
    const text = await html('/admin/ai');
    expect(text).toContain('orcarouter is switched off (incident)');
    expect(text).toContain('aria-label="Dismiss this warning"');
    expect(text).toContain('class="strip"');
    expect(text).toContain('Serving now');
    expect(text).toContain('Claude Sonnet 5');
    expect(text).toContain('Switch off');
    expect((await admin('POST', `/ai/problems/${encodeURIComponent('provider:orcarouter:off')}/dismiss`)).status).toBe(200);
    const after = await html('/admin/ai');
    expect(after).not.toContain('orcarouter is switched off (incident)');
    expect(after).toContain('1 dismissed');
    const shown = await html('/admin/ai?dismissed=1');
    expect(shown).toContain('orcarouter is switched off (incident)');
    expect(shown).toContain('>Undo<');
    await admin('DELETE', `/ai/problems/${encodeURIComponent('provider:orcarouter:off')}/dismiss`);
    expect(await html('/admin/ai')).toContain('orcarouter is switched off (incident)');
  });

  it('settings show every group but the switch, and the migration only while the old table serves', async () => {
    const text = await html('/admin/ai/settings');
    for (const group of ['Routing', 'Limits', 'Credit', 'Reliability', 'Retention']) expect(text).toContain(`>${group}</h3>`);
    expect(text).not.toContain('name="AI_ENABLED"');
    expect(text).not.toContain('Migrate');
  });
});
