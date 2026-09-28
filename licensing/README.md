# Plexora licence service

One Cloudflare Worker, D1 and R2, on the free tier:

- `/v1`: what the Plexora client calls (activate, refresh, deactivate,
  delegate, trial). Frozen once shipped.
- `/portal`: licence holders. Magic-link sign-in, seats and users, devices and
  environments, offline licences, licence tokens, billing and renewal, and
  the trial page.
- `/admin`: us. Manual issuance (the only way a licence is created until
  billing exists), extend, suspend, revoke, signals and stats.
- `/healthz` and a daily cron at 03:30 UTC: expiry, reminders, idle
  release, pruning, signals and backup.

It is fully independent of the telemetry Worker in `../backend`, with no
shared binding, database or bucket. Free users never talk to it.
Architecture and client behaviour are in [`docs/LICENSING.md`](../docs/LICENSING.md).

## Deploy

```sh
npm ci
npx wrangler d1 create plexora-license        # paste the id into wrangler.toml
npx wrangler r2 bucket create plexora-license-backups
npm run db:init                               # applies schema.sql remotely
# set the route and PUBLIC_BASE_URL in wrangler.toml, then:
npx wrangler secret put SIGNING_KEY_PX1       # see Keys
npx wrangler secret put FP_PEPPER             # openssl rand -base64 32
npx wrangler secret put IP_HASH_KEY           # openssl rand -base64 32
npx wrangler secret put SESSION_KEY           # openssl rand -base64 32
npx wrangler secret put ADMIN_TOKEN           # openssl rand -base64 32
npx wrangler secret put KEY_VAULT_KEY         # optional: base64 of 32 bytes; lets the portal re-show seat keys
npx wrangler secret put RESEND_API_KEY        # optional: without it mail is logged, not sent
npm run deploy
```

`DEFAULT_SERVER` in `plexora/licensing/store.py` must match
`PUBLIC_BASE_URL` (a test pins it), so a released Plexora knows where to go.
With it empty, every online action says "no licence service is configured",
and offline licence files still work. Put Cloudflare Access in front of `/admin/*`, and set `ACCESS_TEAM_DOMAIN`
and `ACCESS_AUD`, so the admin token is only a fallback.

Never rotate `FP_PEPPER`: every registered environment is known by a hash
under it.

## Keys

Certificates are signed with Ed25519. `tools/generate_keys.py --out <file>`
makes the `px1`/`px2` pair. It refuses to write inside this repository or into
anything that looks like a synced folder (Dropbox, iCloud, OneDrive, Google
Drive, Box). The private halves go into Worker secrets, typed in, and into one
offline copy in a password manager or on encrypted media. Then delete the
file. Only the public halves are in `plexora/licensing/keys.py` and in
`PUBLIC_KEYS_JSON`.

**Rotation.**

1. Ship a Plexora release that already trusts the standby kid (`px2` is in
   `keys.py` from day one).
2. `wrangler secret put SIGNING_KEY_PX2`.
3. Set `ACTIVE_KID = "px2"` and deploy. Signing is slot-strict: if the px2
   secret is missing, signing fails loudly rather than labelling px1
   signatures as px2.
4. Existing px1 certificates keep verifying from `PUBLIC_KEYS_JSON` and renew
   onto px2 at their next refresh.
5. After the longest certificate lifetime (`OFFLINE_MAX_DAYS`), delete
   `SIGNING_KEY_PX1` and destroy its offline copy.

To retire a kid everywhere, remove it from `keys.py` in a release; its
certificates then read as `unknown_key`, which is Free.

`tools/issue_license.py` signs one certificate outside the service. It is for
development (with a throwaway key) or an emergency offline issuance. Nothing
it makes is recorded.

## Limits, in plain numbers

Every threshold is a `[vars]` knob in `wrangler.toml`:

| Knob | Default | Meaning |
|---|---|---|
| `CERT_MAX_DAYS` | 90 | certificate lifetime; also how long revocation takes to reach an environment that never comes back online |
| `CERT_RENEW_WINDOW_DAYS` | 21 | renewed at refresh inside this window |
| `REFRESH_WRITE_INTERVAL_DAYS` | 7 | a refresh writes at most once in this interval, and otherwise writes nothing |
| `DEFAULT_GRACE_DAYS` | 14 | Paid keeps working this long after expiry (trials get none) |
| `DEFAULT_ENVS_PER_SEAT` | 2 | a laptop and a cluster |
| `COOLDOWN_HOURS` | 48 | between environment removals on a seat. Exempt: the first removal, one unseen for `STALE_ENV_EXEMPT_DAYS`, owners and admins in the portal |
| `IDLE_ENV_RELEASE_DAYS` | 180 | online environments unseen this long are released |
| `OFFLINE_DEFAULT_DAYS` / `OFFLINE_MAX_DAYS` | 180 / 366 | offline certificates cannot be recalled before they run out |
| `JOB_CERT_MAX_DAYS` / `CI_CERT_HOURS` | 7 / 24 | |
| `TOKEN_DEFAULT_TTL_DAYS` / `TOKEN_MAX_TTL_DAYS` | 90 / 365 | never past the licence |
| `TRIAL_DAYS`, `TRIAL_MAX_PER_FP`, `TRIAL_PER_IP_PER_DAY` | 30, 2, 5 | one trial per mailbox is a UNIQUE index |

Caps that matter are enforced by D1 itself: a conditional INSERT for
environments per seat and seats per licence, and a UNIQUE index for one trial
per mailbox. Rate limits are fixed windows in D1 rather than KV, because the
free tier allows 1,000 KV writes a day against 100,000 D1 writes. They guard
against abuse only. Revocation is likewise a status in D1 (licence, seat,
environment, token), read at refresh. There is no KV namespace at all: one
store to back up, and no second copy of the truth to drift.

**Free-tier budget.** Writes per environment per week are at most 2 (the
`last_refresh_at` update and its audit row). An activation is 2 or 3. Reads
are a handful per call. Signals and backups run once a day. At, say, 5,000
environments that is roughly 1,500 writes a day, well inside the account's
share of 100,000.

## Misuse signals

Signals are computed nightly into `signals`, shown at `/admin/signals`, and
**never read on a request path**. They are: environment overuse, release
churn, forged certificates per IP hash, revoked-token use, repeated
clock-rollback reports, trial fingerprint reuse, and, deliberately weak,
several IP hashes refreshing one environment. Usage volume is not a signal.
Refusing a new trial is the strongest automatic action anywhere; nothing bans
or revokes on suspicion.

## Backups and restore

Each day a gzipped JSON snapshot of every table except `sessions`,
`login_links` and `rate_limits` is written to
`backups/daily/YYYY-MM-DD.json.gz`. The last `BACKUP_DAILY_DAYS` are kept.
The first of each month is also copied to `backups/monthly/`, which is kept
indefinitely.

To restore:

```sh
npx wrangler r2 object get plexora-license-backups/backups/daily/2026-10-01.json.gz --file snap.json.gz
gunzip snap.json.gz
# create a fresh database from schema.sql, then load each table in
# snap.json's "tables" with INSERTs (a short script; columns are the keys)
```

## Tests

```sh
npm test            # node (canonical JSON, cross-language vectors, signing) + workers (every route, against local D1/R2)
npm run typecheck
```

`vectors/plexora-license-vectors.json` is written by
`python tools/make_test_vectors.py`. The vitest suite re-signs every payload
byte for byte, and the Python suite verifies every certificate. The route
tests sign with a throwaway key declared in `vitest.workers.config.ts`; no
production key is ever needed by a test.
