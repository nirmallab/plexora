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
                     detectors, candidates merged and ranked, cell modules measured
qc_next ───────────► one packet: question + compact JSON + <= 2 images
qc_answer ─────────► typed answer -> transition -> (write ROI) -> next packet
qc_session_finish ─► close | commit | cancel | rollback; the result becomes active,
                     cells' calls written
qc_report ─────────► HTML + PDF with every denominator stated
```

**Deterministic code finds where to look; the agent decides what it means.**
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
| Cells | `cells/modules.py` (four modules), `cells/bulk.py`, `cells/packets.py`, `cells/calls.py` (`derive`), `propagate.py` (ROI -> cell overlap) |
| Storage | `results.py` (the QC store), `roi_link.py` (ROIs, user edits, adoption) |
| Files | `export.py`, `source_write.py`, `report.py` |
| Tools | `capabilities_session.py` (Paid), `capabilities.py` (Free + analytics) |
| Panel and routes | `server/routes.py`, `static/qc*.js`, `templates/qc/panel.html` |
| MCP | `plugins/qc/mcp.py` through `Plugin.mcp_factory`; skills `ai/skills/qc-image`, `review-qc` |

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
  surround that treats glass as tissue median), tile-seam steps.

The result is stored once per fingerprint (image identity, grid, channels,
pixel size, parameters, cycle override) as
`<agent_root>/qc/<project>/scan_<fp>.npz` + `.json`; a rerun reads it back.

## 4. Detectors and candidates

A detector (`QCDetector`: `available(context)`, `run(context)`) proposes
regions on the map grid with a class hint, a scope hint and a severity; it
never decides. The classical ones: focus, saturation, aggregate, diffuse
bright (fold / autofluorescence / background by how many channels agree),
illumination, seam, dark tissue, empty channel, registration, cycle tissue
loss. Their cut-points (`DETECT`) are permissive on purpose: the audit catches
what they miss, and the agent dismisses what they over-call.

`candidates.build` cleans area masks, merges the same place across detectors
and channels (IoU, or containment within a size ratio; whole-channel classes
never merge into local ones; a merged outline grows only by members that are
the same place), ranks by severity and area, and gives each a content-hash id
(`cand_<sha1>`) so a rerun's memo keys match. Caps per channel and per session;
what is dropped is summarised as `residual` for the report.

## 5. The session

Units: **channel** (audit), **candidate** (confirm, scope, localise, grid),
**cells** (one per module), **final** (one review). `next_unit`: audits in
batches of `ENGINE["audit_batch"]` channels a sheet, two sheets a packet; then
candidates in channel order by score; then cell modules once every candidate
is settled; then the final review.

- **Audit**: a clean row dismisses its candidates below `force_confirm_score`;
  `suspicious` keeps the named ones (`elsewhere` opens a grid over the tissue);
  `uncertain` opens a whole-channel look. Every channel is looked at, whatever
  the detectors said.
- **Confirm** (three looks: overview/neighbourhood/crop/map, then nuclear and a
  matched clean field): `artifact` stores the judgment; then scope if several
  channels could be meant, localisation if the outline does not cover it, then
  the decision. `cannot_tell` and an exhausted allowance are **manual review**:
  a warning region of class `uncertain_manual_review`, never an exclusion.
- **Decide**: `strictness.decide_artifact(decision, measurement, table)`; the
  ROI is written (apply mode) in category `qc_<class>`, named with its action
  (`QC exclude: tissue fold · CD8`), as a child receipt with the `delete_roi`
  undo hint `roi.create` issues; a `roi_meta` row keeps what the ROI schema
  has no field for, with the geometry hash it was written with.
- **Final review**: `inconsistent` reopens the named regions once, at the
  deepest look.

Budgets per candidate (`QC_UNIT_DEFAULT`); the audit, scope and final review
are free. The limit policy (ask / extend / stop) is the shared one.

## 6. Strictness

Presets change thresholds only (`schemas.STRICTNESS_PRESETS`, directions in
`STRICTNESS_KEYS`, monotonicity asserted at import; a custom table outside the
lenient..strict band is refused). Packets carry no strictness. The agent's say
on a cell cutoff is stored as `offset_steps` and a `veto` per side, applied on
top of any preset's proposal, so **Strict ⊇ Standard ⊇ Lenient** holds by
construction (property tests in `tests/test_qc_results.py`).
`set_qc_strictness` re-derives every region's action (renaming its ROI, never
moving it between categories) and every cell's call from stored measurements
and stored membership fractions; no packet, no mask read. The user's own and
approved regions keep theirs.

## 7. Cells

Four modules (`cells/modules.py`): counterstain intensity (first nuclear
cycle), segmentation area (+ shape columns when present; a small object fails
on size alone only under Strict), cycle stability (last vs first nuclear
cycle), channel outliers (at most `MAX_OUTLIER_MARKERS`; excluded only when the
extremes cluster in space and the agent calls them an artifact). Cutoffs are
median ± k·MAD in the right space.

Region membership (`propagate.py`): the fraction of each cell's mask inside
the polygon (holes honoured), read at the smallest level within
`MAX_ROI_PIXELS`; centroid fallback without a mask or past the budget
(recorded as `roi_method`). Membership needs `cells.roi_overlap_fraction` of
the cell, a strictness key. A cell in several regions lists them all; exclude
beats warn; nothing is counted twice.

`qc_cells` holds per cell: `pass`, `action`, `primary_reason`
(`schemas.PRIMARY_ORDER`), `reasons`, `reason_count`, `roi_ids`, `roi_method`
and the `m_*` measurements.

## 8. The user's edits win

`roi_link.sync` runs before every read and change: a QC ROI deleted drops out
of the cells' reasons; edited (geometry hash differs) is kept as drawn and
never auto-updated; moved to another QC category takes that class; moved out
of QC is no longer QC; locked is approved. A region the user draws in a QC
category (matched by id `qc_<class>` or by label `QC: <Class>`) is adopted as
a user-made candidate and always excludes -- **manual QC needs no session and
no licence** (`refresh_qc`). Take-over from the viewer locks the region under
review and the session closes it as the user's.

## 9. Storage and files

`api.store(<project>, "qc")`: the document (results, the active one, the
strictness, the cycle override; newest `KEEP_RESULTS` inline, older archived
to files), `roi_meta`, `qc_cells`, `qc_cell_rois`. Session folders are swept;
results are not. `reset_qc` snapshots first and never deletes a region the
user drew, edited or locked; `restore_qc` puts it all back.

Exports (`export_qc`): `cells.csv`, `qc_regions.geojson`, `summary.json`,
`result.json`. Source write (`write_qc_to_source`, `source_file_write`):
AnnData `obs["plexora_qc_pass" | "_primary_reason" | "_reason_count"]`,
`obsm["plexora_qc_flags"]` (a boolean DataFrame, one column per reason),
`uns["plexora_qc"]`; CSV/Parquet get the scalar columns and
`plexora_qc_reasons`. Rows of other images are `<NA>`; existing QC keys are
refused without `replace`; `obs` is backed up first.

## 10. Licensing

`ai:qc:session` (the session tools and the report) and `ai:qc:analytics`
(`profile_image_qc`, `render_qc_overview`) are Paid; everything that reads,
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

## 12. Not done yet

- A `qc-dataset` scope (one session over a cohort, cross-image drift).
- The optional QUAL-IF-AI detector adapter (its code licence needs a review
  first; the weights are CC BY 4.0).
- A `pixel_setup` packet for images without a pixel size (QC works in pixels
  meanwhile).
- `plexora ai bench qc` with real annotated slides (the synthetic scenes are in
  `plexora/ai/qc_scenes.py`).
- Cell-module table operations for node-hosted tables (the modules read
  columns through `ds.table.columns`, which works remotely, but measurement runs
  on the primary).
