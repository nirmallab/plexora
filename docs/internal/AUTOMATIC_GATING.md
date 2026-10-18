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
| Absence | Before a marker's first look, and after a technical check calls a stain failed or empty: six regions (`clear_positive`, `bright`, `high_density`, `borderline`, `weak`, `sparse`; 200 µm, the calibrated display, nothing marked), one word each (`clear` / `few` / `none` / `cannot_tell`); no judged region `clear` in two pictures of different regions closes the marker `no_positive_population` (§2d) | ~650 (a second only before a close) |
| T2 | Everything else: a collage of cells below / at / above the gate (each cell a morphology and a context panel, the judged nucleus between four ticks) + the context sheet, and one line per partner gated so far; answer = one word per row (`schemas.ROW_VERDICTS`: `own_stain`, `neighbour_only`, `unstained`, `mixed`, `cannot_tell`) and the stain `pattern`; code derives the direction (`verdicts.direction_from_rows`) | ~650 |
| T3 | T2 could not tell, or asked for a reference: the same with a reference channel, plus bivariate numbers (and a density plot only when the contradiction is anomalous) | ~700 |
| T4 | The rows said which way: the lattice points that way, nearest first (a capped chain keeps the nearest, never a far anchor), and the cells that flip between them, one word per row; a first row that says the opposite turns the look back once; a mixed row wider than two background sds is split and looked at again (§2d), a narrower one keeps the gate at moderate at most | ~320 |
| Regression | Seven numeric whole-image checks on the chosen gate (`regression.py`); one overview only if one fails | 0 (or ~350) |
| T5 | What only the user can say (binary vs continuous, expected prevalence): stored as a question, asked at the end | — |
| Set-up | Before any look: the expression matrix when the values cannot settle it (`expression_setup`, blocks the bulk pass), the pixel size of an image that states none (`pixel_setup`: an estimate from the cells' size and three snapshots with a bar and a 10 µm ring; runs beside the bulk pass), unknown markers (`panel_context`), and per image how bright each marker is drawn (`display_setup`: a contrast grid, below) | ~0 / ~400 / ~0 / ~850 per 8 markers |

Every look's context sheet draws its tissue fields at `SessionOptions.field_um`
(400 µm by default, 300-500; `presets.PRESETS["gating_context"]`) converted
with the image's own pixel size, or the session's value from `pixel_setup` for
an image that states none (drawn as approximate, "≈"); only a value the user
stated is written to the project (`set_pixel_size`, receipted into the
session). A T2 or T3 look may name a partner in `within`: the marker is refitted
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

## 2a. Spatial evidence (removed)

A session switch, `spatial` (`diagnose` / `stratify` / `decide`), read each
cell's surroundings -- crowding, the brightest neighbour, local background --
to explain where a gate's errors sat, spread the looks' cells over those
environments, put a move it tied to neighbours or background to a
like-for-like look (`t3_contrast`, a case table), audit T1 acceptances, add
two regression checks and draw an amber "uncertain" layer. It was removed on
2026-10-10: on real tissue `decide` engaged none of its rules, and the one
gain (`stratify` moving two gates toward the human ones through partner
confidence) came at +6-9 packets a session. The measurements are in
`docs/internal/bench/SPATIAL_ABLATION_2026-10-08.md` and
`docs/internal/bench/AUTOGATE_STEP_LEDGER.md`.

## 2b. The cell model (removed)

A small per-cell CNN read each cell near a looked-at marker's gate and said
whether its stain was its own; a session option (`hybrid`) let its confident
calls correct the gate a look approved, and the viewer drew its disagreements
beside the gate. It was removed on 2026-10-12 after the PCA benchmark
(`docs/internal/bench/AUTOGATE_STEP_LEDGER.md`): its per-cell overrides were
worse than the gates. A session stored with it resumes without it
(`Engine._migrate`). What it left is the multi-view look below.

## 2c. Multi-view looks (`tiles`)

One session option, `SessionOptions.tiles` (default False). On, every look's
collage draws each cell as a 2 x 2 block (`collage.LAYOUTS["t2m"]`, 64 px
panels): morphology; the context with the judged cell's outline white; one
expected and one not-expected partner channel, each windowed so its gate sits
at 40 % grey (`collage.PARTNER_GATE_GREY`), the judged outline magenta. The
partners come from the panel context's vocabulary relations alone
(`partners.partner_plan`, gated ones first; `Engine.partner_panels`); a look
that asked for a reference shows it in the panel its relation belongs to.
Guide entries `collage_t2m`, `collage_flips_m`. The pictures differ, so a
tiles session's packets carry `tiles:1` in their memo key; off, packets, memo
keys, provenance and gates are the session's without it, byte for byte. The
bench runs it as `--variants baseline,tiles`.

## 2d. Absence, wide mixed rows, whole-image disagreement (2026-10-18)

Three rules, each a session switch, from the region-look study and the user's
recheck of LSP11321 (`docs/internal/bench/GATEVISION_PHASE1_2026-10-18.md`):

- **`absence_check`** (default on). A mixture splits every column in two, so
  only the image can say a marker has no positive population. Before the
  first look, `absence.render` draws six regions chosen around the scored
  start's band (`absence.CLASSES`; `gate_sampling` gained `weak`, `bright`
  and `sparse`), unmarked; a picture says absent (`absence.absent`) when at
  least two regions were judged and none is `clear` -- a region without
  positives is no evidence while another shows them -- and the marker is
  closed only when a second picture of other regions agrees
  (`absence.ROUNDS`). Four regions in one picture closed LSP11361's dim,
  widespread E-cadherin (the user gates 26 % positive) in the first live
  run. A technical check's
  `technical_failure` / `no_positive_population` goes to the same picture and
  stands only if it agrees; clear regions overrule it (`absence_overruled`)
  and the marker gets its look. Measured: on three PCA images it closed the
  two markers the user called absent that the engine had gated (LSP11761
  PD1, CD20L) and no other of 51; LSP11321 CD20L was failed by a technical
  check whose positives at a low gate were epithelial artifact, though the
  user gates a real 1 % population there. Not budgeted; once per unit
  (`absence_done`).
