# Automatic quality control

*For the people building Plexora. How "QC this image" works end to end, where
each piece lives, what is deterministic, what an agent is asked, and the rules
nothing may break. The design document this was built from is the plan of
2026-09-28; this page describes what exists.*

---

## 1. The shape of it

Quality control is the second server-driven decision session, built on the
same harness as automatic gating (`plexora/agent/sessions/`): the server does
every step code can do and hands an agent **one typed decision packet at a
time**; the agent judges, the server moves on deterministically.

```
qc_session_start ──► bulk job: calibrate display, pyramid scan of every channel,
                     detectors, candidates merged and ranked, the image checks
                     scored (blur, registration, segmentation)
qc_next ───────────► one packet: question + compact JSON + <= 2 images
qc_answer ─────────► typed answer -> transition -> (write ROI) -> next packet
qc_session_finish ─► close | commit | cancel | rollback; the result becomes active,
                     cells' calls written
qc_report ─────────► HTML + PDF with every denominator stated
```

**Deterministic code finds where to look; the agent decides what it means.**
Where a local check scores the whole tissue (Blur QC, the Registration Check,
Segmentation QC), the agent does not rediscover the problem: it is shown a
few places sampled across the score's distribution and says whether they are
artifacts or normal variation, and whether the bar sits right (§5, §13).
The agent never types a coordinate or a threshold: it says a channel is clean,
an outlined region is (or is not) an artifact and of what class, which
channels it reaches, which proposed outline covers it, which grid squares it
covers, and whether the cells beside a proposed cutoff are artifacts or
biology.

QC is an **annotation layer, never a removal**. A confirmed artifact is an ROI
in the ROI plugin (the canonical geometry, editable by the user); a cell gets a
pass/fail call with reasons. Nothing is deleted from the user's data; writing
the calls into the user's file is a separate, explicit, source-write action.

## 2. Where things live

