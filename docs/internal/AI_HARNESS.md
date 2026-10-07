# Plexora AI: in-app harness and gateway (branches `feature/ai-harness`, `feature/ai-in-app`)

This is the first implementation of the architecture proposal
(`develop-a-detailed-technical-steady-moore.md`). Three pieces work today:

1. **Gateway.** It is the BioCognia platform's AI gateway (`ai.biocognia.com`,
   `workers/ai` in the `biocognia-platform` repository), shared with SCIMAP
   Pro. Every model call is tied to the organisation, user, product, seat,
   device and task that made it. It used to live in Plexora's licence Worker
   (`licensing/`), which is gone.
2. **Harness.** `plexora/ai/harness/` gates a project, or quality-controls its
   image, inside Plexora with no external agent. Each call goes through the
   gateway and is billed in the organisation's AI credits.
3. **In-app runs.** The same runs start from the viewer (the agent panel's
   "Gate with Plexora AI" / "QC with Plexora AI"), from `/ai/v1`, or over MCP,
   as the `ai.*` capabilities.
4. **Conversational agent.** Plexora AI chat runs in three places: the viewer
   (`chatPanel.js`), HTTP (`/ai/v1/conversations`) and the terminal
   (`plexora ai chat`). It is a tool loop over the capability registry with
   deferred tool schemas, approvals, sub-agents, tool-result caching and
   offloading.

## How to use it

```
plexora ai run gating <project> [--markers CD3,CD8] [--mode propose]
plexora ai run gating projA projB projC --parallel 3   # several sessions at once
plexora ai run gating <project> --parallel-markers 3   # several markers of one image at once
plexora ai run gating <project> --resume <session>      # after a credit pause
plexora ai run gating <project> --dev [--model claude-sonnet-5]   # internal testing
plexora ai run qc <project> [--channels DAPI,CD3] [--mode propose]  # AutoQC of the image
plexora ai run qc <project> --resume <session>
plexora ai trace [RUN] [--cache]                         # calls, cache verdicts, credits
plexora ai credits [--days 30]                           # balance and usage
plexora ai chat [--resume ID] [--dev]                    # a conversation in the terminal
```

To use it, the device needs to be connected to a Paid licence
(Settings › License › Connect this device, or `plexora license activate`) that
includes the `ai` entitlement (or `ai:gating` / `ai:qc` for the CLI; the in-app routes and
`ai.*` capabilities need `ai`; chat needs `ai:chat`, which `ai` covers).

## Gateway (the BioCognia platform, `bioc-ai`)

The gateway is no longer in this repository. Every model call from every
BioCognia product goes through `ai.biocognia.com` (`workers/ai` in the
`biocognia-platform` repository), which holds the approved models, provider
routes, task routing, the credit ledger and the admin pages; its own docs
describe them. Plexora's side of it:

| Piece | Where |
|---|---|
| The 30-minute `BIOCAI1` token, obtained against this device's certificate (`POST api.biocognia.com/v1/ai/token`) | `biocognia.gateway.TokenSource(LICENSING)` |
| Transport, one token refresh, SSE parsing, `balance` / `usage` / `pricing` / runs | `biocognia.gateway.GatewayClient` |
| `messages()`: one streamed call, retries, the events between `biocognia.accepted` and `biocognia.usage` read into a `ModelResponse` | `plexora/ai/harness/gateway.py` (subclasses the shared client) |
| The wire types (`ModelRequest`, `ModelResponse`, `Usage`) | `plexora/ai/harness/wire.py` |
| What a refusal pauses on | `decision.PAUSE_CODES` = `biocognia.codes.PAUSE_CODES` + provider outages |

A call names its **task** as `plexora.<module>.<task>`
(`plexora.gating.threshold_evaluation`), never a model and never a capability
class; the gateway routes by the longest match (`plexora.gating.*`,
`plexora.*`, `*`). A packet kind no task maps is sent as
`plexora.<module>.default`, which the gateway serves at the product's default
and records. The token's `mods` say which of Plexora's modules this licence
may call; they come from the product manifest's `ai` root
(`manifest.AI_MODULES`: `gating`, `qc`, `chat`).

The task registry (`plexora/ai/tasks.yaml`) and the product manifest
(`plexora/licensing/manifest.py`) are edited only here. `tools/bioc_sync.py`
uploads them (`PUT /admin/api/ai/registry/plexora`,
`PUT /admin/api/products/plexora/manifest`, `Authorization: Bearer
$BIOC_ADMIN_TOKEN`), and `tools/bioc_sync.py --check` diffs both against the
platform.

