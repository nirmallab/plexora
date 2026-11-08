import { beforeEach, describe, expect, inject, it } from 'vitest';

import worker from '../../src/index';
import { resetFeaturesCache } from '../../src/licence';
import { env, SITE } from './helpers';

const fixtures = inject('fixtures');
const features = (path: string) => `http://127.0.0.1:${fixtures.featuresPort}${path}`;

function get(path: string, init: RequestInit = {}, vars = {}) {
  return worker.fetch(new Request(`https://relay.test${path}`, init), { ...env, ...vars });
}

beforeEach(resetFeaturesCache);

describe('/v1/hello', () => {
  it('says what the relay offers, with CORS for an allowed origin', async () => {
    const response = await get('/v1/hello', { headers: { Origin: SITE } });
    expect(response.status).toBe(200);
    expect(response.headers.get('Content-Type')).toBe('application/json');
    expect(response.headers.get('Access-Control-Allow-Origin')).toBe(SITE);
    expect(response.headers.get('Vary')).toContain('Origin');
    expect(response.headers.get('Access-Control-Allow-Credentials')).toBeNull();
    expect(await response.json()).toEqual({
      relay: 1,
      ssh_ports: [22, fixtures.echoPort, fixtures.bannerPort, fixtures.deadPort],
      website: 'free',
    });
  });

  it('defaults: port 22 and the website origin only, when the vars are unset', async () => {
    const vars = { SSH_PORTS: undefined, ALLOWED_ORIGINS: undefined };
    const response = await get('/v1/hello', { headers: { Origin: SITE } }, vars);
    expect(((await response.json()) as { ssh_ports: number[] }).ssh_ports).toEqual([22]);
    expect(response.headers.get('Access-Control-Allow-Origin')).toBe(SITE);
    const local = await get('/v1/hello', { headers: { Origin: 'http://localhost:5173' } }, vars);
    expect(local.headers.get('Access-Control-Allow-Origin')).toBeNull();
  });

  it('never echoes an origin it does not admit; http://localhost:* admits loopback on any port only', async () => {
    for (const origin of ['https://evil.example', `${SITE}.evil.example`, 'http://localhost.evil.example:5173',
      'https://localhost:5173', 'http://localhost:99999', 'null']) {
      const response = await get('/v1/hello', { headers: { Origin: origin } });
      expect(response.status).toBe(200);
      expect(response.headers.get('Access-Control-Allow-Origin'), origin).toBeNull();
    }
    for (const origin of ['http://localhost:5173', 'http://localhost:8765']) {
      const response = await get('/v1/hello', { headers: { Origin: origin } });
      expect(response.headers.get('Access-Control-Allow-Origin')).toBe(origin);
    }
  });

  it('answers a preflight from an allowed origin, and nothing for another', async () => {
    const preflight = (origin: string) => get('/v1/hello', {
      method: 'OPTIONS',
      headers: { Origin: origin, 'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'x-plexora' },
    });
    const ok = await preflight(SITE);
    expect(ok.status).toBe(204);
    expect(ok.headers.get('Access-Control-Allow-Origin')).toBe(SITE);
    expect(ok.headers.get('Access-Control-Allow-Methods')).toBe('GET, OPTIONS');
    expect(ok.headers.get('Access-Control-Allow-Headers')).toBe('x-plexora');
    const other = await preflight('https://evil.example');
    expect(other.status).toBe(204);
    expect(other.headers.get('Access-Control-Allow-Origin')).toBeNull();
    expect(other.headers.get('Access-Control-Allow-Methods')).toBeNull();
  });

  it('reports the website switch from FEATURES_URL', async () => {
    const read = async (url: string) =>
      ((await (await get('/v1/hello', {}, { FEATURES_URL: url })).json()) as { website: string }).website;
    expect(await read(features('/licensed'))).toBe('licensed');
    expect(await read(features('/free'))).toBe('free');
    expect(await read('')).toBe('free');
    expect(await read(features('/broken'))).toBe('free');
  });

  it('is GET, HEAD or OPTIONS only', async () => {
    expect((await get('/v1/hello', { method: 'POST' })).status).toBe(405);
    expect((await get('/v1/hello', { method: 'HEAD' })).status).toBe(200);
  });
});

describe('everything else', () => {
  it('is 404', async () => {
    for (const path of ['/', '/v1', '/v1/hello/', '/v1/rooms', '/v1/rooms/', '/v1/rooms/abc/def', '/v2/hello', '/healthz']) {
      const response = await get(path);
      expect(response.status, path).toBe(path === '/v1/rooms/' ? 426 : 404);
    }
  });

  it('a room path that is not an upgrade is 426', async () => {
    expect((await get('/v1/rooms/AAAAAAAAAAAAAAAAAAAAAA?role=page')).status).toBe(426);
  });
});
