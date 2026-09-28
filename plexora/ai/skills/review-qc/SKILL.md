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

1. `get_qc_results`: the channels' statuses, every region with its class,
   action, scope, who made it and the user's edits, the cells excluded by
   reason, the strictness, and what the detectors found but did not pursue.
   `list_rois` shows the regions as the ROI panel holds them.
2. Show a region the user asks about with `render_qc_overview` (channels at
   their calibrated windows) or `get_roi`.
3. Change only what the user decides:
   - stricter or looser: `set_qc_strictness` with `lenient`, `standard` or
     `strict` -- every region's action and every cell's call is re-derived
     from what was measured and judged, nothing is asked again, and the
     user's own and approved regions keep theirs;
   - a region is wrong: the user deletes or reshapes it in the ROI panel (or
     `update_roi` / `delete_roi` on their request), then `refresh_qc`;
   - a region is right whatever the strictness: `approve_qc_roi` pins its
     action and locks it;
   - undo any of these with `undo_operation`.
4. `qc_report` for the report, `export_qc` for files.

## Tools

`get_qc_results`, `list_qc_results`, `list_rois`, `get_roi`, `update_roi`,
`delete_roi`, `render_qc_overview`, `set_qc_strictness`, `approve_qc_roi`,
`refresh_qc`, `undo_operation`, `qc_report`, `export_qc`.

## Evidence

The regions, the calls and their reasons are in `get_qc_results`; the
numbers in the report state their denominators. Quote them with those.

## Uncertainty

Regions sent to manual review are warnings: say so, and let the user decide.

## Mutation policy

Every change is receipted and undoable except deleting an ROI and writing a
source file, which need the user's explicit request.

## Provenance

`get_qc_results` carries each region's detector, the agent's judgment, the
strictness it was decided under, and who changed it since.

## Done when

The user knows what is excluded and why, and every change they asked for is
made and receipted.

## Failure modes

- Changing regions the user did not ask to change.
- Rerunning a session to change the strictness.
- Reporting a percentage without its denominator.
