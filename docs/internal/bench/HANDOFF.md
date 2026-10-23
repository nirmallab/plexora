# Candidates gating engine: handoff for a fresh review (2026-10-21)

> Superseded on 2026-10-22 by `docs/internal/GATING_ENGINE_V2.md`. The engine
> is now `v2`, and it is the default. This file is kept as the state of the
> work before VERSION 6 and the dev campaign.

Branch `feature/gating-candidates`, worktree `../plexora-candidates` (off main
c380ffdb, unmerged, locked). The design record and every measurement are in
`docs/internal/bench/CANDIDATES_PLAN.md`, and the engine is described in
`docs/internal/AUTOMATIC_GATING.md` §2f. This file says where things stand and
what I think, so that someone with fresh eyes can check it. Claims are tagged
**[measured]**, **[reasoned]** (argued from measurements, not measured itself) or
**[guess]**.

## In five lines

1. Over three Opus runs each on lsp11385_2, the candidates engine is closer to
   the user's gates than the classic engine on average: MAE 0.237 against
   0.291. Over 17 markers that difference can't be told from noise; the 95 %
   interval is [-0.046, 0.167]. **[measured]**
2. It is 2.6 times less stable run to run: the per-marker spread of the final
   gate averages 0.33 against 0.13, and no marker got the same gate in all
   three runs. It also sends 63 % of markers to review. **[measured]**
3. Its confidence means something and classic's does not. Candidates' accepted
   gates are 0.17 off and its review gates 0.28 off; for classic, accepted are
   0.30 off and review 0.23. **[measured]**
4. Most of the instability is reader noise landing on markers that run out of
   looks. Pooling two runs' reads, roughly what a second reader would add, cuts
   the spread from 0.34 to 0.19. **[measured, as an upper bound]**
5. A second problem stays after pooling: on several markers the reader's own
   threshold differs from the user's (Ecad +0.56, CD3e -0.52, CD4 +0.42). This
   is bias, not noise, and more reads will not remove it. **[measured]**

## 1. What the engine is

**The problem it was built for.** On 12 held-out PCA images the classic engine
ended 0.368 MAE from the user's gates, yet its frozen set of candidate thresholds
(the *lattice*) held a point within 0.2 of the user's gate for 93 % of markers.
The error is in choosing among candidates, not in generating them. Classic asks
the model which way a moving gate should go, and shows each cell's value and
current call in the picture. **[measured, earlier]**

**The user's rules for the redesign** (their two design documents, 2026-10-20):
- candidate gates are competing hypotheses and are never averaged;
- the model labels single cells blind: no value, call, gate or ranking is shown;
- code owns selection, stopping and writes;
- one scalar gate per marker per image;
- the classifier never moves a gate by default;
- confidence must be calibrated, meaning P(|gap| ≤ 0.2);
- looks are bounded and novel;
- rare, continuous and fibrous stains get their own paths.

**How it works** (`plexora/plugins/gating/server/autogate/hypotheses/`;
`SessionOptions.engine = "candidates"`; the default stays `classic`):
- **Candidates and panels.** The lattice's points are the candidates, with
  points nearer than 0.1 merged. Each look is a *cell panel*: 20 cells drawn from
  the intervals between candidates, plus 4 clearly negative *sentinel* cells,
  shuffled and numbered. The reader labels each cell `own_stain`,
  `not_own_stain`, `uncertain` or `artifact`.
- **Choosing.** Code turns the labels into an own-stain share per interval,
  made monotone (non-decreasing) and weighted by the cells each interval holds,
  and picks the *leader*: the candidate whose calls would get the fewest cells
  wrong.
- **Resolving.** The leader is *resolved* when, against every rival more than
  0.3 away, the cells between them have a Wilson 95 % interval wholly on one
  side of one half.
- **Other closes.** A marker may take at most 3 looks. If it runs out, it closes
  for *review* at the leader (`budget_exhausted`). Other closes:
  - `unreadable`: most labels uncertain over two looks;
  - `readers_unreliable`: sentinels read stained twice;
  - `no_identifiable_boundary`: a continuous marker;
  - `positives_not_confirmed`: a rare marker whose cells above the gate were
    not read stained.
