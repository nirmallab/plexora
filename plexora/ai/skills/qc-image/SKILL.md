# Quality-control an image automatically

Check one image for imaging and staining artifacts, end to end, and flag the
cells they affect. Plexora does everything code can do: the display
calibration, a pyramid scan of every channel into spatial QC maps, the
detectors that find where to look, the outlines, the writes and their
receipts. You are asked only what code cannot settle, one small decision
packet at a time: whether a channel looks clean, whether an outlined region is
a technical artifact and of what class, which channels it reaches, which
outline covers it, whether the cells beside a cutoff are debris or biology.
**You never type a coordinate or a threshold.**

QC is an annotation layer, never a deletion: a confirmed artifact becomes an
ROI in a QC: category (one per class) that the user can edit, and each cell
gets a pass/fail call with its reasons. Nothing is removed from the user's
data.

The packet is the authority on its own question: what you may answer is its
`allowed` list and its `answer_schema`, and every number you need is in its
`evidence`. The start (and `qc_session_status`) returns a `reading_guide`
once; each packet names the entries it relies on in `evidence.guide` and its
schema in `answer_schema.see`. Pass `guide_version` back as `known_guide` next
time and the guide is not sent again.

## When to use

- "QC this image", "find artifacts", "check for folds, blur, bubbles or
  aggregates", "which channels failed", "flag bad cells before analysis".
- Before gating or clustering a new cyclic-immunofluorescence (or any
  multiplexed) image.

## When not to use

- Reviewing, loosening or tightening a QC already done: review-qc.
- One marker whose stain looks wrong, with no need for regions: marker-qc.
- The user wants to draw the regions themselves: they draw in a QC: category
  in the ROI panel, and `refresh_qc` takes it in (no session, no licence).

## Required features

An image (`inspect_project` says). A cell table with a cell-id and x/y roles
adds the cell half; a segmentation mask makes a region's cells an overlap, not
a centroid. A pixel size sizes the scan's map cells in microns; without one
they are in pixels. Cycles are inferred from repeated nuclear channels or name
suffixes; when the names do not say, `set_qc_cycles` states them.

## Decision logic

1. `inspect_project`. If the user has a strictness in mind, note it; the
   default is `standard`, and it can be changed afterwards without asking you
   anything again (`set_qc_strictness`).
2. `qc_session_start` with the project, `mode: "apply"` (regions are written
   as they are decided, each undoable) unless the user asked to review first
   (`mode: "propose"`). Pass `mirror: true` when the user has the project open
   and wants to watch. The scan and detectors run as a job.
3. `qc_next` with the `session_id`. Its `state` is `decision` (one `packet`),
   `bulk_running` (call again), `waiting_for_user` (below), or `decided`.
