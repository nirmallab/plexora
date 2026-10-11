# Learning from users' cell calls — design notes

Status: notes for a future feature, nothing built. Written 2026-10-09 while
training the first per-cell "own stain or not" model (feature/spatial-gating
work, scratchpad experiment). Read with [`docs/TELEMETRY.md`](../TELEMETRY.md):
the usage telemetry described there must stay exactly as it is, and the data
contribution described here must be a separate channel.

## 1. What the model does and why it needs users' data

Automatic gating sets one threshold per marker. Cells near that threshold are
where the errors are: a negative cell lit by a bright neighbour (glow) reads
above the gate, a dim real positive reads below it. The per-cell model looks at
a small picture around one cell (12 layers, 48 x 48 px, cell-size normalised:
the marker scaled to its gate and to the image's own levels, DNA, this cell's
mask, neighbours' masks, edges, two expected and two not-expected partner
channels) and answers one question: is this marker's signal the cell's own?
The gate stays a number; the model's answer is a call layer beside it
(positive / negative / uncertain), and the user's data is never modified.

What limits it is labels. As of 2026-10-09 there are 1,067 hand labels from
one expert over four images (CyCIF x2, IMC, CODEX). Public sets add volume, but
their labels are often wrong (cell types mapped to markers) and can only be
pretraining signal. The labels that matter most — an expert's call on a cell
near the gate, in the user's own tissue and modality — exist only in users'
hands. Every time a user overrides a call, they produce exactly that label.

## 2. What could be collected, by value

| Signal | Where it comes from | Label quality |
|---|---|---|
| A correction: the user flips a cell's call | the call layer in the viewer, the "review uncertain cells" list | best: explicit, on a cell the model was unsure or wrong about |
| A quick-review answer: own / glow / none / can't tell | a labelling sheet the app offers (the `label_gallery` page made a feature: "10 cells, 30 seconds") | very good, and the cells are chosen by the model (active learning) |
| Acceptance: the user reviewed and left a call as it was | the same lists | weak positive evidence; easy to over-count |
| A gate moved after calls were shown | gating sidebar, `set_gate` | weak, threshold-level only: says nothing about which cells |

