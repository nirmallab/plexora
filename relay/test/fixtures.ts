/**
 * Node-side test fixtures shared by the Workers tests (through globalSetup's `provide`) and the `wrangler dev` run:
 *
 *   - a TCP echo server, a "banner" server that greets like sshd and hangs up, and a port nothing listens on;
 *   - a features server: GET /free, /licensed, /broken (500) and /garbage (not JSON), shaped like
 *     https://api.biocognia.com/v1/features;
 *   - certificates minted with a fresh Ed25519 key (kid `test1`, trusted by nothing outside the tests) in the BIOC1
 *     format the platform signs: a current plexora one, an expired one, one for another product.
 */
import { generateKeyPairSync, sign } from 'node:crypto';
import { createServer as createHttpServer, type Server as HttpServer } from 'node:http';
import { createServer, type AddressInfo, type Server } from 'node:net';

export interface Fixtures {
  echoPort: number;
  bannerPort: number;
  deadPort: number;
  featuresPort: number;
  testKid: string;
  testPublicKey: string;
  certificates: { valid: string; expired: string; wrongProduct: string; inGrace: string };
  close: () => Promise<void>;
}

export const BANNER = 'SSH-2.0-plexora-relay-test\r\n';

function listen(server: Server | HttpServer): Promise<number> {
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve((server.address() as AddressInfo).port)));
}

function shut(server: Server | HttpServer): Promise<void> {
  return new Promise((resolve) => server.close(() => resolve()));
}

const b64url = (bytes: Uint8Array | Buffer) => Buffer.from(bytes).toString('base64url');

/** JSON with sorted keys and no whitespace (licenseVerify.js `canonicalJson`), as the platform signs. */
function canonical(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((k) => `${JSON.stringify(k)}:${canonical(record[k])}`).join(',')}}`;
}

export function mintKey(kid = 'test1') {
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  const raw = publicKey.export({ format: 'jwk' }).x as string;
  const certify = (claims: Record<string, unknown>) => {
    const payload = Buffer.from(canonical({ v: 1, kid, ...claims }));
    return `BIOC1.${kid}.${b64url(payload)}.${b64url(sign(null, payload, privateKey))}`;
  };
  return { kid, publicKey: raw, certify };
}

export async function startFixtures(): Promise<Fixtures> {
  const sockets = new Set<import('node:net').Socket>();
  const echo = createServer((socket) => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
    socket.on('error', () => undefined);
    socket.pipe(socket);
  });
  const banner = createServer((socket) => {
    socket.on('error', () => undefined);
    socket.end(BANNER);
  });
  const dead = createServer();
  const features = createHttpServer((request, response) => {
    const send = (status: number, body: string) => {
      response.writeHead(status, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      response.end(body);
    };
    if (request.url === '/free') send(200, JSON.stringify({ remote_servers: { website: 'free', local: 'free' } }));
    else if (request.url === '/licensed') send(200, JSON.stringify({ remote_servers: { website: 'licensed', local: 'free' } }));
    else if (request.url === '/broken') send(500, '{}');
    else if (request.url === '/garbage') send(200, 'not json');
    else send(404, '{}');
  });

  const [echoPort, bannerPort, deadPort, featuresPort] = await Promise.all([
    listen(echo), listen(banner), listen(dead), listen(features),
  ]);
  await shut(dead); // a port that refuses connections

  const now = Math.floor(Date.now() / 1000);
  const key = mintKey();
  const base = { aud: 'plexora', typ: 'env', ent: ['ai'], lim: {}, iat: now - 3600, grace_days: 14, offline_until: null };
  const certificates = {
    valid: key.certify({ ...base, cid: 'crt_valid', exp: now + 30 * 86400 }),
    inGrace: key.certify({ ...base, cid: 'crt_grace', exp: now - 86400 }),
    expired: key.certify({ ...base, cid: 'crt_expired', exp: now - 20 * 86400 }),
    wrongProduct: key.certify({ ...base, cid: 'crt_scimap', aud: 'scimappro', exp: now + 30 * 86400 }),
  };

  return {
    echoPort,
    bannerPort,
    deadPort,
    featuresPort,
    testKid: key.kid,
    testPublicKey: key.publicKey,
    certificates,
    close: async () => {
      for (const socket of sockets) socket.destroy();
      await Promise.all([shut(echo), shut(banner), shut(features)]);
    },
  };
}