- **Growing the range** (`controller.growth`):
  - `extend`: the range moves to the next lattice point when the leader sits at
    its edge;
  - `step`: a point 0.25 beyond the lattice, only once the leader beats its
    rivals;
  - `split`: a wide interval beside the leader is cut finer when its reads are
    mixed (own share 0.2 to 0.8) or ordered (the stained reads above the
    unstained ones; one-sided Mann-Whitney p ≤ 0.05);
  - `split_at_close`: a marker out of looks makes an ordered split on the reads
    it already has.

## 2. How it was measured

- **Image.** lsp11385_2 only: 17 markers, about 202k cells. The user regated it
  on 2026-10-21; their gates are `~/gatevision/lsp11385_2_user_gates_2026-10-21.json`
  (`{image: {marker: gate}}`, table units).
- **What the gates mean.** The user's conventions are in memory and in the
  plan:
  - a marker carried by several cell types is gated to include its dim
    population (CD4 6.58 because myeloid CD4 is dim);
  - SMA counts round perivascular cells as well as spindle cells;
  - a gate sits where false positives start rising steeply (CD163 7.31).
- **LSP images are high-SNR.** Nothing may be tuned on them. Every rule added
  this week was motivated by this one image, so treat the live gains as
  in-sample.
- **Live runs.** Over MCP in propose mode, so nothing was written. Options:
  display check off, classifier off, no answer reuse, `on_limit: extend`.
  Every packet was answered by a `plexora-gating-worker` on Opus
  (claude-opus-5-5) given all gating tasks, relaunched until the session said
  decided.
- **Offline.** `plexora ai bench gating --synthetic …` with a scripted reader
  (`bench.TruthAgent`): `oracle` (truthful), `noisy:P` (turns a word with
  probability P) or `noisy:P:U` (also calls a cell `uncertain` with probability
  U). The synthetic scenes rarely exercise the edge and split rules.

## 3. Findings

### 3a. The live runs, in order

| run (session) | engine version | MAE | within 0.2 | ≥ 0.4 off | review |
|---|---|---|---|---|---|
| classic s1 (gs_20261021T151220_295e7c) | — | 0.235 | 10/17 | 4 | 6 |
| candidates run 1 (gs_20261021T152452_38f60d) | 3 | 0.309 | 6/17 | 3 | 13 |
| candidates run 2 (gs_20261021T170029_b8c14e) | 4 | 0.201 | 12/17 | 2 | 11 |
| classic s2 (gs_20261021T174335_ab0cb8) | — | 0.321 | 7/17 | 7 | 0 |
| classic s3 (gs_20261021T175910_9c1d8d) | — | 0.317 | 7/17 | 6 | 1 |
| candidates s1 (gs_20261021T174334_60d67c) | 5 | 0.195 | 11/17 | 3 | 9 |
| candidates s2 (gs_20261021T175908_231b7a) | 5 | 0.239 | 9/17 | 3 | 11 |
| candidates s3 (gs_20261021T181504_fc6fa9) | 5 | 0.277 | 7/17 | 5 | 12 |

Classic s1 was classic's best run. Comparing candidates run 2 with it alone
(0.201 vs 0.235) overstated the gain. **[measured]**

What changed between versions (all in CANDIDATES_PLAN.md):
- **3 → 4**, in response to run 1:
  - unreadable needs two looks (four markers had closed after one crowded
    panel);
  - `step` (CD68 had resolved on the lattice's top point with nothing above
    read);
  - `split` of mixed intervals (CD163: nothing between 6.70 and 7.69);
  - the fibrous reading text rewritten (it had told the reader a round cell
    beside a band was not stained; SMA +1.14).
- **4 → 5:** the ordered split and the split at close (CD163 run 2: 4 of 21
  read stained, all four at the top of the interval).

### 3b. Replicates, 3 runs per engine (VERSION 5) **[measured]**

