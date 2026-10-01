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
deterministically. Several agents may answer one session side by side
(`gating_next(reader, parallel)`, below); each still holds one packet at a
time.

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
| Set-up | Before any look: the expression matrix when the values cannot settle it (`expression_setup`, blocks the bulk pass), the pixel size of an image that states none (`pixel_setup`: an estimate from the cells' size and three snapshots with a bar and a 10 µm ring; runs beside the bulk pass), unknown markers (`panel_context`) | ~0 / ~400 / ~0 |

Every look's context sheet draws its tissue fields at `SessionOptions.field_um`
(400 µm by default, 300-500; `presets.PRESETS["gating_context"]`) converted
with the image's own pixel size, or the session's value from `pixel_setup` for
an image that states none (drawn as approximate, "≈"); only a value the user
stated is written to the project (`set_pixel_size`, receipted into the
session). A T2 or T3 look may answer `within_partner`: the marker is refitted
among a `subset` partner's positives (`bivariate.within_partner`, the table
operation `gating.autogate.within`), shown again in a conditional look (the
collage holds only partner-positive cells), and written as the plain gate at
that threshold with method `ai_conditional` and `detail.condition`; its
confidence is capped at moderate. The gate rows, the GPU shader and the AnnData
export are unchanged.

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
    candidates.py    guard band, candidate thresholds (the analytical tools)
    lattice.py       a marker's fixed candidate points, the T4 chain, row placement
    memo.py          answers kept per packet hash, replayed on identical packets
    pixel_estimate.py the pixel size from the cells' own size; the pixel_setup snapshots
    sheet.py         the context sheet: fields at three classes (or one per candidate),
                     the whole image, the partner plot
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
  or an about-right look replaces it; a unit closed meanwhile (refinement not
  allowed) is `insufficient_information` with the gate `proposed` -- recorded
  in provenance, not written (`Engine.finalize`).
- **A marker is never accepted because it ran out.** Reaching an allowance
  (looks, or rounds of candidates) while the evidence says to go on goes to
  the session's limit policy (`Engine.limit_reached`, `schemas.LIMIT_POLICIES`):
  `ask` holds that marker while the user answers a card in the agent panel
  (the calling agent sees `waiting_for_user` and may relay the answer through
  `gating_session_status(limits=...)`), `extend` grants another allowance,
  `stop` flags it. Past `max_extensions`, or on a no, the marker is
  `manual_review_recommended` with the best gate `proposed`
  (`Engine.close_at_limit`). The same holds for a look that could not settle
  the gate: an unsure first look with no reference, a reference view that left
  it uncertain, a whole-image check that cannot tell -- review, not a
  low-confidence acceptance. `plexora mcp serve --gating-on-limit` and
  `plexora ai bench gating --on-limit` set the defaults (environment
  `schemas.LIMIT_ENV`), so unattended runs do not stop on a viewer default.
- **The panel narrates, it does not quote.** Each packet carries a
  `narration` for the user (`packets.narrate`, templates in
  `schemas.NARRATION`) and the thumbnail a label (`schemas.EVIDENCE_LABELS`);
  the question put to the agent is never shown in the viewer.
- **The agent's windows are never the user's.** The bridge's lease
  (`agentBridge.js` `takeLease`) snapshots the slots and the sidebar's
  per-channel memory (`snapshotChannelMemory`: remembered windows) and holds
  channel saves off until `restore`, which runs on finish, stop and take-over.
  Remembered windows are kept in raw units and converted on use
  (`overrideInDomain`); a collapsed or off-scale window is never saved and is
  auto-levelled when loaded (`validRawRange`). The first live run had left
  every inspected channel at `[255, 255]`: a raw window read as bytes.
