/** @jsxImportSource hono/jsx */
/**
 * The admin pages, built around the questions actually asked day to day --
 * "is anything wrong?" and "whose licence needs changing?" -- rather than the
 * shape of the database. Server-rendered from the same tables as the API;
 * every action is an `Action` or `JsonForm` calling /admin/api.
 */
import { Hono } from 'hono';

import { USE_CLASSES } from '../billing';
import type { LicenseRow } from '../db';
import { all, one, parseEntitlements } from '../db';
import { DAY, knob, nowSeconds } from '../env';
import { type AppEnv, page } from '../http';
import {
  Action, Badge, Bars, Card, CheckField, Empty, Field, JsonForm, Note, Reveal, SelectField, Stat, Table,
  TextareaField,
} from '../ui/components';
import { PRESETS } from '../entitlements';
import { date, dateTime, kindCounts, licenceState, plural, relative } from '../ui/format';
import { GrantChips } from '../ui/grants';
import { licenseDetail } from './adminLicense';
import { actorLabel, payloadText, shell } from './adminShell';

export const adminPages = new Hono<AppEnv>();

// -- the licence table ----------------------------------------------------------------

export type LicenceListRow = LicenseRow & {
  account_name: string; owner_email: string | null; seats_used: number; envs_used: number;
};

const LIST_SELECT = `SELECT l.*, a.name AS account_name,
    (SELECT u.email FROM account_members m JOIN users u ON u.id = m.user_id
     WHERE m.account_id = l.account_id AND m.role = 'owner' AND m.status = 'active' LIMIT 1) AS owner_email,
    (SELECT COUNT(*) FROM seat_assignments s WHERE s.license_id = l.id AND s.status = 'active') AS seats_used,
    (SELECT COUNT(*) FROM environments e WHERE e.license_id = l.id AND e.status = 'active') AS envs_used
  FROM licenses l JOIN accounts a ON a.id = l.account_id`;

const LICENCE_HEAD = ['Licence', 'Kind', 'Status', 'Unlocks', 'Seats', 'Environments', 'Ends'];

function LicenceRowView({ row, now }: { row: LicenceListRow; now: number }) {
  const state = licenceState(row, now);
  return (
    <tr>
      <td>
        <a href={`/admin/licenses/${row.id}`}>{row.account_name}</a>
        <div class="sub">{row.owner_email ?? 'no owner'} · <span class="mono">{row.id}</span></div>
      </td>
      <td>{row.is_trial ? <Badge tone="accent">Trial</Badge> : <Badge>Paid</Badge>}
        <div class="sub">{row.use_class}</div></td>
      <td><Badge tone={state.tone}>{state.label}</Badge></td>
      <td><GrantChips grants={parseEntitlements(row.entitlements_json)} /></td>
      <td class="nowrap">{row.seats_used} / {row.seats}</td>
      <td class="nowrap">{row.envs_used} / {row.seats * row.envs_per_seat}</td>
      <td class="nowrap">{date(row.expires_at)}<div class="sub">{relative(row.expires_at, now)}</div></td>
    </tr>
  );
}

function LicenceTable({ rows, now, empty }: { rows: LicenceListRow[]; now: number; empty: string }) {
  if (rows.length === 0) return <Empty>{empty}</Empty>;
  return (
    <Table head={LICENCE_HEAD}>
      {rows.map((row) => <LicenceRowView row={row} now={now} />)}
    </Table>
  );
}

function SearchBox({ q }: { q?: string }) {
  return (
    <form method="get" action="/admin/licenses" class="filters">
      <div class="field grow">
        <label for="q">Search</label>
        <input id="q" type="text" name="q" value={q ?? ''} placeholder="email, account name, or licence id" />
      </div>
      <div class="actions"><button type="submit" class="ghost">Search</button></div>
    </form>
  );
}

/** `?unlocks=` on the licence list, and the dashboard's MCP count. JSON1, as signals.ts uses. */
const UNLOCKS_SQL = {
  ai: `EXISTS (SELECT 1 FROM json_each(l.entitlements_json) j WHERE j.value = 'ai' OR j.value LIKE 'ai:%')`,
  mcp: `EXISTS (SELECT 1 FROM json_each(l.entitlements_json) j WHERE j.value = 'mcp')`,
  none: `json_array_length(l.entitlements_json) = 0`,
};

const UNLOCKS_FILTERS = [{ value: '', label: 'Anything' }, { value: 'ai', label: 'Plexora AI' },
  { value: 'mcp', label: 'External MCP access' }, { value: 'none', label: 'Application only' }];

// -- dashboard -------------------------------------------------------------------------

