# AutoQC in parallel (2026-11-24)

AutoQC used to send one packet at a time to the model and score its image
checks one after another. It now keeps up to 8 packets out at once, and only
when no answer still pending could change whether a packet is asked or what it
says. It also scores the checks on several threads. The decisions are the
serial run's, made with the same model calls.

## What runs side by side

| Packet | Waits for | Out beside |
|---|---|---|
| channel audit page | the scan | anything |
| score review (check × channel) | its check scored | anything (it reads only its own field) |
| stain check | its picture drawn | anything (a verdict changes no channel, cell or gate) |
| stain re-read, second score round | its own unit's previous answer | as above |
| visual pass | the whole bulk pass, every score review | stain checks only |
| candidate (confirm → scope → localize → grid) | the whole bulk pass, every score review, the visual pass | candidates whose map boxes do not meet, stain checks |
| final review | everything | nothing |

**Why the candidates stay in order.** Candidates that touch are decided in
the serial rank order. Four things couple one candidate's decision to
another's, and each needs their masks to overlap: `_explained_by`,
`_merge_target`, `transitions._absorb` and a confirm's neighbour list. So the
order of decisions over each overlapping pair is the serial run's. The run is
conflict-serializable, and its decisions are the serial run's.

**The serial run was not deterministic either.** Before this change, a
candidate could be looked at while the bulk pass was still scoring checks.
Whether a check's region absorbed it, and how its outline was traced, then
depended on which finished first: the look or the scoring. The result followed
the machine's speed: on the `mixed` scene, merely reordering the checks
changed the serial run's calls (14 to 13) and its regions. Candidates and the
visual pass now wait for the whole bulk pass in both the serial and the
parallel run (`QCEngine.region_checks_scoring`). The engine's own design
intent ("checks before the detector candidates") now always holds. Stain
checks, score reviews and audit pages still overlap with the bulk pass.

## How it was measured

`bench_qc.run_harness` (arms `harness:N`) runs the real `QCRun` with N lanes
against spatialagents' `FakeGateway`:

- **The stand-in model** waits each task's median latency from the local
  trace's 13 QC runs (`TASK_LATENCY_S`), scaled down and jittered, then
  answers from the scene's truth (`QCTruthAgent`, oracle).
- **Each arm gets its own copy of the scene**, so the stain ledger and the
  caches an earlier arm left behind cannot answer for it, and
  `reuse_answers: false`, so no arm replays another's answers.
- **`result_signature`** compares what each run decided: every region's class,
  action, state, channels and outline, every channel's state, every stain
  verdict and the failing cells.

Real latencies from the trace: stain check median 104 s (47 calls),
artifact inspection 9.9 s, blur and registration reviews about 4.8 s,
channel audit 7.1 s, visual pass 11.1 s, final review 3.8 s. The stain checks
are most of a run's model time.

## Results

**Four scenes at a quarter of real latency** (stain check 26 s), ±20% jitter:

| Scene | Lanes | Wall s | Model calls | Input tokens | Cache read | Same decisions |
|---|---|---|---|---|---|---|
| mixed | 1 | 101.1 | 9 | 153,987 | 120,498 | — |
| mixed | 2 | 57.7 | 9 | 149,418 | 115,936 | yes |
| mixed | 4 | 45.6 | 9 | 147,124 | 113,649 | yes |
| blur | 1 | 86.1 | 7 | 115,871 | 87,187 | — |
| blur | 4 | 42.5 | 7 | 109,010 | 80,340 | yes |
| dropout | 1 | 88.5 | 8 | 133,757 | 102,815 | — |
| dropout | 4 | 44.7 | 8 | 126,889 | 95,962 | yes |
| fold | 1 | 91.3 | 6 | 100,465 | 73,797 | — |
| fold | 4 | 42.8 | 6 | 93,593 | 66,950 | yes |

**The sixteen-channel `cyclic` scene** (12 stain checks) at a tenth of real
latency:

| Lanes | Wall s | Model calls | Input tokens | Speed-up |
|---|---|---|---|---|
| 1 | 153.7 | 21 | 377,535 | 1.0× |
| 4 | 51.4 | 21 | 346,698 | 3.0× |
| 8 | 40.8 | 21 | 339,528 | 3.8× |

Repeated at a twentieth of real latency (±30% jitter): 1 lane 87.4 s; three
4-lane runs 33.6–33.9 s; two 8-lane runs 27.1–30.5 s. Every run made 21
calls, every signature was identical (check states included), and nothing
was reissued.

