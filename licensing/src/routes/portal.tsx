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
import { all, one, parseEntitlements, validEntitlementList } from '../db';
import * as mail from '../email';
import { baseUrl, DAY, knob, nowSeconds } from '../env';
import { narrows } from '../entitlements';
import { cleanName, release } from '../environments';
import { eventStatement, record } from '../events';
import { ApiError, type AppEnv, int, ok, page, readJson, sameOriginGuard, str } from '../http';
import { issueOffline } from '../offline';
import { environmentAccess, licenseAccess, licensesFor, seatAccess } from '../portalAccess';
import { enforce } from '../ratelimit';
import * as seats from '../seats';
import { acceptInvitation, endSession, requireUser, sendLoginLink, sessionUser, spendLoginLink, startSession } from '../sessions';
import * as tokens from '../tokens';
import {
  Action, Badge, Card, Empty, Field, FileField, IconButton, JsonForm, Mark, Note, Reveal, RowMenu, SelectField, Stat, Table,
} from '../ui/components';
import { date, KIND_LABELS, licenceState, plural, relative } from '../ui/format';
import { GrantChecks, GrantChips } from '../ui/grants';
import { Layout } from '../ui/layout';
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

async function shell(c: Parameters<typeof sessionUser>[0], title: string, active: string, body: Child,
  lede?: Child) {
  const user = await one<{ email: string }>(c.env, 'SELECT email FROM users WHERE id = ?1', c.get('userId'));
  return page(c, (
    <Layout title={title} product="portal" nav={NAV} active={active} who={user?.email ?? null} logout="/portal/logout"
      lede={lede}>
      {body}
    </Layout>
  ) as unknown as string);
}

/** The signed-out pages: one centred card. */
function AuthPage(props: { title: string; lede: Child; wide?: boolean; children?: Child }) {
  return (
    <Layout title={props.title} product="portal" heading={false} center>
      <div class={props.wide ? 'auth-card wide' : 'auth-card'}>
        <div class="auth-badge"><Mark /></div>
        <h1>{props.title}</h1>
        <p class="lede">{props.lede}</p>
        {props.children}
      </div>
    </Layout>
  );
}

// -- signing in -------------------------------------------------------------------

