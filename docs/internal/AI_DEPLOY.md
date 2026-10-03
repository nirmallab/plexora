# Plexora AI gateway: staging and production rollout

The gateway is part of the licence Worker (`licensing/`). Deploying it means
deploying the licence service, so production goes through a staging copy
first. For what the gateway does, see `AI_HARNESS.md`.

| | Production | Staging |
|---|---|---|
| wrangler | top level of `licensing/wrangler.toml` | `[env.staging]` (`--env staging`) |
| Worker | `plexora-licensing` | `plexora-licensing-staging` |
| Host | `license.plexoraapp.com` (custom domain) | `plexora-licensing-staging.<subdomain>.workers.dev` only (`routes = []`) |
| D1 / R2 | `plexora-license` / `plexora-license-backups` | `plexora-license-staging` / `plexora-license-staging-backups` |
| Signing key | `px1` (`px2` standby), trusted by every Plexora build | `pxs1`, trusted by **no** build |
| Admin | Cloudflare Access, with `ADMIN_TOKEN` as break-glass | `ADMIN_TOKEN` only (no Access app on workers.dev) |
| Unbenched routes | allowed (`AI_ALLOW_UNBENCHED_ROUTES = "1"`): any catalogued model | same |
| Mail | Resend | logged, never sent (no `RESEND_API_KEY`) |

The staging config has three traps, and `tests/test_licensing_staging_config.py` checks all three:

- wrangler **inherits** `routes` into an environment. Without
  `routes = []`, a staging deploy would claim `license.plexoraapp.com`.
- wrangler does **not** inherit `vars`. Every knob is repeated in
  `[env.staging.vars]`, and the test fails when the two lists drift.
  A knob added to `[vars]` must be added to both.
- Staging's key id and public key must not appear in production's
  `PUBLIC_KEYS_JSON` or `plexora/licensing/keys.py`. A staging certificate
  can then never unlock a real install, even though anyone holding the
  staging `ADMIN_TOKEN` can issue them.

`tools/ai_staging.py` refuses to act if any of those fail. It also refuses
when the database id is still the placeholder, and when a URL is the
production host.

## Staging: one-time setup

Run this on the machine that holds `~/.plexora-staging/`. That directory
lives outside the repository and outside Dropbox. It holds the staging
signing key, the staging admin token and `state.json`: the URL, plus the test
account and its seat key.

```
cd licensing && npx wrangler login && cd ..       # the Cloudflare account that holds production
python tools/ai_staging.py status                 # read-only; run it between steps
python tools/ai_staging.py create                 # D1 + R2; writes database_id into wrangler.toml (commit it)
python tools/ai_staging.py secrets --dry-run
python tools/ai_staging.py secrets
python tools/ai_staging.py schema                 # schema.sql -> staging D1, before the first deploy
python tools/ai_staging.py deploy                 # records the workers.dev URL, fixes PUBLIC_BASE_URL (commit it)
python tools/ai_staging.py seed                   # free models, routes, test account with `ai`, 2000 credits
```

The signing key (`pxs1`) is already generated. Its public half is in
`wrangler.toml`, and the private half is in
`~/.plexora-staging/signing-keys.json`, on the Mac that generated it. To move
staging to another machine, copy that file (it is staging-only). If it is
lost, generate a new one with:

```
python licensing/tools/generate_keys.py --out ~/.plexora-staging/signing-keys.json --kid pxs2
```

Then put the new public half in `wrangler.toml`, set `ACTIVE_KID`, and run
`secrets --rotate SIGNING_KEY_PXS2`.

### Secrets `secrets` sets (`wrangler secret put <NAME> --env staging`)

| Secret | Source |
|---|---|
| `SIGNING_KEY_PXS1` | `~/.plexora-staging/signing-keys.json`, checked against `PUBLIC_KEYS_JSON` |
| `FP_PEPPER`, `IP_HASH_KEY`, `SESSION_KEY`, `AI_USER_PEPPER` | fresh random values |
| `ADMIN_TOKEN` | fresh random value, saved to `~/.plexora-staging/admin-token` (0600) before it is put |
| `OPENROUTER_API_KEY` (and `ANTHROPIC_`, `OPENAI_`, `ORCAROUTER_`, `SAYGM_API_KEY` if present) | environment, else `licensing/.dev.vars` |

All values go to wrangler on stdin and are never printed. A secret that is
already set is kept unless named in `--rotate`. Never set on staging:
`SIGNING_KEY_PX1/PX2` (the tool refuses), `RESEND_API_KEY` (the tool
refuses), and `KEY_VAULT_KEY`. Never reuse a production value for staging:
staging's peppers, tokens and key are independent.

