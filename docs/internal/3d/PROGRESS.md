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
| Pin a projection as a layer | todo |
| QC/gating fingerprints absorb the view (only needed once those tools take `view`) | deferred to Phase 3/4 |
| DICOM focal planes as a volume; remote TIFF volumes; in-memory (notebook) volumes | todo |
| Import-dialog choice of z semantics (depth vs cycles) | todo |

## Phase 2 — bricks, sections, volume rendering

| Item | State |
|---|---|
| `/volume/<ds>/level` (whole level as uint8 voxels) and `/volume/<ds>/section` | done |
| WebGL2 raycaster (MIP / additive, presets, clip range from the slab band) | done |
| Section panes (XZ / YZ) in the viewer | todo |
| Bricked store (coarse 3-D pyramid + ROI cache) | todo — native TIFF/NGFF levels suffice so far |
| render_sections / render_object_views agent tools | todo |

Verified in headless Chrome (SwiftShader) against a synthetic 3×24×1024² stack: plane view
with badge, MIP with band and notice, 3-D MIP render.

## Phase 3 / 4 / 5

Not started: 3-D label volumes + object table + measurement, segmentation plugin, ROI schema v2,
QC 3D checks, gating 3D evidence (waits for feature/spatial-gating), 4D.

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

## Open questions for the user

- Should the import dialog ask "depth or cycles?" for multi-channel stacks with no z step,
  or keep the silent `cycle` default (today's behaviour) with the edit page as the way out?
- A node computes a volume's window synchronously on first use (a large stack's first tile
  waits for the scan).