`BIOCOGNIA_AI_TOKEN` supplies a token directly (a CI job, a test),
`BIOCOGNIA_AI_GATEWAY` overrides the address, and `BIOCOGNIA_AI_DEV=1` (or
`--dev`) uses the dev route, which only internal testing accounts may call and
which bills at provider cost. Credits belong to the organisation: one wallet
for every product, with member and product caps. A cap reached answers
`member_limit_reached` or `product_limit_reached`, which pause a run like
`insufficient_credits` does; the agent card says which, and `plexora ai
credits` shows the member and product lines next to the organisation's
balance.

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
  canonical JSON, with a single breakpoint. Each call also marks the newest
  message and the user turn before it (`wire.with_breakpoints`, on a copy; the
  stored messages never carry them), so a worker's later packets read its
  earlier packets from cache and only the new packet is written. Providers that cache automatically (OpenAI) ignore both. QC's (`qc_prefix`) adds the
  `qc-image` skill between them (placeholders filled from code constants). Its
  identity says that the harness, not the worker, calls the tools. The bytes are identical for every
  worker, session and user of a build. `CacheMonitor` gives each call a
  verdict of `cold`, `hit` or `miss`, and `plexora ai trace --cache` reports
  them.
- **Cached history** (`wire.with_breakpoints`). Each call also marks the last
  block of the newest message and of the user turn before it, on a copy (the
  stored history is never marked). A worker's call then reads its earlier
  packets, images and answers from cache and writes only the new turn. On
  Anthropic only marked content is cached, so without these every call paid
  full price for its history. Marking the previous turn as well means a call
  reads exactly what the last one wrote, however many images a packet adds.
  Three breakpoints in all, under Anthropic's four. The monitor learns the
  prefix's size only from a worker's first call, which reads just the system
  prompt; later reads include history.
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

## End-to-end pipeline check

The end-to-end runner (`tools/ai_e2e.py`: the gateway under `wrangler dev`, a
fake provider, the real harness) and the staging tool moved to the
`biocognia-platform` repository with the gateway. Against a deployed staging
platform, `docs/internal/AI_DEPLOY.md` lists the manual checks.

## Tests

- `tests/test_ai_tasks.py`: the fragment `tools/bioc_sync.py` uploads is the gateway's `Fragment` shape, every task's wire id is `plexora.<module>.<task>`, every gating and QC packet kind maps to a task, and the registry names no model or vendor.
- `tests/test_ai_route_bench.py` (5 tests): the bench against `FakeGateway` with the truth agent, the metrics, and submission.
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
- `tests/test_ai_routes.py` (9 tests) runs the Flask test client against FakeGateway, which it reaches through `BIOCOGNIA_AI_GATEWAY` / `BIOCOGNIA_AI_TOKEN`. It covers:
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
- `tests/test_ai_parallel_markers.py` (7 tests) covers several packets of one session out at once:
  - three readers reach the serial run's gates, with more than one packet out at once;
  - every issue obeys the rules: a marker never goes out before its earlier partners or its `within` partner are settled, and exclusive packets never go out beside others;
  - a stale answer is refused and served again;
  - a record with the old single-packet scalars still loads;
  - the outstanding map's bookkeeping and the per-reader epochs;
  - the harness with `parallel_markers=3`: the same gates as serial, and one prefix write.

## Conversational agent (`feature/ai-chat`)