- **`split_mixed`** (default on). A T4 walk stops at the first row that does
  not move it; a `mixed` row wider than `lattice.SPLIT_MIN_SD` background sds
  is split into at most `MAX_CHAIN` parts (`lattice.split`: evenly in fit
  space, snapped, joined to the frozen lattice, so a split gate is still a
  lattice point and two runs that judged the same row get the same points)
  and looked at again; a split is a round. With the chain cap keeping its
  nearest points, rows are wide only where the lattice has a gap (LSP11321
  FOXP3: 6.36 to its MART1 control at 6.97; the user's gate 6.93).
- **`whole_image_moves`** (default off). After a T4 look, a whole-image
  check's `too_low` / `too_high` no longer sends the gate back to the
  candidates (where a turn counted as oscillation and closed it for review):
  the gate stays where the rows put it, `whole_image_disagreed` on the record,
  at low confidence (the failed checks are unconfirmed). A review gate is no
  partner's evidence (`hierarchy.grade`); LSP11321's CD45 sat at 7.76 (user
  7.8) in review, and 13 markers were gated without it. Before any T4 look a
  whole-image direction still sends the gate to the candidates.

A session stored before these switches is completed with their defaults
when it is loaded, like every newer option.

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
    partners.py      the expected / not-expected partners a multi-view look shows
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
- **A marker is asked about once, anywhere.** What a `panel_context` answer
  (or `set_panel_context`) says about a marker the shipped vocabulary does
  not know is written to the vocabulary's learned layer
  (`vocabulary.learn`, through `context.learn_from`): a YAML file under the
  data root, or wherever `PLEXORA_VOCABULARY_PATH` points (a lab's shared
  folder). From then on it resolves in every panel (`source: learned`, ranked
  between the shipped vocabulary and an agent), so no later session lists it
  as unresolved. The shipped file always wins; the first account stands and
  only the user's word replaces a model's; `plexora ai vocabulary` lists and
  forgets. `panel_hash` reads the shipped layer only, so learning a marker
  never moves a panel's stored context.
- **A look never says which way.** It answers one closed question per row
  about the cells around the judged nucleus; `verdicts.direction_from_rows`
  turns the words into a direction, `lattice.place` (with `verdicts.MOVES`)
  into a lattice point. Two agents that see the cells alike move the gate
  alike.
- **The picture carries shape, the number intensity.** A marker is drawn
  from its own cells (`cell_window.measure`, read in a disc of
  `crops.CELL_DIAMETER_UM` around each centroid in level-0 crops, never
  inside a mask, never on a QC-left-out cell) on an asinh curve
  (`server/utils/display_curve.py`; linear when that already suffices) fixed
  by two display levels (`calibration.rule_from`): black at the negatives'
  low percentile, under the background, so the background shows as a faint
  floor; the background's noise p99 at `NOISE_LEVEL`; the titre -- the
  brightest cells' disc p99, their ring, nucleus or granules -- at
  `TITRE_LEVEL`, with room above it so the structure is not flattened. A
  marker without bright cells is drawn as if its titre were
  `TITRE_FLOOR_GAPS` background gaps above black (its noise is never
  stretched to white); a negatives band that is itself stained (a marker on
  most cells) is re-anchored on the low band. The morphology panel windows
  each cell on its own disc, floored where the context window draws
  `crops.MORPHOLOGY_FLOOR`, so a negative stays dark. Outlines are thin and
  grey; the ticks mark the centroid, which every project has.
- **A model checks the brightness, code sets it.** `display_setup` (per
  image, after the panel context, before that image's first look;
  `SessionOptions.display_check`, `vision` by default) shows up to
  `ENGINE["display_batch"]` markers in rows and the calibration's brightness
  ladder in columns (`calibration.columns_for`: every display level times
  `GAINS`, c1 darker to c5 brighter, c3 the rule) -- every marker with bright
  cells to show, the dim and floored ones too (`calibration.needs_choice`:
  they are the hardest to draw) -- each a 2 x 2 block of a
  background, a dim and two bright cells, clipped pixels in a fixed red
  (`agent/evidence/display_grid.py`). The answer is two lists of columns per
  row -- where the background is a faint floor, where the row's compartment
  pattern (`schemas.STRUCTURE_WORDS`) shows with detail -- and
  `calibration.pick_column` picks: the grid's numeric vetoes first, then the
  column in both lists nearest the rule's. The choice is stored on the
  project's calibration (`record_choices`, a gain over the rule, kept across
  recomputation) and asked once per image: a later session finds it. Two
  unusable answers keep the rule (`Engine.struck_out`). The task is
  `gating.display` (`vision_routine`: a narrow perceptual question a smaller
  model can serve). `display_check: off` leaves the step out; an image whose
  markers an earlier check (an auto-contrast run) settled asks nothing.