adminPages.get('/', async (c) => {
  const now = nowSeconds();
  const since = now - 30 * DAY;
  const count = async (sql: string, ...params: unknown[]) =>
    (await one<{ n: number }>(c.env, sql, ...params))?.n ?? 0;
  const [paid, trials, newPaid, trials30, grants30, openSignals, environments, perDay, expiring, signals, recent,
    mcpLicences, mcpSeen] = await Promise.all([
      count(`SELECT COUNT(*) AS n FROM licenses WHERE status = 'active' AND expires_at > ?1 AND is_trial = 0`, now),
      count(`SELECT COUNT(*) AS n FROM licenses WHERE status = 'active' AND expires_at > ?1 AND is_trial = 1`, now),
      count('SELECT COUNT(*) AS n FROM licenses WHERE created_at > ?1 AND is_trial = 0', since),
      count('SELECT COUNT(*) AS n FROM trials WHERE created_at > ?1', since),
      count('SELECT COUNT(*) AS n FROM offline_grants WHERE issued_at > ?1', since),
      count('SELECT COUNT(*) AS n FROM signals WHERE acked_at IS NULL'),
      all<{ kind: string; n: number }>(c.env,
        `SELECT kind, COUNT(*) AS n FROM environments WHERE status = 'active' GROUP BY kind ORDER BY n DESC`),
      all<{ d: number; n: number }>(c.env,
        `SELECT CAST(created_at / 86400 AS INTEGER) AS d, COUNT(*) AS n FROM environments
         WHERE created_at > ?1 GROUP BY d`, since),
      all<LicenceListRow>(c.env, `${LIST_SELECT} WHERE l.status = 'active' AND l.expires_at > ?1 AND l.expires_at <= ?2
        ORDER BY l.expires_at LIMIT 50`, now, now + 30 * DAY),
      all<{ id: number; kind: string; subject: string; detail: string | null; created_at: number }>(c.env,
        'SELECT id, kind, subject, detail, created_at FROM signals WHERE acked_at IS NULL ORDER BY created_at DESC LIMIT 5'),
      all<LicenceListRow>(c.env, `${LIST_SELECT} ORDER BY l.created_at DESC LIMIT 8`),
      count(`SELECT COUNT(*) AS n FROM licenses l WHERE l.status = 'active' AND l.expires_at > ?1 AND ${UNLOCKS_SQL.mcp}`, now),
      count(`SELECT COUNT(*) AS n FROM environments WHERE status = 'active' AND last_mcp_at > ?1`, now - DAY),
    ]);
  const today = Math.floor(now / DAY);
  const days = Array.from({ length: 30 }, (_, i) => today - 29 + i);
  const byDay = new Map(perDay.map((row) => [row.d, row.n]));
  const values = days.map((d) => byDay.get(d) ?? 0);
  const labels = days.map((d) => date(d * DAY));
  const envTotal = environments.reduce((sum, row) => sum + row.n, 0);
  return page(c, shell(c, 'Dashboard', '/admin', (
    <>
      <div class="stats">
        <Stat label="Paid licences" value={paid} sub={`${newPaid} new in 30 days`} />
        <Stat label="Trials running" value={trials} sub={`${trials30} started in 30 days`} />
        <Stat label="Environments" value={envTotal} sub={kindCounts(environments) || 'none yet'} />
        <Stat label="Offline files" value={grants30} sub="issued in 30 days" />
        <Stat label="Open signals" value={openSignals} sub={openSignals ? <a href="/admin/signals">review</a> : 'nothing to review'} />
        <Stat label="External MCP" value={<a href="/admin/licenses?unlocks=mcp">{plural(mcpLicences, 'licence')}</a>}
          sub={`${plural(mcpSeen, 'environment')} seen in 24 h`} />
      </div>

      <Card title="Environments registered per day" sub={`${values.reduce((a, b) => a + b, 0)} in the last 30 days`}>
        <Bars values={values} labels={labels} label={`Environments registered per day: ${values.join(', ')}`} />
        <div class="axis"><span>{labels[0]}</span><span>{labels[labels.length - 1]}</span></div>
      </Card>

      <Card title="Find a licence"><SearchBox /></Card>

      <Card title="Ending within 30 days" sub="Reminders go out automatically; these are the ones worth a personal note.">
        <LicenceTable rows={expiring} now={now} empty="Nothing ends in the next 30 days." />
      </Card>

      <div class="grid stack-gap">
        <Card title="Open signals" sub="For review only. Nothing here blocks anything."
          actions={<a class="small" href="/admin/signals">All signals</a>}>
          {signals.length === 0 ? <Empty>Nothing to review.</Empty> : (
            <Table head={['When', 'Signal', 'Subject']}>
              {signals.map((s) => (
                <tr><td class="nowrap">{date(s.created_at)}</td><td><Badge tone="warn">{s.kind}</Badge></td>
                  <td class="mono small">{s.subject}</td></tr>
              ))}
            </Table>
          )}
        </Card>
        <Card title="Maintenance" sub={`Runs by itself every night at 03:30 UTC: expiry, reminders, idle environments,
          pruning, signals and the D1 backup to R2.`}>
          <Action action="/admin/api/cron" label="Run it now" tone="ghost" done="Maintenance ran." />
        </Card>
      </div>

      <Card title="Newest licences" actions={<a class="small" href="/admin/licenses">All licences</a>}>
        <LicenceTable rows={recent} now={now} empty="No licences yet. Issue the first one." />
      </Card>
    </>
  ), { lede: 'Everything below covers the last 30 days unless it says otherwise.',
    actions: <a class="button" href="/admin/issue">Issue a licence</a> }));
});

