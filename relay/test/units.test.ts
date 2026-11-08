/**
 * Pure units in node: hosts, subprotocols, origins and vars, and the website switch's cache. (The certificate check is
 * licence.test.ts.)
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { admittedOrigin, allowedOrigins, exactOrigin, roomIdleMs, sshPorts } from '../src/env';
import type { Env } from '../src/env';
import { checkHost, parseIPv6 } from '../src/hosts';
import { FEATURES_CACHE_MS, resetFeaturesCache, websiteSwitch } from '../src/licence';
import { closeReason, parseProtocols, PROTOCOL } from '../src/protocol';

const env = (vars: Partial<Env>) => vars as Env;
const AUTH = 'A'.repeat(43);

describe('hosts', () => {
  it('takes DNS names and public addresses', () => {
    for (const host of ['o2.hms.harvard.edu', 'Login.Example.ORG', 'example.org.', 'a-b.c1.io', '8.8.8.8', '1.1.1.1',
      '2001:4860:4860::8888', '[2606:4700::1111]', '::ffff:8.8.8.8', '64:ff9b::808:808', '2002:808:808::1']) {
      expect(checkHost(host).ok, host).toBe(true);
    }
    expect(checkHost('Example.ORG.')).toEqual({ ok: true, hostname: 'example.org' });
    expect(checkHost('[2606:4700::1111]')).toEqual({ ok: true, hostname: '2606:4700::1111' });
  });

  it('refuses private, loopback, link-local, CGNAT and other unusable addresses', () => {
    for (const host of ['127.0.0.1', '127.255.255.254', '10.0.0.1', '172.16.0.1', '172.31.255.255', '192.168.0.1',
      '169.254.169.254', '100.64.0.1', '100.127.255.255', '0.0.0.0', '224.0.0.1', '255.255.255.255', '198.18.0.1',
      'localhost', 'LOCALHOST', 'x.localhost', '::', '::1', '[::1]', 'fc00::1', 'fd12:3456::1', 'fe80::1', 'fec0::1',
      'ff02::1', '::ffff:127.0.0.1', '::ffff:192.168.1.1', '::10.0.0.1', '64:ff9b::a00:1', '2002:a00:1::1']) {
      expect(checkHost(host), host).toEqual({ ok: false, reason: expect.any(String) });
    }
    // Just outside the ranges.
    for (const host of ['172.15.255.255', '172.32.0.1', '100.63.255.255', '100.128.0.1', '169.253.0.1', '11.0.0.1']) {
      expect(checkHost(host).ok, host).toBe(true);
    }
  });

  it('lets private addresses through with the development switch, but never a malformed host', () => {
    for (const host of ['127.0.0.1', 'localhost', '::1', '10.0.0.5']) expect(checkHost(host, true).ok, host).toBe(true);
    for (const host of ['127.1', '0x7f000001', '1.2.3', '1.2.3.4.5', '01.2.3.4', '256.1.1.1', 'a b', '', 'a..b', '-a.b',
      'a-.b', `${'a'.repeat(64)}.org`, 'fe80::1%en0', '1::2::3', ':::', '[1.2.3.4]', 'host_name.org']) {
      expect(checkHost(host, true).ok, host).toBe(false);
    }
    expect(checkHost(null).ok).toBe(false);
  });

  it('parses IPv6 forms', () => {
    expect(parseIPv6('::')).toEqual([0, 0, 0, 0, 0, 0, 0, 0]);
    expect(parseIPv6('1::')).toEqual([1, 0, 0, 0, 0, 0, 0, 0]);
    expect(parseIPv6('::ffff:1.2.3.4')).toEqual([0, 0, 0, 0, 0, 0xffff, 0x0102, 0x0304]);
    expect(parseIPv6('1:2:3:4:5:6:7:8')).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    expect(parseIPv6('1:2:3:4:5:6:1.2.3.4')).toEqual([1, 2, 3, 4, 5, 6, 0x0102, 0x0304]);
    expect(parseIPv6('1:2:3:4:5:6:7')).toBeNull();
    expect(parseIPv6('1:2:3:4:5:6:7:8:9')).toBeNull();
    expect(parseIPv6('1::2:3:4:5:6:7:8')).toBeNull();
  });
});

describe('subprotocols', () => {
  it('reads ours and ignores the rest', () => {
    expect(parseProtocols(`${PROTOCOL}, a.${AUTH}`)).toEqual({ protocol: true, auth: AUTH, licence: null, problem: null });
    expect(parseProtocols(`x, ${PROTOCOL},a.${AUTH}, l.BIOC1.k.p.s`)).toEqual({
      protocol: true, auth: AUTH, licence: 'BIOC1.k.p.s', problem: null,
    });
    expect(parseProtocols(null)).toEqual({ protocol: false, auth: null, licence: null, problem: null });
  });

  it('names what is wrong', () => {
    expect(parseProtocols(`${PROTOCOL}, a.${AUTH}, a.${AUTH}`).problem).toMatch(/more than one room auth/);
    expect(parseProtocols(`${PROTOCOL}, a.${AUTH}=`).problem).toMatch(/43 characters/);
    expect(parseProtocols(`${PROTOCOL}, a.${'A'.repeat(42)}`).problem).toMatch(/43 characters/);
    expect(parseProtocols(`${PROTOCOL}, a.${AUTH}, l.x, l.y`)).toMatchObject({ auth: AUTH, licence: null, problem: null });
  });

  it('keeps a close reason within 123 bytes of UTF-8', () => {
    expect(closeReason('short')).toBe('short');
    const long = closeReason('é'.repeat(100));
    expect(new TextEncoder().encode(long).length).toBeLessThanOrEqual(123);
    expect(long.endsWith('...')).toBe(true);
    expect(long).not.toContain('�');
  });
});

describe('vars', () => {
  it('origins: exact, or http loopback on any port; defaults to the website', () => {
    expect(allowedOrigins(env({}))).toEqual(['https://viewer.plexoraapp.com']);
    expect(allowedOrigins(env({ ALLOWED_ORIGINS: 'https://a.example, http://localhost:*  https://b.example/x *' })))
      .toEqual(['https://a.example', 'http://localhost:*']);
    expect(exactOrigin('https://*.example')).toBeNull();
    expect(exactOrigin('http://example.org')).toBeNull();
    expect(exactOrigin('https://*:*')).toBeNull();
    const dev = env({ ALLOWED_ORIGINS: 'http://localhost:*,http://127.0.0.1:8765' });
    expect(admittedOrigin('http://localhost:3000', dev)).toBe('http://localhost:3000');
    expect(admittedOrigin('http://localhost', dev)).toBe('http://localhost');
    expect(admittedOrigin('http://127.0.0.1:8765', dev)).toBe('http://127.0.0.1:8765');
    for (const origin of ['http://127.0.0.1:8766', 'http://localhost:0', 'http://localhost:3000/', 'http://localhost:abc',
      'https://localhost:3000', 'http://localhost.evil:3000', null]) {
      expect(admittedOrigin(origin, dev), String(origin)).toBeNull();
    }
  });

  it('ports and idle days, with their defaults', () => {
    expect(sshPorts(env({}))).toEqual([22]);
    expect(sshPorts(env({ SSH_PORTS: '22, 2222 x 0 70000' }))).toEqual([22, 2222]);
    expect(roomIdleMs(env({}))).toBe(7 * 86_400_000);
    expect(roomIdleMs(env({ ROOM_IDLE_DAYS: '0.5' }))).toBe(43_200_000);
    expect(roomIdleMs(env({ ROOM_IDLE_DAYS: '-1' }))).toBe(7 * 86_400_000);
  });
});

describe('the website switch', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetFeaturesCache();
  });

  const answer = (website: unknown) =>
    new Response(JSON.stringify({ remote_servers: { website, local: 'free' } }), { headers: { 'Content-Type': 'application/json' } });

  it('empty FEATURES_URL is free without a request', async () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    expect(await websiteSwitch('')).toBe('free');
    expect(await websiteSwitch(undefined)).toBe('free');
    expect(fetch).not.toHaveBeenCalled();
  });

  it('is read at most once a minute per URL', async () => {
    const fetch = vi.fn(async () => answer('licensed'));
    vi.stubGlobal('fetch', fetch);
    const url = 'https://api.example/v1/features';
    expect(await websiteSwitch(url, 1_000_000)).toBe('licensed');
    expect(await websiteSwitch(url, 1_000_000 + FEATURES_CACHE_MS - 1)).toBe('licensed');
    expect(fetch).toHaveBeenCalledTimes(1);
    fetch.mockImplementation(async () => answer('free'));
    expect(await websiteSwitch(url, 1_000_000 + FEATURES_CACHE_MS)).toBe('free');
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('when it cannot be read, the last value stands, else free', async () => {
    const url = 'https://api.example/v1/features';
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('fetch failed'); }));
    expect(await websiteSwitch(url, 0)).toBe('free');

    vi.stubGlobal('fetch', vi.fn(async () => answer('licensed')));
    let at = FEATURES_CACHE_MS;
    expect(await websiteSwitch(url, at)).toBe('licensed');
    for (const failure of [
      async () => { throw new TypeError('fetch failed'); },
      async () => new Response('{}', { status: 503 }),
      async () => new Response('<html>'),
      async () => answer('maybe'),
      async () => new Response(JSON.stringify({ remote_servers: 'licensed' })),
    ]) {
      // A minute on, so the switch is read again, and fails.
      at += FEATURES_CACHE_MS;
      const fetch = vi.fn(failure);
      vi.stubGlobal('fetch', fetch);
      expect(await websiteSwitch(url, at)).toBe('licensed');
      expect(fetch).toHaveBeenCalledTimes(1);
    }
  });
});