| | classic | candidates |
|---|---|---|
| MAE, mean of runs (sd) | 0.291 (0.049) | 0.237 (0.041) |
| classic − candidates, bootstrap over markers | | 0.054 [-0.046, 0.167] |
| markers where candidates is nearer | | 8 of 17 |
| ≥ 0.4 off | 17/51 | 11/51 |
| per-marker spread over runs, mean (median) | 0.126 (0.07) | 0.334 (0.25) |
| same gate in all three runs | 6 markers | 0 |
| closed for review | 7/51 | 32/51 |
| MAE accepted / review | 0.301 / 0.227 | 0.166 / 0.279 |
| packets per run; images per run | 44; 80 | 46; 46 |

- **Classic's misses are stable:** MART1 +0.75 to +0.89, CD68 -0.51 three times,
  Ecad +0.48 three times, PD1 about -0.45. Its deterministic start decides most
  markers and its looks seldom move the gate, so it is stable even where it is
  wrong. **[measured; the cause is reasoned]**
- **Candidates' misses move around:** CD163 -0.12 / +0.38 / +0.63, PD1 +0.05 /
  -0.45 / +0.20, CD68 -0.21 / +0.28 / +0.03.
- **Decision gate 1**, pre-registered, on this image only (the PCA dev images
  were not run):
  - MAE not worse: met;
  - share ≥ 0.4 off lower: met;
  - spread not larger: **failed**;
  - ≤ 2 packets per marker: **failed** (2.7; classic 2.6);
  - no empty gate: met.

### 3c. Pooling reads across runs **[measured; an upper bound]**

`tools/bench_gates/pool_reads.py` keeps each run's leader, then recomputes the
leader on the reads of two runs pooled, and of all three. Each run read
different cells with a fresh reader, so a pooled pair is what a second reader
would add. Here the second reader comes with a full three looks, where a real
one would cost about one, so this is an upper bound.

| | single runs | pairs pooled | all three pooled |
|---|---|---|---|
| mean spread (16 markers; SOX10 left out) | 0.344 | 0.194 | — |
| MAE | 0.247 | 0.221 | 0.215 |

- **Where pooling settles the gate:** with any two runs pooled, CD4, CD45, Ecad,
  Ki67 and SMA land on one gate (pair spread 0.00).
- **Where it doesn't:** PD1 (0.61), PDL1 (0.49), CD163 (0.50) and CD8a (0.39)
  still move. These markers are mostly read `uncertain`, or genuinely
  ambiguous.
- **What pooling converges to** (all three runs pooled), by distance from the
  user's gate: Ecad +0.56, CD3e -0.52, CD4 +0.42, PD1 -0.28, CD68 +0.28, CD45
  +0.24, CD11c +0.23, SMA +0.19, PDL1 +0.19. That is the bias in point 5 above.

### 3d. Offline checks of the rules added this week **[measured]**

The bench used 8 synthetic scenes × 5 markers; a 32 % slip reader on 3 seeds;
the same with 40 % uncertain on 3 seeds; and the truthful reader. The arms were
VERSION 3 rules, VERSION 4 and VERSION 5.
- **No change in F1** (0.940 under noise, 0.9505 truthful). One marker improved:
  a heterogeneous CD8 resolved where it had closed unreadable.
- **The step's first version regressed.** It stepped as soon as the leader sat
  on the lattice's edge, and two synthetic CD4s got worse (F1 0.63 → 0.42 and
  0.86 → 0.60): the step spent the look a dispute below needed. It now waits
  for the leader to beat its rivals. `tools/bench_gates/trace_scene.py` shows a
  scene's looks per marker.
- **Some rules barely fire offline.** A step and a split each fired once in
  the 280 markers of each version's runs. The order test never fired on the simulated reader's random slips,
  and the split at close never moved a gate.
- **Replay of the split at close on live reads**
  (`tools/bench_gates/replay_close.py`):
  - with a mixed share allowed at the close: SMA +0.19 → +0.43 and a run-1
    CD163 +0.38 → -0.36. Cut finer with no look to come, each part rests on two
    to six reads;
  - ordered reads only: just run 2's CD163 moved, +0.38 → +0.18. That is the
    case the rule was drawn from, so it is not evidence for the rule.

