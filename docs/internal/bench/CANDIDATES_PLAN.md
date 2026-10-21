# The candidates engine: pre-registered plan and bars

Branch `feature/gating-candidates`, from 2026-10-20. The engine is
`SessionOptions.engine = "candidates"` (default `classic`); the code is
`plexora/plugins/gating/server/autogate/hypotheses/`. These bars were written
before any candidates run was scored. A bar that is changed after a run is
reported as changed, with the run that prompted it.

## What changes

A marker that needs a look (not a clean split the audit sheet settles, not a
conditional gate) is no longer judged in rows below, at and above a moving
gate, then candidates beyond it. Instead:

- the frozen lattice's points are the candidate gates, read as competing
  hypotheses: never averaged, the gate written is always one of them;
- each look is a blind panel of numbered single cells: 20 from the intervals
  between candidates, 4 negative sentinels, shuffled, with no value, call,
  gate or ranking in the picture or the packet;
- code scores every candidate by the cells it would get wrong (interval
  own-stain shares, made monotone in value) and stops by rule: resolved when
  the leader beats every rival outside 0.2 (Wilson 95 % on the pooled cells
  between them), else review at the leader with a stated reason (looks spent,
  no new cell can separate them, sentinels read stained twice, most cells
  unreadable, continuous);
- at most 3 looks a marker; a cell is never shown twice for one marker.

## Measured before the change (zero model calls)

From the 24 stored held-out sessions (12 PCA images x 17 markers, arms B
and C of `~/gatevision/heldout`), scored with `tools/bench_gates/score_gap.py`
against the user's gates:

| arm | MAE | within 0.2 | >= 0.4 off | start MAE | lattice oracle MAE | oracle within 0.2 |
|---|---|---|---|---|---|---|
| B | 0.450 | 40.7 % | 33.3 % | 0.415 | 0.064 | 94.3 % |
| C (classic) | 0.368 | 42.6 % | 30.9 % | 0.421 | 0.079 | 92.6 % |

The lattice holds a point within 0.2 of the user's gate for 93 % of markers:
choosing among the candidates, not generating them, is where the error is.
On the 163 held-out units that reached a lattice, no single deterministic
source is good (Otsu 0.367, the scored start 0.457, the mixture 0.512 MAE).
These numbers were read on the held-out set: no prior, weight or default is
chosen from them, and the held-out set stays the final test.

## Decision gate 1 (dev, then held-out once)

