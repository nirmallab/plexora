# Spatial evidence in automatic gating: ablation (2026-10-08)

The question: does gating's spatial evidence layer (`SessionOptions.spatial`,
`autogate/spatial.py`, design in `docs/internal/AUTOMATIC_GATING.md` §2a) make
gates better on real tissue without costing the clean markers, and should the
switch default to `decide`?

**Answer: no, not yet. The default stays `off`.**

- **Off is today's session.** On the synthetic suite, all 133 marker×arm gates
  are identical to the base commit, at the same 28 packets.
- **The first real image showed a design flaw, and it is fixed.** The raw
  spatial numbers read clustered biology as artefact. On lsp11385_2 they rated
  9 of 12 human-gated markers at moderate or high risk. Every rule now reads an
  *excess*: the share of the cells at the gate in an environment, minus the
  same share among clear positives (`SPATIAL_VERSION` 2). The uncertain layer
  had the same flaw, and it is fixed the same way (`UNCERTAIN_VERSION` 2).
- **The decision machinery never engaged on lsp11385_2.** After the fix, two
  `decide` runs produced no like-for-like look, no case, no hold and no flag.
  The run fully on the final code left no uncertain cells. On this image
  `decide` behaves like `stratify`.
- **Stratified sampling moved two markers toward the human gates, in all four
  spatial-variant runs.** CD8a moved −0.28 → +0.02 and CD68 −0.43 → −0.16.
  The effect runs through the partner cascade: CD3e was accepted at high
  confidence in 3 of 4 spatial-variant runs and in 0 of 2 baselines, and it
  then led CD8a's scored start. MAE fell from 0.155 to 0.136 (stratified) and
  0.143 (decide), against a bar of 0.12. CD163 lost 0.06 in every run.
- **The plan's bar does not hold** (table below), so the default stays `off`.

Raw data, kept locally and gitignored like every campaign's:
`docs/internal/bench/runs/2026-10-08-spatial/`.

## Code

| commit | what |
|---|---|
| `6f21e34e` | base: the in-flight gating and QC work, committed as is |
| `c9410bc9` | spatial evidence v1 (`SPATIAL_VERSION` 1) |
| `829764c5` | rules read excesses over clear positives (`SPATIAL_VERSION` 2) |
| `984adf82` | the uncertain layer gated on the marker's excess (`UNCERTAIN_VERSION` 2) |

## 1. Off is today's session

`plexora ai bench gating --synthetic all --on-limit extend` on `6f21e34e` and
on the branch (`--variants baseline`): 133 of 133 marker×arm gates are the
same, with 28 packets each (`synthetic/off_*`). The memo, payload and
provenance identity of `off` is pinned by
`tests/test_gating_spatial_session.py::test_spatial_off_is_todays_session`.

## 2. What the first real image showed

The first `decide` session on lsp11385_2, on v1, read the summaries below at
each marker's Auto gate (`live/probe_real.jsonl`). The synthetic spill scene
is shown for contrast (`live/probe_synth.jsonl`).

| | raw spill share at the gate | spill excess | low/high-SNR positive-fraction ratio | SNR excess | density excess |
|---|---|---|---|---|---|
| lsp11385_2, 12 markers | 0.12–0.56 (11 of 12 ≥ 0.42) | −0.39 to +0.003 | 3.5–7.7 (all 12) | −0.14 to +0.22 | −0.19 to +0.24 |
| synthetic spill scene, CD3 | 0.57 | **+0.43** | 1.65 | +0.08 | +0.16 |
| synthetic clean markers | 0.03–0.09 | ≤ 0.05 | – | 0 | ≤ 0.15 |

The v1 rules read the left column of each pair:
- the contrast trigger at a spill share ≥ 0.25;
- risk and the T1 audit at a positive-fraction ratio of 1.5–3×.

On real tissue both are what dense tissue looks like. Positives crowd together,
so half the cells at any gate touch a strong positive. The coarse background
map is also raised by the positive cells' own stain where they crowd. v1 rated
9 of 12 markers at moderate or high risk and demoted SOX10's T1 acceptance.

