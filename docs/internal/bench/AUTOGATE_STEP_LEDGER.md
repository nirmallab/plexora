# Automatic gating: which steps earn their place

*A running ledger, kept while the hybrid (cell model + vision) work is built
and tested (2026-10-10 on). For each step of a gating session: what it costs,
what it did to the gates on real tissue, and a verdict. Each entry names its
evidence; numbers come from recorded sessions or from studies in
`docs/internal/bench/runs/` (gitignored), never from memory.*

> **2026-10-12: the cell model was removed.** On the PCA benchmark (3 images x
> 17 markers against the user's gates) its per-cell overrides were worse than
> the gates. `hybrid` is gone with it; the multi-view look survives as its own
> off-by-default session option, `tiles` (`--variants baseline,tiles`). The
> entries below are kept as history: file and option names in them (`hybrid:
> model`, `cellcall.py`, `correction.py`, `neighbourhood.py`, `correction_check`)
> no longer exist in the code, and `docs/internal/CELL_CALL_LEARNING.md` is
> in git history only.

Verdicts: **helped** (moved gates toward the expert more often than away),
**neutral** (no measurable effect), **hurt**, **untested** (no real-tissue
evidence yet), and a proposed action: keep / fix / candidate to remove.

## In short (2026-10-10)

**Helped:** the scored start (the largest single gain); T4 candidates and the
purity push; the multi-view look (`hybrid: tiles`: per-cell reading +11
points, sessions +2.7 points of expert-cell agreement at the same tokens);
the cell model's correction of the approved gate (`hybrid: model`: +12
points on the markers it moves, live; applied without asking, and to gates
written for review). `hybrid: full` in its final form, live: lsp 0.651 near
(baselines 0.612-0.633) at MAE 0.133 (0.152-0.165); LN 0.760 (0.724-0.731);
tokens within +6 %.

**Did not help, removed or switched off:** leaving model-flagged cells out
of estimates before the looks (removed); a vision veto of the model's
calls (never built); the correction's confirmation packet (off: it refused
moves as good as those it took); int8 weights (rejected); the whole
spatial-evidence switch -- `diagnose`, `stratify`, `decide` and the
uncertain layer (removed 2026-10-10; what the cell model reads of a cell's
neighbourhood moved to `autogate/neighbourhood.py`).

**Candidates to remove or rework, not yet acted on:** the T3 reference look
(7 looks, no gate moved, 4 manual reviews); the whole-image confirm after a
repeat purity failure (32 of 44 said "holds"); T2's "about right", wrong 22 of 24 times when the human gate lay
elsewhere -- the correction now covers part of it. With the multi-view
tiles the reader says "unsure" more, and more markers close for review at
T2/T3 (3-5 a run against 2).

**Default:** `hybrid` stays `off`: two to three runs an arm clear "better",
not the pre-registered bar (+5 points near on lsp, MAE ≤ 0.136).

## In short (2026-10-11, PCA benchmark and re-planning)

Second human-gated dataset: three PCA images x 17 markers, against the user's
gates (E12, E13; five corrected by the user on recheck).

- **Helped, kept:**
  - re-planning when a marker fails (an absent PD1 called absent in every run);
  - the cross-lineage contamination screen;
  - no automatic move past cells a look judged, and the data-checked `within`
    (CD163 8.36 → ~7.9; the user says 7.8);
  - the floor-valley scored start (a zero-heavy SOX10 3.03 → 3.87; user 4.0).
- **Did not help, removed:** a look at both sides of the gate after a
  `mixed` first look. The rows beside the gate read mixed too, and its review
  savings came from accepting off gates. The patch is archived.
- **Acted on:** the whole-image look after a repeat purity failure became a
  confidence cap, and the cell model is withheld from an image with no QC result.