Collect the first two. Record the third only as counts. Ignore the fourth for
training (it already feeds gating's own memo).

Record why the cell was in front of the user, because people correct what they
look at: `shown_by` = `model_uncertain` | `random_audit` | `user_clicked`.
Training reweights by it. Without a random-audit slice the data only ever
describes the model's doubts and can't measure overall accuracy.

## 3. Three tiers, three consents

**Tier 0 — counts, inside the existing usage telemetry.** No new consent beyond
the current mode. Fits the present schema (Enum, Int, Hist only; no marker
names, no cell data). One new record, for example `cellcall.summary`:
`model_version` (Pattern), `modality` (Enum: cycif, codex, imc, mibi, vectra,
mxif, other), `mask_kind` (Enum: whole_cell, nuclear, none), `pixel_source`
(Enum: manual, metadata, estimated, none), `n_called` and `n_uncertain`
(Hist), `n_reviewed`, `n_corrected_to_pos` and `n_corrected_to_neg` (Hist),
`agreement` (Hist of per-session agreement). This answers "is the model
getting better in the field, per modality" without a single cell leaving the
machine. Follow "Adding a count" in TELEMETRY.md, including
`scripts/sync_telemetry_vectors.py`.

**Tier 1 — per-correction features, no pixels.** A separate opt-in ("Help
improve cell calls: send anonymous measurements of cells you correct"). Each
record is about 0.5 KB:
- the correction: `user_label` (own / glow / none / unsure) and `shown_by`;
- the model's side: `model_version` and `model_prob`;
- the context, as Enums: `modality`, `mask_kind`, `compartment`, `marker_role`
  (vocabulary role, not the name), and whether expected / not-expected
  partners existed;
- the numbers: a fixed-length vector of the model's penultimate embedding
  (e.g. 64 values quantised to int8) plus the hand-crafted features.

This lets us recalibrate the head and retrain the last layers without seeing
tissue. The present schema has no numeric vectors; add a fixed-length
`ListOf(Int)` field type or send these outside the telemetry batches.

**Tier 2 — crops.** A third, explicit opt-in ("contribute training images"), per
project, never inferred from a licence tier or a telemetry mode. Each record is
the model's own input for the corrected cell: 12 x 48 x 48, quantised to uint8
with per-channel scales, about 28 KB. That is several times the 64 KiB telemetry
batch budget per handful of cells, and pixels are content the telemetry rules
forbid, so tier 2 needs its own endpoint and storage:
- a separate Worker route and an R2 bucket;
- a manifest in D1;
- a contribution id per upload, so a contributor can list and delete what they
  sent.

## 4. Privacy and legal (tier 2 especially)

- **The tissue is a patient's.** Crops are tiny, carry no slide, coordinates,
  file or project names, and are randomly rotated. They are still derived from
  patient material, and the source data's terms decide whether they may leave
  the lab: data-use agreements, IRB protocols, HTAN / consortium rules. The
  opt-in must say so, and an organisation-level switch must be able to forbid
  it for everyone in the org.
- **Show it before sending it.** Extend `plexora telemetry preview`, or add an
  equivalent, so the user can see the exact crops and records queued, as images.
- **Never sent:**
  - channel or marker names, beyond the vocabulary role and compartment;
  - panel composition;
  - cell ids, coordinates, image size or slide-level statistics;
  - anything about the project.

  The marker's canonical vocabulary name could help per-marker evaluation, but
  only inside the tier-2 agreement, and it should default off.
- **Retention, deletion and audit:**
  - a fixed retention period;
  - delete by contribution id, and by install id ("forget me");
  - an audit log of which model versions were trained on which contributions.
- **Licensing:** the contribution terms must grant us the right to train and
  ship models on the data, given Plexora's commercial licence. The academic
  carve-out that lets us use CC BY-SA sets such as Pan-M for research does not
  extend to shipping weights. Get a lawyer's reading before tier 2 ships.

## 5. Label quality: users are noisy too

- Measured on 2026-10-09, the one expert agreed with their own repeated calls
  11/11 (melanoma), 10/11 (CRC), 8/10 (IMC) and 8/10 (CODEX). A crowd of users
  will be noisier, and each has their own line between dim real stain and glow.
- Re-show a small fraction of cells, as the labelling page's repeats do. That
  measures each contributor's self-agreement, and their labels are weighted by
  it.
- Cap any one contributor's share of a training round, so one lab's habits
  don't become the model's.
- Never treat any single source as truth, whether public annotations (often
  wrong), Claude teacher labels or user corrections. Keep a frozen,
  expert-labelled benchmark that no training round touches.

## 6. The improvement loop

1. **Every call records `model_version`.** A correction is attributed to the
   model that made the call. This is what turns corrections into an error rate
   per version.
2. **Collect, deduplicate and freeze.** Pull contributions into a versioned
   dataset snapshot (manifest in D1, blobs in R2). Hold out whole
   contributors, not random cells.
3. **Retrain** off-line, in a torch environment separate from the package,
   exactly as the experiment does now. Pretrain on public and free crops, then
   fine-tune on user labels with noise-tolerant loss.
4. **Gate the release** like the QC bench gates QC changes. Ship only if the
   new model beats the current one on the frozen benchmark, on held-out
   contributors and on every modality separately (leave-one-modality-out),
   with no modality regressing.
5. **Shadow, then switch.** The new model runs beside the current one and
   its agreement with incoming corrections is compared via tier-0 counts.
   Switch on evidence, keep the previous ONNX for rollback.
6. **Ship ONNX weights** the way magic select's are shipped (public weights
   repo, `onnxruntime` in the package, no torch).

**A more private alternative for later:** fine-tune a small adapter per site,
on the user's machine, from their own corrections, and send only the adapter's
weight changes (federated averaging). The pixels never leave, at the cost of a
much more complex loop. Worth revisiting once tier-1/2 volumes show what the
model needs.

## 7. Where the hooks go in Plexora

- **The call layer** (beside `<table>_not_called` / `_uncertain` in AnnData,
  drawn like the gate mask). A per-cell override there is the correction event.
- **A "review uncertain cells" list** in the gating sidebar: the model's least
  certain cells, one keypress each (the labelling page's own/glow/none/can't
  tell), with a random-audit slice mixed in.
- **The labelling-sheet generator** (`label_gallery.py` in the experiment):
  partners, the with/without-mask view and gate-centred windows were what made
  expert judgement possible. They move into the product.
- **The gating session and MCP** (`explain_cell`, the review-gating skill) may
  show calls, but an agent's answers are never logged as user labels.

## 8. Open questions for the user

- Which tiers to offer first: tier 0 only, tier 0+1, or all three?
- Is there an institution or programme (HTAN, a pharma partner) whose data
  terms would allow tier-2 contribution, to seed it?
- Should contributors see a per-lab model (their corrections adapt their own
  calls immediately) as the reason to opt in?