`licensing/.dev.vars` (the OpenRouter key) is on the Windows machine only.
On another machine, export `OPENROUTER_API_KEY` for the `secrets` step.

### Seeding

`seed` is idempotent: run it again after any reset.

- It reads OpenRouter's public model list and picks free text, vision and
  fallback models, exactly as the e2e does (`--text-model` and the other
  flags override).
- It approves them at a **nominal** test price (`--price`, labelled as
  such), each with one OpenRouter route, and assigns the vision model (then
  the fallback) to every task (`*`) and the text model to
  `gating.biological_context`, unbenched.
- It issues or reuses one licence (`staging-test@lab.example.org`, 2 seats,
  `entitlements: ["ai"]`), sets its AI mode (`credits`, or `--dev` for the
  at-cost dev route), and grants `--credits`. The grant uses
  `journal_id = staging-seed:<account>:<credits>`, so a re-run never posts it
  twice.

## Staging: the checks

```
python tools/ai_e2e.py --live --remote staging                 # all nine checks
python tools/ai_e2e.py --live --remote staging --skip gating,qc  # on a tight free-tier budget
```

`--remote` runs the same checks as the local run against the deployed Worker:

- It issues a fresh licence per run, so the accounting check is exact.
- It activates this process against staging, trusting `pxs1` in memory only.
- It reads balances and ledgers with `wrangler d1 execute --env staging --remote` on the staging database, by name.
- The stub cannot be used: Cloudflare cannot reach it. `--remote` needs `--live`.
- The routes it publishes replace staging's at the same slots. Run `seed` afterwards if that matters.

OpenRouter's free accounts allow about 50 requests a day. A full run with
gating and QC can use most of that. The 429s it hits are what the `retries`
check wants to see.

**Tested so far:**

- Locally, `wrangler dev --env staging --local`, with the real staging config
  and `pxs1` key and the stub as provider, ran through `RemoteWorker`: stream,
  structured, retries, shadow, tool_use, gating, failover and accounting all
  passed. `seed`, run twice, posted one grant.
- **Nothing has been deployed.** No Cloudflare resource exists for staging
  yet, so `create` and everything after it has not run.

A real Plexora install cannot talk to staging, because no build trusts
`pxs1`. That is deliberate: do not add a "trust this key" environment
variable, since it would be a licence forgery switch. Interactive testing
against staging goes through the e2e's in-process activation.

## Production: what was done (2026-10-02)

- Restore point: Time Travel bookmark `0000000e-00000000-000050f8-4b6a6ed3a12ae1ef3e82a782eea50738`, and a full
  export in `~/.plexora-prod-backups/` on the Mac (customer data; never the repo).
- `schema.sql` applied: 21 -> 33 tables, licences and environments untouched.
- Secrets added: `OPENROUTER_API_KEY`, `AI_USER_PEPPER` (fresh). No `ANTHROPIC_API_KEY` yet.
- Deployed `f8b7deed` (version `e08cb153`). The first try was refused: Workers Free allows 64 variables per Worker,
  secrets included, so `[vars]` now holds only values that differ from `src/env.ts` DEFAULTS.
- Production accepts any catalogued model (`AI_ALLOW_UNBENCHED_ROUTES = "1"`) and includes 2000 credits per seat
  per month (`AI_ALLOWANCE_PER_SEAT_MICRO`): a run reserves its quote, so without it no seat could start one.
- Models and routes set in the admin page (`/admin/ai`): `openrouter/dots-studio/dots-3-note-preview:free` rank 0,
  `openrouter/google/gemma-4-31b-it:free` rank 1, every capability. Both are $0, so AI is free to users for now.
- Checked: `plexora ai credits` on an activated install gets a token and shows the allowance; one text call was
  served by dots at $0. dots is a reasoning model: a `max_tokens` below a few hundred can return no text.
- Changing models (before schema v4): `/admin/ai` -> Import a model from OpenRouter, then Use one model for
  everything. After v4, see "Task routing and the catalogue" below. Before a paid model, revisit the allowance (it
  becomes real spend per seat).

### Settings and limits (2026-10-02, `45cdcb2c`, version `e3044ee8`)

Bookmark `00000015-00000000-000050f8-1a65bc900b26e1841b9e95f08293d723` and an export first; `schema.sql` added
`ai_settings` and `ai_account_limits`; deployed. The switch, the daily limits and the AI knobs are now set on
`/admin/ai` (rows in `ai_settings`, over `[vars]`), an account's own limits on its licence page.

