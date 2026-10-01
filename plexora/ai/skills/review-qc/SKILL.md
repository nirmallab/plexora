# Review QC regions and cell flags

Go through a project's quality control with the user: what was excluded, why,
and what they want changed. Read first; change only what the user decides.

## When to use

- After a QC session, or when the user asks "what did QC remove", "loosen" or
  "tighten QC", "that region is not an artifact", "keep these cells".
- Before analysis, to confirm the exclusions with the user.

## When not to use

- No QC exists yet: qc-image (or the user draws regions in a QC: category
  and `refresh_qc` takes them in).
- Judging one marker's stain: marker-qc.

## Required features

A project with a QC result (`get_qc_results` says; `list_qc_results` lists
earlier ones).

## Decision logic

1. `get_qc_results`: lead with `categories` -- per category (Blur / focus,
   Registration, Segmentation, Tissue / acquisition, Staining / signal, Needs
   review) its regions by action, the cells its reasons excluded and warned,
   and the subtypes seen -- then the channels' statuses, every region with
   its `category`, `class` (the subtype), action, `score`, `threshold` and
   `threshold_source`, the agent's `ai_decision`, who made it and the user's
   edits, `cell_reasons` with where each came from (`cells_source`: read on
   the cell, or from a region), the strictness, and what the detectors found
   but did not pursue. `list_rois` shows the regions as the ROI panel holds
   them.
2. "Why was this region or cell removed": answer from the record -- the
   category and subtype, the tool that found it, the score against the bar
   and where that bar came from, what the agent's look said, how the outline
   was made. Show a region the user asks about with `render_qc_overview`
   (channels at their calibrated windows) or `get_roi`; `sample_qc_examples`
   shows a check's places across its score again, at the bar in force.
3. Change only what the user decides:
   - stricter or looser: `set_qc_strictness` with `lenient`, `standard` or
     `strict` -- every region's action and every cell's call is re-derived
     from what was measured and judged, nothing is asked again, and the
     user's own and approved regions keep theirs;
   - a region is wrong: the user deletes or reshapes it in the ROI panel (or
     `update_roi` / `delete_roi` on their request), then `refresh_qc`;
   - a region is right whatever the strictness: `approve_qc_roi` pins its
     action and locks it;
   - a cell reason, marker flag or channel verdict is wrong: `dismiss_qc_finding` sets it aside (recorded, restorable with `restore`);
   - a check's bar is wrong (it flagged normal tissue, or missed artifacts):
     the user decides the step, `adjust` moves it one -- `set_blur_check`,
     then `write_blur_regions`; `write_registration_regions`;
     `write_segmentation_flags` -- never a typed number;
   - undo any of these with `undo_operation`.
4. `qc_report` for the report, `export_qc` for files -- with
   `what: "provenance"` the record of every region and cell reason, one row
   each.

## Tools

`get_qc_results`, `get_qc_exclusions`, `list_qc_results`, `list_rois`,
`get_roi`, `update_roi`, `delete_roi`, `render_qc_overview`,
`set_qc_strictness`, `approve_qc_roi`, `dismiss_qc_finding`, `refresh_qc`,
`undo_operation`, `qc_report`, `export_qc`,
`sample_qc_examples`, `set_blur_check`, `write_blur_regions`,
`write_registration_regions`, `write_segmentation_flags`, `get_blur_check`,
`get_registration_check`, `get_segmentation_qc`.

## Evidence

The regions, the calls and their reasons are in `get_qc_results`; the
numbers in the report state their denominators. Quote them with those.

## Uncertainty

Regions sent to manual review are warnings: say so, and let the user decide.

## Mutation policy

Every change is receipted and undoable except deleting an ROI and writing a
source file, which need the user's explicit request.

Automatic gating leaves QC failures out of its estimates (`get_qc_exclusions`
says which cells), so a change made here makes the image's existing automatic
gates stale (`gating_qc` `stale_qc`): tell the user, and offer to re-gate.

## Provenance

`get_qc_results` carries each region's category and subtype, detector or
check, score and bar with its `threshold_source`, the agent's judgment and
notes (`ai.notes`, what it wrote when it looked), the strictness it was
decided under, and who changed it since; the user moving a
region to another category is their say, and its subtype follows.

## Done when

The user knows what is excluded and why, and every change they asked for is
made and receipted.

## Failure modes

- Changing regions the user did not ask to change.
- Moving a bar the user did not ask to move, or typing one.
- Rerunning a session to change the strictness.
- Reporting a percentage without its denominator.