**The fix (v2):** compare the cells at the gate with clear positives, as
`spill_excess` already did. If positives cluster by biology, a true positive
at the gate is as likely as a clear one to be beside one, so only a surplus
points at an artefact. `spatial.excesses()` supplies `spill`, `density` and
`snr` to:
- risk;
- the T1 audit;
- the contrast trigger;
- the case table;
- the uncertain layer.

In the case table, `snr_excess` replaces the SNR ratio and `spill_excess` the
raw spill share.

**The uncertain layer (v1):** every positive near the gate that touched a strong
positive or sat in a low-SNR tile. On lsp11385_2 that was 1,000–9,000 cells a
marker. Against the human's per-cell calls, the human-negative share among
them was no different from the share among all positives at the gate (CD31:
0.566 vs 0.568). **v2** flags a cell only where the marker's own excess says
proximity matters.

## 3. Synthetic ablation

`plexora ai bench gating --synthetic spatial --variants baseline,stratified,spatial`
(`synthetic/`). Code agreement was 0.925 for every arm and every version.

**Decide arm:**

| version | agent | packets (baseline arm) | contaminated-cell FP | clean FP | uncertain precision | uncertain recall |
|---|---|---|---|---|---|---|
| v1 | oracle | 30 (24) | 0.020 | 0.012 | 0.51 | 0.51 |
| v2 | oracle | 25 (24) | 0.020 | 0.012 | 0.44 | 0.51 |
| v2 + uncertain v2 | oracle | 25 (24) | 0.020 | 0.012 | **1.00** | 0.19 |
| v1 | noisy:0.2 | 30 (26) | 0.045 | 0.012 | 0.44 | 0.49 |
| v2 | noisy:0.2 | 27 (26) | 0.020 | 0.013 | 0.36 | 0.51 |
| v2 + uncertain v2 | noisy:0.2 | 27 (26) | 0.020 | 0.013 | **1.00** | 0.19 |

The baseline arm's contaminated-cell FP rate is 0.020 (oracle) and 0.018
(noisy).

**v1's one regression** was heterogeneous CD3 under the noisy agent, which went
from F1 1.000 to 0.917. The T1 audit demoted a correct T1 acceptance on "high
risk", and a wrong word in the extra look moved the gate. v2 keeps it at
1.000.

**No variant reaches a spatial case on these scenes** (`case_correct` 0). With
the gate already near the truth, the first look says the gate is about right,
so no like-for-like look is asked. The scenes paint artefacts but no gate
error large enough for the spatial rules to correct. A scene built to do that
is the next bench step.

**The low-SNR scene is read as unreliable, not as low SNR.** Its background
bump makes over half of its tiles' cells positive at the gate, so those tiles
fall under the safeguard and their SNR is unknown.

**Stability** (`--stability 3`, noisy:0.2): with `decide`, 33 of 35 markers
reach one gate in every run and 33 of 35 replays are identical. With the
switch off, the figures are 31 of 35 and 32 of 35. The two CD4 markers that
fail replay in both arms show the existing first-rerun memo quirk: trimmed
sheets re-ask a look on the first rerun. That quirk is present on the base
commit too.

## 4. Live runs on lsp11385_2

**Setup:**
- 12 human-gated markers: CD11c, CD163, CD31, CD3e, CD4, CD45, CD68, CD8a,
  FOXP3, MART1, SMA, SOX10.
- Session options: `mode: propose`, `qc: strict`, `reuse_answers: false`,
  `on_limit: extend`, a distinct agent label per run, `delegate: false`.
- Sessions were driven through `plexora.agent.invoke`, the same capabilities
  as MCP. Packets were answered by Sonnet subagent workers, 6 markers per
  worker (4 for baseline 1), each with the same brief.
- Two fresh runs per variant. The runs used in this report ran on `984adf82`,
  except as noted:
  - baseline 1 ran its whole session before any spatial code changed, which
    does not matter with the switch off;
  - spatial 1 switched to the v2 uncertain rule after its first 8 markers.
    Uncertain counts never touch a gate, and they are recomputed at scoring.
- Truth was the human gates
  (`--truth csv:lsp11385_2_gated_channel_ranges.csv --truth-column "Human gate"`)
  and the per-cell `gatediff_` columns
  (`--cell-truth column-prefix:gatediff_`).
