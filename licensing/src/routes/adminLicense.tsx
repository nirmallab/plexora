/** @jsxImportSource hono/jsx */
/**
 * One licence, and every change that can be made to it.
 *
 * Two rules hold throughout, as in the SCIMAP Pro admin this follows:
 * anything destructive is folded away behind a disclosure and a confirmation,
 * and every change is recorded with the administrator's identity, so the
 * history at the bottom is a complete account of what was done and by whom.
 */
import { USE_CLASSES } from '../billing';
import type { AccountRow, EnvironmentRow, LicenseRow, SeatRow, TokenRow } from '../db';
import { accountById, all, licenseById, one, parseEntitlements } from '../db';
import { knob, nowSeconds } from '../env';
import { ApiError, type App, page } from '../http';
import { SCOPES } from '../tokens';
import {
  Action, Badge, Card, CheckField, Disclosure, Empty, Field, FileField, JsonForm, Note, Reveal, RowMenu, Section,
  SelectField, Stat, Table, TextareaField,
} from '../ui/components';
import { date, dateTime, KIND_LABELS, licenceState, plural, relative, statusTone } from '../ui/format';
import { environmentView, seatView, tokenView } from '../views';
import { aiAccount, balance, type BalanceRow } from '../ai/ledger';
import { actorLabel, payloadText, shell } from './adminShell';

type Seat = SeatRow & { email: string | null };

interface Detail {
  license: LicenseRow;
  account: AccountRow | null;
  owner: string | null;
  seats: Seat[];
  environments: EnvironmentRow[];
  tokens: (TokenRow & { email: string | null })[];
  grants: { id: string; name: string | null; issued_at: number; offline_until: number; created_by: string | null }[];
  events: { at: number; actor: string; kind: string; payload: string | null }[];
  now: number;
}

export async function licenseDetail(c: App) {
  const license = await licenseById(c.env, c.req.param('id') ?? '');
  if (!license) throw new ApiError(404, 'not_found', 'No such licence.');
  const [account, owner, seats, environments, tokens, grants, events] = await Promise.all([
    accountById(c.env, license.account_id),
    one<{ email: string }>(c.env, `SELECT u.email FROM account_members m JOIN users u ON u.id = m.user_id
      WHERE m.account_id = ?1 AND m.role = 'owner' AND m.status = 'active' LIMIT 1`, license.account_id),
    all<Seat>(c.env, `SELECT s.*, u.email FROM seat_assignments s LEFT JOIN users u ON u.id = s.user_id
      WHERE s.license_id = ?1 ORDER BY s.status = 'active' DESC, s.created_at`, license.id),
    all<EnvironmentRow>(c.env, `SELECT * FROM environments WHERE license_id = ?1
      ORDER BY status = 'active' DESC, created_at`, license.id),
    all<TokenRow & { email: string | null }>(c.env, `SELECT t.*, u.email FROM license_tokens t
      JOIN seat_assignments s ON s.id = t.seat_id LEFT JOIN users u ON u.id = s.user_id
      WHERE t.license_id = ?1 ORDER BY t.revoked_at IS NULL DESC, t.created_at DESC`, license.id),
    all<Detail['grants'][number]>(c.env, `SELECT g.id, e.display_name AS name, g.issued_at, g.offline_until,
      g.created_by FROM offline_grants g LEFT JOIN environments e ON e.id = g.environment_id
      WHERE g.license_id = ?1 ORDER BY g.issued_at DESC`, license.id),
    all<Detail['events'][number]>(c.env, `SELECT at, actor, kind, payload FROM events WHERE license_id = ?1
      ORDER BY at DESC, id DESC LIMIT 50`, license.id),
  ]);
  const [aiSettings, aiBalance] = await Promise.all([aiAccount(c.env, license.account_id),
    balance(c.env, license.account_id)]);
  const detail: Detail = { license, account, owner: owner?.email ?? null, seats, environments, tokens, grants,
    events, now: nowSeconds() };
  const title = account?.name ?? license.id;
  return page(c, shell(c, title, '/admin/licenses', (
    <>
      <Summary detail={detail} />
      <Overrides detail={detail} offlineCeiling={knob(c.env, 'OFFLINE_MAX_DAYS')} />
      <Seats detail={detail} />
      <Environments detail={detail} />
      <Tokens detail={detail} />
      <Offline detail={detail} defaultDays={knob(c.env, 'OFFLINE_DEFAULT_DAYS')} />
      <PlexoraAi accountId={license.account_id} mode={aiSettings?.mode ?? 'credits'} balance={aiBalance}
        entitled={JSON.parse(license.entitlements_json || '[]').some((e: string) => e === 'ai' || e.startsWith('ai:'))} />
      <History detail={detail} />
    </>
  ), {
    lede: <>{detail.owner ?? 'no owner'} · <span class="mono">{license.id}</span> · {license.use_class}</>,
    actions: <a class="button ghost" href="/admin/licenses">All licences</a>,
  }));
}