### OpenRouter backend pinning (not yet deployed)

OpenRouter keeps a session on one backend only while that backend prices cache reads below input, so on the free models every call can land on a different host and read nothing from cache. The gateway now records the `provider` OpenRouter reports for each (account, session, route) in `ai_sticky_upstream`. Later calls in that session ask for the same backend first (`provider.order`, with `allow_fallbacks: true`). A pin lapses after 30 minutes and is pruned with `ai_sticky`.

- Run `npm run db:init` (and `db:init:staging`) before the deploy. Only the new table is added, as schema version 3.
- The code tolerates a database that doesn't have the table yet: no pin is used, and the prune skips the table. So the order is safe either way.

### OrcaRouter session header and SayGM benched (not yet deployed)

SayGM is benched: its confidential models are routed per request across competing operators, with no documented affinity, and cache hits fell to about 1 in 6 under load (measured 2026-10-02). The candidate replacement is OrcaRouter with `anthropic/claude-sonnet-5`: 19 of 19 calls read the cached prompt, with no errors, 4 at a time included. OrcaRouter's Gemma 4 models returned "upstream busy" 429s on most calls, so they are not a fallback.

- The gateway now sends `X-OrcaRouter-Session-Id` (the per-session `cacheKey`) on every OrcaRouter call: OrcaRouter's Session Affinity keeps a session on one upstream deployment and key. No schema change.
- The harness now caches each worker's history too (`wire.with_breakpoints`); that ships with the client, not the gateway.
- To switch: deploy, then on `/admin/ai` catalogue `orcarouter/anthropic/claude-sonnet-5` with OrcaRouter's prices (cache write at 1.25× input) and Switch serving to it with no fallback.

### Task routing and the catalogue (schema v4, deployed 2026-10-02, `0f8ebcaa`, version `657babfd`)

Production: bookmark `0000003e-00000000-000050f9-dc8c0720885c2b33db3e0a87ec3fe5ba` and an export in
`~/.plexora-prod-backups/` first; `db:init`, `deploy`. The v3 table serves until Settings › Migrate. Staging was
migrated the same day (version `82e0fd1e`): every legacy chain copied, task and model recorded on live calls.
This deploy also shipped the two sections above (OpenRouter backend pinning, the OrcaRouter session header).

The admin becomes six pages (Overview, Models, Providers, Task routing, Usage & cost, Settings) over three layers:
approved models, each with up to three provider routes, assigned to tasks by module. Prices are read from the
aggregators nightly. `AI_HARNESS.md` describes the model; this is the rollout. Nothing changes for users until
the migration, and the migration changes no model that serves.

1. Staging first (`python tools/ai_staging.py status`): `cd licensing && npm run db:init:staging`, then
   `npm run deploy:staging`. `GET /admin/api/ai/schema` should say `schema_version: 4`,
   `ai_requests_has_task_columns: true`, `legacy.serving: true`.
2. `POST /admin/api/ai/migrate-legacy` (or Settings › Migrate). The reply lists every assignment it made; `GET
   /admin/api/ai/tasks` shows `*` with the old default chain and any feature-specific chains as task rows. Then
   `python tools/ai_e2e.py --live --remote staging`, and `POST /admin/api/ai/pricing/refresh`:
   `GET /admin/api/ai/pricing/status` should show each aggregator read within the hour.
3. Production (the admin page; Claude has no production admin token): a Time Travel bookmark and an export as
   above, `npm run db:init`, `npm run deploy` (the v3 table still serves), then `/admin/ai/settings` › Migrate.
   Check one call in Usage & cost › Recent calls (task and model set), then Providers › Refresh prices.
4. The client starts naming tasks with the release that carries `plexora/ai/tasks.py`. Older clients keep
   working: without a task they resolve by their feature (`gating.*`, then `*`).
5. The release after: delete `src/ai/routing_legacy.ts`, `hasTaskRouting`'s fallback, `/migrate-legacy`, and the
   `ai_models`/`ai_routes` tables (schema 5).

The migrated models keep their prices as they were: an OpenRouter model imported from its list is refreshed from
then on; one typed by hand stays typed. A migrated model whose reasoning ability the provider list does not state
is flagged on Task routing where a task needs it: tick Reasons on the model's page if it does.

## Capacity monitor (`/admin/ai` Overview; deployed 2026-10-03, `1d48560f`, version `a7de96aa`)

