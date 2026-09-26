# Visual gating

Gate a marker and check the gate by looking at the tissue. The computer proposes the
number; the agent judges the picture qualitatively; a fixed rule turns the judgement
into a new number. You never pick a threshold value by eye.

## When to use

- "Gate CD8", "threshold this marker", "which cells are positive", "check my gate".

## When not to use

- The marker has not been QC'd and looks suspicious: run marker-qc first.
- Multi-marker phenotyping: gate each marker separately with this skill.

## Required features

A cell table with the marker, cell-id/x/y roles, and an image channel of the same name
for the visual check. A segmentation mask makes the check far better (outlines).

## Decision logic

1. `get_gate`: is there already a gate (`thresholded`)? If yes, you are checking it,
   not replacing it -- say so and start at step 3 with the stored gate.
2. `suggest_auto_gate` (computes, stores nothing). Look at `summary_at_auto_gate` and,
   if useful, `get_gate_distribution`. If there is no fit, say the marker has nothing to
   separate and stop.
3. Store the starting gate with `set_gate` (cite the receipt's `operation_id`).
4. `sample_gate_validation_regions` to see which fields exist (clear negative, clear
   positive, borderline); then `render_gate_validation` (it samples the same fields)
   and read every field's three panels: A raw marker, B overlay (magenta = called
   positive), C where the field's cells sit on the distribution.
5. For each field, judge using the response schema below. Weigh borderline fields most:
   the clear fields only confirm that positive and negative look different.
6. If most informative fields agree the gate is too low (negative cells called positive)
   or too high (stained cells missed), call `adjust_gate` with `direction` `up` or `down`
   and `magnitude` `small`/`medium`/`large`. Then re-render borderline fields.
7. Stop when fields look `about_right`, after at most three adjustments, or when the
   evidence is `cannot_tell`. Report the final gate with `get_gated_summary`.
8. Only if the user explicitly asks to save the gates into their file:
   `write_gates_to_source` with `confirm: true` (needs the server's permission).

Per-field judgement (what you record for yourself and report):

```
{assessment: too_low | about_right | too_high | cannot_tell |
             segmentation_problem | image_quality_problem,
 magnitude: small | medium | large | null, confidence: 0-1, reason: "...",
 request_marker: "optional channel to add", request_more_regions: true|false}
```

`too_low` means the gate is too low (too many positives): adjust `up`.

## Tools

`get_gate`, `suggest_auto_gate`, `get_gate_distribution`,
`sample_gate_validation_regions`, `render_gate_validation`, `set_gate`, `adjust_gate`,
`get_gated_summary`, `write_gates_to_source`.

## Evidence

The validation panels (cite each field's `artifact.id`), the field counts
(`cells_in_field`, `called_positive`, borderline below/above) and the dataset positive
fraction before and after each adjustment.

## Uncertainty

- Segmentation errors (merged or split cells, outlines off the stain) make a gate look
  wrong when it is not: assess `segmentation_problem`, do not adjust.
- Dim, blurred or saturated fields: `image_quality_problem`, do not adjust.
- A gate can be right for one region and wrong for another (staining gradients); say so
  instead of averaging.
- The gate is on the table's own scale (`log_transformed` says which).

## Mutation policy

- `set_gate` and `adjust_gate` change Plexora's own gating state (the sidebar's), are
  reversible (each receipt has an `undo_hint`), and never touch the source file.
- `write_gates_to_source` modifies the user's file: only on an explicit request, with
  `confirm: true`, and only if the server allows it. Never as a side effect.
- Pass `expected_revision` from your last read when writing; a `conflict` means the user
  changed gates in the viewer meanwhile -- re-read and ask.

## Provenance

Report every `operation_id`, the gate before and after, the positive fraction before and
after, and the artifact ids of the fields the decision rested on.

## Done when

A stored gate, a sentence on how it was validated (fields and verdicts), the positive
count and fraction, and the receipts.

## Failure modes

- `precondition_missing` with a marker that has no image channel: gate from the table
  only, and say it could not be checked visually.
- `conflict`: someone else wrote gates; re-read with `get_gate` and confirm with the user.
- `permission_required` on `write_gates_to_source`: tell the user how to allow it; do not
  retry.
