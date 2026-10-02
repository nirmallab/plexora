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
- It catalogues them at a **nominal** test price (`--price`, labelled as
  such), and publishes `text_*` and `vision_*` routes at rank 0, with the
  fallback at rank 1, unbenched.
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
- Changing models: `/admin/ai` -> Import a model from OpenRouter, then Use one model for everything. Before a paid
  model, revisit the allowance (it becomes real spend per seat).

### Settings and limits (2026-10-02, `45cdcb2c`, version `e3044ee8`)

Bookmark `00000015-00000000-000050f8-1a65bc900b26e1841b9e95f08293d723` and an export first; `schema.sql` added
`ai_settings` and `ai_account_limits`; deployed. The switch, the daily limits and the AI knobs are now set on
`/admin/ai` (rows in `ai_settings`, over `[vars]`), an account's own limits on its licence page.

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