Production: bookmark `00000042-00000000-000050f9-d214996f75d9baf0df448fb75845a829` and an export in
`~/.plexora-prod-backups/` first; `db:init` (adds `ai_requests_started`), `deploy`. Staging the same day (version
`b6b7ac1a`). `CF_ANALYTICS_TOKEN` is set on both. Checked on staging's live endpoint: every Cloudflare row measured,
no analytics errors. The CPU p99 read 249 ms on plexora-licensing (2026-10-02) with no request stopped, so the card
grades it watch.


The Capacity card on the `/admin/ai` Overview grades every limit that would make users wait, ok / watch / act, from the busiest
day and minute of the last 7 days: Workers requests and CPU, D1 rows written and read, D1 size, D1 queries per
second, the provider's 429 share and written limits, free models serving, and capabilities with no fallback. The
headline says "Time to move to Workers Paid" once any Free limit passes 80%. The same data is
`GET /admin/api/ai/capacity`. Code: `licensing/src/ai/capacity.ts`.

- Free limits are account-wide, so the telemetry Worker counts. Without analytics the card estimates from AI calls
  alone (about 18 rows written, 80 read and 32 queries per call, calibrated on production 2026-10-02) and says so.
- To measure: create an API token with only Account > Account Analytics > Read, then
  `npx wrangler secret put CF_ANALYTICS_TOKEN` (and `--env staging`). `CF_ACCOUNT_ID` is already in `[vars]`.
  If a dataset is refused, the card names it and falls back to the estimate for those rows.
- After upgrading Cloudflare, switch "Cloudflare account is on Workers Paid" on under Settings › Capacity. Enter the
  provider's written per-minute and per-day limits there too.
- Deploy: `npm run db:init` first. It adds the `ai_requests_started` index, so the card's time-window reads don't
  scan every call row. The code works without it, only slower.

### Admin redirect fix and Remove model (deployed 2026-10-03, `a0306ae3`, version `36b26f62`)

UI only, no schema: no bookmark or `db:init`; `npx wrangler rollback` undoes it. Every `next={…/{model.id}}` redirect
had gone to the literal placeholder (a template-literal escape in `CLIENT_JS`); Remove is now on a model's page and
on each unused row of the list. Checked: the live script serves `/\{([\w.]+)\}/g`.

## Effort per model, and the API page (deployed 2026-10-03, `c8d50171`, version `fe7cb25d`)

