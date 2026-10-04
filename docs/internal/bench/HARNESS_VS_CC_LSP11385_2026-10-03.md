# Plexora harness vs Claude Code + MCP: gating LSP11385 (2026-10-03)

The question: given the same task, models, data and Plexora capabilities, is
Plexora's built-in AI harness (`ai_run_session`) comparable to Claude Code
driving the same gating session over the Plexora MCP server? We compare quality,
speed, token use, caching and cost.

**Answer.**
- **Quality:** the two arms are indistinguishable at this n.
- **Cost:** the harness costs about a quarter as much.
- **Speed:** the harness takes about 30% less wall-clock time.
- **Tokens:** the harness reads about 14× fewer input tokens.
- **Cache hit rate:** the harness's hit rate is lower (57% vs 92%). That's because it sends small, fresh contexts; it reads far fewer tokens in absolute terms.

Two harness defects showed up. Neither changed any gate; both are listed under "Defects found" below.

Campaign `docs/internal/bench/runs/2026-10-03b/`, spec
`docs/internal/bench/harness_vs_cc_lsp11385.yaml`, driver
`tools/bench_harness_vs_cc.py`, proxy `tools/bench_proxy.py`.

## Setup

| | |
|---|---|
| Image | LSP11385, primary cutaneous melanoma, 202,049 cells |
| Markers | SOX10, CD45, CD3e, CD8a, FOXP3 |
| Session | `mode: propose`, `qc: strict`, `reuse_answers: false`, `seed: 0`, `on_limit: extend`, a distinct agent label per run |
| Context note | One sentence, verbatim to both arms. The harness gets it as `context`; Claude Code gets it in its prompt. |
| Models | planning and image_inspection: claude-sonnet-5; biological_context: claude-haiku-4-5; threshold_evaluation and final_validation: claude-opus-5-5 |
| Harness routing | Production gateway task routes, provider anthropic, effort unset (API default) |
| Claude Code routing | Claude Code 2.1.288. The `ai-models.yaml` delegate block gives one worker per model. Aliases are pinned to the same model ids. Coordinator on `sonnet`, `--effort high`. |
| Order | H1, C1, C2, H2 (ABBA). Before every run: snapshot restored, all 40 gates reset, memo, panel cache and tool cache deleted. |
| Measurement | One recording proxy in front of each arm. It logs every request body, image, cache mark, usage line and timing. |
| Code | `0d0218dd` plus the uncommitted MCP models framework. The dirty hash is in each run's `meta.json`. |

Every packet's model was checked against its task after the run. All four runs: 0 mismatches. In every run the gates were unchanged, and the starting gate revision was identical.

## Quality (primary endpoint)

Final gates; the reference is the Opus single-conversation run of 2026-10-01:

| marker | reference | H1 | H2 | C1 | C2 |
|---|---|---|---|---|---|
| SOX10 | 6.58 | 6.58 T1 high | 6.58 T1 high | 6.58 T1 high | 6.58 T1 high |
| CD45 | 7.24 | 7.27 T3 moderate | 7.27 T3 moderate | 7.27 T3 moderate | 7.27 T3 moderate |
| CD3e | 6.55 | 6.62 T3 high | 6.62 T3 moderate | 6.62 T3 high | 6.62 T3 high |
| CD8a | 6.02 | 6.06 T3 high | 6.06 T3 high | **6.00 T4 moderate** | 6.06 T3 high |
| FOXP3 | 5.76 | 5.68 T4 moderate | 5.68 T3 moderate | 5.68 T3 moderate | 5.68 T3 moderate |

- **Agreement:** 19 of 20 marker-runs give the same gate. The exception is C1's CD8a, which went to a T4 candidate round and landed 0.06 lower: per-cell F1 0.971 against the other runs, and 9.94% positive vs 9.38%.
- **Spread:** within-arm and between-arm agreement are both a median F1 of 1.000.
- **Against the reference:** median per-cell F1 is 0.980 for the harness and 0.986 for Claude Code (Jaccard 0.961 / 0.971).
- **FOXP3 is the weakest marker in every run:** F1 0.916 against the reference, 4.22% positive vs 3.57%. Since all runs agree, this comes from the engine's candidates, not from either arm.

Co-expression on every run was identical to within 1.3 points:

| run | CD8a+ in CD3e+ | CD3e+ in CD45+ | FOXP3+ in CD3e+ | SOX10+ that are CD45+ |
|---|---|---|---|---|
| reference | 92.0% | 97.3% | 96.0% | 2.3% |
| H1, H2, C2 | 91.0% | 97.7% | 91.6% | 2.2% |
| C1 | 89.7% | 97.7% | 91.6% | 2.2% |