- **Starting over** is `reset_gates` (a reversible write: a snapshot of the
  gates and their provenance, undone by `restore_gates` through
  `undo_operation`); its receipt tells open viewers, which reload rather than
  autosave their old gates back over the reset.
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
- **The fit ignores QC failures.** Where QC has been run (a session, or regions
  drawn by hand in a QC category), the cells it failed are left out of every
  estimate: the mixture fit (`model.fit_for`, `gmm_for`, so the Auto button
  too), the prepared column (`profile.column`), every sampler (through
  `cells.values`, which returns NaN for them), the collages, galleries, sheets
  and report histograms, and the validation fields (`gate_sampling` rejects a
  window more than `cell_exclusions.FIELD_QC_MAX_FRACTION` failures; panel B
  draws them grey). `SessionOptions.qc` (and `qc` on every estimating tool)
  says which: `strict` -- exclude and warn calls, plus a marker's own flags --
  `exclude`, or `off`. The gate still applies to every cell: gate storage,
  `range_mask` and `apply_gate_to_dataset` read the whole column, and an empty
  gate sits at the table's own maximum. The seam is core
  (`plexora/agent/cell_exclusions.py`, `Plugin.cell_exclusions_factory`), so
  gating never imports QC; a table on a data node gets the record in the
  operation's payload. Every result and packet carries `qc_exclusion`; the
  memo key includes its fingerprint; a gate's provenance records it
  (`detail.qc_exclusion`) and `gating_qc` lists `stale_qc` when QC has
  changed since. More than `cell_exclusions.HEAVY_EXCLUSION_FRACTION` left out
  caps confidence at moderate (a cap, never a route).
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
- **Several packets out at once** (parallel markers). The packets out are
  `record["outstanding"]`, `{packet_id: {kind, memo_key, units, reader,
  fingerprint, strict, invalid_answers, seq}}`; `outstanding_packet` /
  `outstanding_kind` name the newest, for the viewer's phase and the session
  resource. A record from before the map (the three scalars, with
  `invalid_answers` and `last_unit` beside them) is migrated on load.
  `gating_next(reader=..., parallel=N)` lets up to N out across readers; the
  default (no reader, `parallel=1`) is the single-packet loop, unchanged.
  `Engine.ready_units` decides what may go out beside the rest: a marker
  waits until every partner it could be judged beside that comes earlier in
  gating order (`Engine.depends_on`, either direction of a vocabulary
  partner at moderate or better), and the partner it is gated `within`, is
  terminal; `panel_context`, `expression_setup`, `pixel_setup` and the T1
  strips are exclusive (`Engine.EXCLUSIVE_KINDS`), and a strip that is due
  waits for what is out to be answered. A reader keeps to the marker it
  answered last (`readers[reader].last_unit`). A packet issued beside others
  is `strict`: it records the fingerprint of the partner gates it was built
  on (`Engine.ledger_fingerprint`), and an answer to it after they changed is
  refused with outcome `reissue` -- nothing applied, the same decision served
  again to that reader. The fingerprint is part of the memo key. Each reader
  has its own `briefed` epoch (the default reader's is `record["briefed"]`),
  so an evidence pointer can only name a packet that reader was sent; a
  reader's packets carry `briefed: {reader, epoch}`. `gating_next` says
  `busy` while everything left waits on another reader's answer.
- **The reading guide travels once.** `gating_session_start` and
  `gating_session_status` return `packets.READING_GUIDE`; a packet names the
  entries it relies on (`evidence.guide`) and carries only what is its own in
  `how_to_read` (usually nothing). The guide also holds the compartment
  readings and every answer schema (a packet's `answer_schema` is
  `{see: ...}`), in a fixed order, so it is the same bytes for every session of
  a build: a client caches it, and `guide_version` / `known_guide` skip sending
  it again. `packets.lean` sends evidence numbers to four significant figures
  (gate values exact) and drops empty fields; packets carry only their own
  charge and a unit count. `SessionOptions.reading="every_packet"` puts the
  texts and schemas back in every packet. Packets carry a profile digest (`packets.profile_digest`), the
  hard flags only, and `partners` first; the full profile is `profile_marker`.
- **The plot partner is the informative one** (`packets.plot_partner`): the
  condition's partner, else one a `bivariate` request named, else the one whose
  numbers contradict the gate most; the sheet says why (`plot.why`).
- **An image-led compartment never raises `nuclear_bleed`**: its cell mean
  follows a nucleus-based mask whatever the stain does
  (`qc_cells.cell_qc`, `PROFILE_VERSION` "5").
- **T4 confidence is measured in the candidates' own steps**
  (`delta_step_sd`: the distance from the GMM gate in sds of the population
  the gate moved into), and a gate at a partner's negative control (`ctrl:`
  step) is not charged for its distance.
- **A conditional gate is conditional everywhere it is shown**: its collage,
  its flip cells (`sampler.delta_cells(within=...)`), its candidates' counts,
  its fields' outlines and its whole-image map hold only the partner's
  positives.

## 5b. Determinism

The code is deterministic (seeded samples, `random_state=0` fits, snapped
writes); what varies between runs is the agent. So the agent's answers choose
among fixed values and are kept:

- **The lattice** (`lattice.py`): every threshold a marker's gate may take --
  the Auto gate, the other estimators, each partner's negative control and
  within-partner fit, fixed steps, the band's edges -- snapped, merged and
  frozen on the unit the first time a look needs it (`Engine.lattice_for`).
  Every gate a look moves to is a lattice point, whatever the path.
- **Rows place the gate** (`lattice.place`): a T4 look shows the chain of
  points beyond the current gate, nearest first, stopping at the first
  control or within-partner fit; the agent judges every row and the server
  moves the gate past each row that says so. `chosen_candidate` is a
  cross-check that caps confidence when it disagrees. When every row says move
  and the chain ended at an anchor or at `MAX_CHAIN` with points beyond, the
  next round starts from there (`unit["t4_continued"]`); a round ends only at a
  row that says stop, the lattice's end, or the round limit. Each candidate's
  tissue field is a different field (`gate_sampling` `avoid`).
- **Words, not floats**: confidence is `sure` / `fairly_sure` / `unsure`
  (`schemas.AI_CONFIDENCE`), each a fixed number between the rule's cut-points.
- **Side channels do not route**: artifact flags cap confidence only; a request
  opens a reference look only when the answer was undecided.
- **The memo** (`memo.py`): each answer is kept under a hash of its packet
  (JSON less ids and charges, image bytes, code versions), per project and per
  `SessionOptions.agent`; a later packet with the same hash is answered from it
  (`reuse_answers`), so a rerun reaches the same gates without asking.
- **Dependencies**: each gate's provenance records the partner gates it stood on
  (`detail.depends_on`); `gating_qc` lists `stale_dependencies` when one changed.
- **Measured**: `plexora ai bench gating --synthetic all --stability N` gates each
  scenario N times with differently seeded agents and replays the first run;
  with a noisy agent (a fifth of its verdicts wrong) 31 of 35 markers reached
  one gate in every run and every replay was identical (2026-09-27).

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

A `pixel_setup` answer applies to every image of the session still waiting
for one (`evidence.applies_to`): one scanner is the usual case.

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
~0.2 s from a local pyramid. A look is two images, the T2 collage
(`collage.layout_pixels("t2")`) and the context sheet (`sheet.max_pixels()`,
1160 x 732: 384 px fields, a 288 px slide-scale row), about 1.4M pixels; `budget.UNIT_DEFAULT`
allows six looks (9.3M pixels; `tests/test_autogate_units.py` pins it), and
`ENGINE["t4_rounds"]` four rounds of candidates; each extension grants the
same again.

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
