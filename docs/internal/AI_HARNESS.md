# Plexora AI: in-app harness and gateway (branches `feature/ai-harness`, `feature/ai-in-app`)

This is the first implementation of the architecture proposal
(`develop-a-detailed-technical-steady-moore.md`). Two pieces work today:

1. **Gateway.** It lives in the licence Worker (`licensing/`). Every model call
   is tied to the account, licence, seat and environment that made it, and is
   tracked next to the licences.
2. **Harness.** `plexora/ai/harness/` gates a project, or quality-controls its
   image, inside Plexora with no external agent. Each call goes through the
   gateway and is billed in Plexora AI credits.
3. **In-app runs.** The same runs start from the viewer (the agent panel's
   "Gate with Plexora AI" / "QC with Plexora AI"), from `/ai/v1`, or over MCP,
   as the `ai.*` capabilities.

## How to use it

```
plexora ai run gating <project> [--markers CD3,CD8] [--mode propose]
plexora ai run gating projA projB projC --parallel 3   # several sessions at once
plexora ai run gating <project> --resume <session>      # after a credit pause
plexora ai run gating <project> --dev [--model claude-sonnet-5]   # internal testing
plexora ai run qc <project> [--channels DAPI,CD3] [--mode propose]  # AutoQC of the image
plexora ai run qc <project> --resume <session>
plexora ai trace [RUN] [--cache]                         # calls, cache verdicts, credits
plexora ai credits [--days 30]                           # balance and usage
```

To use it, the machine needs an activated Paid licence that includes the
`ai` entitlement (or `ai:gating` / `ai:qc` for the CLI; the in-app routes and
`ai.*` capabilities need `ai`).

## Gateway (`licensing/src/ai/`, `licensing/src/routes/ai.ts`)

| Route | What it does |
|---|---|
| `POST /v1/ai/token` | Takes the environment certificate and binding (the same checks as `/v1/refresh`). Returns a `PLXAI1` token that lasts 30 minutes and is signed with the licence keys. The token's `mode` is `dev` only for accounts an admin has put in dev mode. |
| `POST /v1/ai/messages` | One streamed call. The client names a **capability**, never a model. The route checks the request against an allowlist and requires an `Idempotency-Key`. It holds credit for the call, streams the provider's events between `plexora.accepted` and `plexora.usage`, then settles on the **provider's** usage × `AI_MARKUP_BPS` (2.0×). |
| `POST /v1/ai/runs`, `/runs/:id/finish` | A quoted, capped run. The quote is the flat feature price (gating is 25 credits per marker) and is held at the start. The account pays min(metered, quote). Calls beyond the envelope are refused. |
| `GET /v1/ai/balance`, `/usage`, `/requests/:id`, `/pricing` | The account's own view of its balance, usage, individual calls and the price list. |
| `POST /v1/ai/dev/messages`, `/v1/ai/dev/runs` | **Dev route.** Only accounts in mode `dev` can use it. Calls are billed at provider cost with no markup, the caller may name any catalogued model, and every call is still tracked with `billing='dev'`. |
| `/admin/api/ai/usage`, `/requests`, `/accounts/:id` (GET, PATCH), `/accounts/:id/credit` | The tracking: usage, cost against charges (margin), balances, ledgers and runs. Admins also set an account's mode, markup and allowance here, and grant or adjust credit. Every grant is written to `events`. |

### Tables (schema v2 in `licensing/schema.sql`)

| Table | Contents |
|---|---|
| `ai_requests` | One row per call, written from the provider's usage. Each row copies the unit costs it was charged at. Nothing the model saw or said is stored. |
| `ai_balances` / `ai_holds` | The balance and the open holds against it. Credit is reserved with one conditional `UPDATE`, so concurrent calls cannot overspend. |
| `ai_ledger` | Signed entries for every credit movement. They always sum to the balance, and each `(journal_id, bucket)` pair is unique, so a grant or settlement that is retried is never posted twice. |
| `ai_runs`, `ai_idempotency`, `ai_accounts` | Runs, idempotency keys and per-account settings. |