- **Overall:** accuracy against the human gates is about where it was (MAE
  0.364 → 0.345-0.367; 0.327 → 0.308-0.328 with the user's rechecks).
  LSP11321's gates sit ~0.4 below the user's in every arm, old included: an
  image convention, not a step.

## Evidence base so far

- **E1** — seven recorded live sessions on lsp11385_2 (12 human-gated markers
  each): the six of the spatial ablation (2026-10-08, baseline / stratified /
  decide × 2) and `cellcall-live` (2026-10-10, spatial off). The gate after
  every step was rebuilt from each session's `decisions.jsonl` (`scored`,
  `answered` outcomes, `purity_push`, the unit's `final`) and scored against
  the human gates (|miss|, log1p units) and the expert's labelled cells (281
  near the human gate, 73 far from it; gate-call agreement). Scripts:
  `runs/2026-10-1x-hybrid/stages.py`, `transitions.py`.

### E1: the gate after each stage (84 marker-runs)

| stage | MAE vs human | near-cell agreement | far-cell agreement |
|---|---|---|---|
| Auto (mixture) gate | 0.294 | 0.573 | 0.753 |
| scored start | 0.190 | 0.597 | 0.759 |
| after the first T2/T3 look | 0.190 | 0.597 | 0.759 |
| after every look, push and check | 0.149 | 0.616 | 0.765 |
| written gate | 0.147 | 0.619 | 0.765 |
| *the human gate* | *0* | *0.591* | *0.753* |

The near-gate expert cells were drawn around the human gate, and no single
threshold separates them well: the human gate itself agrees with only 59 %.
Gate-call agreement on that set therefore moves little with the gate; it
is the per-cell layer that lifts it (59 % → 79 %, `CELL_CALL_LEARNING.md`).

### E1: each transition

| transition | times reached | moved the gate | mean change in \|miss\| when it moved | toward / away | packets (a run) |
|---|---|---|---|---|---|
| scoring (Auto → start) | 84 | 70 | −0.125 | 63 / 7 | 0 |
| T2 look | 84 | 0 (by design: it gives a direction) | — | — | 12.0 |
| T3 biological look | 7 | 0 | — | — | 1.0 |
| T4 candidates | 110 | 39 | −0.066 | 34 / 3 | 15.7 |
| purity push | 32 | 20 | −0.044 | 17 / 3 | 0 |
| regression confirm | 47 | 0 | — | — | 6.7 |
| final snap | 84 | 41 | −0.004 | 0 / 0 | 0 |

T2's direction against where the human gate lay from the gate it judged
(tolerance 0.05): **up** right 30 of 42; **down** right 11 of 11;
**about right** right 2 of 24 — 16 times the human gate was lower, 6 times
higher. "About right" ended 21 units at the scored start.

Regression confirm (47): 31 "holds" (→ accepted at low confidence), 6 "too
low" (→ more T4; SMA's −0.21 → −0.08 came this way), 9 → manual review, 1
"holds" → accepted.

- **E2** — study S3 (2026-10-10): the expert-labelled near-gate cells of
  lsp11385_2 (281) and IMC (268) drawn with the real collage code, blind
  (index captions), one collage per marker, read by four fresh Sonnet
  readers (~53k tokens each), one word per cell (own / not_own /
  cannot_tell). `runs/2026-10-1x-hybrid/s3_render.py`, `s3_score.py`.

| layout | lsp: accuracy on answered | lsp: all cells | IMC: answered | IMC: all | pixels |
|---|---|---|---|---|---|
| `t2` (today: morphology, context) | 0.570 (235) | 0.477 | 0.539 (206) | 0.414 | 5.05M |
| `t2m` (+ judged outline, expected and not-expected partner) | 0.683 (278) | 0.676 | 0.589 (241) | 0.530 | 5.84M (+16 %) |

`t2m` also cut "cannot tell" from 46 to 3 (lsp) and 62 to 27 (IMC).

