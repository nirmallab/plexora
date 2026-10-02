# Visual gating

Gate one marker by hand, with the user, checking the gate by looking at the
cells. The computer proposes the numbers; you judge the pictures
qualitatively; a fixed rule turns the judgement into a new number. You never
pick a threshold value by eye. For gating a whole image without walking
through each marker, use gate-image instead -- it runs this same judgement,
far cheaper, as a session.

## When to use

- "Gate `CD8` with me", "check my gate", "show me the cells at the gate",
  "which cells are positive" -- one marker, interactively.

## When not to use

- Every marker of an image or dataset: gate-image or gate-dataset.
- The marker looks broken: diagnose-marker first.
- Multi-marker phenotyping: gate each marker separately.

## Required features

A cell table with the marker, cell-id/x/y roles, and an image channel of the
same name for the look. A segmentation mask makes the look far better
(outlines).

## Decision logic

1. `get_gate`: is there already a gate (`thresholded`)? If yes, you are
   checking it, not replacing it -- say so and start at the first look at the
   cells, below.
2. `profile_marker` (stores nothing): `gmm_proposal` is the starting gate,
   `profile.t1` says whether the numbers alone support it and why, and
   `profile.flags` names any technical problem. `get_gate_distribution` draws
   the histogram with the fitted populations if the user wants it. With no
   fit, the marker has nothing to separate: say so and stop.
   For a hard marker -- the profile's class is not bimodal, the estimators
   disagree, or the marker is dim, broad or spilled into from a neighbouring
   lineage -- `score_gate_candidates` first: the candidates scored on the
   distribution, the gated partners and the tissue (gate-image, "Where a look
   starts: scored candidates"), and the proposal a gating session would start
   from. Start from `scored.proposal` instead of `gmm_proposal` when a robust
   partner leads it or the marker is continuous, and say which you chose.
   The partners it scores against are graded: a gate that failed or is under
   review is never one of them (`get_marker_hierarchy` shows which). A
   continuous marker is gated where expression rises out of background, not
   refused.
3. Store the starting gate with `set_gate` (cite the receipt's
   `operation_id`). With a viewer open, `viewer_preview_gate` shows a candidate
   on the user's slider first, saving nothing.
4. `render_gating_collage` with `layout: "t2"`: cells just below, at and just
   above the gate, spread over the slide, with the panels its manifest names
   (nuclear, marker, merge with the cell's outline, and the marker on a log
   scale whose mid-grey is the gate). `layout: "overview"` shows every positive cell on
   the whole image; `render_gate_validation` the older field view.
   `render_cell_gallery` and `explain_cell` look at single cells.
5. Judge the row nearest the gate most. Negative cells called positive: the
   gate is too low, `adjust_gate` `direction: "up"`. Stained cells missed: too
   high, `direction: "down"`. `magnitude` small / medium / large. The step is
   capped inside a guard band, so it cannot run into the background.
6. Re-render and repeat, at most three adjustments. An adjustment that made
   things worse is undone with `undo_operation`. Report the final gate with
   `get_gated_summary`.
7. Only if the user explicitly asks to save the gates into their file:
   `write_gates_to_source` with `confirm: true`.

## Tools

`get_gate`, `profile_marker`, `get_gate_distribution`, `set_gate`,
`viewer_preview_gate`, `render_gating_collage`, `render_gate_validation`,
`render_cell_gallery`, `explain_cell`, `adjust_gate`, `undo_operation`,
`get_gated_summary`, `write_gates_to_source`.

## Evidence

The collages (cite each `artifact.id`), the positive count and fraction before
and after each adjustment, and the profile's reasons.

## Uncertainty

- Segmentation errors (merged or split cells, outlines off the stain) make a
  gate look wrong when it is not: say so, do not adjust.
- Dim, blurred or saturated cells: an image-quality problem, not a gate one.
- A gate can be right for one region and wrong for another (staining
  gradients): say so instead of averaging.
- The gate is on the table's own scale (`log_transformed` says which).
- Where QC has been run, the profile, the collages, the galleries and the
  fields are drawn from the QC-passed cells (`qc_exclusion` in each result;
  gate-image, "QC-passed cells only"); a field's left-out cells are drawn grey
  and counted apart (`qc_left_out`). Pass `qc: "off"` only when the user asks.

## Mutation policy

- `set_gate` and `adjust_gate` change Plexora's own gating state (the
  sidebar's), are reversible (each receipt has an `undo_hint`), and never
  touch the source file. A locked or approved gate refuses them (`conflict`).
- `write_gates_to_source` modifies the user's file: only on an explicit
  request, with `confirm: true`, and only if the server allows it.
- Pass `expected_revision` from your last read when writing; a `conflict`
  means the user changed gates in the viewer meanwhile -- re-read and ask.

## Provenance

Report every `operation_id`, the gate before and after, the positive fraction
before and after, and the artifact ids the decision rested on.

## Done when

A stored gate, a sentence on how it was checked, the positive count and
fraction, and the receipts.

## Failure modes

- `precondition_missing` for a marker with no image channel: gate from the
  numbers only (`profile_marker`), and say it could not be checked visually.
- `conflict`: someone else wrote gates, or the gate is locked; re-read with
  `get_gate` and confirm with the user.
- `permission_required` on `write_gates_to_source`: tell the user how to allow
  it; do not retry.