Dev: lsp11385_2 and the PCA dev images, 3 runs per arm, one model id, blind
readers, `reuse_answers: false` (the panel's pictures differ from the classic
looks', so no answer replays across arms anyway). Candidates vs classic:

- MAE not worse; the share at least 0.4 off lower;
- run-to-run spread of the final gate not larger;
- model calls per marker no more than 2 on average;
- no empty gate on a marker with at least 0.5 % real positives.

Then the 12 held-out images once, frozen:

- MAE <= 0.30 (classic 0.368);
- within 0.2 >= 55 % (42.6 %);
- at least 0.4 off <= 20 % (30.9 %);
- mean |gap| of review-closed gates larger than of accepted ones.

If the held-out bars fail, the engine stays opt-in and the result is written
up here; classic stays the default.

## Offline (simulated reader)

`plexora ai bench gating --synthetic ... --arms session --variants
baseline,candidates --agent noisy:P` answers panels from the scene's truth
with each cell's word turned with probability P. P = 0.32 matches the
measured per-cell accuracy of the multi-view tiles (0.683). This checks the
controller's behaviour (looks used, statuses, placement under noise); it is
not evidence about a real reader.

### Offline result, 2026-10-20 (simulated reader; behaviour only)

8 synthetic scenes (easy, overlap, rare, gradient, dense_spill, low_snr,
dim_positive, heterogeneous) x 5 markers, one run each, classic
(`session:baseline`) vs `session:candidates`. The classic arm's noisy reader
turns a look's direction word, the panel's turns each cell's word: the two
noises are not the same thing, so only each arm's own behaviour is read.

| reader | arm | mean F1 | min F1 | mean abs fraction error | packets | review share |
|---|---|---|---|---|---|---|
| truthful | classic | 0.944 | 0.315 | 0.0107 | 44 | 0 % |
| truthful | candidates | 0.951 | 0.267 | 0.0059 | 26 | 10 % |
| P = 0.32 | classic | 0.936 | 0.367 | 0.0148 | 37 | 0 % |
| P = 0.32 | candidates | 0.947 | 0.380 | 0.0086 | 36 | 17.5 % |

Candidates did better where the classic walk stops short (rare CD4 0.871 to
0.927 truthful, 0.745 to 0.927 noisy; overlap CD4 0.544 to 0.675; gradient
CD4 0.835 to 0.918) and slightly worse on the two scenes no single threshold
separates (heterogeneous CD4, low_snr CD4). Under noise a share of markers
ends in review instead of accepted: by design, never "resolved" because the
looks ran out.

### First live run, 2026-10-21 (lsp11385_2, 5 markers, one run)

Session gs_20261021T010154_b5686e over MCP, propose mode (nothing written),
display check off, no memo; the reader was the developing session's model
(claude-opus-5-5), answering every panel from the picture alone. Against the
user's gates (`~/gatevision/gates_vs_human.csv`) and the classic engine's
2026-10-18 run on the same image:

| marker | user | scored start | candidates | classic |
|---|---|---|---|---|
| CD3e | 6.6 | 6.62 | 6.61 | 6.61 |
| CD8a | 6.1 (6.17) | 5.80 | 6.12 | 6.07 |
| FOXP3 | 6.0 (6.42) | 5.82 | 6.23 | 6.18 |
| CD4 | 7.0-7.1 (table: 6.6) | 6.86 | 7.00 | 6.89 |
| SMA | 6.2 (table: 6.0) | 5.72 | 6.36 | 5.72 |
| MAE, table gates | | 0.208 | 0.204 | 0.158 |
| MAE, rechecked | | 0.234 | 0.094 | 0.172 |
| within 0.2, rechecked | | 3/5 | 4/5 | 4/5 |

The user rechecked CD4 and SMA after seeing this table (2026-10-20): CD4 is
"definitely not 6.6, around 7 or 7.1", SMA "around 6.2". The rechecked rows
score CD4 at 7.05 (7.0 to 7.1 moves each MAE by 0.01); the CSV still holds
the table values. Against them the candidates engine is ahead of classic on
these five (0.094 vs 0.172), but this is one run, one LSP image (high SNR,
not a tuning image), the pre-Phase-2 engine, and the developing session as
reader: no claim beyond "not worse". Every marker spent 3 panels and closed
for review although four of five were within 0.2 -- the accept rule was too
strict. What the records showed, and the Phase 2 change each led to:

- The survey spent a third of its cells on lattice slivers a few hundredths
  apart in the background: candidates nearer than 0.1 are merged
  (`min_gap`), and the survey gives each interval an equal share (by cell
  count the background took the look; by value width, tried next, the long
  bright tail did -- 13 of 20 cells on a synthetic CD8).
- No marker could resolve: a rival just past the tolerance is never beaten
  when the stain's edge lies between the two. Resolved now asks the leader
  to beat every rival farther than 0.3 (`resolve_within`), and a rival whose
  call differs on fewer than 20 cells never blocks (`min_between`); a
  leader at the edge of the surveyed range is not resolved while lattice
  points lie beyond it (a synthetic low-SNR CD4 had resolved at its range's
  top, far below the truth).
- SMA (fibrous): "is the stain this cell's own" read bands beside cells as
  not, where the user counts them; a `panel_fibrous` reading names the
  spindle cell inside or along a band as own stain (untested).
- CD4: the reader called the cells at 6.8 to 7.0 unstained, as the classic
  reader did. Against the table's 6.6 this looked like a stricter reader
  convention; the user's recheck (7.0 to 7.1) says the reader was right.
  Nothing was changed for it.
- The answer to each panel told the reader the leading gate: removed; while
  a marker is still in panels the outcome carries its state and look count
  only.

## Phase 2 to 4 on the branch (2026-10-21)

- 2a: the controller changes above; `SessionOptions.panel_looks` (default 3)
  and the bench variant `candidates_2` for the hard-two-looks arm.
- 2c: a session is sent its own engine's reading guide
  (`packets.ENGINE_ONLY`); a packet needing another engine's entry (a
  classic look for a marker with one candidate) carries the text and its
  answer schema itself.