- **Auto contrast is also its own module.** `contrast_session_start`
  (`gating.contrast_start`; `plexora ai run contrast`; the AI panel's "AI Auto
  Contrast") starts a gating-engine session with `purpose: contrast`: the
  set-up the check needs (matrix, pixel size, the panel's compartments), the
  calibration -- the bulk pass profiles no marker -- and the contrast grids;
  each marker closes `display_set` when its image's brightness is settled, and
  nothing is gated or written but the calibration's choices. `redo` takes an
  earlier run's choices off first (`calibration.forget_choices`). The harness
  answers it as gating's `gating.display` task and the gateway prices it as
  gating, one unit per grid (`decision.contrast_grids`).
- **The viewer shows the same brightness.** The canvas draws the curve
  (`frag.glsl` `range_clamp`, `services/displayCurve.js`, the mini-map and
  the 3-D view; one "Log brightness" switch for the image, in the Image card's
  menu, with the Auto wand fitting in that mode). A mirrored session shows each
  marker the check just judged being set, right after the answer
  (`capabilities_session._mirror_display` -> `mirror_script.run_display` ->
  `animate_contrast`, captioned "Adjusting contrast for X"): the marker
  replaces the one in the second channel slot (the other slots are off for
  the show), arrives at a rough window, and each end of its slider is dragged
  into place, thumb drawn held, the canvas following -- at a pace a person can
  follow, with a deterministic variety (`contrastGesture`: starts too dim or
  too bright in turn, the black end moved for two markers in three, a small
  overshoot and correction for every second or third, a gradual zoom on one
  of its bright cells, `focus`, for every fourth). The window shown is the one
  the canvas keeps: the choice's asinh window with Log brightness on, its
  `linear_window` off. The command is acknowledged at once, so the next packet
  goes to the model while it plays, and the tab holds the view commands that
  arrive meanwhile and runs them after it (only the newest look). A marker
  the rule or an earlier check settled is not played. The nuclear stain leads
  the first show, in the first slot in its own colour. On open, the viewer
  asks for the calibration of the channels on screen (`/display_contrast`)
  and applies it to channels nobody set (`calibration.view_spec`): a marker
  `VIEW_GAIN` brighter than the evidence draws it, and the nuclear stain on
  its own nuclei (`cell_window.measure_nuclear`, `calibration.nuclear_view`:
  black in the gaps, a typical nucleus and the bright ones at
  `NUCLEAR_LEVELS`, dimmer than a marker) so the markers read over it -- the
  evidence keeps its own windows, the nuclei in muted blue; a
  saved channel row records who set its window (`source`: user, auto,
  gating, default; a row saved before that is the user's). A window the user
  set is never replaced: on a channel a check settled (`display_contrast`
  `checked`) the Auto wand applies it (`viewerSidebar.checkedEntry`), and
  fits the mixture otherwise. Log brightness is the viewer's default
  (`DEFAULT_CURVE_MODE`; rows saved with it off keep it off), and that
  default asks no HD of a streamed image -- only the user's tick does.
- **A gate is never accepted against the direction on record.**
  `unit["direction"]` holds the way the last look's rows pointed until a
  candidate, `keep` or an about-right look replaces it; a unit closed meanwhile
  (refinement not allowed) is `insufficient_information`, written at its last
  gate for review.
- **Every gated marker ends with a value.** A unit closed for review
  (`schemas.REVIEW_WRITE_STATES`: `manual_review_recommended`, `not_binary`,
  `insufficient_information`) is written at the best gate it reached -- its
  proposal, else its candidate, the scored start or the Auto gate
  (`Engine._review_gate`) -- with method `needs_review` and confidence
  `manual_review`. Only a gate the user changed in the viewer mid-session is
  not overwritten. Failed and all-negative markers keep their empty gate.
- **Structural channels are never gated.** A nuclear counterstain or an
  autofluorescence / blank channel resolves to the vocabulary's `DNA` /
  `Autofluorescence` entry by name pattern (`vocabulary.structural`: `Nucleus2`,
  `DNA_1`, `c2_DAPI`, `AF3`, `Blank_Cy5`), role `context`, and is dropped from a
  session even when named in `markers` (`capabilities_session._structural`).
- **Continuous markers are gated, not skipped.** `binary: false` means "no
  valley": the unit is `continuous`, starts at the noise ceiling, and its looks
  are judged row by row like any other's.
- **The start is scored before the first look; the algorithms know no
  marker.** `Engine.propose` runs `autogate/scoring.py` against every gated
  partner the panel context relates to the marker (both directions,
  `scoring.evidence_partners`). Biology lives in the vocabulary
  (`markers.yaml`) and the panel context; scoring, lattice and transitions are
  generic over it -- no marker name, no per-marker threshold in code.
- **A marker is never accepted because it ran out.** Reaching an allowance
  (looks, or rounds of candidates) while the evidence says to go on goes to
  the session's limit policy (`Engine.limit_reached`, `schemas.LIMIT_POLICIES`):
  `ask` holds that marker while the user answers a card in the agent panel
  (the calling agent sees `waiting_for_user` and may relay the answer through
  `gating_session_status(limits=...)`), `extend` grants another allowance,
  `stop` flags it. Past `max_extensions`, or on a no, the marker is
  `manual_review_recommended` with the best gate written and tagged
  `needs_review` (`Engine.close_at_limit`). The same holds for a look that could not settle
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
- **An overview window is capped from the cells** (a marker without a cell
  table or enough cells: `calibration.CELL_CAP_FACTOR` times the table's
  `CELL_CAP_PERCENTILE`, in image units), so noisy negatives cannot wash out
  every cell; a window still wider than `THRESHOLDS["wide_ratio"]` is
  flagged. A whole-tissue panel draws coarse pixels and takes the window for
  its scale (`calibration.display_spec`, the cofactor scaled with it). The
  record carries `calibration.VERSION`, the table and the QC fingerprints; an
  older or stale one is recomputed (`load_current`).
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
  texts and schemas back in every packet. A look carries only what its one
  question needs: the profile's class, the hard flags, the compartment, and
  one line per partner (`packets._partner_line`: relation, grade, orphan or
  double-positive share); scored candidates, the hierarchy and the biology
  stay with code and the report, the full profile is `profile_marker`.
- **Each brief travels once per reader** (`SessionOptions.evidence="delta"`,
  the default). The stored packet stays self-contained (memo keys, replays,
  the report); `packets.as_sent` is the view one conversation is sent: an
  evidence value -- each key, and each key of a dict-valued one -- at least
  `AS_IN_MIN_CHARS` long that this reader was already sent goes as
  `{as_in: packet_id}`; regression `checks` lose their passing rows; `budget`
  and `narration` stay with status and the viewer; `gating_answer.progress`
  is three fields. A reader is a conversation: `gating_session_status`
  without the current `known_guide` (what a fresh worker calls first) or any
  rerender starts a new one (`packets.new_reader`), so a pointer only names a
  packet the reader holds. `evidence="full"` sends the stored packet.
- **A sheet row is drawn once per reader and inputs** (`sheets="trim"`).
  `_sheet` fingerprints the fields row (gate, chain, references, seed, field
  size) and the slide row (gate, partner); a row this reader was shown with
  the same fingerprint is left out (`sheet.rows`, `{as_in}` in its evidence),
  and with neither left no sheet is sent. `regression_confirm` draws the slide
  row only. Rows register as seen when sent, not when built, so a replayed
  packet registers nothing; a trimmed packet served to a later reader is
  redrawn whole. Measured on the synthetic bench: same gates, 23% fewer
  characters, 26% fewer pixels (five packets; a real panel repeats more).
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

## 5a. Scored candidates and the starting gate

A look judges the cells it is shown, and they are drawn around the starting
gate. A mixture that split noise, or one lineage's spill, from the dim tail
(`CD11c`, `CD68`, a continuous `PD-L1`) starts every collage in background, and
no direction recovers from that. So `Engine.propose` (just before a unit's
first T2, when its partners are gated; at the bulk pass when no look will
follow) scores candidates and starts from the best supported one
(`unit["start"]`, `source: "scoring"`). The Auto gate stays `unit["gmm"]`.

| candidate | what it is |
|---|---|
| `gmm` | the Auto gate |
| `onset` | first intensity above the background mode where the local fdr against a Gaussian fitted to the peak's core (narrower half-width at half-max) drops below `onset_lfdr` |
| `ceiling` | mode + `ceiling_sd` core sds; the background is the tallest peak unless a lower one stands `background_separation_sd` of its own sds below it (majority-positive markers) |
| `bio:<P>` | x0 + `bio_k`·s of a logistic fit to P(partner+ \| marker) for a subset / co-expressed partner; `robust` when its elasticity (movement per unit of partner-gate movement, gate nudged ±`robust_shift_sd` partner sds) is ≤ `robust_max_elasticity`, it calls ≥ `bio_min_fraction`, and (co-expressed) it is within `coexpressed_past_ceiling_sd` of the ceiling |
| `anti:<Q>` | where an exclusive partner's share finished falling (evidence only) |
| `sens:<P>` | the marker's `sens_quantile` among a reverse-subset partner's positives (evidence only) |
| `score` | the proposal |

Proposal: the strictest robust `bio:` leads (weighted median against the Auto
gate and the ceiling, nudged a third toward the Auto gate within
`nudge_within_sd`); otherwise a weighted mean of the Auto gate (weight by
separation, ~0 for a continuous marker or one calling > `degenerate_fraction`)
and the ceiling (the onset when no ceiling fits). Each candidate carries
`components` (distribution, coexpression, anti, retained, region over a
`tiles`² grid, morphology from the area ratio either side) and a `score` that
ranks it for the report; the proposal does not use it. The record also keeps
`control`: the negative control of the most specific reliably gated subset
partner (`scoring._control`) -- recorded, never applied, because it is only
as good as that partner gate's sensitivity (lsp11385_2: it overshot the
human gate on CD68, CD4 and CD163).

The scored candidates join the lattice (`lattice.SCORED`, kept outside the
mixture's guard band, which is widened to hold them and the steps around a
scored start; steps then use the background's core sd). `bio:` anchors a T4
chain like `ctrl:` and `within:`. A scored start skips the mixture-band
contradiction check (`transitions._contradicts`); an empty direction ends in
review at T4. Regression distances and T4 confidence are measured from the
start (`engine.start_low`).

Calibration (one hand-gated melanoma CyCIF image, 17 reference gates): the
proposal's mean absolute error against the hand gates was 0.13 log1p units,
against 0.26 for the previous Auto + vision run on the 13 markers it gated --
before any look. One image is not a validation set; re-check `PARAMS` on
others before trusting the constants. `score_gate_candidates` shows the same
scoring outside a session.

## 5a′. The evidence graph and the sample's biology

Gates are not independent: a marker scored against a partner inherits that
partner's errors. So the run keeps an evidence graph
(`autogate/hierarchy.py`) and a prior from the sample's biology
(`autogate/biology.py`, knowledge in `plexora/ai/knowledge/biology.yaml`).

- **The tree** comes from the panel's own relations, never a list of names:
  parents are `subset` partners; a marker is placed under the deepest of
  those or of a `coexpressed` partner from an earlier role bucket. Depth
  gives the stage -- broad, lineage, subtype -- and the state/signalling
  roles are `state`; a marker with no role and no relation is `unplaced`.
  `context.order` sorts by stage first, then the old rules (role bucket,
  partners before dependents, unlocks, T1 score). It guides, never blocks.
- **Implied relations**: a subset of a subset is a subset; a subset inherits
  its parent's exclusions; a subset of a child is a child. Derived relations
  are weighted `DERIVED` and never repeat a stated one.
- **Grades**: `hierarchy.grade` -- accepted gates at their confidence, the
  user's kept gates high, review writes / failed QC / no positives `failed`,
  undecided `pending`. Outside a session, `grade_from_provenance`.
- **Selection**: relevance (relation × stated confidence × derived ×
  context) × reliability. Failed and pending are never used and are listed
  as avoided with why; low only when nothing moderate+ of the same kind
  (positive, negative, child) exists, down-weighted. Looks' references
  (`context.references_for`) take moderate+ only and exclude state readouts;
  the scoring's partners (`scoring.evidence_partners`) take all usable ones,
  and a low-reliability partner leads only when no reliable one can, at
  `low_lead_weight`.
- **Upward evidence**: reliable children's `sens:` values that agree
  (`children_min`, within `children_agree_sd`) give the `children`
  candidate, which leads only when the parent's own evidence is weak (no
  robust partner, d below `weak_d`, a degenerate or continuous mixture). A
  weak parent with open children waits for them once
  (`Engine.defer_for_children`, option `defer_parents`) and is re-scored.
- **Failure propagation**: a unit closing `failed` re-scores every unit of
  its image that leaned on it and has not been looked at; one already looked
  at records `stood_on_failed` (packets and report show it).
- **Biology**: `SessionOptions.biology` (the user's words) or, failing that,
  project metadata, or inference from panel indicators (`source` says which;
  inferred is reported as such). `biology.overlay` lays the contexts'
  relations over a *copy* of the panel for the session (never over a
  user/metadata/agent-stated entry), tagged `context` and weighted
  `CONTEXT`. It orders markers and chooses references; it is not sent to a
  look (fewer inputs, fewer ways for two runs to differ). A look's `pattern`
  outside its compartment's (`COMPARTMENT_POLICY[c]["patterns"]`) caps
  confidence at moderate; inside it, with a start led by a reliable partner,
  a fairly sure look may reach high (`engine.biology_agrees`). Never moves a
  gate.
- **Viewer during setup**: an `expression_setup` packet is mirrored as
  `open_tool {ask: true}` -- the tab shows its own requirements prompt (the
  quiet panel open cannot succeed while `features` is unconfirmed); a quiet
  open that fails names the missing keys, not the form's fields.
- **Set-up is the user's** (`SessionOptions.setup`, 2026-10-15). A run went
  on while the viewer's set-up form was still open: the harness's model
  answered `expression_setup` ("X, log1p off"), then the user's answer
  switched the matrix under the running session. Now:
  - `ask` (an MCP agent, the default): a required column not found
    (`Requires.missing_from`) refuses the start with `detail.setup`; the
    matrix is still an `expression_setup` packet the agent puts to the user.
  - `wait`: anything the form would ask -- missing or never-confirmed
    columns, an unsettled matrix -- holds the session in `needs_setup` with
    `setup_wait`, no packet, nothing planned or quoted; `_settle_setup`
    plans it from the tables as they are once the form is finished.
  - `fail`: a missing column or an unsettled matrix refuses the start,
    saying how to supply it (`--matrix`, `--log1p`).
  - The harness collects the set-up before any model call
    (`DecisionRun._collect_setup`, through `capabilities_session.prepare`,
    the start's own function): no session, quote or note interpretation
    until it is done. From the viewer it waits for the form, which the
    launching tab opens (`ai_setup`, by run id; closing it unfinished stops
    the run, `ai.run_control` cancelling the job); on a command line it
    fails saying what. The session-level `wait` is the backstop for a
    set-up that changes between the two.
  - Matrix: an unconfirmed `X` in the log range is settled by its shape
    (`agent/expression.shape`, scale-free skew); only an unclear shape, when
    it is all that is left, goes to a model (`ai/harness/expression.py`),
    whose reading `prepare` checks again and the session records as
    `inferred`. A node-held table is sampled through its loaded rows.
  - A matrix or log1p changed while a session runs stops it
    (`_matrix_moved`, `matrix_changed`), naming the gates written on the old
    values.

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
  moves the gate past each row that says so (going up, cells not positive
  themselves; going down, cells that are). A first row saying the opposite
  turns the look back once (`t4_reversed`). When every row says move
  and the chain ended at an anchor or at `MAX_CHAIN` with points beyond, the
  next round starts from there (`unit["t4_continued"]`); a round ends only at a
  row that says stop, the lattice's end, or the round limit. Each candidate's
  tissue field is a different field (`gate_sampling` `avoid`).
- **Words, not floats**: confidence is `sure` / `fairly_sure` / `unsure`
  (`schemas.AI_CONFIDENCE`), each a fixed number between the rule's cut-points.
- **Side channels do not route**: artifact flags cap confidence only; a request
  opens a reference look only when the answer was undecided.
- **The memo** (`memo.py`): each answer is kept under a hash of its packet
  (JSON less ids and charges, image bytes, and the profile, lattice, scoring,
  calibration and renderer versions), per project and per
  `SessionOptions.agent`; a later packet with the same hash is answered from it
  (`reuse_answers`), so a rerun reaches the same gates without asking. Packet
  stability is therefore the lever: a look carries the fewest evidence keys
  and guide entries that answer its one question.
- **Replay** (`bench.replay_session`, `plexora ai bench gating
  --replay-session ID --rerender`): every packet a stored session issued,
  asked again, each collage drawn again from its manifest; the bytes must
  match (`collage.rerender`), the guard for any renderer or calibration
  change.
- **Purity in code** (`bivariate.purity_excess`): a gate's positives may be
  no less pure against a partner than the marker's brightest tenth are;
  `regression`'s `partners` check, and a failure pushes the gate up the
  lattice once (`Engine.purity_push`) before any whole-image look. A spatial
  check alone failing may hold or reject a gate, never move it.
- **Dependencies**: each gate's provenance records the partner gates it stood on
  (`detail.depends_on`); `gating_qc` lists `stale_dependencies` when one changed.
- **Measured**: `plexora ai bench gating --synthetic all --stability N` gates each
  scenario N times with differently seeded agents and replays the first run;
  with a noisy agent (a fifth of its verdicts wrong) 31 of 35 markers reached
  one gate in every run and every replay was identical (2026-09-27).

## 5c. Who runs what (delegation and model tiers)

A conversation re-reads everything before each call, so one that answers n
packets pays ~n² in context (the lsp11385 run: 91 packets, 29.6M cache-read
tokens). The package, not a client, decides how the work is split
(`plexora/ai/delegation.py`):

- **Tiers name the work, never a model.** `routine` (tool relay, listing,
  bookkeeping, write-ups) and `judgement` (reading pictures, placing a gate,
  judging an artifact). The user maps each to a model of their client's
  (`plexora ai tiers routine=<model>`, the settings file, or
  `PLEXORA_MODEL_<TIER>`); unset, the tier's words say which to pick. No
  vendor or model name ships in the package (a test pins it).
- **Roles** (`ROLES`) are the delegated work: a tier, a worker skill, the
  tools, and the units per worker (`gating_worker`: judgement, gate-packets,
  four tools, `ENGINE.markers_per_worker`).
- **The server says how to hand it out**: `gating_session_start` and
  `gating_session_status` carry `delegate` (`delegation.block`): tier, model
  or `pick`, tools, units per worker and a short `brief` the coordinator
  passes as the worker's whole prompt. The worker reads its skill itself, so
  the coordinator never copies skill or guide text. A delegating start does
  not send the reading guide either (it holds no packet the coordinator must
  answer; each worker's first status call fetches it). `delegate: false`
  leaves the block out and sends the guide.
- **Skills name their tier** (`tier:` in `skill_manifest.yaml`, shown by
  `list_skills` and `server_info.model_tiers`). The coordinator skill
  (gate-image, ~10k characters) plans, starts, hands out and finishes; the
  worker skill (gate-packets, ~5k) judges packets and returns one line per
  marker.
- **Client adapters are generated from the role**: `plexora ai setup claude
  --install-skills` writes `.claude/agents/plexora-gating-worker.md`
  (`delegation.agent_file`: the tools, no model -- the coordinator passes it
  per launch). Other clients use the `brief` as it is.

New AI features declare a role here and a tier in the manifest. The QC loop
is the next candidate (a `qc_worker` with a qc-packets skill).

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
- Batching `transfer_check` across images of one marker.
- A node-side table operation for the contrast check's cell picks (a table on
  a data node reads its marker columns through `columns()` today), and a
  table-to-pixel map so a partner's gate draws on a scaled (z-scored) table.
- Modality plug-ins beyond intensity markers (transcript counts, morphology);
  the seams are the table operations, the distribution class rules and the
  renderer.
