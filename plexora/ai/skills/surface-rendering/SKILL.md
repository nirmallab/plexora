# Surface rendering

Make, check and show surfaces of a z-stack: the isosurface of a channel, or one closed
surface per cell of a volumetric mask. A surface is a picture of the data with a recorded recipe,
never a new measurement of record: what you say about it names how it was made.

## When to use

- The user asks for a surface, an isosurface, a mesh, a volume rendering of vessels, nuclei
  or cells, or "show it in depth like Imaris".
- A volumetric mask exists and the user wants to see, colour, isolate or export its cells.
- The user asks which objects are near one another in depth.

## When not to use

- A planar image (`inspect_volume` says `volumetric: false`): there is nothing to surface.
- Counting, volumes or statistics to report: the mask's voxel measurements
  (`measure_objects`) are the quantitative ones; a mesh describes the drawing.
- Segmenting: a surface never edits a mask, and a threshold here is not a segmentation.

## Required features

A z-stack on this machine (volume-triage first). Sizes in µm need a known z step
(`inspect_volume` `spacing.z.source`); without one every size is in voxels and says so.
Cell surfaces need a label volume (a volumetric mask). Surface Studio needs a
licence with `volume` (`volume:surfaces`); over MCP also `mcp`. A refusal's
`detail` says so in words to pass on.

## Decision logic

1. `inspect_volume`, then `surface_list`: reuse a surface that already answers the question.
2. Isosurface of a channel:
   - `surface_suggest_threshold` gives Otsu, triangle and mixture thresholds in RAW units.
     They are starting points from the histogram, not answers.
   - `surface_create` with `type: "isosurface"`, the channel and `iso` in raw units. Use
     `quality: "preview"` first on a large stack; `standard` for the surface to keep.
   - Blurry or speckled stain: `sigma_um`. Uneven background: `background_um` (the
     diameter of the objects to keep). Noise pieces: `min_size`.
3. Cells of a volumetric mask: `surface_create` with `type: "label_surfaces"` (all cells, or
   `ids`). Each surface carries its cell's id, so the table's rows and the surfaces line up.
   In the volume view, the viewer's own Outlines and Filled draw the whole mask this way
   (`viewer_set_cell_mode` with outlines or filled): built the first time, a preview then
   the standard surfaces, as glass (outlines) or solid (filled); `viewer_get_state` says
   whether they are building or shown. Use that to show the mask; use `surface_create` for
   a selection of cells or your own recipe.
4. Look before you conclude: `viewer_set_volume_mode`, `viewer_set_camera`, then
   `viewer_capture`. `viewer_set_volume_mode` with an `area` (image pixels) draws just that
   part of the image, finer. `viewer_set_inspect` cuts the stack open to see inside -- a
   cutaway takes a side off along one axis, a crop keeps a box -- and the surfaces are cut
   with it. For
   an object in its real intensities, look at a plane through it (`viewer_set_z`,
   `render_sections`). `viewer_set_surface` isolates or highlights objects in this tab only;
   `surface_update` saves a look.
5. Check it: `surface_inspect` (fragments, objects cut by the volume's edge, open objects,
   how far smoothing moved it, mesh versus voxel volume). Fix with `surface_refine`
   (`remove_small`, `keep_largest`, `delete`, `keep`, `smooth`, `decimate`; `undo`).
6. Neighbours: `surface_neighbours` gives surface-to-surface or centroid distances (and,
   for cells, the contact area the mask says two cells share).
7. Numbers: `surface_measure` for distributions; `surface_objects` only when the user
   needs per-object rows.
8. Export: `surface_export` (PLY, OBJ, STL, GLB with its manifest).

## Tools

`inspect_volume`, `surface_list`, `surface_suggest_threshold`, `surface_create`,
`surface_inspect`, `surface_refine`, `surface_measure`, `surface_objects`,
`surface_neighbours`, `surface_update`, `surface_export`, `surface_delete`,
`viewer_set_volume_mode`, `viewer_set_camera`, `viewer_set_inspect`, `viewer_set_surface`,
`viewer_capture`, `measure_objects`.

## Evidence

- Cite the surface's recipe: channel, isovalue in raw units, smoothing, quality and level
  (`surface_list`), and what `surface_inspect` found.
- Show it: a `viewer_capture` of the volume view, and the plane through an object (the plane
  views draw where each surface cuts them) when a boundary matters.
- Report a cell's size from its voxel columns, and say which you used.

## Uncertainty

- An isosurface is the boundary at one chosen value. A nearby value can draw a different
  shape; say which value, and that it was chosen, not validated.
- Smoothing and the inset move vertices by up to about half a voxel: mesh areas and
  volumes are close to the mask's, not equal to them.
- An object that touches the edge of the volume is cut there: its volume is of what is
  inside the stack.
- Distances between surfaces are geometry. Two cells that touch are near; whether they
  interact is not something a surface can say.

## Mutation policy

Every create, refine, update and export is a recorded write with a receipt. `surface_refine`
writes a new version (`undo` returns to the one before). `surface_delete` removes the mesh
files and needs the user's explicit request (`confirm: true`). Nothing here writes into an
image or a mask.

## Provenance

Each surface's manifest records its source, parameters, spacing, algorithm, level, smoothing
report and versions; an export carries it beside the mesh. Name the surface id and its
recipe in what you report.

## Done when

The surface answers the user's question, `surface_inspect` has been read and any fix made,
a capture shows it, and what you report says what was measured on the mask and what was
drawn.

## Failure modes

- `license_required`: Surface Studio is not on this licence; pass on the refusal's hint.
  Without it the viewer does not offer Outlines or Filled in the volume view at all.
- Nothing crosses the isovalue: the value is outside the channel's range; suggest one.
- Too many triangles: choose a lower quality, a region (`box`, `planes`) or fewer cells.
- `resource_not_local`: the stack is on another machine; surfaces are made where it is.
- A dark or empty capture: the volume view may be showing only the volume
  (`viewer_set_volume_mode` with `show_surfaces`).