- 3: `classifier: "check"` on the candidates path
  (`hypotheses/classifier_check.py`): once a leader resolves, the classifier
  trained at it is compared across the range; where it disagrees enough one
  more panel shows its disagreements on both sides (at most half the panel)
  among cells chosen without it. It can make a rival win, never move a gate
  by itself; a low signal grade is not consulted.
- 4: `hypotheses/calibration.py` and `tools/bench_gates/calibrate.py`: a
  logistic calibration over five stored features, fitted on dev records
  only, checked by reliability and coverage on others; read-only at run
  time, and until a table is committed confidence stays `uncalibrated`.
- Not built: cross-marker revisits (2b) -- the engine already rescreens
  dependants when a partner fails, and a revisit loop is a feedback path
  that needs its own bench; the skill consolidation (seven to three) --
  documentation with length caps, best done once the engine's packet kinds
  are settled.

## Everything that needs no person's input (2026-10-20)

Built, so the benchmark measures the engine that would ship:

- **A lone candidate is tested.** A lattice of one point after thinning
  gets steps 0.25 and 0.5 (fit units) either side, snapped like lattice
  points (`hypotheses._steps`); before, such a marker fell back to the
  classic look.
- **Rare markers.** Under 2 % positive at the start, or the `long_tail`
  class: the leader resolves only once the cells above it are read stained
  (a `tail` look draws them, up to the brightest); read unstained, review
  (`positives_not_confirmed`). A rival also blocks when its call changes
  fewer than 20 cells but more than a quarter of the larger candidate's
  positives (`evidence.material`): a population of 15 cells is all of what
  its rival disputes.
- **The leader-vs-rival test weighs intervals by their cells.** The first
  look reads every interval equally, and the pooled reads let a sliver of
  40 cells outvote 1000; the share between two candidates is now each
  interval's share weighted by the cells it holds, at the weighting's
  effective sample size (`evidence.between`).
- **The classifier check reads its picks for what they are.** Its picks
  are enriched by its own calls and were counted as if random within their
  interval; the evidence is now post-stratified by the classifier's call,
  the check's look is the marker's last, and the check is shown only where
  a label-set score from another image has the classifier beating the gate
  (`classifier_trust.json`, `tools/bench_gates/classifier_trust.py`;
  exploratory otherwise). The 2026-10-19 wand bench had it beating the gate
  on lsp11385_2 only (63 % vs 56 %; IMC 75 vs 76, CODEX 79 vs 81) and its
  per-marker result files are not on this machine, so no table is written:
  the check runs exploratory.
- **Panels are blind in their manifest too.** The stored collage manifest,
  served to any client by `get_artifact`, carried each cell's value and why
  it was picked (a sentinel's "clear negative"); it now carries position
  and number only.
- **Replay.** `--replay-session --rerender` re-renders panels byte for byte
  (the manifest now records the pixel size a session drew at; every collage
  on an uncalibrated image had failed it) and checks each number is the
  cell `_panel` scored; two sessions with one seed send the same first
  panel (`tests/test_gating_panel_replay.py`).
- **The live campaign driver.** `tools/bench_gates/run_arms.py`: arms from
  `bench.VARIANTS`, k runs with the same seeds per arm and rotated order,
  propose mode, no answer reuse, refuses the live data root, checks no gate
  was written, records the reader model per task, writes session records
  and a manifest under `docs/internal/bench/campaigns/<campaign>/`. A cell
  panel is the `image_inspection` task (effort medium), classic's look
  `threshold_evaluation` (effort high): run gate 1 with both on one model
  (`--dev --model`) or say which served which.

Offline, these changed nothing measurable on the synthetic scenes (they
guard situations the scenes barely hold): truthful reader 0.9505 F1 and
31-32 packets with each change off in turn or all on; reader turning 32 %
of words, 3 seeds, all on 0.940 F1 / 22.5 % review / 144 packets against
all off 0.941 / 22.5 % / 143.

Not built, on the stored classic records (held-out PCA and dev sessions,
`~/gatevision`):

- **Conditional gates on panels.** 5 of 543 marker-images were
  conditional; on the 3 held-out the conditional gate was farther from the
  person's than the plain gate twice (CD3e 1.43 vs 0.89, PD1 0.16 vs 0.11;
  CD20L 0.10 vs 0.19). A `within:` fit stays a candidate.
