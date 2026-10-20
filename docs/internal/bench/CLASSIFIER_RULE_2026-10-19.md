# Gate plus classifier: which margins, 2026-10-19

A gating session places a gate per marker. A per-cell classifier is then
trained at that gate and scores every cell. The rule `gate_unless_confident`
(`plugins/gating/server/classifier/rules.py`) keeps the gate's call unless the
classifier disagrees confidently: a positive above the gate becomes negative
when its score is below `c_clean` ("cleaning"); a negative below it becomes
positive when its score is above `1 - c_add` ("adding"). The user's prior is
that on low signal-to-noise images the classifier over-calls, so cleaning
should be more trustworthy than adding.

Questions: which (`c_add`, `c_clean`, agreement filter on/off) improves
agreement with the user's own per-cell labels without losing specificity, and
below which signal level must adding be switched off?

## Yardstick

The user's own labelled cells, as in `CLASSIFIER_WAND_2026-10-19.md`: "own" is
positive; "glow" and "none" are negative; "unsure" and inconsistent repeats
are dropped. Never used as picks. 895 cells, 36 markers:

| set | image | cells | markers |
|---|---|---|---|
| near | lsp11385_2 (CyCIF) | 281 | 12 |
| near | imc-immucan-example (IMC) | 267 | 12 |
| near | codex-hubmap-lymphnode (CODEX) | 274 | 12 |
| far | lsp11385_2 | 73 | (same 12) |

Each marker has 19-31 labelled cells. A per-image number rests on about 250
cells, and a per-marker number on about 25.

## Method

Gate arms:

- `human`: the gate the labelled cells were called at. Derived from
  `table_call` as the midpoint between the highest `neg` and the lowest `pos`
  value; where the calls overlap, the value that misplaces fewest. The marker's
  stored gate (`model.get_gate`) is used instead when it is thresholded and
  agrees with the table calls at 95% or more. The table calls were separable
  on every marker (no overlap). Only lsp11385_2 has stored gates: four
  (CD11c, CD163, CD31, CD8a; agreement 96.4-96.8%) were consistent and used;
  the other eight stored gates agree with the table calls at 58-90% (they were
  moved after the labels were made), so those use the derived midpoint. IMC
  and CODEX have no stored gates, so all 24 of their gates are derived.
- `gmm`: `model.gmm_for(ds, marker)["gate"]`, Thresholding's Auto.

Per marker, gate arm and seed (0, 1, 2): `feature_matrix`; `signal.band(ds,
marker, G, 2.0)`; `autopick.pick(..., exclude=<all labelled cells>,
gate=G, exclude_range=<band>)`; `learner.fit` on the picks at weight 0.5
(`runs.AUTO_WEIGHT`, `n_given=0`); score every eligible cell (finite value,
QC-passed per `runs.qc_state`). Arms: `gate`; `raw` (`score_0.5`); and
`gate_unless_confident` over c_add in {None, .02, .05, .1} x c_clean in {None,
.05, .1, .2, .3}, each with and without `min_agreement=0.8`. The same
`rules.apply` ships, so what is measured is what runs. All numbers are means
over the three seeds; "sd" is the sd over seeds of the pooled balanced accuracy.

Scripts: `tools/bench_classifier_wand/bench_rule.py`, `score_rule.py`.
Raw outputs: scratchpad `rulebench/out/` (`*.json` per image, `score_human.txt`,
`score_gmm.txt`, `score_human_ext.txt`, `extra.txt`). The seeds are the only
randomness; picks and fits did not refuse on any marker.

Precision of a direction is over labelled cells only: of the cells the rule
added, the share labelled own; of the cells it removed, the share labelled
glow/none. Counts are seed means.

## Pre-registered decision (written before the results were read)

Choose (c_add, c_clean, agreement on/off) on lsp11385_2 near + IMC with
`human` gates; confirm on CODEX LN (held out), the lsp far set, and the `gmm`
gates.