### Deploying

```
cd licensing
npm run db:init                       # applies schema.sql (idempotent; adds the ai_* tables)
wrangler secret put ANTHROPIC_API_KEY
wrangler secret put AI_USER_PEPPER
npm run deploy
```

To make an internal testing account, run this with the admin token:

```
curl -X PATCH https://license.plexoraapp.com/admin/api/ai/accounts/<acc_id> \
     -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
     -d '{"mode":"dev","notes":"internal testing"}'
curl -X POST  https://license.plexoraapp.com/admin/api/ai/accounts/<acc_id>/credit \
     -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
     -d '{"credits":10000,"kind":"grant","note":"dev budget"}'
```

Then run `plexora ai run gating <project> --dev` on a machine activated with
that account's seat.

## Harness (`plexora/ai/harness/`)

- **Decision loop** (`decision.py`). Deterministic Python acts as the
  coordinator, and each packet is one structured-output call. One loop
  (`DecisionRun`) serves both workflows; a `Workflow` binds what differs:
  the session capabilities, plugins, answer models, prefix, unit of rotation
  and gateway feature.
  - `GatingRun` uses `gating_session_start/next/answer/status/finish`,
    feature `gating`, one unit per marker.
  - `QCRun` uses `qc_session_start`, `qc_next`, `qc_answer`,
    `qc_session_status` and `qc_session_finish`, feature `qc`, one unit per
    channel. Its workers rotate every 4 units (candidates, checks, cell
    modules) as well as on packets and tokens. It waits out the QC bulk pass
    (`bulk_running`) by polling `qc_next`.

  The answer is validated locally against the workflow's `answers.Answer`
  before it is submitted.
  An invalid answer gets one repair turn inside its worker; after that it is
  submitted as an engine strike.
- **Rolling workers.** A worker covers one marker by default. It also ends
  after 8 packets or about 60k tokens of context, whichever comes first, so
  context never grows to the 480k seen in the live run.
- **Cached prefix** (`prefix.py`). The identity text plus the reading guide as
  canonical JSON, with a single breakpoint. QC's (`qc_prefix`) adds the
  `qc-image` skill between them (placeholders filled from code constants). Its
  identity says that the harness, not the worker, calls the tools. The bytes are identical for every
  worker, session and user of a build. `CacheMonitor` gives each call a
  verdict of `cold`, `hit` or `miss`, and `plexora ai trace --cache` reports
  them.