const api = (license: LicenseRow) => `/admin/api/licenses/${license.id}`;

// -- summary ------------------------------------------------------------------------------

function Summary({ detail }: { detail: Detail }) {
  const { license, account, now } = detail;
  const state = licenceState(license, now);
  const activeSeats = detail.seats.filter((s) => s.status === 'active').length;
  const activeEnvs = detail.environments.filter((e) => e.status === 'active').length;
  const grants = parseEntitlements(license.entitlements_json);
  return (
    <Card feature
      title={<><Badge tone={state.tone}>{state.label}</Badge>{license.is_trial ? <Badge tone="accent">Trial</Badge> : null}</>}
      sub={state.detail} actions={<StatusActions license={license} />}>
      <div class="stats">
        <Stat label="Seats" value={`${activeSeats} / ${license.seats}`} sub="assigned" />
        <Stat label="Environments" value={`${activeEnvs} / ${license.seats * license.envs_per_seat}`}
          sub={`${license.envs_per_seat} per seat`} />
        <Stat label="Valid until" value={date(license.expires_at)} sub={relative(license.expires_at, now)} />
        <Stat label="Grace" value={plural(license.grace_days, 'day')} sub={license.is_trial ? 'none for a trial' : 'after the end date'} />
        <Stat label="Unlocks" value={<span class="mono">{grants.join(', ') || 'nothing'}</span>}
          sub={license.is_trial ? 'Paid trial' : 'Plexora Paid'} />
      </div>
      <Section title="Details">
        <Table head={[]} kv>
          <tr><td>Account</td><td>{account?.name ?? '—'} · {account?.kind ?? ''} · <span class="mono small">{license.account_id}</span></td></tr>
          <tr><td>Owner</td><td>{detail.owner ?? <span class="muted">none</span>}</td></tr>
          <tr><td>Offline licence files</td><td>{license.offline_allowed
            ? `Allowed, up to ${plural(license.offline_max_days, 'day')} each` : 'Not allowed'}</td></tr>
          <tr><td>Started</td><td>{date(license.starts_at)} <span class="muted">· created {dateTime(license.created_at)}</span></td></tr>
          <tr><td>Renewal</td><td>{license.renewal_state}</td></tr>
          {license.revoked_at ? (
            <tr><td>Revoked</td><td class="bad-text">{date(license.revoked_at)} · {license.revoke_reason ?? ''}</td></tr>
          ) : null}
          <tr><td>Notes</td><td>{license.notes ?? <span class="muted">Nothing recorded</span>}
            <span class="muted"> · edit under Overrides → Notes</span></td></tr>
        </Table>
      </Section>
    </Card>
  );
}

function StatusActions({ license }: { license: LicenseRow }) {
  if (license.status === 'revoked') return null;
  return license.status === 'suspended' ? (
    <Action action={api(license)} method="PATCH" body={{ status: 'active' }} label="Reactivate" tone="ghost"
      done="Reactivated." reload />
  ) : (
    <Action action={api(license)} method="PATCH" body={{ status: 'suspended' }} label="Suspend" tone="ghost"
      done="Suspended." reload
      confirm="Suspend this licence? Nothing new is issued and refreshes answer revoked until it is reactivated." />
  );
}

