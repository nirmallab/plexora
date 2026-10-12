# Surface Studio — surfaces in the 3-D view

Status: Phases 1 and 2 of the Surface Studio spec, built on `feature/surface-studio`
(off main `a89a84d3`, 2026-10-11). Plan:
`~/.claude/plans/pasted-content-id-a28f-implementation-cryptic-stream.md`.

Surfaces are a representation of a z-stack beside the voxels: the isosurface where a
channel crosses one raw value, one closed surface per cell of a 3-D mask, or an imported
mesh. They are **layers** (`kind: "surface"`), built in the background, drawn by the 3-D
view on its own WebGL2 context and depth-composited with the ray-cast volume, cut where
the 2-D views cut them, measured, refined as versions, exported with their provenance, and
driven by the agent. Nothing here ever writes into an image or a mask.

## 1. Licence

Surface Studio is Paid: a new root entitlement **`surfaces`** in
`plexora/licensing/manifest.py` (label "Surface Studio"), beside `ai` — not under it, not
an add-on. A plain `["ai"]` licence does not unlock it.

| Guarded (`guards.require_entitlement("surfaces")`) | Always open (a read, or editing what exists) |
|---|---|
| `POST /volume/<ds>/surfaces` (make), `/preview`, `/suggest`, `/estimate` | `GET /surfaces` (list, says `licensed`), `/<id>/manifest`, `/mesh`, `/objects`, `/sections` |
| `POST /<id>/refine`, `GET /<id>/neighbours`, `POST /surfaces/import` | `GET /<id>/export`, `POST /<id>/cancel`, the generic layer PATCH (render bag) and DELETE |
| agent: `surface_create`, `surface_suggest_threshold`, `surface_inspect`, `surface_refine`, `surface_neighbours` | agent: `surface_list`, `surface_measure`, `surface_objects`, `surface_update`, `surface_export`, `surface_delete`; viewer commands |

The page never decides: the dock's first request is a Paid one, and a 403's `license`
block opens `PlexoraPaid.explain` with the server's own words. Over MCP the Paid tools also
need `mcp`, by the existing rule. **Before it is sellable** the platform must know the root:
`tools/bioc_sync.py` uploads the product manifest (its `--check` differs until then) and the
Paid plan must grant `surfaces` — both the owner's actions, not done here.

## 2. What existed (assessment)

| Area | Before | Reused |
|---|---|---|
| 3-D renderer `views/volumeRenderer.js` | single-pass WebGL2 ray caster, MIP/additive, quaternion camera (not persisted), brick atlas + detail sub-volume, no depth buffer, no picking | camera, box mapping, program cache, settle logic, snapshot; marked `[surfaces]` hooks only |
| Level data `server/volume/serve.py`, `model.py` | `read_block(level, t, c, z0, z1, rows, cols)`, `area_at`, `z_pool` | the extraction's only read path; area mapping |
| Labels / objects | `LabelVolume.read`, `objects_v1.parquet` with bbox and `volume_vox` | bboxes for per-cell crops, voxel columns joined to mesh rows |
| Derived layers `pin.py` + `layer_jobs` | builder registry, per-layer cancel, `forget` on DELETE | the surface store copies the pattern |
| Figure Builder | grabbed OSD canvases only; export re-rendered planes | capture + export now take the 3-D view's picture |
| Video Studio | not on main; **built on `feature/video-studio` (../plexora-video)** | not rebuilt; surfaces expose a scene seam for it (§8) |

## 3. Comparison, and what was adopted

- **Imaris** Surfaces: threshold on a histogram in raw units, background subtraction keyed
  to an object diameter, per-object statistics/filters/colour-by, small fragments dropped,
  cells drawn slightly apart. *Adopted* all of these; seed splitting is later.
- **3D Slicer** closed surfaces: constrained smoothing, mask stays the truth, voxel vs mesh
  measurements apart, 2-D/3-D in sync. *Adopted*: capped Taubin, measurements kept apart,
  contours in the planes and section panes, no mesh edit ever reaches the mask.
- **ParaView/VTK**: marching cubes / flying edges / surface nets, decimation, clipping, LOD.
  *Adopted*: Lewiner MC in strips (exact seams), decimation (optional), clip by the slab band
  and a free plane, level-of-detail by extraction level.
- **napari**: surfaces as ordinary layers, per-object colour, wireframe. *Adopted*.
- **Blender-class**: PBR/AO/shadows. *Adopted lightly*: Blinn-Phong key + headlight,
  hemisphere ambient, rim, four materials, outline (inverted hull). AO/shadows later.
