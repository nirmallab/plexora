# Plexora AI: in-app harness and gateway (branch `feature/ai-harness`)

This is the first implementation of the architecture proposal
(`develop-a-detailed-technical-steady-moore.md`). Three pieces work today:

1. **Gateway.** It lives in the licence Worker (`licensing/`). Every model call
   is tied to the account, licence, seat and environment that made it, and is
   tracked next to the licences.
2. **Harness.** `plexora/ai/harness/` gates a project inside Plexora with no
   external agent. Each call goes through the gateway and is billed in
   Plexora AI credits.
3. **Conversational agent** (branch `feature/ai-chat`). Plexora AI chat runs in
   three places: the viewer (`chatPanel.js`), HTTP (`/ai/v1/conversations`)
   and the terminal (`plexora ai chat`). It is a tool loop over the capability
   registry with deferred tool schemas, approvals, sub-agents, tool-result
   caching and offloading.

## How to use it

```
plexora ai run gating <project> [--markers CD3,CD8] [--mode propose]
plexora ai run gating projA projB projC --parallel 3   # several sessions at once
plexora ai run gating <project> --resume <session>      # after a credit pause
plexora ai run gating <project> --dev [--model claude-sonnet-5]   # internal testing
plexora ai trace [RUN] [--cache]                         # calls, cache verdicts, credits
plexora ai credits [--days 30]                           # balance and usage
plexora ai chat [--resume ID] [--dev]                    # a conversation in the terminal
```

To use it, the machine needs an activated Paid licence that includes the
`ai` entitlement (or `ai:gating`; chat needs `ai:chat`, which `ai` covers).

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

## Conversational agent (`feature/ai-chat`)

| Module | What it does |
|---|---|
| `runner.py` | `AgentRunner.create/open`. `turn(text, images)` yields events: `turn_started`, `text_delta`, `text`, `tool_call`, `tool_result`, `approval_requested`/`approval_decided`, `usage`, `paused`, `stopped`, `compacted`, `agent_started`/`agent_finished`, `done`, `error`. Calls use capability `text_reasoning`, switching to `vision_routine` once the history holds an image, with `context.feature = "chat"`. |
| `tools.py` | `ToolAdapter`. The catalog is `registry.describe()` filtered by egress and by the write policy. It keeps source-file writes and deletes, because they go through approval. It drops `ai.*`, and drops the viewer tools when there is no viewer. It is sorted by tool name and frozen in the record. Schemas are deferred: the system prompt lists each tool's name and one-line purpose, and `load_tool(names)` **appends** the full definitions to the end of `tools`. The local tools are `load_tool`, `list_skills`, `read_skill`, `read_artifact`, `spawn_agents`, `await_agents`, `read_board` and `post_board`. |
| `approvals.py` | `ApprovalGate`. A `source_file_write` or `destructive` call writes `control.json.approvals.<id>` (`paused_by: "approval"`) and waits. Approve runs that one call with `allow_source_writes` or `allow_destructive` and `confirm: true`. Deny, stop or expiry return an `is_error` tool_result saying the user declined. A `reversible_write` runs, and its receipt's `operation_id` becomes an Undo chip. |
| `toolcache.py` | `ToolResultCache.get_or_call(capability, args, project_revision, call)`. It keys on `(capability, canonical(args), revision, plexora version)` and stores content-addressed files under `.agent/ai/toolcache/`. It caches `read` capabilities only. It never caches `row_level`/`raw_pixels` egress, viewer state, jobs or failures. Entries expire after 15 minutes, because hand edits in the viewer leave no receipt. A hit is recorded as `source: cache` in `trace.sqlite` `tool_calls`. |
| `plexora/agent/revision.py` | Per-project and global write counters in `.agent/revision.json`. `make_receipt` bumps them on every write that changed something. |
| `offload.py` | `offload(result, threshold_tokens=4000)`. A larger result is stored as `.agent/ai/offload/off_<hash>.txt`, and the model is given `{artifact_id, kind, head, tail, size}`. `read_artifact(id, start/end or query)` returns slices or matching lines. |
| `conversations.py` | `ConversationStore(SessionStore)` keeps, under `.agent/ai/conversations/<id>/`: `session.json`, `messages.json`, the numbered transcript (`decisions.jsonl`), `control.json` and `.lock`. `ChatService` runs each turn on a thread. `events(after, wait_s)` is a held poll. `control`, `approve` and `note` are the panel's hooks. |
| `chat_capabilities.py` | `ai.chat_start`, `ai.chat_send`, `ai.chat_approve`, `ai.chat_control` and `ai.chat_history`. Entitlement `ai:chat`, `remote_safe=False`. They are registered with core when the harness is present. Approving a delete or a source-file write needs the caller's own policy to allow it, so an MCP agent cannot approve its own calls. |
| `server/routes/ai_chat_routes.py` | `/ai/v1/conversations`: POST create, GET list or one, POST `/messages`, GET `/events`, and POST `/control`, `/approve` and `/undo`. `/events` serves SSE when the request sends `Accept: text/event-stream`, and a held poll otherwise. Like `/agent/v1`, it answers loopback only when the server has no token, and it is guarded by `ai:chat`. |
| `client/src/js/views/chatPanel.js` | The panel, opened from a launcher chip at the viewer's lower left. It shows the AI disclosure line, the answer as it streams, tool chips (cached, error, Undo, returned images), Approve/Deny cards and sub-agent lines. It has Stop and a credit meter. It uses SSE and falls back to the held poll. |

