/** @jsxImportSource hono/jsx */
/**
 * /portal: where licence holders manage what they have.
 *
 *   Overview                 licences, plan, expiry, what Paid unlocks
 *   Seats & Users            invite, resend, revoke; release; new key   (owner/admin)
 *   Devices & Environments   rename, remove (cooldown; owners override)
 *   Offline Licence          upload a fingerprint report, download a .plexora
 *   Licence Tokens           mint (shown once), last used, revoke
 *   Billing & Renewal        dates and how to renew (manual until billing exists)
 *   Trial                    the page Plexora's "Start trial" button opens
 *
 * Environments are shown by the name their owner gave them. Nothing about a
 * machine beyond that name, its kind and when it was last seen is shown,
 * because nothing more is stored.
 */
import { Hono } from 'hono';
import type { Child } from 'hono/jsx';

import { canonicalEmail, memberStatement, validEmail } from '../accounts';
import { newId, randomToken, sha256Hex } from '../crypto';
import type { EnvironmentRow, TokenRow } from '../db';
import { all, one } from '../db';
import * as mail from '../email';
import { baseUrl, DAY, knob, nowSeconds } from '../env';
import { cleanName, release } from '../environments';
import { eventStatement, record } from '../events';
import { ApiError, type AppEnv, int, ok, page, readJson, sameOriginGuard, str } from '../http';
import { issueOffline } from '../offline';
import { environmentAccess, licenseAccess, licensesFor, seatAccess } from '../portalAccess';
import { enforce } from '../ratelimit';
import * as seats from '../seats';
import { acceptInvitation, endSession, requireUser, sendLoginLink, sessionUser, spendLoginLink, startSession } from '../sessions';
import * as tokens from '../tokens';
import { date, Layout, StatusBadge } from '../ui/layout';
import { environmentView, tokenView } from '../views';
import { ipHash } from '../crypto';

export const portal = new Hono<AppEnv>();

portal.use('*', sameOriginGuard);

const NAV: [string, string][] = [
  ['/portal', 'Overview'],
  ['/portal/seats', 'Seats & Users'],
  ['/portal/environments', 'Devices & Environments'],
  ['/portal/offline', 'Offline Licence'],
  ['/portal/tokens', 'Licence Tokens'],
  ['/portal/billing', 'Billing & Renewal'],
];

async function shell(c: Parameters<typeof sessionUser>[0], title: string, active: string, body: Child) {
  const user = await one<{ email: string }>(c.env, 'SELECT email FROM users WHERE id = ?1', c.get('userId'));
  return page(c, (
    <Layout title={title} product="portal" nav={NAV} active={active} who={user?.email ?? null} logout="/portal/logout">
      {body}
    </Layout>
  ) as unknown as string);
}

// -- signing in -------------------------------------------------------------------

portal.get('/login', async (c) => {
  if (await sessionUser(c)) return c.redirect('/portal');
  return page(c, (
    <Layout title="Sign in" product="portal">
      <div class="login card">
        <p>Enter the email address your Plexora licence or seat was sent to. We will email you a sign-in link.</p>
        <form data-json action="/portal/login" data-done="If that address has a Plexora licence, a sign-in link is on its way.">
          <label>Email<input type="email" name="email" autocomplete="email" required /></label>
          <button class="primary" type="submit">Email me a link</button>
        </form>
        <p class="muted small">No licence yet? Plexora Free needs none. <a href="/portal/trial">Start a Paid trial</a>.</p>
      </div>
    </Layout>
  ) as unknown as string);
});

portal.post('/login', async (c) => {
  const now = nowSeconds();
  await enforce(c.env, 'login-ip', await ipHash(c.env, c.req.raw), knob(c.env, 'LOGIN_PER_IP_PER_HOUR'), 3600, now);
  const email = validEmail((await readJson(c)).email);
  if (!email) throw new ApiError(400, 'invalid_request', 'Give a valid email address.');
  await sendLoginLink(c.env, email, now);
  return ok(c, { ok: true, message: 'If that address has a Plexora licence, a sign-in link is on its way.' });
});

portal.get('/auth', (c) => {
  const secret = c.req.query('t') ?? '';
  return page(c, (
    <Layout title="Sign in" product="portal">
      <div class="login card">
        <form data-json action="/portal/auth" data-next="/portal">
          <input type="hidden" name="t" value={secret} />
          <button class="primary" type="submit">Continue to the licence portal</button>
        </form>
      </div>
    </Layout>
  ) as unknown as string);
});