- **vtk.js / three.js / WebGPU**: rejected as a second engine (see volumeRenderer's header);
  extraction is server-side (scikit-image, scipy).

## 4. Architecture

### Representation and files
`<derived root>/surfaces_v1/<token>/` per surface — `manifest.json` (source, params,
spacing, algorithm, `lods` with sha256, objects summary, build report, versions, notes),
`mesh-L<k>.bin` (+ `-v<n>` versions), `objects.parquet`. The token is a hash of the
source's identity and every shaping parameter, so the same request returns the same layer.

Mesh container `PXMS` (`server/surface/mesh.py`): `uint32 header length | JSON header |
arrays`, each 4-byte aligned — `positions f32 (N,3)` in **level-0 voxels** (x, y, z, voxel
centres on integers), `normals i8 (N,4)` in the voxel frame, `object_id u32 (N)`,
`triangles u32 (M,3)` sorted by object, `objects u32 (K,3)` = [id, first, count], optional
`positions_raw` (unsmoothed). The browser reads it with typed-array views, no copy.

### Engine (`plexora/server/surface/`)
- `marching.py`: Lewiner MC over z-strips of 32 planes plus the next strip's first plane;
  integer strip offsets make the shared plane's vertices bitwise equal, so `weld` is exact
  and surfaces are closed across seams (tested: same mesh at strip 3 and strip 64, Euler
  characteristic 2). A pad far below the isovalue caps structures cut by the volume's edge.
- `isosurface.py`: per-strip preprocessing with a halo (anisotropic Gaussian in µm,
  background = Gaussian of half the object diameter, clipped), components from the vertex
  graph, numbered by size, `min_size` by enclosed volume, then `finish` (Taubin, normals).
- `labels.py`: per-cell crops read in tile groups; each crop blurred `0.6` of a voxel along
  each axis (each axis's own voxel — this is what removes the terraces between planes on
  anisotropic stacks) and marched at 0.5, padded with 0 (cut cells capped on their voxel
  faces); Taubin (capped at half a voxel) and a 0.1-voxel inset so touching cells do not
  z-fight.
- `levels.py`: quality presets (preview 4 M grid voxels / 300 k triangles; standard 48 M /
  3 M; full 512 M / `PLEXORA_SURFACE_BUDGET_TRIANGLES`), regions, `z_stride` (mean-pooled
  planes), cell triangle estimate (calibrated 2.7 triangles per voxel² of sphere-equivalent
  area, within 25 %), refusals with the estimate.
- `preview.py`: the slider's grid read once and cached (512 MB LRU), MC per isovalue,
  contours through z / x / y in the header. `suggest.py`: Otsu, triangle, log-GMM split.
- `refine.py` (versions + undo), `export.py` / `importers.py` (PLY, OBJ, STL, GLB + zip with
  manifest), `contacts.py` (surface/centroid neighbours; contact area from mask voxel faces),
  `sections.py` (mesh–plane segments for the 2-D views, cached by sha).

### Renderer (`views/surfaceRenderer.js`)
Passes, only when a surface shows (otherwise `begin` returns null and the volume draws as
before — probe-asserted):
1. opaque meshes → FBO A (RGBA8 + `DEPTH_COMPONENT24` texture);
2. translucent: nearest depth → F; weighted blended OIT → W (RGBA16F accum with revealage in
   alpha + R16F weights, **one** blend state `ONE, ONE, ZERO, ONE_MINUS_SRC_ALPHA`, weight
   from view-space depth); without `EXT_color_buffer_float`, per-object back-to-front OVER;
3. the volume's ray caster in its surface variant, made by `volumeFragment()` from the
   renderer's own `fragmentSource` with checked anchors: rays stop at the opaque depth and
   accumulate front/back of the translucent depth into V's two outputs (MIP gets a real
   alpha from the per-channel opacity);
4. composite: front OVER translucent OVER back OVER (opaque | ground).

Style, colour, opacity, material, lighting, hidden/isolated objects and the camera are
uniforms or the per-object LUT texture; buffers are uploaded once per mesh (wireframe edges
and unsmoothed positions lazily, once). Picking renders ids + depth bits into one scissored
pixel of an RGBA32UI target. Hooks in `volumeRenderer.js` are marked `[surfaces]`: program
variant, `begin/bindDepth/finish` in `draw()`, mount/lost/destroy, export-size `snapshot`,
click-to-pick, camera persistence (`volume.camera {q, dist, pan}` and `volume.show` in the
view state), and the S key.

### Page
`services/surfaceLayers.js` (load/style/persist/create/preview/cuts/scene seam),
`views/surfaceCard.js`, `surfaceStudio.js` (Quick Surface dock), `surfaceInspect.js`,
`surfaceObjects.js` (table, colour-by, export menu). Entry points: the cubes button in the
Layers header (z-stacks only), the 3-D options menu (Volume and surfaces / Volume only /
Surfaces only / New surface…), S over the 3-D view, a card's Edit.