## 4. Problems, ranked

**P1. A review marker's gate lands wherever that run's reads put it.**
**[measured]** 63 % of markers run out of looks and close at the leader of that
run's reads. Nothing protects that placement, which is where the 0.33 spread
comes from. The design protects only the decision to accept: the sentinels and
the Wilson test. Missing:
- no model of the reader: `uncertain` is dropped, and neither a slip rate nor
  how lenient the reader is per marker is estimated;
- no second reader;
- no sampling control: the gateway owns temperature, and the models think.

**P2. The reader's threshold is not the user's. [measured]** This is bias, and
pooling does not shrink it. Ecad and CD3e are off the same way in both engines
(Ecad: classic +0.48, candidates +0.56; CD3e: -0.30 to -0.39 and -0.41 to
-0.52), so it is the reader against the user, not the controller.
- **[reasoned]** The engine places the gate at the reader's 50 % own-stain
  crossing. The user gates "where false positives start rising steeply". On
  CD163 the reads in 7.07–7.49 were 17 to 33 % own-stained, and the user's 7.31
  sits there, below the reader's crossing (about 7.5).
- **[reasoned]** An earlier note in memory says the 50 % crossing matches the
  user's convention. The CD163 and SMA reads now argue otherwise; I would not
  trust that note.

**P3. CD4 is pushed away from the user's convention by the panel's own design.
[measured, then reasoned]** The panel shows CD3e as CD4's expected partner. A
dim CD4 myeloid cell has no CD3e, so beside that panel it looks like spill. The
lineage line ("helper T cells; also monocytes and macrophages (weaker)") was in
the packet and did not overcome it.

**P4. Crowded immune fields are mostly unreadable. [measured]** About 40 % of
panel cells are labelled `uncertain`: CD31 34/60, PD1 32/60, CD3e 30/60 and
CD20L 30/60 in run 2. PD1 and PDL1 close `unreadable`, or vary run to run.

**P5. Cost and review burden. [measured]** 2.7 packets per marker against a bar
of 2. A 63 % review rate means the user would check most markers by hand. That
is honest, but it may not be a usable product as it stands.

**P6. The evidence base is thin. [measured]**
- One high-SNR image, so the PCA dev images that decision gate 1 names were not
  run.
- No non-LSP image with the user's hand gates exists yet for the external bar.
- Everything added this week was motivated by this image (step, split, ordered
  split, unreadable-after-two, the reading texts).

## 5. What I think would solve them

**P1, the spread: pool more reads where they decide the gate.** This is my
recommendation; **[reasoned]**, with 3c as its evidence.
- When a marker would close for review, put the cells between its leader and
  the runner-up to a fresh second reader, and pool those reads into the same
  evidence. This keeps to the user's "never averaged" rule: reads are pooled,
  gates are not averaged.
- Cost: about +1 panel per review marker. 3c suggests the spread could roughly
  halve.
- Test offline first: a bench reader with run-level variance (each "reader" its
  own leniency, not only independent slips) and a variant that adds the second
  reader. The current noisy reader slips independently per cell, so it cannot
  show this problem at all.
- Cheaper alternatives to compare against: a 4th look only for markers whose
  leader is unresolved, or bigger panels (24 cells).
- Also worth checking: whether `uncertain` should count as weak evidence
  rather than nothing.

**P2, the bias: needs the user's labels, not more rules. [reasoned]**
- Option (a): a per-marker or per-class offset between the reader's crossing
  and the user's gate. It cannot be fitted on LSP, so it needs non-LSP hand
  gates.
- Option (b): place the gate at a lower own-stain share than 50 %, closer to
  "where false positives start". That is one parameter, but still a choice the
  user must confirm against their labelled cells.
- I would not touch P2 before P1: with a 0.33 spread, a bias of 0.2 is hard to
  see.

**P3, CD4's partner: for a marker carried by several cell types, show the
partner they share (CD45) as expected. [reasoned]**
- Side effects: partners also feed classic's multi-view looks and the marker
  hierarchy.
- The user must confirm the biology first.