portal.post('/auth', async (c) => {
  const now = nowSeconds();
  const secret = str(await readJson(c), 't', 128);
  if (!secret) throw new ApiError(400, 'invalid_request', 'Missing sign-in link.');
  const user = await spendLoginLink(c.env, secret, now);
  await startSession(c, user, now);
  return ok(c, { ok: true });
});

portal.post('/logout', async (c) => {
  await endSession(c);
  return ok(c, { ok: true });
});

portal.get('/invite', (c) => page(c, (
  <Layout title="Accept your Plexora seat" product="portal">
    <div class="login card">
      <p>You have been given a Plexora Paid seat. Accepting it emails you its key and signs you in here.</p>
      <form data-json action="/portal/invite" data-next="/portal">
        <input type="hidden" name="t" value={c.req.query('t') ?? ''} />
        <button class="primary" type="submit">Accept</button>
      </form>
    </div>
  </Layout>
) as unknown as string));

portal.post('/invite', async (c) => {
  const now = nowSeconds();
  const secret = str(await readJson(c), 't', 128);
  if (!secret) throw new ApiError(400, 'invalid_request', 'Missing invitation.');
  const accepted = await acceptInvitation(c.env, secret, now);
  await c.env.LICENSE_DB.batch([
    memberStatement(c.env, accepted.account_id, accepted.user.id, accepted.role, now),
    eventStatement(c.env, now, { actor: `portal:${accepted.user.id}`, kind: 'invitation.accepted',
      account_id: accepted.account_id, license_id: accepted.license_id }),
  ]);
  if (accepted.license_id) {
    const license = await one<import('../db').LicenseRow>(c.env, 'SELECT * FROM licenses WHERE id = ?1',
      accepted.license_id);
    const existing = await one<{ id: string }>(c.env,
      `SELECT id FROM seat_assignments WHERE license_id = ?1 AND user_id = ?2 AND status = 'active'`,
      accepted.license_id, accepted.user.id);
    if (license && !existing) {
      const { key } = await seats.create(c.env, license, accepted.user.id, now, `portal:${accepted.user.id}`);
      await mail.send(c.env, mail.seatKeyMail(accepted.user.email, key, { expires_at: license.expires_at,
        portal: `${baseUrl(c.env)}/portal` }));
    }
  }
  await startSession(c, accepted.user, now);
  return ok(c, { ok: true });
});

// -- the trial page (no sign-in: it is how people arrive) -------------------------

portal.get('/trial', (c) => {
  const fp = (c.req.query('fp') ?? '').toLowerCase();
  const fromPlexora = /^[0-9a-f]{64}$/.test(fp);
  return page(c, (
    <Layout title="Try Plexora Paid for 30 days" product="portal">
      <div class="login card">
        <p>Paid unlocks Plexora's AI features: guided gating sessions, the evidence an agent gathers, and what comes
          next. Everything Free stays free, and everything you make during the trial stays yours afterwards.</p>
        {fromPlexora ? (
          <form data-json action="/v1/trial/start" data-done="Check your email for your trial key.">
            <input type="hidden" name="fingerprint" value={fp} />
            <label>Email<input type="email" name="email" autocomplete="email" required /></label>
            <button class="primary" type="submit">Send me a trial key</button>
          </form>
        ) : (
          <div class="notice">Open this page from Plexora (Settings &gt; License &gt; Start trial), or run
            <code> plexora license trial --email you@example.org</code>. A trial is one per person and per machine,
            and Plexora includes a hashed machine fingerprint to check that; nothing else about the machine is sent.</div>
        )}
      </div>
    </Layout>
  ) as unknown as string);
});

// -- signed-in pages ------------------------------------------------------------------

portal.use('/api/*', requireUser);
for (const path of ['/', '/seats', '/environments', '/offline', '/tokens', '/billing']) portal.use(path, requireUser);

