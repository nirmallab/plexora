import { env, SELF } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import worker from '../../src/index';
import { dayOf } from '../../src/env';
import { forceStop, isQuotaError, resetBudgetCache } from '../../src/telemetry/budget';
import { BASE, budgetRow, count, hexId, sampleBatch, seconds, setBudget, tokenFor, upload } from './helpers';

const SHARE = 40000;

async function uploadAt(ratio: number) {
  const today = dayOf(seconds());
  await setBudget(today, Math.round(SHARE * ratio));
  resetBudgetCache();
  const id = hexId('q');
  const response = await upload(sampleBatch(id), await tokenFor(id));
  const body = (await response.json()) as Record<string, any>;
  const row = await env.TELEMETRY_DB.prepare('SELECT events_json FROM batches WHERE install_id = ?').bind(id).first<{ events_json: string }>();
  return { response, body, stored: row ? (JSON.parse(row.events_json) as Record<string, any>[]) : null };
}

describe('budget states', () => {
  it('ok and warn: normal ingest, base config', async () => {
    for (const ratio of [0, 0.5]) {
      const { response, body } = await uploadAt(ratio);
      expect(response.status).toBe(202);
      expect(body.dropped).toBe(0);
      expect(body.config).toEqual({ level_max: 'diagnostics', upload_interval_s: 3600, sample: 1, disabled_until: 0 });
    }
  });

  it('aggregate: normal ingest, 3x interval and sample 0.5', async () => {
    const { body } = await uploadAt(0.7);
    expect(body.dropped).toBe(0);
    expect(body.config).toEqual({ level_max: 'diagnostics', upload_interval_s: 10800, sample: 0.5, disabled_until: 0 });
  });

  it('reduce: drops priority >= 6, strips diagnostics fields, anonymous ceiling', async () => {
    const { body, stored } = await uploadAt(0.85);
    expect(body.config).toEqual({ level_max: 'anonymous', upload_interval_s: 10800, sample: 0.25, disabled_until: 0 });
    // render.summary 6, node.summary 6, capability.transition 7, telemetry.health 8.
    expect(body.dropped).toBe(4);
    expect(stored!.map((e) => e.type)).not.toContain('render.summary');
    const opened = stored!.find((e) => e.type === 'dataset.opened')!;
    expect(opened.props.channels).toBeUndefined();
  });

  it('critical: keeps priority <= 3 only, 6x interval', async () => {
    const { body, stored } = await uploadAt(0.95);
    expect(body.config).toMatchObject({ level_max: 'anonymous', upload_interval_s: 21600 });
    expect(stored!.map((e) => e.type).sort()).toEqual(['dataset.opened', 'error.fingerprint', 'project.load', 'session.summary']);
  });

  it('stop: 503 with Retry-After to midnight, body unread, nothing written', async () => {
    const today = dayOf(seconds());
    const before = await budgetRow(today);
    const { response, body, stored } = await uploadAt(1.0);
    expect(response.status).toBe(503);
    const retry = Number(response.headers.get('Retry-After'));
    expect(retry).toBeGreaterThan(0);
    expect(retry).toBeLessThanOrEqual(86400 + 900);
    expect(body.config.disabled_until).toBeGreaterThan(seconds());
    expect(Object.keys(body.config).sort()).toEqual(['disabled_until', 'level_max', 'sample', 'upload_interval_s']);
    expect(stored).toBeNull();
    const after = await budgetRow(today);
    expect(after!.d1_rows_written).toBe(SHARE);
    expect(before === null || before.requests === after!.requests).toBe(true);
  });

  it('the ratio is the max over requests, writes and reads', async () => {
    const today = dayOf(seconds());
    await setBudget(today, 0, 2_000_000 * 0.9, 0);
    resetBudgetCache();
    const id = hexId('r');
    const body = (await (await upload(sampleBatch(id), await tokenFor(id))).json()) as Record<string, any>;
    expect(body.config.level_max).toBe('anonymous');
  });

  it('a D1 quota error forces stop for the rest of the day', async () => {
    expect(isQuotaError(new Error('D1_ERROR: Exceeded maximum daily rows written limit'))).toBe(true);
    expect(isQuotaError(new Error('SQLITE_CONSTRAINT'))).toBe(false);
    forceStop(seconds());
    const id = hexId('s');
    const response = await upload(sampleBatch(id), await tokenFor(id));
    expect(response.status).toBe(503);
    const health = (await (await SELF.fetch(`${BASE}/healthz`)).json()) as Record<string, unknown>;
    expect(health.budget_state).toBe('stop');
    expect(await count('batches', 'install_id = ?', id)).toBe(0);
  });

  it('per-version client_config overrides the defaults and can disable a version', async () => {
    const now = seconds();
    await env.TELEMETRY_DB.prepare(
      `INSERT INTO client_config (version, level_max, upload_interval_s, sample, disabled_until, updated_at)
       VALUES ('0.0.25', 'anonymous', 7200, 0.5, ?1, ?2)`,
    )
      .bind(now + 86400, now)
      .run();
    const id = hexId('t');
    const body = (await (await upload(sampleBatch(id), await tokenFor(id))).json()) as Record<string, any>;
    expect(body.config).toEqual({ level_max: 'anonymous', upload_interval_s: 7200, sample: 0.5, disabled_until: now + 86400 });
  });
});

describe('/healthz', () => {
  it('never touches D1', async () => {
    const poisoned = new Proxy(
      {},
      {
        get() {
          throw new Error('healthz read D1');
        },
      },
    );
    const response = await worker.fetch(
      new Request(`${BASE}/healthz`),
      { ...env, TELEMETRY_DB: poisoned as D1Database },
      { waitUntil() {}, passThroughOnException() {} } as unknown as ExecutionContext,
    );
    expect(response.status).toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body.ok).toBe(true);
    expect(['ok', 'unknown']).toContain(body.budget_state);
  });
});
