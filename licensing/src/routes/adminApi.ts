/**
 * /admin/api: the JSON the admin pages call, and what a script with
 * ADMIN_TOKEN can call directly. Manual issuance here is the only way a paid
 * licence is created until a billing provider is wired in, and it goes through
 * the same `provisionLicense` a webhook will.
 */
import { Hono } from 'hono';

import { memberStatement, upsertUser, validEmail } from '../accounts';
import { provisionLicense, USE_CLASSES } from '../billing';
import type { EnvironmentRow, LicenseRow, SeatRow, TokenRow } from '../db';
import { accountById, all, environmentById, licenseById, one, seatById, validEntitlementList } from '../db';
import * as mail from '../email';
import { baseUrl, DAY, nowSeconds } from '../env';
import { release } from '../environments';
import { eventStatement, record } from '../events';
import { ApiError, type AppEnv, int, ok, readJson, str } from '../http';
import { issueOffline } from '../offline';
import { runMaintenance, type Task } from '../cron';
import * as seats from '../seats';
import * as tokens from '../tokens';
import { environmentView, licenseView, seatView, tokenView } from '../views';

export const adminApi = new Hono<AppEnv>();

const actorOf = (who: string) => `admin:${who}`;

adminApi.post('/licenses', async (c) => {
  const now = nowSeconds();
  const body = await readJson(c);
  const email = validEmail(body.owner_email);
  if (!email) throw new ApiError(400, 'invalid_request', 'owner_email must be an email address.');
  const provisioned = await provisionLicense(c.env, {
    account_id: str(body, 'account_id', 40) ?? undefined,
    account: { kind: body.account_kind === 'organization' ? 'organization' : 'individual',
      name: str(body, 'account_name', 120) ?? email },
    owner_email: email,
    use_class: str(body, 'use_class', 20) ?? 'academic',
    seats: int(body, 'seats') ?? 1,
    envs_per_seat: int(body, 'envs_per_seat') ?? undefined,
    days: int(body, 'days') ?? undefined,
    expires_at: int(body, 'expires_at') ?? undefined,
    entitlements: body.entitlements === undefined ? undefined : (body.entitlements as string[]),
    grace_days: int(body, 'grace_days') ?? undefined,
    offline_allowed: body.offline_allowed === undefined ? undefined : body.offline_allowed === true,
    offline_max_days: int(body, 'offline_max_days') ?? undefined,
    notes: str(body, 'notes', 2000),
    purchase: { provider: 'manual', provider_ref: str(body, 'reference', 120), kind: 'manual',
      amount_cents: int(body, 'amount_cents') ?? 0, currency: str(body, 'currency', 3) },
  }, actorOf(c.get('admin')), now);
  if (provisioned.seat_key && body.send_email !== false) {
    await mail.send(c.env, mail.seatKeyMail(email, provisioned.seat_key, {
      expires_at: provisioned.license.expires_at, portal: `${baseUrl(c.env)}/portal` }));
  }
  return ok(c, {
    license: licenseView(provisioned.license),
    account_id: provisioned.account.id,
    seat: provisioned.seat ? { id: provisioned.seat.id, key: provisioned.seat_key } : null,
  }, 201);
});

adminApi.get('/licenses', async (c) => {
  const q = (c.req.query('q') ?? '').trim().toLowerCase();
  const status = c.req.query('status');
  const limit = Math.min(Number(c.req.query('limit') ?? 50) || 50, 200);
  const like = `%${q.replace(/[%_]/g, '')}%`;
  const rows = await all<LicenseRow & { account_name: string; owner_email: string | null }>(c.env,
    `SELECT l.*, a.name AS account_name,
       (SELECT u.email FROM account_members m JOIN users u ON u.id = m.user_id
        WHERE m.account_id = l.account_id AND m.role = 'owner' AND m.status = 'active' LIMIT 1) AS owner_email
     FROM licenses l JOIN accounts a ON a.id = l.account_id
     WHERE (?1 = '' OR l.id LIKE ?2 OR lower(a.name) LIKE ?2 OR l.account_id LIKE ?2 OR EXISTS (
       SELECT 1 FROM account_members m JOIN users u ON u.id = m.user_id
       WHERE m.account_id = l.account_id AND u.email_canonical LIKE ?2))
       AND (?3 IS NULL OR l.status = ?3)
     ORDER BY l.created_at DESC LIMIT ?4`, q, like, status ?? null, limit);
  return ok(c, { licenses: rows.map((r) => ({ ...licenseView(r), account_name: r.account_name,
    owner_email: r.owner_email })) });
});

