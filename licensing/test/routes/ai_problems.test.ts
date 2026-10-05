import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { clearListingCache } from '../../src/ai/pricing';
import { setUpstreamFetch } from '../../src/ai/providers';
import openrouter from './fixtures/openrouter_models.json';
import orcarouter from './fixtures/orcarouter_models.json';
import { admin, call, count } from './helpers';

/**
 * What needs attention on /admin/ai: every problem has a stable key, can be
 * dismissed until it changes, and comes back once it goes away and returns.
 * And the summary the step strip and the Overview read.
 */

beforeEach(() => {
  clearListingCache();
  setUpstreamFetch(async (url) => {
    const body = url.includes('openrouter.ai') ? openrouter : url.includes('orcarouter') ? orcarouter : null;
    return body ? new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
      : new Response('no', { status: 503 });
  });
});
afterEach(() => setUpstreamFetch(null));

/** Claude Opus 5.5 on OrcaRouter, then Anthropic, then OpenRouter; Sonnet for every task, one task on Opus. */
async function seed() {
  await admin('POST', '/ai/catalog/seed-builtin');
  await admin('POST', '/ai/catalog/import', { provider: 'orcarouter', provider_model: 'anthropic/claude-opus-5.5' });
  await admin('POST', '/ai/catalog/import', { provider: 'openrouter', provider_model: 'anthropic/claude-opus-5.5' });
  await admin('POST', '/ai/catalog/claude-opus-5-5/routes/orcarouter/primary');
  await admin('PUT', '/ai/settings', { AI_ALLOW_UNBENCHED_ROUTES: 1 });
  await admin('PUT', '/ai/tasks/*', { primary: 'claude-sonnet-5', fallback_1: 'claude-opus-5-5' });
  await admin('PUT', '/ai/tasks/gating.threshold_evaluation', { primary: 'claude-opus-5-5', effort: 'high' });
}

const problems = async () => (await admin('GET', '/ai/problems')).json;
const dismiss = (key: string) => admin('POST', `/ai/problems/${encodeURIComponent(key)}/dismiss`);

describe('dismissing problems', () => {
  it('hides a problem until it changes, and shows it again on undo', async () => {
    await seed();
    await admin('POST', '/ai/providers/orcarouter/disable', { reason: 'incident' });
    const before = await problems();
    const off = before.problems.find((p: any) => p.key === 'provider:orcarouter:off');
    expect(off).toMatchObject({ tone: 'bad', dismissed: false });
    expect(off.text).toContain('orcarouter is switched off (incident)');
    const dismissed = await dismiss('provider:orcarouter:off');
    expect(dismissed.status, JSON.stringify(dismissed.json)).toBe(200);
    const after = await problems();
    expect(after.problems.find((p: any) => p.key === 'provider:orcarouter:off')).toMatchObject({ dismissed: true,
      dismissed_by: 'admin:token' });
    expect(after.open).toBe(before.open - 1);
    expect(after.dismissed).toBe(1);
    expect(await count('events', "kind = 'ai.problem_dismissed'")).toBe(1);
    expect((await admin('DELETE', '/ai/problems/provider%3Aorcarouter%3Aoff/dismiss')).status).toBe(200);
    expect((await problems()).problems.find((p: any) => p.key === 'provider:orcarouter:off').dismissed).toBe(false);
    expect((await admin('DELETE', '/ai/problems/provider%3Aorcarouter%3Aoff/dismiss')).status).toBe(404);
  });

  it('forgets a dismissal once its problem is gone, so it shows again if it comes back', async () => {
    await seed();
    await admin('POST', '/ai/providers/orcarouter/disable', { reason: 'incident' });
    await dismiss('provider:orcarouter:off');
    await admin('POST', '/ai/providers/orcarouter/enable');
    expect((await problems()).problems.map((p: any) => p.key)).not.toContain('provider:orcarouter:off');
    expect(await count('ai_dismissals')).toBe(0);
    await admin('POST', '/ai/providers/orcarouter/disable', { reason: 'again' });
    expect((await problems()).problems.find((p: any) => p.key === 'provider:orcarouter:off').dismissed).toBe(false);
  });

  it('takes keys with * and dots, refuses unknown and malformed ones, and is for admins only', async () => {
    await seed();
    await admin('PUT', '/ai/catalog/claude-opus-5-5', { enabled: false });
    const key = 'task:gating.threshold_evaluation:skipped:claude-opus-5-5';
    expect((await problems()).problems.map((p: any) => p.key)).toContain(key);
    expect((await dismiss(key)).status).toBe(200);
    expect((await dismiss('provider:saygm:off')).status).toBe(404);
    expect((await dismiss('Not A Key')).status).toBe(400);
    expect((await call('GET', '/admin/api/ai/problems')).status).toBe(401);
    expect((await call('POST', `/admin/api/ai/problems/${encodeURIComponent(key)}/dismiss`)).status).toBe(401);
    expect((await admin('GET', '/ai/schema')).json).toMatchObject({ schema_version: 6, dismissals_table: true });
  });
});

describe('the summary', () => {
  it('counts providers, models, the general model and its fallback, tasks and problems', async () => {
    await seed();
    const s = (await admin('GET', '/ai/summary')).json;
    expect(s.providers).toMatchObject({ total: 5, connected: 0 });
    expect(s.models).toMatchObject({ total: 3, unpriced: 0 });
    expect(s.general).toMatchObject({ model: 'Claude Sonnet 5', chain: ['Claude Sonnet 5', 'Claude Opus 5.5'],
      level: 'global' });
    expect(s.tasks).toMatchObject({ total: 18, own: 2 });
    await admin('POST', '/ai/providers/orcarouter/disable', { reason: 'incident' });
    expect((await admin('GET', '/ai/summary')).json.fallback.forced).toEqual(['orcarouter']);
  });
});
