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

The image checks run first and do the finding: Blur QC on the nuclear
channels (every channel when no DNA channel is recognisable by name), the
Registration Check of every nuclear channel against the reference (not run,
and said so, with fewer than two recognisable DNA channels), Segmentation QC
on the mask (where cells far too large or too small cluster), and the
Artifact Detector (folds, tears, debris, saturation) each score the whole
tissue. A check not run is listed in the final review's `planning_notes`:
its silence is not a clean result. You
are shown a few places from each part of a score's distribution -- clearly
fine, just below and just above the bar, far above it, the heart of the
largest flagged regions -- and you say what they are: artifact or normal
variation, and whether the bar sits right. Plexora moves the bar a step when
you say so, and turns what you confirmed into regions as snug as the check's
own grid, and into cell calls. You never have to find a blurred field, a
shifted cycle or a merged cell yourself.

The outline you judge is a search envelope, not the region written: Plexora
traces the artifact's own pixels inside it (an aggregate's specks, a fold's
band, the blurred patch) and writes only those, so the normal tissue the
envelope takes in is kept. Your job is that the whole artifact lies inside
the envelope; the tracing is code's.

QC is an annotation layer, never a deletion: a confirmed artifact becomes an
ROI in one of five QC: categories the user can edit -- Blur / focus issue,
Registration issue, Segmentation issue, Tissue / acquisition artifact,
Staining / signal artifact (and Needs review) -- with the class you named kept
as its subtype, and each cell gets a pass/fail call with its reasons. Nothing
is removed from the user's data.

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
- Only the image checks, without a session (no licence needed): qc-checks.
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
   user does not want the viewer driven. The scan, the detectors and the image
   checks (`checks`: `blur`, `registration`, `segmentation`, all on by default;
   the result's `checks` lists the ones planned) run as one job. A check
   supersedes the scan detector that looked for the same thing on its coarser
   grid, and hands back to it when it cannot run.
3. `qc_next` with the `session_id`. Its `state` is `decision` (one `packet`),
   `bulk_running` (call again), `waiting_for_user` (below), or `decided`.
   A start (or `qc_session_status`) with `delegate` means the user's models
   file assigns QC tasks to models: do not answer packets yourself. Launch the
   entry of `delegate.workers` whose `launch` is `now`, on its `model`, with
   its `brief` as the whole prompt and only `delegate.tools`, in the
   foreground, and wait for its lines (no wake-ups, messages or status polls
   while it runs). One that returns `other_tasks <task>`: launch the worker
   whose `tasks` hold that task (an `on_demand` one only then); one that
   stops at its quota: launch its group again. Relaunch until a worker
   reports `decided`, then go to step five. A client that cannot set a
   worker's model answers itself (step four) and tells the user which tasks
   ran on another model; status `models` shows which model answered each.
4. Answer with `qc_answer` `{session_id, packet_id, answer: {kind, ...}}`; the
   result carries the next packet in `next`. How to judge each kind:
   - `channel_audit`: one tile per channel, whole tissue, at most
     {{QC_ENGINE.audit_batch}} to a sheet, the scan's candidates outlined and
     labelled. Per row: `clean` unless something technical is plainly wrong at
     this scale -- a sparse, dim or tissue-patterned stain is clean. For
     `suspicious`, name the outlines that worry you in `where` (or `elsewhere`
     for a problem no outline covers) and give a `class_hint`. `uncertain`
     costs one closer look: name in `where` the outlines you are unsure of
     (the others on that tile are then settled as on a clean row), or none
     to have every outline on it looked at. An outline seen in several channels is one
     candidate with the same label on each of their tiles. Candidates you do
     not name on a clean row are dismissed -- shading, background and
     failed stains are settled by the tile itself -- unless the scan scored
     them very high and they are too small or too fine for a tile to show
     (a little blur or fold, aggregates): name those in `where` when they
     worry you.
   - `score_review`: one check on one channel. Each row holds places from one
     part of its score, captioned with the score; the last row is the whole
     tissue with the regions at the bar and the score map. Answer `strata`,
     one verdict per row shown, keyed by its name (`clear_good`,
     `borderline_below`, `borderline_above`, `strongly_abnormal`,
     `clustered`): `artifact`, `normal`, `mixed` or `cannot_tell` -- judged
     from the tiles, not the numbers. Then `threshold`: `accept` when the
     rows beyond the bar are artifacts and the borderline rows are what a bar
     should split; `too_lenient` when just below already shows the problem;
     `too_aggressive` when just above looks normal. A move shows the places
     again at the new bar, at most {{QC_ENGINE.score_rounds}} looks, each
     step stored as `offset_steps` with `threshold_source` `agent_refined`.
     When the far and just-above rows are both artifacts every region at the
     bar is written at once; a mixed just-above row sends the regions near the
     bar to `artifact_confirm` one by one; a normal far row writes nothing.
     `whole_tissue` is asked only when `global.possible` is true: `artifact`
     when the whole channel, cycle or mask shows the problem (one region over
     the tissue; for registration, a verdict on the channel instead -- its
     cycle's markers unreliable in every cell, no region drawn). Give `artifact_class` only when the flagged places show
     another artifact than the check's own.
     What normal variation looks like: a sparse or dim stain is not blur, and
     nor is a region with few nuclei; nuclei moved a cell or two in a few
     places are a local mismatch, a whole field shifted is a cycle shift
     (`whole_tissue`); dense tumour is not under-segmentation, small
     lymphocytes are not fragments, big macrophages are not merges.
   - `artifact_confirm`: the channel with the outline, its neighbourhood, a
     close crop, and the detector's own map; deeper looks add the nuclear stain
     and a matched clean field. `artifact` when it is technical (fold, blur,
     bubble, debris, aggregates, saturation, shifted or lost tissue),
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
     fields at the top level (`verdict`, ...). A check's region (its evidence
     says which check) is the score map's own outline: its `boundary` is
     nearly always `covers`, and it is never re-localised.
   - `artifact_scope`: the same place in several channels; choose the option
     whose channels show it.
   - `artifact_localize`: envelopes from tight to the bounding box, each with
     what Plexora traced inside it drawn solid; choose the smallest letter
     whose trace holds the whole artifact, `current`, or `none_fits` for a
     grid. When no solid trace is right -- each misses part of the artifact,
     spills onto clean tissue, or joins two objects -- and `allowed` lists
     `redraw`, answer `redraw`: the segmentation model outlines the region
     again (once per region, any class), and the same packet comes back with
     its trace drawn and `evidence.redrawn` saying whether it was kept.
   - `artifact_grid`: name every square the artifact touches (its pixels are
     traced inside them); never coordinates. `refine` asks once for a finer
     grid. When the closer view shows another artifact than the one raised
     (a fold, not debris), say so with `artifact_class`.
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
`get_qc_exclusions`, `set_qc_strictness`, `set_qc_cycles`, `export_qc`,
`list_rois`, `undo_operation`, `write_qc_to_source`. `refine_qc_roi` retraces a region
already written (one, or `all`) -- for a region the user drew by hand, say; a
registration region retraces to its mismatch map, a segmentation cluster to
its density map, a blur region to the blur trace.

When the server has magic select set up, the session's tracer also asks it
for physical artifacts -- debris, a fold, a bubble, torn tissue -- and for a
blurred patch, and keeps its outline only where it agrees with the classical
trace (or where that trace found nothing to trust). The Artifact Detector's
own objects are sharpened the same way, held to agreement with the
detector's outline. The outlines on a localize sheet are then
already snug; you still choose among them by letter, never by coordinate.

The image checks the session runs are also tools of their own, free and the
same the QC panel runs: the qc-checks skill covers them.

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

At the end of a session the regions are consolidated: one ROI per category
and action (named for its category, action and how many regions it holds), so a user
excludes cells by a handful of ROIs, not tens. Each finding keeps its own
class, channels and outline in the ROI's `findings`, and the cells follow each
finding: an aggregate in one marker still flags only that marker. A region
nothing measured -- no
detector, check or trace, only your words -- is never excluded automatically;
it is a warning until the user approves it.
Source files are written only by `write_qc_to_source`, only when asked.

Automatic gating reads these calls: every fit, sample and picture it makes
leaves out the cells QC excluded or warned about (`get_qc_exclusions` counts
them). So a QC change -- a region drawn, an action changed, strictness moved
-- makes gates decided before it stale (`gating_qc` `stale_qc`). Say so when
the image is already gated.

## Provenance

The category is the user's word, the class yours: a region sits in one of
the five categories (`blur_focus`, `registration`, `segmentation`,
`tissue_acquisition`, `staining_signal`) and keeps the class you named as
its subtype. Every region records its detector or check and version, the
score and the bar it crossed with that bar's `threshold_source` (`auto`, or
`agent_refined` when your looks moved it) and `offset_steps`, your class,
severity, confidence and scope, the strictness it was decided under, the
evidence artifacts, and how its outline was made (traced by which method and
how much of the envelope it keeps, the check's map, or the envelope and
why). The `notes` you give with an answer are kept with what it decided --
the region, the check -- and shown to the user as the
region's or cell's one-line explanation when they hover it in the viewer:
write them as that line (what you saw, in a sentence or two of plain
words). The result's `checks` holds each check's bar, the rows you
judged in every round, and what became of its regions. `get_qc_results`, the
report and `export_qc` with `what: "provenance"` show them.

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
- Moving a bar to chase a number: `too_lenient` and `too_aggressive` are
  about what the borderline tiles show, never about how much tissue the bar
  flags.
- Calling a check's far-above row an artifact because its score is high:
  judge the tiles; a high Blur Score on sparse tissue can be normal.
