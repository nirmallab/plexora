# Check an image's blur, registration and segmentation

Run the three local QC checks the QC panel runs, look at what they flag, and
write what is truly an artifact as snug regions and cell flags. Each check
scores the whole tissue on its own grid -- Blur QC a Blur Score per tile,
the Registration Check the share of nuclear pixels that disagree between two
cycles, Segmentation QC a score per cell -- so the finding is done before you
look. Your part is to tell artifact from normal variation on a few places
sampled across each score, and to say whether the bar sits right. **You
never type a threshold**: a bar moves one step at a time (`adjust`), and is
stored as steps from the automatic one.

Everything here is Free except `sample_qc_examples` (Paid): the whole loop
works without a licence, with a narrower look.

## When to use

- "Is this image in focus", "are the cycles aligned", "is the segmentation
  any good", "find blurred / misregistered / badly segmented areas".
- The user wants the checks' results written as QC regions or cell flags
  without a full QC session.

## When not to use

- A whole QC of the image, every channel and every kind of artifact: qc-image
  (its session runs these same checks first).
- Reviewing or loosening QC already written: review-qc.
- Looking at the tissue without a QC question: visual-inspection.

## Required features

An image (`inspect_project`). Blur QC needs a channel with tissue in it (the
nuclear ones by default); the Registration Check two or more nuclear
channels (`detect_nuclear_channels`); Segmentation QC a segmentation mask and
a DNA channel. Cell flags need the project's cell table. A pixel size makes
every grid and tile a size in microns.

## Decision logic

1. `inspect_project`, then run what applies, each a job (`job_wait`) reused
   when its inputs have not changed: `run_blur_check`,
   `compute_registration_mismatch` (once per `comparison`; `set_registration_check`
   and `step_registration_comparison` pick the pair), `run_segmentation_qc`.
2. Read each result with its denominator and its bar: `get_blur_check`
   (`blurred_pct` of evaluable tissue, `threshold` with its `source`,
   `distribution`), `get_registration_check` (`mismatch_map`: the share of
   nuclear area flagged, `threshold`, `distribution`; `stats.pattern`
   widespread or isolated), `get_segmentation_qc` (cells and area flagged,
   kept apart; `clusters`). A number alone is not a finding.
3. Look. `sample_qc_examples` with `check` (and `channel`, `comparison`, or a
   segmentation `module`) draws a row each of places clearly fine, just
   below and just above the bar, far above it, and inside the largest
   regions, and the whole tissue with the regions and the score map; the
   `manifest` says where every tile is. Judge each row by its tiles:
   artifact, normal or mixed. Without a licence it answers
   `license_required`: read `get_blur_check` or `get_registration_check`
   with `include_regions` instead and `render_region` the largest region and
   a place just below the bar, and say that the look was narrower.
4. Move a bar only when the borderline rows say so: artifacts just below it
   (`adjust: "tighter"`) or normal tissue just above it (`adjust: "looser"`).
   `sample_qc_examples` with `adjust` previews the step without storing it;
   `set_blur_check` and `write_registration_regions` store one. At most
   {{QC_ENGINE.adjust_max_steps}} steps either way; look again after each.
