# Gating engine v2: how it works, how it was measured, where it fails

Handoff, 2026-10-22. For engineers who will work on automatic gating next.

## Status

- **Branch:** `feature/gating-candidates`. Not merged.
- **Names:** v2 was first called the "candidates" engine. v1 is the rows
  engine, first called "classic".
- **Default:** v2 is the default engine, `schemas.DEFAULT_ENGINE`. v1 stays as
  the alternative.
- **The first names still work:** `classic` and `candidates` are accepted
  everywhere an engine is named (`schemas.ENGINE_ALIASES`).
- **Old sessions:** a session stored before the engine option existed
  resumes as v1.
- **Rule revision:** v2's decision rules carry their own revision,
  `hypotheses.VERSION` (6 at the time of writing). The engine name and the
  rule revision are separate.

**Where the detail lives:**

| what | where |
|---|---|
| every measurement and design decision, in order | `docs/internal/bench/CANDIDATES_PLAN.md` |
| the gating system as a whole | `docs/internal/AUTOMATIC_GATING.md`, §2f for v2 |
| this engine's code | `plexora/plugins/gating/server/autogate/hypotheses/` |
| the earlier review handoff (state before VERSION 6) | `docs/internal/bench/HANDOFF.md` |

## 1. What v2 is, and why it exists

A marker that needs a look is decided by choosing among candidate gates, not
by moving a gate.

- **The candidates:** the marker's lattice of candidate thresholds
  (`autogate/lattice.py`) is fixed before any look. The candidates compete as
  hypotheses.
- **The reader's job:** it is shown *blind* panels of single cells: numbered,
  shuffled, with no value, call or gate anywhere. It labels each cell
  `own_stain`, `not_own_stain`, `uncertain` or `artifact`.
- **Code's job:** it turns the labels into a probability for each candidate.
  It picks the gate, decides whether to look again, and closes the marker,
  either accepted or sent to review with a stated reason.
- **The reader never picks a gate or a direction.**

**Why.** The motivating measurement was on 12 held-out PCA images:
- **The lattice was already good enough.** It held a point within 0.2 of the
  person's gate for 93 % of markers. Choosing the best point every time (the
  *oracle*) gives MAE 0.08.
- **v1 ended far from it.** v1's looks ended at MAE 0.368.
- **So the error is in choosing, not in generating candidates.** v1 asks the
  hardest question for the job: which way should a moving gate go? It also
  shows each cell's value and current call in the caption, so the reader is
  not blind.

**The person's design rules.** These bind every change.

- Candidate gates are competing hypotheses. They are never averaged, and the
  gate written is always one of them. Each marker in each image gets one
  scalar gate.
- The reader labels single cells blind. A cell is never shown twice for one
  marker.
- Code owns selection, stopping, confidence and writes.
- The marker's own stain is the criterion. Partner markers are context, never
  proof.
- The classifier is a discrepancy detector. It never moves a gate by default.
- Biology is a prior, never a rule.
- No marker-specific numbers in code. Every `[cal]` constant changes by hand,
  with a VERSION bump and a bench on frozen data.
- Never tune on the LSP images alone: their signal is better than most images'.
- A change that makes any marker more than 0.30 worse is rejected, even when
  the mean improves.
- A marker is never accepted because it ran out of looks. Every gated marker
  ends with a value.

## 2. Architecture

