# Plane-by-plane gating in 3-D

Branch `feature/plane-gating-3d`, worktree `../plexora-plane-gating` (off main 8e4ecee5).
The user's spec: "Plane-Aware Segmentation and Slab-Based Intensity Gating in 3D" (2026-10-11),
narrowed by the user the same day: **the gating measure is always the whole cell; the slab is for
visualization only.**

## What a gate is

A gate is always applied to each cell's whole-cell value, its mean over every voxel
(`objects_v1`, unchanged). That is the same number on every plane and whatever the image shows,
so a cell has one call for the whole stack and navigating never changes it. Nothing in this
branch changes how gates are stored, evaluated, exported or fitted.

A first version also offered plane and slab *measurements* as gate values (a per-cell slab
around each cell's own centre plane, medians by a job, gates stamped with their measurement). The
user ruled that out, and it was removed before anything was committed.

## What was already there

- **Label tiles are one plane.** `viewState.labelSpec()` asks for `z<N>` and the tile route
  reads that plane of the label volume, so a cell is drawn only where it has voxels on the plane,
  its outline is its own cross-section there, and its id is the same on every plane.
- **Projections.** The viewer's slab projections (`z<a>-<b><op>`: max, mean, median and more),
  including a slab that follows the plane (`slab.follow`). Their outlines were the slab's middle
  plane's.

## What this branch adds

| Piece | Where |
|---|---|
| Thresholding panel row **View: Plane / Slab**, thickness (µm, default 5; planes without a known z step) and Mean / Max / Median. Slab sets the viewer's following projection with `clip`; Plane takes it back. Shown only for a z-stack with a 3-D mask, and not in the 3-D view. The last slab is remembered per project (`localStorage`). | `plugins/gating/static/gatingPlanes.js`, `panel.html`, `gating.css` |
| **Clipped following slab**: recentred on z at every step and cut where the stack ends ("extending equally where possible"), never shifted. A following slab's outlines come from the plane it follows (z), not from its middle, so at the ends the masks are still the plane's own. | `services/viewState.js` (`slab.clip`, `labelSpec`, `movedTo`), `volume_routes._clean_view_state` keeps `clip` |
| **The plane's cells under the histogram**: shaded (smoothed, scaled to its own peak) and counted, "z 12 · 41 of 312 positive". Counts are the gate's calls as the viewer drew them (the layer's `gateMask`). | `gatingPlanes.js`, `gatingSidebarController.drawGateDistribution` / `redrawPlaneOverlay` |
| **Per-plane cell index**: which cells have voxels on each plane, and how many voxels. Written by the measure pass from the same reads (`measure.measure(planes=)`), beside the object table (`objects_planes_v1/`, memory-mapped column files), and sent back from a data node with the table. | `server/volume/planes.py`, `measure.py`, `objects.py`, `node/api.py`, `providers/node.py` |
| `GET /volume/<ds>/plane_cells?z=&t=&column=`: the plane's cells (exact, from the index; else from `bbox_z0..z1`, flagged `source: "bbox"`) and each one's value in the loaded table. | `server/routes/volume_routes.py` |
| **Label planes prefetched** beside the image's standby planes (also for a following slab): adopted by `prepareLabelPlane`, so outlines arrive with the picture. | `viewerManager.prepareLabelStandby` / `takeLabelStandby`, `planeMotion.NeighbourPrefetch.neighbourStates` |
| **Masks in the XZ / YZ panes**: each cell's stepped outline where the pane cuts it, coloured as the plane colours it (`labelTile.cellColour` over `maskDrawList`, so the gate's cells only when a gate is on). `GET /volume/<ds>/section_cells` reads the runs along the cut at the pane's label level, coarser past 16 M voxels; local or node label volume. | `server/volume/section_cells.py`, `views/sectionCells.js`, `sectionPanes.strokeCellOutline` |

## Rules worth keeping

- **Thickness to planes**, centre to centre: plane z is in the slab of plane k when
  `|z - k| * dz <= T / 2`, so `half = floor(T / (2 dz))` and `2 half + 1` planes: 5 µm is 5 planes
  at 1 µm, 3 at 2 µm, 11 at 0.5 µm and one plane at 3 µm. Only the z step enters (anisotropy needs
  nothing more). A z step whose source is not `metadata` or `manual` is not a z step: the field is
  in planes.
- **Clip, do not shift**: at z = 0 a 5-plane slab is planes 0–2, not 0–4. The Depth card's own
  "Centre on the current plane" keeps its shifting behaviour (no `clip`).
- The plane counted, the outlines drawn and the plane named in the readout are always the same
  plane (`labelSpec`'s rule, mirrored in `GatePlaneControl.planeOnScreen`).

## Measured (synthetic 3 × 30 × 512² stack, 90 cells, GPU headless Chrome)

- Plane steps in Slab: readout and outlines both updated within the 100 ms polling interval; the
  outline planes were already built (label standby adopted on every step).
- `plane_cells` counts matched the server's own count on every plane checked; the server's slab
  mean matched numpy within 0.4 (rounding).
- The 3-D view hides the controls; Plane after Slab returns to the plane.

## Not done

- No new agent tool: an agent sets the same view with `viewer_set_slab` (no `clip` there yet).
- A slab's picture keeps the channel's saved window; a mean dims cells only partly in the slab,
  and a high saved minimum can hide them (core's contrast behaviour, unchanged).
- Real 1 TB stacks were not run here: everything per plane is chunk-aware (label planes are one
  chunk each; the index is memory-mapped), and the section read is capped by level.
