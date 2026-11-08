import { afterEach, beforeEach, describe, expect, inject, it } from 'vitest';

import { resetFeaturesCache, setTrustedKeysForTests } from '../../src/licence';
import { LICENCE_REQUIRED } from '../../src/protocol';
import { BANNER } from '../fixtures';
import worker from '../../src/index';
import { closeAll, env, open, PROTOCOL, refusal, SITE } from './helpers';

const fixtures = inject('fixtures');
const features = (path: string) => `http://127.0.0.1:${fixtures.featuresPort}${path}`;
const ssh = (host: string, port: number | string) => `/v1/ssh?host=${encodeURIComponent(host)}&port=${port}`;

beforeEach(() => {
  resetFeaturesCache();
  setTrustedKeysForTests({ [fixtures.testKid]: fixtures.testPublicKey });
});
afterEach(() => {
  closeAll();
  setTrustedKeysForTests(null);
});

describe('/v1/ssh pipe', () => {
  it('pipes binary both ways to a TCP server, answering plexora-relay.v1 when offered', async () => {
    const client = await open(ssh('127.0.0.1', fixtures.echoPort), { protocols: [PROTOCOL] });
    expect(client.protocol).toBe(PROTOCOL);
    client.send(Uint8Array.from([1, 2, 3, 250]));
    expect(Array.from(await client.binary())).toEqual([1, 2, 3, 250]);
    // Text is not TCP bytes: ignored.
    client.send('ignored');
    const big = crypto.getRandomValues(new Uint8Array(60000));
    client.send(big);
    const back: number[] = [];
    while (back.length < big.length) back.push(...(await client.binary()));
    expect(Uint8Array.from(back)).toEqual(big);
  });

  it('works with no subprotocol offered, and answers none', async () => {
    const client = await open(ssh('localhost', fixtures.echoPort));
    expect(client.protocol).toBeNull();
    client.send(Uint8Array.from([7]));
    expect(Array.from(await client.binary())).toEqual([7]);
  });

  it('passes on what the server says first, then closes 1000 when it hangs up', async () => {
    const client = await open(ssh('127.0.0.1', fixtures.bannerPort));
    expect(new TextDecoder().decode(await client.binary())).toBe(BANNER);
    expect((await client.closed).code).toBe(1000);
  });

  it('closes 4502 with the error when the connect fails', async () => {
    const client = await open(ssh('127.0.0.1', fixtures.deadPort));
    const closed = await client.closed;
    expect(closed.code).toBe(4502);
    expect(closed.reason.length).toBeGreaterThan(0);
  });

  it('answers a plain GET 426: it is a WebSocket or nothing', async () => {
    const plain = await worker.fetch(new Request(`https://relay.test${ssh('127.0.0.1', 22)}`, { headers: { Origin: SITE } }), env);
    expect(plain.status).toBe(426);
  });
});

describe('/v1/ssh refusals', () => {
  it('an Origin not allowed, or none: 4403', async () => {
    for (const origin of [null, 'https://evil.example', 'http://localhost.evil.example:80', 'https://plexora-viewer-preview.pages.dev']) {
      const closed = await refusal(await open(ssh('example.org', 22), { origin, protocols: [PROTOCOL] }));
      expect(closed.code, String(origin)).toBe(4403);
    }
  });

  it('a port not in SSH_PORTS: 4403; no port or not a port: 4400', async () => {
    expect((await refusal(await open(ssh('example.org', 2222)))).code).toBe(4403);
    expect((await refusal(await open('/v1/ssh?host=example.org'))).code).toBe(4400);
    for (const port of ['0', '65536', '22x', '-22', '022']) {
      expect((await refusal(await open(ssh('example.org', port)))).code, port).toBe(4400);
    }
  });

  it('no host, or not a DNS name or IP literal: 4400', async () => {
    expect((await refusal(await open('/v1/ssh?port=22'))).code).toBe(4400);
    for (const host of ['exa mple.org', '-bad.org', 'a..b', '127.1', '0x7f.1', '2130706433', '[1.2.3.4]', 'fe80::1%eth0']) {
      expect((await refusal(await open(ssh(host, 22)))).code, host).toBe(4400);
    }
  });

  it('private, loopback, link-local and CGNAT addresses: 4400 unless ALLOW_PRIVATE_HOSTS is "1"', async () => {
    const vars = { ALLOW_PRIVATE_HOSTS: undefined };
    for (const host of ['127.0.0.1', '10.1.2.3', '172.16.0.1', '192.168.1.1', '169.254.169.254', '100.64.0.1', '0.0.0.0',
      'localhost', 'db.localhost', '::1', '[::1]', 'fd00::1', 'fe80::1', '::ffff:127.0.0.1', '::ffff:10.0.0.1']) {
      const closed = await refusal(await open(ssh(host, fixtures.echoPort), { vars }));
      expect(closed.code, host).toBe(4400);
    }
  });
});

describe('/v1/ssh licence switch', () => {
  const licensed = { FEATURES_URL: features('/licensed') };

  it('"free" (and an empty FEATURES_URL) asks for no certificate', async () => {
    for (const vars of [{ FEATURES_URL: features('/free') }, { FEATURES_URL: '' }]) {
      const client = await open(ssh('127.0.0.1', fixtures.echoPort), { vars, protocols: [PROTOCOL] });
      client.send(Uint8Array.from([1]));
      expect(Array.from(await client.binary())).toEqual([1]);
      client.close();
    }
  });

  it('"licensed" refuses no certificate, a bad one, an expired one and another product\'s, with 4403', async () => {
    const offers = [
      [PROTOCOL],
      [PROTOCOL, 'l.not-a-certificate'],
      [PROTOCOL, `l.${fixtures.certificates.expired}`],
      [PROTOCOL, `l.${fixtures.certificates.wrongProduct}`],
      [PROTOCOL, `l.${fixtures.certificates.valid.slice(0, -4)}AAAA`],
    ];
    for (const protocols of offers) {
      const client = await open(ssh('127.0.0.1', fixtures.echoPort), { vars: licensed, protocols });
      expect(client.protocol).toBe(PROTOCOL);
      expect(await client.closed, protocols.join()).toEqual({ code: 4403, reason: LICENCE_REQUIRED });
    }
  });

  it('"licensed" lets a valid Plexora certificate through, and one in its grace days', async () => {
    for (const certificate of [fixtures.certificates.valid, fixtures.certificates.inGrace]) {
      const client = await open(ssh('127.0.0.1', fixtures.echoPort), { vars: licensed, protocols: [PROTOCOL, `l.${certificate}`] });
      client.send(Uint8Array.from([2]));
      expect(Array.from(await client.binary())).toEqual([2]);
      client.close();
    }
  });

  it('a certificate signed by a key the relay does not trust is refused (the platform keys only, by default)', async () => {
    setTrustedKeysForTests(null);
    const client = await open(ssh('127.0.0.1', fixtures.echoPort), {
      vars: licensed,
      protocols: [PROTOCOL, `l.${fixtures.certificates.valid}`],
    });
    expect((await client.closed).code).toBe(4403);
  });

  it('a switch that cannot be read is "free" when nothing was read before', async () => {
    // (A server that refuses the connection is the node project's case: workerd logs a caught fetch failure as uncaught.)
    for (const url of [features('/broken'), features('/garbage')]) {
      const client = await open(ssh('127.0.0.1', fixtures.echoPort), { vars: { FEATURES_URL: url } });
      client.send(Uint8Array.from([3]));
      expect(Array.from(await client.binary()), url).toEqual([3]);
      client.close();
    }
  });
});