| Module | What it does |
|---|---|
| `runner.py` | `AgentRunner.create/open`. `turn(text, images)` yields events: `turn_started`, `text_delta`, `text`, `tool_call`, `tool_result`, `approval_requested`/`approval_decided`, `usage`, `paused`, `stopped`, `compacted`, `agent_started`/`agent_finished`, `done`, `error`. Calls use capability `text_reasoning`, switching to `vision_routine` once the history holds an image, with `context.feature = "chat"`. |
| `tools.py` | `ToolAdapter`. The catalog is `registry.describe()` filtered by egress and by the write policy. It keeps source-file writes and deletes, because they go through approval. It drops `ai.*`, and drops the viewer tools when there is no viewer. It is sorted by tool name and frozen in the record. Schemas are deferred: the system prompt lists each tool's name and one-line purpose, `load_tool(names)` returns the full definitions as its result (in the history), and `call_tool(name, arguments)` runs one. The `tools` array never changes during a conversation. A catalog tool called before it was loaded still runs; if it fails, its definition is added to the error. Approvals are asked for the tool `call_tool` names. The local tools are `load_tool`, `call_tool`, `list_skills`, `read_skill`, `read_artifact`, `spawn_agents`, `await_agents`, `read_board` and `post_board`. |
| `approvals.py` | `ApprovalGate`. A `source_file_write` or `destructive` call writes `control.json.approvals.<id>` (`paused_by: "approval"`) and waits. Approve runs that one call with `allow_source_writes` or `allow_destructive` and `confirm: true`. Deny, stop or expiry return an `is_error` tool_result saying the user declined. A `reversible_write` runs, and its receipt's `operation_id` becomes an Undo chip. |
| `toolcache.py` | `ToolResultCache.get_or_call(capability, args, project_revision, call)`. It keys on `(capability, canonical(args), revision, plexora version)` and stores content-addressed files under `.agent/ai/toolcache/`. It caches `read` capabilities only. It never caches `row_level`/`raw_pixels` egress, viewer state, jobs or failures. Entries expire after 15 minutes, because hand edits in the viewer leave no receipt. A hit is recorded as `source: cache` in `trace.sqlite` `tool_calls`. |
| `plexora/agent/revision.py` | Per-project and global write counters in `.agent/revision.json`. `make_receipt` bumps them on every write that changed something. |
| `offload.py` | `offload(result, threshold_tokens=4000)`. A larger result is stored as `.agent/ai/offload/off_<hash>.txt`, and the model is given `{artifact_id, kind, head, tail, size}`. `read_artifact(id, start/end or query)` returns slices or matching lines. |
| `conversations.py` | `ConversationStore(SessionStore)` keeps, under `.agent/ai/conversations/<id>/`: `session.json`, `messages.json`, the numbered transcript (`decisions.jsonl`), `control.json` and `.lock`. `ChatService` runs each turn on a thread. `events(after, wait_s)` is a held poll. `control`, `approve` and `note` are the panel's hooks. |
| `chat_capabilities.py` | `ai.chat_start`, `ai.chat_send`, `ai.chat_approve`, `ai.chat_control` and `ai.chat_history`. Entitlement `ai:chat`, `remote_safe=False`. They are registered with core when the harness is present. Approving a delete or a source-file write needs the caller's own policy to allow it, so an MCP agent cannot approve its own calls. |
| `server/routes/ai_chat_routes.py` | `/ai/v1/conversations`: POST create, GET list or one, POST `/messages`, GET `/events`, and POST `/control`, `/approve` and `/undo`. `/events` serves SSE when the request sends `Accept: text/event-stream`, and a held poll otherwise. Like `/agent/v1`, it answers loopback only when the server has no token, and it is guarded by `ai:chat`. |
| `client/src/js/views/chatPanel.js` | The panel, opened from a launcher chip at the viewer's lower left. It shows the AI disclosure line, the answer as it streams, tool chips (cached, error, Undo, returned images), Approve/Deny cards and sub-agent lines. It has Stop and a credit meter. It uses SSE and falls back to the held poll. |

**Prefix.** The order is:

1. Tools: the local tools only, the same array for the whole conversation: `load_tool` returns definitions in its result and `call_tool` runs them.
2. System: identity and house rules, the `dataset-triage` skill, then the tool catalog. The system's one breakpoint is on the catalog. Each call also marks the newest message and the user turn before it (`wire.with_breakpoints`, on a copy: the saved conversation is unmarked), so the history is read from cache too. Compaction and image limiting rewrite earlier turns, and the next call writes the cache afresh.

Nothing in the prefix is specific to one conversation. The record keeps the prefix, so a resumed conversation sends the same bytes. Providers cache tools first, then system, then messages, so a tools array that grew with each `load_tool` (the first design) made the next call rewrite the whole prefix and history; loaded definitions now travel in the history instead.

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

- **The gateway is the BioCognia platform's, not Plexora's.** It began in
  Plexora's licence Worker so tracking lived with licensing; it moved, with
  licensing, to the platform every BioCognia product shares (`bioc-ai`, one
  wallet per organisation).
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
- **Providers:** daily reconciliation against providers' cost reports (the
  per-call `reported_cost_micro` is recorded but not yet compared); a QC
  route bench (gating only so far); Bedrock and Vertex adapters. No call has
  been made to a real OpenAI, OpenRouter, OrcaRouter or SayGM endpoint from
  this repository (the platform's end-to-end runner is the first).
- **In-app:**
  - the signed policy bundle;
  - polling `/v1/ai/balance` while a run is paused for credit (today, resuming waits for the user to click Resume);
  - a `plexora.ai.run()` notebook entry.
- **Gateway:** the `/v1/ai/preflight` affordability check. Until it exists, the estimate is units × the price list.
- **Chat:** external MCP servers as tools (`ExternalServers`); `run_analysis`
  (Code Mode); `plexora ai trace --graph`; task checkpoints for sub-agents.
  A sub-agent cut off by a restart starts again from its brief.
- **Engine and caching:** the model-call cache. Parallel markers still need
  two things: the mirror script and the agent panel show one subject (the
  newest packet) rather than several, and QC sessions keep one packet at a
  time (`BaseEngine.next_ready`'s default).
- **Real provider:** no end-to-end run against the real provider has been
  done from this branch. Every test uses a fake provider.
