# Pixel evidence for gates: Stage 1 (2026-11-23)

The plan: deterministic pixel evidence that lowers the engine's dependence on
noisy reads, proven on images it was not designed on, with a declared
no-call wherever it does not apply. Two signals:

- **A, neighbour-referenced contrast ("self-lit share").** Per cell, own
  minus the brightest neighbour (touching cells, within 1.5 cell diameters).
  The table version uses segmentation means; the pixel version uses a disk of
  the cell's own radius at every centroid. The null is the contrast of cells
  below the background mode, by neighbour count. Per interval between
  candidates, Storey's share of cells the null cannot explain, divided by the
  detection efficiency (the share among cells above every candidate). The
  engine's own loss (`evidence.monotone` + `losses`) then picks the gate.
- **B, spill-controlled exclusive background.** The marker's p99 among cells
  clearly positive for an exclusive partner (partner gate + 0.2). Cells
  whose neighbours are brighter in the marker than the image median are left
  out.

Code: `tools/bench_gates/pixel_evidence.py` (the study),
`tools/bench_gates/pixel_signals.py` (the measurements),
`tests/test_gating_pixel_evidence.py`. Nothing in the engine changed.

## Verdict

**No role passed its pre-registered test.** A fails as a gate, as a
tie-break, as a check and as a reader, in both cohorts. B's check has too
little evidence (six distinct markers) and fails on CRC. Stage 2 is not
started. Do not re-propose neighbour contrast as a gate, a prior, a
tie-break or a substitute reader without a new reason. The reason is below.

**Why A fails.** The contrast marks spill-lit cells, not positives. Positive
cells cluster, so a positive's brightest neighbour is often a brighter
positive. A real dim positive therefore has a contrast *below* that of the
background cells the null is built from. Between the background and the
gate, the curve shows the signature: the share of local peaks dips just
above the background (SOX10 0.13 → 0.03, CD8a 0.18 → 0.08), where cells lit
by a bright neighbour dominate. It climbs only well into the positives. A
gate read off the self-lit share therefore lands high, and the error is
structural: lowering the level (share 0.25, 0.1) moves it by about 0.1.

## Data

| cohort | image | gates | where they come from |
|---|---|---|---|
| PCA (LSP CyCIF, melanoma panel) | lsp11385_2 | 33 | `runs/2026-10-22-e2-dev/truth_dev.json` |
| CRC (LSP CyCIF, colorectal panel) | lsp35766_p193_2 | 34 | the owner's gates in the project, 2026-11-23 (`get_all_gates`); 4 set at the column's top, meaning no positives, left out |

LSP11321, LSP11361, LSP11761 and lsp35766_p193 are on the HMS-O2 node,
which was not connected. Both images were looked at before (lsp11385_2 for
the v6 rules, p193_2 on 2026-11-22). Every number here is development
evidence, not a held-out result.

Per-cell label sets for E1: `runs/2026-10-09-labels{,-crc,-imc,-codex-ln}`.
They hold the person's own / glow / none labels on near-gate cells, read
from each set's own crops.

## E1: does contrast separate own stain from not-own? (per cell, four platforms)

AUC, own vs not-own, on the labelled near-gate cells:

| set | cells | value vs gate | contrast, table | contrast, pixel |
|---|---|---|---|---|
| LSP CyCIF (lsp11385_2) | 281 | 0.63 | **0.80** | 0.76 |
| CRC CyCIF (fc078201) | 243 | 0.59 | **0.69** | 0.69 |
| IMC (immucan) | 268 | 0.57 | **0.84** | 0.84 |
| CODEX lymph node | 275 | 0.62 | **0.82** | 0.83 |

- It holds in dense fields (0.78-0.87).
- It is weak for fibrous stains (CD31 0.56-0.76, SMA on IMC 0.54) and packed
  B follicles (CD20 on CODEX-LN 0.50).
- The table version is as good as the pixel version.

As a reader (threshold chosen with the platform left out), the held-out
balanced accuracy is LSP 0.73, CRC 0.58, IMC 0.77, CODEX 0.73. Opus readers
score 0.49-0.54 on LSP and 0.61-0.69 on CRC near the gate (E0.5).