**Inspection.** Both arms looked at the same evidence: all four runs were sent the same 17 distinct images. Packets by kind:

| run | t1 | t2 | t3 | t4 | qc |
|---|---|---|---|---|---|
| H1 | 1 | 4 | 3 | 1 | 0 |
| H2 | 1 | 4 | 4 | 0 | 0 |
| C1 | 1 | 4 | 3 | 1 | 1 |
| C2 | 1 | 4 | 4 | 0 | 0 |

**What this does and doesn't show.** With 5 markers and 2 runs per arm, the arms can't be told apart. The session engine does most of the work: it scores candidates and picks the packets, so the model mostly confirms or nudges. A harder panel, or markers that reach T4 more often, would test model judgement harder.

## Efficiency

| | H1 | H2 | **harness mean** | C1 | C2 | **Claude Code mean** |
|---|---|---|---|---|---|---|
| Wall clock (s) | 169 | 163 | **166** | 274 | 205 | **240** |
| Inference busy (s) | 153 | 147 | 150 | 263 | 195 | 229 |
| Model calls | 14 | 13 | 13.5 | 53 | 37 | 45 |
| Peak concurrent calls | 1 | 1 | 1 | 4 | 4 | 4 |
| Input tokens, total | 206k | 184k | **195k** | 3.39M | 2.06M | **2.72M** |
| Cache read | 123k | 100k | 111k | 3.11M | 1.91M | 2.51M |
| Cache write | 83k | 84k | 83k | 284k | 145k | 214k |
| Uncached input | 28 | 24 | 26 | 170 | 124 | 147 |
| Output (incl. thinking) | 12.2k | 12.7k | 12.5k | 29.4k | 25.7k | 27.6k |
| Cache hit rate | 59.8% | 54.2% | 57.0% | 91.6% | 93.0% | 92.3% |
| List cost | $0.59 | $0.54 | **$0.56** | $2.82 | $1.50 | **$2.16** |
| Image payloads sent (unique) | 37 (17) | 31 (17) | 34 | 168 (17) | 125 (17) | 147 |
| Image tokens sent (est.) | 31.8k | 26.7k | 29k | 136k | 108k | 122k |
| Invalid answers, repaired | 4 | 3 | 3.5 | 4 | 2 | 3 |
| HTTP errors / retries | 1 / 0 | 1 / 0 | | 0 / 0 | 0 / 0 | |

**Notes on these numbers:**
- **List cost.** Both arms are priced from the proxy's token counts, at one list price table (`tools/transcript_cost.py`). The gateway actually charged $0.83 and $0.72 for H1 and H2, because of its markup.
- **Claude Code's own cost figure.** It reported $1.90 and $1.03 for C1 and C2. Its transcripts leave out thinking output, so its figure undercounts; the proxy's numbers are what Anthropic billed.
- **Input tokens, total** is input_uncached plus cache read plus cache write.
- **Hit rate** is cache read divided by total input.

**Where Claude Code's cost goes.**
- **The coordinator.** Its conversation carries a 129k-token prefix: Claude Code's system prompt plus about 130 Plexora MCP tool schemas (296k characters). Its 14 and 8 calls cost $1.56 and $0.53, for 2.0M and 1.1M input tokens, while answering no packets.
- **The workers alone.** They cost $1.26 and $0.97, about twice the whole harness run.
- **Idle workers.** The Haiku worker answered nothing: over MCP no packet carries the biological_context task, yet it made 10 and 8 calls. The Opus worker made 10 and 6 calls for 1 and 0 packets. Both mostly fetched `other_tasks` and stopped.

**Where the harness's time goes.** It is serial: one lane, peak concurrency 1. It still finishes first because it makes 3–4× fewer calls, even though each one starts streaming later (median time to first token about 6 s through the gateway, against about 1.5 s for Claude Code). Time outside inference is 16 s per harness run and 10 s per Claude Code run.

## Inputs: what each arm sent

