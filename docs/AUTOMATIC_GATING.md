# Automatic gating

*For the people building Plexora. How "gate this image" and "gate this
dataset" work end to end, where each piece lives, what is deterministic, what
an agent is asked, and the rules nothing may break.*

---

## 1. The shape of it

Plexora has no model of its own, and the MCP clients it serves cannot be
called back. So automatic gating is a **server-driven session**: the server
does every step code can do and hands an agent **one small decision packet at
a time**; the agent answers with a typed judgement, and the server moves on
deterministically.

```
gating_session_start ──► bulk job: calibrate display, profile every marker,
                         settle what numbers can settle (T1 accept, QC fail, skip)
gating_next ───────────► one packet: question + compact JSON + <= 2 small images
gating_answer ─────────► typed answer -> transition -> (write gate) -> next packet
gating_session_finish ─► close | commit (propose mode) | cancel | rollback
gating_report ─────────► HTML + PDF review report
```

The agent never types a threshold. It judges whether staining is real, says
which way a gate is wrong, picks among candidate thresholds the server
proposed, or flags an artifact.

## 2. Tiers

| Tier | What decides | Cost to the agent |
|---|---|---|
| T1 | Clean bimodal marker (separation, valley, stability, estimator consensus, no technical flag): the GMM gate is written by the bulk pass and shown on a batched **audit sheet** (`ENGINE["strip_batch"]` markers a sheet) | ~70 vision tokens per marker |
| QC | A hard technical flag (`schemas.HARD_FLAGS`: saturation, empty channel, illumination gradient, no fit): one whole-image overview, `real_signal` / `technical_failure` | ~350 |
| T2 | Everything else: a collage of cells below / at / above the gate (the `t2` layout's panels) + a positive-cell map, and the whole-image numbers against every partner gated so far; answer = plausibility + direction | ~650 |
| T3 | T2 could not tell, or asked for a reference: the same with a reference channel, plus bivariate numbers (and a density plot only when the contradiction is anomalous) | ~700 |
| T4 | A direction was given: a few candidate thresholds (`candidates.STEPS` inside the guard band, an overshooting step clipped to the band's edge; `EQUAL_COUNT_SHARES` in an empty valley) and the cells that flip between them | ~320 |
| Regression | Seven numeric whole-image checks on the chosen gate (`regression.py`); one overview only if one fails | 0 (or ~350) |
| T5 | What only the user can say (binary vs continuous, expected prevalence): stored as a question, asked at the end | — |

Confidence (`high` / `moderate` / `low`, or `manual_review` / `failed_qc`)
comes from a fixed rule table (`engine.confidence_for`) over the numbers and
the agent's answers. A self-reported confidence cannot lift a marker whose
populations overlap.

## 3. Where things live

```
plexora/server/utils/jit.py              numba shim (identity fallback), primers, prime()
plexora/server/utils/label_kernels.py    label boxes, outline rule (compiled)
plexora/agent/evidence/                  generic pixel evidence
    image_qc.py      overview QC per channel
    calibration.py   the stored display calibration (plugin store "display")
    crops.py         batched per-tile cell crops
    collage.py       pixel-budgeted collages, whole-image overview (PNG stored, WebP sent)
    density_plot.py  two-marker density from the bivariate grid
plexora/agent/sessions/                  generic decision sessions
    store.py         on-disk session, packets, decisions.jsonl, control.json, lock
    budget.py        characters and pixels per unit and session
    mirror.py        best-effort viewer script runner
plexora/plugins/gating/server/autogate/  the intensity-marker implementation
    profile.py       Column, estimators, split-aware pools, class, T1 score
    qc_cells.py      tile statistics, Moran's I, illumination, size, nuclear, edge
    cells.py         row-aligned geometry, neighbour grid, density
    kernels.py       grid-hash neighbour counts (compiled)
    sampler.py       strata, delta (flip) cells, quadrants
    bivariate.py     quadrants, orphans, contradiction score
    candidates.py    guard band, candidate thresholds
    regression.py    the seven numeric checks
    reference.py     cross-image alignment, drift classes, strategy
    transfer.py      carrying the reference image's gates to the rest
    context.py       panel context (vocabulary first), order, references
    provenance.py    sidecar table: method, status, confidence, locks
    engine.py        the session state machine, writes, confidence
    packets.py       one builder per packet kind
    transitions.py   one handler per answer kind
    answers.py       the typed answers (pydantic, discriminated by kind)
    bulk.py          the deterministic pass (a job)
    mirror_script.py what the viewer is told, from the collage manifest
    report.py        HTML + reportlab PDF; CSV export
    tableops.py      the column work as table operations (runs on a data node)
plexora/plugins/gating/capabilities_autogate.py   analytical tools
plexora/plugins/gating/capabilities_session.py    the session tools
plexora/ai/knowledge/markers.yaml, vocabulary.py  shipped marker vocabulary
plexora/ai/skills/{gate-image,gate-dataset,review-gating,diagnose-marker}
plexora/mcp/prompts.py, resources_gating.py       prompts and plexora://gating/*
plexora/ai/bench.py, bench_data.py                `plexora ai bench gating`
```

## 4. Invariants

- **Nothing in the agent layer imports `data_model`** (the autogate folder is in
  `tests/test_agent_architecture.py`'s scan). Column work is a table operation
  with a local fast path (`tableops.local_or_node`), so it runs where the
  table is.
- **The gate's value stays in the sidebar's row list**; everything else about a
  gate (method, status, confidence, the decisions behind it) is in the
  provenance sidecar, because the browser saves the row list whole.
- **Locked and approved gates are never written by an agent** (`model.set_gate`
  raises `GateLocked`); the sidebar's own save restores a locked marker
  (`/save_gating_list`). A gate the user changes during a session wins; the
  marker goes to review.
- **Every write is a child receipt** of the session's operation
  (`<op>.<nnn>`) with an undo hint; `rollback` replays them newest first.
- **Numbers travel in JSON, pixels in images.** Packets are self-contained, so a
  new conversation continues a session with `gating_next(session_id)`.
- **The collage manifest is the only source of what a mirrored viewer is
  shown**; a preview on the slider is never saved (`agentPreview`).
- **Context never moves a gate.** The vocabulary outranks agent-supplied
  biology, which can only order markers, choose references and lower a
  confidence.
- **A gate is never written against the agent's direction.** `unit["direction"]`
  holds the way the last look said the gate is wrong until a candidate, `keep`
  or an about-right look replaces it; a unit closed meanwhile (its looks spent,
  refinement not allowed) is `insufficient_information` with the gate
  `proposed` -- recorded in provenance, not written (`Engine.finalize`,
  `close_on_budget`).
- **Kernels are compiled before any request** (`jit.prime()` in
  `prime_hot_code` and at MCP start), never with `parallel=True`.

## 5. Budgets, guards and windows

- **A unit's budget counts looks** (`engine.BUDGETED_KINDS`: T2, T3, T4;
  defaults `budget.UNIT_DEFAULT`). Technical checks, whole-image confirmations
  and transfer checks are bounded by the state machine (its loop table is in
  `transitions.py`'s docstring) and always issued, so a confident refinement is
  never thrown away for want of a confirmation.
- **The fit ignores unmeasured cells.** A spike at the column minimum set well
  apart from the body (`model.floor_spike`) is left out of every mixture fit --
  the Auto button's too -- and of the dynamic range and the cell-QC
  background; the cells are negatives.
- **Two regimes for "the distribution says no"** (`candidates.contradicts`):
  with the populations clearly apart (`profile.THRESHOLDS["bimodal_d"]`) the
  mixture means are a ceiling; with them overlapping, a direction is refused
  only at the guard band's edge. A candidate step that would overshoot the band
  is clipped to its edge, and `none_separates` at the edge ends the unit.
- **The whole-image flip check scales with the step** it is checking: never
  stricter than what the smallest candidate step flips.
- **Requests are honoured.** A T2 answer asking for a reference channel goes to
  T3 beside that partner when one is gated (by the run, or a user gate the run
  kept), ahead of a soft artifact flag; a technical check that finds real
  signal after a look gives the marker another look.
- **Display windows are capped from the cells**
  (`calibration.CELL_CAP_FACTOR` times the table's `CELL_CAP_PERCENTILE`, in
  image units), so bright debris cannot black out every cell; a window still
  wider than `THRESHOLDS["wide_ratio"]` is flagged. The record carries
  `calibration.VERSION`; an older one is recomputed.
- **Mirroring reports itself.** Each packet's `mirror` says what the open tab
  was sent and whether it acknowledged; a `get_state` first wakes a background
  tab and skips set-up already in effect, and the HD swap has its own timeout
  (`mirror_script.COMMAND_TIMEOUT_S`). A viewer that predates `/agent/v1`
  answers `capability_unavailable` with "restart it".
- **`gating_next(rerender=true)`** draws the outstanding packet again (same id,
  same charge) after a renderer change; a packet whose images were lost is
  redrawn by itself.

## 6. Datasets

The reference image (the user's choice, else the one with most cells) is gated
in full first. Each other image is profiled by the bulk pass meanwhile and its
markers wait (`transfer_pending`); once the reference's marker is settled, the
image's quantile curve (p5-p95, fit space) is aligned to the reference's by
least squares, and the reference gate carried through the line:

- `stable` -> the aligned gate is accepted (audit sheet), confidence following
  the reference's;
- `image_specific_shift`, `smooth_drift`, `batch_effect` -> one
  `transfer_check` packet (reference cells beside this image's);
- `distribution_change`, `staining_failure`, a reference that was not accepted,
  an aligned gate outside the guard band -> the image's own full evaluation.

`gating_session_status.dataset` gives each marker's classes and strategy
(`global_aligned`, `per_batch`, `globally_informed_per_image`, `per_image`).
`compare_gates_across_images` answers the same question without a session.

## 7. Numbers worth knowing

Measured on the synthetic scenarios (`plexora ai bench gating --synthetic all`,
oracle agent, 576-cell images; 2026-09-26): code agreement (the share of cells
whose whole positive/negative code matches the truth) 0.955 for the session
against 0.915 for the Auto gate, at ~20k vision tokens and 40 packets for 7
images. Real slides have far more cells, so more markers settle at T1.
Performance budgets: a marker's profile ~1-3 s (dominated by the Auto fit
itself); neighbour counts for 1M cells ~1 s once per project; a T2 collage
~0.2 s from a local pyramid.

Every cut-point marked `[cal]` in the code (`profile.THRESHOLDS`,
`qc_cells.THRESHOLDS`, `candidates`, `regression.THRESHOLDS`,
`reference.THRESHOLDS`, `schemas.ENGINE`, `calibration`'s cap) is a first guess,
to be calibrated on expert-gated data with `plexora ai bench gating --project ...
--truth uns:gates` before its default is frozen.

Nothing an agent reads restates these numbers: the tools' models derive their
Literals and defaults from the vocabulary modules (`schemas`, `context` /
`plexora.ai.vocabulary`, `collage.LAYOUTS`, `budget`), hints name tools through
`registry.tool_name_of`, the MCP instructions and prompts are rendered from the
registry and the skill manifest, and a skill's numbers are `{{placeholders}}`
filled from `skills.constants()`. `tests/test_ai_skills.py` fails a skill that
backticks a name the code no longer uses or puts a digit in its prose.

## 8. Not done yet

- A per-cell `obs` gate column (deliberately not written: full-height, not
  undoable, and derivable exactly from the gates and the documented rule).
- MCP sampling / elicitation: the packets and answers are designed so a
  sampling-driven loop can reuse them unchanged.
- Batching `transfer_check` across images of one marker; a calibration-check
  packet for a flagged display window (flags are passed into T2 instead).
- Modality plug-ins beyond intensity markers (transcript counts, morphology);
  the seams are the table operations, the distribution class rules and the
  renderer.
