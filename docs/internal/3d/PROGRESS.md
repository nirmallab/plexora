# 3D / 4D imaging — implementation progress

Branch `feature/volumetric-3d` (worktree `../plexora-3d`, cut from main ca641a34).
Plan: `~/.claude/plans/pasted-content-id-f2b3-plan-sparkling-papert.md` (sections A–H).
**Merge only after the user has tested it.** Gating/autogate edits are coordinated with the
feature/spatial-gating session (small additive hunks on main; Phase 4 3D gating evidence waits
for that branch).

Run tests from the worktree with `PYTHONPATH=$PWD` (the conda env's editable install points at
the main tree).

## Phase 1 — volume model, Z navigation, projections

| Item | State |
|---|---|
| `plexora/server/volume/` (axes, viewspec, model, provenance, reduce, plane_view, windows) | done |
| Readers: OME-TIFF / ImageJ / pyramidized copies, OME-Zarr (t/z pins, volume extension), IMS | done |
| `ImageSpec` volume fields, registration records them, z/t semantics rule | done |
| Tile route `view=`, ETag/key unchanged for planar, volume + std window classes | done |
| `/volume/<ds>/info`, `/view_state/<ds>`, `set_pixel_size(axis=z|t)` | done |
| Stats/GMM `view=` (projection histograms) | todo |
| Node API `view=` (tile/region/quantization/geometry) | todo |
| `SourceImage(view=)`, `api.dataset.ImageHandle.view()/read_region(view=)` | todo |
| Pin a projection as a layer | todo |
| Client: viewState.js, depth strip, viewerManager `view=`, sidebar view-class windows, Depth card | todo |
| Plugin contract: `Requires.dims/representations/spacing/time`, `plexora/api/representation.py`, chip | todo |
| Agent: inspect_volume, set_z_spacing, sample_z_planes, axis_profile, render_projection, render_region(view) | todo |
| QC/gating fingerprints absorb the representation token (2D byte-identical) | todo |
| Import proposal: .ims, z-semantics choice | todo |
| DICOM focal planes as a volume; remote TIFF volumes; in-memory volumes | todo |

## Decisions taken while implementing

- A volume's window is exact (every plane, level 0) while one channel's level-0 stack is
  ≤ 4 GiB; beyond that it is max(default plane level 0, all planes at the finest level that
  fits). `std` window is (0, ceil(M/2)) — exact bound, no scan.
- Existing projects keep their reading: without `imageZSemantics`, only a single-channel,
  single-frame stack whose OME-XML/ImageJ header states its planes is depth.
- New imports: multi-channel z-stacks are depth only when the file states a z step;
  otherwise `cycle` (recorded). T is time only when a time increment is stated.
- IMS levels: XY-halving prefix; a coarse level with fewer planes is read at z·Z_L/Z_0.