5. Write what you judged an artifact, only then: `write_blur_regions` (per
   channel), `write_registration_regions` (per comparison;
   `include_widespread` for a whole cycle shifted), `write_segmentation_flags`
   (where Segmentation QC's flagged cells crowd together, as regions). Each
   region is receipted; the cells' calls are re-derived at once.
6. `refine_qc_roi` retraces a region (a registration region to its mismatch
   map, a cluster to its density grid, a blur region to the blur trace;
   `method: sam` asks magic select for the outline instead);
   `refresh_qc` takes in the user's edits from the ROI panel.
   An obvious object the checks missed, or outlined loosely -- debris, a
   fold, a bubble, torn tissue you can see in a picture -- is outlined with
   `segment_qc_roi` (see Outlining an object below).
7. Report with `get_qc_results`: each category's regions and cells with
   denominators, the bars and their sources. Offer `export_qc` with
   `what: "provenance"`.

## Tools

`inspect_project`, `detect_nuclear_channels`, `run_blur_check`,
`get_blur_check`, `set_blur_check`, `write_blur_regions`, `clear_blur_check`,
`set_registration_check`, `step_registration_comparison`,
`compute_registration_mismatch`, `get_registration_check`,
`write_registration_regions`, `run_segmentation_qc`, `get_segmentation_qc`,
`write_segmentation_flags`, `clear_segmentation_qc`, `sample_qc_examples`,
`render_region`, `job_wait`, `refine_qc_roi`, `segment_qc_roi`, `refresh_qc`,
`get_qc_results`, `export_qc`, `undo_operation`.

## Outlining an object

`segment_qc_roi` is magic select for an agent: you point, a segmentation
model draws the snug outline. Reach for it only for an object you saw in a
picture and can name -- never to go looking.

- Point on the picture you were shown: `{artifact_id, px}` with the
  `render_region` artifact, one include point (label one) well inside the
  object, away from its edge. No arithmetic: the tool turns picture pixels
  into image pixels.
- When it takes tissue it should not, add an exclude point (label zero) on
  that tissue. For a large or textured object, give a `box` round it
  instead of more points. At most {{SAM.max_points}} points.
- `preview: true` first, always: it writes nothing and returns the proposed
  outline drawn on a new picture. Look at it; point again on that picture if
  it is wrong. After {{SAM.max_refinements}} tries that still miss, stop
  and leave the object for the user, saying where it is.
- A region a detector outlined loosely: `roi_id` with `mode: replace` and
  no points -- the region itself is the starting point (its outline and its
  box), and the model redraws it snug to the object inside. Add points only
  when that preview is wrong.
- `mode: new` with `artifact_class` writes a region; `replace`, `union` and
  `subtract` (with `roi_id`) reshape one. A locked region is refused, a
  region the user reshaped needs `force`, and an open QC session refuses the
  tool (its regions are the session's).
- `capability_unavailable` means magic select is not set up on this server:
  say so, and fall back to `refine_qc_roi` or the regions the checks wrote.

## Evidence

A score says where to look, never what is there. The sampled rows are the
evidence: places from across the distribution, spaced apart, the same
places for the same scores, bar and `seed`. Blur tiles show the scored
channel; registration tiles the reference in red and the comparison in
green -- yellow where they agree, a red or green crescent where a nucleus
moved; segmentation tiles the DNA with the mask's outlines. The whole-tissue
row shows whether flagged places cluster or cover everything.

## Uncertainty

`mixed` and `cannot_tell` are real answers: write nothing you could not tell
apart from normal tissue, and say which check and which rows. A check that
flags nothing near its bar needs no look. `global.possible` (blur everywhere,
a whole cycle shifted) is a warning an in-image bar cannot settle: say so
rather than report an empty share as clean.

## Mutation policy

The runs, reads and previews change nothing but the checks' own caches.
`set_blur_check` stores a channel's bar; the three writers write ROIs in the
QC categories and cell calls, each receipted and undoable (`undo_operation`,
each region by its own receipt). A writer replaces its own
earlier regions unless the user edited, locked, renamed or moved them. No
source file is written.

## Provenance

Every region and cell call records its category (`blur_focus`,
`registration`, `segmentation`), its subtype, the check and its version, the
score and the bar with `threshold_source` (`auto`, `user` for a typed value,
`user_relative` for steps you stored) and `offset_steps`, and the notes you
gave when you judged its rows (shown as the region's explanation when the
user hovers it). `get_qc_results` reads them back and `export_qc` writes them
to files.

## Done when

Every check that applies has been run and read with its denominator, what it
flagged has been looked at, the artifacts are written, and the user has been
told per category what was flagged, at which bar and why, and what was left
as normal variation.

## Failure modes

- Typing a threshold: the bar moves in steps, and only after a look.
- Writing regions straight from a number, without looking at the rows.
- Calling sparse tissue blurred, dense tissue under-segmented, or a few
  nuclei that moved a whole cycle shifted.
- Moving the bar to make a percentage look better: the borderline tiles
  decide, never the share.
- Reporting a percentage without what it is a share of.