// -- overrides ----------------------------------------------------------------------------

function Overrides({ detail, offlineCeiling }: { detail: Detail; offlineCeiling: number }) {
  const { license } = detail;
  const target = api(license);
  return (
    <Card title="Overrides" sub="Every change here is written to the history below with your identity.">
      <Disclosure summary="Term: extend, or set the end date" open>
        <div class="actions">
          {[[30, '+30 days'], [90, '+90 days'], [365, '+1 year']].map(([days, label]) => (
            <Action action={target} method="PATCH" body={{ extend_days: days }} label={String(label)} tone="ghost"
              done="Extended." reload />
          ))}
        </div>
        <JsonForm action={target} method="PATCH" submit="Set end date" tone="ghost" done="End date set." reload inline>
          <input type="date" name="expires_at" data-date="" value={date(license.expires_at)} aria-label="End date" />
        </JsonForm>
        <div class="hint">Extending brings an ended licence back. Each environment picks up the new date at its next
          check, within a week.</div>
      </Disclosure>

      <Disclosure summary="Seats, environments and grace">
        <JsonForm action={target} method="PATCH" submit="Apply" tone="ghost" done="Licence updated." reload>
          <div class="form-grid three">
            <Field label="Seats" name="seats" type="number" num value={license.seats} min={1} id="o-seats" />
            <Field label="Environments per seat" name="envs_per_seat" type="number" num value={license.envs_per_seat}
              min={1} id="o-envs" />
            <Field label="Grace days" name="grace_days" type="number" num value={license.grace_days} min={0}
              id="o-grace" />
            <Field label="Longest offline file, days" name="offline_max_days" type="number" num
              value={license.offline_max_days} min={1} max={offlineCeiling} id="o-offline" />
          </div>
          <CheckField name="offline_allowed" checked={license.offline_allowed === 1} label="Offline licence files allowed" />
        </JsonForm>
      </Disclosure>

      <Disclosure summary="Grants and use class">
        <JsonForm action={target} method="PATCH" submit="Apply" tone="ghost" done="Licence updated." reload>
          <div class="form-grid">
            <Field label="Grants" name="entitlements" list value={parseEntitlements(license.entitlements_json).join(', ')}
              hint="ai covers every AI feature; a narrower grant such as ai:gating is possible." id="o-grants" />
            <SelectField label="Use class" name="use_class" value={license.use_class} id="o-use"
              options={USE_CLASSES.map((value) => ({ value, label: value }))} />
          </div>
        </JsonForm>
      </Disclosure>

      <Disclosure summary="Notes" open>
        <JsonForm action={target} method="PATCH" submit="Save notes" tone="ghost" done="Notes saved.">
          <TextareaField label="Notes" name="notes" value={license.notes} keepEmpty rows={3} id="o-notes"
            placeholder="Why this licence looks the way it does." />
        </JsonForm>
      </Disclosure>

      {license.status !== 'revoked' ? (
        <Disclosure summary="Revoke">
          <Note warn>Revoking ends this licence for good. Every online environment drops to Free at its next check,
            within a week. Offline licence files already issued keep working until their end date: they cannot be
            recalled. For a payment problem, Suspend is usually what you want.</Note>
          <JsonForm action={`${target}/revoke`} submit="Revoke licence" tone="danger" reload done="Revoked."
            confirm="Revoke this licence? This cannot be undone.">
            <Field label="Reason" name="reason" required placeholder="Chargeback, key shared, duplicate" id="o-reason" />
          </JsonForm>
        </Disclosure>
      ) : null}
    </Card>
  );
}

// -- Plexora AI ----------------------------------------------------------------------------

function credits(micro: number): string {
  return (micro / 10_000).toLocaleString('en-US', { maximumFractionDigits: 2 });
}