portal.get('/', async (c) => {
  const access = await licensesFor(c.env, c.get('userId'));
  const now = nowSeconds();
  return shell(c, 'Overview', '/portal', (
    <>
      {access.length === 0 ? (
        <p>You have no Plexora licence on this address. Plexora Free needs none; <a href="/portal/trial">try Paid</a>.</p>
      ) : null}
      <div class="grid">
        {access.map((a) => (
          <div class="card">
            <div class="muted small">{a.account_name} · {a.role}</div>
            <p><strong>Plexora Paid{a.license.is_trial ? ' (trial)' : ''}</strong> <StatusBadge status={
              a.license.expires_at <= now && a.license.status === 'active' ? 'expired' : a.license.status} /></p>
            <p class="small">Valid until {date(a.license.expires_at)}
              {a.license.is_trial ? '' : ` · grace ${a.license.grace_days} days`} · {a.license.use_class}</p>
            <p class="small">Your seat: {a.seat ? <code>PLEX-****-****-****-{a.seat.seat_key_hint}</code> : 'none'}</p>
            {a.seat && a.seat.seat_key_vault ? (
              <button type="button" data-action={`/portal/api/seats/${a.seat.id}/reveal-key`} data-reveal="key">
                Show my seat key</button>
            ) : null}{' '}
            {a.seat ? (
              <button type="button" data-action={`/portal/api/seats/${a.seat.id}/rotate-key`} data-reveal="key"
                data-confirm="Issue a new seat key? The old key stops working; activated environments keep working.">
                New seat key</button>
            ) : null}
          </div>
        ))}
      </div>
      <h2>Using your seat</h2>
      <p>Activate in Plexora under Settings &gt; License, or run <code>plexora license activate &lt;key&gt;</code>.
        For an HPC cluster, run it once on a login node with <code>--cluster</code>: every node, job, notebook and
        container sharing that home directory then uses one registration.</p>
    </>
  ));
});

portal.get('/seats', async (c) => {
  const managed = (await licensesFor(c.env, c.get('userId'))).filter((a) => a.manage);
  const sections = await Promise.all(managed.map(async (a) => ({
    a,
    seats: await all<{ id: string; status: string; email: string | null; seat_key_hint: string; created_at: number }>(c.env,
      `SELECT s.id, s.status, u.email, s.seat_key_hint, s.created_at FROM seat_assignments s
       LEFT JOIN users u ON u.id = s.user_id WHERE s.license_id = ?1 AND s.status = 'active' ORDER BY s.created_at`,
      a.license.id),
    invitations: await all<{ id: string; email_canonical: string; expires_at: number }>(c.env,
      `SELECT id, email_canonical, expires_at FROM invitations WHERE license_id = ?1 AND accepted_at IS NULL
         AND revoked_at IS NULL AND expires_at > ?2 ORDER BY created_at`, a.license.id, nowSeconds()),
  })));
  return shell(c, 'Seats & Users', '/portal/seats', (
    <>
      {managed.length === 0 ? <p>Only an account owner or admin manages seats.</p> : null}
      {sections.map(({ a, seats: seatRows, invitations }) => (
        <>
          <h2>{a.account_name} · {a.license.id} · {seatRows.length}/{a.license.seats} seats in use</h2>
          <form class="inline" data-json action={`/portal/api/licenses/${a.license.id}/invite`} data-reload
            data-done="Invitation sent.">
            <label>Invite by email<input type="email" name="email" required /></label>
            <label>Role<select name="role"><option value="member">Member</option><option value="admin">Admin</option></select></label>
            <button type="submit">Invite</button>
          </form>
          <div class="scroll"><table>
            <thead><tr><th>Holder</th><th>Key</th><th>Since</th><th></th></tr></thead>
            <tbody>
              {seatRows.map((s) => (
                <tr><td>{s.email ?? '(unassigned)'}</td><td class="mono">…{s.seat_key_hint}</td><td>{date(s.created_at)}</td>
                  <td class="actions">
                    <button type="button" data-action={`/portal/api/seats/${s.id}/rotate-key`} data-reveal="key"
                      data-confirm="Issue a new key for this seat?">New key</button>{' '}
                    <button type="button" class="danger" data-action={`/portal/api/seats/${s.id}/release`} data-reload
                      data-confirm="Release this seat? Its environments drop to Free at their next check.">Release</button>
                  </td></tr>
              ))}
              {invitations.map((i) => (
                <tr><td>{i.email_canonical} <span class="badge">invited</span></td><td></td><td>until {date(i.expires_at)}</td>
                  <td class="actions">
                    <button type="button" data-action={`/portal/api/invitations/${i.id}/resend`} data-done="Sent again.">Resend</button>{' '}
                    <button type="button" data-action={`/portal/api/invitations/${i.id}/revoke`} data-reload>Revoke</button>
                  </td></tr>
              ))}
            </tbody>
          </table></div>
        </>
      ))}
    </>
  ));
});