```
session start (shared with v1)
  profile every marker, fit, scored start (scoring.py), lattice (lattice.py)
  T1: clean splits settled on an audit sheet, no look needed
  display check, absence check, panel context, replan    (shared with v1)
        |
        v  a marker that needs a look: t2_confirm becomes cell_panel (engine._to_panel)
hypotheses/candidates.py   the candidates (lattice points, nearer than 0.1 merged),
                           the surveyed window, the prior = the scored start
hypotheses/panel.py        sample the cells: 20 from the intervals between
                           candidates + 4 clear negative sentinels, spatially
                           spread, shuffled, numbered; runs as the table op
                           gating.autogate.panel_cells, so it works on a data node
packets.py                 the cell_panel packet (collage: morphology / context /
                           expected partner / not-expected partner), no numbers
        |  reader labels each cell
        v
hypotheses/evidence.py     counts per interval and stratum (crowded / isolated);
                           Beta(own + 1/2, not + 1/2) per interval, 400 seeded draws,
                           isotonic (PAV) per draw, the candidate with the fewest
                           cells wrong per draw -> P(c* = j) for every candidate
hypotheses/controller.py   decide: plateau = {P >= 0.1} + argmax; leader = the
                           plateau member nearest the prior; resolved when
                           P(best gate within 0.2 of the leader) >= 0.8 and no
                           side is thin; else plan the next look by value of
                           information (voi_plan) or close with a reason
hypotheses/__init__.py     the unit state machine: looks, validity (sentinels),
                           growth (step past the lattice edge, split a wide
                           mixed interval), close, confidence
        |
        v
written (or proposed) gate, plus the review note (the plateau with each member's P)
whole-image check, classifier layer beside the gate     (shared with v1)
```

