# Testing the 3-D / 4-D branch before it merges

Branch `feature/volumetric-3d`, worktree `../plexora-3d`. Nothing here is on `main`.
Progress and decisions: `PROGRESS.md` beside this file.

## Running the branch

From the worktree, with the usual environment:

```bash
cd ../plexora-3d
PYTHONPATH="$PWD" ~/miniconda3/envs/plexora/bin/python run.py 8765
```

`PYTHONPATH` matters: the conda environment's editable install points at the main checkout.
To keep your real projects untouched while trying things, add
`PLEXORA_DATA_PATH=/some/scratch/folder` (and import copies there).

Going back to `main` afterwards is safe: every new project key is additive and is kept,
unread, by `main`. ROI documents stay schema version 1, so `main` opens them and simply
forgets which planes a region was limited to.

## What to try, and what you should see

**Opening z-stacks.** A confocal OME-TIFF, the Xenium run's `morphology.ome.tif`
(`~/Downloads/Xenium_V1_Human_Ovarian_Cancer_Addon_FFPE_outs`), an Imaris `.ims`, an OME-Zarr
with a z axis, a DICOM fluorescence slide with focal planes, or any of these at a web address.
Expect: the viewer opens on the middle plane, a Z strip at the right edge, and a badge under
the sample name (`z 7/14 · 21 µm`). A 2-D image must look exactly as before.

**Moving through planes.** Drag the strip's rail, press `]` / `[` (Shift for ten, Alt for the
ends), or Shift + mouse wheel over the image. The old plane stays until the new one has drawn,
every channel changes plane in the same frame, and the picture dissolves into the new plane over
up to 0.16 s (shorter while you scrub fast) -- on screen only: when it ends you see the plane the
readout names, unblended, and captures never contain a fade. A pan or zoom during a fade ends it
at once. Under the hand the rail's thumb moves between planes and settles onto the plane on
screen when you let go. The next planes in the direction you are going (and one behind) are
built ahead, invisibly, so the next step is immediate: two ahead at rest, up to six ahead while
you hold a key, asked for as each plane lands. A jump (a click far down the rail, Alt+`]`, a
typed plane) no longer waits behind a plane still loading: the plane on its way is dropped and
the jump goes at once. A step shows the next plane's coarse tiles before the last plane finishes
sharpening. In a projection that follows the plane, a step no longer waits for the new range's
contrast statistics: it shows at once, and the contrast eases to the new range a moment after
you stop.

**Speed and staying in step (the performance pass, `PERF.md`).** Try these on the Imaris stack
and the light-sheet stack, with the section panes on:

- *No brightness flash on a Z step.* Step with `]` through a bright and a dim plane: the picture
  dissolves from one to the other and is never brighter than both mid-fade.
- *Everything moves together.* Step or hold `]`: the plane, the panes' z-line, the 3-D label
  outlines and (in a projection) the contrast change in the same frame. Pan and zoom: the panes
  follow within about a tenth of a second rather than over a second later.
- *Channels.* Switch a channel off and on, or swap one for another, in the plane and in 3-D:
  it should be immediate (a channel already seen is not downloaded again). In 3-D, entering,
  leaving and re-entering over the same area fetches nothing; a reload re-enters 3-D without
  downloading the levels again.
- *Contrast drags* move the plane, both panes and the 3-D view together.
- *First open of a stack nobody has opened before* (or after deleting its saved windows): the
  first picture appears on a provisional brightness within about a second, and may shift once,
  a little, when the exact brightness arrives seconds later. Nothing on screen says the first
  brightness is provisional yet -- tell me if it should.
- *3-D detail.* In 3-D, zoom in on part of a large stack and hold still: within about two
  seconds that part sharpens (a finer level drawn inside its box); turning keeps the frame rate.
- *A project whose image is on a data node* behaves the same: 3-D levels, windows and sections.