/** The account's AI balance and mode, and a credit grant (1 credit = $0.01; the ledger records every grant). */
function PlexoraAi(props: { accountId: string; mode: string; balance: BalanceRow; entitled: boolean }) {
  const api = `/admin/api/ai/accounts/${props.accountId}`;
  const b = props.balance;
  return (
    <Card title="Plexora AI" sub={props.entitled ? 'This licence includes AI.'
      : 'This licence has no `ai` entitlement: add it under Grants above for its seats to use AI.'}
      actions={<a class="small" href="/admin/ai">Models and routes</a>}>
      <Table head={['', '']} kv>
        <tr><th>Available</th><td>{credits(b.prepaid_micro + b.allowance_micro - b.held_micro)} credits</td></tr>
        <tr><th>Prepaid</th><td>{credits(b.prepaid_micro)} credits</td></tr>
        <tr><th>Allowance</th><td>{credits(b.allowance_micro)} credits{b.allowance_period ? ` (${b.allowance_period})` : ''}</td></tr>
        <tr><th>Held by calls in flight</th><td>{credits(b.held_micro)} credits</td></tr>
        <tr><th>Mode</th><td><Badge tone={props.mode === 'disabled' ? 'bad' : props.mode === 'dev' ? 'accent' : 'plain'}>
          {props.mode}</Badge></td></tr>
      </Table>
      <Disclosure summary="Grant credits" open>
        <JsonForm action={`${api}/credit`} submit="Grant" done="Credit posted." reload inline>
          <Field label="Credits" name="credits" type="number" num min={1} required id="ai-credits"
            hint="1 credit = $0.01 of metered use." />
          <Field label="Note" name="note" id="ai-note" placeholder="why" />
        </JsonForm>
      </Disclosure>
      <Disclosure summary="Mode">
        <JsonForm action={api} method="PATCH" submit="Set mode" tone="ghost" done="Mode set." reload inline>
          <SelectField label="Mode" name="mode" value={props.mode} id="ai-mode" options={[
            { value: 'credits', label: 'credits: metered, at the markup' },
            { value: 'dev', label: 'dev: internal testing, at cost, any model' },
            { value: 'disabled', label: 'disabled: no AI calls' }]} />
        </JsonForm>
      </Disclosure>
    </Card>
  );
}

// -- seats, environments, tokens, offline --------------------------------------------------

function Seats({ detail }: { detail: Detail }) {
  const { license } = detail;
  const active = detail.seats.filter((s) => s.status === 'active');
  const full = active.length >= license.seats;
  return (
    <Card title="Seats" sub={`${active.length} of ${license.seats} assigned. A seat is one person; each has its own key.`}>
      {detail.seats.length === 0 ? <Empty>No seats yet.</Empty> : (
        <Table head={['Person', 'Key', 'Since', 'Status', '']}>
          {detail.seats.map((row) => {
            const seat = seatView(row);
            return (
              <tr>
                <td>{seat.email ?? <span class="muted">unassigned</span>}<div class="sub mono">{seat.id}</div></td>
                <td class="mono small">{seat.key_hint}</td>
                <td class="nowrap">{date(seat.created_at)}</td>
                <td><Badge tone={statusTone(seat.status)}>{seat.status}</Badge></td>
                <td class="actions">{seat.status === 'active' ? (
                  <RowMenu>
                    <Action action={`/admin/api/seats/${seat.id}/rotate-key`} label="New key" tone="ghost" small
                      reveal="key" revealInto="#seat-key" confirm="Issue a new key for this seat? The old one stops working." />
                    <Action action={`/admin/api/seats/${seat.id}/release`} label="Release" tone="ghost" small reload
                      done="Seat released." confirm="Release this seat? Its environments drop to Free at their next check." />
                    <Action action={`/admin/api/seats/${seat.id}/revoke`} label="Revoke" tone="danger" small reload
                      done="Seat revoked." confirm="Revoke this seat? Its key stops working and its environments drop to Free." />
                  </RowMenu>
                ) : null}</td>
              </tr>
            );
          })}
        </Table>
      )}
      <Reveal id="seat-key" />
      <Disclosure summary="Add a seat" open={active.length === 0}>
        {full ? <Note warn>Every seat is assigned. Release one, or raise the seat count under Overrides.</Note> : null}
        <JsonForm action={`${api(license)}/seats`} submit="Add seat" tone="ghost" reveal="key" revealInto="#seat-new"
          done="Seat added. Copy its key below.">
          <div class="form-grid">
            <Field label="For (email)" name="email" type="email" placeholder="colleague@university.edu (optional)"
              id="s-email" hint="Leave empty for an unassigned seat whose key you hand over." />
          </div>
          <CheckField name="send_email" checked label="Email the seat key to them" />
        </JsonForm>
        <Reveal id="seat-new" />
      </Disclosure>
    </Card>
  );
}

