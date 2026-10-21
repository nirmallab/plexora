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

## Not done in this branch

- Sampling control (temperature, seed): the gateway chooses the model and
  its parameters, and the gating tasks run with reasoning effort, which
  rules out temperature 0 on the models that think. Variance is measured by
  replicate runs instead.
- Calibrated confidence: the panel path's confidence is a rule (`hypotheses.
  confidence`) tagged `uncalibrated` in provenance until it is fitted on the
  dev records (isotonic, at most five features) and checked on held-out.
- Cross-marker revisits and the classifier as a discrepancy detector: after
  decision gate 1, each behind its own option.
