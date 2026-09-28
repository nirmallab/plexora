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
        S3 budget_daily upsert RETURNING ──▶ 202 {accepted, rejected, dropped, duplicate, rotate, config}
cron */15 ─▶ one slice of one step: fold → export → verify → purge_batches → purge_rollups
             → archive_sweep → finalize (+ r2_reconcile on Sundays, monthly_export/verify on the 1st)
```

- **The vectors are the allowlist.** `vectors/plexora-vectors.json` is
  generated from the Python client's `schema.py` (`scripts/sync_telemetry_vectors.py`;
  never hand-edit). `src/telemetry/schema.ts` is a generic interpreter of its
  type language (`enum, pattern, int, bool, hist, list, map, struct`). Nothing
  in the Worker names a field for validation. An invalid event is rejected
  whole (counted in `rejected`); it is never trimmed into shape. Unknown keys
  are never copied. Diagnostics-level fields are stripped when the effective
  level is anonymous (client `mode`, `client_config`, or budget state).
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
  SameSite=Lax; Path=/admin`, `Secure` on https). Every page is HTML or JSON by
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

## Runbook (the owner runs this; nothing here has been deployed)

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
clean with `rejected == 0`; it is the cross-language contract.