async function visibleEnvironments(c: { env: AppEnv['Bindings'] }, userId: string) {
  const access = await licensesFor(c.env, userId);
  const out: { account: string; license_id: string; rows: (EnvironmentRow & { email: string | null })[]; manage: boolean }[] = [];
  for (const a of access) {
    const rows = await all<EnvironmentRow & { email: string | null }>(c.env,
      `SELECT e.*, u.email FROM environments e JOIN seat_assignments s ON s.id = e.seat_id
       LEFT JOIN users u ON u.id = s.user_id
       WHERE e.license_id = ?1 AND e.status = 'active' AND (?2 = 1 OR s.user_id = ?3) ORDER BY e.created_at`,
      a.license.id, a.manage ? 1 : 0, userId);
    out.push({ account: a.account_name, license_id: a.license.id, rows, manage: a.manage });
  }
  return out;
}

portal.get('/environments', async (c) => {
  const groups = await visibleEnvironments(c, c.get('userId'));
  return shell(c, 'Devices & Environments', '/portal/environments', (
    <>
      <p class="muted">An environment is a computer, or a whole HPC cluster. Removing one frees its slot on the seat;
        to stop a seat being rotated across many machines, a seat can remove one environment every
        {' '}{knob(c.env, 'COOLDOWN_HOURS')} hours (owners and admins are exempt, and so is an environment unseen for
        {' '}{knob(c.env, 'STALE_ENV_EXEMPT_DAYS')} days).</p>
      {groups.map((g) => (
        <>
          <h2>{g.account} · {g.license_id}</h2>
          <div class="scroll"><table>
            <thead><tr><th>Name</th><th>Kind</th>{g.manage ? <th>Seat holder</th> : null}<th>Registered</th><th>Last seen</th><th></th></tr></thead>
            <tbody>{g.rows.map((row) => {
              const e = environmentView(row);
              return (
                <tr><td>
                  <form class="inline" data-json action={`/portal/api/environments/${e.id}/rename`} data-done="Renamed.">
                    <input name="name" value={e.name} aria-label="Name" /><button type="submit">Rename</button>
                  </form></td>
                  <td>{e.kind}{e.registration === 'offline' ? ' (offline)' : ''}</td>
                  {g.manage ? <td>{row.email ?? '—'}</td> : null}
                  <td>{date(e.created_at)}</td><td>{date(e.last_seen)}</td>
                  <td><button type="button" class="danger" data-action={`/portal/api/environments/${e.id}/remove`} data-reload
                    data-confirm="Remove this environment? Paid features there stop at its next check.">Remove</button></td></tr>
              );
            })}</tbody>
          </table></div>
        </>
      ))}
    </>
  ));
});

