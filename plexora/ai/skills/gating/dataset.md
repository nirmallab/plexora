# Gate a dataset automatically

A reference of skill gate-image, which holds the session loop a dataset
follows: read it first. This adds what differs when every image of a dataset
is gated in one session. With engine `v4` (the default for a dataset) no
single image decides for the rest: once every image is profiled, the images
of each marker are grouped by how their backgrounds align, each group is
decided once with looks drawn from several of its images, and the group's
gate is carried into every member through that member's alignment and
checked there. An image that does not align (a failed stain, a different
distribution) is gated on its own. Nothing is copied blindly, and every
carried gate says so in its provenance.

## When to use

- "Gate this dataset / cohort / all these slides / this TMA", "apply the
  gates to every image", "are the gates consistent across images?".

## When not to use

- One image: gate-image alone.
- The user insists on one identical threshold everywhere: `apply_gate_to_dataset`
  (and say what `compare_gates_across_images` shows about whether that is wise).

## Required features

A dataset (`get_dataset`) whose images have cell tables with the same markers
(a marker missing from an image is skipped there), and image channels for the
looks.

## Decision logic

1. `get_dataset`, then `inspect_project` on one image and `get_panel_context`;
   set up as gate-image says (gating/setup): lineage-specific partners most of
   all, since they are what a hard marker's start stands on. Structural
   channels are never gated and continuous markers always are
   (gating/decisions).
2. Optionally `compare_gates_across_images` first (a job; `job_wait`): per
   marker, each image's drift class. It tells the user up front whether one
   gate can serve the cohort.
3. `gating_session_start` with `scope: "dataset"`, the dataset,
   `reference_image` if the user named one (it becomes the scale image of the
   group it belongs to; it never decides who is in a group), `mirror: true`
   when the user watches a viewer, and `biology` when the user named the
   samples' tissue or disease: one context for the whole dataset, so say so if
   the images come from different tissues. Then the gate-image loop until
   `decided`. The images are profiled a few at a time; a marker's units wait
   (`group_pending`) until every image has been profiled. Packets are the
   gate-image ones, plus:
   - `pixel_setup`, once, when images of the dataset state no pixel size: the
     answer sizes the pictures of every image still waiting
     (`evidence.applies_to`) -- one scanner is the usual case.
   - `display_setup`, once per image: staining differs between images.
   - `cell_panel` looks of a group come from up to {{GROUPS.anchors_max}} of
     its images in turn: each packet names the image its cells are from
     (`units`, `evidence.project`); read each as an ordinary panel of that
     image. Never compare cells across packets.
   - `transfer_check`: the scale image's cells either side of its gate (top)
     and this image's cells either side of the carried gate (bottom). `holds`
     if this image splits as cleanly; `too_low` / `too_high` if its split is
     off in that direction (the image then gets its own decision);
     `cannot_tell` keeps the carried gate, flagged unverified.
4. A group asks at most {{GROUPS.checks_min}} checks (or a tenth of its
   members, when more), gives at most {{GROUPS.own_looks}} of its images
   their own decision, and a marker never spends more than
   {{GROUPS.packets_base}} packets plus one per image; past that its gates
   close for review instead of being re-gated image by image.
5. Without workers, start a fresh conversation when the panel is large;
   continue with `gating_next` and the `session_id`.
6. `gating_session_status` shows each marker's `groups` and `strategy`, and
   `dataset_progress` (which images are being gated now); with
   `detail: "full"`, each group's scale image, members, anchors, gate and
   check outcomes under `dataset`. Report it per marker, with the image as
   the unit.
7. `gating_session_finish`, `gating_report` (its "Across images" page lists
   each marker's groups), `export_gates` with `dataset` for a long CSV.

## The viewer

With `mirror: true` the mirror follows the tab: it shows only looks of the
image the tab is on and never moves the tab. The user switches images from
the viewer's image switcher, which marks the images being gated; the agent
panel says how many images are being gated in parallel.

## Tools

{{tools}}

## Evidence

Each group records its members, their background alignment onto the scale
image (shift in background sds, contrast ratio, fit of the background
quantiles) and each member's class: `stable`, `member_shifted`, or
`no_positives` (no positives of its own on the same background, carried and
flagged `carried_no_positives`). Images under {{GROUPS.cell_floor}} cells
that align with no group of their own are attached to the nearest one
(`attached_under_floor`). Transfer packets carry the alignment, the positive
fraction at the carried gate beside the scale image's, and the image's own
GMM gate. Cite the group and the scale image with every carried gate.

## Uncertainty

- The image is the experimental unit: per-image positive fractions and their
  spread, never pooled cell counts as replicates.
- A carried gate no check confirmed is flagged `group_check_unverified` (or
  `packet_ceiling`) at low confidence; list those images.
- A marker that failed technically on one image is gated alone there and
  listed; it does not block the others.
- Each image is estimated on its own QC-passed cells (gating/qc-cells).

## Mutation policy

As gate-image: each image's gates are written as child receipts of the one
session, each undoable; locked, approved and user-set gates are untouched;
`rollback` undoes the session's writes on every image.

## Provenance

Each carried gate's provenance says `group_aligned`, its group, scale image,
alignment and class. A session started with engine `v1` or `v3` gates a
dataset from its reference image instead (`transfer_aligned`).

## Done when

Every image's markers have final states; each marker's groups, the images
gated alone and the images needing attention are reported; the report path
is given.

## Failure modes

- An image whose table is on a data node that is asleep: its markers end as
  `insufficient_information`; the rest continue. Retry later with a new
  session for that image.
- A scale image closed for review: its group's members are written at the
  carried gate for review too, never re-gated one by one.