- **E3** — the check row (a vision veto of the model's left-out cells),
  tested offline on the same cells with the held-out model's p at the human
  gates. Where the model was confident not own and the reader said own, the
  expert sided with the model 30 of 32 times (`t2`) and 17 of 18 (`t2m`);
  where it was confident own and the reader said not own, the model was right
  14 of 17 and 11 of 13. A reader's word on a confident model call is worse
  than the call. `check_row.py`.

- **E4** — onnxruntime throughput (M2 Pro, CPU): every split of the cores
  into sessions × threads ran the 3-seed model at 1.1-1.5 ms a cell (it is
  compute bound). int8: static quantisation moved 34-44 % of confident calls
  (max |Δp| 0.32); dynamic missed the parity bar (max |Δp| 0.04-0.06) and was
  not faster; fp16 weights were exact but not reliably faster. fp32 stays.
  `quant/README.md`. End to end (`plexora ai bench cellcall`, 40,000 cells of
  lsp11385_2, idle machine): 433 cells/s with one worker, 528 with two, 455
  with four -- about 75 s a marker; the single-threaded path before was ~400.

- **E5** — study S1 (2026-10-10), hybrid step 1 on lsp11385_2: baseline-1's
  scoring rebuilt for 11 markers (it reproduces every recorded start to the
  digit), the held-out model swept at each start (±4 halves, 40k cells), and
  the Auto fit and the scoring redone with the model's confident not-own
  cells left out of estimation (`cell_exclusions.omitting`). `s1_lsp.py`,
  `s1_summary.py`.

| left out | MAE vs human | near-cell agreement | far | toward / away |
|---|---|---|---|---|
| nothing (the recorded start) | 0.198 | 0.587 | 0.752 | — |
| at or above the gate | 0.199 | 0.587 | 0.752 | 1 / 2 |
| from 2 band halves below | 0.182 | 0.564 | 0.752 | 5 / 2 |
| from 4 band halves below | 0.211 | 0.567 | 0.752 | 2 / 4 (CD45 +0.21) |
| 2 halves, partners' too | 0.196 | 0.564 | 0.739 | 5 / 3 (FOXP3 +0.17) |

The gate moves toward the human gate but away from the expert's cells
(CD4: 6.83 → 6.73 against a human 6.6, and the cells between were mostly
labelled glow). The pre-registered bar (+3 points of agreement) is not met.

- **E6** — study S4 (2026-10-10), hybrid step 3 on lsp11385_2: every
  recorded session's final gate (84 marker-runs) and the 12 human gates,
  each swept by the held-out model (±6 halves, 40k cells; gates within half
  a band half share a sweep), corrected by `correction.solve` over a grid of
  settings, the T4 intervals the session's looks judged replayed as the
  clip. Scored on the expert's cells. The confirmation is simulated with
  the readers' measured confusion from E2 (`t2m`: a reader agreed with the
  truth 68-69 % either way) on the cells each move changes. `s4_lsp.py`,
  `s4_confirm.py`.

| grid, window, λ, clip | moved | Δ near-cell agreement | Δ far | Δ MAE | worst marker (near) | with the check simulated |
|---|---|---|---|---|---|---|
| lattice, ±4, λ 1 | 3 / 58 | 0 | 0 | −0.005 | 0 | 0 |
| lattice, ±4, λ 0.5, unclipped | 29 / 58 | +6.3 pts | −0.5 | −0.010 | 0 | +4.9 pts |
| **lattice, ±4, λ 0.5, clipped** | 25 / 58 | **+5.2 pts** | −0.5 | −0.009 | 0 | **+4.1 pts** (78 % confirmed) |
| lattice, ±6, λ 0.5, clipped | 26 / 58 | +4.9 pts | −1.2 | +0.001 | 0 | +3.8 pts |
| fine grid, ±4, λ 0.5, clipped | 42 / 84 | +3.4 pts | −0.7 | +0.006 | −1.9 pts | +2.6 pts |
| human gates, fine, ±4, λ 0.5 | 5 / 12 | +3.5 pts | 0 | +0.065 | −8.3 pts | +2.6 pts |

