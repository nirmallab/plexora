# Slice & Clip: cutting the 3-D view open

Branch `feature/slice-clip` (worktree `../plexora-slice`), built on `feature/3d-controls` and
merged with main on 2026-10-12. Plan:
`~/.claude/plans/pasted-content-id-1ea9-implementation-transient-anchor.md`.

On 2026-10-13 the five cut modes became two tools, Cut and Crop, on the depth strip. The
floating 3-D dock became a compact panel beside the strip. Sections and slabs were removed
completely, with "Panes follow the sections", the pinned pane crosshair, the panes' cut guides
and the view state's `section` position.

On 2026-10-21 (branch `feature/area-tool`, modelled on milume's Inspect) the 3-D view gained an
AREA, a Cut became one axis (the corner it took out over several axes, `hole`, is gone), "Crop
to area" and saved areas came in. See "The area" below.

On 2026-10-22 (`feature/bookmarks`) the area became a temporary view of its own: the mode
buttons always mean the whole stack and leaving 3-D lets the square go; Back (B) returns to the
view a square or Crop to area came from (`services/viewHistory.js`); the area has its own grips;
the square keeps its size on screen; and saved areas were retired for bookmarks, which are
Figure Builder captures (`services/bookmarks.js`).

## What it does

The 3-D view draws an area of the stack (the whole stack, or the Area tool's square) and can be
cut inside it with one tool at a time:

| Tool (`inspect.mode`) | What is drawn | Axes | Starts on |
|---|---|---|---|
| none (`full`) | the whole area | – | – |
| Cut (`cutaway`) | a side taken off (keep the near or the far side) | ONE of X, Y or Z: the panel's chips are a radio | X |
| Crop (`crop`) | a range kept along each axis: a box | any of X, Y and Z | X, Y and Z |

A Cut saved over two or three axes (before 2026-10-21) opens on its first, x before y before z
(`inspectGeometry.cleanAxes(..., {single})`, the server's `axes_of(..., single=True)`); the agent
refuses a cutaway named with more than one axis.

Either tool can draw its cut faces solid ("Solid cut faces"), so the voxels on the cut show
rather than the projection through them.

Each tool keeps its own settings and its own axes while the other is used, and they come back
with it. Hiding the cut (`O`, or the panel's Show/Hide) shows the whole stack and keeps them
too, the panel left out, so a cut can be flicked on and off while looking; the panel's × does
the same and closes the panel.

## State and units

The cut is the view state's `inspect` (services/viewState.js), saved per image at
`/view_state/<ds>` and repaired on both sides (client `inspectGeometry.clean`, server
`volume_routes.normalize_inspect`):

```
inspect: {
  mode: "full" | "cutaway" | "crop",       // the tool on, or the whole stack
  tool: "cutaway" | "crop",                // the tool last chosen, kept while it is off
  axes:   {x, y, z},                       // the current tool's: one at least, exactly one for a Cut
  axesBy: {cutaway: {x, y, z}, crop: {x, y, z}},   // each tool's, as last left
  crop:    {x: [x0, x1], y: [y0, y1], z: [z0, z1]},   // x/y px boundaries, z inclusive planes
  cutaway: {x: {at, keep: "lo"|"hi"}, y: {...}, z: {...}},
  face,
}
volume.interpolation: "linear" | "nearest"
volume.area:  {x, y, width, height} | null      // full-res px; null = the whole stack
```

(`volume.saved`, the saved areas, is gone: a stored one is dropped on read.)

Every position the cut holds lies inside the area (`clean` clamps to `whole(dims)`, the area or
the stack), and moving the area moves the cut with it (see "The area").

All values are in IMAGE units: full-resolution pixels across x and y, plane indices along z.
Microns are shown only where the image or its user states the spacing (`metadata` or `manual`).
An assumed z step is never turned into microns.

Picking the other tool goes through `inspectState.withTool`: the tool it replaces leaves its
axes in `axesBy`, and the new tool takes its own back. Axes a patch names itself (the agent's)
win. A cut saved as a section or slab opens as the whole stack.

`state.slab` is the 2-D projection's range. It does not clip the 3-D view. A 3-D view saved
with a narrowed range (before Slice & Clip) opens as a Z crop of those planes
(`viewState._migrate`).

## How a change reaches the picture

- `services/inspectGeometry.js` is pure maths. `resolve(inspect, dims)` turns the state into a
  box, in continuous image coordinates (plane k spans [k, k+1)), inside `dims.area`.
  `toTexture()` turns it into the renderer's [0,1] numbers, relative to the area the view
  FRAMES (`renderer.frameCovered()`). Every consumer asks this one module.
- `services/inspectState.js` (`window.PlexoraInspect`) is the only way the controls change the cut:
  - `preview(patch)` while a hand is on a control: redrawn from uniforms in the next frame, with
    nothing saved and no revision. It announces `plexora:inspect-preview`.
  - `commit()` on release, and `set()` for a one-off change, each push one undo entry. Repeated
    key nudges of one grip fold into one entry.
  - `pick(tool)`, `toggle()` (the tool off and on), `undo()`, `redo()` and
    `reset(axis|mode|full)`.
- `inspect` is not in `revisionKey`. The renderer reads `PlexoraInspect.resolved()` at every
  frame (`inspectNow` → `cutUniforms`), so a cut follows the hand with no swap behind it.
- A cut is part of the renderer's `cameraKey`: it never dissolves through the crossfade ghost.
- In 3-D, `viewState.step` (`]` / `[`, Shift+wheel) moves whatever is cut along Z: a Z crop as
  a whole, or a Z cutaway's cut.

## The ray caster (views/volumeRenderer.js)

No program variants of its own. Every cut is uniforms on the one shader per channel count:

- `u_box_lo/hi` (the box test clamps each ray, so a crop costs nothing per sample). Video
  Studio's `setClip` still narrows it (it never widens it).
- `u_tex_lo/span`: where the frame's [0,1] lies in the base textures (`baseRead`): identity for
  a level-0 texture of the area, a few voxels' shift at a coarser level, and the old area's
  texture while a new area loads (nothing is read outside it).
- `u_cut == 1`: the cut faces. The voxel where the ray first meets kept voxels across a cropped
  or cut side, sampled there (`sampleAt`, which reads the same textures as the march, bricks and
  details included) and drawn solid.

`surfaceRenderer.volumeFragment` still patches this source at its anchors, every original anchor
byte-identical. One swap puts a cut face in front of or behind the nearest translucent surface,
by depth.

## Surfaces (views/surfaceRenderer.js)

Meshes are cut with the voxels, by the box. A closed surface cut open shows its
inside as a CAP: back faces (`!gl_FrontFacing`), one flat unlit tone of the object's colour, so
a cut face never passes for its biology. Caps are on only while something is cut. Server-made
surfaces are wound outward; an imported mesh wound inward would show caps everywhere while cut.

## The controls

- **The depth strip** (`views/depthStrip.js`) offers Cut (scissors) and Crop (crop) in 3-D,
  under the mode buttons; the tool that cuts is lit. It has no rail in 3-D and no readout in
  any mode.
- **The cut panel** (`views/volumeDock.js`) opens to the strip's LEFT from either tool, as an
  extension of the strip: the same ground, border and type, its right edge the strip's left
  border, sliding out from under it (`clip-path`, no motion under reduced motion). Every
  control is an entry on the registry's `dock` surface (services/viewControls.js), for the tool
  cutting or, while the cut is hidden, the one last chosen:
  - the head: the tool's name, › (collapse; the cut stays as it is), × (close: back to the
    whole stack);
  - X, Y and Z down the left, each a chip in its handles' colour; the last one stays on. In a
    Cut each axis cut has a flip beside it whose full half is the side kept;
  - beside them, Solid cut faces, the handles shown or hidden (eye), Reset (the tool's cut
    back where it starts, the axes kept) and Undo;
  - Voxels (smooth or blocks) across the panel's width, then the handles' strength;
  - at the foot, `O` Show/Hide: the cut hidden or shown again, the panel left out.

  Crop is the same panel; its chips fill the flip column. Pressing the lit tool again collapses
  the panel, and again brings it back; Esc in the panel collapses it. While the cut is hidden
  the panel stays out with its axes and actions dimmed and the `O` key cap lit; any of them
  used, or the strip's tool pressed, shows the cut again (one undo entry). Whether the panel is
  out is remembered in the browser (`plexora.cutPanel.open`); it first comes out from a tool.
- **The key `O`** hides the cut and shows it again, settings kept and the panel left as it is,
  anywhere the 3-D view is on screen and nobody is typing or in a dialog. No modifier, no
  repeat. The panel's Show/Hide and the strip's tooltips say so.
- **Rows about voxels need the volume drawn.** The cut faces and Voxels need the `volumeDrawn`
  capability, so they are gone over "Surfaces only".
- **The cog popover** sits left of the panel when the panel is out; it no longer carries the
  cut.
- **The Depth card** has no projection rows (2026-10-13: the gear's alone, VIEW_CONTROLS.md); in
  3-D it hides the plane and the sections' sizes.
- **The handles** (`views/volumeHandles.js`) are an SVG over the canvas, z-index 204. Each face the
  cut puts in the stack is outlined in its axis's gizmo colour with a grip at its centre: both
  faces of a crop plus its body, a cutaway's cut. An area has its own, white and dashed, shown
  with no cut too: a grip on each of its four sides resizes it (`inspectState.resizeArea`: the
  cut stays where it is in the image, clamped into the new area; never under 16 px) and one in
  its middle slides it exactly as Shift+drag does (`volumeRenderer.dragArea`, the same
  SLIDE_MS throttle, one undo entry, the cut carried). A crop's grips win where they meet the
  area's on screen (Crop to area puts the crop on the area's sides).
  - A press on a grip is stopped and captured before the canvas sees it. It never turns the stack.
  - The pointer's travel is measured along the axis as the screen shows it at the grip's depth,
    so perspective is included. The body moves along the two axes the screen shows best.
  - Dragging previews and commits once. Esc cancels; a double-click resets the axis; arrows nudge
    a focused grip a voxel (Shift ten).
  - The handles' visibility and strength are viewer prefs (`handles`).
- **Keys.** The 3-D view's X/Y/Z lock works over the panel and the handles too (`engaged()`), and
  Cmd/Ctrl+Z and Shift+Cmd/Ctrl+Z undo and redo a cut there.

## Fidelity and loading

- With bricks served, the atlas plans only the bricks the cut keeps (`view.box`).
- `renderer.resolved()` is false while a level, a brick batch or a detail is in flight, and the
  panel shows a breathing dot meanwhile.
- A cut does not touch the contrast: a stack has one per channel for every view
  (views/viewerSidebar.js `autoChannel`), so dragging a cut reads nothing for it.

## Elsewhere

- **Figure Builder.** `scene.plugins.volume` carries `inspect`, `z`, `interpolation` and the cut
  in words (`cut`). Reopening a panel restores the cut. Preflight and export report `cut` for a
  3-D panel (`export.volume_cut`).
- **Video Studio.**
  - A frame's view carries the cut it was made with (`view.inspect`), which the server keeps
    bounded and the browser repairs on apply. A frame cut as a section or slab plays the whole
    stack.
  - The clip track still animates reveals through `setClip`.
  - A frame's slab still narrows z in 3-D, laid into the clip, as before.
- **Agent and MCP.**
  - `viewer_set_inspect` takes `mode` (`full`, `cutaway`, `crop`), `axes`, `crop`, `cutaway` and
    `face`, in image units.
  - The server checks the form: ranges in order, something asked for. The browser repairs the
    cut and answers with exactly what it drew.
  - It enters 3-D. `viewer_get_state`'s volume part gives `inspect`, `cut`, `camera`, `show`
    and `box`.
  - The lease gives a cut back on restore.
  - It is in the MCP `volume` profile and the surface-rendering skill.

## Not done

- Oblique planes. `surfaceRenderer.setClipPlane` is the seam.
- A voxel-label rendering of 3-D masks. In 3-D the Cells control's Outlines and Filled draw
  the mask as cell surfaces (services/maskSurfaces.js, SURFACES.md §4), which are cut with
  the voxels.
- Video Studio animates a cut only through its clip track. `inspect` is a frame's static cut.

## Tests

- **Probes:**
  - `inspect_geometry_probe`: both tools over every axis set on an anisotropic stack, each
    tool's axes, a saved section opening whole, texture space and units;
  - `view_state_probe`: the cut in the state, steps, migration, revision key, no `section`;
  - `volume_renderer_probe`: cut uniforms per tool, `setClip` narrowing, no section or slab
    uniform or shader function left;
  - `view_controls_probe`: the strip's Cut and Crop, the panel's truth table;
  - `depth_strip_probe`: Cut and Crop in 3-D, lit while they cut, no rail;
  - `volume_dock_probe`: the panel per tool, each tool's axes, flips, collapse, ×, `O` and
    Show/Hide with the panel left out, edits while hidden, undo;
  - `volume_handles_probe`: drag maths against the real camera, never a turn, clamps;
  - `section_panes_probe`: nothing of the 3-D cut left in the panes;
  - `video_studio_scene_probe`: the cut in a frame, the slab into the clip.
- **pytest:**
  - `test_volume_routes` (the cut and handles round-trip, each tool's axes, a saved section
    opening whole);
  - `test_agent_viewer_api` (`viewer_set_inspect`, the old modes and options refused);
  - `test_figure_builder_volume` (the cut in the report);
  - `test_video_schema` (the cut in a frame's view).
- **Shader compile:** every variant (1, 2, 3 and 6 channels; base, detail, atlas; with and
  without surfaces) was compiled on the GPU through ANGLE/Metal in headless Chrome on
  2026-10-12. After the 2026-10-13 removal, the base and surface variants were compiled and
  drawn through SwiftShader in headless Chromium (the live app, a Cut over X and Y).

## The area (2026-10-21)

The view state's `volume.area` is what the 3-D view loads and frames; the cut decides what is
drawn inside it. Moving the area downloads; changing a cut never does.

- **Placing it.** The Area tool (`views/areaTool.js`, the strip's Area button, plane and
  projection only): a dashed square follows the pointer, the viewer pref `area` (`{screen: 320}`)
  its side ON SCREEN -- `sizePx` divides by `viewerScene.scaleOf`, so zooming in takes less
  tissue and out more; the wheel zooms and a drag pans while it is armed, and `onViewportChange`
  keeps the square under a still pointer at the new zoom. A readout under it says how much tissue
  (`inspectGeometry.label`: µm where the spacing is trusted, else px). A click, or V with the
  pointer on the image, pushes the view onto the history and is ONE
  `viewState.enterVolume({area})`. From a fixed projection the cut becomes a Z crop of its planes
  (`viewState._bandCrop`); a following one never does.
- **It is temporary.** The mode buttons' `viewControls.transition` and `viewState.toggleVolume`
  set `volume.area: null` (the 3-D button is always the whole stack); `enterVolume()` with no
  area does the same. A square let go puts the cut back on the whole stack's places, mode `full`,
  its tool and axes kept (`viewState._wholeCut`), unless the patch says where the cut is (a
  restore, Back). Nothing of the square is drawn on the plane once 3-D is left.
- **Back** (`services/viewHistory.js`, `views/backButton.js`): the Area tool's `place` and
  `cropToArea` push where the view was -- mode, plane, range, area, cut, the 3-D camera or the
  2-D view (`viewerScene.viewOf`), never channels or anything rendered. `back()` is one
  `PlexoraInspect.set(..., {extra, exact: true})`, then `renderer.turnTo` (a 3-D parent) or
  `viewerScene.glideTo` (a 2-D one), instant under reduced motion. The pill sits in the
  top-center dock while there is history; B is Back's in the capture phase while there is
  history (preventDefault; `datasetNav.onKeyDown` stands aside for a taken key), else the
  dataset chip's. Going back to 2-D any other way ends the trail.
- **V** toggles 2-D and 3-D on any z-stack (`viewState.toggleVolume`: back to the projection when
  it was entered from one). It yields to the ROI tool, typing and open dialogs.
- **The cut moves with it.** `viewState.set` with a new `volume.area` and no `inspect` shifts the
  cut by the area's move (`inspectGeometry.shift`); a patch that says where the cut is (a restore,
  a Video Studio frame, Crop to area, an undo) is never shifted.
- **Sliding it.** In 3-D, Shift+drag slides the area under a fixed frame (the tissue follows the
  hand); Alt+drag or the middle button pans the camera (Shift+drag pans when there is no area).
  `onSlide` at most every `SLIDE_MS` (40 ms), the last on release; a slide is one undo entry with
  the cut it carried (`inspectState.slideArea`). Under the hand, a fetch waits while two loads a
  channel are in flight (`setArea`), so a fast slide asks for where it is.
- **Never a blank frame.** `entryFor` stands the last-drawn texture in for any area;
  `frameCovered()` is the area's, and `baseRead` reads the old texture where the new area
  overlaps it. A same-size slide keeps the level (`sameSize`), so the atlas made for it stays and
  its bricks (absolute) keep drawing. The area's own texture dissolves in at the same camera.
- **Crop to area** (`cut.toArea`, while the crop is narrower than the area in x or y): one change,
  the area the crop's x and y, the crop the area — fetched at the finest level that box allows.
- **Bookmarks** replaced saved areas (2026-10-22): `services/bookmarks.js` keeps views as
  Figure Builder captures (`bookmarked`, `label`), 2-D and 3-D; in 3-D the capture's
  `scene.plugins.volume` (services/surfaceLayers.js `sceneState`) carries the camera, the area,
  the cut and the surfaces, and on a plane or a projection which one. A restore is kept
  (`FigureScene.restore(..., {persist: true})`, `applyScene(..., {save: true})`).