portal.get('/login', async (c) => {
  if (await sessionUser(c)) return c.redirect('/portal');
  return page(c, (
    <AuthPage title="Sign in"
      lede="Enter the email address your Plexora licence or seat was sent to. We will email you a sign-in link.">
      <JsonForm action="/portal/login" submit="Email me a link" wideSubmit
        done="If that address has a Plexora licence, a sign-in link is on its way.">
        <Field label="Email" name="email" type="email" autocomplete="email" required placeholder="you@university.edu" />
      </JsonForm>
      <p class="foot-note">No licence yet? Plexora Free needs none. <a href="/portal/trial">Start a Paid trial</a>.</p>
    </AuthPage>
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
    <AuthPage title="Signing in" lede="One more click: this link works once, and only from this browser.">
      <JsonForm action="/portal/auth" next="/portal" submit="Continue to the licence portal" wideSubmit>
        <input type="hidden" name="t" value={secret} />
      </JsonForm>
    </AuthPage>
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
  <AuthPage title="Your Plexora seat"
    lede="You have been given a Plexora Paid seat. Accepting it emails you its key and signs you in here.">
    <JsonForm action="/portal/invite" next="/portal" submit="Accept the seat" wideSubmit>
      <input type="hidden" name="t" value={c.req.query('t') ?? ''} />
    </JsonForm>
  </AuthPage>
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
    <AuthPage title="Try Plexora Paid" wide
      lede={`${knob(c.env, 'TRIAL_DAYS')} days of Plexora's AI features: guided gating sessions, the evidence an agent
        gathers, and what comes next. Everything Free stays free, and everything you make during the trial stays
        yours afterwards.`}>
      {fromPlexora ? (
        <JsonForm action="/v1/trial/start" submit="Send me a trial key" wideSubmit
          done="Check your email for your trial key.">
          <input type="hidden" name="fingerprint" value={fp} />
          <Field label="Email" name="email" type="email" autocomplete="email" required placeholder="you@university.edu" />
        </JsonForm>
      ) : (
        <Note>Open this page from Plexora (Settings &gt; License &gt; Start trial), or run
          <code> plexora license trial --email you@example.org</code>. A trial is one per person and per machine,
          and Plexora includes a hashed machine fingerprint to check that; nothing else about the machine is sent.</Note>
      )}
      <p class="foot-note">Already have a licence? <a href="/portal/login">Sign in</a>.</p>
    </AuthPage>
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
        <Card>
          <Empty>You have no Plexora licence on this address. Plexora Free needs none; <a href="/portal/trial">try
            Paid</a>.</Empty>
        </Card>
      ) : null}
      {access.map((a) => {
        const state = licenceState(a.license, now);
        const box = `key-${a.license.id}`;
        return (
          <Card feature
            title={<>Plexora Paid <Badge tone={state.tone}>{state.label}</Badge>
              {a.license.is_trial ? <Badge tone="accent">Trial</Badge> : null}</>}
            sub={<>{a.account_name} · {a.role} · <span class="mono">{a.license.id}</span></>}
            actions={a.seat ? (
              <>
                {a.seat.seat_key_vault ? (
                  <Action action={`/portal/api/seats/${a.seat.id}/reveal-key`} label="Show my seat key" tone="ghost"
                    reveal="key" revealInto={`#${box}`} />
                ) : null}
                <Action action={`/portal/api/seats/${a.seat.id}/rotate-key`} label="New seat key" tone="ghost"
                  reveal="key" revealInto={`#${box}`}
                  confirm="Issue a new seat key? The old key stops working; activated environments keep working." />
              </>
            ) : null}>
            <div class="stats">
              <Stat label="Valid until" value={date(a.license.expires_at)} sub={relative(a.license.expires_at, now)} />
              <Stat label="Grace" value={a.license.is_trial ? 'None' : plural(a.license.grace_days, 'day')}
                sub="after the end date" />
              <Stat label="Unlocks" value={<span class="mono">{parseEntitlements(a.license.entitlements_json).join(', ')}</span>}
                sub={`${a.license.use_class} use`} />
              <Stat label="Your seat" value={a.seat ? <span class="mono">…{a.seat.seat_key_hint}</span> : 'None'}
                sub={a.seat ? 'the end of your seat key' : 'an owner or admin can give you one'} />
            </div>
            <Reveal id={box} />
          </Card>
        );
      })}
      <Card title="Using your seat">
        <p>Activate in Plexora under <strong>Settings &gt; License</strong>, or run
          {' '}<code>plexora license activate &lt;key&gt;</code>.</p>
        <p>For an HPC cluster, run it once on a login node with <code>--cluster</code>: every node, job, notebook and
          container sharing that home directory then uses one registration.</p>
        <p class="muted small">No internet where Plexora runs? Use an <a href="/portal/offline">offline licence</a>.</p>
      </Card>
    </>
  ));
});