| | Harness | Claude Code |
|---|---|---|
| System prompt (characters) | 1,282 (note interpreter); 20,566 (packets: identity plus reading guide, no skills) | 27,480 (Claude Code); 1,628 (aux) |
| Tool schemas (characters) | none: the model gets no tools | 295,834 (coordinator: built-ins plus every Plexora tool); 5,998 (workers: 4 Plexora tools) |
| Skills | none injected | `gate-packets` read by each worker over `read_skill` |
| Context note | interpreted by one `biological_context` call (failed, see below), then raw text | in the coordinator's prompt; the coordinator writes the session's `biology` itself |
| Thinking / effort | none sent; effort unset (API default) | adaptive thinking; `effort: high`; Haiku with a 32k thinking budget |
| Cache marks | system prefix plus 2 rolling breakpoints | Claude Code's own (1-hour TTL on the main conversation) |
| Provider path | Plexora gateway, then Anthropic | Anthropic API directly (OAuth) |
| Other context | none | a system reminder with the user's email and the date; the Agent tool's agent list |

Request bodies (images stored once by hash) are in each run's `proxy/bodies/`. They are kept locally and gitignored.

## Defects found (fixed after the benchmark; see "Follow-up")

1. **The context interpreter's request is rejected by Anthropic.** On a direct Anthropic route, the `biological_context` call fails with `invalid_request_error` in every harness run. Its output schema has `corrected_terms: {additionalProperties: {type: string}}`, which Anthropic's structured outputs do not accept. The harness then passes the note on as written, with `source: unprocessed`. The session still read melanoma and skin from the raw text, so the effect on these gates was nil. A note that restricts or excludes markers would be ignored, though. This worked on the earlier OpenRouter routes.
2. **First answers often fail validation.** In 7 of 18 packets (H1 and H2) the harness's first answer was invalid. The causes: `notes` over the schema's 300 characters (the provider doesn't enforce `maxLength` in structured output), or a reply cut at the harness's `max_tokens` of 4,096 and so not JSON. Each repair costs one extra call. Claude Code's workers had `gating_answer` rejected 6 times across C1 and C2, 4 of them for the same 300-character limit, so the cost of this falls on both arms. The fix belongs in the schema or prompt, not in either arm.

## Follow-up: the fixes, and the Claude Code arm re-measured

Everything the campaign found was fixed the same evening. Only the Claude Code
arm was re-measured: harness runs bill the Plexora gateway, and none were made.
The harness's numbers below are therefore unmeasured.

**Fixed in the harness (Python, measured only by tests and `ai_e2e.py --stub`):**
- `notes` and other free text over their limit are cut at a sentence, not refused (no repair turn).
- An answer's output cap is its task's (16,000), not 4,096.
- A task's packet kinds share one output schema (image inspection's t1/t2/t3/QC looks), so a worker moving from t2 to t3 keeps its cached prefix. QC's artifact inspection kinds do not fit one schema and keep theirs.
- A worker never spans two tasks (no 22k re-write when an Opus packet follows Sonnet ones). A worker known to be full does not write its last turn to the cache.
- The note interpreter's schema is closed (Anthropic accepts it). A note passed on unread says so, with why, in the panel, the CLI and the trace.
- The trace records tries, refusal reasons and the harness's own tool calls. `plexora ai trace --cache` shows cost per packet and the share of input not read from cache.
- An open circuit is waited out once (`retry_after` is now the time left), then the run pauses instead of failing after six blind retries.

**Fixed in the gateway (in production since 2026-10-03, versions `b9240efb` and `5afc31e6`; AI_DEPLOY.md):**
- The independent D1 reads on the request path run in parallel. The sticky-route write and the shadow lookup are off the critical path.
- An open circuit reports itself (`details.failure: circuit_open`, the time left in `retry_after`); the code stays `provider_unavailable` for released clients.
- A new task assignment defaults to effort `auto`. Each provider route has a failover select.
- A model that refuses the 16,000-token output cap (a non-Anthropic model whose limit is not catalogued) is asked once more at 4,096.