async function licenseOr404(c: { env: AppEnv['Bindings'] }, id: string): Promise<LicenseRow> {
  const license = await licenseById(c.env, id);
  if (!license) throw new ApiError(404, 'not_found', 'No such licence.');
  return license;
}

adminApi.get('/licenses/:id', async (c) => {
  const license = await licenseOr404(c, c.req.param('id'));
  const [account, seatRows, envRows, tokenRows, grants, events] = await Promise.all([
    accountById(c.env, license.account_id),
    all<SeatRow & { email: string | null }>(c.env,
      `SELECT s.*, u.email FROM seat_assignments s LEFT JOIN users u ON u.id = s.user_id
       WHERE s.license_id = ?1 ORDER BY s.created_at`, license.id),
    all<EnvironmentRow>(c.env, 'SELECT * FROM environments WHERE license_id = ?1 ORDER BY created_at', license.id),
    all<TokenRow>(c.env, 'SELECT * FROM license_tokens WHERE license_id = ?1 ORDER BY created_at', license.id),
    all(c.env, 'SELECT * FROM offline_grants WHERE license_id = ?1 ORDER BY issued_at DESC', license.id),
    all(c.env, `SELECT id, at, actor, kind, seat_id, environment_id, payload FROM events WHERE license_id = ?1
                ORDER BY at DESC, id DESC LIMIT 100`, license.id),
  ]);
  return ok(c, {
    license: licenseView(license), account,
    seats: seatRows.map(seatView), environments: envRows.map(environmentView), tokens: tokenRows.map(tokenView),
    offline_grants: grants, events,
  });
});

adminApi.patch('/licenses/:id', async (c) => {
  const now = nowSeconds();
  const license = await licenseOr404(c, c.req.param('id'));
  const body = await readJson(c);
  const changes: Record<string, unknown> = {};
  const extend = int(body, 'extend_days');
  if (extend !== null) changes.expires_at = Math.max(license.expires_at, now) + extend * DAY;
  const expires = int(body, 'expires_at');
  if (expires !== null) changes.expires_at = expires;
  for (const name of ['seats', 'envs_per_seat', 'grace_days', 'offline_max_days'] as const) {
    const value = int(body, name);
    if (value !== null) {
      if (value < (name === 'grace_days' ? 0 : 1)) throw new ApiError(400, 'invalid_request', `${name} is too small.`);
      changes[name] = value;
    }
  }
  if (body.use_class !== undefined) {
    if (!(USE_CLASSES as readonly string[]).includes(String(body.use_class))) {
      throw new ApiError(400, 'invalid_request', 'Unknown use_class.');
    }
    changes.use_class = body.use_class;
  }
  if (body.entitlements !== undefined) {
    const list = validEntitlementList(body.entitlements);
    if (list === null) throw new ApiError(400, 'invalid_request', 'entitlements must be entitlement strings.');
    changes.entitlements_json = JSON.stringify(list);
  }
  if (body.status !== undefined) {
    if (!['active', 'suspended'].includes(String(body.status))) {
      throw new ApiError(400, 'invalid_request', 'status may be set to active or suspended; use /revoke to revoke.');
    }
    if (license.status === 'revoked') throw new ApiError(409, 'conflict', 'A revoked licence stays revoked.');
    changes.status = body.status;
  }
  if (body.offline_allowed !== undefined) changes.offline_allowed = body.offline_allowed === true ? 1 : 0;
  if (body.notes !== undefined) changes.notes = str(body, 'notes', 2000);
  if (body.renewal_state !== undefined) changes.renewal_state = str(body, 'renewal_state', 20);
  // An expired licence brought back to life by an extension is active again.
  if (changes.expires_at !== undefined && license.status === 'expired' && (changes.expires_at as number) > now) {
    changes.status ??= 'active';
  }
  const names = Object.keys(changes);
  if (names.length === 0) throw new ApiError(400, 'invalid_request', 'Nothing to change.');
  const sets = names.map((name, i) => `${name} = ?${i + 2}`).join(', ');
  await c.env.LICENSE_DB.batch([
    c.env.LICENSE_DB.prepare(`UPDATE licenses SET ${sets}, updated_at = ?${names.length + 2} WHERE id = ?1`)
      .bind(license.id, ...names.map((n) => changes[n]), now),
    eventStatement(c.env, now, { actor: actorOf(c.get('admin')), kind: 'license.updated', license_id: license.id,
      account_id: license.account_id, payload: changes }),
  ]);
  return ok(c, { license: licenseView((await licenseById(c.env, license.id))!) });
});