// -- the licence list ------------------------------------------------------------------

const STATUS_FILTERS = [
  { value: '', label: 'Any status' },
  { value: 'active', label: 'Active' },
  { value: 'ended', label: 'Ended' },
  { value: 'suspended', label: 'Suspended' },
  { value: 'revoked', label: 'Revoked' },
];

adminPages.get('/licenses', async (c) => {
  const now = nowSeconds();
  const q = (c.req.query('q') ?? '').trim().toLowerCase();
  const status = c.req.query('status') ?? '';
  const kind = c.req.query('kind') ?? '';
  const ending = Number(c.req.query('ending') ?? '') || 0;
  const unlocksFilter = c.req.query('unlocks') ?? '';
  const params: unknown[] = [];
  const param = (value: unknown) => { params.push(value); return `?${params.length}`; };
  const where: string[] = [];
  if (q) {
    const like = param(`%${q.replace(/[%_]/g, '')}%`);
    where.push(`(l.id LIKE ${like} OR lower(a.name) LIKE ${like} OR l.account_id LIKE ${like} OR EXISTS (
      SELECT 1 FROM account_members m JOIN users u ON u.id = m.user_id
      WHERE m.account_id = l.account_id AND u.email_canonical LIKE ${like}))`);
  }
  if (status === 'active') where.push(`l.status = 'active' AND l.expires_at > ${param(now)}`);
  else if (status === 'ended') where.push(`(l.status = 'expired' OR (l.status = 'active' AND l.expires_at <= ${param(now)}))`);
  else if (status === 'suspended' || status === 'revoked') where.push(`l.status = ${param(status)}`);
  if (kind === 'trial') where.push('l.is_trial = 1');
  else if (kind === 'paid') where.push('l.is_trial = 0');
  if (ending > 0) {
    where.push(`l.status = 'active' AND l.expires_at > ${param(now)} AND l.expires_at <= ${param(now + ending * DAY)}`);
  }
  if (unlocksFilter in UNLOCKS_SQL) where.push(UNLOCKS_SQL[unlocksFilter as keyof typeof UNLOCKS_SQL]);
  const rows = await all<LicenceListRow>(c.env,
    `${LIST_SELECT} ${where.length ? `WHERE ${where.join(' AND ')}` : ''} ORDER BY l.created_at DESC LIMIT 200`,
    ...params);
  const select = (name: string, value: string, options: { value: string; label: string }[], label: string) => (
    <div class="field">
      <label for={`f-${name}`}>{label}</label>
      <select id={`f-${name}`} name={name}>
        {options.map((o) => <option value={o.value} selected={o.value === value ? true : undefined}>{o.label}</option>)}
      </select>
    </div>
  );
  return page(c, shell(c, 'Licences', '/admin/licenses', (
    <>
      <Card>
        <form method="get" action="/admin/licenses" class="filters">
          <div class="field grow">
            <label for="q">Search</label>
            <input id="q" type="text" name="q" value={q} placeholder="email, account name, or licence id" />
          </div>
          {select('status', status, STATUS_FILTERS, 'Status')}
          {select('kind', kind, [{ value: '', label: 'Paid and trial' }, { value: 'paid', label: 'Paid' },
            { value: 'trial', label: 'Trial' }], 'Kind')}
          {select('ending', ending ? String(ending) : '', [{ value: '', label: 'Any end date' },
            { value: '30', label: 'Ending in 30 days' }, { value: '90', label: 'Ending in 90 days' }], 'Ends')}
          {select('unlocks', unlocksFilter, UNLOCKS_FILTERS, 'Unlocks')}
          <div class="actions"><button type="submit" class="ghost">Filter</button></div>
        </form>
      </Card>
      <Card>
        <LicenceTable rows={rows} now={now} empty="Nothing matched. Try part of an email address." />
      </Card>
    </>
  ), { lede: rows.length === 0 ? 'No licences match.' : `${plural(rows.length, 'licence')}${rows.length === 200 ? ' (the newest 200)' : ''}.`,
    actions: <a class="button" href="/admin/issue">Issue a licence</a> }));
});