| module | lines | what it owns |
|---|---|---|
| `hypotheses/__init__.py` | 770 | `PARAMS`, `VERSION`, `V5_RULES`; the per-unit state machine (start, next look, answer, close); sentinel validity; growth; confidence |
| `hypotheses/controller.py` | 630 | `support` (the decision); `thin_sides`, `eligible`, `crowding`; `voi_plan` (where the next look's cells go); `REASONS` |
| `hypotheses/evidence.py` | 590 | counts and strata, `posterior` (Beta draws + PAV), `plateau_leader`, `equivalence` (candidates that differ by fewer than `material` cells), `look_gains` |
| `hypotheses/panel.py` | 150 | `panel_cells` (the table op), `crowded`, `arrange` |
| `hypotheses/candidates.py` | 110 | the candidate set and its window |
| `hypotheses/calibration.py` | 165 | confidence levels; a logistic table, not fitted yet |
| `hypotheses/classifier_check.py` | 200 | `classifier: check`: one look at cells the classifier disputes. Off by default; no trust table exists |

The packet loop is the same for both engines. `gating_next` and
`gating_answer` run it, and the `gate-packets` skill drives the workers.
`packets.ENGINE_ONLY` decides which guide texts and answer kinds a session is
sent:
- **v2's only kind** is `cell_panel`.
- **v1's kinds** are `t2_confirm`, `t3_biological` and `t4_candidates`.
- **Conditional gates and markers without candidates** fall back to v1's looks
  inside a v2 session (`hypotheses.active`).

### The decision, in more detail

- **Evidence.** For each interval between adjacent candidates, the share of
  `own_stain` among definite reads is drawn from Beta(own + 1/2, not + 1/2).
  - An interval never read stays at one half, with weight zero.
  - Each draw is made monotone with isotonic regression (PAV), weighted by the
    cells each interval holds.
  - The candidate that would call the fewest cells wrong wins the draw.
- **Candidate probabilities.** Over 400 draws this gives P(c* = j) for each
  candidate. The draws use common random numbers, seeded by the unit, so a
  replay is exact.
- **The leader.** The plateau is every candidate with P >= 0.1, plus the most
  probable one. The leader is the plateau member nearest the scored start.
  - This tie-break toward the prior keeps the spread down.
  - The alternative tried was a resolving member leading
    (`PARAMS["leader"] = "resolving"`). It was measured three times and was
    not better (section 5).
- **Resolved.** P(the best gate is within 0.2 of the leader, or changes fewer
  than `material` cells) >= 0.8, and no interval beside the leader has fewer
  than 5 definite reads while it still has unshown cells (`thin_sides`).
- **Sampling.** The first cells go to thin sides, and one cell to any interval
  never shown. The rest go by the expected rise in the best P(within) from 8
  more reads per interval, capped at 8 cells an interval.
  - **Early stop:** a whole look that could not raise P(within) by 0.02 closes
    the marker as `looks_uninformative`, never while a side is thin.
- **Strata.** A cell is *crowded* when the brightest of its six nearest
  neighbours is brighter by more than the tolerance. This uses table features
  only, no pixels.
  - With at least 5 definite reads of each kind, shares are weighted by the
    cells each kind holds.
  - A marker whose crowded cells hold most of the `uncertain` reads may
    resolve on its isolated cells. It is flagged `isolated_only`, and its
    confidence is capped.
- **Validity.** Four clear-negative sentinels go in every panel. A look where
  two of them read `own_stain` is invalid; two invalid looks close the marker
  as `readers_unreliable`. Sentinel miss rates are recorded as calibration
  features, never as evidence.
- **Growth.**
  - `step` adds a point past the lattice edge, but only once the leader beats
    its rivals.
  - `split` cuts a wide mixed interval whose reads are ordered by value
    (one-sided Mann-Whitney, p <= 0.05).
  - An added point may lead only after 5 reads on each side.
  - `split_at_close` is off.
- **Close reasons** (`controller.REASONS`):

  | reason | what it means |
  |---|---|
  | `budget_exhausted` | `panel_looks`, default 3, used up |
  | `looks_uninformative` | another look could not settle it |
  | `unreadable` | most reads `uncertain` after 2 valid looks |
  | `readers_unreliable` | the sentinels failed twice |
  | `no_identifiable_boundary` | `not_binary`: the stain rises with the value |
  | `positives_not_confirmed` | a rare tail not confirmed |
  | `unresolved_evidence` | the reads leave it undecided |

  A review closure writes the leader and records the plateau, so the note can
  say "7.31 or 7.69, reads split 0.55 / 0.35".
- **Confidence.** P(within 0.2), mapped to levels by `calibration.LEVELS`.
  **It is not calibrated** (section 4, F1).

### The constants

Every `[cal]` constant is in `hypotheses.PARAMS`. Changing one needs a VERSION
bump and a frozen bench. These matter most:

| constant | value | what it sets |
|---|---|---|
| `panel_cells` | 20 | cells per panel |
| `sentinels` | 4 | clear negatives per panel |
| `tolerance` | 0.2 | the band "within" is judged against |
| `plateau` | 0.1 | P a candidate needs to join the plateau |
| `resolve_p` | 0.8 | P(within) needed to resolve |
| `draws` | 400 | posterior draws |
| `added_min_reads` | 5 | reads a side before an added point can lead |
| `voi_cap` | 8 | most cells to one interval |
| `voi_epsilon` | 0.02 | smallest rise worth a look |
| `strata_min_reads` | 5 | reads of each kind before strata are used |
| `panel_looks` | 3 | looks before review (a session option) |

## 3. How it was measured

The person's gates were the reference throughout. Gates are in the table's
units, log1p.

| test | what | v2 | v1 |
|---|---|---|---|
| lsp11385_2, 3 runs, Opus | MAE / spread / packets per marker | 0.226 / 0.285 / 2.73 | 0.291 / 0.126 / 2.59 |
| E2 dev campaign: LSP11321, LSP11361, LSP11761 (17 markers), p193 (6 gated); 3 runs each, Opus, propose | MAE / within 0.2 / 0.4 or more off | 0.277 / 55 % / 20.5 % | 0.273 / 58.5 % / 21.1 % |
| same | accepted share (MAE) / review MAE | 33 % (0.14) / 0.296 | 83 % (0.21) / 0.315 |
| same | spread / packets per marker / oracle MAE | 0.288 / 2.63 / 0.096 | 0.252 / 2.84 / 0.079 |
| same, against the person's rechecks of 17 markers (ranges where they gave one) | MAE / 0.4 or more off / accepted MAE | 0.203 / 11 % / 0.130 | 0.208 / 12 % / 0.165 |

- **Paired difference** over 57 markers, v1 minus v2: -0.005 on the original
  gates and +0.005 on the rechecked ones. Both 95 % CIs span zero.
- **Per image** (rechecked truth): v2 is ahead on LSP11361 and p193, v1 on
  LSP11321 and LSP11761.
- **The person's verdict** after reviewing the gates: "overall it is doing a
  good job".
- **The person's own gates moved.** Five of their 17 rechecks moved their own
  gate by more than 0.3; the mean move was 0.21. A single human gate is not a
  0.2-tight truth.
- **Records:** in the main checkout, gitignored, under
  `docs/internal/bench/runs/2026-10-22-e2-dev/`:
  - `campaign.json` (session ids);
  - `truth_dev.json` and `truth_rechecks_2026-10-22.json`;
  - `gates_comparison_v6_vs_classic_vs_human.csv`;
  - `rescore.py`.
- **Pre-registered bars on the rechecked truth.** v2 passes four: MAE not worse
  than v1, 0.4 or more off <= 20 %, accepted MAE <= 0.20 at 50 % coverage
  (0.193), and review gates farther than accepted. It fails three:
  - **spread** <= 0.25: 0.288 (v1 fails it too, at 0.252);
  - **packets** <= 2.2 a marker: 2.63;
  - **no marker** more than 0.30 worse than v1: LSP11761 CD163 at +0.39, not
    rechecked.

## 4. Where it fails

Ranked by how much error each failure explains, with the evidence for each.

**F1. The reader's own standard per marker sets the ceiling, and the engine
cannot see it.**
- **What happens:** the posterior only sees one run's reads by one reader.
  When the reader is consistently stricter or more lenient than the person on
  a marker, every run agrees with every other and all of them are wrong.
- **Examples:**
  - LSP11361 SOX10: 5.49 in all three runs against 4.00 (the person later
    accepted 4.00 to 5.49).
  - LSP11321 Ecad: 6.66 in all three against 6.25, the same direction as
    lsp11385_2's Ecad (+0.56).
  - LSP11321 FOXP3: 7.30 in all three, above the person's highest acceptable
    gate of 6.99.
- **Calibration:** P(within 0.2) is not calibrated. Units at P >= 0.9 were
  within 0.2 for 51 % (62 % on the rechecked truth); units below 0.5 were
  within for 53 % (68 %).
- **The reader audit** (`tools/bench_gates/reader_audit.py`) showed blind
  Opus near-gate labels at chance against the person's labels on lsp
  (balanced accuracy 0.49 to 0.54), and 0.61 to 0.69 on CRC. Opus is
  self-consistent on identical panels (0 flips on 305 cells), so this is a
  standard, not noise.
- **Not the cause:** the controller. Replays show the controller finds the
  reader's gate.

**F2. Run-to-run spread is 0.29, above the 0.25 bar.**
- **Resampling within a run** explains about 40 % of the variance (replay on
  the stored reads).
- **The rest is path dependence:**
  - which looks a run took, and where;
  - partner gates decided earlier, which change the lattice and the prior;
  - reader differences between conversations, which shift the abstention rate
    from 44 % to 58 % on the same panels.
- **Pooling helps:** pooling three runs' reads cut lsp11385_2's spread from
  0.34 to 0.19.
- **For scale:** the person's own regate of lsp11385_2 moved their gates by
  0.28 on average.

**F3. Half the reads are `uncertain`.** The `uncertain` share is 0.48 in E2,
mostly in crowded tissue: dense immune aggregates, packed tumour nests.
- **Cost:** it drives the cost (2.6 packets a marker) and the `unreadable` and
  `looks_uninformative` closes, so two thirds of markers end in review.
- **Bias:** the shares lean toward isolated cells, which may not represent
  the population. The person's disagreements with any reader are 1.3 to 3.5
  times higher in dense tissue.

**F4. Failed markers are gated, not failed.**
- **The case:** LSP11761 PD1 has no positive cells; its bright regions are
  tissue folds. Both engines wrote about 7.7. One v2 run accepted 8.65 at low
  confidence.
- **What the readers saw and said:** they described haze and an edge band, but
  the whole-image check answered `cannot_tell` rather than naming an artifact.
- **No QC:** the image had no QC run, and the session warned of this. Since
  2026-10-22 v2 runs basic QC on such an image first (see "Artifacts and the
  display window" below); whether it catches these folds is not yet measured.
- **What the person expects:** the whole-slide view should catch it and fail
  the marker.
- **The absence check** (shared with v1) is off by default: on held-out images
  9 of its 17 closes were real populations.

**F5. Dim real populations are read as a neighbour's light.**
- **Examples:** dim tumour nuclei beside bright ones (SOX10); dim myeloid CD4
  next to bright T cells.
- **The person's convention:** include the dimmest cell type a marker is
  truly on, and stop where false positives start rising steeply. That is not
  the reader's 50 % own-stain crossing.
- **Status:** the guide's partner sentence was rewritten so partners are
  context, never a decider; that fixed one mechanism. The dim-population
  standard is still the reader's.

**F6. The display window changes the reader more than the text does.**
- **What helps where:** a brighter window lifts lsp's near-gate accuracy (from
  0.49–0.54 to 0.60–0.64). On IMC (0.61 to 0.55) and CODEX-LN (0.69 to 0.66)
  it makes things worse.
- **Per-marker or per-image rules:** neither can be learned from 4 label sets.
  The best per-marker rule gained about 1 %, inside the noise. Per-image there
  were 4 images with one gain.
- **Text and model:** old and new partner text made no difference. Sonnet is
  more lenient than Opus on lsp (+0.37).

**Artifacts and the display window (2026-10-22).** The window a cell panel is
drawn at is measured once per marker per image from its cells: black from the
30th–50th percentile band, the bright end from 16 cells of the 97th–99.5th.
Cells QC excludes are never used. Without QC an artifact brighter than the real
positives fills that bright band once it covers about 1–2 % of cells:
- **Synthetic:** a fold twice as bright over 2 % of a scene lifted every
  marker's white 2.1–2.3× and drew real positives at 0.12–0.31 instead of
  0.40–0.75. The vision contrast check cannot undo it: its grid shows the
  cells nearest that same bright reference, and its brightest column is 1.6×.
- **Tried and rejected: a cell-table screen** (cells bright in 3 or more
  consecutive channels, the person's cyclic-imaging idea, with a spatial patch
  condition). Clean on synthetic tables, but on real ones it flagged 6–27 % of
  cells: on lsp11385 a dense T-cell infiltrate, because panels place related
  markers side by side (CD45, Ki67, CD3e; PDL1, PD1, CD4, FOXP3), and it would
  have drawn immune markers 20–50 % too bright.
- **Built instead, on pixels: the Artifact Detector's cycle rule**
  (`artifacts.cycle_hits`). Where QC knows the cycles (`cycles.resolved`, each
  2–20 channels; otherwise the most-channels rule alone, as before), a place is
  a fold seed when a run of consecutive cycles has half its markers raised
  and its own nuclear stain differs from the median of every cycle's there.
  Nuclei do not change between cycles; cells really bright in one cycle's
  markers keep the same nuclei, so the lsp11385 infiltrate is not flagged.
  A bright one-cycle artifact over 12 % of a synthetic image drew that cycle's
  marker at 2.6–2.7× its window; after basic QC, 1.00×. QC bench unchanged.
- **Basic QC (`run_basic_qc`, gating option `basic_qc`, on for v2):** on an
  image with no QC result the bulk pass runs the detector and DNA retention
  before calibrating, with no model calls, writes the objects scoring 0.7 or
  more as automatic, unapproved regions in a `basic` result, and the cells
  inside are left out everywhere. Weaker objects are held back: on lsp11385
  the most-channels rule scores the epidermis (the Ecad and SOX10 population)
  0.51 as a fold. A full QC session reviews and supersedes it.
- **Not caught:** an artifact raised in a cycle's markers but not in its
  nuclear stain, and folds present in every cycle that cover under half the
  channels.

**F7. The validation base is thin.**
- **What was measured:** four dev images plus lsp11385_2, all multiplexed
  immunofluorescence (PCA melanoma and p193 colon).
- **Not run yet:** the 12 held-out PCA images (E3, run once only, frozen).
- **Not available:** non-LSP hand gates for IMC, CODEX-LN and CRC.
- **Not done:** a blind re-gate by the person, which would set the spread band.
- **Defaults brought to what was measured (2026-10-22).** The campaign ran
  with `display_check` off, `classifier` off, combined Opus workers and
  propose mode. Since then:
  - `display_check` left out is `off` with v2 (`vision` with v1), because the
    cell panels are drawn at the windows the check chooses and the window
    moves the reader (F6). An image that already had auto contrast keeps
    its choice either way.
  - Cell panels are the `threshold_evaluation` task, not `image_inspection`,
    so they go to the model that chooses gates (Opus in the campaign).
  - `classifier: "layer"` stays the default. It runs after a gate is written
    and never moves it, so v2's gates are the same with it on or off.

  - `basic_qc` is on with v2: an image with no QC gets QC's model-free checks
    first (see "Artifacts and the display window").

  Still never run live with v2: the classifier layer, basic QC, dataset scope, `tiles`,
  and whatever model the Plexora AI admin page routes `threshold_evaluation`
  to for the app's own runs. Run one live session with the defaults before
  relying on them.

**F8. Operational traps found during the campaign.**
- **One table per node:** a data node holds one cell table at a time.
  Concurrent sessions on different node images break each other
  (`had not loaded table`).
- **A reconnect strands a separate MCP process.** The new node address is only
  noticed in the process that reconnected. A separate `plexora mcp serve`
  keeps the old port in two caches keyed by project, not by address: the
  AgentSession table cache and `source_image.SHELF`.
  - Workaround: load two local tables and render four tiny local regions, then
    resume the bulk job.
  - The fix is not written (section 6).

## 5. Tried and not adopted

So nobody repeats them. Numbers are in CANDIDATES_PLAN.md.

| idea | result |
|---|---|
| A resolving plateau member leads, not the one nearest the prior | Not better on lsp11385_2 (MAE 0.235 to 0.222, spread up), on the bench, or on three dev images (0.239 / 0.270 / 0.394 to 0.246 / 0.282 / 0.393) |
| A global asymmetric cost (weight false negatives) | Trades markers; mean flat (kappa 2: CD4 better, CD45 and PD1 worse) |
| Symmetric shrinkage of interval shares | Cannot change a leader: a Beta(a, a) mean never crosses one half |
| `split_at_close` (cut on reads in hand when out of looks) | Few reads per part follow reader noise; off |
| Wilson test per rival (v5) | Exact ties never fire; the posterior rule replaced it |
| Deterministic fallback gate (Otsu, scored start, mixture) | None is good on held-out: 0.37 / 0.46 / 0.51 |
| Per-marker offsets | Banned by the rules; leave-one-out offsets on PCA gave 0.34 to 0.45 |
| Brighter display window everywhere | Helps lsp, hurts IMC and CODEX-LN |
| Per-marker or per-image window rule | Not learnable from 4 label sets (+1 %, noise) |
| Wider field around each cell (36 µm) | Worse everywhere (lsp 0.39) |
| Conditional gates, partner revisits, saturation stops, fibrous shape detector | Dropped on stored v1 records: no error separation |

## 6. Opportunities

Ordered by expected value per unit of cost. Each says how to test it.

1. **Calibrate P(within 0.2) on the E2 records.** There are 171 units with
   reads.
   - Use `tools/bench_gates/calibrate.py` and check reliability on held-out.
   - It costs no model calls and makes the confidence honest.
   - It cannot move a gate, and it cannot see F1's bias. Report the fitted
     rates beside the review note.
2. **Make failed-marker detection part of the default path (F4).**
   - Run QC first, or warn harder when an image has no QC; folds are what QC
     detects.
   - Look at why the whole-image packet for LSP11761 PD1 read `cannot_tell`.
   - Test a whole-image question that names folds and edge bands explicitly.
   - Bench: the dev images' failed markers, plus a synthetic fold scene.
3. **Pool reads across readers or runs on the plateau cells (F2).**
   - Within "never averaged", the allowed form is more reads into the same
     posterior, never two gates averaged.
   - Pooling three runs cut spread 0.34 to 0.19 on lsp11385_2.
   - A cheaper variant: put only the cells that decide a review marker to a
     fresh conversation.
   - Measure spread and cost together; the bar is 0.25 and 2.2 packets.
4. **Crowded cells (F3).** Two options:
   - **Re-show uncertain cells at a larger crop.** This touches "a cell is
     never shown twice", so it is the person's call (plan question 1). Test it
     on the labelled contested cells first.
   - **Use table features as evidence in crowded strata.**
     `value_over_neighbours` and `neighbour_max` already exist, and pixel
     features beat the eye there (83 % on contested cells). The classifier is
     the natural carrier, as a discrepancy detector that never moves the gate.
5. **Score against ranges, and measure the human band.** The person's rechecks
   are ranges, and their own gates move 0.21. Ask for the blind re-gate (plan
   question 3) to set the spread bar. Use range truth in `score_gap.py`; it is
   in `rescore.py` today, not in the scorer.
6. **Guide text for dim populations (F5).** Test on the labelled cells per
   marker class, with multi-lineage markers like CD4 broken out. Change the
   text only if per-cell balanced accuracy does not drop on any class in any
   of the four label sets.
7. **Cost (F3, section 3).** The panel task is `threshold_evaluation` (effort
   high) since 2026-10-22; it was `image_inspection` (effort medium) before.
   - If a cheaper model matched Opus per cell, the loop would cost far less.
     The reader audit found Sonnet lenient on lsp, so validate per marker class
     before routing.
   - Earlier `looks_uninformative` closes save looks. Set `voi_epsilon` on the
     synthetic bench only.
8. **Fix the operational bugs (F8).**
   - `nodes.address_generation` should notice another process's reconnect:
     compare the endpoint and token in nodes.json, or re-resolve on a refused
     connection.
   - A node could hold more than one table.
   - Both block running campaigns unattended.

## 7. How to run things

| task | how |
|---|---|
| unit tests | `PYTHONPATH=.:../spatialagents/src pytest tests/test_gating_hypotheses.py tests/test_autogate_units.py tests/test_gating_panel_replay.py`. A worktree lacks `client/node_modules`, so ignore JS-probe failures |
| offline bench, paired arms | `tools/bench_gates/bench_v6.py` (synthetic scenes, `noisy:P:U:L` reader) |
| one scene traced | `tools/bench_gates/trace_scene.py` |
| replay the decision on stored sessions | `tools/bench_gates/replay_v6.py TRUTH IMAGE SESSION...` (`--set leader=resolving`, `--common-prior`) |
| score sessions | `tools/bench_gates/score_gap.py --truth T.json ARM=GLOB ... --baseline v1arm --oracle` |
| live campaign | `tools/bench_gates/run_arms.py` (arms `v1`, `v2`, `candidates_2`; records under `docs/internal/bench/campaigns/`) |
| reader audit on labelled cells | `tools/bench_gates/reader_audit.py render / briefs / score`. Labelled sets are gitignored under `docs/internal/bench/runs/` |
| live, by hand | `gating_session_start` with `engine` (default v2), `mode: "propose"`, then the `gate-packets` workers; for a node image see the operations notes in CANDIDATES_PLAN.md (E2) |

Live runs go in propose mode only, and benches use scratch data roots, never a
user's install. Never choose a constant on an LSP image.