- Provenance and display calibration were snapshotted before the runs and
  restored after. Gate values are never written in propose mode.

**Final gate per run (miss against the human gate):**

| marker | human | baseline 1 | baseline 2 | stratified 1 | stratified 2 | decide 1 | decide 2 |
|---|---|---|---|---|---|---|---|
| CD11c | 6.10 | 6.16 (+0.06) | 6.03 (−0.07) | 6.16 (+0.06) | 6.11 (+0.01) | 6.11 (+0.01) | 6.16 (+0.06) |
| CD163 | 6.50 | 6.48 (−0.02) | 6.48 (−0.02) | 6.42 (−0.08) | 6.42 (−0.08) | 6.42 (−0.08) | 6.42 (−0.08) |
| CD31 | 6.00 | 5.92 (−0.08) | 5.92 (−0.08) | 5.92 (−0.08) | 5.92 (−0.08) | 5.92 (−0.08) | 5.92 (−0.08) |
| CD3e | 6.60 | 6.66 (+0.06) | 6.66 (+0.06) | 6.66 (+0.06) | 6.66 (+0.06) | 6.62 (+0.02) | 6.66 (+0.06) |
| CD4 | 6.60 | 6.82 (+0.22) | 6.82 (+0.22) | 7.09 (+0.49) | 6.90 (+0.30) | 6.82 (+0.22) | 6.82 (+0.22) |
| CD45 | 7.20 | 7.30 (+0.10) | 7.30 (+0.10) | 7.30 (+0.10) | 7.30 (+0.10) | 7.30 (+0.10) | 7.30 (+0.10) |
| CD68 | 6.00 | 5.57 (−0.43) | 5.57 (−0.43) | 5.84 (−0.16) | 5.84 (−0.16) | 6.01 (+0.01) | 5.84 (−0.16) |
| CD8a | 6.10 | 5.82 (−0.28) | 5.82 (−0.28) | 6.12 (+0.02) | 6.12 (+0.02) | 6.22 (+0.12) | 6.12 (+0.02) |
| FOXP3 | 6.00 | 5.80 (−0.20) | 5.83 (−0.17) | 5.79 (−0.21) | 5.79 (−0.21) | 5.66 (−0.34) | 5.72 (−0.28) |
| MART1 | 5.80 | 5.96 (+0.16) | 6.03 (+0.23) | 6.03 (+0.23) | 5.96 (+0.16) | 6.10 (+0.30) | 6.10 (+0.30) |
| SMA | 6.00 | 5.92 (−0.08) | 5.92 (−0.08) | 5.92 (−0.08) | 5.79 (−0.21) | 5.70 (−0.30) | 5.79 (−0.21) |
| SOX10 | 6.40 | 6.54 (+0.14) | 6.54 (+0.14) | 6.54 (+0.14) | 6.54 (+0.14) | 6.54 (+0.14) | 6.54 (+0.14) |
| **MAE** | | 0.152 | 0.157 | 0.143 | 0.128 | 0.143 | 0.143 |
| **within 0.1** | | 6 | 6 | 7 | 6 | 6 | 6 |
| **packets** | | 32 | 34 | 41 | 39 | 35 | 36 |
| **worker tokens** | | 260k | 203k | 230k | 229k | 214k | 221k |

**Run-to-run drift.** The two baselines differ on CD11c (−0.13, 4 lattice
steps), FOXP3 (+0.03) and MART1 (+0.07). Everything else is identical.

**Paired effects.** In all 4 spatial-variant runs and neither baseline:
- CD8a moved +0.30 (7–11 lattice steps, toward the human gate);
- CD68 moved +0.27 (toward it);
- CD163 moved −0.06 (away from it).

SMA moved −0.13 to −0.22, away from the human gate, in 3 of 4.

**Where the CD8a and CD68 shifts come from.** A unit's scored start is rescored
from partners already decided in the same session, so one look's answer on a
partner reshapes every marker after it. With stratified rows, the model
accepted CD3e at high confidence in 3 of 4 spatial-variant runs and in 0 of 2
baselines. CD8a's start then follows CD3e (6.12–6.13) instead of CD45 (5.83).