**A race the benchmark found, now fixed.** Two blur checks scoring at once
both read-modify-wrote the project's blur pointer file (`current.json`), and
Windows refused the second replace (WinError 5). The check was skipped
(`failed: Access is denied`): one fewer score review, and a fallback
detector in its place. Two other outcomes were possible as well: a lost pointer,
whose result the sweep then deletes, or a later "scores no longer cached".
`blur._point` and `_sweep` now run under one lock, and both blur's and
registration's `_atomic_write` retry a refused replace. `result_signature`
now includes each check's state, so a skipped check counts as a different
run.

- **Calls:** never more than the serial run's, with no reissues and no invalid
  answers.
- **Tokens:** input is 4–10% lower in parallel, because each lane's rolling
  worker carries a shorter history. Cache writes are unchanged, since the warm
  gate writes each task's prefix once.
- **Consistency:** the signatures were identical in every comparison: 8 scenes
  at 1 and 4 lanes (±30% jitter), 12 jittered 4-lane repeats of `mixed` and
  `dropout`, and the runs above (the cyclic ones after the write-race fix).
  `tests/test_qc_parallel.py` pins this.

**What this means for a real image.** On a 40-channel image, about 40 stain
checks at 104 s take about 70 minutes serially. They are independent, so 8
lanes need about 9 minutes, rate limits permitting. The bulk pass's checks
now score 3 at a time (`PLEXORA_QC_CHECK_WORKERS`), with stain pictures and
registration first so the slow looks start early. The bulk pass was not
timed on a real project for this report: the stand-in runs use small scenes
where scoring takes seconds.

## What else changed

- **Gateway:**
  - A limiter (`_Throttle`) drops a lane on each rate-limit retry and adds it
    back after 10 clean calls.
  - The gateway's own `rate_limited`, once its retries are spent, now pauses
    the run so it can be resumed, instead of failing it.
  - The first call of each task goes alone until its reply's first piece
    (`WARM_WAIT_S`), so the prefix is cached once.
  - Keep-warm pings are shared per task.
  - Stop cuts a reply that is still streaming.
- **Engine:**
  - A reader told `busy` sleeps until an answer wakes it, instead of taking
    the session lock every 0.1 s.
  - It re-checks stop and pause each time it wakes. Before, a lane woken
    after a Stop could still draw a packet.
  - The memo reads each file once.
- **UI:**
  - The panel lists the looks out ("N at once", folded by default), keyed by
    packet id, so answers can arrive in any order.
  - The headline stays on the oldest look.
  - Progress counts never step back (`progress.at`).
  - Retries are shown on their row.
  - A pause says the looks already out are kept.
  - The run's final spend shows even after the session's `finished`.
  - QC's sidebar reloads are coalesced (400 ms, at most 2 s, one at a time).
  - Two existing bugs are fixed: QC's "paused" state was never shown, and the
    gating classifier ignored `answered.closed[]`.

## Round 2: lanes sized from the panel, external lanes, packets drawn outside the lock

**Lanes come from the work.** A fixed 8 left a 40-channel image's 40 stain
checks queuing for lanes. A session now sizes its lanes from what it plans:
stain checks, plus one lane per 4 score reviews, plus one, clamped to 2..32
(`lanes_for`). A 5-channel panel gets 5 lanes and a 40-channel panel 32. A
number given at the start, or `PLEXORA_QC_LANES`, still wins. More lanes only
help while there are more independent looks than lanes. Candidates stay in
rank order wherever they touch, so the cap is set by the stain checks.
Gating keeps its cap of 8.

**A pace across the process.** With up to 32 lanes, and several runs at
once, the limiter that reacts to rate-limit retries is not enough. Every call
and keep-warm ping now passes a shared per-minute pace
(`PLEXORA_AI_CALLS_PER_MIN`, default 100) before it is sent.

**External MCP workers get lanes.** The QC delegate block now lists
`min(8, lanes)` workers. Each is its own reader that passes `parallel`, all
launched in one message, as gating's are.

**Packets drawn outside the session's lock.** Real sessions'
`decisions.jsonl` show what building a packet costs: median 2.1 s, p90 7 s,
max 25 s (audit pages 6.3 s, score reviews 2.65 s, confirm sheets 2.5 s).
While one lane built a packet, every other lane waited for the lock. Now,
for audit pages, score reviews and confirm sheets:

1. Under the lock, the units are chosen and reserved. A reservation counts
   as out and lapses after 120 s.
2. Outside the lock, their panels are drawn into the content-keyed panel
   cache.
3. Under the lock again, the packet is built, reading the panels from the
   cache, but only if no unit moved meanwhile.

The panel cache grew from 64 entries to 1024, capped at 256 MB
(`PLEXORA_QC_PANEL_CACHE_MB`). `PLEXORA_QC_PREPARE=0` turns this off.