- **A whole-image look on a failed spatial or edge check.** It would fire
  on 28 % of markers (spatial tiles 116 of 467, edge 22), whose gates are no
  farther from the person's (MAE 0.337 and 0.327 vs 0.308; 0.4 or more off
  27 % and 30 % vs 28 %).
- **Revisiting a partner when a dependent stays unresolved (2b).** Of 226
  dependents, those whose partner gate was 0.4 or more off were no farther
  off themselves (0.310 vs 0.323 with a partner within 0.2) and went to
  review less often (6 % vs 15 %): an unresolved dependent does not point
  at a wrong partner.
- **A fibrous detector from measured shape.** No labelled fibrous set to
  set it on, and a false trigger changes how every cell of a marker is
  read; the `panel_fibrous` reading stays tied to the panel's morphology.
- **Saturation and oscillation stops.** Under a three-look cap a leader
  cannot alternate twice; the cap and the novelty rule already stop it.
- **The zero-call falsifier (Phase 0.9a).** Superseded: the panels it was
  to justify are built and measured directly.

## Live run 1, 17 markers (2026-10-21)

lsp11385_2 against the user's regate of the same day
(`~/gatevision/lsp11385_2_user_gates_2026-10-21.json`; CD4 6.58 on purpose,
myeloid CD4 being dim; CD163 7.31, below which false positives rise
steeply). Both engines over MCP in propose mode, nothing written: display
check off, classifier off, no answer reuse, seed 1, `on_limit: extend`;
every packet answered by an Opus (claude-opus-5-5) gating worker. Classic
gs_20261021T151220_295e7c, candidates gs_20261021T152452_38f60d. One run
each on a high-SNR image: a reading of where the engine fails, not a
score to tune to.

| marker | user | classic | candidates |
|---|---|---|---|
| CD45 | 7.37 | 7.25 (-0.12) | 7.61 (+0.24) |
| CD31 | 5.97 | 5.96 (-0.01) | 6.45 (+0.48) |
| Ecad | 6.15 | 6.63 (+0.48) | 6.96 (+0.81) |
| SMA | 6.17 | 6.20 (+0.03) | 7.31 (+1.14) |
| Ki67 | 6.27 | 6.33 (+0.06) | 6.36 (+0.09) |
| PDL1 | 6.75 | 6.42 (-0.33) | 6.45 (-0.30) |
| SOX10 | 6.49 | 6.49 (+0.00) | 6.41 (-0.08) |
| MART1 | 5.52 | 6.27 (+0.75) | 5.39 (-0.13) |
| CD3e | 7.02 | 6.72 (-0.30) | 6.72 (-0.30) |
| CD20L | 6.57 | 6.38 (-0.19) | 6.38 (-0.19) |
| CD4 | 6.58 | 6.92 (+0.34) | 6.85 (+0.27) |
| CD8a | 6.32 | 6.12 (-0.20) | 5.98 (-0.34) |
| CD11c | 6.11 | 6.17 (+0.06) | 6.33 (+0.22) |
| CD68 | 6.34 | 5.83 (-0.51) | 6.13 (-0.21) |
| CD163 | 7.31 | 7.27 (-0.04) | 7.69 (+0.38) |
| FOXP3 | 6.30 | 6.17 (-0.13) | 6.23 (-0.07) |
| PD1 | 6.97 | 6.53 (-0.44) | 6.98 (+0.01) |
| **MAE** | | **0.235** | **0.309** |
| within 0.2 | | 10 / 17 | 6 / 17 |
| 0.4 or more off | | 4 | 3 |
| closed for review | | 6 | 13 |

