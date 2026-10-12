# Gating: re-planning on failure, and four generic guards

Branch `feature/gating-replan`. Written 2026-10-11 from the PCA benchmark (old vs
hybrid gating, LSP11321 / LSP11761 / LSP11361 x 17 markers against the user's
gates; report on O2 `~/plexora-gating-bench-20261011/final`).

## Why

Of the 16 runs that finished 0.4 or more off the user's gates, 12 came from one
engine rule (a "mixed" look settles the gate). Most of the remaining large
misses came from three gaps:

- **Failures don't change the plan.** Planning runs once at session start, and
  nothing re-plans. A failed gate is dropped from evidence, but a dependent
  that was already looked at only gets a `stood_on_failed` note, and an
  accepted one keeps its gate. A partner gated later never rescores anyone.
- **Absent markers have no route to the outcome that exists.**
  `no_positive_population` is reached only from a first look with
  `pattern: none`. LSP11761 PD1 got an `artifact` verdict in both runs, and no
  look ever confidently saw own stain, yet it ended in manual review at an
  artifact gate. The tree already derives "PD1 excludes SOX10" (PD1 is a
  subset of CD45, and CD45 excludes SOX10), but that relation never reached the
  purity check. 54% of PD1 positives were SOX10+.
- **Automatic moves ignore what a look just saw.** The CD163 refit within CD45
  moved the gate from 7.86 to 8.36, although the reader had called the cells
  above 7.86 own stain, and 65% of CD163 positives were CD45-negative. Only the
  hybrid correction respects judged rows (`hybrid.judged_limits`).
- **A zero-heavy channel breaks the scored start.** LSP11361 SOX10 is 50%
  exact zeros (over `FLOOR_MAX_FRACTION`), so the zeros became "the
  background" and the ceiling landed at 0.40. The density valley is 4.0, which
  is the user's gate.
- **Strict QC with no QC result is silent.** In LSP11761 the edge band drove
  six markers, and `qc_exclusion` said `applied: false` with nobody told.

## Rules every change keeps

- Generic: no marker names or per-marker numbers in code. Biology lives in
  `markers.yaml` / `biology.yaml` and the panel context. Every number is a
  named `[cal]` parameter.
- Context never moves a gate. Re-planning routes, orders, rescores and asks;
  thresholds still come from data and looks.
- Bounded: every loop below terminates (each round removes a marker or
  closes a question; a unit enters each step at most once).
- Node interface unchanged: the data node may run an older Plexora, so every
  new computation runs app-side (`scoring` and `purity_excess` already read
  columns through the table handle).

## Phase 1: built and tested on one image before anything larger

### 1. Re-planning loop (`autogate/replan.py`)

Triggered from `Engine._close_tail` whenever a unit closes in a failed state
(`hierarchy.FAILED_STATES`) or with `needs_review`.

Deterministic part, run by code every time:

1. **Order.** Recompute the order of markers not yet looked at from the panel
   with the failed markers removed. Started and closed units keep their place.
2. **Dependents.** Units not looked at yet are rescored without the failed
   marker; this already happens. Open units past a look get their references
   refreshed, and the existing ledger fingerprint makes their outstanding
   packets stale. Accepted units that leaned on the failed marker get the
   numeric whole-image checks again with the surviving references. If those
   fail now, the unit reopens to one whole-image look, once per unit.
3. **Patterns.** Failures are grouped by scope:
   - marker: one marker in one image;
   - image: at least `image_flag_min` markers of one image carry the same
     artifact flag (edge, autofluorescence, ...);
   - panel: in a multi-image session, the same marker fails the same way in
     at least two images.

Agent part: a session-level `replan` packet, answered by the planning task
the same way `panel_context` is. It is issued only when a failure needs a
biological call that code cannot make:

- suspected absence: an `artifact` verdict, or lineage contamination (see
  2), with no look ever confidently seeing own stain;
- an image-level flag cluster;
- a panel-level repeat.

Plain "the look could not settle it" failures don't ask the agent; the
deterministic part handles them.

The agent answers per marker one of `absent`, `bleed_through`,
`technical_failure` or `keep_for_review`, and per flagged image one of
`image_artifact` or `not_image_level`. Code then applies:

- `absent` closes the marker as `no_positive_population` (the existing empty
  gate);
- `bleed_through` reopens it once for candidates going up, with the
  contaminating partner as a purity reference;
- `technical_failure` closes it as `technically_failed`;
- `image_artifact` records an image finding, notes it on that image's
  flagged gates, and recommends QC. An accepted gate stays accepted: its
  looks judged its cells. Live run 3 closed SOX10 and Ki67 for review this
  way, although they sat 0.15 and 0.54 from the user's gates, so the
  finding became a note.

