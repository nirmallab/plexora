# Plexora telemetry backend

One Cloudflare Worker (Hono + TypeScript) with one D1 database and one R2
bucket. It serves `/v1/telemetry/*` (the Plexora client), `/healthz`, and
`/admin/*` (dashboard and API), and runs one quarter-hourly cron. It is not
part of the Python wheel and shares no code with SCIMAP Pro's backend.

## Architecture

```
client ──POST /v1/telemetry/register──▶ token (PLEXORAT1.<b64url payload>.<HMAC>)
client ──POST /v1/telemetry/events (gzip JSON)──▶ cleanBatch (vectors) ──▶ one db.batch():
        S1 batches row (events_json + events_gz)   S2 installs upsert (only with session.summary)
        S3 budget_daily upsert RETURNING ──▶ 202 {accepted, rejected, dropped, unknown, duplicate, rotate, config}
cron */15 ─▶ one slice of one step: fold → export → verify → purge_batches → purge_rollups
             → archive_sweep → finalize (+ r2_reconcile on Sundays, monthly_export/verify on the 1st)
```

- **The vectors are the allowlist.** `vectors/plexora-vectors.json` is
  generated from the Python client's `schema.py` (`scripts/sync_telemetry_vectors.py`;
  never hand-edit). `src/telemetry/schema.ts` is a generic interpreter of its
  type language (`enum, pattern, int, bool, hist, list, map, struct`). Nothing
  in the Worker names a field for validation. The vectors are compiled in at
  deploy time, so a Plexora release can know words a deployed Worker does
  not; the interpreter is built for that. The wire format decides *shape*
  and the vectors decide *vocabulary*:
  - a shape fault (wrong JSON type, unknown envelope or row key, bad count or
    histogram, unknown event type) rejects the event, counted in `rejected`;
    in the envelope or client block (not an object, no `install_id`) it
    rejects the batch or registration with 400;
  - a vocabulary fault (a field the vectors do not name, a value they do not
    allow) drops just that value, counted in `unknown`, and keeps the rest. A
    counter row with an unknown key or dim goes whole (dropping the dim
    would merge buckets), and so does a list item or map entry, so a plugin
    with an unknown id is dropped rather than kept as `{version}`.

  Nothing unknown is ever stored, whichever way it fails. Diagnostics-level
  fields are stripped when the effective level is anonymous (client `mode`,
  `client_config`, or budget state). `/healthz` reports `schema` and
  `vectors`, the fingerprint of the allowlist this deploy was built with
  (also in the Usage page's header); `unknown > 0` in a 202 means the
  Worker is behind the client and should be redeployed.
- **Ingest writes ≤ 3 D1 rows** (2 without a session summary). Every table is
  `WITHOUT ROWID` and there are no secondary indexes, so each write is one
  row. Both are asserted in the tests.
- **The fold is SQL.** `rollup.ts` runs `INSERT … SELECT … FROM batches,
  json_each(events_json) … ON CONFLICT DO UPDATE SET col = col + excluded.col`.
  The first slice of a day clears its rollups (idempotent re-folds). Slices
  partition by install, so `COUNT(DISTINCT install_id)` adds up correctly.
  The slice and its cursor commit in one `db.batch()`.
- **Archives.** Each batch row carries its JSONL line as one gzip member
  (`events_gz`, stored as base64 TEXT because D1 returns BLOBs as number
  arrays). A daily archive is a byte concatenation of those members: a
  multi-member gzip, which `zcat`, Python's `gzip` and node's `zlib` read as
  one stream. Verify HEADs every part (size + `customMetadata.sha256`).
  `purge_batches` deletes a day only when its archive is verified and its fold
  is done. The one exception is R2 at HARD past 45 days, which is logged as a
  `failed` `purge_unverified` row.
- **Admin.** Access JWT (RS256, verified against the team certs) → `Bearer
  ADMIN_TOKEN` (constant-time) → a stateless HMAC cookie (`HttpOnly;
  SameSite=Lax; Path=/admin`, `Secure` on https). The token sign-in
  (`POST /admin/login`) takes 10 attempts an hour per address and 30 in all
  (`src/admin/limit.ts`; counters in `register_quota`). Every page is HTML or JSON by
  `Accept`. Mutations need JSON and same-origin `Sec-Fetch-Site`/`Origin`. The
  CSP is `default-src 'none'`, with the one inline script and stylesheet pinned
  by hash. Charts are inline SVG and nothing loads from outside.

## Budget

The account's free tier is shared with SCIMAP Pro, so every limit is a
configurable **share** in `[vars]`: 40k requests, 40k D1 writes and 2M D1 reads
per day. The ratio is `max(requests, writes, reads)` over the shares.

| ratio | state | ingest | config reply | cron |
|---|---|---|---|---|
| < 0.5 | ok | normal | base | all |
| ≥ 0.5 | warn | normal | base | all |
| ≥ 0.7 | aggregate | normal | interval ×3, sample ≤ 0.5 | all |
| ≥ 0.85 | reduce | drop priority ≥ 6, strip diagnostics | anonymous, ×3, ≤ 0.25 | fold, export/verify, purges |
| ≥ 0.95 | critical | keep priority ≤ 3 | anonymous, ×6 | export/verify, purges |
| ≥ 1.0 | stop | 503 + Retry-After to midnight, body unread | disabled_until midnight | tick reads and returns |

Ingest spends no read on this. It refreshes a per-isolate cache from the
`RETURNING` row of its own budget upsert. Requests that write nothing are
counted in memory and ride on the next upsert. A D1 error matching
`/limit|exceed|quota/` forces `stop` for the rest of the UTC day. `/healthz`
reports the cached state and never touches D1. The first ceiling reached is
always D1 writes (see plan B.9). The escape hatch is Workers Paid: raise the
shares and slice knobs, and nothing else changes.

Every response to the client that is a 2xx, 429 or 503 carries `config`
with exactly `level_max, upload_interval_s, sample, disabled_until`.

## Runbook (the owner runs this; deployed at https://analytics.plexoraapp.com)

### Deploying

Nobody needs to remember to. The Worker is deployed:

- by `scripts/release.py` before anything is built for a new version:
  `bump`, `all --release` and `ci` each compare this checkout's vectors
  fingerprint with the live `/healthz` and deploy `backend/` (typecheck,
  tests, `wrangler deploy`, then wait until `/healthz` reports the new
  fingerprint) when they differ. A dev build (`all --dev`) only warns;
- by `.github/workflows/backend.yml` on every push to `main` that changes
  `backend/`, once the two GitHub secrets below exist.

By hand, from a machine that has run `npx wrangler login`:

```bash
python scripts/release.py telemetry           # deploy if the live Worker is behind
python scripts/release.py telemetry --check   # only say; exit 1 if it is behind
python scripts/release.py telemetry --deploy  # deploy anyway (a Worker-only change)
```

### One-time setup

1. **Each release machine:** `cd backend && npm ci && npx wrangler login`.
   `python scripts/release.py doctor` shows `wrangler login` with the account,
   or `NOT logged in`.
2. **CI (optional, for the push-to-main deploy):** an API token from the
   Cloudflare dashboard's "Edit Cloudflare Workers" template, plus
   *Account · D1 · Edit*, scoped to this account and the `plexoraapp.com` zone;
   then `gh secret set CLOUDFLARE_API_TOKEN` and
   `gh secret set CLOUDFLARE_ACCOUNT_ID` (the account id from
   `npx wrangler whoami`). Until they exist the deploy job logs a notice and
   does nothing.

### Initial setup (once per Cloudflare account)

```bash
cd backend && npm i
npx wrangler d1 create plexora-telemetry            # paste database_id into wrangler.toml
npx wrangler r2 bucket create plexora-telemetry-archives
npm run db:init                                     # schema.sql → remote D1
npx wrangler secret put TELEMETRY_HMAC_KEY          # openssl rand -base64 32
npx wrangler secret put IP_HASH_KEY
npx wrangler secret put ADMIN_TOKEN
# optional: ACCESS_TEAM_DOMAIN / ACCESS_AUD in [vars], ADMIN_COOKIE_KEY secret, routes = [...]
npx wrangler deploy
curl -s https://<telemetry host>/healthz
```

`ENVIRONMENT = "production"` in `[vars]`: with `TELEMETRY_HMAC_KEY` or
`IP_HASH_KEY` unset, registration answers 503, no token verifies and the token
sign-in is refused. Only a non-production Worker (`.dev.vars`) falls back to
the development stand-ins in `src/env.ts`. A second registration of an install
id moves its `last_seen` only; the description changes through its uploads.

To rotate the HMAC key, move the old value to `TELEMETRY_HMAC_KEY_PREVIOUS`
and set a new `TELEMETRY_HMAC_KEY`. Old tokens keep working and get
`rotate: true`, so clients re-register.

Local loop: `cp .dev.vars.example .dev.vars`, `npm run db:init:local`,
`npx wrangler dev`, then run Plexora with
`PLEXORA_TELEMETRY_ENDPOINT=http://127.0.0.1:8787`. To trigger a tick, use
`npx wrangler dev --test-scheduled` and
`curl "http://127.0.0.1:8787/__scheduled?cron=*/15+*+*+*+*"`, or
`POST /admin/api/maintenance/run`.

## Tests

`npm test` runs both projects. The node project (`test/*.test.ts`) covers the
vectors, the fixture contract, the interpreter, tokens and percentiles. The
workers project (`test/routes/*.test.ts`) applies `schema.sql` in workerd and
covers ingest, budget, fold, archive, retention, cron, admin and auth.
`npm run typecheck` runs `tsc --noEmit`.
`test/fixtures/sample-batch.json` is generated by the Python client and must
clean with `rejected == 0` and `unknown == 0`; it is the cross-language
contract.
