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
plexora ai run gating <project> --parallel-markers 3   # several markers of one image at once
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
- **Parallel markers** (`GatingOptions.parallel_markers`, `--parallel-markers N`).
  N lanes answer one session at once. Each lane is a reader of its own
  (`gating_next(reader=..., parallel=N)`; lane 0 is the session's default
  reader) with its own rolling workers. The lanes run under the `Scheduler`
  with `stagger_first`, so lane 0's first call writes the cached prefix
  before the others start. The engine decides what may be out side by side:
  - a marker waits for the partners it is judged beside that come earlier in
    gating order, and for the partner it is gated `within`;
  - set-up packets and T1 strips go out alone;
  - an answer whose partner gates changed while it was out is refused
    (`reissue`) and the decision is served again.

  So the gates are the serial run's. A lane told `busy` asks again. The
  summary adds `parallel_markers`, `peak_outstanding` and `reissued`. How
  much this gains depends on the panel: in a T-cell panel every marker is a
  partner of the others, so only independent markers overlap.
- **Pausing for credit.** When the gateway returns `insufficient_credits`,
  `run_envelope_exceeded` or a similar refusal, the session is **paused**,
  not abandoned. `--resume <session>` continues it.
- **Trace** (`trace.py`). `<data_root>/.agent/ai/trace.sqlite` records runs,
  model calls (tokens, cache verdict, credit, gateway request id) and tasks.

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
- `tests/test_ai_parallel_markers.py` (7 tests) covers several packets of one session out at once:
  - three readers reach the serial run's gates, with more than one packet out at once;
  - every issue obeys the rules: a marker never goes out before its earlier partners or its `within` partner are settled, and exclusive packets never go out beside others;
  - a stale answer is refused and served again;
  - a record with the old single-packet scalars still loads;
  - the outstanding map's bookkeeping and the per-reader epochs;
  - the harness with `parallel_markers=3`: the same gates as serial, and one prefix write.

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
- **In-app:** the signed policy bundle; `/ai/v1` routes and the "Gate with
  Plexora AI" button in the agent panel; the conversational agent (chat
  panel, approvals, `spawn_agents` tools).
- **Other features:** the QC worker.
- **Engine and caching:** `ToolResultCache`, offloading and the model-call
  cache. Parallel markers still need two things. The mirror script and the
  agent panel show one subject (the newest packet) rather than several. QC
  sessions keep one packet at a time (`BaseEngine.next_ready`'s default).
- **Real provider:** no end-to-end run against the real provider has been
  done from this branch. Every test uses a fake provider.
