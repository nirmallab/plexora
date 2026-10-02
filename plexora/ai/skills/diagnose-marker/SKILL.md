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
2. `score_gate_candidates`: where the onset, the noise ceiling and each gated
   partner's curve put the gate, and how each candidate trades purity against
   the positives it keeps (`components`). A partner curve far from the Auto
   gate says the mixture split something other than background from signal
   (another lineage's spill, a dim tail); `robust` false says a partner only
   follows its own gate; `unused_partners` names partners the vocabulary knows
   but nobody has gated yet. A marker the panel calls continuous is diagnosed
   for where its expression starts, not for a valley it will never have.
   `get_marker_hierarchy` with the marker says where it sits in the panel's
   tree, which references its evidence would lean on and which it avoids
   (a failed or low-reliability gate); pass the sample's `tissue` and
   `disease` when the user named them, so what the marker also marks there
   (`biology_for_marker`) frames the pictures.
3. From the flags and the candidates, the one question worth a picture:
   - separation / class -> `render_gating_collage` `layout: "strata"` (every
     band, plus spatially inconsistent cells);
   - a partner relation -> `bivariate_evidence` (a density plot comes back only
     when the contradiction is anomalous), then `layout: "quadrants"`;
   - gradient / edge / saturation -> `layout: "overview"`;
   - one strange cell -> `explain_cell`; its neighbourhood -> `render_region`.
4. `sample_gating_cells` gives cell ids for `render_cell_gallery` when a
   specific band needs a closer look.
5. Say plainly: what is wrong, which evidence shows it, and the remedy (re-stain,
   re-segment, a user-set gate, start from a scored candidate, exclude from phenotyping,
   or a gate conditional on a subset partner -- in a gating session, a
   `within_partner` answer -- when the stain is real only inside that
   partner's cells).
   A membrane or cytoplasmic marker that follows the nucleus is the
   nucleus-based mask, not bleed-through: the profile records the DNA
   correlation (`cell_qc` `nuclear`) without the `nuclear_bleed` flag.

## Tools

`profile_marker`, `score_gate_candidates`, `render_gating_collage`,
`bivariate_evidence`, `sample_gating_cells`, `render_cell_gallery`, `explain_cell`, `render_region`,
`get_panel_context`.

## Evidence

The profile's numbers and flags; each picture's artifact id and what it shows.

## Uncertainty

- Overlapping populations have no clean threshold; say the error either side
  of a gate rather than pretending there is none. A continuous marker still
  has an onset above background: that is its gate.
- A nuclear counterstain or an autofluorescence channel is not a marker to
  diagnose: automatic gating never gates one.
- Autofluorescent cells are bright in every channel: check two unrelated
  markers before calling positives real.
- The profile and pictures leave out QC failures where QC was run
  (`qc_exclusion`). A problem that vanishes with `qc: "off"` set aside was an
  artifact QC already caught; one that appears only with it is the remaining
  tissue's. Compare both before blaming the marker.

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
