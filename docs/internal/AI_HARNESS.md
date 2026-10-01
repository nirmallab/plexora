# Plexora AI: in-app harness and gateway (branch `feature/ai-harness`)

This is the first implementation of the architecture proposal
(`develop-a-detailed-technical-steady-moore.md`). Two pieces work today:

1. **Gateway.** It lives in the licence Worker (`licensing/`). Every model call
   is tied to the account, licence, seat and environment that made it, and is
   tracked next to the licences.
2. **Harness.** `plexora/ai/harness/` gates a project inside Plexora with no
   external agent. Each call goes through the gateway and is billed in
   Plexora AI credits.

## How to use it

```
plexora ai run gating <project> [--markers CD3,CD8] [--mode propose]
plexora ai run gating projA projB projC --parallel 3   # several sessions at once
plexora ai run gating <project> --resume <session>      # after a credit pause
plexora ai run gating <project> --dev [--model claude-sonnet-5]   # internal testing
plexora ai trace [RUN] [--cache]                         # calls, cache verdicts, credits
plexora ai credits [--days 30]                           # balance and usage
```

To use it, the machine needs an activated Paid licence that includes the
`ai` entitlement (or `ai:gating`).

## Gateway (`licensing/src/ai/`, `licensing/src/routes/ai.ts`)

| Route | What it does |
|---|---|
| `POST /v1/ai/token` | Takes the environment certificate and binding (the same checks as `/v1/refresh`). Returns a `PLXAI1` token that lasts 30 minutes and is signed with the licence keys. The token's `mode` is `dev` only for accounts an admin has put in dev mode. |
| `POST /v1/ai/messages` | One streamed call. The client names a **capability**, never a model. The route checks the request against an allowlist and requires an `Idempotency-Key`. It holds credit for the call, streams the provider's events between `plexora.accepted` and `plexora.usage`, then settles on the **provider's** usage × `AI_MARKUP_BPS` (2.0×). |
| `POST /v1/ai/runs`, `/runs/:id/finish` | A quoted, capped run. The quote is the flat feature price (gating is 25 credits per marker) and is held at the start. The account pays min(metered, quote). Calls beyond the envelope are refused. |
| `GET /v1/ai/balance`, `/usage`, `/requests/:id`, `/pricing` | The account's own view of its balance, usage, individual calls and the price list. |
| `POST /v1/ai/dev/messages`, `/v1/ai/dev/runs` | **Dev route.** Only accounts in mode `dev` can use it. Calls are billed at provider cost with no markup, the caller may name any catalogued model, and every call is still tracked with `billing='dev'`. |
| `/admin/api/ai/usage`, `/requests`, `/accounts/:id` (GET, PATCH), `/accounts/:id/credit` | The tracking: usage, cost against charges (margin), balances, ledgers and runs. Admins also set an account's mode, markup and allowance here, and grant or adjust credit. Every grant is written to `events`. |

### Providers, routes and failover (`licensing/src/ai/providers.ts`, `routing.ts`, `translate.ts`)

The client always names a capability and always receives Anthropic-shaped
events. Which provider answers is the route table's decision.

| Provider | Wire | Secret | Notes |
|---|---|---|---|
| `anthropic` | Anthropic Messages, passthrough | `ANTHROPIC_API_KEY` | Direct. The built-in default for every capability. |
| `openai` | Responses API, translated both ways | `OPENAI_API_KEY` | Direct. An OpenAI **API** account (platform.openai.com); a ChatGPT subscription cannot be used. Sends `safety_identifier`, `prompt_cache_key`, `store: false`. |
| `openrouter` | Chat Completions, translated | `OPENROUTER_API_KEY` | Aggregator. Keeps `cache_control`, sends `user`, `session_id`, `usage.include`, `provider.data_collection: deny`. Its reported `cost` is recorded beside ours. |
| `orcarouter` | Anthropic Messages, passthrough | `ORCAROUTER_API_KEY` | Aggregator. Asks for inline cost; records `X-Orca-Resolved-Model`. |
| `saygm` | Anthropic Messages, passthrough | `SAYGM_API_KEY` | Aggregator, **confidential (`-TEE`) models only**: frontier calls through SayGM run on an anonymous operator's key, which Plexora's provider obligations do not allow. |

