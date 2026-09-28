/** @jsxImportSource hono/jsx */
/**
 * The admin pages. Server-rendered from the same queries as the API; every
 * action is a data-action button or data-json form calling /admin/api.
 */
import { Hono } from 'hono';
import type { Child } from 'hono/jsx';

import type { EnvironmentRow, LicenseRow, SeatRow, TokenRow } from '../db';
import { accountById, all, licenseById, one } from '../db';
import { nowSeconds } from '../env';
import { ApiError, type AppEnv, page } from '../http';
import { date, Layout, StatusBadge } from '../ui/layout';
import { environmentView, licenseView, seatView, tokenView } from '../views';

export const adminPages = new Hono<AppEnv>();

const NAV: [string, string][] = [
  ['/admin', 'Licences'],
  ['/admin/issue', 'Issue'],
  ['/admin/signals', 'Signals'],
  ['/admin/stats', 'Stats'],
];

function shell(c: { get: (k: 'admin') => string }, title: string, active: string, body: Child) {
  return (
    <Layout title={title} product="admin" nav={NAV} active={active} who={c.get('admin')} logout="/admin/logout">
      {body}
    </Layout>
  ) as unknown as string;
}

adminPages.get('/', async (c) => {
  const q = (c.req.query('q') ?? '').trim().toLowerCase();
  const like = `%${q.replace(/[%_]/g, '')}%`;
  const rows = await all<LicenseRow & { account_name: string; owner_email: string | null; seats_used: number }>(c.env,
    `SELECT l.*, a.name AS account_name,
       (SELECT u.email FROM account_members m JOIN users u ON u.id = m.user_id
        WHERE m.account_id = l.account_id AND m.role = 'owner' AND m.status = 'active' LIMIT 1) AS owner_email,
       (SELECT COUNT(*) FROM seat_assignments s WHERE s.license_id = l.id AND s.status = 'active') AS seats_used
     FROM licenses l JOIN accounts a ON a.id = l.account_id
     WHERE (?1 = '' OR l.id LIKE ?2 OR lower(a.name) LIKE ?2 OR EXISTS (
       SELECT 1 FROM account_members m JOIN users u ON u.id = m.user_id
       WHERE m.account_id = l.account_id AND u.email_canonical LIKE ?2))
     ORDER BY l.created_at DESC LIMIT 200`, q, like);
  return page(c, shell(c, 'Licences', '/admin', (
    <>
      <form class="inline" method="get" action="/admin">
        <label>Search<input name="q" value={q} placeholder="licence id, account or email" /></label>
        <button type="submit">Search</button>
      </form>
      <div class="scroll">
        <table>
          <thead><tr><th>Licence</th><th>Account</th><th>Owner</th><th>Status</th><th>Use</th><th>Seats</th>
            <th>Expires</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr>
                <td><a href={`/admin/licenses/${r.id}`} class="mono">{r.id}</a>{r.is_trial ? ' (trial)' : ''}</td>
                <td>{r.account_name}</td>
                <td>{r.owner_email ?? '—'}</td>
                <td><StatusBadge status={r.status} /></td>
                <td>{r.use_class}</td>
                <td>{r.seats_used}/{r.seats}</td>
                <td>{date(r.expires_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )));
});

adminPages.get('/issue', (c) => page(c, shell(c, 'Issue a licence', '/admin/issue', (
  <>
    <p class="muted">Manual issuance: the only way a Paid licence is created until billing is connected. The owner
      gets the first seat and its key by email; the key is also shown here once.</p>
    <form class="stack" data-json action="/admin/api/licenses" data-reveal="seat.key,license.id">
      <label>Owner email<input type="email" name="owner_email" required /></label>
      <label>Account name<input name="account_name" placeholder="Lab or organization" /></label>
      <label>Account kind<select name="account_kind"><option value="individual">Individual</option>
        <option value="organization">Organization</option></select></label>
      <label>Use class<select name="use_class"><option>academic</option><option>commercial</option>
        <option>nonprofit</option><option>government</option></select></label>
      <label>Seats<input name="seats" data-num value="1" /></label>
      <label>Environments per seat<input name="envs_per_seat" data-num value="2" /></label>
      <label>Days<input name="days" data-num value="365" /></label>
      <label>Entitlements (comma separated)<input name="entitlements" data-list value="ai" /></label>
      <label>Grace days<input name="grace_days" data-num value="14" /></label>
      <label>Offline max days<input name="offline_max_days" data-num value="180" /></label>
      <label>Reference (PO, invoice)<input name="reference" /></label>
      <label>Notes<textarea name="notes"></textarea></label>
      <button class="primary" type="submit">Issue licence</button>
    </form>
  </>
))));

adminPages.get('/licenses/:id', async (c) => {
  const license = await licenseById(c.env, c.req.param('id'));
  if (!license) throw new ApiError(404, 'not_found', 'No such licence.');
  const [account, seatRows, envRows, tokenRows, grants, events] = await Promise.all([
    accountById(c.env, license.account_id),
    all<SeatRow & { email: string | null }>(c.env,
      `SELECT s.*, u.email FROM seat_assignments s LEFT JOIN users u ON u.id = s.user_id
       WHERE s.license_id = ?1 ORDER BY s.created_at`, license.id),
    all<EnvironmentRow>(c.env, 'SELECT * FROM environments WHERE license_id = ?1 ORDER BY created_at', license.id),
    all<TokenRow>(c.env, 'SELECT * FROM license_tokens WHERE license_id = ?1 ORDER BY created_at', license.id),
    all<{ id: string; environment_id: string; issued_at: number; offline_until: number }>(c.env,
      'SELECT id, environment_id, issued_at, offline_until FROM offline_grants WHERE license_id = ?1 ORDER BY issued_at DESC',
      license.id),
    all<{ at: number; actor: string; kind: string; payload: string | null }>(c.env,
      'SELECT at, actor, kind, payload FROM events WHERE license_id = ?1 ORDER BY at DESC, id DESC LIMIT 50',
      license.id),
  ]);
  const view = licenseView(license);
  const api = `/admin/api/licenses/${license.id}`;
  return page(c, shell(c, `Licence ${license.id}`, '/admin', (
    <>
      <div class="grid">
        <div class="card stat"><div class="k">Account</div><div class="v">{account?.name}</div>
          <div class="muted small">{account?.kind} · {account?.id}</div></div>
        <div class="card stat"><div class="k">Status</div><div class="v"><StatusBadge status={view.status} /></div>
          <div class="muted small">{view.trial ? 'trial · ' : ''}{view.use_class}</div></div>
        <div class="card stat"><div class="k">Expires</div><div class="v">{date(view.expires_at)}</div>
          <div class="muted small">grace {view.grace_days} days</div></div>
        <div class="card stat"><div class="k">Grants</div><div class="v mono">{view.entitlements.join(', ') || '(none)'}</div>
          <div class="muted small">{view.seats} seats · {view.envs_per_seat} envs/seat</div></div>
      </div>
      <h2>Change</h2>
      <form class="inline" data-json action={api} data-method="PATCH" data-reload>
        <label>Extend by days<input name="extend_days" data-num /></label>
        <label>Seats<input name="seats" data-num /></label>
        <label>Envs/seat<input name="envs_per_seat" data-num /></label>
        <label>Entitlements<input name="entitlements" data-list placeholder={view.entitlements.join(',')} /></label>
        <button type="submit">Save</button>
      </form>
      <p>
        {view.status === 'active' ? (
          <button type="button" data-action={api} data-method="PATCH" data-body='{"status":"suspended"}' data-reload>
            Suspend</button>
        ) : view.status === 'suspended' ? (
          <button type="button" data-action={api} data-method="PATCH" data-body='{"status":"active"}' data-reload>
            Reactivate</button>
        ) : null}{' '}
        {view.status !== 'revoked' ? (
          <button type="button" class="danger" data-action={`${api}/revoke`} data-reload
            data-confirm="Revoke this licence? Every environment drops to Free at its next check.">Revoke</button>
        ) : null}
      </p>
      <h2>Seats</h2>
      <form class="inline" data-json action={`${api}/seats`} data-reveal="key" data-reload>
        <label>Add a seat for (email, optional)<input type="email" name="email" /></label>
        <button type="submit">Add seat</button>
      </form>
      <div class="scroll"><table>
        <thead><tr><th>Seat</th><th>Holder</th><th>Status</th><th>Key</th><th></th></tr></thead>
        <tbody>{seatRows.map(seatView).map((s) => (
          <tr><td class="mono">{s.id}</td><td>{s.email ?? '(unassigned)'}</td><td><StatusBadge status={s.status} /></td>
            <td class="mono">{s.key_hint}</td>
            <td class="actions">{s.status === 'active' ? (<>
              <button type="button" data-action={`/admin/api/seats/${s.id}/rotate-key`} data-reveal="key"
                data-confirm="Issue a new key? The old one stops working.">New key</button>{' '}
              <button type="button" data-action={`/admin/api/seats/${s.id}/release`} data-reload
                data-confirm="Release this seat and its environments?">Release</button>
            </>) : null}</td></tr>
        ))}</tbody>
      </table></div>
      <h2>Environments</h2>
      <div class="scroll"><table>
        <thead><tr><th>Name</th><th>Kind</th><th>Seat</th><th>Status</th><th>Last seen</th><th></th></tr></thead>
        <tbody>{envRows.map(environmentView).map((e) => (
          <tr><td>{e.name}<div class="muted small mono">{e.id}</div></td><td>{e.kind}{e.registration === 'offline' ? ' (offline)' : ''}</td>
            <td class="mono small">{e.seat_id}</td><td><StatusBadge status={e.status} /></td><td>{date(e.last_seen)}</td>
            <td class="actions">{e.status === 'active' ? (<>
              <button type="button" data-action={`/admin/api/environments/${e.id}/release`} data-reload>Release</button>{' '}
              <button type="button" class="danger" data-action={`/admin/api/environments/${e.id}/revoke`} data-reload
                data-confirm="Revoke this environment?">Revoke</button></>) : null}</td></tr>
        ))}</tbody>
      </table></div>
      <h2>Licence tokens</h2>
      <div class="scroll"><table>
        <thead><tr><th>Label</th><th>Scope</th><th>Expires</th><th>Last used</th><th>Uses</th><th></th></tr></thead>
        <tbody>{tokenRows.map(tokenView).map((t) => (
          <tr><td>{t.label}<div class="muted small mono">{t.hint}</div></td><td>{t.scope}</td><td>{date(t.expires_at)}</td>
            <td>{date(t.last_used_at)}</td><td>{t.use_count}</td>
            <td>{t.revoked_at ? 'revoked' : (
              <button type="button" class="danger" data-action={`/admin/api/tokens/${t.id}/revoke`} data-reload>Revoke</button>
            )}</td></tr>
        ))}</tbody>
      </table></div>
      <h2>Offline grants</h2>
      <p class="muted small">An offline certificate cannot be recalled before it runs out.</p>
      <div class="scroll"><table>
        <thead><tr><th>Grant</th><th>Environment</th><th>Issued</th><th>Valid until</th></tr></thead>
        <tbody>{grants.map((g) => (
          <tr><td class="mono">{g.id}</td><td class="mono">{g.environment_id}</td><td>{date(g.issued_at)}</td>
            <td>{date(g.offline_until)}</td></tr>
        ))}</tbody>
      </table></div>
      <h2>History</h2>
      <div class="scroll"><table>
        <thead><tr><th>When</th><th>Who</th><th>What</th></tr></thead>
        <tbody>{events.map((e) => (
          <tr><td>{new Date(e.at * 1000).toISOString().slice(0, 16).replace('T', ' ')}</td><td>{e.actor}</td>
            <td>{e.kind}</td></tr>
        ))}</tbody>
      </table></div>
    </>
  )));
});

adminPages.get('/signals', async (c) => {
  const rows = await all<{ id: number; kind: string; subject: string; detail: string; created_at: number }>(c.env,
    'SELECT id, kind, subject, detail, created_at FROM signals WHERE acked_at IS NULL ORDER BY created_at DESC LIMIT 200');
  return page(c, shell(c, 'Signals', '/admin/signals', (
    <>
      <p class="muted">For review only. Nothing here blocks, bans or revokes anything automatically, and most signals
        have innocent explanations -- a reinstall, a new laptop, a cluster behind a NAT pool.</p>
      <div class="scroll"><table>
        <thead><tr><th>When</th><th>Kind</th><th>Subject</th><th>Detail</th><th></th></tr></thead>
        <tbody>{rows.map((s) => (
          <tr><td>{date(s.created_at)}</td><td>{s.kind}</td><td class="mono small">{s.subject}</td>
            <td class="mono small">{s.detail}</td>
            <td><button type="button" data-action={`/admin/api/signals/${s.id}/ack`} data-reload>Reviewed</button></td></tr>
        ))}</tbody>
      </table></div>
    </>
  )));
});

adminPages.get('/stats', async (c) => {
  const now = nowSeconds();
  const [licenses, environments, trials, signals] = await Promise.all([
    all<{ status: string; is_trial: number; n: number }>(c.env,
      'SELECT status, is_trial, COUNT(*) AS n FROM licenses GROUP BY status, is_trial ORDER BY status'),
    all<{ kind: string; n: number }>(c.env,
      `SELECT kind, COUNT(*) AS n FROM environments WHERE status = 'active' GROUP BY kind`),
    one<{ n: number }>(c.env, 'SELECT COUNT(*) AS n FROM trials WHERE created_at > ?1', now - 30 * 86400),
    one<{ n: number }>(c.env, 'SELECT COUNT(*) AS n FROM signals WHERE acked_at IS NULL'),
  ]);
  return page(c, shell(c, 'Stats', '/admin/stats', (
    <>
      <div class="grid">
        <div class="card stat"><div class="k">Trials, 30 days</div><div class="v">{trials?.n ?? 0}</div></div>
        <div class="card stat"><div class="k">Open signals</div><div class="v">{signals?.n ?? 0}</div></div>
        {environments.map((e) => (
          <div class="card stat"><div class="k">Active {e.kind} environments</div><div class="v">{e.n}</div></div>
        ))}
      </div>
      <h2>Licences</h2>
      <table><thead><tr><th>Status</th><th>Kind</th><th>Count</th></tr></thead>
        <tbody>{licenses.map((l) => (
          <tr><td>{l.status}</td><td>{l.is_trial ? 'trial' : 'paid'}</td><td>{l.n}</td></tr>
        ))}</tbody></table>
      <h2>Maintenance</h2>
      <button type="button" data-action="/admin/api/cron" data-done="Maintenance ran.">Run daily maintenance now</button>
    </>
  )));
});