λ ≥ 2 never moved a gate. The human gates move away from the human
(+0.065 MAE) toward the expert's per-cell labels: the same disagreement
between gate and labels as E1 (the human gate itself agrees with 59 %).

- **E7** — study S2 (from the same sweeps): one marker swept at gates 1-2
  band halves apart. On the cells both read, no confident call ever turned
  into the opposite one (0 of ~300k); 4-40 % moved between confident and
  unsure (CD68 across 1.9 halves: 40 %). A run is reused within 2 band halves
  (`hybrid.HYBRID["rerun_halves"]`).

- **E5b** — S1 on IMC and CODEX LN (no recorded sessions: the start is the
  scoring proposal each label page was drawn around; the model held out of
  each image). Near-cell agreement, nothing left out → 2 halves → 4 halves:
  IMC 0.565 → 0.594 → 0.538 (Ki67 +29 points, CD8a −12); LN 0.615 → 0.631
  → 0.546 (CD21 +38, CD4 −27). Partners' sets too made it worse (LN FoxP3
  −52). Step 1 stays dropped. `s1_other.py`.

- **E8** — S4 across images: the correction at each image's starting gates
  (IMC, LN: the scoring starts, fine grid, no looks to clip by) beside the
  lsp session gates (lattice, clipped), window ±4 halves. Δ near-cell
  agreement (worst marker):

| λ | lsp11385_2, look-decided gates | IMC, scoring starts | CODEX LN, scoring starts |
|---|---|---|---|
| 0.5 | +5.2 (0) | +15.2 (CD68 −14.3) | +15.3 (−4.3) |
| **0.6** | **+5.1 (0)** | **+12.8 (CD68 −9.5)** | **+15.6 (0)** |
| 0.7 | +3.0 (0) | +12.1 (−4.8) | +14.9 (0) |
| 1.0 | 0 (0) | +9.7 (0) | +14.9 (0) |

Gates the looks already refined move less; gates straight from the scoring
move more and gain more. λ = 0.6 is the setting that clears the bar on lsp
and one other image (`correction.CORRECTION`); IMC's CD68 is the cost on
record. `s4_other.py`.

- **E9** — the live ablation (2026-10-10), `mode: propose`, `qc: strict`,
  `reuse_answers: false`, Sonnet workers (two a session, six markers each)
  through a shell driver running the working tree, held-out weights,
  per-cell layers off; stores snapshotted before and restored after. lsp
  baselines and stratify runs are E1's. Near/far: gate-call agreement with
  the expert's cells; MAE against the human gates. `aggregate_live.py`,
  `score_live.py`, `corr_attr.py`.

| image, arm | runs | near | far | MAE | packets | worker tokens | marked for review |
|---|---|---|---|---|---|---|---|
| lsp baseline | 3 | 0.612 / 0.630 / 0.633 | 0.767 | 0.152 / 0.157 / 0.165 | 32 / 34 / 38 | 260k / 203k / 213k | 1 / 2 / 2 |
| lsp stratify | 2 | 0.616 / 0.609 | 0.767 | 0.143 / 0.128 | 41 / 39 | 230k / 229k | 1 / 1 |
| lsp tiles | 2 | 0.662 / 0.641 | 0.767 / 0.699 | 0.177 / 0.421* | 37 / 33 | 214k / 205k | 1 / 1 |
| lsp full (tiles + correction, checked) | 2 | 0.644 / 0.669 | 0.767 | 0.158 / 0.174 | 36 / 41 | 229k / 234k | 3 / 1 |
| LN baseline | 2 | 0.724 / 0.731 | — | — | 29 / 28 | 200k / 205k | 2 / 2 |
| LN full (checked) | 2 | 0.753 / 0.738 | — | — | 27 / 28 | 201k / 201k | 3 / 3 |

\* one look called FOXP3 "no positive population" (written at the column's
maximum against a human 6.0); without FOXP3 that run's MAE is 0.18.