- **Structured outputs** (`schema.py`). The pydantic answer models are
  reduced to the subset providers accept: closed objects and no numeric or
  length constraints. The dropped constraints are still enforced by local
  validation. A map field (`dict[str, X]`: gating's per-marker `verdicts`,
  QC's per-channel ones, `modules`, `strata`) has no closed form. It is sent
  as a list of `{key, value}` entries, and `schema.decode` turns it back into
  the object before validation. Before this change, maps were reduced to a
  closed empty object, so a provider that honours the schema could only have
  answered `{}`.
- **Orchestrator** (`orchestrator.py`).
  - Runs a `TaskGraph` with hard and soft dependencies and `ready_when`
    predicates.
  - Tasks can be spawned dynamically, up to `max_depth`.
  - A `Scheduler` runs up to `max_parallel` tasks at once. Its
    `stagger_first` option lets the first call warm the cache, so the prefix
    is written once rather than N times.
  - Tasks share state through a `Blackboard` of facts, artifact ids and a
    mailbox.
  - `run_many` uses it to gate several projects in parallel.
- **Pausing for credit.** When the gateway returns `insufficient_credits`,
  `run_envelope_exceeded` or a similar refusal, the session is **paused**,
  not abandoned, and its gateway run is left open. `--resume <session>`
  lifts the pause and continues the session under the same gateway run (no
  second quote).
- **Trace** (`trace.py`). `<data_root>/.agent/ai/trace.sqlite` records runs,
  model calls (tokens, cache verdict, credit, gateway request id) and tasks.

## In-app runs (`plexora/ai/harness/capabilities.py`, `plexora/server/routes/ai_routes.py`)

| Capability | Route | What it does |
|---|---|---|
| `ai.run_session` (`execution="job"`) | `POST /ai/v1/runs` | Starts a gating or QC run of the project on a job, or resumes one with `resume_session`. Args: `kind`, `project`, `markers`/`channels`, `mode`, `resume_session`, `dev`, `model` (dev only), `units_per_worker`, `start_options`. Answers `{job_id, run_id}`. The run id is the job id's (`air_<hex>` for `job_<hex>`). |
| `ai.run_status` | `GET /ai/v1/runs`, `GET /ai/v1/runs/<id>` | One run, by run id or job id: status, session, packets, credits charged, cache-read share, verdicts and the job's state. Without an id, the recent runs. |
| `ai.run_control` | `POST /ai/v1/runs/<id>/control` | `pause`, `resume` or `stop` the run's session through `sessions/control.py`, the viewer's own mechanism. |
| `ai.balance` | `GET /ai/v1/balance` | The balance and price list from the gateway. With `project` (and optionally `kind`, `markers`/`channels`), the estimate a run would be quoted: units × the feature's credits per unit, and whether it is affordable. |

- **Access.** All four capabilities need the `ai` entitlement.
  - The blueprint is guarded by `guards.guard_blueprint(ai_bp, "ai")`. Without a server token it answers loopback only, because a run spends the account's credit.
  - Every route calls `registry.invoke` with principal `viewer`, so receipts and audit lines match MCP's.
  - The capabilities are core ones (`agent/core/__init__.py`), so MCP agents see them too.
- **Events.** The job runs `GatingRun`/`QCRun` with the job's own call: its session, policy, audit, viewer link and notifier. So the session's own events (`gating.session` / `qc.session`) reach the open tabs as an external agent's do. The harness adds four events on the same channel:
  - `ai_run`: the quote;
  - `ai_usage`: packets, credits and cache-read share, once per packet;
  - `ai_paused`: the reason, the message, `top_up_url` and the `resume` arguments;
  - `ai_finished`.

  `call.check_cancelled` is checked before every model call and every `qc_next`, so cancelling the job pauses the session.
- **UI** (`views/agentPanel.js`, `css/agentPanel.css`).
  - The session card shows a usage line ("Plexora AI · 12 packets · 3.4 credits · 87% from cache").
  - A credit pause or a gateway error shows a card with two buttons, the limit card's pattern. Resume sends `POST /ai/v1/runs` with the event's `resume`. The other button is Add credits when the gateway gave a top-up page, and Not now otherwise.
  - The launcher has "Gate with Plexora AI" and "QC with Plexora AI". Each shows its estimate from `/ai/v1/balance` ("About 125 credits · 5 markers").
  - A launcher button is disabled, with the reason shown, when the licence lacks AI, the project cannot run that kind, or the balance cannot pay.
  - A "Plexora AI" chip in the viewer corner opens the launcher. It appears only when the page's licence hint includes `ai`, so Free users see no chip. `PlexoraAgentPanel.openLauncher()` opens the launcher either way.

### A QC deadlock this found

Any agent that answered while the QC bulk pass was still running hung there forever:

1. The bulk pass reports progress from inside an open image reader (`blur.run` → `bulk.announce`).
2. `announce` took the session lock.
3. Meanwhile, an answer drawing the next packet holds the session lock and waits for that reader.

The existing QC tests drain the job first, so they never saw it. The fix: `SessionStore.lock(timeout=)` now raises `SessionBusy`, and `announce` skips its report when the session is busy.

## Tests

- `licensing/test/routes/ai.test.ts` (19 tests, run with `npx vitest run`) uses a fake provider. It covers token issue and refusals, markup billing, ledger and balance consistency, provider request shape, the allowlist, idempotency, outage release, cut streams, entitlements, the dev route at cost with a model override, runs (quote cap, envelope, dev runs), admin tracking, purchase idempotency and the allowance draw order.
- `tests/test_ai_harness.py` (13 tests) uses `tests/ai_harness_fixtures.py::FakeGateway`, which speaks the real wire format and emulates the prompt cache. It covers:
  - gating a five-marker project end to end with the Oracle;
  - one call per packet and rotation between workers;
  - a single stable prefix with no cache misses;
  - pausing for credit and resuming;
  - the repair turn after an invalid answer;
  - parallel projects that write the prefix to the cache once;
  - the dev route;
  - retries with the same idempotency key;
  - the scheduler: parallelism, dependencies, spawning, depth bound and stagger.
- `tests/test_ai_harness_qc.py` (6 tests) uses `QCOracle` from `test_qc_session.py`. It covers:
  - the QC prefix: byte-stable, with the skill and the guide, and one breakpoint;
  - provider-ready schemas for every QC kind, and maps sent and decoded as entries (for gating too);
  - a QC run end to end: regions written, the result active, one call per packet, a declared `qc` run with one unit per channel, WebP images, a single prefix with no cache misses, and rolling workers;
  - pausing for credit, then resuming under the same gateway run;
  - `plexora ai run qc` from the command line.
- `tests/test_ai_routes.py` (9 tests) runs the Flask test client against FakeGateway, which it reaches through `PLEXORA_AI_GATEWAY` / `PLEXORA_AI_TOKEN`. It covers:
  - the entitlement guard and the loopback guard;
  - registration: all four `ai.*` capabilities are Paid, and the run is a job;
  - the estimate;
  - a QC run over HTTP that an open tab hears (both the session's and the harness's events), with its audit lines;
  - a credit pause: the tab is told what to resume, control works on the paused session, and the run resumes over HTTP;
  - a stop through control;
  - a gating run over HTTP.
- `tests/js/agent_panel_probe.mjs` has 5 new checks, listed in `tests/test_agent_client_probes.py`. They cover:
  - the launcher on Free and on Paid, with its estimates, and kinds that are unaffordable or unavailable;
  - the usage line;
  - the credit card, with Resume and Add credits;
  - the gateway-error card.

## Differences from the proposal

- **The gateway is in the licence Worker, not a separate Worker.** This is so
  that tracking lives with licensing, as asked. It can be split out later as
  a route move.
- **Balances use D1 conditional updates, not a Durable Object per account.**
  This is correct under concurrency and needs no new binding. A Durable
  Object is the scaling step once one account sends hundreds of calls per
  second.
- **The harness is synchronous and uses threads, not asyncio.** The registry
  and the gateway stream both block, and threads keep the code small.

## Not built yet (proposal phases 2–5)

- **Commercial:** Paddle checkout and webhooks, auto-recharge, spend
  policies, threshold emails, and a portal page for usage.
- **Providers:** OpenAI, OpenRouter, OrcaRouter and SayGM adapters; the
  per-module routing bench (`route_evaluations`, the publish gate, shadow
  runs); circuit breakers and failover; daily reconciliation against provider
  cost reports.
- **In-app:**
  - the signed policy bundle;
  - the conversational agent (chat panel, approvals, `spawn_agents` tools), which is being built on its own branch;
  - polling `/v1/ai/balance` while a run is paused for credit (today, resuming waits for the user to click Resume);
  - a `plexora.ai.run()` notebook entry.
- **Gateway:** the `/v1/ai/preflight` affordability check. Until it exists, the estimate is units × the price list.
- **Engine and caching:** the engine change that lets several packets of one
  session be outstanding at once (parallel markers within one image);
  `ToolResultCache`, offloading and the model-call cache.
- **Real provider:** no end-to-end run against the real provider has been
  done from this branch. Every test uses a fake provider.
