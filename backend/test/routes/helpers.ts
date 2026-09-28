/**
 * Calling conventions for the route tests. Batches start from the fixture the
 * Python client generated, so a test exercises the wire format the client
 * really sends, not a hand-written approximation of it.
 */
import { env, SELF } from 'cloudflare:test';

import fixture from '../fixtures/sample-batch.json';
import { mintToken } from '../../src/telemetry/tokens';

export const BASE = 'http://localhost';
export const ADMIN_TOKEN = 'test-admin';

export function seconds(): number {
  return Math.floor(Date.now() / 1000);
}

let counter = 0;

/** A fresh 32-hex id, distinct per call. */
export function hexId(prefix = ''): string {
  counter += 1;
  const tail = (Date.now().toString(16) + counter.toString(16).padStart(8, '0')).padStart(32, '0');
  return (prefix + tail).slice(-32).padStart(32, '0');
}

export type Batch = {
  schema: number;
  batch_id: string;
  client: Record<string, unknown>;
  events: Record<string, unknown>[];
};

export function sampleBatch(installId: string, overrides: Partial<Batch> = {}): Batch {
  const batch = structuredClone(fixture) as unknown as Batch;
  batch.batch_id = hexId('b');
  batch.client.install_id = installId;
  return { ...batch, ...overrides };
}

export function eventOf(batch: Batch, type: string): Record<string, unknown> {
  const event = batch.events.find((e) => e.type === type);
  if (!event) throw new Error(`fixture has no ${type}`);
  return event;
}

export async function tokenFor(installId: string): Promise<string> {
  return (await mintToken(env, installId, seconds(), 86400 * 365)).token;
}

export async function gzipBytes(data: string | Uint8Array): Promise<Uint8Array> {
  const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data;
  const stream = new Blob([bytes]).stream().pipeThrough(new CompressionStream('gzip'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

export async function upload(
  batch: unknown,
  token: string | null,
  options: { gzip?: boolean; headers?: Record<string, string>; raw?: Uint8Array } = {},
): Promise<Response> {
  const json = JSON.stringify(batch);
  const gz = options.gzip ?? true;
  const body = options.raw ?? (gz ? await gzipBytes(json) : new TextEncoder().encode(json));
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(gz ? { 'Content-Encoding': 'gzip' } : {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...options.headers,
  };
  return SELF.fetch(`${BASE}/v1/telemetry/events`, { method: 'POST', headers, body });
}

export async function register(installId: string, client: Record<string, unknown> = {}, ip = '203.0.113.7') {
  return SELF.fetch(`${BASE}/v1/telemetry/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'CF-Connecting-IP': ip },
    body: JSON.stringify({ schema: 1, install_id: installId, client: { plexora_version: '0.0.25', os: 'mac', ...client } }),
  });
}

export async function count(table: string, where = '1 = 1', ...binds: unknown[]): Promise<number> {
  const row = await env.TELEMETRY_DB.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE ${where}`)
    .bind(...binds)
    .first<{ n: number }>();
  return row?.n ?? 0;
}

export async function budgetRow(day: string) {
  return env.TELEMETRY_DB.prepare('SELECT * FROM budget_daily WHERE day = ?').bind(day).first<Record<string, number | string>>();
}

/** Sets today's budget counters so the ratio lands in a chosen state. */
export async function setBudget(day: string, writes: number, reads = 0, requests = 0) {
  await env.TELEMETRY_DB.prepare(
    `INSERT INTO budget_daily (day, d1_rows_written, d1_rows_read, requests) VALUES (?1, ?2, ?3, ?4)
     ON CONFLICT(day) DO UPDATE SET d1_rows_written = ?2, d1_rows_read = ?3, requests = ?4`,
  )
    .bind(day, writes, reads, requests)
    .run();
}

export function admin(path: string, init: RequestInit = {}, token = ADMIN_TOKEN): Promise<Response> {
  const headers = new Headers(init.headers);
  if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`);
  return SELF.fetch(`${BASE}${path}`, { ...init, headers, redirect: 'manual' });
}

export function adminPost(path: string, body: unknown, headers: Record<string, string> = {}) {
  return admin(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
  });
}
