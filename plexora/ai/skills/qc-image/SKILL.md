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

The outline you judge is a search envelope, not the region written: Plexora
traces the artifact's own pixels inside it (an aggregate's specks, a fold's
band, the blurred patch) and writes only those, so the normal tissue the
envelope takes in is kept. Your job is that the whole artifact lies inside
the envelope; the tracing is code's.

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
   (`mode: "propose"`). An open Plexora tab is mirrored by default (the result's
   `mirror.reason` says why not when none is); pass `mirror: false` when the
   user does not want the viewer driven. The scan and detectors run as a job.
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
     costs one closer look. An outline seen in several channels is one
     candidate with the same label on each of their tiles. Candidates you do
     not name on a clean row are dismissed -- seams, shading, background and
     failed stains are settled by the tile itself -- unless the scan scored
     them very high and they are too small or too fine for a tile to show
     (a little blur or fold, aggregates): name those in `where` when they
     worry you.
   - `artifact_confirm`: the channel with the outline, its neighbourhood, a
     close crop, and the detector's own map; deeper looks add the nuclear stain
     and a matched clean field. `artifact` when it is technical (fold, blur,
     bubble, debris, aggregates, saturation, seam, shifted or lost tissue),
     with `artifact_class`, `severity` (the guide says what each word means),
     `boundary` (`covers`: the whole artifact lies inside the outline, however
     much larger it is; `too_small`: part lies outside; `too_large`: it takes
     in other tissue the trace could mistake for the artifact) and `scope`
     when you can tell.
     `not_artifact` when it is real tissue or signal -- a lymphoid follicle, a
     vessel, necrosis, a real bright population. `autofluorescence` only when
     an autofluorescence (blank, unstained) channel shows it or the same
     structures are bright in two or more markers; one marker's diffuse
     off-target signal is `excessive_background` (Plexora records the change if
     you say otherwise). Prefer `need_more_evidence`
     over guessing; after the closest look an unclear region goes to manual
     review, never to an exclusion. First looks come several to a sheet,
     one row per candidate, labelled as on the audit: answer `verdicts`, keyed by
     label, each entry those same fields; `need_more_evidence` on one gives
     that one its own closer sheet. A single-candidate packet takes the
     fields at the top level (`verdict`, ...).
   - `artifact_scope`: the same place in several channels; choose the option
     whose channels show it.
   - `artifact_localize`: envelopes from tight to the bounding box, each with
     what Plexora traced inside it drawn solid; choose the smallest letter
     whose trace holds the whole artifact, `current`, or `none_fits` for a
     grid.
   - `artifact_grid`: name every square the artifact touches (its pixels are
     traced inside them); never coordinates. `refine` asks once for a finer
     grid.
   - `cell_intensity`, `cell_area`, `cycle_stability`, `channel_outlier`: rows
     of cells far beyond, just beyond and just inside a proposed cutoff; the
     evidence's `asks` says what each side is asking. Per side: `accept`;
     `too_aggressive` when real cells are flagged; `too_lenient` when
     artifacts pass; `not_artifact` when the extremes form a coherent
     population (small lymphocytes, a bright real subset) -- that side then
     only warns. Only a side you judged an artifact excludes anything; a side
     you were not shown, or `cannot_tell`, only warns. High counterstain and
     cycle gain only ever warn. A `channel_outlier` compares the brightest
     cells with the marker's own positive cells (the "just inside" row is the
     brightest real-looking positives): `accept` marks that marker unreliable
     in the cells beyond -- it never removes a cell. For `cycle_stability` say
     the `pattern`. One correction moves a cutoff a full step (at least a
     MAD, and a quarter of its distance from the median), so say it once.
   - `cell_modules`: several of those modules in one packet, every row
     labelled `module | side: row`. Answer `modules`, keyed by module name,
     each entry the single module's fields. A side not drawn had nothing
     beyond its cutoff and stays as proposed.
   - `final_qc_review`: every region on the tissue, as its traced outline.
     `consistent` when nothing
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
   tell the user the excluded tissue and cells with their denominators, the
   markers flagged in cells (the cells kept), which channel-scoped regions the
   cells' own values did not bear out (`cells.not_borne_out`), and which
   regions are for manual review.
