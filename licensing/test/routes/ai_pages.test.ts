import { SELF } from 'cloudflare:test';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { clearListingCache } from '../../src/ai/pricing';
import { setUpstreamFetch } from '../../src/ai/providers';
import openrouter from './fixtures/openrouter_models.json';
import orcarouter from './fixtures/orcarouter_models.json';
import { admin, BASE } from './helpers';

/**
 * The six Plexora AI admin pages: each renders, keeps the CSP's rules (no
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
    const body = url.includes('openrouter.ai') ? openrouter : url.includes('orcarouter') ? orcarouter : null;
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

describe('Plexora AI admin pages', () => {
  it('every page renders soundly, empty and configured', async () => {
    const paths = ['/admin/ai', '/admin/ai/models', '/admin/ai/providers', '/admin/ai/routing', '/admin/ai/usage',
      '/admin/ai/usage?days=7', '/admin/ai/settings', '/admin/ai/models?add=openrouter&q=opus'];
    for (const path of paths) await html(path);
    await seed();
    for (const path of [...paths, '/admin/ai/models/claude-opus-5-5', '/admin/ai/routing?edit=gating.*',
      '/admin/ai/routing?model=claude-opus-5-5', '/admin/ai/models?show=used']) {
      const text = await html(path);
      for (const [label] of [['Overview'], ['Models'], ['Providers'], ['Task routing'], ['Usage &amp; cost'], ['Settings']]) {
        expect(text, path).toContain(`>${label}</a>`);
      }
    }
  });

  it("a model's page shows its provider chain in order, with the switches to reorder it", async () => {
    await seed();
    const text = await html('/admin/ai/models/claude-opus-5-5');
    const order = ['Primary', 'Fallback 1', 'Fallback 2'].map((label) => text.indexOf(`>${label}</td>`));
    expect(order.every((i) => i > 0)).toBe(true);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    // OrcaRouter first, as reordered; Anthropic's list price; OpenRouter's fee.
    expect(text.indexOf('orcarouter</span>')).toBeLessThan(text.indexOf('anthropic</span>'));
    expect(text).toContain('Make primary');
    expect(text).toContain('list price');
    expect(text).toContain('+5.5%');
    expect(text).toContain('$4.00 / $20.00');
    expect((await SELF.fetch(`${BASE}/admin/ai/models/nope`, { headers: HEADERS })).status).toBe(404);
  });

  it('task routing shows what each task inherits, what is set on it, and its editor', async () => {
    await seed();
    const text = await html('/admin/ai/routing');
    for (const label of ['Threshold evaluation', 'Image inspection', 'Final QC review', 'Conversation turn']) {
      expect(text).toContain(label);
    }
    expect(text).toContain('set here');
    expect(text).toContain('from All tasks');
    expect(text).toContain('effort high');
    expect(text).toContain('data-toggle="#ed-gating-threshold_evaluation"');
    // Editors are hidden until opened, except the one the URL names.
    expect(text).toMatch(/id="ed-gating-threshold_evaluation" hidden/);
    const opened = await html('/admin/ai/routing?edit=gating.threshold_evaluation');
    expect(opened).not.toMatch(/id="ed-gating-threshold_evaluation" hidden/);
    // A model that cannot do a task is offered, but not selectable, with the reason.
    await admin('PUT', '/ai/catalog/text-only', { name: 'Text Only', supports_vision: false });
    expect(await html('/admin/ai/routing')).toMatch(/<option value="text-only" disabled="">Text Only \(no vision\)<\/option>/);
  });

  it('the overview lists problems and what serves; the switch is on every page', async () => {
    await seed();
    await admin('POST', '/ai/providers/orcarouter/disable', { reason: 'incident' });
    const text = await html('/admin/ai');
    expect(text).toContain('orcarouter is switched off (incident)');
    expect(text).toContain('Serving now');
    expect(text).toContain('Claude Sonnet 5');
    expect(text).toContain('Switch off');
    const providers = await html('/admin/ai/providers');
    expect(providers).toContain('Switch on');
    expect((providers.match(/Refresh prices/g) ?? []).length).toBe(3);   // the three aggregators
  });

  it('settings show every group but the switch, and the migration only while the old table serves', async () => {
    const text = await html('/admin/ai/settings');
    for (const group of ['Routing', 'Limits', 'Credit', 'Reliability', 'Retention']) expect(text).toContain(`>${group}</h3>`);
    expect(text).not.toContain('name="AI_ENABLED"');
    expect(text).not.toContain('Migrate');
  });
});
