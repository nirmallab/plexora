# Find and outline large artifacts by eye

A person spots a long fold, a torn or lifted corner, a smear of debris or a
bubble at a glance on a thumbnail of the slide; intensity rules delineate
them badly or miss them. This skill is that glance, with the drawing done
by magic select. Plexora shows you the whole tissue in four views on which
such artifacts stand out; you name the places that are clearly abnormal,
pick the channels that show each one, and point for the segmentation model
until its outline fits. You never type a coordinate: every point and box is
a click on a picture you were shown, and every number you need is in a
tool's answer.

It complements the detectors and never replaces them. The scan and the
Artifact Detector keep finding what rules capture; your pass is for what
they miss or outline poorly, and only when the artifact is clear enough
that a region is justified.

## When to use

- "Look for folds, tears, debris, bubbles or lifted tissue the detectors
  missed", "is there anything obviously wrong with this slide".
- A QC session hands you a `visual_scan` packet (its own visual pass): do
  this pass with the session's id, then answer the packet.
- After a session, when its review shows a large artifact nothing outlined.

## When not to use

- Small artifacts a whole-slide view cannot show (aggregates, specks, a
  blurred field): the scan detectors and the image checks (qc-image,
  qc-checks).
- Staining and signal problems: verdicts on channels, never regions.
- Reshaping a region the user asked about: review-qc.

## Required features

A fluorescence image with channels (`inspect_project`); magic select set up
on the server (`segment_qc_roi` says so when it is not, and a session plans
no pass). DNA channels recognisable by name make the DNA views; without them
the pan view and the agreement view still show folds and debris. A pixel
size makes the tissue's feather a size in microns.

## Decision logic

1. `inspect_project`. Inside a session, the `visual_scan` packet names the
   `session_id`, and its `evidence` lists the regions already written and
   the detector's pending objects; outside one, `get_qc_results` says what
   QC exists.
2. `render_artifact_overview`: the whole tissue in four tiles. `dna_max`
   is the brightest DNA across cycles (folds are doubled tissue, debris is
   bright); `dna_cycles` the first cycle's DNA cyan under the last cycle's
   red (cyan only: tissue lost or lifted later; red only: debris gained or a
   shifted cycle; red along one side of a piece and cyan along the other:
   the piece tore loose and moved between cycles, and the whole piece is the
   artifact, not just its rims); `pan` the mean of the channels (autofluorescent folds,
   debris bright everywhere); `agreement` where channels are bright together
   (red) or dark together (blue). The tissue is outlined cyan; regions
   already written are solid and labelled with r-numbers, pending detector
   objects dashed with d-numbers; a grid names places. Name at most
   {{VISUAL.max_regions}} places that are clearly abnormal and that no solid
   outline covers, largest first -- or say nothing is, and stop. A place you
   are unsure of is named in your report, not outlined. The whole-tissue
   sheet is coarse: before saying nothing is there, zoom (step three) on
   every place where outlines or dashed objects cluster and on every tip and
   corner of the tissue. Thin solid outlines along an edge are the rims a
   check found, not the piece between them.
3. Too small on the sheet to judge: `render_artifact_overview` again with
   `region` as a box on the sheet (`{artifact_id, px}`) for the same views
   zoomed on it.
4. `inspect_artifact_channels` with a `box` round the place: every channel
   ranked by how much the place differs from the tissue round it (`ranked`,
   each with its `direction`), and a sheet of the top
   {{VISUAL.channel_sheet_top}} beside a DNA reference tile. Choose two to
   four channels that show the artifact, by name, from what the tiles show;
   the ranking is a shortlist (every DNA and autofluorescence channel is
   listed wherever it ranks; the rest are counted). On an image of many
   channels, name one in `channels` to put it on the sheet whatever its
   rank. A fold shows in the DNA and the
   autofluorescence channel; debris in nearly every channel; lifted tissue
   as DNA missing in later cycles. Skip this step when the overview's DNA
   views already show the whole artifact -- a tear, a hole, tissue lost or
   moved between cycles: draw it on the DNA channels. Debris, a bubble, a
   fold, aggregates or anything seen first in one marker still gets the
   channel sheet.