portal.get('/offline', async (c) => {
  const userId = c.get('userId');
  const access = await licensesFor(c.env, userId);
  const seatOptions: { id: string; label: string }[] = [];
  for (const a of access) {
    if (a.license.offline_allowed !== 1) continue;
    if (a.manage) {
      const rows = await all<{ id: string; email: string | null }>(c.env,
        `SELECT s.id, u.email FROM seat_assignments s LEFT JOIN users u ON u.id = s.user_id
         WHERE s.license_id = ?1 AND s.status = 'active'`, a.license.id);
      rows.forEach((r) => seatOptions.push({ id: r.id, label: `${a.account_name}: ${r.email ?? 'unassigned seat'}` }));
    } else if (a.seat) {
      seatOptions.push({ id: a.seat.id, label: `${a.account_name}: your seat` });
    }
  }
  const grants = await all<{ id: string; name: string; issued_at: number; offline_until: number }>(c.env,
    `SELECT g.id, e.display_name AS name, g.issued_at, g.offline_until FROM offline_grants g
     JOIN environments e ON e.id = g.environment_id JOIN seat_assignments s ON s.id = g.seat_id
     WHERE g.license_id IN (SELECT l.id FROM licenses l JOIN account_members m ON m.account_id = l.account_id
       WHERE m.user_id = ?1 AND m.status = 'active')
       AND (s.user_id = ?1 OR EXISTS (SELECT 1 FROM account_members m2 WHERE m2.account_id = s.account_id
         AND m2.user_id = ?1 AND m2.role IN ('owner', 'admin') AND m2.status = 'active'))
     ORDER BY g.issued_at DESC LIMIT 100`, userId);
  return shell(c, 'Offline Licence', '/portal/offline', (
    <>
      <p>For a computer or cluster that cannot reach the internet. On it, run
        <code> plexora license fingerprint --out fp.json</code> (add <code>--cluster</code> on an HPC login node),
        upload that file here, and install the licence file you get with <code>plexora license install</code>.</p>
      <div class="notice">An offline licence <strong>cannot be recalled</strong> before its end date, even if the
        seat is released. Issue one only for a machine that really is offline, and keep the file private.</div>
      {seatOptions.length === 0 ? <p class="muted">None of your licences include offline licences.</p> : (
        <form class="stack" data-json action="/portal/api/offline" data-download data-done="Downloaded.">
          <label>Seat<select name="seat_id">{seatOptions.map((s) => <option value={s.id}>{s.label}</option>)}</select></label>
          <label>Fingerprint report (fp.json)<input type="file" name="report" data-file-json accept=".json,application/json" required /></label>
          <label>Days (up to your licence's limit)<input name="days" data-num value={String(knob(c.env, 'OFFLINE_DEFAULT_DAYS'))} /></label>
          <button class="primary" type="submit">Create offline licence</button>
        </form>
      )}
      <h2>Issued offline licences</h2>
      <div class="scroll"><table>
        <thead><tr><th>Environment</th><th>Issued</th><th>Valid until</th></tr></thead>
        <tbody>{grants.map((g) => <tr><td>{g.name}</td><td>{date(g.issued_at)}</td><td>{date(g.offline_until)}</td></tr>)}</tbody>
      </table></div>
    </>
  ));
});

portal.get('/tokens', async (c) => {
  const userId = c.get('userId');
  const access = await licensesFor(c.env, userId);
  const seatOptions: { id: string; label: string }[] = [];
  const rows: (TokenRow & { email: string | null })[] = [];
  for (const a of access) {
    const seatsHere = await all<{ id: string; email: string | null }>(c.env,
      `SELECT s.id, u.email FROM seat_assignments s LEFT JOIN users u ON u.id = s.user_id
       WHERE s.license_id = ?1 AND s.status = 'active' AND (?2 = 1 OR s.user_id = ?3)`,
      a.license.id, a.manage ? 1 : 0, userId);
    seatsHere.forEach((s) => seatOptions.push({ id: s.id, label: `${a.account_name}: ${s.email ?? 'unassigned seat'}` }));
    for (const s of seatsHere) {
      rows.push(...(await all<TokenRow>(c.env,
        'SELECT * FROM license_tokens WHERE seat_id = ?1 ORDER BY created_at DESC', s.id)).map((t) => ({ ...t, email: s.email })));
    }
  }
  return shell(c, 'Licence Tokens', '/portal/tokens', (
    <>
      <p>A licence token lets automation activate Plexora without a person: set <code>PLEXORA_LICENSE_TOKEN</code>
        where it runs. <strong>hpc</strong> tokens register a cluster once; <strong>ci</strong> tokens never register
        anything and get a {knob(c.env, 'CI_CERT_HOURS')}-hour certificate per use. A token is shown once.</p>
      {seatOptions.length ? (
        <form class="inline" data-json action="/portal/api/tokens" data-reveal="token" data-reload>
          <label>Seat<select name="seat_id">{seatOptions.map((s) => <option value={s.id}>{s.label}</option>)}</select></label>
          <label>Scope<select name="scope"><option>interactive</option><option>hpc</option><option>automation</option>
            <option>ci</option></select></label>
          <label>Label<input name="label" placeholder="e.g. O2 cluster jobs" required /></label>
          <label>Days<input name="ttl_days" data-num value={String(knob(c.env, 'TOKEN_DEFAULT_TTL_DAYS'))} /></label>
          <button class="primary" type="submit">Create token</button>
        </form>
      ) : null}
      <div class="scroll"><table>
        <thead><tr><th>Label</th><th>Scope</th><th>Seat</th><th>Expires</th><th>Last used</th><th>Uses</th><th></th></tr></thead>
        <tbody>{rows.map((row) => {
          const t = tokenView(row);
          return (
            <tr><td>{t.label}<div class="muted small mono">{t.hint}</div></td><td>{t.scope}</td><td>{row.email ?? '—'}</td>
              <td>{date(t.expires_at)}</td><td>{date(t.last_used_at)}</td><td>{t.use_count}</td>
              <td>{t.revoked_at ? <span class="badge bad">revoked</span> : (
                <button type="button" class="danger" data-action={`/portal/api/tokens/${t.id}/revoke`} data-reload
                  data-confirm="Revoke this token? Anything using it can no longer activate.">Revoke</button>)}</td></tr>
          );
        })}</tbody>
      </table></div>
    </>
  ));
});

portal.get('/billing', async (c) => {
  const access = (await licensesFor(c.env, c.get('userId'))).filter((a) => a.manage);
  const support = c.env.SUPPORT_EMAIL ?? 'support@plexora.example';
  return shell(c, 'Billing & Renewal', '/portal/billing', (
    <>
      <p>Licences are issued and renewed by hand for now. To renew, add seats or change your plan, email
        {' '}<a href={`mailto:${support}`}>{support}</a> with your licence id.</p>
      <p class="muted small">Plexora's licence covers Plexora. AI model providers your agent uses bill you separately.</p>
      <div class="scroll"><table>
        <thead><tr><th>Licence</th><th>Account</th><th>Kind</th><th>Valid until</th><th>Renewal</th></tr></thead>
        <tbody>{access.map((a) => (
          <tr><td class="mono">{a.license.id}</td><td>{a.account_name}</td><td>{a.license.is_trial ? 'trial' : 'paid'}</td>
            <td>{date(a.license.expires_at)}</td><td>{a.license.renewal_state}</td></tr>
        ))}</tbody>
      </table></div>
    </>
  ));
});

// -- the portal API -------------------------------------------------------------------

const actor = (c: { get: (k: 'userId') => string }) => `portal:${c.get('userId')}`;

portal.post('/api/environments/:id/rename', async (c) => {
  const { environment } = await environmentAccess(c.env, c.get('userId'), c.req.param('id'));
  const name = cleanName((await readJson(c)).name, environment.display_name);
  await c.env.LICENSE_DB.prepare('UPDATE environments SET display_name = ?2 WHERE id = ?1').bind(environment.id, name).run();
  return ok(c, { name });
});

portal.post('/api/environments/:id/remove', async (c) => {
  const now = nowSeconds();
  const { environment, seat, manage } = await environmentAccess(c.env, c.get('userId'), c.req.param('id'));
  const result = await release(c.env, environment, seat, { now, reason: 'portal', actor: actor(c), override: manage });
  return ok(c, result);
});

portal.post('/api/seats/:id/reveal-key', async (c) => {
  const { seat, own } = await seatAccess(c.env, c.get('userId'), c.req.param('id'));
  if (!own) throw new ApiError(403, 'forbidden', 'Only the seat holder can see its key.');
  const key = await seats.revealKey(c.env, seat);
  if (!key) throw new ApiError(404, 'not_found', 'This key cannot be shown again; issue a new one.');
  return ok(c, { key });
});

portal.post('/api/seats/:id/rotate-key', async (c) => {
  const now = nowSeconds();
  const { seat } = await seatAccess(c.env, c.get('userId'), c.req.param('id'));
  if (seat.status !== 'active') throw new ApiError(409, 'conflict', 'That seat is not active.');
  const key = await seats.rotateKey(c.env, seat, now, actor(c));
  return ok(c, { key });
});

portal.post('/api/seats/:id/release', async (c) => {
  const { seat, manage } = await seatAccess(c.env, c.get('userId'), c.req.param('id'));
  if (!manage) throw new ApiError(403, 'forbidden', 'Only an owner or admin releases seats.');
  await seats.end(c.env, seat, 'released', nowSeconds(), actor(c));
  return ok(c, { released: true });
});

portal.post('/api/licenses/:id/invite', async (c) => {
  const now = nowSeconds();
  const access = await licenseAccess(c.env, c.get('userId'), c.req.param('id'));
  if (!access.manage) throw new ApiError(403, 'forbidden', 'Only an owner or admin invites.');
  const body = await readJson(c);
  const email = validEmail(body.email);
  if (!email) throw new ApiError(400, 'invalid_request', 'Give a valid email address.');
  const role = body.role === 'admin' ? 'admin' : 'member';
  const secret = randomToken(32);
  const id = newId('inv');
  await c.env.LICENSE_DB.batch([
    c.env.LICENSE_DB.prepare(
      `INSERT INTO invitations (id, account_id, license_id, email_canonical, role, token_hash, invited_by, created_at,
         expires_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)`,
    ).bind(id, access.license.account_id, access.license.id, canonicalEmail(email), role, await sha256Hex(secret),
      c.get('userId'), now, now + knob(c.env, 'INVITE_DAYS') * DAY),
    eventStatement(c.env, now, { actor: actor(c), kind: 'invitation.sent', account_id: access.license.account_id,
      license_id: access.license.id, payload: { role } }),
  ]);
  await mail.send(c.env, mail.inviteMail(email, `${baseUrl(c.env)}/portal/invite?t=${secret}`, access.account_name));
  return ok(c, { id }, 201);
});

async function invitationFor(c: { env: AppEnv['Bindings']; get: (k: 'userId') => string }, id: string) {
  const invite = await one<{ id: string; license_id: string; email_canonical: string; account_id: string }>(c.env,
    'SELECT id, license_id, email_canonical, account_id FROM invitations WHERE id = ?1', id);
  if (!invite) throw new ApiError(404, 'not_found', 'No such invitation.');
  const access = await licenseAccess(c.env, c.get('userId'), invite.license_id);
  if (!access.manage) throw new ApiError(404, 'not_found', 'No such invitation.');
  return { invite, access };
}

portal.post('/api/invitations/:id/revoke', async (c) => {
  const { invite } = await invitationFor(c, c.req.param('id'));
  await c.env.LICENSE_DB.prepare('UPDATE invitations SET revoked_at = ?2 WHERE id = ?1 AND accepted_at IS NULL')
    .bind(invite.id, nowSeconds()).run();
  return ok(c, { revoked: true });
});

portal.post('/api/invitations/:id/resend', async (c) => {
  const now = nowSeconds();
  const { invite, access } = await invitationFor(c, c.req.param('id'));
  // A new secret: the old link stops working, so resending never multiplies valid links.
  const secret = randomToken(32);
  await c.env.LICENSE_DB.prepare(
    'UPDATE invitations SET token_hash = ?2, expires_at = ?3 WHERE id = ?1 AND accepted_at IS NULL AND revoked_at IS NULL',
  ).bind(invite.id, await sha256Hex(secret), now + knob(c.env, 'INVITE_DAYS') * DAY).run();
  await mail.send(c.env, mail.inviteMail(invite.email_canonical, `${baseUrl(c.env)}/portal/invite?t=${secret}`,
    access.account_name));
  return ok(c, { sent: true });
});

portal.post('/api/tokens', async (c) => {
  const now = nowSeconds();
  const body = await readJson(c);
  const { seat, license } = await seatAccess(c.env, c.get('userId'), str(body, 'seat_id', 40) ?? '');
  const scope = String(body.scope ?? 'interactive');
  if (!(tokens.SCOPES as readonly string[]).includes(scope)) throw new ApiError(400, 'invalid_request', 'Unknown scope.');
  const label = str(body, 'label', 80);
  if (!label) throw new ApiError(400, 'invalid_request', 'Give the token a label.');
  const { token, row } = await tokens.mint(c.env, { license, seat, scope: scope as tokens.Scope, label,
    ttlDays: int(body, 'ttl_days'), createdBy: actor(c), now });
  return ok(c, { token, info: tokenView(row) }, 201);
});

portal.post('/api/tokens/:id/revoke', async (c) => {
  const row = await one<TokenRow>(c.env, 'SELECT * FROM license_tokens WHERE id = ?1', c.req.param('id'));
  if (!row) throw new ApiError(404, 'not_found', 'No such token.');
  await seatAccess(c.env, c.get('userId'), row.seat_id);
  return ok(c, { revoked: await tokens.revoke(c.env, row.id, actor(c), nowSeconds()) });
});

portal.post('/api/offline', async (c) => {
  const now = nowSeconds();
  const body = await readJson(c);
  const { seat, license } = await seatAccess(c.env, c.get('userId'), str(body, 'seat_id', 40) ?? '');
  const report = (body.report && typeof body.report === 'object' ? body.report : {}) as Record<string, unknown>;
  const result = await issueOffline(c.env, license, seat, report, int(body, 'days'), actor(c), now);
  await record(c.env, now, { actor: actor(c), kind: 'offline.downloaded', license_id: license.id, seat_id: seat.id,
    environment_id: result.environment_id });
  return ok(c, result, 201);
});