- Ship `gate_unless_confident` with both margins only if: pooled balanced
  accuracy >= gate + 3 points; no image worse than -2 points; far set not worse
  than the gate; cleaning precision >= add precision on every image.
- Otherwise ship with `c_add = None` (cleaning only), provided cleaning alone
  meets: pooled BA >= gate + 1, no image worse than -2, far not worse.
- If neither passes, the layer ships as a non-deciding report.
- SNR bars: the d and pixel-contrast values below which add precision falls
  under 0.5 (-> `SNR_LOW`), and below which even clean precision falls under 0.5
  (-> `SNR_NONE`). State how many markers each tercile holds and whether the bar
  is supported or a guess.

The grid is the registered one (c_add up to 0.1, c_clean up to 0.3). Anything
beyond it below is labelled post hoc.

## Results

### Human gates, registered grid

Balanced accuracy (agreement in brackets); the chosen cleaning-only rule is
c_clean 0.3, no adding.

| | gate | raw classifier | cleaning only, c_clean 0.3 | both, c_add 0.1 + c_clean 0.3 |
|---|---|---|---|---|
| pooled, all 895 | .604 (.594) | .680 (.649) | .624 (.620) | .624 (.620) |
| pooled near, 822 | .586 (.580) | .672 (.640) | .608 (.608) | .608 |
| far, 73 | .747 (.753) | .747 (.753) | .747 (.753) | .747 |
| lsp11385_2 near | .597 (.591) | .617 (.582) | .612 (.609) | .612 |
| IMC near | .557 (.558) | .689 (.679) | .579 (.583) | .579 |
| CODEX LN near | .613 (.591) | .713 (.661) | .641 (.633) | .641 |

Seed spread of the pooled BA is 0.001-0.006 for the rules and 0.006 for raw.

Selection set (lsp near + IMC, 548 cells): gate .577; c_clean .2 .583; c_clean
.3 .595; both margins (.1, .3) .596. Confirmation (CODEX + far, 347 cells):
gate .659; c_clean .3 .682; raw .727.

The agreement filter (`min_agreement=0.8`) changed no number in any row: every
override the rule makes already has agreement above 0.8.

The grid's c_add values do almost nothing to the labelled cells: at c_add 0.1
the rule added 2.3 labelled cells per seed in 895 (precision .44), at 0.05 0.3
cells, at 0.02 none. The classifier's scores below the near-gate band rarely
exceed 0.9. So the registered grid cannot tell adding from not adding.

### Per direction (human gates)

| | cells (seed mean) | precision |
|---|---|---|
| raw classifier: added | 149 (codex 44, IMC 43, lsp 62) | .47 (codex .35, IMC .68, lsp .42) |
| raw classifier: removed | 95 (codex 42, IMC 30, lsp 23) | .80 (codex .89, IMC .78, lsp .68) |
| rule c_clean .3: removed | 23 (codex 11, IMC 7, lsp 5) | 1.00 on every image |
| rule c_add .1: added | 2.3 (lsp only) | .44 |
| rule c_clean .4, post hoc: removed | 50 | .92 |

Cleaning is more precise than adding on every image at every margin
measured. The same holds for the raw classifier's two directions on CODEX and
lsp, but not IMC (added .68, removed .78, the nearest).

Base rates of the labelled sets, which set a floor for precision: of the human-
gate-negative labelled cells 36% (IMC), 31% (lsp near), 16% (CODEX) are own; of
the human-gate-positive ones 52%, 51%, 67% are glow/none. These near-gate
sets are not a sample of the whole image.

Over all eligible cells the rule at (.1, .3) removes 1,723 (CODEX), 1,392
(IMC) and 1,044 (lsp) positives of 117,459, 99,626 and 370,535 gate positives
(summed over markers, seed mean); that is 1.5%, 1.4% and 0.3%. The raw
classifier would remove 9,020, 7,484 and 12,950, and add 6,265, 11,681 and
29,877.

### Far set

Equal to the gate for every rule under human gates: the classifier overrides
no labelled far cell, even raw. Not worse; no
evidence either way.