portal.get('/seats', async (c) => {
  const managed = (await licensesFor(c.env, c.get('userId'))).filter((a) => a.manage);
  const sections = await Promise.all(managed.map(async (a) => ({
    a,
    seats: await all<{ id: string; status: string; email: string | null; seat_key_hint: string; created_at: number;
      entitlements_override_json: string | null }>(c.env,
      `SELECT s.id, s.status, u.email, s.seat_key_hint, s.created_at, s.entitlements_override_json FROM seat_assignments s
       LEFT JOIN users u ON u.id = s.user_id WHERE s.license_id = ?1 AND s.status = 'active' ORDER BY s.created_at`,
      a.license.id),
    invitations: await all<{ id: string; email_canonical: string; expires_at: number }>(c.env,
      `SELECT id, email_canonical, expires_at FROM invitations WHERE license_id = ?1 AND accepted_at IS NULL
         AND revoked_at IS NULL AND expires_at > ?2 ORDER BY created_at`, a.license.id, nowSeconds()),
  })));
  return shell(c, 'Seats & Users', '/portal/seats', (
    <>
      {managed.length === 0 ? <Card><Empty>Only an account owner or admin manages seats.</Empty></Card> : null}
      {sections.map(({ a, seats: seatRows, invitations }) => {
        const box = `seat-${a.license.id}`;
        const licensed = parseEntitlements(a.license.entitlements_json);
        return (
          <Card title={a.account_name}
            sub={<><span class="mono">{a.license.id}</span> · {seatRows.length} of {a.license.seats} seats in use</>}>
            <JsonForm action={`/portal/api/licenses/${a.license.id}/invite`} submit="Invite" inline reload
              done="Invitation sent.">
              <input type="email" name="email" required placeholder="colleague@university.edu" aria-label="Invite by email" />
              <select name="role" aria-label="Role"><option value="member">Member</option><option value="admin">Admin</option></select>
            </JsonForm>
            <div class="section">
              {seatRows.length + invitations.length === 0 ? <Empty>No seats in use yet.</Empty> : (
                <Table head={['Holder', 'Unlocks', 'Key', 'Since', '']}>
                  {seatRows.map((s) => {
                    const own = s.entitlements_override_json !== null;
                    const grants = own ? parseEntitlements(s.entitlements_override_json) : licensed;
                    const editor = `grants-${s.id}`;
                    return [
                    <tr>
                      <td>{s.email ?? <span class="muted">unassigned</span>}</td>
                      <td><GrantChips grants={grants} />{own ? null : <div class="hint">as the licence</div>}</td>
                      <td class="mono small">…{s.seat_key_hint}</td>
                      <td class="nowrap">{date(s.created_at)}</td>
                      <td class="actions">
                        <IconButton icon="pencil" toggle={`#${editor}`} label="What this seat unlocks" />
                        <RowMenu>
                          <Action action={`/portal/api/seats/${s.id}/rotate-key`} label="New key" tone="ghost" small
                            reveal="key" revealInto={`#${box}`} confirm="Issue a new key for this seat?" />
                          <Action action={`/portal/api/seats/${s.id}/release`} label="Release" tone="danger" small reload
                            confirm="Release this seat? Its environments drop to Free at their next check." />
                        </RowMenu>
                      </td>
                    </tr>,
                    <tr class="editor" id={editor} hidden>
                      <td colspan={5}>
                        <JsonForm action={`/portal/api/seats/${s.id}/entitlements`} submit="Save" reload
                          done="Saved. Running copies of Plexora pick it up within 15 minutes.">
                          <GrantChecks name="entitlements_override" grants={grants} within={licensed} />
                          <p class="hint">A seat can be given less than the licence, never more.</p>
                        </JsonForm>
                        {own ? <Action action={`/portal/api/seats/${s.id}/entitlements`}
                          body={{ entitlements_override: null }} label="Same as the licence" tone="ghost" small reload /> : null}
                      </td>
                    </tr>,
                    ];
                  })}
                  {invitations.map((i) => (
                    <tr>
                      <td>{i.email_canonical} <Badge tone="warn">invited</Badge></td>
                      <td></td>
                      <td></td>
                      <td class="nowrap">until {date(i.expires_at)}</td>
                      <td class="actions">
                        <RowMenu>
                          <Action action={`/portal/api/invitations/${i.id}/resend`} label="Resend" tone="ghost" small
                            done="Sent again." />
                          <Action action={`/portal/api/invitations/${i.id}/revoke`} label="Withdraw" tone="danger" small
                            reload />
                        </RowMenu>
                      </td>
                    </tr>
                  ))}
                </Table>
              )}
              <Reveal id={box} />
            </div>
          </Card>
        );
      })}
    </>
  ), 'Everyone with a seat has their own key and their own environments.');
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
  const now = nowSeconds();
  return shell(c, 'Devices & Environments', '/portal/environments', (
    <>
      <Note>Removing an environment frees its slot on the seat. So that a seat is not rotated across many machines,
        a seat can remove one environment every {knob(c.env, 'COOLDOWN_HOURS')} hours. Owners and admins are exempt,
        and so is an environment unseen for {knob(c.env, 'STALE_ENV_EXEMPT_DAYS')} days.</Note>
      {groups.length === 0 ? <Card><Empty>No licences here yet.</Empty></Card> : null}
      {groups.map((g) => (
        <Card title={g.account} sub={<><span class="mono">{g.license_id}</span> · {plural(g.rows.length, 'environment')}</>}>
          {g.rows.length === 0 ? <Empty>Nothing activated yet.</Empty> : (
            <Table head={['Name', 'Kind', ...(g.manage ? ['Seat holder'] : []), 'Registered', 'Last seen', '']}>
              {g.rows.map((row) => {
                const e = environmentView(row);
                return (
                  <tr>
                    <td>
                      <JsonForm action={`/portal/api/environments/${e.id}/rename`} submit="Rename" tone="ghost" inline
                        done="Renamed.">
                        <input name="name" value={e.name} aria-label="Name" />
                      </JsonForm>
                    </td>
                    <td>{KIND_LABELS[e.kind] ?? e.kind}{e.registration === 'offline' ? <div class="sub">offline file</div> : null}</td>
                    {g.manage ? <td class="small">{row.email ?? '—'}</td> : null}
                    <td class="nowrap">{date(e.created_at)}</td>
                    <td class="nowrap">{relative(e.last_seen, now)}</td>
                    <td class="actions">
                      <Action action={`/portal/api/environments/${e.id}/remove`} label="Remove" tone="danger" small reload
                        confirm="Remove this environment? Paid features there stop at its next check." />
                    </td>
                  </tr>
                );
              })}
            </Table>
          )}
        </Card>
      ))}
    </>
  ), 'An environment is a computer, or a whole HPC cluster.');
});