**P4, unreadable crowded fields: open. [guess]** Possibilities:
- a wider crop or the 2×2 multi-view block on immune markers;
- a reading rule for crowded cells (one partial-ring rule was added this week,
  and its effect is unmeasured);
- accept that the reader cannot gate PD1 or PDL1 in dense aggregates, and close
  them `unreadable` at the deterministic start.

**P5 and P6** follow from P1 and from running more images.

## 6. Immediate next steps

1. **Review this document with fresh eyes**, especially the questions in §7.
   Don't take my framing of P1 and P2 as given.
2. **Decide how to handle P1.** Build the run-level-variance reader into
   `bench.TruthAgent`, prototype the second reader offline, and replay it on the
   stored live sessions using `pool_reads.py` as a template. Zero model cost.
3. **Run the PCA dev images on O2** for decision gate 1 (noisier images). Use
   `tools/bench_gates/run_arms.py` with 3 runs per arm on one model.
4. **Get the user's hand gates on at least two non-LSP images**; the
   external bar needs them.
5. **Ask the user** about CD45 as the expected partner of multi-lineage markers
   (P3).
6. **Housekeeping.** The local `plexora` MCP server still points at this
   worktree. Restore it with:
   `claude mcp remove plexora -s local` then
   `claude mcp add-json plexora '{"type":"stdio","command":"<plexora env python>","args":["-P","-m","plexora","mcp","serve"],"env":{"PYTHONPATH":"<biocognia>/plexora-split:<biocognia>/spatialagents/src"}}' -s local`.
   The user then reconnects with `/mcp`. The server reads code once at start, so
   any engine change also needs a reconnect.

## 7. Questions where I most want fresh eyes

- **Is "spread not larger than classic" the right bar?** Classic is stable
  partly because it is stuck at its start, and its large misses repeat
  identically. Expected error, or error plus spread, might be the fairer test.
  Note the 6 classic markers with identical gates include Ecad +0.48 and CD68
  -0.51.
- **Is the 50 % own-stain crossing the right target at all?** (P2)
- **Is a 63 % review rate acceptable** if review gates really are the bad
  ones (0.28 vs 0.17)?
- **Are this week's rules overfitted to lsp11385_2?** They are neutral offline
  only because the synthetic scenes barely trigger them.
- **Is the pooled-runs experiment (3c) a fair proxy for a second reader?** The
  pooled reads cover more intervals than one extra panel would.
- **Should `uncertain` stay neutral?** About 40 % of all reads are discarded
  as it stands.

## 8. Reproducing

- **Session records** (this Mac):
  `~/Library/Application Support/plexora/.agent/sessions/gating/<session id>/session.json`.
  Each candidates unit's `hypotheses` holds the points, window, observations
  (cell, value, label), status and `split_at_close`.
- **Scoring:** `tools/bench_gates/score_gap.py --truth <gates.json> NAME=GLOB …`,
  or the ad-hoc scoring in this file's tables (|final − user| per marker).
- **Pooling:** `PYTHONPATH=. python tools/bench_gates/pool_reads.py <gates.json> lsp11385_2 <s1> <s2> <s3>`.
- **Split at close:** `PYTHONPATH=. python tools/bench_gates/replay_close.py <gates.json> lsp11385_2 <session.json …>`.
- **Offline ablation:** `tools/bench_gates/ablate.py`. Its docstring gives the
  command and the `ABL` switches. Use a scratch `PLEXORA_DATA_PATH`; the bench
  makes its own temporary root.
- **One scene's looks:** `tools/bench_gates/trace_scene.py`.
- **Tests:** `tests/test_gating_hypotheses.py` (42 tests), `test_autogate_units.py`,
  `test_gating_panel_replay.py`, `test_ai_skills.py`. Run with
  `PYTHONPATH=<worktree>:../spatialagents/src`.
- **Constraints:**
  - live runs in propose mode only;
  - benches on scratch data roots, never the live install;
  - commit on the feature branch only;
  - never tune on LSP;
  - the user's labelled cell sets live under the gitignored
    `docs/internal/bench/runs/`.