4. Answer with `qc_answer` `{session_id, packet_id, answer: {kind, ...}}`; the
   result carries the next packet in `next`. How to judge each kind:
   - `channel_audit`: one tile per channel, whole tissue, at most
     {{QC_ENGINE.audit_batch}} to a sheet, the scan's candidates outlined and
     labelled. Per row: `clean` unless something technical is plainly wrong at
     this scale -- a sparse, dim or tissue-patterned stain is clean. For
     `suspicious`, name the outlines that worry you in `where` (or `elsewhere`
     for a problem no outline covers) and give a `class_hint`. `uncertain`
     costs one closer look. Candidates you do not name on a clean row are
     dismissed, unless the scan scored them very high.
   - `artifact_confirm`: the channel with the outline, its neighbourhood, a
     close crop, and the detector's own map; deeper looks add the nuclear stain
     and a matched clean field. `artifact` when it is technical (fold, blur,
     bubble, debris, aggregates, saturation, seam, shifted or lost tissue),
     with `artifact_class`, `severity` (the guide says what each word means),
     `boundary` (does the outline cover it) and `scope` when you can tell.
     `not_artifact` when it is real tissue or signal -- a lymphoid follicle, a
     vessel, necrosis, a real bright population. Prefer `need_more_evidence`
     over guessing; after the closest look an unclear region goes to manual
     review, never to an exclusion.
   - `artifact_scope`: the same place in several channels; choose the option
     whose channels show it.
   - `artifact_localize`: outlines from tight to the bounding box; choose the
     letter that covers the artifact and little else, `current`, or
     `none_fits` for a grid.
   - `artifact_grid`: name every square the artifact covers; never
     coordinates. `refine` asks once for a finer grid.
   - `cell_intensity`, `cell_area`, `cycle_stability`, `channel_outlier`: rows
     of cells far beyond, just beyond and just inside a proposed cutoff. Per
     side: `accept`; `too_aggressive` when real cells are flagged;
     `too_lenient` when artifacts pass; `not_artifact` when the extremes form
     a coherent population (small lymphocytes, a bright real subset) -- that
     side then only warns. Scattered bright outliers are rare biology; only
     clustered ones can be excluded, and only when you say `accept` or
     `too_lenient` on them. For `cycle_stability` say the `pattern`.
   - `final_qc_review`: every region on the tissue. `consistent` when nothing
     obvious is missed, nothing real is excluded and no region is far larger
     than its artifact; otherwise `inconsistent` with `concerns` naming region
     labels. Named regions are looked at once more.
   `waiting_for_user`: a candidate reached its allowance while the evidence
   still says to keep looking. The viewer asks the user; without a viewer ask
   them yourself and pass their answer with `qc_session_status` `limits`. Do
   not answer for them.
5. When `next.state` is `decided`: `qc_session_finish` with `action: "close"`
   (or `commit` in propose mode). The session's result becomes the project's
   active QC, and the cells' calls are written beside it. Then `qc_report` and
   tell the user the excluded tissue and cells with their denominators, and
   which regions are for manual review.
6. Offer `export_qc` for files. Only on the user's explicit request,
   `write_qc_to_source` with `confirm: true` writes the calls into their own
   table file.

## Tools

`inspect_project`, `set_pixel_size`, `qc_session_start`, `qc_next`, `qc_answer`,
`qc_session_status`, `qc_session_finish`, `qc_report`, `get_qc_results`,
`set_qc_strictness`, `set_qc_cycles`, `export_qc`, `list_rois`,
`undo_operation`, `write_qc_to_source`.

## Evidence

Every packet's pictures are drawn from the scan and the stored calibration,
the same bytes for the same data, so an identical packet from an earlier run
of the same agent is answered from the memo without asking. What you say is
stored as a judgment, never as a number: the region's class, severity and
scope; a cutoff side's verdict as a relative adjustment.

## Uncertainty

`unsure`, `cannot_tell` and `need_more_evidence` are real answers. An unclear
region becomes a warning for manual review; it never excludes cells. A limit
reached never makes evidence vanish.

## Mutation policy

In apply mode each region is an ROI written as a child receipt of the session
(`delete_roi` undoes it); `qc_session_finish` with `action: "rollback"` undoes
them all, newest first. The user's edits win: a region they reshape, move to
another category, lock or delete is theirs, and QC never changes it again.
Source files are written only by `write_qc_to_source`, only when asked.

## Provenance

Every region records its detector and version, your class, severity,
confidence and scope, the strictness it was decided under, and the evidence
artifacts; `get_qc_results` and the report show them.

## Done when

The session is finished, the report is written, and the user has been told
what was excluded (tissue and cells, with denominators), what was only warned,
and what needs a person.

## Failure modes

- Excluding real biology: a bright follicle is not an aggregate, a necrotic
  area is tissue. When in doubt, `not_artifact` or `need_more_evidence`.
- Guessing a boundary: choose an outline or name grid squares.
- Calling every channel suspicious: the audit is cheap because most rows are
  `clean`.
- Changing the strictness by rerunning: `set_qc_strictness` re-derives
  everything without a session.
