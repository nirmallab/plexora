# Diagnose a hard marker

Find out why one marker is hard to gate -- overlapping populations, a rare
population, a failed stain, saturation, a staining gradient, segmentation,
bleed-through -- from the numbers first and pixels second, and say what would
make it gateable.

## When to use

- A gating session ended a marker in `manual_review_recommended`,
  `technically_failed`, `not_binary` or `accepted_low_confidence`, and the user
  asks why.
- "Why is `CD4` so hard?", "is this stain working?", "is this bleed-through?".

## When not to use

- Routine gating: gate-image (it diagnoses as it goes and only asks about what
  is hard).

## Required features

The marker's column; its image channel and a mask for the pictures.

## Decision logic

1. `profile_marker`: the class, the first-tier reasons, the estimators
   (where they agree and disagree), separation (`ashman_d`, `valley_ratio`,
   `overlap_mass`), stability (`boot`), and the flags from the cell table
   (`cell_qc`: size, nuclear bleed, edge, illumination) and the image overview
   (`image_qc`: saturation, empty channel, gradient).
2. From the flags, the one question worth a picture:
   - separation / class -> `render_gating_collage` `layout: "strata"` (every
     band, plus spatially inconsistent cells);
   - a partner relation -> `bivariate_evidence` (a density plot comes back only
     when the contradiction is anomalous), then `layout: "quadrants"`;
   - gradient / edge / saturation -> `layout: "overview"`;
   - one strange cell -> `explain_cell`; its neighbourhood -> `render_region`.
3. `sample_gating_cells` gives cell ids for `render_cell_gallery` when a
   specific band needs a closer look.
4. Say plainly: what is wrong, which evidence shows it, and the remedy (re-stain,
   re-segment, a user-set gate, treat as continuous, exclude from phenotyping).

## Tools

`profile_marker`, `render_gating_collage`, `bivariate_evidence`,
`sample_gating_cells`, `render_cell_gallery`, `explain_cell`, `render_region`,
`get_panel_context`.

## Evidence

The profile's numbers and flags; each picture's artifact id and what it shows.

## Uncertainty

- Overlapping populations have no right threshold; say the error either side
  of a gate rather than pretending one exists.
- Autofluorescent cells are bright in every channel: check two unrelated
  markers before calling positives real.

## Mutation policy

Read-only. Any gate change follows from the user's decision through
gate-image, review-gating or visual-gating.

## Provenance

List the profile's key numbers and the artifact ids behind the diagnosis.

## Done when

The user has the cause, the evidence and a remedy for the marker.

## Failure modes

- `precondition_missing` for a column with no image channel: diagnose from the
  numbers alone and say the pixels could not be checked.