| Piece | Module |
|---|---|
| The shared harness | `agent/sessions/{engine,tools,memo,events,control,mirror}.py` (gating's `Engine` is a `BaseEngine` too) |
| Vocabulary, states, strictness presets | `plugins/qc/server/schemas.py` |
| The scan | `plugins/qc/server/scan.py` (maps), `cycles.py` (cycle inference) |
| Detectors | `plugins/qc/server/detectors/` (`base.py` interface, `classical.py`, entry point `plexora.qc_detectors`) |
| Candidates and outlines | `candidates.py` (merge, rank, ids, caps), `polygons.py` (mask -> GeoJSON, variants, grid) |
| Evidence | `sheets.py` on `agent/evidence/sheet_layout.py`; `render_region` draws `RenderInput.shapes` |
| The session | `engine.py` (units, next_unit, decide, writes), `packets.py`, `answers.py`, `transitions.py`, `bulk.py`, `finalize.py`, `mirror_script.py`, `events.py` |
| Cells | `cells/calls.py` (`derive`), `propagate.py` (ROI -> cell overlap) |
| Storage | `results.py` (the QC store), `roi_link.py` (ROIs, user edits, adoption) |
| Files | `export.py`, `source_write.py`, `report.py` |
| Tools | `capabilities_session.py` (Paid), `capabilities.py` (Free + analytics) |
| The free image checks | `server/registration.py`, `server/blur.py`, `server/segqc/`, `capabilities_checks.py` |
| The checks as one shape | `server/score_fields.py` (fields, bars in steps, regions, strata), `score_review.py` (the sampled look, shared by the session and `sample_qc_examples`) |
| The checks in a session | `server/checks_bulk.py` (scored in the bulk pass, detector fallback), `check_candidates.py` (regions as candidates on the check's grid), `checks_result.py` (`result.checks`) |
| Categories and provenance | `schemas.CATEGORIES`, `CLASS_CATEGORY`, `CELL_REASON_CATEGORY`; `server/provenance.py` (the one builder the panel, `get_qc_results` and the exports read) |
| Panel and routes | `server/routes.py`, `static/qc*.js`, `templates/qc/panel.html` |
| MCP | `plugins/qc/mcp.py` through `Plugin.mcp_factory`; skills `ai/skills/qc-image`, `review-qc`, `qc-checks` |

The QC plugin never imports `plugins/gating` (the boundary golden pins it).

## 3. The scan

Every pixel is read through `SourceImage.read(channel, level, box)`, block by
block (at most `BLOCK_PX` a side, with a `HALO` so neighbourhood filters are
exact), so node-hosted images work and memory is bounded.

- **Overview level** (`image_qc.overview_level`, about a megapixel): global
  numbers per channel, the tissue mask (the nuclear stain smoothed over
  `TISSUE_SMOOTH_UM`, then Otsu; small holes filled; components under a percent
  of the largest dropped), and the cross-cycle checks (phase correlation of
  each cycle's nuclear stain against the first, globally and on an eight by
  eight block grid; tissue lost where a cycle is four times dimmer than the
  first predicts).
- **Map level**: map cells of `CELL_UM` (50 µm; `CELL_PX` without a pixel
  size), read where one cell is about sixteen pixels. Per cell: mean, p10,
  median, p90, p99, saturation, zero fraction, focus (Laplacian energy over the
  cell's own log-variance, so the amount of stain does not look like
  sharpness), contrast, compact bright objects (white top-hat of radius
  `TOPHAT_UM`, smaller than a nucleus). Derived: relative focus, background,
  the illumination surface and residual, diffuse brightness (local against a
  surround that treats glass as tissue median). Tile seams are not detected:
  the user judged them not worth the false alarms (a round core's rim reads as
  one). The `stitching_or_tile_seam` class stays for hand-drawn and earlier
  regions, and `SCAN_VERSION` was bumped so scans that carry a seam map recompute.

The result is stored once per fingerprint (image identity, grid, channels,
pixel size, parameters, cycle override) as
`<agent_root>/qc/<project>/scan_<fp>.npz` + `.json`; a rerun reads it back.

## 4. Detectors and candidates

A detector (`QCDetector`: `available(context)`, `run(context)`) proposes
regions on the map grid with a class hint, a scope hint and a severity; it
never decides. The classical ones: focus, saturation, aggregate, diffuse
bright (fold / autofluorescence / background by how many channels agree),
illumination, dark tissue, empty channel, registration, cycle tissue
loss. Their cut-points (`DETECT`) are permissive on purpose: the audit catches
what they miss, and the agent dismisses what they over-call.

`candidates.build` cleans area masks, merges the same place across detectors
and channels before anything is shown (IoU, or containment within a size
ratio; whole-channel and grid-line classes merge only with their own class --
one shading pattern in twelve channels is one candidate carrying the channel list, and
the scope question settles which it affects -- never into local ones; a
failed channel never merges; a merged outline grows only by members that are
the same place), ranks by severity and area, and gives each a content-hash id
(`cand_<sha1>`) so a rerun's memo keys match. Caps per channel and per session;
what is dropped is summarised as `residual` for the report.

## 5. The session

Units: **channel** (audit), **check** (one per image check and channel: Blur
QC per nuclear channel, the Registration Check per comparison, Segmentation
QC once), **candidate** (confirm, scope, localise, grid), **final** (one
review). `next_unit`: audits in batches of
`ENGINE["audit_batch"]` channels a sheet, two sheets a packet; then each
check awaiting its `score_review` (checks in `schemas.CHECKS` order, then
channel order); then candidates in channel order by score -- first looks up to
`ENGINE["confirm_batch"]` to a sheet, one row each, answered by label (same
class first, then same channel); deeper looks and reopened regions alone; then
the final review.

- **Audit**: a clean row dismisses its candidates, except those at or above
  `force_confirm_score` that an audit tile cannot show (`schemas.OVERVIEW_BLIND`
  at any size, other local classes at most `overview_small_fraction` of the
  tissue; shading, background and failed stains never);
  a candidate drawn on several rows is kept when any row names it;
  `suspicious` keeps the named ones (`elsewhere` opens a grid over the tissue);
  `uncertain` opens a whole-channel look. Every channel is looked at, whatever
  the detectors said.
- **Confirm** (three looks: overview/neighbourhood/crop/map, then nuclear and a
  matched clean field): `artifact` stores the judgment; then scope if several
  channels could be meant, localisation if the outline does not cover it, then
  the decision. `cannot_tell` and an exhausted allowance are **manual review**:
  a warning region of class `uncertain_manual_review`, never an exclusion.
- **Decide**: `strictness.decide_artifact(decision, measurement, table)`; the
  ROI is written (apply mode) in its class's category `qc_<category>` (§13),
  named with its action and subtype (`QC exclude: tissue fold · CD8`), as a child receipt with the `delete_roi`
  undo hint `roi.create` issues; a `roi_meta` row keeps what the ROI schema
  has no field for, with the geometry hash it was written with.
- **Final review**: `inconsistent` reopens the named regions once, at the
  deepest look.
- **Score review** (one check unit): the sheet holds a row per stratum
  (`SCORE_STRATA`: clearly fine, just below and just above the bar, far above
  it, inside the largest regions) and the whole tissue with the regions and
  the score map. `too_lenient` / `too_aggressive` move `offset_steps` by one
  (clamped to `adjust_max_steps`, `threshold_source` `agent_refined`) and look
  again, at most `score_rounds` looks; a move on the last look is applied and
  its newly borderline regions are confirmed, not decided. Then
  `transitions._settle_check`: far-above and just-above `artifact` -> every
  region decided by the review (`ai_decision.source` `score_review`); just-above
  mixed / unclear -> regions `score_direct_confirm_margin_steps` clear of the
  bar decided, the rest `artifact_confirm` (bounded by
  `candidates_per_channel`, overflow to manual review); just-above `normal` ->
  the rest dismissed; far-above not `artifact` -> every region confirmed; every
  row `cannot_tell` -> the `check_max_manual_regions` largest for manual
  review; `whole_tissue: artifact` -> one region over the tissue. A decided
  check region takes in the detector candidates of its class lying inside it
  (`_absorb`), before a look is spent on them. Every region the settle
  creates (decided, to confirm, manual review) carries the review's
  `artifact_class` as its `class_hint` and its severity (`review_hint`).
  **Probes** (`check_candidates.hold_for_probes` / `settle_held`): of a
  check's fresh to-confirm regions only the `check_confirm_probe` strongest
  (score, then id) are asked; the rest are held (`held_for`, never asked by
  `next_unit`). When every probe comes back the same -- all `not_artifact`,
  or all artifacts of one class -- the held regions take that verdict without
  a look (`extrapolated`: the rule and the probe ids; a decision's `source`
  `extrapolated`); otherwise (or when a probe ended in manual review) they
  are released and confirmed one by one. The overflow past
  `check_confirm_per_channel` stays warning regions, summarised once on the
  check (`manual_overflow`: counts and the three largest).
- **What a check samples**: a stratum place, and a region's peak (where its
  confirm crop goes), must hold the check's content -- Blur QC's cell needs
  `min_nuclear_fraction` of its pixels above the channel's own Otsu level
  (nucleus-free cells are not evaluable at all), registration the nuclear
  share both cycles hold; at least `SAMPLE_CONTENT_OF_MEDIAN` of the
  field's median content (`ScoreField.sampleable`). The Registration Check
  scores a map cell only where both cycles have nuclei in balance
  (`MAP_BALANCE`); where one cycle has nuclei and the other under
  `ONE_CYCLE_RATIO` of them (`registration.one_cycle`) is tissue lost in a
  cycle (or debris), turned in the bulk pass into
  `cycle_specific_tissue_loss` candidates scoped to the cycle that lost them
  (`check_one_cycle_regions`, `check_one_cycle_min_cells`). A registration
  region's confirm crop is the two cycles red / green; a crop that shows
  nothing (`sheets.visible`) moves to the region's strongest tissue. A check with nothing at or
  within a step of its bar and no `global.possible` is closed in the bulk pass
  without a look.

Budgets per candidate (`QC_UNIT_DEFAULT`); the audit, scope and final review
are free. The limit policy (ask / extend / stop) is the shared one.

### Registration in a session

- **Only on resolved cycles.** `cycles.resolved` asks for the user's groups or
  repeated nuclear channels at equal gaps (`RESOLVED_CONFIDENCE` 0.9). Below
  that the Registration Check is skipped (`checks_bulk._Unresolved`, no
  detector fallback) and so are the scan's `registration` and `tissue_loss`
  detectors: a verdict on a guessed DNA assignment is worse than none.
- **Against the segmentation DNA channel** (`capabilities_session.
  registration_reference`): the masks were drawn on it, so it is the frame a
  cycle is out of.
- **A whole-cycle shift is a channel verdict** (`check_candidates.global_unit`,
  `channel_level`): recorded in the result, never drawn; `cells.calls` flags
  the cycle's markers in every cell. Local regions of that channel are not
  made (the verdict covers them).
- **A local region is outlined by its nuclei** (`nuclei_trace`): every
  reference nucleus in the envelope is read in both cycles and called lost,
  displaced or aligned once; a registration region keeps its displaced nuclei,
  a one-cycle (tissue-loss) region its lost ones, so the two never claim the
  same nucleus. Nuclei are the mask's labels, else the reference's own blobs;
  a region too large to read or with too few nuclei keeps its map outline.
- **One table** (`checks_result.registration_table`, in the report and in
  `get_qc_results` as `registration`): per DNA channel, its reference, the
  verdict (aligned / shifted / local / skipped and why), the whole shift in
  microns, the local places and tissue lost with their nuclei, the markers
  and cells it costs.

### Consolidation

At `qc_session_finish` (close or commit, apply mode), before the cells are
counted, `consolidate.run` turns the session's written findings into the
smallest set of non-overlapping ROIs: the tissue they cover is partitioned,
each place going to the best-fit explanation (`consolidate.rank`: whole-cell
classes, then registration, focus, signal / staining, the rest, needs review;
then action, confidence, area), and every finding of one class and channels
becomes one (multi-part) ROI. Each consolidated ROI (`origin: consolidated`)
lists its `findings` -- class, channels, the part of it each covers -- its
description names them all, and its channels are all of theirs. The findings
stay in the result (`consolidated_into`, their ROIs deleted with receipts);
the cells follow each finding inside its own outline clipped to the ROI as it
stands, with its own class, channels and current action
(`roi_link.membership_meta`, member keys `<roi id>#<n>`). Nothing runs when
no two findings overlap (by `MIN_OVERLAP_SHARE`) or share a class and
channels; user-drawn, edited, approved or locked regions are never touched.

## 6. Strictness

Presets change thresholds only (`schemas.STRICTNESS_PRESETS`, directions in
`STRICTNESS_KEYS`, monotonicity asserted at import; a custom table outside the
lenient..strict band is refused). Packets carry no strictness. The agent's say
on a cell cutoff is stored as `offset_steps` and a `veto` per side, applied on
top of any preset's proposal (a step is `max(offset_step_mad` MADs,
`offset_step_share` of the cutoff's distance from the median), never nearer
the median than `offset_min_keep` of it), so **Strict ⊇ Standard ⊇ Lenient** holds by
construction (property tests in `tests/test_qc_results.py`).
`set_qc_strictness` re-derives every region's action (renaming its ROI, never
moving it between categories) and every cell's call from stored measurements
and stored membership fractions; no packet, no mask read. The user's own and
approved regions keep theirs.