adminApi.post('/licenses/:id/revoke', async (c) => {
  const now = nowSeconds();
  const license = await licenseOr404(c, c.req.param('id'));
  const body = await readJson(c).catch(() => ({} as Record<string, unknown>));
  const reason = str(body, 'reason', 200) ?? 'revoked by an administrator';
  await c.env.LICENSE_DB.batch([
    c.env.LICENSE_DB.prepare(
      `UPDATE licenses SET status = 'revoked', revoked_at = ?2, revoke_reason = ?3, updated_at = ?2 WHERE id = ?1`,
    ).bind(license.id, now, reason),
    c.env.LICENSE_DB.prepare('UPDATE license_tokens SET revoked_at = ?2 WHERE license_id = ?1 AND revoked_at IS NULL')
      .bind(license.id, now),
    eventStatement(c.env, now, { actor: actorOf(c.get('admin')), kind: 'license.revoked', license_id: license.id,
      account_id: license.account_id, payload: { reason } }),
  ]);
  return ok(c, { license: licenseView((await licenseById(c.env, license.id))!) });
});

adminApi.post('/licenses/:id/seats', async (c) => {
  const now = nowSeconds();
  const license = await licenseOr404(c, c.req.param('id'));
  const body = await readJson(c).catch(() => ({} as Record<string, unknown>));
  const email = body.email === undefined ? null : validEmail(body.email);
  if (body.email !== undefined && !email) throw new ApiError(400, 'invalid_request', 'Not an email address.');
  const user = email ? await upsertUser(c.env, email, now) : null;
  if (user) await memberStatement(c.env, license.account_id, user.id, 'member', now).run();
  const { seat, key } = await seats.create(c.env, license, user?.id ?? null, now, actorOf(c.get('admin')));
  if (email && body.send_email !== false) {
    await mail.send(c.env, mail.seatKeyMail(email, key, { expires_at: license.expires_at,
      portal: `${baseUrl(c.env)}/portal` }));
  }
  return ok(c, { seat: seatView(seat), key }, 201);
});

async function seatOr404(c: { env: AppEnv['Bindings'] }, id: string): Promise<SeatRow> {
  const seat = await seatById(c.env, id);
  if (!seat) throw new ApiError(404, 'not_found', 'No such seat.');
  return seat;
}

adminApi.post('/seats/:id/release', async (c) => {
  const seat = await seatOr404(c, c.req.param('id'));
  await seats.end(c.env, seat, 'released', nowSeconds(), actorOf(c.get('admin')));
  return ok(c, { seat: seatView((await seatById(c.env, seat.id))!) });
});

adminApi.post('/seats/:id/revoke', async (c) => {
  const seat = await seatOr404(c, c.req.param('id'));
  await seats.end(c.env, seat, 'revoked', nowSeconds(), actorOf(c.get('admin')));
  return ok(c, { seat: seatView((await seatById(c.env, seat.id))!) });
});

adminApi.post('/seats/:id/rotate-key', async (c) => {
  const seat = await seatOr404(c, c.req.param('id'));
  if (seat.status !== 'active') throw new ApiError(409, 'conflict', 'That seat is not active.');
  const key = await seats.rotateKey(c.env, seat, nowSeconds(), actorOf(c.get('admin')));
  return ok(c, { key });
});

adminApi.post('/seats/:id/tokens', async (c) => {
  const now = nowSeconds();
  const seat = await seatOr404(c, c.req.param('id'));
  const license = await licenseOr404(c, seat.license_id);
  const body = await readJson(c);
  const scope = String(body.scope ?? 'interactive');
  if (!(tokens.SCOPES as readonly string[]).includes(scope)) throw new ApiError(400, 'invalid_request', 'Unknown scope.');
  const { token, row } = await tokens.mint(c.env, { license, seat, scope: scope as tokens.Scope,
    label: str(body, 'label', 80) ?? scope, ttlDays: int(body, 'ttl_days'), createdBy: actorOf(c.get('admin')), now });
  return ok(c, { token, info: tokenView(row) }, 201);
});