5. `segment_qc_roi` with `preview` true, those `channels`, and a prompt on
   the picture: a `box` round a large or textured artifact, or one include
   point well inside it; exclude points (label zero) on tissue it should not
   take. The answer draws the outline snug and in context, and says
   `on_tissue_fraction`, `overlaps` and -- when a region of the same class
   already covers the place -- `duplicate_of`. Wrong? Add a point where it
   misses or where it took tissue, give `continue_from` the answer's `sam`
   token so the model starts from this mask, and preview again, at most
   {{SAM.max_refinements}} times; then leave the place for the user and say
   where it is.
6. Right? Write it: `artifact_class`, `confidence` (`sure` when the
   artifact and its class are plain; `fairly_sure`; `unsure` to leave it in
   Needs review as a warning), `severity`, `reasoning` (what you saw, in
   which channels) and `evidence_artifacts` (the overview, the channel
   sheet, the preview). Inside a session pass `session_id`. A duplicate is
   refused: reshape that region (`roi_id` with `mode` `replace` or `union`)
   only when yours is plainly snugger. Strictness turns your words into the
   action; an outline by magic select counts as measured support, so a
   `sure`, severe artifact excludes under the standard preset.
7. Inside a session: answer the packet with `qc_answer` -- `done`, with
   `left` naming what looked abnormal but was not outlined and why -- and go
   on with the session. Outside one: report every region written (its
   receipt, class and action), every place left, and why.

## Tools

`inspect_project`, `render_artifact_overview`, `inspect_artifact_channels`,
`segment_qc_roi`, `render_region` (a plain look at a place in channels of
your choice), `get_artifact` (a picture again), `get_qc_results`,
`list_rois`, `refresh_qc`, `undo_operation`, `qc_answer` (the session's
packet).

## Evidence

The overview is drawn from the image's own pixels at an overview level, the
same bytes for the same image; every tile records where its pixels are, so
a click on any tile is a place on the image. The channel sheet's ranking is
a contrast against the ring of tissue round the place, never a verdict. What
is written is the segmentation model's outline cut to the tissue;
`on_tissue_fraction` says how much of the raw outline was there.

## Uncertainty

Outline only what is clearly an artifact; a place you cannot name, or cannot
tell from biology, is reported, not drawn. `unsure` writes a warning in
Needs review, never an exclusion. An outline that will not fit after
{{SAM.max_refinements}} tries is left for the user, with where it is.

## Mutation policy

Every write is one QC region, receipted and undoable (`undo_operation`;
inside a session, the session's rollback undoes them all). Previews, the
overview and the channel sheet change nothing. An outline on the glass is
refused; one over the tissue's edge is cut to the tissue. A region of the
same class already over the place is refused unless `force_duplicate`.
Locked regions are never reshaped.

## Provenance

A region drawn here records `origin` agent and `method` magic select, the
prompts, the channels shown to the model, your `confidence`, `severity` and
`reasoning`, the `evidence_artifacts` you judged it on, and the regions it
overlaps; `get_qc_results`, the hover card, the report and `export_qc` show
them.

## Done when

Every clearly abnormal place has a region or a named reason it has none,
and the user (or the session) has the list: what was written, with its
class, action and receipt; what was left, and why.

## Failure modes

- Outlining biology: necrosis, a lymphoid aggregate, fat, mucin, cartilage,
  a vessel, the ragged edge of a biopsy. When in doubt, leave it.
- Drawing on the glass: debris beside the tissue affects no cell.
- Redrawing what a solid outline already covers.
- Taking a cluster of thin outlines for a covered place: a torn, displaced
  tip is ringed by small tissue-loss rims while the piece itself goes
  unflagged. Zoom on it.
- A box far larger than the artifact: the model returns the box.
- Refining past {{SAM.max_refinements}} tries, or writing `unsure` as if it
  were a decision.
- Working through forty channels one by one: the overview and the channel
  sheet are the look.