### gmm gates (confirmation)

| | gate | raw | cleaning only c_clean .3 | both (.1, .3) |
|---|---|---|---|---|
| pooled, 895 | .717 (.713) | .729 (.711) | .722 (.720) | .723 |
| lsp near | .586 | .642 | .602 | .605 |
| IMC | .765 | .754 | .765 | .765 |
| CODEX LN | .710 | .734 | .710 | .710 |
| far | .761 | .756 | .761 | .761 |

The Auto gate is already 11 points better than the human gate on these
labelled cells (BA .717 against .604), so there is less for a rule to do.
Cleaning removed 8.7 labelled cells per seed, .85 precise (IMC .50 on 0.7
cells, lsp .87 on 8.0, CODEX none). Nothing is worse than the gate.

### By signal level

Terciles of d over the 36 markers (12 each; human gates; raw classifier):

| d | markers | gate BA | raw BA | raw added (precision, n) | raw removed (precision, n) |
|---|---|---|---|---|---|
| < 2.02 | 12 | .590 | .630 | .61 (71) | .71 (9) |
| 2.02-2.76 | 12 | .576 | .637 | .40 (43) | .77 (43) |
| > 2.76 | 12 | .678 | .726 | .28 (35) | .86 (42) |

The same, post hoc, in fixed d bins (rule at c_add .3, c_clean .3, see below):

| d | markers | gate BA | raw added precision | rule BA |
|---|---|---|---|---|
| < 1.5 | 4 | .581 | .76 (30 cells) | .622 |
| 1.5-2 | 8 | .592 | .50 (41) | .598 |
| 2-3 | 16 | .592 | .38 (57) | .630 |
| >= 3 | 8 | .695 | .24 (20) | .718 |

Add precision does not collapse at low d; it is highest there and falls as d
rises. This is the opposite of the prior, and it is confounded: the label sets
were built around gates, and at low d the own cells that sit below the gate
(the weak positives) are common, so more of what the classifier adds is real.
Clean precision stays at .71-.90 down to d 1.5; below 1.5 there are four
markers and the classifier removes 1.3 labelled cells per seed (.17), which is
too few to read. Under gmm gates the tercile pattern is not monotone (add
precision .43, .10, .47).

Pixel contrast cannot be terciled meaningfully here: the lowest tercile (cut at
4.9) is exactly the 12 IMC markers and the others are CyCIF and CODEX (cuts 4.9
and 6.9). It measures the modality. Observed values run 1.0 (IMC MPO) to 8.5
(CODEX CD21) across all 36 markers; the bars in `rules.py` (0.15 and 0.0) are
below every marker, so they never fire, and the data here cannot place them.

## Decision

Applying the registered rule:

1. Both margins: pooled BA .624 against .604 is +2.0 points, below the +3
   required. Does not pass.
2. Cleaning only, c_clean 0.3 (the largest registered value): pooled BA +2.0;
   selection set +1.8; no image worse (lsp near +1.5, IMC +2.2, CODEX +2.8);
   far unchanged; gmm gates not worse (+0.5 pooled); cleaning precision 1.00 on
   every image. Passes. c_clean 0.2 gives only +0.6 pooled and does not.
3. Agreement filter: no effect; leave it off.

**Ship cleaning only: `C_CLEAN = 0.3`, `C_ADD = None`.**

