# 3D / 4D imaging — implementation progress

Branch `feature/volumetric-3d` (worktree `../plexora-3d`, cut from main ca641a34).
Plan: `~/.claude/plans/pasted-content-id-f2b3-plan-sparkling-papert.md` (sections A–H).
**Merge only after the user has tested it.** Gating/autogate edits are coordinated with the
feature/spatial-gating session (small additive hunks on main; Phase 4 3D gating evidence waits
for that branch).

Run tests from the worktree with `PYTHONPATH=$PWD` (the conda env's editable install points at
the main tree). The vendor bundle is rebuilt with `cd plexora/client && npx webpack --mode
production` (node_modules is a symlink to the main tree's, so chunk ids differ from main's;
the new chunk files are committed beside the old ones).

## Phase 1 — volume model, Z navigation, projections — done

| Item | State |
|---|---|
| `plexora/server/volume/` (axes, viewspec, model, provenance, reduce, plane_view, windows) | done |
| Readers: OME-TIFF / ImageJ / pyramidized copies, OME-Zarr (t/z pins, volume extension), IMS | done |
| `ImageSpec` volume fields, registration records them, z/t semantics rule | done |
| Tile route `view=`, ETag/key unchanged for planar, volume + std window classes | done |
| `/volume/<ds>/info`, `/view_state/<ds>`, `set_pixel_size(axis=z|t)` | done |
| Stats/GMM `view=` (slab overviews) | done |
| Node API `view=` (tile/region/stats/gmm/quantization/geometry), direct routing query | done |
| `SourceImage(view=)`, `open_project_volume`, `ImageHandle.volume()/read_region(view=)` | done |
| Client: viewState.js, Z strip + badge, viewerManager `view=` handover + zombie cache, sidebar std window, Depth card, capture label | done |
| Plugin contract: `Requires.dims/representations/spacing/time`, `plexora.api.representation` | done |
| Agents: inspect_volume, set_z_spacing, sample_z_planes, axis_profile, render_projection, render_region(view), viewer_set_view/z/slab, volume-triage skill, MCP `volume` profile | done |
| Import: .ims, the row says how a stack's planes are read | done |
| Pin a projection as a layer: exact level-0 pyramid in `projections_v1/`, `proj-<token>` layer built by the `projection` builder, Depth card button, `pin_projection` agent job; removing the layer deletes the file | done |
| QC/gating fingerprints absorb the view (only needed once those tools take `view`) | deferred to Phase 3/4 |
| DICOM focal planes as a volume (fluorescence; one handle, `at_focal_plane`; z step from the planes' spacing; derived coarse levels resampled per plane, never borrowed) | done; colour (brightfield) slides too (2026-10-11): `Volume.color`, planes one at a time (viewspec refuses projections/sections), the RGB base item swapped per plane (`swapBrightfieldBase`), strip and Depth card offer plane mode only |
| In-memory (notebook) volumes: `register_memory_datasource`/`plexora.view(image=array, axes="czyx", z_step=…)`; served by the kernel's node, planes built on demand, the caller's array never copied | done |
| Remote volumes: OME-TIFF stacks at a web address (planes read by rectangle over the halving chain) and OME-Zarr stacks (the remote provider now asks the volume layer first, with a lazy overview) | done |
| Choice of z semantics (depth vs cycles): edit page "Planes on Z", re-reads the image | done (edit page); import dialog still silent |

## Phase 2 — bricks, sections, volume rendering

| Item | State |
|---|---|
| `/volume/<ds>/level` (whole level as uint8 voxels) and `/volume/<ds>/section` | done |
| WebGL2 raycaster (MIP / additive, presets, clip range from the slab band) | done |
| Section panes (XZ / YZ) in the viewer, aligned with the main view, shared centre | done |
| Bricked store (coarse 3-D pyramid + ROI cache): `server/volume/bricks.py`, zarr v3 sharded bricks (32,128,128), built in the background for large plane-major stacks, an area cache at finer levels (`POST /volume/<ds>/prepare`, 4 GB LRU), reads per level through `BrickedSource`; the 3-D view of the area on screen (`/volume/<ds>/level?box=`) | done |
| render_sections agent tool (XY + XZ + YZ through one point, one window per channel) | done |
| render_object_views (a measured cell by id: XY/XZ/YZ through its centre, its outline in yellow, its row); render_sections `segmentation: outlines` | done |
| Orientation (2026-10-10): quaternion camera with named views matching the panes (`views/volumeCamera.js`; the old turntable presets were mislabelled), X/Y/Z axis-locked turns in image coordinates (keys while the 3-D view is engaged, Esc), gizmo with click-to-align (`views/orientationGizmo.js`); one canvas layout for everything over the image (`services/canvasLayout.js`: edge bands + corner docks, `ctx.view.dock/reserve/insets`, `data-canvas-dock` for plugins), Figure Builder's dock adopted, scale bar and AI panels clear of bands | done |
| Clip-range contrast (2026-10-11): `/volume/<ds>/hint` (the level's hint over planes `z=z0,z1`, at the level drawn; local or node), asked by the renderer when the clip range is narrower than the stack, settled 220 ms | done |
| Layout follow-up (2026-10-11): fold-to-fit (`place(el, {collapse})` / `data-canvas-collapsible`, folded only when the room changes; Figure Builder dock and mini-map fold), published dock sizes and a bottom lane (`--plx-dock-*`, `--plx-lane-*`) the chat sizes itself in, closed mini-map takes its lens's room, sidebar expand button and AI chip docked top-left, a floating agent panel docked bottom-right, Figure Builder's frame clamped to the band-free area | done |
| Interaction (2026-10-10): plane changes committed for every channel in one frame and dissolved over a copy of the picture (`views/planeMotion.js`, never the data); next/previous plane prefetched once still; the rail's thumb follows the hand between planes; the 3-D view coasts after a release (exponential decay per ms, capped, interruptible), eases zoom and preset turns, lowers resolution only for slow self-driven frames; resizable XZ/YZ panes (drag, keys, double-click to reset), the cut at the centre of the uncovered plane; `/viewer_prefs` in the settings file (pane shares, motion on/off); reduced motion honoured; Imaris datasets kept open with a plane-sized chunk cache | done |

Verified in headless Chrome (SwiftShader) against a synthetic 3×24×1024² stack: plane view
with badge, MIP with band and notice, 3-D MIP render, section panes with content, 3-D label
outlines that change with z.

Real data (2026-10-09): the Xenium ovarian run's `morphology.ome.tif` (DAPI, 14 planes,
45450×27241, 3 µm z step from the file, 8 levels) registers in 0.3 s as depth. Plane tiles
~100 ms at level 0, a 14-plane MIP tile 0.86 s (cached after), sections 0.3–0.8 s. The first
tile waits ~15 s for the window scan -- the same as the planar focus image of the same size
today. Pinning a 4-plane MIP took ~60 s and is pixel-identical to numpy at level 0.

## Performance — one revision, one scheduler, resident textures (2026-10-10) — done

Plan: `~/.claude/plans/pasted-content-id-7217-deep-lively-orbit.md` (S0–S7). Method, root
causes, before/after tables and what was re-scoped: `PERF.md`. Harness: `tools/bench3d/`.

| Item | State |
|---|---|
| S0 harness + `window.PlexoraViewPerf` + Server-Timing on the volume routes + `/volume/<ds>/perf` | done |
| S1 opaque fade ghost (the Z-step brightness flash), shaders built once per page, zombie cache on disable | done |
| S2 `NavigationRevision`: plane, labels, panes, 3-D and contrast commit one revision in one task; units pinned per item | done |
| S3 `views/tileFetch.js` scheduler (dedup, order of need, one decode, cancellation); standby planes in the direction of travel | done (residency ledger and coarse-first commit not needed at measured sizes) |
| S4 3-D textures kept in a 1 GB LRU by what their bytes are; level payload cached, ETag/304, `w=` immutable | done |
| S5 provisional window first, exact in the background; decoded-chunk cache; Imaris inflate outside the lock; bricks pinned not locked; section read budget | done (brick routes with the atlas, below) |
| S6 3-D detail sub-volume where the camera rests (re-scoped from a brick atlas) | done |
| S7 node `/image/<rid>/level` from the same builder, forwarded and revalidated by the primary; node tiles honour `w=`; node provisional windows; node view pyramids cached | done |
| Brick atlas (2026-10-11): `/volume/<ds>/bricks/manifest` + `/bricks` (`server/volume/brick_serve.py`: apron, pooled planes, per-brick max, empty bricks, read per source tile), `views/volumeAtlas.js` (page table, greedy cut by screen-space size within 384 MB, shader walk with empty-space skipping); the detail sub-volume stays for node-backed images | done |
| Slow links (2026-10-11): HTTP/2 measured and not adopted (TLS-only in browsers); tile batching when a server's least first-byte wait is over 60 ms (`/generated/batch`, `/node/v1/batch`, `server/tile_batch.py`); gzip for levels and bricks once the link measures under 30 MB/s (`z=gzip`) | done |

## Phase 3 — labels, objects, QC

| Item | State |
|---|---|
| 3-D label import → label volume (OME-Zarr z,y,x uint32, XY levels), per-plane label tiles | done |
| `measure_objects` job → `objects_v1.parquet` (+ manifest), attached with roles label/X/Y/Z (roles confirmed) | done |
| Depth card "Cells in 3-D": measure from the viewer (µm, or voxels without a z step), progress, reload to use the table | done |
| Plane-level stack QC: focus_profile, axial_attenuation, missing_slices (`check_volume_quality`) | done |
| Segmentation plugin (2026-10-11): `plugins/segmentation`, Cellpose 4 / Cellpose-SAM via extra `segment3d` (Cellpose 3 cannot share numpy 2), 3-D (`do_3D`, anisotropy from the z step) or stitched planes, XY blocks with halo and centroid-in-core placement, auto level with labels upsampled to full size, label volume attached as the mask by the import path, then measured; card (Tools → Segment in 3-D), routes, agent `segment_3d` (job). Runs where Plexora runs (a node-backed image is read through the node); not on the node itself | done |
| ROI extents: `extent {kind: plane|slab, z|z0,z1, t}` per region (absent = every plane, so 2-D documents are unchanged); cell mapping and counts filter by the z role; off-plane regions draw as a faint outline; row badge + menu "Only on z N" / "On every plane"; agent create/update_roi `extent`; GeoJSON round-trip; plugins get `ctx.view` | done |
| ROI 3-D shapes: `solid` contours / ellipsoid (a box is a rectangle with a slab extent), `roi.set_solid`, plane-by-plane mapping, GeoJSON, section-pane overlays; magic select carried through planes (`vision/propagate.py`, `/segment/v1/carry`, MobileSAM or SAM 2.1 via the `volume3d` extra; `carry_roi_through_planes`). Measured in `MAGIC_3D.md` | done |
| ROI cells and hand edits (2026-10-11): `labels` solids (mask label ids + per-plane outlines from `SegHandle.label_volume`, gaps allowed; membership by id in `mapping.assign(cell_ids=)` / `cells_in_roi`), `/plugins/roi/api/from_cells` + panel "From cells" (viewer selection) + `create_roi(cells=)`; one plane's outline dragged by hand (`RoiGeometry.editableGeometry`, `keys`), the server re-deriving the footprint when `set_solid` sends none; a cells region reshaped becomes contours | done |
| QC plugin integration (2026-10-11): `qc3d` gains `z_registration_drift` (3-D phase correlation per cycle), `stitching_seams`, `segmentation_3d` (flat / clipped / taller than wide / split / volume outliers), faint planes, runs merged; QC `run_qc_3d` (job) / `get_qc_3d` / `write_qc_3d_regions` (plane findings as QC regions with `extent`, existing classes) / `set_qc_plane`; every QC check reads the chosen plane and fingerprints it (`server/plane.py`); QC's cell propagation keeps a region's planes/frame and maps by centroid on a 3-D mask; panel Stack section. Per-object findings stay data (not regions) | done |

## Phase 4 / 5

Gating evidence in depth (2026-10-11), kept to files `feature/spatial-gating` does not touch
except one additive hunk each in `agent/cell_gallery.py` and `autogate/sheet.py`:

- **Clipped cells** leave estimation under `qc="strict"` through QC's exclusion provider
  (`plugins/qc/server/exclusions.py` `clipped_z`): the object table's `touches_z_bounds`, only
  while the table describes the project's current mask, by id across frames, counted as reason
  `clipped_z` and in `n_clipped_z`; none is left out when more than `CLIPPED_MAX_SHARE` (half)
  of the cells touch an end (a stack thinner than its cells). `exclude` keeps them.
- **Per-cell z profile**: `server/volume/zprofile.py`, agent `object_z_profile` (core; gating
  and volume MCP profiles) and a `z_profile` block in `render_object_views`' manifest: each
  marker's mean plane by plane inside the cell and over its footprint one plane past each end,
  peak plane, width at half height (planes and µm), `beyond` ratios.
- **Pictures at the cells' plane**: `render_region` now cuts a 3-D mask at the plane it draws
  (it outlined the default plane under every view -- a bug); gate validation fields draw A and
  B at the median plane of their borderline cells (else all their cells) and add row D (XY,
  XZ, YZ through the field centre, `agent/sections.py`); the cell gallery crops each cell at
  its centroid plane; the autogate context sheet draws each field at its cells' plane
  (`sections.field_plane`).
- A measured object table is attached as one image (`single_image`), so gating no longer asks
  for an image-id column it cannot have.
- Field sampling with every cell left out says so (`precondition_missing`) instead of failing.
- **A user's CSV with X, Y and Z** (2026-10-11): the viewer's centroid dots are the planes on
  screen's cells (`/volume/<ds>/info` `table_z` → `ImageViewer.setCentroidPlanes`, a z range
  added to the centroid tile filter; a slab's whole range; old dots stay until the new
  plane's land); `read_cell_crops` reads each cell at its own plane, pixels and 3-D mask
  (`crops.cell_planes`), so session collages, per-cell calls, cell windows and the pixel
  estimate stop reading the default plane; pixel-estimate snapshots draw at `field_plane`;
  `flat_table_spec` (Python/notebook registration) keeps the guessed z role; `zplane`,
  `zslice`, `zindex` and `plane` are z names and metadata. Z is read as a plane index, like
  every z role. Tests: `tests/test_csv_xyz.py`, `tests/js/centroid_planes_probe.mjs`.
  Not done: `sample_gating_cells` rows carry no z; the legacy centroid path (no manifest) is
  unfiltered; the sidebar's positive counts are the whole volume's.

Not done, on purpose: display calibration per view kind (gating draws planes only, never a
projection, so there is one kind of window); the 3-D neighbourhood for the hybrid cell model
(the 2-D one is uncommitted work in the gating session's tree); the gating packets' z-profile
sparkline (`autogate/packets.py` is the gating branch's).

Agent tools (2026-10-11): `colocalize_3d` (core `agent/core/volume_analysis.py`: voxels at the
finest level under 30 M per channel, ROI by its extent / cells by id, Otsu Manders, refuses a
projection), `find_regions_of_interest_3d` (bright / dense / seg_suspicious / clipped /
out_of_focus), `run_qc_3d` (QC), object review in QC (`capabilities_objects.py`: start / next /
answer / status / finish, delegation role `object_review_worker`, skill `review-objects`,
quota `OBJECT_REVIEW.objects_per_worker`; finish writes a `labels` region in QC's
segmentation category with a `qc-class` token).

SCIMAP hand-off in µm (2026-10-11): `pixel_scale.spacing(project)` (x/y pixel size, z step typed
or from the file, never defaulted); `TableHandle.physical_coordinates()` (X_um/Y_um/Z_um, read
from an object table's µm columns or scaled from pixels; `CoordinatesUnavailable` names the
missing step); a hand-off's `table_for` carries `roles` and `coordinates` (`agent/coordinates.py`)
instead of writing µm columns into the user's table; `bind_project`/`bridge_bind` accept and
record `z` and `t` roles; `inspect_project` reports them. SCIMAP Pro must read `coordinates`.

Time series (first slice): a T×Z OME-TIFF with a stated time increment registers with
`imageTSemantics: time`; the viewer steps frames (`.`/`,`, the Depth card's Frame field), the
badge reads "t 3/3 · 60 s · z 2/4", tiles carry `t2z2`. ROI extents carry `t`: a region drawn on
one frame is faint on the others and its badge names the frame. The strip has frame step
buttons under its readout.

Cells through time (2026-10-11): label volumes per frame (`labels_v1` with `t<k>` groups,
`LabelVolume.frame(t)`, label tiles at the view's frame; a (T, Z, Y, X) mask TIFF is detected as
one); `measure_objects` measures every frame (rows per cell per frame, `t` column, new table
role `t`); `server/volume/tracking.py` links consecutive frames by least-total-distance
assignment under a cap (median nearest-neighbour distance by default; greedy past 16 M
candidate pairs) into a `track` column (`/volume/<ds>/tracks`, Depth card "Link across frames",
agent `track_objects`); ROI mapping and counts keep a frame-limited region to its frame's rows;
the viewer's row reads (cells, centroids, nearest cell, gates, Cell Explorer values) carry the
frame on screen (`dataLayer.tableFrame`, `ctx.view.tableFrame()`). Not done: divisions, gap
closing; the gating plugin's server-side gated-id route ignores the frame (the viewer's local
gate does not).

## Decisions taken while implementing

- A volume's window is exact (every plane, level 0) while one channel's level-0 stack is
  ≤ 4 GiB; beyond that it is max(default plane level 0, all planes at the finest level that
  fits). `std` window is (0, ceil(M/2)) — exact bound, no scan.
- Existing projects keep their reading: without `imageZSemantics`, only a single-channel,
  single-frame stack whose OME-XML/ImageJ header states its planes is depth.
- New imports: multi-channel z-stacks are depth only when the file states a z step;
  otherwise `cycle` (recorded). T is time only when a time increment is stated.
- IMS levels: XY-halving prefix; a coarse level with fewer planes is read at z·Z_L/Z_0.
- In 3-D mode the depth strip's slab band is the clip range (one gesture for "planes of
  interest" in both projection and 3-D).
- Node-backed volumes use the primary's all-planes window, so node tiles are byte-identical
  to a local read; an old node refuses views rather than serving the default plane.
- A volume opens on a provisional window (the default plane at a fine level plus the coarsest
  level's planes), the exact scan runs in the background (on the node for a node-backed one),
  and every 8-bit tile, section and 3-D level names the window it was made with (`w=`), so a
  provisional picture and the exact one never share an address in any cache (user's choice,
  2026-10-10).
- 3-D detail is a sub-volume over the whole level, not a brick atlas (`PERF.md` says why).

- ROI schema stays at version 1: a version bump would make main refuse every ROI document
  drawn on this branch if the user goes back to main after testing; at version 1 main simply
  forgets the extents (the regions read as every plane again).

## Open questions for the user

- Should the import dialog also ask "depth or cycles?" for multi-channel stacks with no z
  step, or is the edit page's "Planes on Z" enough as the way out of the silent `cycle` default?


## Surface Studio (feature/surface-studio, 2026-10-11)

Surfaces in the 3-D view, Phases 1 + 2 of the Surface Studio spec, Paid (`surfaces`).
Design, measurements, limits and merge notes: [SURFACES.md](SURFACES.md).

| Item | State |
|---|---|
| Engine (strip MC, isosurfaces, cell surfaces, levels, preview, suggest) | done, tests/test_surface_engine.py |
| Surface layer, routes, licence split | done, tests/test_surface_layer.py |
| Renderer (opaque, WBOIT/sorted, ray caster variant, picking) | done, surface_renderer_probe.mjs + GPU Chrome e2e |
| Quick Surface dock, card, inspector, objects table, plane/pane cuts | done, surface_studio_probe.mjs + e2e screenshots |
| Refine (versions/undo), export/import, neighbours/contacts, sections | done, tests/test_surface_phase2.py |
| Agent (10 tools, 3 viewer commands, skill surface-rendering) | done, tests/test_surface_agent.py |
| Figure Builder 3-D capture + client-raster export | done, figure_builder/tests/test_figure_builder_volume.py |
| Video Studio | not rebuilt (feature/video-studio); scene seam `ctx.view.surfaces` ready |
| Bench scenario `surfaces` in tools/bench3d | not done; numbers from the e2e in SURFACES.md §6 |
