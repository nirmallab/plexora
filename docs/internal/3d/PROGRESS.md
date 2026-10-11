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
| S5 provisional window first, exact in the background; decoded-chunk cache; Imaris inflate outside the lock; bricks pinned not locked; section read budget | done (brick routes not built) |
| S6 3-D detail sub-volume where the camera rests (re-scoped from a brick atlas) | done |
| S7 node `/image/<rid>/level` from the same builder, forwarded and revalidated by the primary; node tiles honour `w=`; node provisional windows; node view pyramids cached | done |

## Phase 3 — labels, objects, QC

| Item | State |
|---|---|
| 3-D label import → label volume (OME-Zarr z,y,x uint32, XY levels), per-plane label tiles | done |
| `measure_objects` job → `objects_v1.parquet` (+ manifest), attached with roles label/X/Y/Z (roles confirmed) | done |
| Depth card "Cells in 3-D": measure from the viewer (µm, or voxels without a z step), progress, reload to use the table | done |
| Plane-level stack QC: focus_profile, axial_attenuation, missing_slices (`check_volume_quality`) | done |
| Segmentation plugin (Cellpose 3-D on a node, extra `segment3d`) | todo |
| ROI extents: `extent {kind: plane|slab, z|z0,z1, t}` per region (absent = every plane, so 2-D documents are unchanged); cell mapping and counts filter by the z role; off-plane regions draw as a faint outline; row badge + menu "Only on z N" / "On every plane"; agent create/update_roi `extent`; GeoJSON round-trip; plugins get `ctx.view` | done |
| ROI 3-D shapes: `solid` contours / ellipsoid (a box is a rectangle with a slab extent), `roi.set_solid`, plane-by-plane mapping, GeoJSON, section-pane overlays; magic select carried through planes (`vision/propagate.py`, `/segment/v1/carry`, MobileSAM or SAM 2.1 via the `volume3d` extra; `carry_roi_through_planes`). Measured in `MAGIC_3D.md`. LabelRef not done | done |
| QC plugin integration (findings as ROIs, z_registration_drift, seams, segmentation_3d) | todo — coordinate with the gating session |

## Phase 4 / 5

Gating 3D evidence waits for feature/spatial-gating; SCIMAP hand-off in µm not started.

Time series (first slice): a T×Z OME-TIFF with a stated time increment registers with
`imageTSemantics: time`; the viewer steps frames (`.`/`,`, the Depth card's Frame field), the
badge reads "t 3/3 · 60 s · z 2/4", tiles carry `t2z2`. ROI extents carry `t`: a region drawn on
one frame is faint on the others and its badge names the frame. Not yet: cell mapping by frame
(object tables are measured per frame, t=0 today), a frame control on the strip, tracking.

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