So the contrast does carry per-cell information. Every way of turning it
into a gate failed, as the rest of this report shows.

## E2: gates against the person's

Free estimates on a grid every 0.1 from the background mode up. "Paired"
compares each method with the scored start on the markers both cover.

**lsp11385_2** (33 markers):

| method | covered | mean miss | within 0.2 | paired vs scored start (n) |
|---|---|---|---|---|
| A (table) | 19 | 0.98 | 16% | 1.18 vs 0.39 (10) |
| A (pixel) | 20 | 0.97 | 10% | 1.20 vs 0.39 (10) |
| A at share 0.25 | 19 | 0.88 | 16% | 1.11 vs 0.39 (10) |
| A at share 0.1 | 19 | 0.89 | 16% | 1.16 vs 0.39 (10) |
| local-peak onset (table) | 33 | 0.72 | 12% | 0.85 vs 0.35 (18) |
| local-peak onset (pixel) | 33 | 0.70 | 15% | 0.84 vs 0.35 (18) |
| scored start | 18 | 0.35 | 33% | |
| Otsu | 11 | 0.28 | 55% | 0.28 vs 0.45 (11) |
| mixture | 17 | 0.42 | 29% | 0.42 vs 0.36 (17) |
| engine (newest session) | 18 | 0.23 | 61% | 0.23 vs 0.35 (18) |

**lsp35766_p193_2** (34 markers):

| method | covered | mean miss | within 0.2 | paired vs scored start (n) |
|---|---|---|---|---|
| A (table) | 24 | 0.62 | 21% | 0.58 vs 0.39 (22) |
| A (pixel) | 24 | 0.61 | 25% | 0.56 vs 0.39 (22) |
| A at share 0.25 | 24 | 0.54 | 12% | 0.52 vs 0.39 (22) |
| A at share 0.1 | 24 | 0.52 | 21% | 0.52 vs 0.39 (22) |
| local-peak onset (table) | 34 | 0.51 | 35% | 0.46 vs 0.37 (30) |
| local-peak onset (pixel) | 34 | 0.47 | 38% | 0.41 vs 0.37 (30) |
| scored start | 30 | 0.37 | 53% | |
| Otsu | 20 | 0.49 | 25% | 0.49 vs 0.39 (20) |
| mixture | 24 | 0.39 | 50% | 0.39 vs 0.37 (24) |
| engine (newest session) | 32 | 0.35 | 69% | 0.28 vs 0.37 (30) |

- A's no-calls were the expected ones: flat for packed or ubiquitous
  markers (HLA-ABC, b2m, PD-L1), and the rise spanning the window for
  fibrous stains (CD31) and continuum markers (PD1, TCF1).
- The local-peak onset is the comparator: the 2026-11-22 independent run's
  code-only index, 10% of the way from baseline to plateau. Its 0.20 on 7
  CRC markers did not hold on 34 CRC markers (0.47) or on LSP (0.70).
- On p193_2 the engine's numbers are flattered: the owner set several gates
  by accepting the engine's (median miss 0.065).

## E3: tie-break at an unresolved close (frozen reads)

Among each close's plateau members, the one A's loss favours, against the
likeliest (what a close writes since VERSION 8).

| image | closes | A valid | mean miss where A acts: A vs likeliest | better / worse / >0.30 worse |
|---|---|---|---|---|
| lsp11385_2 | 45 | 4 | 0.24 vs 0.07 | 1 / 2 / 1 |
| lsp35766_p193_2 | 129 | 51 | 0.25 vs 0.21 | 15 / 24 / 4 |

**How much a tie-break could gain at most.** If the oracle always picked the
plateau member nearest the person's gate, the mean miss would fall:

- lsp11385_2: 0.26 → 0.16;
- p193_2: 0.35 → 0.21.

In 29-33% of closes the person's gate is not within 0.2 of any member: the
reads ruled it out. Choosing the member nearest a deterministic estimate gets
none of that gain consistently:

| prior | lsp11385_2 | p193_2 |
|---|---|---|
| likeliest (now) | 0.26 | 0.35 |
| nearest the scored start | 0.25 | 0.36 |
| nearest the background ceiling | **0.20** | 0.42 |
| nearest the local-peak onset (pixel) | 0.29 | **0.32** |
| nearest Otsu | 0.22 | 0.36 |
| nearest B | 0.27 | 0.37 |

Each prior that wins on one image loses on the other.

## E3b: checks on accepted gates

**A-check** ("material self-lit share below the gate"): it fired on 0
accepted gates on lsp11385_2 and on 1 on p193_2, and that one was wrong.

**B-check** ("gate below B − 0.2"). It is scored by direction: right when the
gate really is more than 0.2 below the person's.

| image | accepted | too low | B fires | right direction | distinct markers |
|---|---|---|---|---|---|
| lsp11385_2 | 214 | 78 | 42 | 32 (0.76) | 4 (CD68, CD163, FOXP3 right; Ecad wrong) |
| lsp35766_p193_2 | 19 | 4 | 2 | 0 | 2 |

For comparison, the engine's review flag has precision 0.56-0.57 on the same
sessions.

B sits below the person's gate for most markers, but not reliably: Ecad 7.05
vs 6.15, FOXP3 6.64 vs 6.30, CD8a on CRC 7.46 vs 7.28. Six distinct markers
in all is far below the evidence the rule asks for, and it fails on CRC.
Not adopted. Kept as a record in the tool.

## E4 (exploratory): contrast as a reader of the cells a look showed

Stored sessions, replayed by `controller.decide` with every stored read
kept, except the variant's own labels. 40% of LSP reads and 49% of CRC reads
are "uncertain". Thresholds come from E1 with the platform left out.

| image | units | stored reads | uncertain → contrast label | contrast labels every cell |
|---|---|---|---|---|
| lsp11385_2 (table) | 137 | 0.24 | 0.28 (19 better, 35 worse, resolved 58 vs 29) | 0.49 |
| p193_2 (table) | 174 | 0.37 | 0.38 (36 better, 51 worse, resolved 54 vs 41) | 0.54 |
| lsp11385_2 (pixel) | 137 | 0.24 | 0.27 | 0.53 |
| p193_2 (pixel) | 174 | 0.37 | 0.38 | 0.53 |

Labelling the uncertain cells by contrast makes the engine more sure and
less right. This is the failure the rules guard against.

## Blind methods (independent agents)

See the comparison section added below.

## Rules this followed

- No constant was chosen on these images. Reach (1.5 diameters), the disk
  radius (half a diameter), `min_between`, the tolerance and the loss are
  structural or the engine's own. E4's thresholds came from the label sets
  with the platform left out.
- No engine file changed. Gating VERSION 8 and QC CELLS_VERSION 8 are
  untouched.
- No live session ran and no model was paid for (blind agents aside).

## How to rerun

    export PLEXORA_TESTING=1 PLEXORA_DATA_PATH=<scratch> BIOCOGNIA_MARKER_GRAPH=<scratch>/g \
           PLEXORA_VOCABULARY_PATH=<scratch>/v PYTHONPATH=.
    R=<main checkout>/docs/internal/bench/runs
    S="$HOME/Library/Application Support/plexora/.agent/sessions/gating"
    python tools/bench_gates/pixel_evidence.py e1 $R/2026-10-09-labels{,-crc,-imc,-codex-ln} --out e1.json --rows
    python tools/bench_gates/pixel_evidence.py prepare --image lsp11385_2 --out prep_lsp11385_2
    python tools/bench_gates/pixel_evidence.py prepare --image lsp11385_2 --out prep_lsp11385_2_px --pixel --level 1
    python tools/bench_gates/pixel_evidence.py e2 --prepared prep_lsp11385_2 ... --truth truth.json --sessions "$S" --out e2.json
    python tools/bench_gates/pixel_evidence.py e3 --prepared ... --truth truth.json --sessions "$S" --e2 e2.json --out e3.json
    python tools/bench_gates/pixel_evidence.py e4 --prepared ... --truth truth.json --sessions "$S" --out e4.json

`truth.json` is `truth_dev.json` with the owner's p193_2 gates added as
`lsp35766_p193_2`.