Candidates lost. What its looks show (the session's observations), and
what was changed for each:

- **Most cells uncertain.** About 40 % of the panel cells in crowded immune
  fields were labelled `uncertain` (every sentinel was read right), and
  four markers closed `unreadable` after a single look. Unreadable now
  takes two valid looks (`unreadable_looks`); the panel guide says how to
  read a cell whose ring is partial in a crowd (stain on its free sides is
  its own; light only on the side facing a brighter neighbour is not).
  CD8a's second look was 14 of 16 uncertain: it would still close
  unreadable.
- **Resolved on the lattice's top point, nothing above read.** CD68: every
  cell shown up to 6.13, the lattice's highest point, read unstained; the
  user's gate is 6.34. A leader that beats its rivals there now gets a
  point 0.25 beyond (`step`, at most two a side).
- **No candidate near the gate.** CD163: nothing between 6.70 and 7.69,
  whose cells read 4 stained, 5 not, 11 uncertain; the user's gate is 7.31.
  A wide interval beside the leader that reads mixed is now cut into parts
  about the tolerance wide, once (`split`).
- **SMA** (+1.14): the `panel_fibrous` text called a round nucleus beside a
  band not own stain, and the reader followed it (cells 6.1 to 6.9 read
  unstained). The user counts round cells next to vessels as well as
  spindle cells (pericytes, myofibroblasts): rewritten so shape does not
  decide, the band hugging the cell's outline does; the aSMA caveat says
  the same.
- **The reader's own threshold, not changed.** CD31 (cells to 6.2 read
  unstained or uncertain; user 5.97), Ecad (to 6.5; user 6.15, classic's
  reader also high at 6.63), CD45 (to 7.5; user 7.37). No rule moves a
  gate past what the cells were read as; these wait for the reader
  comparison and the non-LSP images.

The step was first written to fire whenever the leader sat on the
lattice's edge. Offline, two synthetic CD4s under a noisy reader then
spent their second look above the lattice while a rival below still
blocked, and closed farther off (F1 0.63 to 0.42, 0.86 to 0.60); it now
fires only once the leader beats its rivals. A split stays a refinement
of the dispute itself and may come first.

Offline (8 synthetic scenes x 5 markers; the three rules on against all
three off): truthful reader 0.9505 F1 and 32 packets either way; a reader
turning 32 % of words, 3 seeds, 0.940 F1 / 26.7 % review / 47.7 packets
against 0.940 / 26.7 % / 47.0; the same reader also calling 40 % of cells
`uncertain` (`noisy:0.32:0.4`), 0.940 / 25.8 % / 47.3 against 0.940 /
26.7 % / 46.0. The one marker that changed is a heterogeneous CD8 whose
first look was 18 of 27 uncertain: closed unreadable before, resolved at
the truth's F1 1.0 now. A step and a split each fired once in the 280
markers: the scenes rarely put a leader on the lattice's edge or a wide
mixed interval beside it, so the live rerun is their test.

### Live run 2, candidates after the fixes (2026-10-21)

gs_20261021T170029_b8c14e: the same image, markers, options and seed as
run 1, VERSION 4, every packet answered by Opus (48 packets). Gaps to the
user's gates:

| marker | classic (run 1) | candidates run 1 | candidates run 2 | run 2 looks |
|---|---|---|---|---|
| CD45 | -0.12 | +0.24 | +0.08 | survey, refine, refine: resolved |
| CD31 | -0.01 | +0.48 | +0.05 | survey, refine, refine |
| Ecad | +0.48 | +0.81 | +0.40 | survey, refine, refine |
| SMA | +0.03 | +1.14 | +0.19 | survey, refine, refine |
| Ki67 | +0.06 | +0.09 | +0.09 | survey, refine: resolved |
| PDL1 | -0.33 | -0.30 | -0.19 | survey, extend, tail: no boundary |
| SOX10 | +0.00 | -0.08 | +0.08 | deterministic (T1) |
| MART1 | +0.75 | -0.13 | +0.22 | survey, refine, refine |
| CD3e | -0.30 | -0.30 | -0.30 | survey, refine, refine: resolved |
| CD20L | -0.19 | -0.19 | +0.10 | survey, step (6.92), tail |
| CD4 | +0.34 | +0.27 | +0.63 | survey, refine, refine |
| CD8a | -0.20 | -0.34 | -0.20 | survey, refine, refine |
| CD11c | +0.06 | +0.22 | +0.17 | survey, extend: resolved |
| CD68 | -0.51 | -0.21 | +0.04 | survey, step (6.38), step (6.63): resolved |
| CD163 | -0.04 | +0.38 | +0.38 | survey, refine, refine |
| FOXP3 | -0.13 | -0.07 | -0.17 | survey, refine, refine: resolved |
| PD1 | -0.44 | +0.01 | -0.12 | three looks: unreadable |
| **MAE** | **0.235** | 0.309 | **0.201** | |
| within 0.2 | 10 | 6 | 12 | |
| 0.4 or more off | 4 | 3 | 2 | |
| review | 6 | 13 | 11 | |

