import { SELF } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { admittedOrigin, exactOrigin, extraOrigins, WEB_ORIGINS } from '../../src/cors';
import { BASE, hexId, sampleBatch, tokenFor, upload } from './helpers';

const SITE = 'https://viewer.plexoraapp.com';
const PREVIEW = 'https://plexora-viewer-preview.pages.dev';

function preflight(path: string, origin: string, method = 'POST') {
  return SELF.fetch(`${BASE}${path}`, {
    method: 'OPTIONS',
    headers: {
      Origin: origin,
      'Access-Control-Request-Method': method,
      'Access-Control-Request-Headers': 'authorization,content-encoding,content-type',
    },
  });
}

function registerFrom(origin: string | null) {
  return SELF.fetch(`${BASE}/v1/telemetry/register`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'CF-Connecting-IP': '192.0.2.44',
      ...(origin ? { Origin: origin } : {}),
    },
    body: JSON.stringify({ schema: 1, install_id: hexId('c'), client: { plexora_version: '0.0.36', deployment: 'web', runtime: 'web' } }),
  });
}

describe('exact-origin CORS for the website', () => {
  it('admits the website and its preview, exactly', () => {
    expect(WEB_ORIGINS).toEqual([SITE, PREVIEW]);
    expect(admittedOrigin(SITE)).toBe(SITE);
    expect(admittedOrigin(PREVIEW)).toBe(PREVIEW);
    for (const other of [`${SITE}/`, 'https://viewer.plexoraapp.com.evil.example', 'http://viewer.plexoraapp.com',
      'https://VIEWER.plexoraapp.com', 'null', '', null]) {
      expect(admittedOrigin(other)).toBeNull();
    }
    expect(exactOrigin('https://*.pages.dev')).toBeNull();
    expect(extraOrigins('http://127.0.0.1:8700, https://staging.example https://x.example/path *')).toEqual([
      'http://127.0.0.1:8700',
      'https://staging.example',
    ]);
    expect(admittedOrigin('http://127.0.0.1:8700', extraOrigins('http://127.0.0.1:8700'))).toBe('http://127.0.0.1:8700');
  });

  it('answers a preflight from the site with what the uploader sends, and nothing else runs', async () => {
    for (const path of ['/v1/telemetry/register', '/v1/telemetry/events']) {
      const response = await preflight(path, SITE);
      expect(response.status).toBe(204);
      expect(response.headers.get('Access-Control-Allow-Origin')).toBe(SITE);
      expect(response.headers.get('Access-Control-Allow-Methods')).toBe('POST, OPTIONS');
      expect(response.headers.get('Access-Control-Allow-Headers')).toBe('Authorization, Content-Type, Content-Encoding');
      expect(response.headers.get('Access-Control-Max-Age')).toBe('7200');
      expect(response.headers.get('Vary')).toContain('Origin');
      expect(response.headers.get('Access-Control-Allow-Credentials')).toBeNull();
    }
    const config = await preflight('/v1/telemetry/config', PREVIEW, 'GET');
    expect(config.headers.get('Access-Control-Allow-Methods')).toBe('GET, OPTIONS');
  });

  it('refuses a preflight from any other origin by saying nothing', async () => {
    const response = await preflight('/v1/telemetry/events', 'https://evil.example');
    expect(response.status).toBe(204);
    expect(response.headers.get('Access-Control-Allow-Origin')).toBeNull();
    expect(response.headers.get('Access-Control-Allow-Headers')).toBeNull();
  });

  it('lets the site read a registration, and its errors', async () => {
    const response = await registerFrom(SITE);
    expect(response.status).toBe(200);
    expect(response.headers.get('Access-Control-Allow-Origin')).toBe(SITE);
    expect(response.headers.get('Access-Control-Expose-Headers')).toBe('Retry-After');
    const token = ((await response.json()) as { install_token: string }).install_token;
    expect(token).toMatch(/^PLEXORAT1\./);

    const refused = await upload(sampleBatch(hexId('d')), 'PLEXORAT1.not.valid', { headers: { Origin: SITE } });
    expect(refused.status).toBe(401);
    expect(refused.headers.get('Access-Control-Allow-Origin')).toBe(SITE);

    const id = hexId('e');
    const batch = sampleBatch(id);
    batch.client = { ...batch.client, deployment: 'web', launch_mode: 'web', install_kind: 'web', runtime: 'web' };
    const accepted = await upload(batch, await tokenFor(id), { headers: { Origin: PREVIEW } });
    expect(accepted.status).toBe(202);
    expect(accepted.headers.get('Access-Control-Allow-Origin')).toBe(PREVIEW);
    expect(((await accepted.json()) as { unknown: number }).unknown).toBe(0);
  });

  it('answers a Plexora process, which sends no Origin, exactly as before', async () => {
    const response = await registerFrom(null);
    expect(response.status).toBe(200);
    expect(response.headers.get('Access-Control-Allow-Origin')).toBeNull();
    const other = await registerFrom('https://evil.example');
    expect(other.status).toBe(200);
    expect(other.headers.get('Access-Control-Allow-Origin')).toBeNull();
  });
});