Post hoc, not registered and chosen on the same data (do not treat as
validated): extending the grid, c_add 0.3 with c_clean 0.3 reaches pooled BA
.639 (+3.5), and per image lsp near .633, IMC .602, CODEX .645, far .747. Add
precision there is .55 pooled but .29 on CODEX and .80 on IMC, on 34 cells per
seed, and under the registered bar "cleaning precision >= add precision" it
holds. It would pass the both-margin bar numerically; the margin is outside
what was registered, adding is the direction the user doubts, and the add
precision swings by image, so it is a candidate for the next bench, not a
setting. Also post hoc: c_clean .4 gives +3.4 pooled BA at .92 precision, and
c_clean .5 (the raw classifier's own negative call) +3.9 at .80.

The raw classifier alone beats every rule on the human-gate labelled cells
(BA .680) but overrides the gate in bulk (see the volumes above) and is worse than the gmm gate on
CODEX/IMC agreement; the rule is the conservative layer, not the most accurate
one.

## Recommended constants

- `rules.C_CLEAN = 0.3`
- `rules.C_ADD = None`
- `rules.SNR_LOW`: no change on this evidence (d 2.0 is a guess); with
  `C_ADD = None` it only matters once adding is switched on. Add precision
  did not fall under 0.5 at low d here, so this bench does not support any
  value for it.
- `rules.SNR_NONE`: no change (d 0.8 is a guess: one marker, lsp CD11c at
  d 0.77, lies below it, and there the raw classifier is +6 BA over the gate).
  Clean precision stayed .71-.90 above d 1.5 and could not be measured below it.
- Contrast bars: unsupported. The bars as written never fire on these 36
  markers; the contrast scale of `px_contrast` is not what their comment
  assumes (1.0-8.5 observed, not near 0), and it separates modalities, not
  weak from strong stains.

## Caveats

- Per-marker labelled counts are small (19-31); per-image BA moves 1-2 points
  on a handful of cells. The seed sd is far smaller than the cell sampling
  noise, so it understates the uncertainty.
- The human gates sit in the middle of the labelled cells by construction
  (separable midpoints), and the near sets were built around them; the
  gate's own BA is .60, well below the Auto gate's .72 on the same cells.
- The classifier is trained without the labelled cells but picks and
  labels are on the same three images; nothing here is out of image.
- Precision is over labelled cells in the near-gate bands, not over the image.
- **The images are better than most.** The user (2026-10-19): the LSP
  (CyCIF) samples are an outlier for signal-to-noise -- most images do not
  look like them -- and nothing should be tuned on them. Of the three images
  here, lsp11385_2 gained least from cleaning (+1.5) and the shipped values
  hold on IMC (+2.2) and CODEX (+2.8) without it. The defaults lean to the
  side that suits poorer images: no additions (the direction the classifier
  over-calls in where signal is poor; .29 add precision on CODEX even at the
  post-hoc c_add 0.3), no push, and the SNR guards kept as unmeasured
  guesses. A label set from a low-SNR image is the next test, before any
  margin is loosened.

## The shipped layer in a whole session (lsp11385_2, 2026-10-19)

`plexora ai bench gating --project lsp11385_2 --truth csv:<the user's gates>
--arms session --variants baseline,push` on a copy of the data root (the
truth agent answers every packet; QC present). 17 markers, 43 + 40 packets,
both sessions with their layers 203 s, 0.9 GB peak.

- Layers made on 15 of 17 markers; CD20L (D 0.29) and CD11c (D 0.77) were
  withheld at `SNR_NONE`. Six more graded `low` (D 1.3-2.0: CD31, CD68,
  FOXP3, Ki67, PD1, PDL1) -- on an image the user calls better than most,
  so the D bars are cautious, not calibrated.
- Removals: 0-122 cells a marker (CD163 117, SOX10 122, CD31 73), 0.1-0.5 %
  of the gate's positives.
- The classifier on its own (`raw_calls`) would have added 700-4,224
  positives below the gate per marker (Ki67 +1,362 on 3,813 gate positives,
  PDL1 +2,716 on 2,831) and removed 20-1,592: the over-calling the user
  described. The rule adds none of them.
- The push (`classifier: push`, up only) fired on no marker: confident
  negatives just above the gate reached the 30-cell bar on three markers
  (CD45 64, SOX10 84, CD163 116) but never a quarter of the band. Gates and
  agreement were identical in both arms (code agreement 0.924).

## The classifier check on p193, blind readers (2026-10-19)

`classifier: check` on lsp35766_p193 (an LSP image: nothing below is a
tuned value), propose mode, blind general-purpose readers (brief
`~/gatevision/p193cmp/BRIEF-N.md`), one reader id per arm, answers replayed
from the memo where a packet matched. Sessions under
`~/gatevision/p193cmp/sessions/` (K, KM, KC); `rescore_K.py` reads a check
arm's own answers by each word rule.

The first word arm (K) ran before three fixes, and its checks measured them:
first rows a hundredth wide (CD11c 5.617 to 5.61, CD4 7.038 to 7.03: the
unrounded gate beside a lattice point), a row the classifier flagged whole
whose empty other half read `cannot_tell` and vetoed a confirmed `own_stain`
(CD8a), and one row where two were asked. Fixed: rows at least one
half-band wide (`check_points`, `row_halves`), an empty other half not read.

| marker | user | no check (arm N) | word (KM's answers re-read) | mixed (KM, live) | count (KC, live) |
|---|---|---|---|---|---|
| CD3E | 5.08 (22.5 %) | 5.68 (5.3 %) | 5.28 (15.2 %) | 5.18 (18.6 %) | 5.42 (11.2 %) |
| CD8a | 7.30 (12.9 %) | 7.62 (5.0 %) | 7.62 | 7.24 (15.4 %) | 7.62 |
| aSMA | 4.71 (15.8 %) | 4.63 (17.2 %) | 4.33 (23.1 %) | 4.03 (30.2 %) | 4.63 |
| CD163 | - | 7.12 (5.2 %) | 7.12 | 6.84 (8.6 %) | 7.12 |
| CD20, CD11c, CD31, CD4 | | | unchanged by the check | | |

Mean |gate - user| over the 7 user gates: no check 0.29, word 0.27, mixed
0.26, count 0.24 (count's lead is Ecadherin 5.38 against 5.28, moved by
ordinary looks, not the check; within reader noise, ~0.07 an image).

- The check fired on 7-8 of 10 markers, always `down` on the fixed code:
  the classifier trained at the gate calls 34-99 % of the first row below
  it positive.
- Read `mixed`, it moved CD3E and CD8a to the user's gates; it also moved
  aSMA the wrong way, to 30 % positive (row 1 flagged share 0.34, the
  reader called dim haze along muscle spindle nuclei `own_stain`).
- Read by counts, no row confirmed: flagged halves 1-3 of 6 cells stained,
  the other halves 0-2 of 6, never the 2-cell margin. The classifier's extra
  cells are a little more stained than its rejects at the same values, and
  about half of them are stained by a reader's count -- what the rule bench
  measured for additions (47 %) and what `mixed` means.
- A flagged-share bar between 0.34 and 0.49 would keep CD3E/CD8a and stop
  aSMA. That is a value read off one LSP image; it is not set. A noisier
  image with user gates comes first.

Which way the classifier pulls, trained at the session's gate
(`~/gatevision/p193cmp/beside_K.py`; share of the first half-band row called
the other way): aSMA 17.2 % gate -> 21.8 % alone (user 15.8 %; row below 34-38 %
called positive), CD31 5.4 % -> 6.9 % (user 1.7 %; 59 %), both the wrong way;
Ecadherin 53.7 % -> 47.7 % (user 47.4 %; 65 % of the row above called
negative), CD3E 5.3 % -> 12.6 % (user 22.5 %), both the right way. Trained at
another gate it moves with it (aSMA at 4.03: 31.4 %): it extends the gate it
is given rather than finding its own edge. CD31 was spared only because
earlier looks had judged the rows below `neighbour_only`/`unstained`
(`_contradicted`). Ecadherin's check never ran in the mixed arm: a look's
four nearest points (5.16-5.22) all lay inside one half-band -- fixed, the
check now reads the walk to its anchor (`cellcalls._reach`).

**Rule change, the first real run (2026-10-19, live lsp35766_p193, apply mode,
delegated workers on the user's models file).** CD3E's check fired down from
5.94 (the classifier called 11,893 of 11,894 cells of the first row below
positive); the reader read the flagged cells `own_stain` in both rows, and
the cells beside them `cannot_tell` (one cell) and `own_stain`. Under the
halves-must-differ rule nothing moved and CD3E was accepted at 5.94 (user
5.08) -- stricter than a plain look, which moves on its row's word. The
flagged half now decides alone; the other half keeps the reader blind to
which is which and its word is recorded (`alike`), never a veto
(`cellcalls.confirms`, `confirms_count` without `count_margin`). Re-read under
it, the earlier arms change only where the other half read the flagged one's
way: KM unchanged, K's Ecadherin and FOXP3 checks would have moved up. The
run was rolled back and restarted on the new rule.

**Rule change, the lsp11385 real run (2026-10-20, session
gs_20261020T000004_f28b41, apply mode).** The user then moved six gates by
hand: Ki67 5.89 -> 6.31, PCNAL 6.33 -> 5.55, PD1 6.61 -> 6.96, NGFR 6.51 ->
5.64, SOX9 6.50 -> 6.39, CD163 7.06 -> 6.73. Ki67's check walked two rows
down from 6.36: the first (6.19-6.36) flagged 781 cells read `own_stain`
beside 2,239 read `mixed`; the second (5.89-6.19) flagged 230 read
`own_stain` beside 28,217 read `unstained` -- the reader's note said "the
5.89 field marks most cells, many with no nuclear stain", and the
flagged-half rule walked it anyway. A row is now walked by its cells
(`cellcalls.confirms`, `row_share`). Replayed on the run's own answers
(`~/gatevision/p193cmp/replay_rows.py`, no reader):

| | flagged half decides | row's cells >= 0.25 | row's cells >= 0.5 | user |
|---|---|---|---|---|
| lsp11385 Ki67 | 5.89 | 6.19 | 6.19 | 6.31 |
| lsp11385 CD20L | 6.28 | 6.41 | 6.41 | higher (user, by eye) |
| lsp11385 NGFR | 6.51 | 6.24 | 6.51 | 5.64 |
| lsp11385 SOX9 | 6.50 | 6.50 | 7.08 (row 49.8 %) | 6.39 |
| p193 KM CD8a | 7.24 | 7.24 | 7.62 (row 48.8 %) | 7.30 |
| every other check, both images | | unchanged | | |

0.25 is `min_share`, the share that opens a check; 0.5 lost two markers on
near-half rows. Both images are LSP: a noisier image with user gates is
still owed.

PCNAL is not the rule: the reader read the flagged cells below 6.33
(13,077 of the row's 50,998) `unstained` -- "only the dim nuclear floor PCNA
leaves in most nuclei" -- which is markers.yaml's own caveat ("a dim nuclear
floor in most cells is background, not proliferation"). The user's gate,
5.55 (54 % positive), counts that fill. The caveat now says a dim even
nuclear fill is PCNA (G1, repair, a long-lived protein): the gate calls the
stain, not the S phase.

PCNAL re-run alone (2026-10-20, propose, fresh answers, the user's wording
added: "a high dynamic range, dim to bright"). The first re-run still read
the old caveat: stored panel contexts rebuild only when
`vocabulary.version()` changes, and that was markers.yaml's hand-set
`version` -- an edited caveat never reached a panel resolved before it. The
version now carries the file's digest (`vocabulary._shipped`), and
`clear_cache` drops the shipped layer too. On the new caveat the check row
5.82-6.33 read `mixed` beside `mixed` and the gate went 6.33 -> 5.82 (about
9 % -> 34 % positive; the user 5.55, 54 %); the rows below 5.82 read
`unstained`/`neighbour_only`, and in that look's own picture those nuclei
are grey with at most a faint tint at the session's display window -- the
rest of the gap is what the window shows, not what the caveat says.

Aside, PCNAL's own estimators (lsp11385): KDE 5.52, Otsu 5.60, two-component
GMM 5.35 -- all at the user's 5.55 -- while the scored start took the noise
ceiling, 6.33 (`gmm+ceiling`; the three-component GMM sat at 3.87). The
display window's background anchor was 246 (log 5.51). Not acted on: one image.