All runs below use the stand-in model at 0.05× the traced latencies.

| Cyclic scene (16 channels) | Wall s | Model calls | Panel cache hits / misses | Same decisions |
|---|---|---|---|---|
| 1 lane | 87.7 | 21 | 5 / 154 | – |
| 8 lanes | 29.3 | 21 | 156 / 154 | yes |
| lanes from the panel (16) | 29.0 | 21 | 156 / 154 | yes |

At 8 lanes, the time spent building packets under the lock was:

| Scene | Before | After |
|---|---|---|
| cyclic | 4276 ms | 3321 ms (−22%) |
| mixed | 1980 ms | 1411 ms (−29%) |

The decisions were the same in both cases.

Sixteen lanes match eight here, because the scene has only 12 stain checks.
The gain from more lanes appears on panels with more stain checks than 8.

## Round 3: tokens paid twice (AutoQC and auto gating)

A review of both workflows looked for the same information paid for in two
steps. What the trace (`<data root>/.agent/ai/trace.sqlite`) showed first:
$12.5 of ~$20 of QC spend was stain-check replies cut at the 16,000-token cap,
all before the runaway fix (b346fd45); gating's spend is mostly `cell_panel`
looks, one call each, ~12k input tokens (mostly cache reads).

**The prefix is written once per answer schema.** Changing
`output_config.format` invalidates the prompt cache, and the provider adds
its format prompt ahead of the system prompt: every QC kind's first call read
0 and wrote ~20k (17,425 / 17,765 / 18,301 / 18,535 tokens read afterwards for
stain / audit / score / confirm). One schema for all kinds is out of reach
(24 optional and 16 union parameters per request; `schema.VERIFIED` is lower
still), and per-task schemas already exist. So the prefix itself shrank:

| Prefix | Before | After |
|---|---:|---:|
| QC | 13,902 tokens | 6,296 |
| gating | 7,635 | 4,021 |

- Both guides leave out `answer_schemas` (the call's own schema carries every
  field's words) and `answer_with` (the harness calls the tool):
  `prefix.HARNESS_DROPS`. A packet pointing at `answer_schemas` is pointed at
  its call's schema (`decision._own_schema`); one asked in words carries the
  schema itself.
- QC's prefix carries the qc-image skill cut to what judges a packet
  (`prefix.skill_digest`: the opening, step 4's kinds, Uncertainty, Failure
  modes), not the external agent's steps.
- The answer model's docstring is no longer sent as the schema's description
  (`schema._provider`); some were developer notes.

**Independent packets get a worker of their own** (`Workflow.solo_kinds`:
QC `stain_check`, `visual_scan`, `final_qc_review`; gating `cell_panel`,
`qc_confirm`, `t1_strip`): never written to the cache, and the task's worker
waits parked. They were re-reading earlier packets at every call and paying
1.25x to write a turn nothing read. A keep-warm ping after such a packet is
the prefix alone, with the same schema.

**Bench** (stand-in model, `harness:1` and `harness:auto`, the same scenes as
above; before is fd1a049d, after is a41e81c4; cost at the stand-in's prices,
reads 0.05x and writes 1.25x of input). The stand-in's cache keys on the
answer schema as the provider's does (`SchemaKeyedCache`, below); a first
measurement on a stand-in that did not showed only −22% to −29%, because it
let every kind read one shared prefix.

| Scenario, arm | Input tokens | Cache writes | Cost | Decisions, calls |
|---|---|---|---|---|
| cyclic, 1 lane | 377,535 → 186,736 | 131,836 → 88,706 | −34% | same |
| cyclic, auto | 330,484 → 180,453 | 129,571 → 86,700 | −33% | same |
| mixed, 1 lane | 153,974 → 82,765 | 91,358 → 55,574 | −39% | same |
| mixed, auto | 140,532 → 76,219 | 86,945 → 51,208 | −41% | same |

**The stand-in's cache and the harness's verdicts key on the schema.** The
provider puts a format prompt ahead of the system prompt, so a prefix is
cached per answer schema. spatialagents' `FakeGateway` now keys on it
(a62d0d4, after 0.2.6); until Plexora pins a release with it, `bench_qc.
SchemaKeyedCache` is mixed into the bench's and the tests' stand-in. The bench
reports `prefix_writes` and `answer_schemas`, and `test_qc_parallel` pins one
write per schema at 1 and 4 lanes. The harness's cache monitor keyed a prefix
on the system prompt alone, so every kind's first call (and a warm worker's
first call under a second kind's schema, as `artifact_inspection` asks
`score_review` then `artifact_confirm`) was logged as a "prompt-cache miss";
it keys on the schema too (`_cache_key`), and those calls are cold.