function Environments({ detail }: { detail: Detail }) {
  const { license } = detail;
  const holders = new Map(detail.seats.map((s) => [s.id, s.email]));
  const active = detail.environments.filter((e) => e.status === 'active').length;
  return (
    <Card title="Environments"
      sub={`${active} active of ${license.seats * license.envs_per_seat} allowed. A computer, or a whole HPC cluster.`}>
      {detail.environments.length === 0 ? <Empty>Nothing has activated this licence yet.</Empty> : (
        <Table head={['Name', 'Kind', 'Seat holder', 'Plexora', 'Registered', 'Last seen', 'Status', '']}>
          {detail.environments.map((row) => {
            const env = environmentView(row);
            return (
              <tr>
                <td>{env.name}<div class="sub mono">{env.id}</div></td>
                <td>{KIND_LABELS[env.kind] ?? env.kind}
                  {env.registration === 'offline' ? <div class="sub">offline file</div> : null}</td>
                <td class="small">{holders.get(env.seat_id) ?? <span class="muted">unassigned</span>}</td>
                <td class="small">{env.app_version ?? '—'}<div class="sub">{env.platform ?? ''}</div></td>
                <td class="nowrap">{date(env.created_at)}</td>
                <td class="nowrap">{env.status === 'active' ? relative(env.last_seen, detail.now)
                  : <span class="muted">{env.release_reason ?? env.status} {date(env.released_at)}</span>}</td>
                <td><Badge tone={statusTone(env.status)}>{env.status}</Badge></td>
                <td class="actions">{env.status === 'active' ? (
                  <RowMenu>
                    <Action action={`/admin/api/environments/${env.id}/release`} label="Release" tone="ghost" small
                      reload done="Released." confirm={`Free the slot used by ${env.name}? No cooldown applies.`} />
                    <Action action={`/admin/api/environments/${env.id}/revoke`} label="Revoke" tone="danger" small
                      reload done="Revoked." confirm={`Revoke ${env.name}? It drops to Free at its next check.`} />
                  </RowMenu>
                ) : null}</td>
              </tr>
            );
          })}
        </Table>
      )}
    </Card>
  );
}

function SeatSelect({ detail, id }: { detail: Detail; id: string }) {
  const options = detail.seats.filter((s) => s.status === 'active')
    .map((s) => ({ value: s.id, label: s.email ?? `unassigned seat ${s.id}` }));
  return <SelectField label="Seat" name="seat_id" options={options} id={id} />;
}