- **Models.** Anthropic's list prices are built in. Every other model is
  catalogued by an admin (`PUT /admin/api/ai/models/:provider/:model`) with its
  unit costs, the aggregator fee (`fee_bps`, OpenRouter 550) and the URL the
  price was read from. Nothing is hard-coded for providers other than Anthropic.
- **Routes** (`ai_routes`) are per (feature, capability). The call's feature
  wins, then `*`, then the built-in default. `rank` orders them. A `serve` row
  that is not a direct provider's `*` default can be published only with a
  passing routing-bench evaluation on that module's **current** bench version
  (`catalog.ts::BENCH`). The gateway computes `passed` from the metrics; it
  never takes the submitter's word.
- **Retries and failover.** The same route is retried `AI_UPSTREAM_RETRIES`
  times on a 429/5xx (that keeps its prompt cache). The call moves down a rank
  only as the route's `failover` says:
  - `outage` (default): only once the route's circuit is open;
  - `error`: on any exhausted retry;
  - `never`.

  A provider 400 is the request's own fault and never fails over. A provider
  401/403 is our key and counts as an outage. Nothing fails over after the
  first streamed byte.
- **Circuits** (`ai_circuits`) are per `provider:model`, over a
  `AI_CIRCUIT_WINDOW_S` window. They open at `AI_CIRCUIT_MIN_FAILURES`
  failures that are at least half the window, for `AI_CIRCUIT_OPEN_S`; after
  that, one call probes. `POST /admin/api/ai/providers/:key/disable` is the
  kill switch, for a whole provider or one model.
- **Sticky sessions** (`ai_sticky`). A session stays on the route that last
  served it, so a session that failed over keeps its warm cache there after
  the primary recovers. New sessions go back to rank 0.
- **Shadow routes.** A `role: shadow` row duplicates `shadow_pct` % of
  sessions (chosen by a stable hash of account and session) to a candidate,
  at Plexora's cost. The candidate's answer is held in memory only long
  enough to compare its decision fields with the served answer. Rows are
  recorded with `billing = 'shadow'` and `shadow_agree`, and are never shown
  to or charged to the account. `GET /admin/api/ai/shadow` reports agreement
  and cost per candidate.
- **The routing bench, client half.**
  `plexora ai route-bench openai/<model> --synthetic easy,hard [--submit]`
  gates synthetic scenes with known phenotypes through the **dev route**,
  naming the candidate. It scores code agreement and per-marker F1, adds
  invalid-answer rate, failure rate, cache-read share, and cost and time per
  image. `--submit` (with `PLEXORA_ADMIN_TOKEN`) records it; the reply says
  whether it passed and gives the `evaluation_id` to publish with. Memo reuse
  is off in the bench, and the memo's agent key includes the model on the dev
  route, so one model's answers are never replayed for another.

To put a second provider into service:

```
wrangler secret put OPENAI_API_KEY
curl -X PUT  $LIC/admin/api/ai/models/openai/<model> -H "Authorization: Bearer $ADMIN"      -d '{"in_micro":…,"cache_read_micro":…,"cache_write_5m_micro":…,"cache_write_1h_micro":…,"out_micro":…,
          "source_url":"https://openai.com/api/pricing"}'
plexora ai route-bench openai/<model> --synthetic all --submit          # an internal dev account
curl -X POST $LIC/admin/api/ai/routes -H "Authorization: Bearer $ADMIN"      -d '{"capability":"vision_judgement","provider":"openai","model":"<model>","rank":1}'
```

### Tables (schema v2 in `licensing/schema.sql`)

