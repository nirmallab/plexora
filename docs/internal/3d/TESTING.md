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
ends), or Shift + mouse wheel over the image. The old plane stays until the new one has drawn.

**Projections.** The strip's second button, or the Depth card (View menu → Depth). Maximum,
mean, minimum, standard deviation, median, percentile, over a range you set. The badge turns
amber and names the range (`MIP z 0–13 · 14 planes · 42 µm`). "Add as a layer" computes it
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

**3-D view.** The cube button: a maximum-intensity or additive rendering; the projection
range on the rail is the clip range.

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

## Not in this branch yet

- 3-D segmentation inside Plexora (Cellpose on a node): bring a label volume made elsewhere.
- Region shapes edited plane by plane by hand (a carried outline is redrawn by carrying again),
  and labels/masks as regions (`LabelRef`).
- The area 3-D view's contrast comes from every plane, not the clip range: a narrow clip range
  draws dim until the slider is moved.
- The QC panel's stack checks (focus by plane, fading with depth, empty or repeated planes)
  are agent-only (`check_volume_quality`); QC is shared with the gating work in progress.
- Gating evidence in depth waits for `feature/spatial-gating` to merge.
- Hand-off to SCIMAP Pro with µm coordinates; a bricked store for very large stacks; colour
  (brightfield) DICOM z-stacks; object tables per frame of a time series.

## Questions for you

1. Should the import dialog ask "depth or imaging cycles?" for a multi-channel stack with no
   z step, or is the edit page's "Planes on Z" enough?
2. Is the ROI schema staying at version 1 (so `main` can still open regions drawn here) the
   right trade, given that `main` then forgets their planes?