**Projections.** The strip's second button, or Show under its gear. Maximum, mean, minimum,
standard deviation, median, percentile, over a range you set (the gear's Planes). The badge
turns amber and names the range (`MIP z 0–13 · 14 planes · 42 µm`). "Add as a layer" computes it
exactly at full resolution and adds it to Layers; removing that layer deletes the file. A
projection too deep to compute at the zoom on screen (about 1 GB of planes per tile, e.g. all
1937 planes of a 2048² light-sheet stack) is drawn from a coarser level: a thinner range is
sharper, and the pinned layer is exact.

**Contrast on a stack.** Channels you have not set follow the view: a projection and the 3-D
view get their own auto-contrast, and going back to a plane restores the plane's. The wand
auto-contrasts whatever is on screen. Ranges you set, or that were saved earlier, are kept. A
stack whose 8-bit tiles cannot show its default range (a dim stack with a few saturated voxels)
turns HD on by itself and says so once.

**Sections.** The strip's grid button: XZ along the bottom and YZ along the right, cut through
the centre of the view and lined up with it. Zoom in to see them grow to the stack's real depth.
Hover a pane's inner edge (a thin accent line with a grip appears) and drag it either way to
resize the pane; focus it and use the arrow keys (Shift for bigger steps); double-click it, press
Home, or use the Depth card's "Default sizes" to restore the default. The size you choose is the
size shown. While the stack at the plane's scale is taller than the pane, the pane shows a window
of its planes at the plane's proportions. Past the stack's own height every plane is shown and z
is stretched to fill the pane, in whole-pixel bands. The label then says how much ("XZ · y 343 ·
z ×2.4"); within 4 px of the stack's own height the pane snaps to it ("· all planes"). A pane
never covers the last 240 px of the plane, or the depth strip. Nothing reloads while you drag:
the cut holds still until you let go, then moves to the middle of the part of the plane the
panes leave uncovered, and the sections there are fetched once. The zoom buttons and the legend
move clear of a tall XZ pane. Sizes are yours, not the image's: they hold across images, modes
and restarts.

**The strip follows the mode, and the cog.** Every control on the strip is one that does
something now. In 3-D the plane rail and the Sections button go (the plane under the view is
frozen there), and Cut and Crop come; the strip is only as tall as its buttons. `]` / `[` and
Shift+wheel move whatever is cut along Z. There is no readout under the rail in any mode. The
cog opens the settings for the mode on screen, one line each:
- on a plane, Show (one plane or a projection's operation) and Motion;
- in a projection, the operation, the percentile, the planes, Follow and Motion;
- in 3-D, Render (MIP/Additive) and Motion; Turn (the axis, X Y Z in the gizmo's colours, and
  the direction) and Speed; Grid (on, walls or floor, the labels) and, while it is on, Lines
  (spacing, Auto, strength); Surfaces (what is drawn once there is one, New and Import as icons)
  and Light.

No headings and no notes: hover a control for what it does (Speed says how long a turn takes).
With the grid off its row is the switch alone. Change the mode with the popover open and its
rows change in place. Escape, a click elsewhere, a resize or a scroll closes it; with Play
turning, the first Escape closes the popover and the second stops the turn. Nothing you change
there moves the camera. The full matrix is in `VIEW_CONTROLS.md`.

**The Depth card** (View → Depth) opens the sidebar if it is collapsed and scrolls to the card.
It holds what the strip and its gear do not: the plane or frame typed exactly, Add as a layer,
the z step, the 3-D copy, Cells in 3-D and the sections' default sizes. In 3-D the plane and the
sizes go.

**Play in 3-D turns the stack.** In 3-D, Play turns the stack about the screen's vertical at
30°/s from where the camera is, keeping its zoom and pan, until you press it again, press on the
view, press Esc, lock an axis or pick a view. The cog sets the speed (5–90°/s), the axis (the
screen's vertical, or the image's X, Y or Z, which then stays still on screen) and the
direction; changing them while it turns takes effect at once. It turns even with Motion off,
since it is your action and one press stops it. A figure capture records the pose and leaves it
turning; a video render stops it. Whether it was turning is never saved.

**No flash in 3-D.** Turn the stack and let go, then zoom in on part of a large stack and wait
for it to sharpen. Toggle HD, or wait for a new stack's brightness to turn exact. The picture
should never go black or jump in brightness. A channel being replaced keeps its old texture,
read in its own window, until the new one lands. A finer picture at the same camera (the full
frame after a turn, a texture, a detail or bricks landing, a channel switched off) dissolves in
over a sixth of a second rather than cutting. Under MIP a turning stack keeps its brightness:
the same number of samples moving as at rest.

**3-D view.** The cube button: a maximum-intensity or additive rendering of the whole stack
(the projection's range is the projection's alone). Drag to turn it and let go while moving: it keeps turning
and slows to a stop -- a hard flick about 55 degrees, an ordinary drag about 25, a slow placed
release not at all. Any press stops it. The wheel's zoom eases; the view presets (and
double-click) turn there rather than cut. With the view focused, the arrow keys turn it, + / -
zoom, 0 returns to the three-quarter view. While it moves it may draw at a lower resolution if
frames come slowly; it is redrawn in full once it rests.

*Contrast that follows the cut.* In 3-D, crop Z to a dim part of the stack (say the deepest few
planes), or cut along Z: channels whose range you have not set
re-level to the planes kept, about a quarter of a second after you stop dragging, and back to the
whole stack's when you widen it again. A channel whose range you set (or that was saved) is left
alone.

**Slice & Clip** (docs/internal/3d/SLICE_CLIP.md). In 3-D, the strip's Cut and Crop cut the stack:
- **The two tools.** Press Cut (scissors): the stack loses a side along X, and the cut panel
  slides out to the strip's left. Press Crop: the panel changes in place, cropping on X, Y and Z.
  - Cut: each axis takes a side off; X+Y takes out one corner, so the inside shows. The flip
    beside an axis keeps the other side; its full half is the side kept.
  - Crop: each axis keeps a range; X+Y+Z keeps a box.
- **Dragging, and the camera.** Drag a handle's grip in the view: the face follows the hand at
  any angle, and the camera never moves. Esc puts a drag back. The release is one undo step (the
  panel's Undo, or Cmd/Ctrl+Z over the view or the panel).
- **Settings are kept.** Change Cut's axes, switch to Crop and back: Cut's axes, flips and
  faces are as they were. Collapse the panel (›, Esc, or the lit tool again): the cut stays.
  Press `O` with the pointer anywhere: the whole stack, and the panel stays out, dimmed, its
  `O` key cap lit; `O` again (or the panel's Show/Hide): the cut, exactly as it was. Hide it
  and click an axis or Reset: the cut shows again with the change. × closes the panel and
  goes back to the whole stack. Reload: the cut is kept.
- **Surfaces.** With a cell surface shown, the meshes are cut with the voxels, and a cut-open
  cell shows a flat cap.

**Which way it faces, and turning about one axis.** In 3-D a small X / Y / Z gizmo sits in the
top-right corner (x red, y green, z blue), faint until you hover it, move the view or lock an
axis. It follows every turn. Click X, Y or Z to look along that axis at the plane across it --
X gives the YZ plane, Y the XZ plane, Z the XY plane, oriented exactly as the section panes show
them -- and click it again (or the small dot at the far end) for the far side. With the pointer
over the 3-D view, press X, Y or Z: dragging (and its coast) then turns the stack about that axis
of the image only, the gizmo rings it and says "Turning about X · Esc"; the same key again or Esc
lets go, another axis key switches. Elsewhere on the page those letters keep doing what they did
(Thresholding's Z / X step markers). The menu's view presets were mislabelled before (its "XZ"
showed the XY face); they now match.

**Nothing over the image overlaps.** The caption, the dataset arrows, the legend, the mini-map,
the scale bar, the orientation gizmo and a plugin's floating panel (Figure Builder's capture
dock) now share one layout: the depth strip and the section panes each hold a band along an edge,
and everything else stacks in the corners of what is left. Try Figure Builder with sections on,
with a wide YZ pane, and in 3-D: its dock sits top-right beside the strip (no longer over it),
the gizmo stacks under it, the legend stays above the XZ pane, the scale bar steps left of the
panes. Resizing a pane or opening a panel moves controls, never the image.

*When there is not room.* Make the window short (or drag the XZ pane tall) with Figure Builder
open: its dock folds to its head rather than scrolling under the legend, and opens again when
the room comes back (your own open/closed choice is kept apart; its button opens it while
folded). An open mini-map folds the same way. The Plexora AI chat runs along the bottom between
the mini-map and the legend while that gap is at least about 320 px wide, and above them when it
is not; a closed mini-map now takes only its button's room. With the sidebar closed, its "show"
button sits above the caption instead of on it, and a running AI session's chip sits under the
caption. Figure Builder's capture frame stays out from under the strip and the section panes,
and steps clear when one opens.

**Motion off.** Motion, under the strip's gear, stops the plane fades, the thumb's glide, the
3-D coast and every eased turn or zoom, for every image. The system's reduced-motion setting
does the same (the switch then says so).

**A colour (brightfield) DICOM slide with several focal planes** now opens as a stack: the strip
steps through its planes (each the RGB picture scanned at that depth, swapped in only once it has
drawn), but offers no projection, sections or 3-D -- a maximum or mean of colour samples is not a
picture anybody stained. The strip and its gear hide those controls for it. The same slide read as
fluorescence (the edit page's override) is three channels through z and projects like any stack.

**How a stack's Z axis is read.** A multi-channel stack whose file states no z step imports as
imaging cycles (the Akoya/CODEX reading, unchanged). If that is wrong, the project's edit page
has "Planes on Z" beside the image type; switching to depth re-reads it.

**3-D masks and whole cells.** Add a 3-D label TIFF (one id per cell across planes) as the
mask. Outlines follow the plane on screen. The Depth card then shows "Cells in 3-D": Measure
cells writes one row per whole cell (volume, surface, sphericity, centroid in pixels and µm,
each channel's mean and integrated intensity) and makes it the cell table; reload to use it
in Cell Explorer and gating. Without a z step it offers voxels instead of µm.

**Regions on a z-stack.** Draw a region, then its row menu: "Only on z 12" (or the projection's
range) limits it to those planes, "On every plane" undoes that. A limited region is a faint
dashed outline on other planes, its row says `z 12`, and mapping regions onto cells counts
only cells on its planes (needs a table with a z column, e.g. the measured one).

**Time series.** A T×Z OME-TIFF: `.` / `,` or the step buttons under the strip's readout move
between frames, the Depth card has a Frame field, the badge reads `t 3/3 · 60 s · z 2/4`. A
region limited on one frame is faint on the others.

*Cells through time.* Add a mask with one label volume per frame (a T×Z×Y×X label TIFF), or one
3-D mask for every frame. "Measure cells" now measures every frame: one row per cell per frame,
its frame in a `t` column, which becomes the table's frame. With a mask per frame, each frame
numbers its cells on its own: "Link across frames" matches each frame's cells to the next frame's
by distance and writes a `track` column (the same number for the same cell through the series;
the card says how many were followed). Divisions and a cell missed for a frame are not followed.
In the viewer, outlines, centroids, Cell Explorer's colours, gates and a click on a cell all use
the frame on screen's rows; step frames and they follow. A region drawn on one frame counts and
maps only that frame's cells.

**QC on a stack.** The QC card has a Stack section on a z-stack. Run checks the planes of every
channel (up to eight): runs of planes much less sharp than the sharpest, signal fading with
depth, empty planes, planes carrying almost no signal (beyond the tissue, usually), planes
repeated pixel for pixel, planes whose level jumps, straight edges at the same columns of every
plane (tile seams), a cycle whose nuclear stain sits deeper or shallower than the first cycle's,
and -- with a measured 3-D mask -- objects that are not one whole cell (one plane only, cut off
by the stack's ends, much taller than wide, split between neighbouring planes, far from the
usual volume). Click a line to go to its first plane. "Make regions" writes the findings as QC
regions limited to their planes, in the category their class belongs to (focus as blur, fading
as illumination, empty planes as a failed channel, seams as acquisition, a drifting cycle as
registration); a region on some planes only holds the cells on them, which needs the cell
table's z column -- without one those findings are skipped and the card says why. The plane line
says which plane every other QC check reads ("Use this plane" makes it the one on screen); each
check runs again on a new plane. On the Imaris stack: 47 findings over six channels.

**Segmenting in 3-D (Cellpose).** Tools → Segment in 3-D, on a z-stack. It needs the optional
install `pip install "plexora[segment3d]"` (Cellpose 4 and PyTorch); without it the card says so.
The first run downloads Cellpose-SAM's weights (about 1.2 GB, from the Cellpose authors' model
page, under their licence -- non-commercial at the time of writing; the card says this). Choose the
nuclear channel (and a membrane/cytoplasm channel for whole cells), "In 3-D" (needs the z step)
or "Plane by plane", and press Segment: the result becomes this project's mask (one id per cell
through the planes) and is measured into the cell table; reload when it says so. Measured here on
a synthetic 10 x 256 x 256 stack of 24 nuclei on the M2 Pro's GPU: in 3-D found 23 in 8.4 min,
plane by plane 33 (cells split between planes) in 41 s. Large stacks are segmented at a coarser
level ("Detail: Auto") and the mask drawn back up to full resolution. It runs where Plexora runs;
a node-backed image's pixels are read through the node.

**Regions of cells, and one plane's outline by hand.** With a 3-D mask, select cells in the
viewer (click one, Shift-click to add) and press "From cells" in the ROI panel: the region is
those cells, outlined on every plane they reach, and it holds exactly them when counted or
mapped (by id, not by outline -- a neighbour whose centre falls inside is not in it). Select a
region carried through planes (or made from cells) and step to one of its planes: its outline
there has handles; drag one to reshape that plane only. The footprint follows, and a region of
cells reshaped this way becomes an ordinary outline region. Under a projection there are no
handles (no single plane to edit).

**Hand-off to SCIMAP Pro in µm.** A hand-off from Plexora now says where the cells are and in
what units: an object table measured in µm hands over its `x_um`/`y_um`/`z_um` columns; any
other table hands over its pixel columns with the µm per pixel (and per plane, for 3-D objects)
and where each step came from. A step nobody knows is named as missing, never defaulted, and
nothing is written into your table. Binding a table from SCIMAP Pro can name its z and frame
columns too. (SCIMAP Pro has to read the new fields on its side.)

**Magic select on a stack.** It outlines the plane (or projection) on screen; move to another
plane and the next click is outlined from that plane's pixels.

**Regions through the planes.** Outline a cell with magic select (or select any region), then
the magic bar's layers button "Through planes", or the row menu's "Carry through planes": the
outline is followed up and down until the object ends, and the region keeps one outline per
plane. Its row reads `3-D z 81–105`, a note says why each direction stopped, every plane shows
its own outline, and the XZ / YZ panes draw the region's cut. One undo puts it back. With a
projection range on screen, "Ellipsoid through z a–b" makes a region taper to its poles; "Flat"
items make it a plain region again. Counting cells in it uses each cell's own plane (needs a z
column). Carrying uses MobileSAM with no extra install; with `pip install "plexora[volume3d]"`
(PyTorch) it uses SAM 2.1, about ten times slower on a laptop and, measured, no better
(`MAGIC_3D.md`).

**The 3-D copy.** A large stack stored plane by plane (a TIFF, most OME-Zarr) is copied in the
background, coarse levels only, into bricks 32 planes deep (`<project>/derived/volume_v1`, tens
to hundreds of MB): the Depth card's "3-D copy" line says so. Projections, sections and the 3-D
view at zoomed-out levels then read the copy. "Prepare this area" (and the section panes, once
the view settles) copies the area on screen at a finer level, kept within 4 GB, oldest areas
dropped first. Zoomed in on part of the image, the 3-D view draws that area, finer, rather than
the whole stack.

**Notebook.** `plexora.view("name", image=array, axes="czyx", z_step=2.0)` with a 4-D array.

**Agents (reconnect `/mcp` first).** `inspect_volume`, `sample_z_planes`, `axis_profile`,
`check_volume_quality`, `render_region` with `view`, `render_projection`, `render_sections`,
`render_object_views` (a measured cell by id, outlined in yellow), `measure_objects`,
`pin_projection`, `viewer_set_z` / `viewer_set_slab`, and `create_roi` with an `extent`.
Since 2026-10-11 also: `segment_3d`, `track_objects`, `create_roi` with `cells`,
`colocalize_3d` (Pearson and Manders over planes in a region or cells; ask it on a projection
view and it refuses, saying why), `find_regions_of_interest_3d` (brightest spots of a marker,
crowded cells, suspicious or clipped cells, blurred planes -- each with a centre and a reason),
`run_qc_3d` / `write_qc_3d_regions` / `set_qc_plane`, and an object review:
`object_review_start` queues the suspicious cells and returns a `delegate` block for worker
conversations (skill `review-objects`), each `object_review_next` shows one cell's XY/XZ/YZ
views, and `object_review_finish` writes the cells judged not one whole cell as one QC
segmentation region holding exactly them.

**Sharp up close in 3-D.** Zoom into the 3-D view and let it rest: within a second or two the
part near you sharpens to full resolution, farther parts as fine as they need to be (a stack
of a few hundred MB fills in about a second here). Nothing to switch on. On a slow connection
the 3-D view's data comes compressed, and a volume's tiles come several to a request.

**Gating on a z-stack.** Measure the cells (Depth card), then gate a marker as usual. The
validation pictures draw each field at the plane its cells are on (the header says "A, B at
plane z 7/10") with a fourth panel under them: the field in XY, XZ and YZ, cut where the
dashed lines cross. Cells cut off by the top or bottom of the stack are left out of the gate's
estimate (QC strict, the default) and counted as `clipped_z` in `get_qc_exclusions`, unless
most cells are cut off. Over MCP, `object_z_profile` gives a cell's markers plane by plane.

## Measured on this machine

The Xenium focus stack (DAPI, 14 planes, 45450×27241): registers in 0.3 s; plane tiles about
100 ms at full resolution; a 14-plane MIP tile about 0.9 s the first time; sections 0.3–0.8 s;
the first tile waits about 15 s for the brightness scan, as the flat focus image of the same
size does on `main`. Pinning a 4-plane MIP took about a minute and matched numpy pixel for pixel.

Two real stacks, opened through the browser (screenshots sent 2026-10-10):

- `nirmal lab/temp/3D/F8IIc-smallchunk.ims` (Imaris, 6 channels, 190 planes of 1538×763,
  0.14/0.28 µm, gzip chunks 64 planes deep): registers in 0.25 s; the first tiles of three
  channels, brightness scan included, 2.5 s; a 190-plane MIP of the view about 3 s; section
  tiles 0.06–0.8 s; the 3-D view about 1 s. Imaris's "(name not specified)" reads as Channel 1–6.
- `~/Downloads/ExpA_VIP_ASLM_on.zarr` (mesoSPIM mouse brain, OME-Zarr 0.5, 1937 planes of
  2048², 6.55/5 µm, shards 10 planes deep, 9.5 GB; download verified shard by shard): registers
  in 0.04 s; brightness scan 5 s; a plane change about 0.5 s; the full-depth MIP of the view
  about 7 s (2 s a tile); the 3-D view 1.3 s (level 3, ten planes max-pooled per voxel). HD
  turns on by itself: its tissue sits under 1000 beside voxels at 65535.

Regions and the copy, measured 2026-10-10: carrying a real nucleus of the Imaris stack took
45 s with SAM 2 on the CPU (25 planes, stopped where it faded both ways), 4 s with MobileSAM.
The light-sheet stack's coarse copy took 4 s (133 MB) and its whole 3-D level then 0.6 s (from
4.9 s); a 512-px area at full resolution prepared in 4 s. The Xenium stack's coarse copy took
20 s (41 MB); its 3-D level then 0.2 s, a coarse section tile 2 ms.

Moving, measured 2026-10-10 in Chrome on this Mac's GPU (Apple M2 Pro, 120 Hz) on the Imaris
stack: a step to the next plane draws in 45-160 ms, then fades over 160 ms; Imaris chunks run
64 planes deep, and the reader now keeps a plane's worth of decoded chunks per channel (96 MB
each, at most six; `PLEXORA_IMS_CHUNK_CACHE_MB`), so a plane tile inside a chunk already read
costs about 1 ms instead of about 120 ms, and crossing into the next chunk about 0.6 s once
(3 channels). A held key through 20 planes settles 0.1 s after the last press inside a chunk.
The 3-D view turns at 120 frames a second at full resolution; a preset turn takes 0.45 s.
(Headless Chrome without a GPU is 5-10x slower at all of this; it was not used for these.)

## Not in this branch yet

- A z-profile line in automatic gating's packets: it lives in the gating work that is not
  merged yet. (Gating's per-cell model, whose 3-D neighbourhood was listed here, was removed
  on 2026-10-12.)
- Full resolution in 3-D for an image on a data node: it refines the part you rest on (bricks
  are served for local images only). 3-D levels of a node-backed image go through this server
  (tiles may be fetched straight from the node).

## Questions for you

1. Should the import dialog ask "depth or imaging cycles?" for a multi-channel stack with no
   z step, or is the edit page's "Planes on Z" enough?
2. Is the ROI schema staying at version 1 (so `main` can still open regions drawn here) the
   right trade, given that `main` then forgets their planes?

## Thresholding plane by plane (feature/plane-gating-3d)

Design and measurements: `PLANE_GATING.md`. Gates always use each cell's whole-cell value; the
slab only changes what the image shows.

- **Plane by plane.** Open a z-stack with a 3-D mask whose cells are measured (Depth card, Cells
  in 3-D), turn Outlines on, open Thresholding and pick a marker. Step with `]` / `[`: only the
  cells with voxels on the plane are outlined, the shading under the histogram's line is that
  plane's cells, and the line under the plot reads "z 12 · 41 of 312 positive". Move the gate:
  the count follows the outlines exactly.
- **Slab.** View → Slab: the image becomes the mean of 5 µm around the plane (the badge names the
  planes), the outlines stay the plane's own cells, and "z 12–16" beside the fields names the
  planes combined. Step to the first or last plane: the slab is cut there ("z 0–2"), and the
  outlines are still plane 0's. Change the thickness (planes it comes to are shown as you type) and
  Mean / Max / Median. Without a known z step the field is in planes.
- **Kept.** Switch markers, step planes, reload: the slab and its thickness stay. View → Plane
  returns to the plane; the 3-D view hides the row.
- **Sections.** With the section panes on, each cell the XZ / YZ cut crosses is outlined plane by
  plane; with a gate on, only the cells it passes (as on the plane).
- **Measured before this branch?** Counts still appear, from each cell's planes in depth (the
  tooltip says so); measure the cells again for an exact count.