| Table | Contents |
|---|---|
| `ai_requests` | One row per call, written from the provider's usage. Each row copies the unit costs it was charged at. Nothing the model saw or said is stored. |
| `ai_balances` / `ai_holds` | The balance and the open holds against it. Credit is reserved with one conditional `UPDATE`, so concurrent calls cannot overspend. |
| `ai_ledger` | Signed entries for every credit movement. They always sum to the balance, and each `(journal_id, bucket)` pair is unique, so a grant or settlement that is retried is never posted twice. |
| `ai_runs`, `ai_idempotency`, `ai_accounts` | Runs, idempotency keys and per-account settings. |
| `ai_models`, `ai_routes`, `ai_route_evaluations` | The model catalogue with price sources, the published route table, and routing-bench results. |
| `ai_circuits`, `ai_sticky` | Circuit breakers and kill switches; the route each session last used. |

### Deploying

```
cd licensing
npm run db:init                       # applies schema.sql (idempotent; adds the ai_* tables)
wrangler secret put ANTHROPIC_API_KEY
wrangler secret put AI_USER_PEPPER
# optional, one per further provider the route table will name:
# wrangler secret put OPENAI_API_KEY | OPENROUTER_API_KEY | ORCAROUTER_API_KEY | SAYGM_API_KEY
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
  coordinator, and each packet is one structured-output call. The answer is
  validated locally against `autogate.answers.Answer` before it is submitted.
  An invalid answer gets one repair turn inside its worker; after that it is
  submitted as an engine strike.
- **Rolling workers.** A worker covers one marker by default. It also ends
  after 8 packets or about 60k tokens of context, whichever comes first, so
  context never grows to the 480k seen in the live run.
- **Cached prefix** (`prefix.py`). The identity text plus the reading guide as
  canonical JSON, with a single breakpoint. The bytes are identical for every
  worker, session and user of a build. `CacheMonitor` gives each call a
  verdict of `cold`, `hit` or `miss`, and `plexora ai trace --cache` reports
  them.
- **Structured outputs** (`schema.py`). The pydantic answer models are
  reduced to the subset providers accept: closed objects and no numeric or
  length constraints. The dropped constraints are still enforced by local
  validation.
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
  not abandoned. `--resume <session>` continues it.
- **Trace** (`trace.py`). `<data_root>/.agent/ai/trace.sqlite` records runs,
  model calls (tokens, cache verdict, credit, gateway request id) and tasks.

## Tests

- `licensing/test/routes/ai_routing.test.ts` (12 tests) covers:
  - the OpenAI request and stream translation, including tools, tool calls and tool results;
  - the OpenRouter tool-call stream, its fee and reported cost;
  - the OrcaRouter passthrough and the model it reports as answering;
  - SayGM's TEE-only rule;
  - the publish gate (no evaluation, failing, stale bench, wrong model);
  - same-route retries, failover only on an open circuit, half-open recovery and stickiness;
  - `failover: error`, and no failover on a 400;
  - the kill switch;
  - dev-route `provider/model`;
  - shadow agreement, never billed or shown.
- `tests/test_ai_route_bench.py` (5 tests): the bench against `FakeGateway` with the truth agent, the metrics, submission, and bench versions that match `catalog.ts`.
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
- **Providers:** daily reconciliation against providers' cost reports (the
  per-call `reported_cost_micro` is recorded but not yet compared); a QC
  route bench (gating only so far); Bedrock and Vertex adapters. No call has
  been made to a real OpenAI, OpenRouter, OrcaRouter or SayGM endpoint: the
  translations follow their documented wire formats and are tested against
  fakes, and should be checked with one dev call each before a route is
  published.
- **In-app:** the signed policy bundle; `/ai/v1` routes and the "Gate with
  Plexora AI" button in the agent panel; the conversational agent (chat
  panel, approvals, `spawn_agents` tools).
- **Other features:** the QC worker.
- **Engine and caching:** the engine change that lets several packets of one
  session be outstanding at once (parallel markers within one image);
  `ToolResultCache`, offloading and the model-call cache.
- **Real provider:** no end-to-end run against the real provider has been
  done from this branch. Every test uses a fake provider.