portal.get('/offline', async (c) => {
  const userId = c.get('userId');
  const now = nowSeconds();
  const access = await licensesFor(c.env, userId);
  const seatOptions: { value: string; label: string }[] = [];
  for (const a of access) {
    if (a.license.offline_allowed !== 1) continue;
    if (a.manage) {
      const rows = await all<{ id: string; email: string | null }>(c.env,
        `SELECT s.id, u.email FROM seat_assignments s LEFT JOIN users u ON u.id = s.user_id
         WHERE s.license_id = ?1 AND s.status = 'active'`, a.license.id);
      rows.forEach((r) => seatOptions.push({ value: r.id, label: `${a.account_name}: ${r.email ?? 'unassigned seat'}` }));
    } else if (a.seat) {
      seatOptions.push({ value: a.seat.id, label: `${a.account_name}: your seat` });
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
      <Card title="Create an offline licence">
        <p>On the computer or cluster, run <code>plexora license fingerprint --out fp.json</code> (add
          {' '}<code>--cluster</code> on an HPC login node). Upload that file here, then install the licence file you
          get with <code>plexora license install</code>.</p>
        <Note warn>An offline licence <strong>cannot be recalled</strong> before its end date, even if the seat is
          released. Issue one only for a machine that really is offline, and keep the file private.</Note>
        {seatOptions.length === 0 ? <Empty>None of your licences include offline licences.</Empty> : (
          <JsonForm action="/portal/api/offline" submit="Create offline licence" download done="Downloaded.">
            <div class="form-grid">
              <SelectField label="Seat" name="seat_id" options={seatOptions} />
              <Field label="Days" name="days" type="number" num min={1} value={knob(c.env, 'OFFLINE_DEFAULT_DAYS')}
                hint="Up to your licence's limit." />
              <FileField label="Fingerprint report (fp.json)" name="report" accept=".json,application/json" required />
            </div>
          </JsonForm>
        )}
      </Card>
      <Card title="Issued offline licences">
        {grants.length === 0 ? <Empty>None yet.</Empty> : (
          <Table head={['Environment', 'Issued', 'Works until']}>
            {grants.map((g) => (
              <tr><td>{g.name}</td><td class="nowrap">{date(g.issued_at)}</td>
                <td class="nowrap">{date(g.offline_until)}<div class="sub">{relative(g.offline_until, now)}</div></td></tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  ), 'For a computer or cluster that cannot reach the internet.');
});

portal.get('/tokens', async (c) => {
  const userId = c.get('userId');
  const now = nowSeconds();
  const access = await licensesFor(c.env, userId);
  const seatOptions: { value: string; label: string }[] = [];
  const rows: (TokenRow & { email: string | null })[] = [];
  for (const a of access) {
    const seatsHere = await all<{ id: string; email: string | null }>(c.env,
      `SELECT s.id, u.email FROM seat_assignments s LEFT JOIN users u ON u.id = s.user_id
       WHERE s.license_id = ?1 AND s.status = 'active' AND (?2 = 1 OR s.user_id = ?3)`,
      a.license.id, a.manage ? 1 : 0, userId);
    seatsHere.forEach((s) => seatOptions.push({ value: s.id, label: `${a.account_name}: ${s.email ?? 'unassigned seat'}` }));
    for (const s of seatsHere) {
      rows.push(...(await all<TokenRow>(c.env,
        'SELECT * FROM license_tokens WHERE seat_id = ?1 ORDER BY created_at DESC', s.id)).map((t) => ({ ...t, email: s.email })));
    }
  }
  return shell(c, 'Licence Tokens', '/portal/tokens', (
    <>
      <Card title="Create a token">
        <p>Set <code>PLEXORA_LICENSE_TOKEN</code> where Plexora runs unattended. <strong>hpc</strong> tokens register a
          cluster once; <strong>ci</strong> tokens never register anything and get a {knob(c.env, 'CI_CERT_HOURS')}-hour
          certificate each time. A token is shown once.</p>
        {seatOptions.length ? (
          <JsonForm action="/portal/api/tokens" submit="Create token" reveal="token" revealInto="#token-new"
            done="Token created. Copy it below.">
            <div class="form-grid">
              <SelectField label="Seat" name="seat_id" options={seatOptions} />
              <SelectField label="Scope" name="scope" value="interactive"
                options={tokens.SCOPES.map((value) => ({ value, label: value }))} />
              <Field label="Label" name="label" required placeholder="e.g. O2 cluster jobs" />
              <Field label="Days" name="ttl_days" type="number" num min={1} value={knob(c.env, 'TOKEN_DEFAULT_TTL_DAYS')} />
            </div>
          </JsonForm>
        ) : <Empty>You need a seat to create a token.</Empty>}
        <Reveal id="token-new" />
      </Card>
      <Card title="Your tokens">
        {rows.length === 0 ? <Empty>No tokens yet.</Empty> : (
          <Table head={['Label', 'Scope', 'Seat', 'Expires', 'Last used', 'Uses', '']} right={[5]}>
            {rows.map((row) => {
              const t = tokenView(row);
              return (
                <tr>
                  <td>{t.label}<div class="sub mono">{t.hint}</div></td>
                  <td><Badge tone="accent">{t.scope}</Badge></td>
                  <td class="small">{row.email ?? '—'}</td>
                  <td class="nowrap">{date(t.expires_at)}</td>
                  <td class="nowrap">{t.last_used_at ? relative(t.last_used_at, now) : 'never'}</td>
                  <td class="right">{t.use_count}</td>
                  <td class="actions">{t.revoked_at ? <Badge tone="bad">revoked</Badge> : (
                    <Action action={`/portal/api/tokens/${t.id}/revoke`} label="Revoke" tone="danger" small reload
                      confirm="Revoke this token? Anything using it can no longer activate." />
                  )}</td>
                </tr>
              );
            })}
          </Table>
        )}
      </Card>
    </>
  ), 'Credentials for automation: clusters, pipelines and CI.');
});

portal.get('/billing', async (c) => {
  const access = (await licensesFor(c.env, c.get('userId'))).filter((a) => a.manage);
  const support = c.env.SUPPORT_EMAIL ?? 'support@plexoraapp.com';
  const now = nowSeconds();
  return shell(c, 'Billing & Renewal', '/portal/billing', (
    <>
      <Card title="Renewing">
        <p>Licences are issued and renewed by hand for now. To renew, add seats or change your plan, email
          {' '}<a href={`mailto:${support}`}>{support}</a> with your licence id.</p>
        <p class="muted small">Plexora's licence covers Plexora. AI model providers your agent uses bill you separately.</p>
      </Card>
      <Card title="Your licences">
        {access.length === 0 ? <Empty>Only an account owner or admin sees billing.</Empty> : (
          <Table head={['Licence', 'Account', 'Kind', 'Valid until', 'Renewal']}>
            {access.map((a) => (
              <tr>
                <td class="mono small">{a.license.id}</td>
                <td>{a.account_name}</td>
                <td>{a.license.is_trial ? <Badge tone="accent">Trial</Badge> : <Badge>Paid</Badge>}</td>
                <td class="nowrap">{date(a.license.expires_at)}<div class="sub">{relative(a.license.expires_at, now)}</div></td>
                <td>{a.license.renewal_state}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
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

// An owner or admin narrows what a seat unlocks (`mcp` off for one person,
// say), or sets it back to the licence's (null). Never wider than the licence.
portal.post('/api/seats/:id/entitlements', async (c) => {
  const { seat, license, manage } = await seatAccess(c.env, c.get('userId'), c.req.param('id'));
  if (!manage) throw new ApiError(403, 'forbidden', 'Only an owner or admin changes what a seat unlocks.');
  if (seat.status !== 'active') throw new ApiError(409, 'conflict', 'That seat is not active.');
  const body = await readJson(c);
  if (!('entitlements_override' in body)) throw new ApiError(400, 'invalid_request', 'Nothing to change.');
  let list: string[] | null = null;
  if (body.entitlements_override !== null) {
    list = validEntitlementList(body.entitlements_override);
    if (list === null) throw new ApiError(400, 'invalid_request', 'entitlements_override must be entitlement strings or null.');
    if (!narrows(list, parseEntitlements(license.entitlements_json))) {
      throw new ApiError(400, 'invalid_request', 'A seat cannot be granted more than its licence.');
    }
  }
  await seats.setEntitlements(c.env, seat, list, nowSeconds(), actor(c));
  return ok(c, { entitlements_override: list });
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
