import { SELF } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';

import { outbox, resetOutbox } from '../../src/email';
import { activate, admin, BASE, call, environmentBody, issue, pubkey } from './helpers';

const ADMIN = { Authorization: 'Bearer test-admin', Accept: 'text/html' };

async function html(path: string, headers: Record<string, string> = ADMIN) {
  const response = await SELF.fetch(`${BASE}${path}`, { headers, redirect: 'manual' });
  return { status: response.status, text: await response.text(), csp: response.headers.get('Content-Security-Policy') };
}

async function signIn(email: string): Promise<string> {
  resetOutbox();
  await call('POST', '/portal/login', { email });
  const secret = /t=([\w-]+)/.exec(outbox.at(-1)!.text)![1]!;
  const response = await SELF.fetch(`${BASE}/portal/auth`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ t: secret }) });
  return response.headers.get('Set-Cookie')!.split(';')[0]!;
}

/** The CSP pins one stylesheet by hash, so an inline style="" would be ignored. */
function pageIsSound(page: { status: number; text: string; csp: string | null }, path: string) {
  expect(page.status, path).toBe(200);
  expect(page.csp, path).toContain("style-src 'sha256-");
  expect(page.text, path).not.toMatch(/\sstyle="/);
  expect(page.text.match(/<script/g), path).toHaveLength(1);
}

describe('admin pages', () => {
  it('every page renders, sound, and never shows a seat key', async () => {
    const issued = await issue({ owner_email: 'pages@lab.example.org', account_name: 'Pages Lab' });
    await activate(issued.seat.key, environmentBody('desktop', { display_name: 'Bench iMac' }));
    const id = issued.license.id as string;
    for (const path of ['/admin', '/admin/licenses', '/admin/licenses?q=pages&status=active&kind=paid&ending=400',
      `/admin/licenses/${id}`, '/admin/issue', '/admin/signals', '/admin/signals?all', '/admin/events',
      `/admin/events?license=${id}&kind=license.`]) {
      const page = await html(path);
      pageIsSound(page, path);
      expect(page.text, path).not.toContain(issued.seat.key);
    }
  });

  it('the licence page shows the licence, its seat and its environment', async () => {
    const issued = await issue({ owner_email: 'detail@lab.example.org', account_name: 'Detail Lab' });
    await activate(issued.seat.key, environmentBody('cluster', { display_name: 'HMS O2', delegation_pubkey: pubkey() }));
    const page = await html(`/admin/licenses/${issued.license.id}`);
    expect(page.text).toContain('Detail Lab');
    expect(page.text).toContain('detail@lab.example.org');
    expect(page.text).toContain('HMS O2');
    expect(page.text).toContain('HPC cluster');
    expect(page.text).toContain('Overrides');
  });

  it('the list filters, and the dashboard counts', async () => {
    await issue({ owner_email: 'filter-a@lab.example.org', account_name: 'Filter Alpha' });
    const list = await html('/admin/licenses?q=filter-a');
    expect(list.text).toContain('Filter Alpha');
    expect((await html('/admin/licenses?q=nobody-matches-this')).text).toContain('Nothing matched');
    expect((await html('/admin')).text).toContain('Paid licences');
  });

  it('a revoked licence says so and offers no revoke', async () => {
    const issued = await issue({ owner_email: 'gone@lab.example.org' });
    await admin('POST', `/licenses/${issued.license.id}/revoke`, { reason: 'test' });
    const page = await html(`/admin/licenses/${issued.license.id}`);
    expect(page.text).toContain('Revoked');
    expect(page.text).not.toContain('Revoke licence');
  });

  it('the old stats page is the dashboard', async () => {
    const page = await SELF.fetch(`${BASE}/admin/stats`, { headers: ADMIN, redirect: 'manual' });
    expect(page.status).toBe(302);
    expect(page.headers.get('Location')).toBe('/admin');
  });

  it('the sign-in page renders without a session', async () => {
    const page = await html('/admin/login', { Accept: 'text/html' });
    pageIsSound(page, '/admin/login');
    expect(page.text).toContain('ADMIN_TOKEN');
  });
});

describe('portal pages', () => {
  it('every page renders, sound', async () => {
    await issue({ owner_email: 'portal-pages@lab.example.org' });
    const cookie = await signIn('portal-pages@lab.example.org');
    for (const path of ['/portal', '/portal/seats', '/portal/environments', '/portal/offline', '/portal/tokens',
      '/portal/billing']) {
      pageIsSound(await html(path, { Cookie: cookie }), path);
    }
    for (const path of ['/portal/login', '/portal/trial', `/portal/trial?fp=${'b'.repeat(64)}`, '/portal/auth?t=x',
      '/portal/invite?t=x']) {
      pageIsSound(await html(path, {}), path);
    }
  });
});
