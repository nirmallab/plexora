# Gate a dataset automatically

Gate every marker of every image in a dataset in one session. The reference
image is gated in full first; each other image's markers are then aligned to
the reference's intensities and the settled gate carried across -- accepted
where the image matches, checked with one comparison where it has shifted,
re-gated on its own where its distribution changed or its staining failed.
Nothing is copied blindly, and every carried gate says so in its provenance.

## When to use

- "Gate this dataset / cohort / all these slides", "apply the gates to every
  image", "are the gates consistent across images?".

## When not to use

- One image: gate-image.
- The user insists on one identical threshold everywhere: `apply_gate_to_dataset`
  (and say what `compare_gates_across_images` shows about whether that is wise).

## Required features

A dataset (`list_datasets`, `get_dataset`) whose images have cell tables with
the same markers (a marker missing from an image is skipped there), and image
channels for the looks.

## Decision logic

1. `get_dataset`, then `inspect_project` on one image and `get_panel_context`;
   fill `unresolved` markers as in gate-image (`set_panel_context`, only what
   you know -- lineage-specific partners most of all: they are what a hard
   marker's starting gate stands on). Nuclear counterstains and
   autofluorescence channels are never gated, and continuous markers always
   are (gate-image, "Which channels are gated").
2. Optionally `compare_gates_across_images` first (a job; `job_wait`): per
   marker, each image's drift class and the strategy it implies. It tells the
   user up front whether one gate can serve the cohort.
3. `gating_session_start` with `scope: "dataset"`, the dataset, and
   `reference_image` if the user named one (default: the image with most
   cells), and `biology` when the user named the samples' tissue or disease
   (gate-image, "The sample's biology and the evidence graph"): one context
   for the whole dataset, so say so if the images come from different
   tissues. `get_marker_hierarchy` on the reference image shows the order the
   panel will be gated in and, under that context, which references each
   marker leans on. Then the gate-image loop: hand the packets to workers from
   the session's `delegate` block (or answer them yourself: `gating_next`,
   `gating_answer`, skill gate-packets) until `decided`. The packets are the
   gate-image ones (judged the same way, answered from each packet's
   `allowed`, read with the session's `reading_guide`) plus:
   - `pixel_setup`, once, when images of the dataset state no pixel size: the
     snapshots are of the first such image, and the answer sizes the pictures
     of every image still waiting (`evidence.applies_to`) -- one scanner is
     the usual case. Say so if the user mentions images from another scanner.
   - `transfer_check`: the reference image's cells either side of its gate
     (top) and this image's cells either side of the carried gate (bottom).
     `holds` if this image splits as cleanly; `too_low` / `too_high` if its
     split is off in that direction; `cannot_tell` sends the marker to a full
     look on this image.
4. Start a fresh conversation per image when the panel is large; continue with
   `gating_next(session_id)`.
5. `gating_session_status` shows each marker's `strategy` under `dataset`
   (the same classes `compare_gates_across_images` gives: one aligned gate
   `global_aligned`, a gate `per_batch`, `globally_informed_per_image`, or each
   image `per_image`). Report it per marker, with the image as the unit.
6. `gating_session_finish`, `gating_report` (includes the spread of each
   marker's gate across images), `export_gates` with `dataset` for a long CSV.

## Tools

`get_dataset`, `inspect_project`, `get_panel_context`, `set_panel_context`,
`compare_gates_across_images`, `job_wait`, `gating_session_start`,
`gating_next`, `gating_answer`, `gating_session_status`,
`gating_session_finish`, `gating_report`, `export_gates`, `set_pixel_size`.

## Evidence

Transfer packets carry the alignment (shift in background sds, scale, fit
residual), the positive fraction at the carried gate beside the reference's,
and the image's own GMM gate. Cite the reference image and the drift class with
every carried gate.

## Uncertainty

- The image is the experimental unit: per-image positive fractions and their
  spread, never pooled cell counts as replicates.
- A drift class is relative to the reference image; a poor reference (few
  cells, a failed marker) is said so, and the user can name another.
- A marker that failed technically on one image is excluded there and listed;
  it does not block the others.
- Each image is estimated on its own QC-passed cells (gate-image, "QC-passed
  cells only"): the start's `qc_exclusion` has a block per image. Report each
  image's left-out count with its gates, and name an image QC was never run on
  -- its gates stand on every cell, the others' do not.

## Mutation policy

As gate-image: each image's gates are written as child receipts of the one
session, each undoable; locked, approved and user-set gates are untouched;
`rollback` undoes the session's writes on every image.

## Provenance

Each carried gate's provenance says `transfer_aligned`, its reference image,
the drift class and the alignment. The report's dataset page shows each
marker's gate across images and the strategy.

## Done when

Every image's markers have final states; the per-marker strategy and any
images needing attention are reported; the report path is given.

## Failure modes

- An image whose table is on a data node that is asleep: its markers end as
  `insufficient_information`; the rest continue. Retry later with a new
  session for that image.
- A reference marker that is not accepted: its images are gated on their own.