## 7. Cells

Every call is at one of two levels (`schemas`): a **cell** reason fails or
warns the whole cell (the tissue under it is folded, torn or gone; its nucleus
is not a nucleus; the object is not one cell); a **marker** flag says one
channel's value is unreliable in that cell and leaves the cell, and its other
markers, alone. Nothing that concerns one channel ever fails a whole cell.

There are no per-cell modules: a cell fails or warns only through the
regions it sits in and the channel-level verdicts (`cells/calls.py`).
Segmentation QC runs on its own (`segqc/`) and reaches the cells through its
cluster regions. A result saved before may still hold `cells.modules`;
`calls.derive` ignores it, and `strictness.RETIRED_KEYS` drops retired keys
from an old custom table.

Regions (`class_rules.region_level`): a class in `WHOLE_CELL_CLASSES` (the
physical ones and `segmentation_error`), a region
scoped to every channel, or one reaching the segmentation nucleus fails whole
cells (`region:<class>`, membership `cells.roi_overlap_fraction`). A region
scoped to some channels flags those markers only. For a class that adds signal
(`SIGNAL_RAISING_CLASSES`) the region must be **borne out by the cells**: its
cells (any overlap) are compared with the cells in a ring round it, outside
every region of that marker (`MARKER_EVIDENCE`: a one-sided rank test for a
shift, a binomial test on the ring's 99th percentile for a tail, p <= alpha);
then only the cells above the ring's `cells.marker_quantile` are flagged. A
region the cells do not bear out flags nothing and is listed with its numbers
(`not_borne_out`, the report's "Regions the cells do not bear out"). A channel
the table does not measure is never flagged.

Classes need their evidence (`class_rules.supported_class`, in
`engine.decide`): autofluorescence is kept only with an autofluorescence /
blank / unstained channel or the same structures bright in two or more
markers; otherwise it is recorded as `excessive_background`, with
`decision.class_adjusted` saying why.

Region membership (`propagate.py`): the fraction of each cell's mask inside
the polygon (holes honoured), read at the smallest level within
`MAX_ROI_PIXELS`; centroid fallback without a mask or past the budget
(recorded as `roi_method`). A cell in several regions lists them all; exclude
beats warn; nothing is counted twice.

`qc_cells` holds per cell: `pass`, `action`, `primary_reason`
(`schemas.PRIMARY_ORDER`), `reasons` and `excluded_by` (whole-cell),
`reason_count`, `unreliable_markers`, `marker_flags` (`marker|reason|status`),
`roi_ids`, `roi_method` and the `m_*` measurements. The result's `cells`
summary keeps what every call rests on: `evidence` (per reason: channels,
cutoffs, verdicts, regions) and `marker_evidence` (per region and marker:
the test and its numbers) -- the channels the viewer shows on a click.

## 8. The user's edits win

`roi_link.sync` runs before every read and change: a QC ROI deleted drops out
of the cells' reasons; edited (geometry hash differs) is kept as drawn and
never auto-updated; moved to another of the five categories takes that
category's default class (moved within its own category it keeps its
subtype); moved out of QC is no longer QC; locked is approved. A region the
user draws in a QC category (matched by id `qc_<category>` or by label) is
adopted as a user-made candidate and always excludes -- **manual QC needs no
session and no licence** (`refresh_qc`). A subtype picked in the panel rides
in the notes as `qc-class:<class>` until adoption. Take-over from the viewer
locks the region under review and the session closes it as the user's.

ROI categories are the five (`qc_blur_focus`, `qc_registration`,
`qc_segmentation`, `qc_tissue_acquisition`, `qc_staining_signal`), `qc_review`
and `qc_custom_*`; the class lives in `roi_meta.class`, the candidate, the
name and the notes. A project written before the five (`qc_<class>` ids,
"QC: <Class>" labels) is migrated on its first write
(`roi_link.migrate_categories`): one ROI revision reassigns every region and
deletes the emptied categories, the rows' `written_category_id` move in the
same step (so it is not a relabel), a region QC never adopted gets a
`qc-class:` token, and a colour the user gave a legacy category is carried. A
read (`sync(save=False)`) reports `legacy` instead; the panel then refreshes
once. A half-written migration (regions moved, rows not) is recognised by the
same-category rule and repaired.

## 9. Storage and files

`api.store(<project>, "qc")`: the document (results, the active one, the
strictness, the cycle override; newest `KEEP_RESULTS` inline, older archived
to files), `roi_meta`, `qc_cells`, `qc_cell_rois`. Session folders are swept;
results are not. `reset_qc` snapshots first and never deletes a region the
user drew, edited or locked; `restore_qc` puts it all back.

Exports (`export_qc`, `what` rois | cells | provenance | both): `cells.csv`
(with `qc_category`, `categories`, `flag_source`), `qc_regions.geojson` (with
category, subtype, score, threshold and its source, the agent's verdict,
cells), `qc_provenance.json` (`provenance.document`: vocabulary, checks,
every region, cell reason and marker reason), `qc_findings.csv` (the same,
one row each), `summary.json` (with the checks), `result.json`. Source write
(`write_qc_to_source`, `source_file_write`): AnnData `obs["plexora_qc_pass" |
"_primary_reason" | "_category" | "_reason_count" | "_unreliable_markers"]`, `obsm["plexora_qc_flags"]` (a boolean DataFrame, one
column per whole-cell reason), `obsm["plexora_qc_marker_flags"]` (one boolean
column per marker: unreliable in that cell), `uns["plexora_qc"]` (with cell and
marker definitions and the category vocabulary -- a file written before it
has the old keys, so rewriting needs `replace`); CSV/Parquet get the scalar columns,
`plexora_qc_reasons`, `plexora_qc_unreliable_markers` and
`plexora_qc_marker_flags`. `plexora_qc_pass` is about the whole cell: filter on
it and on the markers you read. Rows of other images are `<NA>`; existing QC keys are
refused without `replace`; `obs` is backed up first.

## 10. Licensing

`ai:qc:session` (the session tools and the report) and `ai:qc:analytics`
(`profile_image_qc`, `render_qc_overview`, `refine_qc_roi`,
`sample_qc_examples`) are Paid; the image checks and their writers are Free; everything that reads,
changes or exports what QC made -- and manual QC -- is Free and survives
expiry (`tests/test_licensing_enforcement.py`, `test_licensing_hardening.py`).

## 11. Rules nothing may break

1. The scan, the detectors, the candidates and every sheet are deterministic;
   mirroring never enters a memo key.
2. No packet carries strictness.
3. A limit reached, an unreadable answer or an unclear look is a warning for
   manual review, never an exclusion, and never evidence lost.
4. The user's regions and edits are never changed by QC.
5. Membership is by overlap where there is a mask, and says when it was not.
6. Every percentage is stated against its denominator.
7. Nothing about one channel fails a whole cell; nothing excludes a cell
   without a look; no label rests on a channel that does not exist or a
   region its cells do not bear out; every call keeps its evidence.
8. Every finding maps to one of five categories; the subtype is never lost.
   No threshold is typed by an agent: a bar moves in steps, and records where
   it came from.

9. QC removes nothing. A consumer may leave QC failures out of what it
   *estimates* -- automatic gating does, below -- but never out of what it
   *applies to*.

## 11b. Who reads the calls

`server/exclusions.py` answers `Plugin.cell_exclusions_factory`
(plexora/agent/cell_exclusions.py): for a mode (`strict`: exclude and warn
calls, and per marker the cells its `marker_flags` name with either status;
`exclude`: exclude calls and exclude flags) it returns the cell ids to leave
out, keyed by a fingerprint of the active result and the store's revision.
Automatic gating is the consumer today: its fits, samples, collages and
validation fields are made on the QC-passed cells (AUTOMATIC_GATING.md,
"The fit ignores QC failures"); `get_qc_exclusions` reports the same counts.

The calls must describe the regions as they stand. `calls.write_for_active`
stamps the ROI document's hash on the result (`cells.roi_revision`); a
provider asked about a result whose stamp no longer matches -- a region drawn,
reshaped or deleted in the ROI panel with no `refresh_qc` since -- runs the
same sync and derivation `refresh_qc` does before answering. A region drawn
by hand in a QC category therefore leaves its cells out of the next gate fit
without any QC tool being called.

## 12. The free image checks: Registration Check, Blur QC, Segmentation QC and the Artifact Detector

Four folds at the top of the QC panel, shown with or without a QC result,
Free, and the same capabilities for the panel and an agent
(`capabilities_checks.py`). None touches the QC document until asked (Blur
QC's `write_blur_regions`): their state and
results live in files under the QC store directory, so reading or toggling
them never moves `revision` (a running session or a strictness edit is never
refused over a key press).

**Nuclear channels** are named by one rule, `agent/presets.is_nuclear_name` /
`nuclear_channels` (the vocabulary's DNA entry, or DAPI / DNA / Hoechst /
nuclear as a whole token with any prefix, suffix or cycle number; `pDNA`,
`DNase`, `DNA-PK`, `Nucleolin` are not). `cycles.is_nuclear` is that rule.

**Registration Check** (`server/registration.py`, `static/qcRegistration.js`).
State in `registration/state.json`: on/off, the rule, reference (any channel;
default the first nuclear one), comparison (default the next), thresholds
(2 µm, or 4 px without a pixel size), block size, overlay, flicker (on at every
activation; `F` pauses), colours (a colour set through the tool is the user's
and stays). The panel mirrors it: reference in slot 1, comparison in slot 2,
slots 3+ untouched, slots 1-2 put back when it is turned off; a slot the user
coloured keeps its colour. `Z` / `X` step the comparison only while QC is the
selected tool (gating's guard). Flicker is zebra stripes crawling over the
pixel disagreement (`/registration/disagreement`: an RGBA PNG, alpha the
disagreement) -- an overlay repaint, never a tile refetch -- inside the blocks
displaced past the threshold, and anywhere the disagreement is dense
(`dense_mismatch`: at least 15% of the nuclear pixels in a 25 µm patch; the
PNG's blue channel is 255 there). Dense mismatch is a cell or two that moved,
deformed or lifted between cycles: it cannot shift an 80 µm block, so without
it the stripes stayed blank on an image with 0% displaced. The stripes stop on
pause, off, a hidden panel or tab, or a lost focus.

The heatmap draws the MISMATCH MAP, not the block shift: `mismatch_map`, built
in the same compute as the field and cached with it (field `VERSION` 2), is per
~6.5 µm cell the share of nuclear pixels whose stains disagree, smoothed over
~8 µm -- the stripes' measure, pooled, so it heats wherever disagreement
crowds. It travels as two base64 byte planes in `overlay.mismatch` only when
the caller passes `include_map` (the panel does; an agent gets
`stats.dense_mismatch_pct` and `stats.mismatch_hotspots` instead). The ramp is
in %, auto from 0 to the map's 99th percentile over tissue (never under 20%).
The arrows still show the shift, coloured on the shift's own scale. The block
shift heatmap it replaced painted sub-pixel noise: on a well-registered image
every block is under the threshold, and the ramp stretched 0.2-1.3 px of
measurement error across its colours.

The mismatch field: the two planes at an overview level (finer while the
threshold is under a level pixel, at most 4.5 MP), log and a 1 px Gaussian;
one Hann-windowed phase correlation for the global shift (parabolic sub-pixel,
peak-to-sidelobe confidence); the comparison pre-aligned by its integer part;
then one batched FFT phase correlation over every block. A block's
displacement is global + local, so a cycle shifted everywhere is `widespread`,
not clean. Block states: not evaluated (under `min_tissue` of tissue, the
scan's `tissue_estimate`), uncertain (low confidence), ok, highlighted.
`highlighted_fraction` = tissue of highlighted blocks / tissue of ok +
highlighted blocks (`denominator: "evaluated_tissue"`). The field is cached per
image, pair, level and block size (memory, then `registration/<fp>.npz`);
thresholds apply afterwards, so changing one re-reads nothing.

**Segmentation QC** (`server/segqc/`, `static/qcSegmentation.js`). One
framework: DNA peaks against the mask's labels on their adjacency graph.
Sizing reads a 4 x 3 grid of blocks: the median label area at level 0, then
the **nuclear** scale by scale selection on the DNA, at the coarsest level
where a label is still >= 16 px (finer, chromatin texture dominates): each
label votes with the scale of its strongest DoG scale-space maximum, and the
mode (parabola-refined) is the nucleus (`d_nucleus_px`, `scale_method: "dna"`;
under 50 votes it falls back to the label size, `"labels"`). Never the label
size by default: a whole-cell mask's labels are twice a nucleus across, and
smoothing at their size erased the gaps between packed nuclei (v1 gave a peak
to a quarter of the labels on `lsp70267` and flagged 5.9 % over, 39 %
ambiguous). The run level is the coarsest with nuclei >= 5 px and <= 1 µm/px.
Per 2048 px tile with a halo wider than a label and every filter: DoG maxima
over log DNA at the nuclear scale above one global floor (a fifth of the
sample's median nuclear peak), their label, and for each label whose strongest
peak the tile owns, the second peak, separation and valley depth; brightness,
mass and boundaries are read on a fine plane (half the scale). Compiled kernels
(`segqc/kernels.py`) accumulate per-label area / DNA / centroid / perimeter /
brightest DNA / DNA mass above the tile's background, and every label-label
boundary pair exactly once (length and brightest pair per edge). Context is
each cell's 12 nearest neighbours (area, the share with two peaks, and over
those with a peak the median DNA mass and peak strength). Under: two robust,
separated, similar peaks in a label large for its neighbourhood, with a DNA
valley between them required (an elongated nucleus has two maxima and no
dip), damped where two-peak labels are common. Over, per edge, a product of
necessary conditions -- the strong side has a peak; the cut's brightest DNA
is >= ~0.5 of the strong side's brightest (it runs through a nucleus, not
cytoplasm: a dim neighbour is not a fragment); not two peaks a nucleus apart;
the weak side holds a real share of the pair's DNA mass; the pair together
holds about one local nucleus's mass, not two; softened by how much of the
weak label's perimeter the cut is. The **blob rule** adds big nuclei cut into
several labels (each piece holding a normal nucleus's mass, invisible to the
pair test): coarse-DoG maxima (2 and 3.5 x the scale) with no nuclear-scale
structure within 0.8 R; members are labels with a centroid within 0.9 R, the
one nearest the centre is home, and a member is a piece when it holds >= ~10 %
of the blob's mass, is nearly as bright as home and has no nucleus-strength
peak (< ~0.55 of its neighbours' median -- a gland wall of packed nuclei is a
coarse blob too, but each nucleus has a full peak). Only the weak side / piece
is flagged, with `partner_id`. >= 0.6 flags, 0.4-0.6 is ambiguous (counted,
not drawn). When < 50 % of the DNA peaks fall on a label, the summary carries
a `notice` (a cell-ring or cytoplasm mask), shown on the panel row.
Results: `segqc/<fp>.parquet|.json`, `current.json`; fingerprint = image and
mask identity (path or node resource, size, mtime, scale), DNA channel,
parameters, `VERSION`. Cancellation is checked per tile; an image or mask that
changes mid-run is refused (`conflict`). Summary keeps `pct_cells` and
`pct_area` apart. Export adds `seg_qc_*` columns to `cells.csv` and
`segmentation_qc` to `summary.json`; a source write adds
`plexora_seg_qc_status | _under_score | _over_score | _partner_id` and
`uns["plexora_seg_qc"]` -- either block may be written alone.

**Blur QC** (`server/blur.py`, `static/qcBlur.js`). One result per listed
channel -- the ones picked (`settings.json` `channels`, at most 12), else the
first three nuclear ones; brightfield reads the darkness plane, listed as
`brightfield` -- each with its own threshold and colour. A channel is read at
the level nearest
0.5 µm/px (level 0 without a pixel size; coarsened past 400 MP), on a grid of
40 µm cells (96 px without a pixel size) built in `choose_map_grid`'s order so
`iter_blocks` and `polygons` read it. Per block, haloed by the widest filter's
reach: validity (fluorescence `> 0` and under 98 % of the dtype's ceiling;
brightfield only the saturation test, so glass counts), a support mask eroded
by that reach, and Sobel energy after Gaussians of 0, 1 and 2 px, summed per
cell in float64. Per cell: mean energy per scale, less the glass's median
(cells with < 5 % tissue, the nuclear stain's `tissue_estimate` whichever
channel is analysed; with < 30 of them no correction). A cell is evaluable with
>= 50 % valid pixels, >= 50 % tissue and coarse energy >= 4 x the glass's.
`fine_share_s = max(E'_s, E'_coarse) / E'_coarse` -- floored at 1, since
smoothing only removes gradient, so a fine energy lost to the noise
subtraction reads "no fine detail" and never an infinite deficit. The
reference is the median fine share of the top 15 % of evaluable cells (at
least 20; under 40 evaluable cells the result is `insufficient`). A cell's
deficit is `log(R_s / fs_s) / log(R_s)` clipped to 0..1 (the share of the
image's own fine-detail range lost), averaged over the fine scales; the tile
score is the mean deficit of the evaluable cells of the 3 x 3 around it --
pooled in the log domain because summed energy let a tile's sharp part
outweigh a blurred part (a 5 px blur read 0.2 that way, 0.5-0.7 this way) --
then a masked Gaussian (0.7 cells). Auto threshold: median + 3 x 1.4826 MAD,
floored at 0.35, within 0.2-0.8. `global_blur.possible` when the reference's
finest share is under 1.6 (sharp synthetic tissue ~3.7, the same blurred
everywhere by 3 px ~1.4) or the result is insufficient. Stored:
`blur/<fp>.npz|.json`, `current.json` (`by_channel`: each channel's last
fingerprint; the sweep never drops a pointed one), `settings.json`
(`channels`, `per: {channel: {threshold, color}}` -- an absent threshold is
the automatic one, an absent colour the palette's, core's swatch presets in
order -- `min_region_tiles`), `running.json` (the job and its channels); the
fingerprint covers image identity and stamp, channel,
level, grid, parameters and pixel size, never the threshold. `evaluate`
thresholds the stored scores (8-connected, >= 4 cells a region, `blurred_pct`
= tissue in flagged cells / tissue in evaluable ones). The panel lists one
line per channel, as the image channels are listed -- swatch, name, its
slider, the blurred share, an eye -- and a + that adds the next nuclear
channel (and measures it once the others are); a slider previews through
`/blur/mask?channel=` and commits through `set_blur_check`. Play runs every
listed channel in one job (an unchanged one is reused).
`write_blur_regions` writes every listed channel's regions at its own
threshold (or one channel's) as candidates with `detector`/`created_by` `blur`,
`severity` None (a Blur Score is not a severity) and the action pinned
(`user_state.approved`, `approved_action: exclude`, as `approve_qc_roi` pins
one), so a strictness change never renames them; a rewrite bulk-deletes the
blur ROIs of the channels written (the meta row's `channels`) that nobody
edited, locked, renamed or moved (not through `delete_roi`,
which the viewer's policy refuses) and keeps the rest. Each region has a child
receipt (`<op>.NNN`) whose undo deletes it.

**Artifact Detector** (`server/artifacts.py`, `static/qcArtifacts.js`): folds,
tears, debris and saturation as snug scored objects across every channel,
after the standalone `artifact_scan.py`, made multi-channel. Needs a pixel
size (`precondition_missing`, `detail.hint: params.pixel_um`) and refuses
brightfield (`unsupported_modality`). Four categories, each with its class:
`fold` (`tissue_fold`), `tear` (`tissue_damage_or_detachment`), `debris`
(`debris_or_foreign_object`; `metrics.shape` `fiber` or `compact`) and
`saturation` (`saturation_or_clipping`, one channel each). Defocus is Blur
QC's; the script's haze is not kept.

*Stage 1* reads each channel once, whole, at the finest level whose longer
side is <= 4096 px (and <= 2e7 px): the smoothed log intensity (10 µm) is a
robust z against the provisional tissue (the first nuclear channel's
`tissue_estimate`), counted into two uint8 planes (`agree_bright` /
`agree_dark`: channels with z > 1.5 / < -1.5 per pixel). A subset of
channels (`pan_subset`: one nuclear per cycle, at most three, then the others
evenly spaced, `pan_channels` = 8 in all; the same subset at both stages) is
averaged into the *pan*, each log channel over its tissue p99, clipped at 1.5.
A channel's ceiling (`_effective_ceiling`) is its dtype maximum unless the
data hold a lower standard full scale exactly (12-bit data in uint16 clip at
4095); pixels at 0.35 x it are saturation seeds (packed bits). From the pan:
the tissue (Otsu on the pan smoothed to a sixth of 60 µm, closed, components
>= 1 % of the largest, holes filled), the analysis region (the filled tissue
opened by 200 µm -- a plain opening, so attached ribbons are severed -- then
grown 100 µm) and the glass (the region more than 40 µm off the tissue).
Seeds: **fold** -- the pan smoothed at 20 µm over 2.5 robust SDs of the
tissue core, opened, >= 1e4 µm², agreement (mean `agree_bright` share) >= 0.5;
**tear** -- inside the body >= 60 µm, at most 0.35 of the tissue's signal
over the glass or darker in >= 60 % of channels, and >= 1.5 tissue SDs under
the tissue, >= 2e4 µm²; **debris**, on the glass only (on tissue it collides
with collagen and bright cells) -- fibers by a white top-hat (40 µm) z-scored
against the top-hat's own spread on the glass (the raw glass spread joined all
the glass into one: a top-hat of noise sits a couple of SDs up everywhere),
hysteresis 2.5 / 3, speckles dropped, a 6-angle line close (80 µm), then long
(>= 250 µm) and either thin (axis ratio >= 5) or branched (elongation >= 4
with solidity <= 0.6); compact debris over 3 glass SDs, 150-5e4 µm², solidity
>= 0.35. Every per-object statistic is one `bincount` or one sort over a label
image (`_label_mean`, `_label_quantile`); there is no loop over pixels or
objects in the detection.

*Attribution* re-reads each channel once and takes every seed's mean z
(tissue-relative for folds and tears, glass-relative for debris): `channels`
(z >= 1, <= -1 for a tear, >= 3 for debris), `source_channel` (largest |z|)
and the fold's nuclear gate (a nuclear channel's z >= 1).

*Stage 2*: seeds padded by 200 µm are grouped by painting their rectangles on
a coarse canvas (a summed-area paint and one labelling, never pairwise), and a
group whose box no finer level affords is split along its long axis. Each box
is read at the finest level where box pixels x pan channels <= 2.4e7 (between
0.5 and 8 µm/px), and each seed is regrown by hysteresis from its seed --
never ORed back in, since the coarse seed is smoothed wider than the
artifact: a fold over the share of pan channels raised against their own
400 µm neighbourhood (>= 0.28) where the pan is bright; a tear over the
tissue-depth z; debris over the top-hat (fibers) or contrast (compact)
against the glass ring round it. A seed the fine evidence loses keeps its
upsampled outline (`refined: false`, `refine_reason`); so does one past
`MAX_CANDIDATES` or the 2e9-pixel budget. Saturation is confirmed per channel
at full resolution at 0.98 x the ceiling (aggregates at 0.92 x are not). Every
closing pads the mask with zeros first: cv2's default border counts as
foreground for the erosion and filled from an object to the image's edge.
Outlines: contours through pixel centres, grown half a pixel back to the
edges, `make_valid`, then `polygons.to_geojson` (simplified harder until the
400-vertex budget fits).

*Scores*: `soft(strength, floor, half) = 1 - 2^(-(s - floor) / (half -
floor))` above the floor -- 0 at the universe gate, 0.5 at the script's
published default, never saturating. Fold: median z (2, 4) x min(1,
agreement / 0.7); tear: depth z (1.5, 3); debris: p90 z (3, 6); saturation:
log2(area / 25 µm²) (0 at 25, 0.5 at 1000 µm²). The automatic threshold is
0.5 for every category; `adjust` steps move it 0.1. A threshold keeps objects
at or above it: `evaluate` and `objects_at` read the stored objects only.
Ids are `art_<category>_<rank>` by score within the result.

Stored: `artifacts/<fp>.json` (summary and every object: geometry in level-0
pixels, bbox, centroid, area, score, strength, channels, source channel,
channel evidence, metrics, refined), `<fp>.npz` (per category the object
scores on a 25 µm field, the tissue and analysis-region fractions, small
tissue / region masks), `current.json`, `settings.json` (`per: {category:
{threshold | offset_steps, color}}`, `channels` -- the panel's channel
filter), `running.json`. The fingerprint covers image identity and stamp,
channel keys, level shapes, stage-1 level, pan subset, parameters and pixel
size. Capabilities: `run_artifact_check` (job), `get_artifact_check`,
`set_artifact_check`, `clear_artifact_check`, `write_artifact_regions`
(retained objects as ROIs, class per object, saturation scoped to its
channel; `trace: object`, refinement `status: detector`).

The panel: one line per category -- swatch, name, kept count, its slider, an
eye -- then the channels shown: All channels (with no line, its eye shows or
hides everything; with lines, every line) and one line per listed channel
(+). Every object is fetched once per result (`/artifacts/objects`) and the
sliders filter it locally; release stores the threshold. Channels never move
a threshold. `QcHoverProbe` stays the one click owner: outside every region
it hands the objects under the pointer to `onSelectArtifacts`; one is
selected and its source channel put on screen (a slot already holding it, or
one slot the section keeps -- never Registration's pair while on, never the
nuclear slot); several open an "Artifacts here" menu, and a second click at
the same spot steps to the next.

**Inside a session** the Artifact Detector is one check unit per category
(`artifacts:<category>`), on by default (`checks={"artifacts": false}`
turns it off; `PLEXORA_QC_CHECKS=all` includes it). Its `ScoreField`
carries the objects (`ScoreField.objects`), and `score_fields.regions`
answers with them (`artifacts.regions_from_objects`), so every region is an
object's own outline; candidates are `trace: object`. It supersedes the
scan's saturation detector. A fold overlapping the scan's `diffuse_bright`
fold candidate may be merged into it; the scan's dark and diffuse-bright
detectors stay (they also hint at other things).

## 13. Categories, thresholds and provenance

**Five categories over the classes.** `schemas.CATEGORIES` (Blur / focus,
Registration, Segmentation, Tissue / acquisition, Staining / signal, each
with its colour, what it groups and the default class of a region drawn in
it) and `REVIEW`. Every class maps to one (`CLASS_CATEGORY`; a tile seam is
acquisition, an intensity step at a tile border), every cell
and marker reason to one (`CELL_REASON_CATEGORY`: the `seg_*` reasons are
segmentation, a region reason takes its class's category); `_assert_categories` checks it at
import. Three classes exist for this: `segmentation_error` (a region where the
mask failed; whole-cell), and `tissue_artifact` / `staining_artifact` (what a
region drawn by hand in those categories is). `AGENT_CLASSES` leaves the two
generic ones out: an agent always names what it saw. The UI shows the
category; the details view and every record keep the subtype.

**One look per check** (`score_review.review`). A check's scores as a
`ScoreField` (values, the weight each cell holds, the grid in full-resolution
pixels, the automatic bar); `score_fields.bar` moves it in steps (the larger
of `score_step_mad` MADs and the check's `score_step_floor`; positive is
tighter); `regions` joins the cells at the bar into GeoJSON on the check's own
grid (Blur QC's 40 µm, the mismatch map's ~6.5 µm, the density grid's cell and
a half); `sample_strata` picks `score_per_stratum` places per row,
`score_spacing_um` apart, deterministically. Segmentation's reasons sample
cells instead (`CellScores`, `sample_cells`). `sheets.score_sheet` draws the
rows and the whole-tissue row. The session's packet and `sample_qc_examples`
(Paid) call the same function, so the same field, bar and seed are the same
sheet.

**Thresholds are never typed by an agent.** A session stores
`offset_steps` per check unit (`threshold_source` `agent_refined`); the free
path's `adjust` (`set_blur_check`, `write_registration_regions`;
`sample_qc_examples` previews) stores steps too
(`user_relative`), refused past `adjust_max_steps`. A typed value is the
user's (`user`). Every region and cell reason records its bar, source and
steps.

**One provenance builder** (`provenance.py`): `region_record` (category,
class, action, level, channels, cycles, scope, tool and version, score and
its kind, threshold, source, steps, the agent's judgment and its source,
the agent's notes (`ai.notes`: every answer's `notes`, kept on the candidate,
and on the check's entry in `result.checks`; a check's region with none of its own borrows its
score review's), evidence artifacts, how the outline was made, geometry hash and a
`qc_regions.geojson#<roi_id>` reference, user state, cells derived),
`cell_reason_records` (the regions, channels, excluded / warned with the denominator, `cells_source` direct or
roi), `marker_reason_records`, `categories_summary`, `document`
(`qc_provenance.json`) and `findings_rows` (`qc_findings.csv`, with
`ai_notes`). The panel's region rows, `get_qc_results`, the GeoJSON and the
report read these. `cell_record` is one cell's: each reason with its
channels and the regions behind it, the cell's marker flags with their values
and bars, and its Segmentation QC call. The ROI's notes carry an `agent:`
line with the notes (QC's own tokens taken out).

**The hover card** (`static/qcHover.js`). The pointer resting on a QC region
or a flagged cell shows a small card beside it: category, subtype, whether it
excludes or warns, one line of why (the agent's notes, else the score over
the bar or the cell's value against its cutoff) and a few rows; several
reasons on a cell list under the primary one. Regions are hit-tested in the
browser (`QcRegionOverlay.hitTest`, which core's `overlayAt` also reaches);
a cell is asked of `GET /plugins/qc/cell_at` (`viewer_data.cell_at`: the mask
label at the point or the nearest within `radius`, the nearest centroid
without a mask), only while QC's cell layer is drawn, at once: one question
in flight, the newest point asked next. With the mask the answer carries the
cell's pixels (`shape`: box and packed bits), so every later move over a cell
already seen is answered in the browser. A clean cell gets no card. A click
inside a region runs `focusRegion`, whatever cell it lands on; outside every
region, a click on a flagged cell runs `focusCell` (its reason shown, the
cell framed, the channels behind its call put up). A click the card acts on
sets `preventDefaultAction`, or OSD's click-to-zoom would pull the view off
the region it was fitted to. No card while drawing, panning with Space, during a press,
drag or wheel, or while the ROI tool is on screen.

**The free writers** (`capabilities_checks.py`). `write_blur_regions`,
`write_registration_regions` and the clusters of `write_segmentation_flags`
share `_write_regions`: a check's earlier regions for the same keys are
replaced unless the user edited, locked, moved or renamed them; each region is
a child receipt; the action is pinned as an approval pins one (a strictness
change never renames it); the cells are re-derived. `write_segmentation_flags`
writes only Segmentation QC's clusters: its per-cell calls reach the cells
through those regions, not as cell reasons of their own.

## 14. Not done yet

- The QC store's lock (`results.lock`) is per process. A headless `plexora ai`
  process and the server writing one project's QC at the same moment can lose
  the earlier write; Free writers pass `expected_revision` to be refused
  instead, the session's own writes do not yet.

- A `qc-dataset` scope (one session over a cohort, cross-image drift).
- The optional QUAL-IF-AI detector adapter (its code licence needs a review
  first; the weights are CC BY 4.0).
- A `pixel_setup` packet for images without a pixel size (QC works in pixels
  meanwhile).
- Real annotated slides for `plexora ai bench qc` (it runs the synthetic
  scenes of `plexora/ai/qc_scenes.py` today: a detectors-only arm and a session
  arm driven by `QCTruthAgent`; `tests/test_qc_bench.py` pins its floor), and a
  calibration of the `[cal]` cut-points against them.