adminPages.get('/licenses/:id', licenseDetail);

// -- issuing -----------------------------------------------------------------------------

/**
 * Hand-issuing: the only way a Paid licence is created until billing is wired
 * in. The no-charge preset is the reason the page has two forms: giving a
 * reviewer or a collaborator a licence should take fifteen seconds.
 */
adminPages.get('/issue', (c) => {
  const mailOff = !c.env.RESEND_API_KEY;
  const useClasses = USE_CLASSES.map((value) => ({ value, label: value[0]!.toUpperCase() + value.slice(1) }));
  return page(c, shell(c, 'Issue a licence', '/admin/issue', (
    <>
      {mailOff ? (
        <Note warn>Email is not set up yet (no <code>RESEND_API_KEY</code>), so nothing is sent: copy the seat key
          from this page and hand it over yourself.</Note>
      ) : null}
      <Card title="A Paid licence" sub="Creates the account if the address is new. The owner gets the first seat.">
        <JsonForm action="/admin/api/licenses" submit="Issue licence" reveal="seat.key,license.id" revealInto="#issued"
          done="Licence issued. Copy the key below.">
          <div class="form-grid">
            <Field label="Owner email" name="owner_email" type="email" required placeholder="pi@university.edu" />
            <Field label="Account name" name="account_name" placeholder="Lab or organisation (optional)" />
            <SelectField label="Account kind" name="account_kind" value="individual"
              options={[{ value: 'individual', label: 'Individual' }, { value: 'organization', label: 'Organisation' }]} />
            <SelectField label="Use class" name="use_class" value="academic" options={useClasses}
              hint="Shown in the certificate. It never changes what Paid unlocks." />
            <Field label="Seats" name="seats" type="number" num value={1} min={1} />
            <Field label="Environments per seat" name="envs_per_seat" type="number" num
              value={knob(c.env, 'DEFAULT_ENVS_PER_SEAT')} min={1} hint="A laptop and a cluster is two." />
            <Field label="Term in days" name="days" type="number" num value={365} min={1} max={3650} />
            <Field label="Grace days" name="grace_days" type="number" num value={knob(c.env, 'DEFAULT_GRACE_DAYS')}
              min={0} hint="How long Paid keeps working past the end date." />
            <div class="field">
              <label for="f-tier">Tier</label>
              <select id="f-tier" data-fill="#f-entitlements" aria-describedby="f-tier-hint">
                {PRESETS.map((p) => <option value={p.entitlements.join(', ')}
                  selected={p.id === 'ai' ? true : undefined}>{p.label}</option>)}
              </select>
              <div class="hint" id="f-tier-hint">Fills Grants. MCP is external MCP access: Paid tools from Claude
                Code, Codex or Cursor with their own model.</div>
            </div>
            <Field label="Grants" name="entitlements" list value="ai"
              hint="Comma separated. ai covers every AI feature; mcp is external MCP access." />
            <Field label="Longest offline file, days" name="offline_max_days" type="number" num
              value={knob(c.env, 'OFFLINE_DEFAULT_DAYS')} min={1} max={knob(c.env, 'OFFLINE_MAX_DAYS')} />
            <Field label="Reference" name="reference" placeholder="PO or invoice number (optional)" wide />
            <TextareaField label="Notes" name="notes" wide
              placeholder="Why this licence exists: a purchase, a reviewer, a collaborator, a replacement." />
          </div>
          <CheckField name="offline_allowed" checked label="Offline licence files allowed"
            hint="For machines and clusters that cannot reach the internet." />
          <CheckField name="send_email" checked label="Email the seat key to the owner"
            hint="Uncheck to hand it over yourself. Either way it is shown once, here." />
        </JsonForm>
        <Reveal id="issued" />
      </Card>

      <Card title="A no-charge licence" sub="One seat for a year, for a reviewer or a collaborator.">
        <JsonForm action="/admin/api/licenses" submit="Issue no-charge licence" tone="ghost" reveal="seat.key,license.id"
          revealInto="#issued-free" done="Licence issued. Copy the key below.">
          <div class="form-grid">
            <Field label="Email" name="owner_email" type="email" required placeholder="reviewer@journal.org" id="f-free-email" />
            <Field label="Account name" name="account_name" placeholder="optional" id="f-free-name" />
          </div>
          <input type="hidden" name="days" value="365" data-num="" />
          <input type="hidden" name="seats" value="1" data-num="" />
          <input type="hidden" name="reference" value="no-charge" />
          <TextareaField label="Notes" name="notes" value="No-charge licence." rows={2} id="f-free-notes" />
          <CheckField name="send_email" checked label="Email the seat key" />
        </JsonForm>
        <Reveal id="issued-free" />
      </Card>
    </>
  ), { lede: 'Licences are issued by hand until a billing provider is connected. Every one is recorded with your identity.' }));
});