Then the deterministic part runs again, which is the loop. Each marker enters
the packet at most once, so the rounds are bounded by the panel size.

### 2. Lineage exclusions reach the purity check

- `Engine.run_regression` also checks every exclusive relation the tree gives
  the marker, direct or derived (`hierarchy.relations`), whose partner is
  gated at moderate or better. Today it checks only the two reference
  markers. The computation runs app-side (`bivariate.purity_excess`), and an
  excess over tolerance fails `partners` as today, so the existing purity push
  applies.
- New absolute check: if even the marker's brightest positives sit on an
  exclusive lineage (`at_top >= contaminated_top`, and at least
  `contaminated_vs_prevalence` times that partner's share of all cells), the
  whole population is suspect. The unit closes for review with that reason,
  and goes to the `replan` packet as suspected absence or bleed-through. The
  self-calibrated excess cannot see this case, because the top is as
  contaminated as the gate.
- **Across lineages only.** The two markers must share no parent in the tree
  (`cross_lineage`).
  - Learned live: CD8a in LSP11761 was flagged against CD4 (71% of its
    brightest cells read CD4+). The planning worker then called it absent,
    and a real population got an empty gate.
  - Siblings of one lineage light each other in dense aggregates, so their
    overlap is spill.
  - Offline on all 51 benchmark pairs, the corrected rule flags exactly the
    two markers the user calls absent (LSP11761 PD1 and CD20L) and nothing
    else.
- **Low derived exclusions count for the question, never for the push.**
  PD1, a moderate subset of CD45, inherits CD45's SOX10 exclusion at `low`,
  and skipping low exclusions is why live run 1 missed PD1. The
  contamination check only raises a question; the automatic purity push
  still takes moderate or better.
- **A smaller brightest tenth is enough for a share** (`min_top` 30, against
  the purity push's 50). This is a margin for absent markers with very few
  positives. PD1 itself had 60, so it did not need it.
- **Checked again at the close.** A look can go out before the lineage
  partner is gated (live run 1: CD20L before SOX10). The check therefore runs
  again when a marker closes, and a derived exclusion now makes a look wait
  for that partner the way a stated one does.
- **A marker queued for the `replan` packet is held** (`awaiting_replan`, not
  terminal) until the answer comes. Live run 1 reported the session decided
  with PD1's question still queued.

### 3. No automatic move crosses a judged row

- First looks and looks beside a reference record what their rows judged, by
  value (the sampled cells' range per row), in `unit["judged_rows"]`, beside
  the T4 intervals' `judged`. This is recorded only for looks at
  `fairly_sure` or better.
- `Engine.clamp_to_judged` (using `hybrid.judged_limits`) clips every refit
  code makes after a look to both records: the `within` refit, and a rescore
  after a look. A move past a row the reader called `own_stain` (going up) or
  `unstained` / `neighbour_only` (going down) is clipped to that row, and the
  clip is logged.
- The hybrid cell-model correction keeps its existing clip (T4 intervals
  only). Its per-cell calls are evidence in their own right, and on the
  benchmark it moved LSP11321 FOXP3 toward the user's gate across a
  first-look row. Clipping it there too failed the hybrid's own tests and
  would have undone that move.
- `within: P` is offered only when the data agree that the marker sits inside
  P: the share of the marker's positives that are P-negative must be at most
  `within_max_orphan`, and P must be accepted cleanly (no `needs_review`).

### 4. Strict QC without a QC result is never silent

- `gating_session_start` returns a `qc_warning` naming the images that have no
  QC result, and the record keeps `qc_missing`. The status and the report
  show it.
- The image-level pattern in 1 names it again when looks keep flagging the
  same artifact across markers.
- Later (phase 2): `SessionOptions.qc_missing` = `warn` (default), `ask`, or
  `run` (runs the deterministic checks before gating).

### 5. Floor-shaped backgrounds (`scoring.background`)

- If the background peak sits on the column's floor (the smallest values,
  with density falling away from it, rather than an interior peak), the
  Gaussian ceiling and onset don't apply.
- Use the smoothed density's valley between the floor and the main mode above
  it as the bound.
- A GMM gate inside the floor's ramp (below that valley) splits the floor, not
  background from positives, so it barely counts (the same weight as
  `degenerate`).
- Recorded as `background.basis = "floor_valley"`. Columns with an ordinary
  background peak are unchanged; checked offline on all 51 benchmark columns.

## Phase 2: only after the small test, and with the user's go-ahead

- **"Mixed" settles nothing.** A first look of `near: mixed` brackets the gate
  (one interval each side) instead of accepting it. The alternative is
  splitting `mixed` into "mostly own" and "mostly not". Ship behind a session
  option, compared on the benchmark with replicates.
- **Panel-level rules across images.** A repeat failure, such as CD20L's SOX10
  bleed-through in all three images, becomes a dataset rule applied to every
  image's plan.
- `qc_missing = run`.
- **Benchmark:** 3 images x 17 markers, two replicates per arm (looks shared
  across arms where possible via `reuse_answers`), scored against a band
  around the user's gate rather than the point. The user's gate is a
  reference, not gold: CD163 in LSP11761 can sit above 7.2, but 8.36 is
  wrong.

## Small test (phase 1)

- **Unit tests:** each piece above, in the gating test files.
- **Offline, deterministic, on the real columns:**
  - the floor rule fires only where it should (all 51 columns);
  - lineage contamination numbers for every immune marker in all three
    images: which markers would be flagged;
  - the `within` guard on CD163 in LSP11761.
- **Live:** LSP11761, markers SOX10, CD45, CD3e, CD163, PD1, CD20L, CD11c,
  PDL1.
  - Setup: hybrid off, propose mode, the benchmark's settings.
  - Driver: a headless Claude Code session whose Plexora MCP server runs this
    branch (the `tools/bench_harness_vs_cc.py` arrangement).
  - Compared with the benchmark's two arms on the same markers, and against
    the user's gates.
  - One run cannot separate the changes from reader variance. The check is
    that each mechanism fired as designed (from the decision log), plus the
    gate deltas.

### Small-test result (live run 3, LSP11761, 16 markers)

CD163 was left out: the user's manual gate (7.73) makes the session skip it.

- **Absent markers.** PD1 and PDL1 were called `no_positive_population`
  in every run on the final code; the old arm wrote gates of 7.69 and 6.98
  for review. CD20L was caught before any look in every run. The planning
  call on it split: `absent` in two runs, `keep_for_review` in one ("B cells
  are plausible in melanoma").
- **Present markers.** 11 of 13 gates were identical to the old arm. CD4
  went 7.32 → 7.20 and FOXP3 6.39 → 6.45. Both moves are replayed reader
  answers that differ from the old arm's: the memo of agent `bench-old`
  also holds the fresh answers from live runs 1–2. So they are reader
  variance, not these changes. Paired MAE 0.302 → 0.295.
- **CD8a** stayed on the old path; run 2's wrong `absent` is gone.
- **No clamp fired.** The `within` guard and the judged-row clamp were not
  exercised live (CD163 left out); they are checked offline and in unit
  tests.
- **The image finding closed SOX10 and Ki67 for review** (both near the
  user's gates). It is now a note (see 1); the rerun kept both accepted.
- **Cost.** `replan` packets are text only, 2–3k characters each, two or
  three rounds per session.

## Phase-2 benchmark (2026-10-11): 3 images x 17 markers, four arms

Scored against the user's gates as points. LSP11761 CD163 was included with
`overwrite_manual` in propose mode. The user's gate (7.73) was never
written, and its provenance label was restored afterwards.

| Arm | Looks | Bracket | MAE | <= 0.2 | >= 0.4 | Accepted | Review | Absent called |
|---|---|---|---|---|---|---|---|---|
| old (benchmark) | fresh | - | 0.364 | 18 | 15 | 32 | 19 | 0/3 |
| R0 | old arm's, replayed | off | 0.367 | 20 | 15 | 32 | 19 | 1/3 |
| R1 | old arm's, replayed | on | 0.345 | 21 | 15 | 39 | 12 | 1/3 |
| F1 | fresh | on | 0.359 | 19 | 15 | 42 | 9 | 2/3 |
| F2 | fresh | on | 0.356 | 19 | 14 | 40 | 11 | 1/3 |

R0 and R1 replay only the old arm's answers: agent `bench-old-clean`, a
copy of the old arm's memo entries. A changed packet is asked afresh, so
reader variance enters wherever the evidence moved.

- **Deterministic gains, in every arm:**
  - LSP11361 SOX10: 3.03 → 3.87 (user 4.0), from the floor valley;
  - LSP11761 PD1: called absent;
  - LSP11761 CD163: 8.36 → 7.85–8.00 (user 7.2; the user says above 7.2 is fine).
- **Absent markers still hang on reader calls.**
  - CD20L is flagged every time, but the planning agent keeps it for review in most runs.
  - PDL1 is called absent only when the whole-image check says `artifact`.
- **The bracket fired 87 times in the three bracket arms and stayed on `mixed` 63
  times.** The rows next to the gate read mixed on both sides too, so it
  moves little.
  - Its effect at fixed answers (R1 vs R0) is MAE −0.02.
  - Its review savings came partly from accepting gates 0.5–1.0 off (CD68 in
    two images, LSP11321 CD31). Those were unsure looks the bracket added
    nothing to. Now such a look goes on as before: a reference look, else review.
- **Rejected: the up side passing `mixed` rows (rule B).**
  - Offline it gained only in LSP11321 (MAE 0.487 → 0.402, 33 brackets).
    Without LSP11321 it lost (0.297 → 0.304).
  - LSP11321's gates sit about 0.4 below the user's in every arm, old included,
    so rule B fits that image's convention, not a generic rule.
- **The `mixed` question stays open.** The bracket was removed (see the final
  configuration below); its patch is archived.
- Scripts: scratchpad `bench/phase2/` (`compare.py`, `brackets.py`,
  `simulate.py`, `evidence.py`).

### The user's rechecks of the five evidence-backed disagreements

| Marker | Recheck | System | Old reference |
|---|---|---|---|
| LSP11761 Ki67 | 7.4 | 7.46 | 8.0 |
| LSP11761 CD45 | ~7.8 (lots of unspecific staining) | 7.74–8.14 | 7.3 |
| LSP11761 CD163 | ~7.8 | 7.85–8.00 (old arm 8.36) | 7.2 |
| LSP11321 CD3e | ~7.0 | 6.92 | 7.3 |
| LSP11321 MART1 | 6.5 | ~5.96 | 6.5 |

The system was right on four. On MART1 the looks called the cells between
the two gates own stain, and the user's gate stands. Re-scored with these
values, MAE is 0.327 for the old arm and 0.308–0.328 for the new arms. The
row-judgment filter that picked these five is a useful way to choose
rechecks, but it is not proof.

## Final configuration (2026-10-11, after the re-check)

Re-checked against every test since 2026-10-08 (the step ledger's E1-E13).

**Kept:**
- re-planning with the lineage screen;
- the judged-row clamp and the `within` guard;
- the floor-valley scored start;
- the QC-missing warning;
- image findings as notes.

**Removed:** the bracket (`mixed_first_look`). The patch is in
`docs/internal/bench/runs/2026-10-11-replan/mixed_bracket.patch`
(gitignored) and still applies.

**Added:**
- **A confidence cap instead of a repeat purity look.** Once the purity push
  has run, purity failing again with nothing else failing caps the gate's
  confidence at low (`Engine.purity_capped`). A spatial check failing too
  still asks.
- **The cell model needs a QC result.** It is withheld from an image strict
  QC cannot act on (`record["model_withheld"]`, said in `qc_warning`).
  Synthetic bench scenes run with `qc: off`, since they have no QC.
- **A re-screen when a partner lands** (`replan.rescreen`). A marker whose
  look settled nothing can close for review before its excluding partner is
  gated. When that partner is accepted, the lineage check runs again and the
  marker is queued as at a close. Live: LSP11761 PD1 closed before SOX10 and
  was never asked.

**Tried and removed the same day: a picture for each suspect marker in the
`replan` packet.** It showed the positives just above the gate and the
brightest tenth, each beside the excluding partner, or the whole image at
the gate. In two fresh runs (K1, K2) on LSP11761 the planner called the
tiles "diffusely saturated":
- it kept PD1 for review, which it had called absent from numbers six
  times out of six;
- CD20L was called absent 1 of 2 times (3 of 7 without the picture);
- PDL1 was called absent 1 of 1 time from its sheet.

The patch is in `docs/internal/bench/runs/2026-10-11-replan/replan_pictures.patch`.

**Left alone:**
- T3 (17 looks over two datasets, 1 move);
- LSP11321's offset (an image convention).


### The final code on LSP11761 (K3, K4: fresh looks; Ki67, CD45 and CD163 are now the user's manual gates and are skipped)

- **Present markers** match the old arm to reader variance. The purity cap
  replaced two whole-image looks in session B.
- **PD1 was flagged in both runs; the re-screen works.** The planner kept it
  for review both times ("T cells are plausible in melanoma").
- **CD20L was called absent in 1 of 2 runs.**
- **What decides an absent call.** Over every run, PD1 was called absent 6
  of 6 times when a whole-image look had also called its positives artifact,
  and 0 of 3 times on the lineage numbers alone.
- **Next step, proposed, not built:** a lineage suspect gets one whole-image
  check (`qc_confirm`, which already offers `no_positive_population`) before
  the planner is asked.