**Effort.** A task route asks for `auto`, a level of `none minimal low medium high xhigh max`, or nothing (the
model's default). Each model of the chain is sent the nearest level it takes, in its own form. The per-model
records are `licensing/src/ai/effort.ts` `PROFILES`; keep that file current when a model ships. An admin can
override a model's record on its page. Defaults per task are `effort:` in `plexora/ai/tasks.yaml`. Rows saved
before this keep "model default", so set the tasks to Auto on /admin/ai/routing to use them. A provider that
refuses the effort is asked once more without it, and the gateway records `ai.effort_rejected`.

**Keys.** /admin/ai/api checks a key with the provider (an empty request, so nothing is billed), seals it with
`KEY_VAULT_KEY`, and uses it ahead of the Worker secret. Removing the key hands the provider back to the
secret. It needs `KEY_VAULT_KEY`: production has it, staging deliberately does not, so staging keys stay
secrets. The `ai_provider_keys` table is left out of the nightly backup, so re-enter keys after a restore.

Deploy:

1. `npm run db:init`. This adds the `ai_provider_keys` table, and in a fresh database the effort columns.
   `ensureLateColumns` adds `ai_requests.effort`, `ai_catalog.effort_json` and `ai_task_routes.effort_spec`
   on the first request either way.
2. `GET /admin/api/ai/schema` should show `effort_columns: true, provider_keys_table: true`.
3. `npm run deploy`.

Done 2026-10-03. `db:init` created `ai_provider_keys`, and the first gateway requests after the deploy added the
three late columns (checked with `pragma_table_info` on the remote database). Every production task route still
has "model default" effort until it is set to Auto on /admin/ai/routing. `npx wrangler rollback` undoes the code.
The new columns and table are unused by the old code, so they can stay.

## Production rollout (the original plan, for reference)

Preconditions:

1. `feature/ai-harness` (with `feature/ai-staging`) merged into `main`, with
   conflicts resolved. Licensing vitest, the AI Python suites and the
   gating/QC sessions are green on the merge.
2. `tools/ai_e2e.py --live --remote staging` passes, gating and QC included.
3. A decision on which provider serves first. Production refuses unbenched
   routes, so with no route rows every capability uses the built-in
   **Anthropic** default. That needs only `ANTHROPIC_API_KEY`, and no rows
   in `ai_models` or `ai_routes`. Any other provider needs a passing
   `plexora ai route-bench ... --submit` first. Run the bench on staging to
   rehearse it; run it on production to publish.

Steps, from `licensing/` on the merged `main`:

1. **Record a restore point.** Run `npx wrangler d1 time-travel info plexora-license` and note the bookmark. Also take a dump: `npx wrangler d1 export plexora-license --remote --output ~/plexora-license-pre-ai.sql`, kept outside the repository and Dropbox, because it holds customer data.
2. **Schema, before the code.** Run `npm run db:init`. It only adds the `ai_*` tables: `CREATE ... IF NOT EXISTS`, and the branch changes no existing table. The order matters. The new cron prune deletes from `ai_idempotency`, `ai_requests` and `ai_sticky` in the same D1 batch as the licence pruning, so code deployed before the tables exist would fail the whole nightly prune.
   - Check it with `npx wrangler d1 execute plexora-license --remote --command "SELECT name FROM sqlite_master WHERE name LIKE 'ai_%'"`.
3. **Secrets.** The existing licence secrets stay as they are: `SIGNING_KEY_PX1/PX2`, `FP_PEPPER`, `IP_HASH_KEY`, `ADMIN_TOKEN`, `SESSION_KEY`, `KEY_VAULT_KEY` and `RESEND_API_KEY`.
   - New: `npx wrangler secret put ANTHROPIC_API_KEY`. Use Plexora's own org key, with a spend limit set in the Anthropic console.
   - New: `npx wrangler secret put AI_USER_PEPPER`. A fresh random value, not staging's.
   - Further provider keys only once their routes have benched.
4. **Deploy.** Run `npm run deploy`. This ships the whole licence Worker from `main`. Outside `src/ai/`, it changes the cron prune, `backup.ts`, `certs.ts` (the `PLXAI1` token), `http.ts`, the `/v1/ai` and `/admin/api/ai` mounts, and the export of `presented` in `v1.ts`.
5. **Smoke test, no spend.**
   - `GET /healthz`.
   - `GET /admin/api/ai/providers`: `anthropic` should show as configured.
   - `GET /admin/api/ai/routes`: `default_serving` should be anthropic for every capability.
   - The existing licence paths: an activation and a refresh from a real install, and `POST /admin/api/cron` for the prune and backup (the backup includes the `ai_*` ledger tables, but skips holds, idempotency keys, sticky routes and circuits).
6. **Internal account.**
   - `PATCH /admin/api/ai/accounts/<acc_id>` with `{"mode":"dev","notes":"internal testing"}`.
   - `POST /admin/api/ai/accounts/<acc_id>/credit` with `{"credits":2000,"kind":"grant","journal_id":"internal-dev:<acc_id>:1"}`.
   - Then run `plexora ai run gating <project> --dev` from an install activated on that account. Expect real Anthropic spend.
7. **Entitlement.** `PAID_ENTITLEMENTS = ['ai']` is already on `main`, so new paid licences carry `ai`.
   - List existing licences without it (read-only): `SELECT id, entitlements_json FROM licenses WHERE is_trial = 0 AND entitlements_json NOT LIKE '%"ai"%'`.
   - Grant it per licence with `PATCH /admin/api/licenses/<id>` and `{"entitlements":[..., "ai"]}`. Certificates pick it up at their next refresh.
   - Granting `ai` costs nothing by itself: balances start at 0 and `AI_ALLOWANCE_PER_SEAT_MICRO = "0"`, so no account can spend until it is given credit.
8. **Client.** A Plexora release with the harness. It calls the licence server (`PLEXORA_AI_GATEWAY` overrides that), so nothing has to be configured on the client.

Rollback:

- `npx wrangler rollback` restores the previous Worker version. The `ai_*` tables can stay; the old code never reads them.
- To stop AI calls without a rollback: `POST /admin/api/ai/providers/anthropic/disable`.
- To stop one account: `PATCH /admin/api/ai/accounts/<id>` with `{"mode":"disabled"}`.

## Still to build before real users

- Credit purchase checkout (Paddle) and its webhook into `/admin/.../credit kind=purchase`.
- `POST /v1/ai/preflight`, an affordability check. Until it exists, the client's estimate is units × the price list.
- Daily cost reconciliation against the providers' cost reports. `reported_cost_micro` is recorded but not compared.
- A load test of concurrent runs on one account: the conditional-UPDATE holds under real D1 latency.
- A routing-bench pass on real models, for any non-Anthropic route.
- Merging into `main`, where conflicts are expected.