6. Offer `export_qc` for files. Only on the user's explicit request,
   `write_qc_to_source` with `confirm: true` writes the calls into their own
   table file.

## Tools

`inspect_project`, `set_pixel_size`, `qc_session_start`, `qc_next`, `qc_answer`,
`qc_session_status`, `qc_session_finish`, `qc_report`, `get_qc_results`,
`set_qc_strictness`, `set_qc_cycles`, `export_qc`, `list_rois`,
`undo_operation`, `write_qc_to_source`. `refine_qc_roi` retraces a region
already written (one, or `all`) -- for a region the user drew by hand, say.

Two free checks sit beside the session, and the QC panel runs the same tools.
**Registration Check** compares two nuclear channels: `detect_nuclear_channels`
lists them, `set_registration_check` turns it on (the open viewer puts the
reference in the first channel slot and the comparison in the second; its
"flicker" is zebra stripes over the pixels where the two stains disagree, not
a swap of channels), `step_registration_comparison` moves the comparison,
and `compute_registration_mismatch` measures each block's displacement and the
share of evaluated tissue at or above the threshold (`highlighted_pct`, its
denominator in `denominator`); `get_registration_check` reads it back. A
widespread `pattern` is a whole cycle shifted; an isolated one is local.
A block shift misses cells that moved, deformed or lifted between cycles, so
`highlighted_pct` can read as none displaced over plainly wrong tissue: read
`dense_mismatch_pct` (the nuclear area where disagreement crowds together)
and `mismatch_hotspots` (its densest places, full-resolution pixels), and
`render_region` a hotspot before calling an image clean. The image edge and
nuclei present in one cycle only show up there too.
**Segmentation QC** checks the mask against the DNA stain:
`run_segmentation_qc` is a job (`job_wait`), `get_segmentation_qc` gives the
share of cells and, separately, of segmented area called under- or
over-segmented; ambiguous cells are counted, never drawn;
`clear_segmentation_qc` forgets the result. Both are reused when
their inputs have not changed, and `export_qc` / `write_qc_to_source` carry the
Segmentation QC columns.
**Blur QC** finds out-of-focus regions without a model: `run_blur_check` is a
job (`job_wait`) scoring every grid cell of the tissue from sharp to fully
blurred against the image's own sharpest tissue, once per listed DNA channel
(the first nuclear ones until someone picks others; `channels` lists them);
`get_blur_check` gives each channel's colour, threshold (automatic unless
someone set one), the share of evaluable tissue that is blurred
(`blurred_pct`; its `denominator` names what it is a share of) and, with
`include_regions`, each region's outline. A `threshold` passed to
`get_blur_check` is a preview; `set_blur_check` stores one for a `channel`
(`"auto"` puts the automatic one back) and re-reads no pixel; it also sets a
channel's colour and which channels are listed. `global_blur.possible` means
even the sharpest tissue has little fine detail: an in-image reference cannot
see blur that is everywhere, so say so rather than report an empty blurred
share as clean. Nothing becomes an ROI until `write_blur_regions`, which
writes the regions as "QC: Out of focus" (action exclude) and replaces that
channel's earlier blur ROIs unless the user edited or locked them; each region
has its own receipt. `clear_blur_check` forgets a channel's result, or every
one.

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
confidence and scope, the strictness it was decided under, the evidence
artifacts, and how its outline was made (traced by which method and how much
of the envelope it keeps, or the envelope and why); `get_qc_results` and the
report show them.

## Done when

The session is finished, the report is written, and the user has been told
what was excluded (tissue and cells, with denominators), which markers are
unreliable in which cells, what was only warned, and what needs a person.

## Failure modes

- Excluding real biology: a bright follicle is not an aggregate, a necrotic
  area is tissue. When in doubt, `not_artifact` or `need_more_evidence`.
- Guessing a boundary: choose an outline or name grid squares.
- Saying `too_small` or choosing a tighter outline to get a tighter region:
  the trace does that. Choose the envelope that holds the whole artifact.
- Calling every channel suspicious: the audit is cheap because most rows are
  `clean`.
- Changing the strictness by rerunning: `set_qc_strictness` re-derives
  everything without a session.
