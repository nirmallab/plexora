# Slice & Clip: cutting the 3-D view open

Branch `feature/slice-clip` (worktree `../plexora-slice`), built on `feature/3d-controls` and
merged with main on 2026-10-12. Plan:
`~/.claude/plans/pasted-content-id-1ea9-implementation-transient-anchor.md`.

## What it does

The 3-D view can be cut in four ways, along any combination of X, Y and Z:

| Mode | What is drawn | Several axes |
|---|---|---|
| Full | the whole stack | – |
| Cutaway | a side taken off along each axis (keep the near or the far side) | one corner removed: where every active axis is on its removed side. The rest is kept, so the inside shows. |
| Section | a plane per axis, one voxel thick, in its real intensities (the channels' LUTs and windows) | the union of the planes (orthoslices) |
| Slab | a thick slice per axis, its voxels combined along each ray: one plane, maximum, mean, minimum, sum | the union of the slabs, combined as one region |
| Crop | a range kept along each axis | the intersection: a box |

Section and slab modes draw the rest of the stack dimmed ("Around", the context, 15 % by
default; 0 turns it off). Crop and cutaway can draw their cut faces solid ("Solid cut faces"),
so the voxels on the cut show rather than the projection through them.

The modes are exclusive. Each mode keeps its own settings while another is used, and they come
back with it. A mode that is not on has no effect on the picture.

## State and units

The cut is the view state's `inspect` (services/viewState.js), saved per image at
`/view_state/<ds>` and repaired on both sides (client `inspectGeometry.clean`, server
`volume_routes.normalize_inspect`):

```
inspect: {
  mode: "full" | "cutaway" | "section" | "slab" | "crop",
  axes: {x, y, z},                        // booleans, one at least
  crop:    {x: [x0, x1], y: [y0, y1], z: [z0, z1]},   // x/y px boundaries, z inclusive planes
  cutaway: {x: {at, keep: "lo"|"hi"}, y: {...}, z: {...}},
  slab:    {thickness: {x, y, z}, composite: "single"|"max"|"mean"|"min"|"sum"},
  opacity, context, face, linkPanes,
}
volume.interpolation: "linear" | "nearest"
```

All values are in IMAGE units: full-resolution pixels across x and y, plane indices along z.
Microns are shown only where the image or its user states the spacing (`metadata` or `manual`).
An assumed z step is never turned into microns. Section and slab positions are not part of
`inspect`: they are the view's own `z` and `section.x/y`. So the Z section in 3-D is the plane
the 2-D view comes back to, and with "Panes follow the sections" the XZ/YZ panes cut at the X
and Y sections.

`state.slab` is the 2-D projection's range again. It no longer clips the 3-D view. A 3-D view
saved with a narrowed range (before this branch) opens as a Z crop of those planes
(`viewState._migrate`).

## How a change reaches the picture

- `services/inspectGeometry.js` is pure maths. `resolve()` turns the state into a box, a cutaway
  corner (`hole`) and planes, in continuous image coordinates (plane k spans [k, k+1)).
  `toTexture()` turns those into the renderer's [0,1] numbers, relative to the area the textures
  cover (`covered`). Every consumer asks this one module.
- `services/inspectState.js` (`window.PlexoraInspect`) is the only way the controls change the cut:
  - `preview(patch)` while a hand is on a control: redrawn from uniforms in the next frame, with
    nothing saved and no revision. It announces `plexora:inspect-preview`.
  - `commit()` on release, and `set()` for a one-off change, each push one undo entry. Repeated
    key nudges of one grip fold into one entry.
  - `undo()`, `redo()` and `reset(axis|mode|cut|full)`.
- `inspect` is not in `revisionKey`. The renderer reads `PlexoraInspect.resolved()` at every
  frame (`inspectNow` → `cutUniforms`), so a cut follows the hand with no swap behind it.
- A cut is part of the renderer's `cameraKey`: it never dissolves through the crossfade ghost.
- In 3-D, `viewState.step` (`]` / `[`, Shift+wheel) moves whatever is cut along Z: the Z section
  or slab, a Z crop as a whole, or a Z cutaway's cut.

## The ray caster (views/volumeRenderer.js)

No new program variants. Every cut is uniforms on the one shader per channel count:

- `u_box_lo/hi` replace `u_clip_z` / `u_clip_xy`. The box test clamps each ray, so a crop costs
  nothing per sample. Video Studio's `setClip` still narrows it (it never widens it).
- `u_hole_*`: the cutaway corner, which the march skips.
- `u_cut == 1`, sections: each ray is intersected with each active plane and sampled exactly
  there (`sampleAt`, which reads the same textures as the march, bricks and details included),
  then composited nearest first at the section opacity. A one-voxel plane is never missed between
  two steps.
- `u_cut == 2`, slabs: the ray's intervals inside the active slabs, merged where they overlap,
  sampled at half the march's step (always at least once) and combined:
  - max, mean and min over the samples;
  - sum as the voxels added along the ray (`u_vox`).

  The march skips slab samples, so the context does not double-count them.
- `u_cut == 3`, cut faces: the voxel where the ray first meets kept voxels across a cropped side
  or the corner's face, drawn solid.
- Context: `u_color` / `u_opacity` are dimmed by `u_context` and the cut uses `u_cut_color`, so
  the march needs no extra branch. With context 0 the march is skipped.

`surfaceRenderer.volumeFragment` still patches this source at its anchors, every original anchor
byte-identical. One new swap puts a section, slab or cut face in front of or behind the nearest
translucent surface, by depth.

## Surfaces (views/surfaceRenderer.js)

Meshes are cut with the voxels:

- the box and the corner, as before;
- in section mode, all but one voxel either side of each plane is discarded, so the cells a
  section crosses show as rings on it;
- in slab mode, all but the slabs.

A closed surface cut open shows its inside as a CAP: back faces (`!gl_FrontFacing`), one flat
unlit tone of the object's colour, so a cut face never passes for its biology. Caps are on only
while something is cut. Server-made surfaces are wound outward; an imported mesh wound inward
would show caps everywhere while cut.

## The controls

- **The 3-D dock** (`views/volumeDock.js`). It sits top-right in the canvas layout, under the gizmo,
  and only in 3-D; it folds to "Cut ▸". Every row is an entry on the registry's `dock` surface
  (services/viewControls.js), shown only where it applies:
  - The cut row picks the mode (cube, scissors, grip-lines, layer-group, crop-simple).
  - Axes is a set of X/Y/Z chips, and the last one stays on.
  - Each active axis has a slider (a range in crop; one value in cutaway, section and slab) with
    typeable fields and a note in the image's units. Cutaway adds a flip for the kept side.
  - Slab adds Combine and Thick; Section adds Opacity. Section and slab have Around, the context.
    Crop and cutaway have Solid cut faces.
  - The tool row has handles on/off, Reset (an axis, the mode, every cut, the whole stack), and
    undo and redo.
  - "More" holds Voxels (smooth or blocks), the handles' strength, and Panes follow the sections.
- **Rows about voxels need the volume drawn.** Combine, Around, the cut faces and Voxels all need
  the `volumeDrawn` capability, so they are gone over "Surfaces only".
- **The depth strip's rail** in 3-D is the cut along Z:
  - in Full, the band is every plane, and dragging an end makes a Z crop;
  - in a Z crop, the band is resized and moved;
  - in a Z cutaway, the one end that is the cut moves;
  - in a Z section or slab, the thumb is the plane, picked and dragged;
  - with nothing cut along Z, a press does nothing.

  The drags preview and commit once. The readout says "all", "4–30", "z 12" or the mode.
- **The cog popover's** "Planes drawn" row is now "Cut": the cut in words, "Whole stack", and
  "Cut…", which opens the dock.
- **The Depth card** hides its projection rows in 3-D (`card.projection`).
- **The handles** (`views/volumeHandles.js`) are an SVG over the canvas, z-index 204. Each face the
  cut puts in the stack is outlined in its axis's gizmo colour with a grip at its centre: both
  faces of a crop plus its body, a cutaway's cut, a section's or slab's plane.
  - A press on a grip is stopped and captured before the canvas sees it. It never turns the stack.
  - The pointer's travel is measured along the axis as the screen shows it at the grip's depth,
    so perspective is included. The body moves along the two axes the screen shows best.
  - Dragging previews and commits once. Esc cancels; a double-click resets the axis; arrows nudge
    a focused grip a voxel (Shift ten).
  - The handles' visibility and strength are viewer prefs (`handles`).
- **Keys.** The 3-D view's X/Y/Z lock works over the dock and the handles too (`engaged()`), and
  Cmd/Ctrl+Z and Shift+Cmd/Ctrl+Z undo and redo a cut there.

## Fidelity and loading

- The base texture may be z-pooled, so a thin Z section or slab first reads pooled planes. A
  section or slab drawn alone (no context) along one axis narrows `visibleRegion` to it, so the
  detail sub-volume `considerDetail` fetches is that thin region at the finest level the budget
  allows. That means exact, unpooled planes.
- With bricks served, the atlas plans only the bricks the cut keeps (`view.box`), and for a
  section or slab drawn alone only the bricks it crosses (`view.only`).
- `renderer.resolved()` is false while a level, a brick batch or a detail is in flight, and the
  dock shows a breathing dot meanwhile.
- The contrast is levelled over the planes the cut keeps (`clipRange`): a Z crop's or cutaway's
  range, or a Z section's or slab's planes when drawn alone (`/volume/<ds>/hint`).

## Elsewhere

- **Figure Builder.** `scene.plugins.volume` carries `inspect`, `z`, `section`, `interpolation`
  and the cut in words (`cut`). Reopening a panel restores the cut. Preflight and export report
  `cut` for a 3-D panel (`export.volume_cut`).
- **Video Studio.**
  - A frame's view carries the cut it was made with (`view.inspect`, `view.section`), which the
    server keeps bounded and the browser repairs on apply.
  - The clip track still animates reveals through `setClip`.
  - A frame's slab still narrows z in 3-D, now laid into the clip, as before.
  - Animating `acquired_z` moves a Z section in 3-D.
- **Agent and MCP.**
  - `viewer_set_inspect` takes `mode`, `axes`, `crop`, `cutaway`, `z`, `section_x/y`,
    `thickness`, `composite`, `opacity`, `context` and `face`, in image units.
  - The server checks the form: ranges in order, a slab at least a voxel thick, something asked
    for. The browser repairs the cut and answers with exactly what it drew.
  - It enters 3-D. `viewer_set_z` takes `mode: "keep"` to move a Z section without leaving 3-D.
  - `viewer_get_state`'s volume part gives `inspect`, `cut`, `section`, `camera`, `show` and
    `box`.
  - The lease gives a cut back on restore.
  - It is in the MCP `volume` profile and the surface-rendering skill.

## Not done

- Oblique planes. `surfaceRenderer.setClipPlane` and the geometry module's per-plane records are
  the seam.
- A voxel-label rendering of 3-D masks. Surface Studio's per-cell surfaces are cut instead.
- One thickness slider for all axes, the same physical length. The agent can set them one by one.
- Video Studio animates a cut only through its clip track. `inspect` is a frame's static cut.

## Tests

- **Probes:**
  - `inspect_geometry_probe`: every mode and axis set on an anisotropic stack, plus texture space
    and units;
  - `view_state_probe`: the cut in the state, steps, migration, revision key;
  - `volume_renderer_probe`: cut uniforms per mode, `setClip` narrowing, shader functions;
  - `view_controls_probe`: the dock's truth table and the 3-D rail;
  - `depth_strip_probe`: rail drags in each mode, one undo per drag;
  - `volume_dock_probe`: rows per mode and axes, preview/commit, units, undo;
  - `volume_handles_probe`: drag maths against the real camera, never a turn, clamps;
  - `video_studio_scene_probe`: the cut in a frame, the slab into the clip.
- **pytest:**
  - `test_volume_routes` (the cut and handles round-trip);
  - `test_agent_viewer_api` (`viewer_set_inspect`, `set_z keep`);
  - `test_figure_builder_volume` (the cut in the report);
  - `test_video_schema` (the cut in a frame's view).
- **Shader compile:** every variant (1, 2, 3 and 6 channels; base, detail, atlas; with and
  without surfaces) was compiled on the GPU through ANGLE/Metal in headless Chrome.
