import { env, SELF } from 'cloudflare:test';
import { afterEach, expect } from 'vitest';

import { setUpstreamFetch } from '../../src/ai/providers';
import { activate, admin, BASE, environmentBody, issue, post, travel } from './helpers';

/**
 * The gateway route tests' stand-ins: every provider faked by URL, a licensed
 * seat with credit and a token, one model call, and what it streamed back.
 */

interface Seen { url: string; body: Record<string, any>; headers: Headers }
export const seen: Seen[] = [];

type Reply = (s: Seen) => Response;
const replies: Array<{ match: string; reply: Reply }> = [];

export function on(match: string, reply: Reply) {
  replies.unshift({ match, reply });
}

setUpstreamFetch(null);

export function install() {
  setUpstreamFetch(async (url, init) => {
    const s = { url, body: JSON.parse(String(init.body)), headers: new Headers(init.headers) };
    seen.push(s);
    const hit = replies.find((r) => url.includes(r.match));
    return hit ? hit.reply(s) : new Response('no fake for this url', { status: 500 });
  });
}

afterEach(() => {
  setUpstreamFetch(null);
  replies.length = 0;
  seen.length = 0;
  travel(0);
});

export const sseOf = (events: Array<[string | null, unknown]>, headers: Record<string, string> = {}) =>
  new Response(events.map(([name, data]) => `${name ? `event: ${name}\n` : ''}data: ${
    typeof data === 'string' ? data : JSON.stringify(data)}\n\n`).join(''),
  { headers: { 'content-type': 'text/event-stream', ...headers } });

export function anthropicStream(text = '{"kind":"t2_confirm","direction":"about_right"}', headers: Record<string, string> = {}) {
  const events: Array<[string, Record<string, unknown>]> = [
    ['message_start', { type: 'message_start', message: { id: 'msg_a', model: 'claude-opus-5-5',
      usage: { input_tokens: 100, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, output_tokens: 1 } } }],
    ['content_block_start', { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } }],
    ['content_block_delta', { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text } }],
    ['content_block_stop', { type: 'content_block_stop', index: 0 }],
    ['message_delta', { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 10 } }],
    ['message_stop', { type: 'message_stop' }],
  ];
  return sseOf(events, headers);
}

export function responsesStream(text = '{"kind":"t2_confirm","direction":"about_right"}') {
  const half = Math.floor(text.length / 2);
  return sseOf([
    ['response.created', { type: 'response.created', response: { id: 'resp_1', model: 'gpt-test' } }],
    ['response.output_item.added', { type: 'response.output_item.added', output_index: 0,
      item: { type: 'message', id: 'item_1' } }],
    ['response.output_text.delta', { type: 'response.output_text.delta', item_id: 'item_1', content_index: 0,
      delta: text.slice(0, half) }],
    ['response.output_text.delta', { type: 'response.output_text.delta', item_id: 'item_1', content_index: 0,
      delta: text.slice(half) }],
    ['response.output_item.done', { type: 'response.output_item.done', item: { id: 'item_1' } }],
    ['response.completed', { type: 'response.completed', response: { id: 'resp_1', model: 'gpt-test',
      usage: { input_tokens: 1000, input_tokens_details: { cached_tokens: 800 }, output_tokens: 50 } } }],
  ]);
}

export function chatToolStream(upstream?: string) {
  const via = upstream ? { provider: upstream } : {};
  return sseOf([
    [null, { id: 'gen-1', model: 'anthropic/claude-opus-5-5', ...via, choices: [{ index: 0, delta: { role: 'assistant',
      content: 'Looking.' } }] }],
    [null, { id: 'gen-1', choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'call_1', type: 'function',
      function: { name: 'read_board', arguments: '' } }] } }] }],
    [null, { id: 'gen-1', choices: [{ index: 0, delta: { tool_calls: [{ index: 0,
      function: { arguments: '{"keys":["CD3"]}' } }] } }] }],
    [null, { id: 'gen-1', choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] }],
    [null, { id: 'gen-1', choices: [], usage: { prompt_tokens: 500, completion_tokens: 20,
      prompt_tokens_details: { cached_tokens: 400 }, cost: 0.0021 } }],
    [null, '[DONE]'],
  ]);
}

export const failing = (status = 503) => () => new Response('{"error":"down"}', { status });

export async function setup(overrides: Record<string, unknown> = {}) {
  const issued = await issue(overrides);
  const body = environmentBody();
  const activated = await activate(issued.seat.key, body);
  expect(activated.status).toBe(200);
  await admin('POST', `/ai/accounts/${issued.account_id}/credit`, { credits: 1000 });
  const reissue = async () => {
    const reply = await post('/v1/ai/token', { certificate: activated.json.certificate, binding: body.binding,
      app_version: '0.0.27' });
    expect(reply.status, JSON.stringify(reply.json)).toBe(200);
    return reply.json.token as string;
  };
  const token = await reissue();
  install();
  return { account_id: issued.account_id, token, reissue };
}

let keys = 0;
export function request(capability = 'vision_judgement', ctx: Record<string, unknown> = {}, extra: Record<string, unknown> = {}) {
  return {
    capability,
    context: { feature: 'gating', agent: 'gating_worker', session_id: 'gs_route', ...ctx },
    request: {
      system: [{ type: 'text', text: 'You are a Plexora worker.', cache_control: { type: 'ephemeral' } }],
      messages: [{ role: 'user', content: [{ type: 'text', text: '{"packet": 1}' }] }],
      max_tokens: 1024,
      output_schema: { type: 'object', properties: { kind: { type: 'string' } }, required: ['kind'] },
      ...extra,
    },
  };
}

interface Streamed { status: number; json: Record<string, any>; events: Array<{ event: string; data: any }> }

export async function message(token: string, body: unknown, { key, path = '/v1/ai/messages' }:
  { key?: string; path?: string } = {}): Promise<Streamed> {
  const response = await SELF.fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`,
      'Idempotency-Key': key ?? `rkey-${++keys}-abcdef`, 'CF-Connecting-IP': '203.0.113.9' },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.headers.get('content-type')?.includes('text/event-stream')) {
    return { status: response.status, json: text ? JSON.parse(text) : {}, events: [] };
  }
  const events = text.split('\n\n').filter(Boolean).map((chunk) => ({
    event: /^event: (.*)$/m.exec(chunk)?.[1] ?? '',
    data: JSON.parse(/^data: (.*)$/m.exec(chunk)?.[1] ?? 'null'),
  }));
  return { status: response.status, json: {}, events };
}

export const usageOf = (s: Streamed) => s.events.find((e) => e.event === 'plexora.usage')?.data;
export const textOf = (s: Streamed) => s.events.filter((e) => e.event === 'content_block_delta' &&
  e.data.delta?.type === 'text_delta').map((e) => e.data.delta.text).join('');

export const row = (id: string) => env.LICENSE_DB.prepare('SELECT * FROM ai_requests WHERE id = ?1').bind(id)
  .first<Record<string, any>>();