**Prefix.** The order is:

1. Tools: the local tools, then any appended loads.
2. System: identity and house rules, the `dataset-triage` skill, then the tool catalog. The one breakpoint is on the catalog.

Nothing in the prefix is specific to one conversation. The record keeps the prefix, so a resumed conversation sends the same bytes. An append keeps every earlier byte. It does cost one cache write, on the next call, of the system prompt and history that follow it. That is why `load_tool` takes a list.

**Compaction.** Past 50k tokens of history, the older turns are replaced by a deterministic local summary at the start of the kept window. The cut is always at the start of a user turn, so tool_use and tool_result pairs stay together. The prefix is never edited. The gateway allows 24 images per request, so only the newest 20 images are kept.

**Control.** `control.json` is read before every model call and every tool call. Stop ends the turn, and pause makes it wait. A refusal for credit or entitlement pauses the conversation (`paused_by: "credits"`) with a `paused` event. The next message clears the pause and joins the waiting user turn, so roles still alternate.

**Sub-agents.** `spawn_agents([{id, role, brief, inputs, depends_on, tools}], wait)` builds a `TaskGraph` and runs it on the `Scheduler` with `stagger_first`: the first sub-agent's first call warms the cache, then the rest start. Each sub-agent:

- forks the parent's exact `tools` and `system`, and starts with its brief after the breakpoint;
- may call only its tool subset, which is read-only by default. The schema bytes stay the same; the adapter refuses anything outside the subset;
- shares the parent's approvals and board;
- returns `{summary, findings, operation_ids, artifact_ids, state, model_calls, tool_calls, charged_micro}`.

Depth is bounded by `max_depth` (default 1). `wait: false` with `await_agents(ids)` runs them in the background.

**Tests.** `tests/test_ai_chat.py` (27 tests) and `tests/test_ai_chat_panel.py`. The panel test runs `tests/js/chat_panel_probe.mjs` (17 checks) and checks the template. `FakeGateway` brains can return `Reply(text, tool_uses)`, which streams `tool_use` blocks with `input_json_delta`. The gateway client parses these into `ModelResponse.blocks`, and `ModelRequest.tools` carries the definitions.

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
  `AgentRunner.turn` is a generator over a worker thread, not an async
  iterator.
- **Thinking blocks are not sent back.** The gateway accepts only text,
  image, tool_use and tool_result blocks, so the client keeps only the text
  and tool_use blocks of an answer.
- **A conversation's blackboard is in memory.** It is saved as facts in the
  conversation record, not in tables in `trace.sqlite`.

## Not built yet (proposal phases 2–5)

- **Commercial:** Paddle checkout and webhooks, auto-recharge, spend
  policies, threshold emails, and a portal page for usage.
- **Providers:** OpenAI, OpenRouter, OrcaRouter and SayGM adapters; the
  per-module routing bench (`route_evaluations`, the publish gate, shadow
  runs); circuit breakers and failover; daily reconciliation against provider
  cost reports.
- **In-app:** the signed policy bundle; the `/ai/v1/runs` routes and the
  "Gate with Plexora AI" button in the agent panel (another branch).
- **Chat:** external MCP servers as tools (`ExternalServers`); `run_analysis`
  (Code Mode); `plexora ai trace --graph`; task checkpoints for sub-agents.
  A sub-agent cut off by a restart starts again from its brief.
- **Other features:** the QC worker.
- **Engine and caching:** the engine change that lets several packets of one
  session be outstanding at once (parallel markers within one image); the
  model-call cache. `ToolResultCache` and offloading are built, for chat.
- **Real provider:** no end-to-end run against the real provider has been
  done from this branch. Every test uses a fake provider.