In decide run 1, CD45 closed for manual review: its first look could not
settle the gate. CD68 then lost CD45 as its reference and started from its
own distribution (6.02 instead of 5.49). Two runs per arm cannot separate how
much of that is the variant and how much is noise.

**What `decide` did beyond `stratify`:** nothing.
- No like-for-like look (`t3_contrast`), no case, no hold and no spatial flag
  in either decide run.
- No T1 demotion after v2.
- Decide run 2, fully on the final code, left 0 uncertain cells on every
  marker.

On lsp11385_2 no marker shows an excess of spill or background at the gate.
That fits the image: its errors against the human gates are threshold
placement, not attribution.

**The per-cell truth is the human threshold.** The `gatediff_` columns are
derived from the human gate, so per-cell metrics against them (dense and
sparse FP rates, spill-suspect shares, uncertain precision) measure threshold
disagreement, not contamination. Real data has no attribution truth here.

**Replay:** decide run 1's 27 collages redraw byte-identically from their
manifests (`live/replay_spatial-1.json`).

### The plan's bar

| criterion | target | stratified | decide | holds |
|---|---|---|---|---|
| MAE against the human gates | ≤ 0.12 | 0.143, 0.128 | 0.143, 0.143 | no (baseline 0.152, 0.157) |
| within 0.1 | ≥ 6/12 | 7, 6 | 6, 6 | yes |
| CD163 / CD4 dense-region FP down, recall −0.02 at most | | CD163 0.014 → 0.021; CD4 recall 0.889 → 0.756 / 0.849 | CD163 0.014 → 0.021; CD4 unchanged | no |
| clean markers within one lattice step of baseline | | CD8a 7 steps (toward the human gate) | CD8a 7–11 steps | no (as worded) |
| feature seconds per marker | ≤ 3 s | ~0.3 s + map | ~0.3 s + map | yes |
| packets | ≤ baseline + 1 per hard marker | +7 a run | +2.5 a run | stratified no, decide yes |

The bar was set for CD163 −0.19 and CD4 +0.27, the misses of earlier runs.
In these baselines CD163 is −0.02, and CD4's +0.22 is above the human gate,
so its dense-region false positives are already 0. That leaves the
false-positive criterion little to measure on this image.

## 5. Cost of the feature layer

`plexora ai bench spatial --sizes 200000,1000000 --targeted 50,200`
(`cost/`). The machine was otherwise idle (M2 Pro, numba). Clustered synthetic
tables, 4 markers. The profile is warmed outside the timing and the trace,
because every session pays for it whatever the switch.

| cells | graph s | per marker s (median / max) | peak MB | bytes/cell |
|---|---|---|---|---|
| 200,000 | 0.49 | 0.32 / 0.45 | 61.8 | 324 |
| 1,000,000 | 1.28 | 1.47 / 1.50 | 308.0 | 323 |

**Memory:** the peak holds the shared neighbour graph, the density, the
per-cell features of the last two markers (`per_cell_cache_markers` was 4,
then 2) and the transients. It was 487 MB a million cells with a 4-marker
cache, and 308 MB now. The target was 300.

**Targeted pass on 200 cells** (2048 px scene):
- mode A: 0.05 s;
- mode B: 0.03 s.

That is on top of the crop reads the collage already makes: 284 reads for
mode A and 142 for mode B.

## 6. Decision and next steps

**`LIMIT_DEFAULTS["spatial"]` stays `off`.** `stratify` is the variant with a
measurable effect on real tissue: it gave a better MAE in both runs, at +6–9
packets a session. That effect runs through partner confidence, not through
any spatial rule. Before a default changes, it needs:
- more runs;
- a second image;
- a check that CD163 and SMA do not pay for it.

**Before `decide` can be judged:**
- **A real attribution truth.** That means an expert's per-cell labels
  (positive, negative, weak positive, contaminated, background) on cells
  sampled across the strata. It is not built yet: `--cell-truth` reads only
  `column-prefix:` today, and the plan's `csv:` source and blinded candidate
  galleries are still to come.
- **A synthetic scene where spill or background moves the gate.** The current
  scenes leave the gate right and reach no case.
- **A background estimate the positives' own stain does not raise.** For
  example, a tile's negative cells' table values instead of its dimmer
  pixels. The coarse pixel map is the weakest part of the layer.