// -- signals and events ------------------------------------------------------------------

adminPages.get('/signals', async (c) => {
  const everything = c.req.query('all') !== undefined;
  const rows = await all<{ id: number; kind: string; subject: string; detail: string | null; created_at: number;
    acked_at: number | null; acked_by: string | null }>(c.env,
    `SELECT id, kind, subject, detail, created_at, acked_at, acked_by FROM signals
     ${everything ? '' : 'WHERE acked_at IS NULL'} ORDER BY created_at DESC LIMIT 200`);
  return page(c, shell(c, 'Signals', '/admin/signals', (
    <>
      <Note>For review only. Nothing here blocks, bans or revokes anything, and most signals have an innocent
        explanation: a reinstall, a new laptop, a cluster behind a NAT pool. A conversation, not a cut-off.</Note>
      <Card title={everything ? 'Every signal' : 'Waiting for review'}
        actions={everything ? <a class="small" href="/admin/signals">Only open ones</a>
          : <a class="small" href="/admin/signals?all">Include reviewed</a>}>
        {rows.length === 0 ? <Empty>Nothing to review.</Empty> : (
          <Table head={['When', 'Signal', 'Subject', 'Detail', '']}>
            {rows.map((s) => (
              <tr>
                <td class="nowrap">{date(s.created_at)}</td>
                <td><Badge tone={s.acked_at ? 'plain' : 'warn'}>{s.kind}</Badge></td>
                <td class="mono small">{s.subject}</td>
                <td class="mono small">{payloadText(s.detail)}</td>
                <td class="actions">{s.acked_at ? <span class="sub">reviewed {date(s.acked_at)}</span> : (
                  <Action action={`/admin/api/signals/${s.id}/ack`} label="Reviewed" tone="ghost" small reload />
                )}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  ), { lede: 'Patterns worth a look, found by the nightly run.' }));
});

adminPages.get('/events', async (c) => {
  const licence = (c.req.query('license') ?? '').trim();
  const kind = (c.req.query('kind') ?? '').trim();
  const rows = await all<{ at: number; actor: string; kind: string; license_id: string | null; payload: string | null }>(c.env,
    `SELECT at, actor, kind, license_id, payload FROM events
     WHERE (?1 = '' OR license_id = ?1) AND (?2 = '' OR kind LIKE ?3)
     ORDER BY at DESC, id DESC LIMIT 200`, licence, kind, `${kind.replace(/[%_]/g, '')}%`);
  return page(c, shell(c, 'Events', '/admin/events', (
    <>
      <Card>
        <form method="get" action="/admin/events" class="filters">
          <div class="field grow">
            <label for="f-license">Licence id</label>
            <input id="f-license" type="text" name="license" value={licence} placeholder="lic_…" />
          </div>
          <div class="field grow">
            <label for="f-kind">What</label>
            <input id="f-kind" type="text" name="kind" value={kind} placeholder="e.g. license. or environment.registered" />
          </div>
          <div class="actions"><button type="submit" class="ghost">Filter</button></div>
        </form>
      </Card>
      <Card>
        {rows.length === 0 ? <Empty>No events match.</Empty> : (
          <Table head={['When', 'Who', 'What', 'Licence', 'Detail']}>
            {rows.map((e) => (
              <tr>
                <td class="nowrap small">{dateTime(e.at)}</td>
                <td class="small">{actorLabel(e.actor)}</td>
                <td><Badge>{e.kind}</Badge></td>
                <td class="mono small">{e.license_id ? <a href={`/admin/licenses/${e.license_id}`}>{e.license_id}</a> : '—'}</td>
                <td class="mono small muted">{payloadText(e.payload)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  ), { lede: 'The audit trail: every change lands here with who made it. Newest first, 200 at a time.' }));
});

// The old stats page is the dashboard now.
adminPages.get('/stats', (c) => c.redirect('/admin'));