Means against the baselines: lsp tiles +2.7 points near, full +3.2; LN full
+1.8. Tokens: tiles −7 %, full +3 % (lsp), ±0 (LN). The correction step
on its own, every check of the four full runs (agreement at the approved
gate against the corrected one, on the corrected markers):

| | checks | taken | Δ near on those markers | Δ \|miss\| |
|---|---|---|---|---|
| lsp | 10 | 10 | +12.1 points | −0.022 |
| LN, taken | 8 | 2 | 0 | — |
| LN, refused | | 6 | **+12.0 points had they been taken** (CD21 +21 twice, CD8 +30) | — |

The check took every lsp move and refused six of eight LN moves whose gains
match the taken ones: no better than taking every move (consistent with
E3). `HYBRID["confirm"]` now defaults to off: the move is applied as
computed.

- **E10** — LN `full` with the check off (`HYBRID["confirm"]` False, one
  run): near-cell agreement **0.760** (baselines 0.724 / 0.731, checked
  0.753 / 0.738), 24 packets (28-29), ~189k worker tokens (~202k): the three
  corrections applied as computed (CD21 8.63 → 8.91, 0.71 → 0.92 on its
  cells). Five markers closed for review at T2/T3 ("about right" but unsure,
  no reference left: more often with the multi-view tiles, 3-5 a run against
  2), and a unit closed that way never reached the correction. Offline,
  correcting those review gates from the run already read ahead of their first
  look (no wait, no question; `hybrid.correct_for_review`) moved 4 of 14 in
  the five hybrid runs: CD4 +13.6 points in all three LN runs, CD8 0, none
  worse. Now part of `model`. `review_corr.py`.

- **E11** — the final configuration, live, once on lsp11385_2 (`full`,
  check off, review gates corrected): near 0.651, far 0.753, MAE **0.133**,
  34 packets, ~239k worker tokens, 3 markers for review. Corrections applied:
  CD163 6.36 → 6.63, SMA 5.79 → 5.92, SOX10 (no change in calls); on a review
  gate CD8a 6.10 → 6.21. Over the three lsp `full` runs: near 0.644 / 0.669 /
  0.651 (mean 0.655, baselines 0.625), MAE 0.158 / 0.174 / 0.133 (mean 0.155,
  baselines 0.158).

- **E12** — the PCA benchmark (2026-10-11), 3 images (LSP11321, LSP11761,
  LSP11361) x 17 markers, `mode: propose`, `qc: strict` (no image had a QC
  result), Sonnet workers. Arms:
  - old (main, hybrid off) and hybrid (`model`);
  - R0 / R1: re-planning code replaying only the old arm's answers (agent
    `bench-old-clean`), without / with the bracket;
  - F1 / F2: fresh looks with the bracket.
  Scored against the user's gates as points (48 present; PD1, CD20L and PDL1
  in LSP11761 absent). Scratchpad `bench/phase2/`; plan
  `docs/internal/GATING_REPLAN_PLAN.md`.

| arm | MAE | within 0.2 | 0.4+ off | accepted | review | absent called |
|---|---|---|---|---|---|---|
| old | 0.364 | 18 | 15 | 32 | 19 | 0/3 |
| hybrid (model) | 0.338 | — | — | — | — | 0/3 |
| R0 | 0.367 | 20 | 15 | 32 | 19 | 1/3 |
| R1 (+ bracket) | 0.345 | 21 | 15 | 39 | 12 | 1/3 |
| F1 (fresh, bracket) | 0.359 | 19 | 15 | 42 | 9 | 2/3 |
| F2 (fresh, bracket) | 0.356 | 19 | 14 | 40 | 11 | 1/3 |

  - **The bracket.** It fired 87 times over R1, F1 and F2 and stayed on `mixed`
    63 times. At fixed answers it changed MAE by −0.02, and it accepted
    several 0.5-1.0-off gates that unsure looks had sent to review.
  - **Rule B** (passing `mixed` going up) gained only in LSP11321, where the
    user's gates sit higher everywhere. Without that image, MAE went 0.297 → 0.304.
  - **Hybrid (old benchmark).** Per-cell overrides lowered agreement with the
    human-gate calls (κ 0.765 → 0.684, worse on 33 of 46 markers), mostly by
    promoting whole regions, an artifact edge band in LSP11761 among them.
    That image had no QC result.