**Fixed on the way:**

- A serial gating run trimmed context-sheet rows "already shown" by the
  session to whichever worker the packet went to. The harness now asks for
  `sheets: full`, as it already did for `evidence`.
- The visual pass was served to harness workers, which have no tools, with
  "outline each with the tools". QC's `agent_tools: false` (the harness's
  start) asks them to list each place in `left` for a person instead.

**Changed what the model sees** (not measurable on the stand-in model, which
answers from the truth):

- QC's confirm brief carries `audit_said`, the class the audit named (it was
  stored as `agent_hints` and read nowhere).
- A deeper confirm look is one row (crop, nuclear stain, clean field): the
  neighbourhood was look 1's panel again. That is a quarter less image.
- Gating's `qc_confirm` read `real_signal` on a marker whose stain QC read
  `real` stands on one reading (`second_verdict.agreed_real`): with that stain
  verdict gating cannot close the marker failed (`blocks_failure`), so more
  readers could only send it to a person.

**Left as it was, and why:**

- Score review round 2 redraws rows round 1 showed. Trimming them would need
  the threshold logic to carry round 1's verdicts, for an estimated <1% of a
  run's tokens.
- The audit's nuclear reference tile on each sheet is the comparison anchor
  for that sheet.
- QC's stain reading of a channel the audit called failed: the stain check is
  never decided from the whole slide alone (stain.py), and such channels are
  rare.
- Stain rereads, contested gating rereads and partner review's second draw
  are deliberate independent reads.

The answer memo keys on a packet's text and image bytes, so packets whose
contents changed (confirms with `audit_said`, deeper looks, the visual pass)
are asked again once rather than replayed from an earlier run.

## Round 3, live: before and after on the real gateway (2026-11-25)

exemplar-001 (12 channels, 2511 x 3138, 11,201 cells), copied twice
(`exab-before`, `exab-after`: own image files, so no shared QC state, stain
ledger or memo), the set-up answers of exemplar-001 applied to both. Before
is fd1a049d, after is ba951908; default lanes; one run per arm. Credits are
the gateway's metered price of each call (trace `model_calls.price_micro`);
both gating runs reached their 225-credit quote, so their charge says
nothing.

| Run | Calls | Input tokens | Cache writes | Output | Metered credits |
|---|---|---|---|---|---|
| QC before | 21 | 425,294 | 194,007 | 6,204 | 118.7 |
| QC after | 21 | 225,841 | 88,742 | 6,490 | 73.5 (−38%) |
| Gating before | 39 | 511,284 | 116,511 | 43,386 | 306.0 (7.85 a call) |
| Gating after | 44 | 351,561 | 38,039 | 51,137 | 300.3 (6.83 a call, −13%) |

- **QC:** the same packets and calls, the saving the bench predicted. Its
  decisions differ in two score reviews, both DNA_7 and both one step:
  registration (before stepped once, then accepted what after accepted at
  once, with the same strata) and blur (both read every borderline cell
  normal; before answered `too_aggressive`, after `accept`, against its own
  strata and the packet's rule). The trimmed skill keeps every line on
  score review, so this reads as run-to-run variation, not lost guidance.
- **Gating:** dearer output (43-51k tokens, at 5x input) is most of its
  cost, so the input saving is 13% a call. `qc_confirm` took 2 reads, not
  4 (`agreed_real`). The after run took 7 more cell looks, so its total is
  about level. Its markers: 8 to manual review and none accepted, against
  before's 6 to review, 2 accepted (CD57, FOXP3) and ELANE no positive
  population; rereads 16 against 10. CD57 traced: the per-cell reads of each
  panel are nearly the same in both; after's second reader disagreed on one
  `uncertain` cell, which asked a third and spent the looks. What differs by
  design is that a gating look is a worker of its own (`solo_kinds`): before,
  one worker answered all CD57's looks and saw its earlier ones. One run per
  arm cannot say whether that, or chance on contested cells, moved the
  outcome.
- **The warm gate keyed on the task.** artifact_confirm has two forms (one
  candidate, a batch), so two schemas in one task; a lane started a form's
  first call beside another of that form and both wrote the prefix (both
  runs). The gate keys on task and schema (`_await_warm(task, key, lane)`),
  and a worker skips it only when its last call was under the same schema
  (`_Worker.cache_key`).

## Not done

- **Replicates.** One run per arm. Gating's outcome difference wants a
  second run of after, and one of after with gating looks sharing a worker
  again (`GatingWorkflow.solo_kinds` without `cell_panel`), before the solo
  change is called neutral for gating.