adminApi.post('/seats/:id/offline', async (c) => {
  const now = nowSeconds();
  const seat = await seatOr404(c, c.req.param('id'));
  const license = await licenseOr404(c, seat.license_id);
  const body = await readJson(c);
  const report = (body.report && typeof body.report === 'object' ? body.report : {}) as Record<string, unknown>;
  return ok(c, await issueOffline(c.env, license, seat, report, int(body, 'days'), actorOf(c.get('admin')), now), 201);
});

adminApi.post('/environments/:id/revoke', async (c) => {
  const now = nowSeconds();
  const environment = await environmentById(c.env, c.req.param('id'));
  if (!environment) throw new ApiError(404, 'not_found', 'No such environment.');
  await c.env.LICENSE_DB.batch([
    c.env.LICENSE_DB.prepare(
      `UPDATE environments SET status = 'revoked', released_at = ?2, release_reason = 'admin'
       WHERE id = ?1 AND status = 'active'`).bind(environment.id, now),
    eventStatement(c.env, now, { actor: actorOf(c.get('admin')), kind: 'environment.revoked',
      license_id: environment.license_id, seat_id: environment.seat_id, environment_id: environment.id }),
  ]);
  return ok(c, { environment: environmentView((await environmentById(c.env, environment.id))!) });
});

adminApi.post('/environments/:id/release', async (c) => {
  const now = nowSeconds();
  const environment = await environmentById(c.env, c.req.param('id'));
  if (!environment) throw new ApiError(404, 'not_found', 'No such environment.');
  const seat = await seatOr404(c, environment.seat_id);
  await release(c.env, environment, seat, { now, reason: 'admin', actor: actorOf(c.get('admin')), override: true });
  return ok(c, { environment: environmentView((await environmentById(c.env, environment.id))!) });
});

adminApi.post('/tokens/:id/revoke', async (c) => {
  const revoked = await tokens.revoke(c.env, c.req.param('id'), actorOf(c.get('admin')), nowSeconds());
  return ok(c, { revoked });
});

adminApi.get('/signals', async (c) => {
  const open = c.req.query('all') === undefined;
  const rows = await all(c.env,
    `SELECT * FROM signals ${open ? 'WHERE acked_at IS NULL' : ''} ORDER BY created_at DESC LIMIT 200`);
  return ok(c, { signals: rows });
});

adminApi.post('/signals/:id/ack', async (c) => {
  const now = nowSeconds();
  const result = await c.env.LICENSE_DB.prepare(
    'UPDATE signals SET acked_at = ?2, acked_by = ?3 WHERE id = ?1 AND acked_at IS NULL',
  ).bind(Number(c.req.param('id')), now, c.get('admin')).run();
  return ok(c, { acked: (result.meta.changes ?? 0) > 0 });
});

adminApi.get('/stats', async (c) => {
  const now = nowSeconds();
  const [licenses, environments, trials, signals] = await Promise.all([
    all<{ status: string; is_trial: number; n: number }>(c.env,
      'SELECT status, is_trial, COUNT(*) AS n FROM licenses GROUP BY status, is_trial'),
    all<{ kind: string; n: number }>(c.env,
      `SELECT kind, COUNT(*) AS n FROM environments WHERE status = 'active' GROUP BY kind`),
    one<{ n: number }>(c.env, 'SELECT COUNT(*) AS n FROM trials WHERE created_at > ?1', now - 30 * DAY),
    one<{ n: number }>(c.env, 'SELECT COUNT(*) AS n FROM signals WHERE acked_at IS NULL'),
  ]);
  return ok(c, { licenses, environments, trials_30d: trials?.n ?? 0, open_signals: signals?.n ?? 0,
    server_time: now });
});

adminApi.post('/cron', async (c) => {
  const body = await readJson(c).catch(() => ({} as Record<string, unknown>));
  const task = str(body, 'task', 40) as Task | null;
  const report = await runMaintenance(c.env, nowSeconds(), task ?? undefined);
  await record(c.env, nowSeconds(), { actor: actorOf(c.get('admin')), kind: 'cron.manual', payload: { task } });
  return ok(c, report);
});
