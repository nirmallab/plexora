/**
 * Calling conventions for the route tests: the requests the Plexora client
 * sends, the admin API a person drives, and a few ways to look at D1.
 */
import { env, SELF } from 'cloudflare:test';
import { expect } from 'vitest';

import { base64urlDecode } from '../../src/crypto';
import { setClockOffset } from '../../src/env';
import { ERROR_CODES } from '../../src/http';

export const BASE = 'http://localhost';
export const ADMIN_HEADERS = { Authorization: 'Bearer test-admin', 'Content-Type': 'application/json' };

export interface Reply<T = Record<string, any>> {
  status: number;
  json: T;
  headers: Headers;
}

export async function call(method: string, path: string, body?: unknown,
  headers: Record<string, string> = {}): Promise<Reply> {
  const response = await SELF.fetch(`${BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', 'CF-Connecting-IP': '203.0.113.7', ...headers },
    body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body),
    redirect: 'manual',
  });
  const text = await response.text();
  let json: any = {};
  try {
    json = text ? JSON.parse(text) : {};
  } catch {
    json = { text };
  }
  if (response.status >= 400 && response.headers.get('Content-Type')?.includes('application/json')) {
    // Every refusal has the one shape, with a known code.
    expect(json.error, `${method} ${path} -> ${text}`).toBeTypeOf('object');
    expect(ERROR_CODES).toContain(json.error.code);
    expect(json.error.message).toBeTypeOf('string');
  }
  return { status: response.status, json, headers: response.headers };
}

export const post = (path: string, body?: unknown, headers?: Record<string, string>) => call('POST', path, body ?? {}, headers);
export const admin = (method: string, path: string, body?: unknown) =>
  call(method, `/admin/api${path}`, body, ADMIN_HEADERS);

let counter = 0;
/** A distinct 64-hex client binding per call (what sha256(secret) looks like). */
export function binding(): string {
  counter += 1;
  return (counter.toString(16).padStart(8, '0') + 'ab'.repeat(28)).slice(0, 64);
}

/** A 43-char base64url string, as a delegation public key looks. */
export function pubkey(seed = 1): string {
  return 'A'.repeat(42) + String.fromCharCode(65 + (seed % 26));
}

export async function issue(overrides: Record<string, unknown> = {}) {
  const reply = await admin('POST', '/licenses', {
    owner_email: `owner${++counter}@lab.example.org`, use_class: 'academic', seats: 2, days: 365,
    send_email: false, ...overrides,
  });
  expect(reply.status, JSON.stringify(reply.json)).toBe(201);
  return reply.json as { license: Record<string, any>; account_id: string; seat: { id: string; key: string } };
}

export function environmentBody(kind = 'desktop', extra: Record<string, unknown> = {}) {
  return { kind, display_name: `${kind} ${counter}`, binding: binding(), platform: 'linux-x86_64',
    scheduler_hint: kind === 'cluster' ? 'slurm' : 'none', app_version: '0.0.25', ...extra };
}

export async function activate(credential: string, environment: Record<string, unknown> = environmentBody()) {
  return post('/v1/activate', { credential, environment });
}

/** The payload of a certificate, decoded without verifying (tests only). */
export function claims(certificate: string): Record<string, any> {
  return JSON.parse(new TextDecoder().decode(base64urlDecode(certificate.split('.')[2]!)));
}

export async function count(table: string, where = '1 = 1', ...params: unknown[]): Promise<number> {
  const row = await env.LICENSE_DB.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE ${where}`)
    .bind(...params).first<{ n: number }>();
  return row?.n ?? 0;
}

/** Every row of every table, for "this request wrote nothing" assertions. */
export async function fingerprint(): Promise<string> {
  const tables = ['accounts', 'users', 'account_members', 'licenses', 'seat_assignments', 'environments',
    'license_tokens', 'offline_grants', 'trials', 'events', 'signals', 'purchases'];
  const parts: string[] = [];
  for (const table of tables) {
    const rows = await env.LICENSE_DB.prepare(`SELECT * FROM ${table}`).all();
    parts.push(JSON.stringify(rows.results));
  }
  return parts.join('\n');
}

export function travel(seconds: number): void {
  setClockOffset(seconds);
}