### Agent
`plexora/agent/core/surface.py` (11 capabilities), viewer commands `viewer_set_volume_mode`,
`viewer_set_camera`, `viewer_set_surface` (bridge handlers in `agentBridge.js`), MCP
`volume` profile, skill `surface-rendering`.

## 5. Figure Builder

A capture in 3-D draws the volume renderer again at print size (`snapshot({width,
height})`, longest side ≤ 2400 px) for the preview, and core contributes
`scene.plugins.volume = {mode, camera, show, slab, surfaces}` (no `SNAPSHOT_VERSION` bump;
restored on reopen). Export: `export.volume_state(panel)` → the stored preview raster,
resampled, reported `client_rendered` with its effective dpi (preflight too).

## 6. Measured (M2 Pro, GPU Chrome, 2026-10-11)

Synthetic stack 40 × 320 × 320, 3 channels, 70 cells (`tools/bench3d` style e2e, scratch
server with a test licence):

| | |
|---|---|
| preview per slider position (4.1 M voxels, cached block) | 120–136 ms |
| threshold suggestions | 90 ms (EM on ≤ 100 k values; was 2 s) |
| draw, volume only / volume + 265 k triangles / surfaces only (1060 × 855) | 3.2 / 3.0 / 1.9 ms |
| `bufferData` calls on colour/opacity changes | 0 |
| GPU bytes for both surfaces | 5.8 MB |
| engine: 8 M-voxel isosurface MC / per-cell MC | 74 ms / ~0.8 ms per cell |

Rays stop at opaque surfaces, so volume + surfaces is cheaper than volume alone.

## 7. Limits and how to read a surface

- An isosurface is the boundary at a chosen value, not a validated object boundary.
- Smoothing and the inset move vertices by ≤ about half a voxel; mesh volumes are a few %
  under voxel volumes (cells on the test scene: median 0.945, 5th–95th 0.93–0.96). Report voxel columns.
- Objects touching the volume's edge are cut there (`touches_bounds`).
- Distances are geometry, not interaction. Contact area is from the mask, not the mesh.
- Data on a data node: the extraction runs on the node that holds the voxels it reads
  (`store._builds_on`: the image for an isosurface, the mask for cell surfaces) as a
  polled job (`POST /node/v1/volume/surface`, `/volume/jobs/<id>` status, result as an
  `.npz` of the mesh arrays, cancel); the primary measures the mesh, joins its object
  table (sent to the node for sizing) and writes the surface, so every read after the
  build is local. Previews and suggestions read their block through
  `POST /node/v1/image/<rid>/block` (`providers.node.NodeVolumeReader`); contact areas
  read one mask block through `NodeLabelVolume`. A stack read from a web address is
  still refused (`resource_not_local`). `tests/test_node_surfaces.py` pins node = local.
- Per-cell extraction is a Python loop (~1 ms/cell): fine to ~50 k cells; a one-pass numba
  multi-label kernel is the Phase 3 answer. The full-resolution ceiling is the browser's
  triangle budget; chunked region LOD is Phase 3.
- WBOIT is approximate for many overlapping translucent layers; the sorted fallback can
  pop between interpenetrating objects.
- Glass over black reads dark (it is see-through); raise opacity or use Outline for figures.

## 8. Merging

- **feature/3d-index-pyramids** edits `volumeRenderer.js`, `serve.py`, `labels.py`,
  `model.py`: this branch adds marked hooks only (and no change to those server modules).
- **feature/video-studio** (../plexora-video) edits `volumeRenderer.js` (`setClip`,
  `pinSize`, `settled`, `renderFrame`): both add a `_pinnedSize` in `draw()` — keep one.
  Video Studio's `videoSceneState` should add `ctx.view.surfaces.captureState()` /
  `applyState(list, {save: false})` to its SceneState so surfaces animate (opacity, style,
  isolation), and its `settled()` should also wait for surface meshes in flight
  (`PlexoraSurfaceLayers` records' `loading`). `renderFrame()` already draws surfaces
  (it calls `draw()`). `clipRangeZ` is used by `placement()` when present.

## 9. Next (Phases 3–4, designed only)

One-pass numba multi-label surface nets; chunked out-of-core extraction with halos (the
strip is already the unit); region surfaces at level 0 with camera-aware LOD and frustum
culling; watershed splitting; AO/shadows/
supersampling; time-resolved surfaces per frame with tracking-aware colour; SCIMAP Pro
hand-off of surface measurements.