function Tokens({ detail }: { detail: Detail }) {
  const hasSeat = detail.seats.some((s) => s.status === 'active');
  return (
    <Card title="Licence tokens"
      sub="Credentials for automation (PLEXORA_LICENSE_TOKEN). A token can only ask this service for a certificate.">
      {detail.tokens.length === 0 ? <Empty>No tokens.</Empty> : (
        <Table head={['Label', 'Scope', 'Seat holder', 'Expires', 'Last used', 'Uses', '']} right={[5]}>
          {detail.tokens.map((row) => {
            const token = tokenView(row);
            return (
              <tr>
                <td>{token.label}<div class="sub mono">{token.hint}</div></td>
                <td><Badge tone="accent">{token.scope}</Badge></td>
                <td class="small">{row.email ?? <span class="muted">unassigned</span>}</td>
                <td class="nowrap">{date(token.expires_at)}</td>
                <td class="nowrap">{token.last_used_at ? relative(token.last_used_at, detail.now) : 'never'}</td>
                <td class="right">{token.use_count}</td>
                <td class="actions">{token.revoked_at ? <Badge tone="bad">revoked</Badge> : (
                  <Action action={`/admin/api/tokens/${token.id}/revoke`} label="Revoke" tone="danger" small reload
                    done="Token revoked." confirm="Revoke this token? Anything using it can no longer activate." />
                )}</td>
              </tr>
            );
          })}
        </Table>
      )}
      {hasSeat ? (
        <Disclosure summary="Create a token">
          <JsonForm action="/admin/api/seats" template="/admin/api/seats/{seat_id}/tokens" submit="Create token"
            tone="ghost" reveal="token" revealInto="#token-new" done="Token created. Copy it below.">
            <div class="form-grid">
              <SeatSelect detail={detail} id="t-seat" />
              <SelectField label="Scope" name="scope" value="hpc" id="t-scope"
                options={SCOPES.map((value) => ({ value, label: value }))}
                hint="hpc registers a cluster once; ci never registers and gets a short certificate per use." />
              <Field label="Label" name="label" required placeholder="e.g. O2 cluster jobs" id="t-label" />
              <Field label="Days" name="ttl_days" type="number" num value={90} min={1} id="t-days" />
            </div>
          </JsonForm>
          <Reveal id="token-new" />
        </Disclosure>
      ) : null}
    </Card>
  );
}

function Offline({ detail, defaultDays }: { detail: Detail; defaultDays: number }) {
  const { license } = detail;
  const hasSeat = detail.seats.some((s) => s.status === 'active');
  return (
    <Card title="Offline licence files"
      sub="For machines and clusters that cannot reach the internet. A file cannot be recalled before its end date.">
      {detail.grants.length === 0 ? <Empty>None issued.</Empty> : (
        <Table head={['Environment', 'Issued', 'Works until', 'By']}>
          {detail.grants.map((grant) => (
            <tr>
              <td>{grant.name ?? '—'}<div class="sub mono">{grant.id}</div></td>
              <td class="nowrap">{dateTime(grant.issued_at)}</td>
              <td class="nowrap">{date(grant.offline_until)}<div class="sub">{relative(grant.offline_until, detail.now)}</div></td>
              <td class="small">{grant.created_by ? actorLabel(grant.created_by) : '—'}</td>
            </tr>
          ))}
        </Table>
      )}
      {license.offline_allowed && hasSeat ? (
        <Disclosure summary="Issue a file on their behalf">
          <p class="small muted">For a support thread: they run <code>plexora license fingerprint --out fp.json</code>
            {' '}(with <code>--cluster</code> on an HPC login node) and send you the file.</p>
          <JsonForm action="/admin/api/seats" template="/admin/api/seats/{seat_id}/offline" submit="Create and download"
            tone="ghost" download done="Licence file downloaded.">
            <div class="form-grid">
              <SeatSelect detail={detail} id="x-seat" />
              <Field label="Days" name="days" type="number" num value={Math.min(defaultDays, license.offline_max_days)}
                min={1} max={license.offline_max_days} id="x-days" hint={`Up to ${license.offline_max_days} for this licence.`} />
              <FileField label="Fingerprint report (fp.json)" name="report" accept=".json,application/json" id="x-file" />
              <TextareaField label="…or paste it" name="report" jsonText rows={2} id="x-paste" placeholder='{"schema": 1, …}' />
            </div>
          </JsonForm>
        </Disclosure>
      ) : null}
    </Card>
  );
}

function History({ detail }: { detail: Detail }) {
  return (
    <Card title="History" sub="Every change to this licence, newest first."
      actions={<a class="small" href={`/admin/events?license=${detail.license.id}`}>Full event log</a>}>
      {detail.events.length === 0 ? <Empty>Nothing recorded yet.</Empty> : (
        <Table head={['When', 'Who', 'What', 'Detail']}>
          {detail.events.map((e) => (
            <tr>
              <td class="nowrap small">{dateTime(e.at)}</td>
              <td class="small">{actorLabel(e.actor)}</td>
              <td><Badge>{e.kind}</Badge></td>
              <td class="mono small muted">{payloadText(e.payload)}</td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}