Ahead of classic on this run, by less than one run can separate from
reader noise (Opus words agreed 0.78 run to run on the classic looks):
replicates on frozen code come before any claim. The steps did what they
were for (CD68 -0.21 to +0.04); no split fired; no marker closed
unreadable before its third look. Two misses the records explain:

- **CD163** (+0.38): the interval 6.87 to 7.69 read 4 stained of 21, under
  the split's 0.2 share, so it was not cut -- but all four stained reads
  lie at 7.26 to 7.55 and the unstained mostly below 7.2: the edge is
  inside the interval, and the share hides it because the dim end holds
  most of its cells. A trigger on the reads' order by value within the
  interval would have split it (built next, VERSION 5).
- **CD4** (+0.63): the reader called the cells at 6.5 to 7.0 unstained
  although the panel named the lineage (helper T cells; monocytes and
  macrophages, weaker). Its partner panel showed CD3e as the expected
  partner, so a dim CD4 myeloid cell -- CD3e-negative -- looks like spill
  beside it. A multi-lineage marker's expected partner could be the
  lineages' common one (CD45) instead (proposed, not built: CD4's
  partners also feed the classic engine's looks and the hierarchy).

### The ordered split (VERSION 5, 2026-10-21)

An interval is also split when its stained reads lie above its unstained
ones (`controller.ordered`: at least two of each, a one-sided Mann-Whitney
test at 0.05): a reader's slips fall anywhere in an interval, an edge
inside it puts the stained reads at its top. On run 2's CD163 the order
was plain only after the third and last look (p 0.040; after two looks one
stained read), so a trigger inside the loop could not have fired: a marker
out of looks may now make the cut on the reads in hand, with no look, and
close for review at the leader among the finer points
(`split_at_close`).

Replayed on the stored reads of runs 1 and 2 (counts at the new points
interpolated between lattice points; no snapping), the trigger fires
inside the loop on none of the 33 markers before their last look. At the
close:

| marker | ordered reads only | mixed share too |
|---|---|---|
| CD163, run 2 | +0.38 to +0.18 (p 0.040) | the same |
| SMA, run 2 | not split (p 0.089) | +0.19 to +0.43 (8 of 10 stained) |
| CD163, run 1 | not split (p 0.21) | +0.38 to -0.36 (4 of 9) |

Cut finer with no look to come, each part rests on two to six reads, and
the leader among them carries the reader's slips: the close takes ordered
reads only. CD163 is the case the rule was drawn from, so this replay is
not evidence for it; the replicate runs are its first test.

Offline (the 8 scenes, the same seeds and readers as VERSION 4): every
marker's gate and state identical to VERSION 4 (0.9505 truthful; 0.940
under 32 % slips, with or without 40 % uncertain). Of the 37 markers that
ran out of looks none moved at the close, and the order test never fired
inside the loop: a reader whose slips are blind to the value gives it
nothing to find.

## Not done in this branch

- Sampling control (temperature, seed): the gateway chooses the model and
  its parameters, and the gating tasks run with reasoning effort, which
  rules out temperature 0 on the models that think. Variance is measured by
  replicate runs instead.
- Calibrated confidence: built (`hypotheses/calibration.py`, logistic over
  five stored features) but not fitted: the panel path's confidence stays a
  rule tagged `uncalibrated` until the dev campaign's records are fitted
  and checked on held-out.
- What needs runs or the user: the replicate and decision-gate-1
  campaigns (`run_arms.py`), the calibration fit, the classifier trust
  table (the wand bench's per-marker results), and the user's hand gates
  on at least two non-LSP images for the external bar.
