# Judge the suspicious cells of a mask in depth

You judge, from three views each, whether the cells of an object review are
one whole cell. Plexora chose them -- a measured check called each one
suspicious -- and draws them; you say what the pictures show.

## When to use

- Your brief names a `review_id` and this skill: you are a worker of an object
  review someone started (`object_review_start`).
- Or you started the review yourself and have no workers: follow the same loop.

## When not to use

- A flat mask (one segmentation drawn on every plane): there is no cell in
  depth to judge; say so.
- Deciding a gate or a QC region: other skills.

## Required features

A z-stack with a mask in depth (one id per cell through the planes) whose cells were
measured (`measure_objects`). A licence with `volume`; over MCP also `mcp`.

## Decision logic

1. `object_review_next` with the `review_id`. It returns one cell: its XY view
   at its centre plane, its XZ and YZ views through its centre (its outline in
   yellow among the others), its measured row, and the reasons it was queued.
2. Judge from the views:
   - `ok`: one whole cell -- one nucleus, a closed outline in every view.
   - `merged`: two or more cells under one outline; in XZ or YZ often two
     bodies stacked along z.
   - `split`: part of a cell; the rest is another outline above or below.
   - `clipped`: a cell cut off by the first or last plane (flat at the top or
     bottom of XZ and YZ).
   - `debris`: not a cell.
   - `unsure`: the views do not say. Prefer it to a guess.
3. `object_review_answer` `{review_id, object_id, verdict, note, model}`:
   `note` one short clause on what you saw; `model` the model you run on.
4. Stop after the number of cells in your brief (default
   {{OBJECT_REVIEW.objects_per_worker}}) or when `object_review_next` returns
   no cell, and return one line per cell: `<object_id> <verdict> -- <note>`.

## Tools

`read_skill`, `object_review_status`, `object_review_next`, `object_review_answer`.

## Evidence

The measured row (volume, planes, whether it touches the stack's ends) is a
hint, not the answer: a cell queued as `clipped_z` can be ok when the views
show it ends before the last plane. Cite the `artifact` of the views a verdict
rested on when the note needs it.

## Uncertainty

`unsure` leaves the cell as it is. Never call a cell `merged` or `split` on
size alone.

## Mutation policy

Answers are recorded against the review; nothing changes the mask. The
coordinator's `object_review_finish` writes the cells judged not one whole
cell as one QC region (receipted, undoable). Do not finish the review
yourself unless you started it.

## Done when

`object_review_next` returns no cell, or you judged the number of cells in
your brief, and you returned one line per cell.

## Failure modes

- Calling a cell `merged` because it is large: look for two bodies in XZ or
  YZ, not at the volume.
- Calling a cell `clipped` from its row alone: the views decide.
- Answering a cell twice, or one not in the review: `object_review_status`
  says which are left.

## Provenance

Each verdict is kept with its note, the model you named and the time.