**Fixed for MCP agents, and measured** (campaign `runs/fix3b/`, same spec, C1 then C2):
- `plexora mcp serve --profile gating` offers the 54 tools a gating session uses: tool schemas 296k → 190k characters.
- No worker for the note task (the harness's own call): the idle Haiku worker is gone.
- Only the worker whose packets come first is launched (`launch: now`); the rest on demand.
- An answer that names no `reader` draws the next packet in its own packet's scope. Without this, campaign `runs/fix3/` C2 lost 3.5 minutes: the Sonnet worker's answer drew the Opus worker's t4 packet, which the Sonnet coordinator then answered itself (3 model mismatches).

| run | wall s | list cost | carried-over tokens | adjusted cost | model calls | mismatches |
|---|---|---|---|---|---|---|
| 2026-10-03b C1 | 274 | $2.82 | 0 | $2.82 | 53 | 0 |
| 2026-10-03b C2 | 205 | $1.50 | 122,592 | $1.93 | 37 | 0 |
| fix3b C1 | 196 | $1.89 | 78,520 | $2.16 | 36 | 0 |
| fix3b C2 | 179 | $1.21 | 81,420 | $1.50 | 27 | 0 |

- **Cost:** mean list cost $2.16 → $1.55 (−28%); carry-over adjusted $2.38 → $1.83 (−23%).
- **Wall clock:** 240 s → 188 s (−22%).
- **Quality:** unchanged. Median per-cell F1 against the reference is 0.980; C1 and C2 placed identical gates; every gate matches the baseline's to within 0.06. The accept/review triage moved on one marker per run (C1 CD8a, C2 FOXP3 to `manual_review_recommended`), at the same gates.
- **Foreground rule** (campaign `runs/fix4/`): the `delegate` block now names Claude Code's `run_in_background` false. C2 launched one worker and waited: 17 calls, $0.95. C1 still launched four workers, one in the background, polled and messaged it, and one t4 packet was answered on Sonnet. The rule helps but does not hold every time.

| run | wall s | list cost | adjusted cost | model calls | mismatches | wake-ups + messages |
|---|---|---|---|---|---|---|
| fix4 C1 | 240 | $2.06 | $2.33 | 36 | 1 | 4 |
| fix4 C2 | 205 | $0.95 | $1.22 | 17 | 0 | 0 |

**Non-Anthropic models.** Every change was checked for what it sends to an OpenAI-wire or OpenRouter route, where the
gateway passes the output schema as advice (`strict: false`):
- The per-task schema lets a model name a sibling kind or fill another kind's fields. The harness now reads a
  sibling kind as the packet's own and drops other kinds' fields; a field of no kind is still refused.
- The 16,000-token cap would be refused by a model with a lower, uncatalogued limit; the gateway retries at 4,096.
- A provider still rate limiting after every retry pauses the run (resumable) instead of failing it.
- `tools/ai_e2e.py --live` on OpenRouter's free models (`dots-3-note-preview`): stream, shadow, tool use, gating and
  accounting pass; QC failed (empty replies, then the free tier's 429s). The same check on the pre-change code ran
  after the free quota was spent and was refused before any call, so there is no comparison yet.

## Threats to validity

- **Cache carry-over between runs.** Provider prompt caches outlive a run (5 minutes, or 1 hour on Claude Code's main conversation), and they can't be cleared.
  - C2's first coordinator call read 119.7k cached tokens that C1 had written, worth about $0.74 of C1's $2.82.
  - H1's first packet call read a 9.4k prefix cached by an earlier run.
  - Without carry-over, Claude Code would sit nearer C1's $2.8. The ABBA order spreads the effect but doesn't remove it.
- **n = 2 per arm, 5 markers.** The arms tie on quality, so this campaign can't rank them on judgement. Cost and speed differences are large next to the run-to-run spread.
- **Claude Code version matters.** 2.1.235, the version on PATH, maps `opus` to claude-opus-5 and can't serve claude-opus-5-5. An earlier campaign (`runs/2026-10-03-invalid-opus-alias/`) ran Claude Code's Opus tasks on the wrong model and is kept only as a record. The driver now pins every alias and refuses a Claude Code older than 2.1.280.
- **Provider path.** The harness goes through the Plexora gateway (sub-second overhead per call). That overhead is part of what is being measured.

## Rerunning

```
export BENCH_CLAUDE=<a Claude Code >= 2.1.280>       # when the one on PATH is older
python tools/bench_harness_vs_cc.py --campaign <name> preflight
python tools/bench_harness_vs_cc.py --campaign <name> snapshot
for r in H1 C1 C2 H2; do python tools/bench_harness_vs_cc.py --campaign <name> run $r; done
python tools/bench_harness_vs_cc.py --campaign <name> restore
python tools/bench_harness_vs_cc.py --campaign <name> collect
python tools/bench_harness_vs_cc.py --campaign <name> report     # quality.json, comparison.json, tables_<name>.md
```

Preflight checks:
- the models file agrees with the spec, and the Claude Code version;
- the gateway is reachable and has balance;
- the gate revision.

The production routes must serve the spec's models (`ai_task_routes`). Each run costs about $0.6 (harness) or $1.5–3 (Claude Code) at list price, and takes 3–5 minutes.