- **E13** — T3 and the whole-image confirm on every PCA session (old, hybrid,
  R0-F2; `bench/phase2/steps.py`):
  - **T3:** 10 looks, 1 sent the gate on (0.33 → 0.11 from the user's), 5 ended
    in review.
  - **Whole-image confirms:** 185; 128 said "holds", 25 "too low" (0 toward the
    user's gate, 4 away), 17 "artifact".
  - **The confirm after a purity push, with only partners failing:** 62; 42
    "holds", 14 "too low" (none improved a gate; Ecad 0.01 → 0.37 twice), and
    4 "artifact", all on CD8a, a real population.

## The ledger

| step | cost | evidence | verdict | action |
|---|---|---|---|---|
| Bulk pass, display calibration, profile | code | needed by everything after | — | keep |
| T1 acceptance (+ audit strip) | ~70 tokens a marker | E1: no human-gated lsp marker settled at T1, so untested on this image | untested | measure on an image with T1 markers |
| QC tier and QC exclusions | code (+~350 a flagged marker) | not exercised by the lsp markers | untested | keep |
| **Scored start** (`scoring.score`) | code | E1: the largest gain of any step (MAE 0.294 → 0.190; 63 toward, 7 away) | **helped** | keep. Fix: a broad partner's `coexpression` lead pulled subtypes far low — CD8a led by CD45 started −0.27, by CD3e +0.03; CD68 led by CD45 −0.51, from its own distribution +0.02 (decide-1, where CD45 had failed) |
| T2 look (rows → direction) | ~650 tokens, 12 a run | E1: "up" and "down" are reliable, **"about right" is wrong 22 of 24** and mostly where the gate should go down | helped (direction), weak at stopping | the hybrid's correction (model's confident own cells below the gate) targets the missed downs |
| T3 biological look | ~700 tokens | E1: 7 looks, no move, 4 ended in manual review | **neutral** | candidate to remove or fold into T2 |
| T4 candidates | ~320 tokens, 15.7 a run | E1: 39 moves, 34 toward the human gate; 71 of 110 packets moved nothing | **helped** | keep; the 71 non-moving packets are the cost to look at |
| Purity push | code | E1: 20 moves, 17 toward (CD163, FOXP3, MART1, SMA), 3 away (CD4 twice, CD8a) | **helped** | keep |
| Regression checks + confirm | ~350 tokens, 6.7 a run | E1: never moves a gate itself; mostly records "holds" at low confidence; 6 "too low" reopened T4 usefully. The partners (purity) check is behind 44 of the 47 whole-image looks -- a second purity failure after the one purity push -- and 32 of those 44 said "holds"; edge_ratio 13 (7 "too low"), guard_band 7, flip_share 5, delta_from_gmm 3 (`regress.py`) | neutral on gates, some value as a trigger | candidate: after the purity push, a repeat purity failure alone caps confidence instead of asking (saves ~5 looks a run) |
| Spatial `diagnose` | code | SPATIAL_ABLATION_2026-10-08: no change by design | neutral | **removed** 2026-10-10 |
| Spatial `stratify` (environment-spread rows) | +6-9 packets a run | SPATIAL_ABLATION: MAE 0.155 → 0.136 via partner confidence (CD3e high → CD8a); CD163 −0.06 every run | helped (small, costly) | **removed** 2026-10-10 with the switch. E9: its MAE (0.143 / 0.128) was the best of the live arms, but its near-cell agreement (0.616 / 0.609) no better than baseline, at +6-9 packets; the hybrid `full` (tiles + correction) reached MAE 0.133 in its final form without them |
| Spatial `decide` (contrast look, case table, T1 audit, spatial regression, uncertain layer) | code + ~800 a contrast look | SPATIAL_ABLATION: engaged nothing on lsp11385_2; the spatial regression checks are dead code (`Engine.run_regression` never passes `spatial`) | **neutral** | **removed** 2026-10-10, with the amber uncertain toggle, its AnnData column and `plexora ai bench spatial` |
| Cell model layer (per-cell calls beside the gate) | ~3 min a marker CPU, 0 tokens | CELL_CALL_LEARNING: near 59-63 % → 79-80 %, far 75-77 % → 84-85 % | **helped** (cells) | keep |
| Hybrid step 1 (left-out cells before the looks) | a model run per look marker before its first look | E5: MAE −0.016 but expert-cell agreement −2.3 points; the left-out set moves the mixture fit more than the threshold | **neutral to hurt** | dropped; the estimation-only layer removed from `cell_exclusions` (patch `runs/2026-10-1x-hybrid/s1_omit_seam.patch`) |
| Hybrid step 2a: `t2m` multi-view tiles | +16 % collage pixels | E2: per-cell accuracy +11 points (lsp), +5 (IMC). E9 live: near-cell agreement +2.7 points on lsp at −7 % tokens; MAE 0.177 against 0.158; one FOXP3 "no positive population" in two runs | **helped** (cells), mixed on gates | keep (`hybrid: tiles`); two runs per arm, watch FOXP3-like collapses |
| Hybrid step 2b: blind check row (vision veto of step 1) | ~1 row a look | E3: the veto would be wrong ~95 % of the time | **would hurt** | dropped before it was built |
| Hybrid step 3 (correction after vision) | one model run near the approved gate (~75 s CPU, read ahead while the first look is out) | E6, E8 offline; E9 live: the 10 lsp corrections +12 points near on their markers, MAE −0.02 | **helped** | keep (`hybrid: model`), also on gates written for review (E10, E11) |
| Hybrid step 3b: the correction's check packet (`correction_check`) | ~1 small packet a corrected marker | E9: took all 10 lsp moves, refused 6 of 8 LN moves that would have gained +12 points | **hurt** | off by default (`HYBRID["confirm"]`); the code path stays for a reader that does better |
| Hybrid `full` (tiles + correction) | ≈ baseline tokens (+3-6 %) | E9-E11: near +3.0 (lsp, 3 runs), +3.2 (LN, check off: 0.760); final form lsp MAE 0.133 against 0.158 | **helped**, below the pre-registered default-on bar | default stays `off`; more runs and a second human-gated image next |
| Model speed: int8 / pooled sessions | — | E4: int8 breaks confident calls; pooling ≤ 20 % | neutral | fp32; parallelise the crops, not the model |
| Re-planning on failure (`replan.py`, the `replan` packet) | a text packet (2-3k chars) per round, 1-3 rounds a session | E12: LSP11761 PD1 called absent in every run; CD20L flagged every run, called absent 1 of 4 from numbers alone; an image finding as a note, never a demotion | **helped** | keep (`SessionOptions.replan`, on) |
| Lineage contamination screen (cross-lineage only) | code | E12: flags exactly the two absent LSP11761 markers among all 51 pairs; siblings (CD8a vs CD4) excluded after a wrong `absent` | **helped** | keep |
| Judged-row clamp + `within` orphan guard | code | E12: LSP11761 CD163 8.36 → 7.85-8.00 in every arm; user rechecked 7.8 | **helped** | keep |
| Floor-valley scored start | code | E12: LSP11361 SOX10 3.03 → 3.87 (user 4.0); fires on no other benchmark column | **helped** | keep |
| `mixed` first look → bracket (both sides) | +1 T4 a bracketed unit | E12: stays on mixed 63 of 87; MAE −0.02 at fixed answers; accepted off gates | **neutral to hurt** | **removed** 2026-10-11 (patch `runs/2026-10-11-replan/mixed_bracket.patch`) |
| Whole-image look after a repeat purity failure | ~350 tokens, ~2 a session | E1 32/44 and E13 42/62 "holds"; none of the PCA's other answers helped | **neutral** | **replaced** 2026-10-11: confidence capped at low (`Engine.purity_capped`); with a spatial check failing too, still asked |
| Cell model on an image with no QC result | — | E12: the model promoted an artifact edge band | **hurt** | withheld from such images (`model_withheld`), said at session start |
| A picture per suspect marker in the `replan` packet | 1 image a suspect marker | 2 fresh LSP11761 runs: the planner read the tiles as saturated haze and kept PD1 (absent 6/6 from numbers); CD20L 1/2 | **hurt** | **removed** 2026-10-11 (patch `runs/2026-10-11-replan/replan_pictures.patch`) |
| Lineage re-screen when a partner is accepted | code | live K1: PD1 closed (look settled nothing) before SOX10 was gated and was never asked | fix | `replan.rescreen` |
| Absence check before the first look | +1.2 packets/marker | held-out 12 PCA images: 17 absent closes, 9 false (real 0.5-7.7 % populations); 0.449 vs classic 0.368 MAE | **hurt** | **off by default** 2026-10-19 (`absence_check`; `--variants absence`) |
| Classifier layer at each gate, cleaning only (`classifier: layer`) | CPU seconds a marker once features are cached; 0 tokens | CLASSIFIER_RULE_2026-10-19: +2.0 balanced accuracy pooled on 895 user labels (IMC +2.2, CODEX +2.8, lsp +1.5), every removal right, far set unchanged; additions 47 % right | **helped (per cell)** | keep, default on; never decides until adopted |
| Classifier check (`classifier: check`): flagged cells beside agreed ones in a T4 look | +1 T4 look a marker it disagrees on | p193 blind (LSP, CLASSIFIER_RULE_2026-10-19.md): read `mixed` CD3E 5.42→5.18 and CD8a 7.62→7.24 (user 5.08, 7.30) but aSMA 4.63→4.03 (user 4.71); read by counts nothing moved; mean gap 0.29 → 0.26 mixed / 0.27 word | **mixed**, one LSP image | off by default; `check_reading` default `auto` (word for markers.yaml `morphology: fibrous`, else mixed: the user's rule); a non-LSP image with user gates next |
| Check rows walked by their cells (`row_share` 0.25), not the flagged half alone | none | lsp11385 real run replayed (CLASSIFIER_RULE_2026-10-19.md): Ki67 5.89→6.19 (user 6.31), CD20L 6.28→6.41 (user: higher), NGFR 6.51→6.24 (user 5.64); every other check on lsp11385 and p193 unchanged; 0.5 lost SOX9 and p193 CD8a | **helped**, replay on two LSP images | live in `check`; a noisier image owed |
| markers.yaml PCNA caveat: a high dynamic range, dim to bright; a dim even nuclear fill is PCNA, not background | none | lsp11385 PCNAL alone, propose, fresh answers: 6.33 → 5.82 (user 5.55); the check row 5.82-6.33 read `mixed`/`mixed` (was `unstained`/`unstained`); rows below read unstained/neighbour at the session's display window | **helped**, one image | the user's wording; the rest of the gap is the display window, not the words |
| Split looks (`split_views: auto`): DNA \| marker (display black to the gate at mid-grey, blurred) \| combined, for dim markers and every look at candidates | +46 % on those looks' collage | A/B split off vs auto, lsp11385 (11) + p193 (7) markers with user gates, one run each: pooled mean \|gap\| 0.344 vs 0.355; p193 CD8a 0.06→0.70 and SOX9 0.35→0.69 broke the pre-registered 0.30 bar (lsp CD8a, FOXP3, CD163, PD1, p193 CD11c gained) | **against** (regressions) | removed 2026-10-20; `~/gatevision/splitab/` |

