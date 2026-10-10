# Volume triage

Settle what a z-stack is before looking at it in depth: whether it is a volume, whether its
z step is known, which planes are worth looking at, and which picture -- a plane, a
projection -- answers the question. Every picture you show or cite says which it is.

## When to use

- The project is a z-stack or time series (`inspect_project` or the user says confocal,
  light-sheet, volume, planes, focus, depth).
- Before any render, measurement or claim about depth, focus, or "through the tissue".
- The user asks which plane is in focus, whether signal fades with depth, or to "show the
  whole stack".

## When not to use

- A planar image (`inspect_volume` says `volumetric: false`): use visual-inspection.
- Counting or statistics on cells: an image is evidence, not a measurement.

## Required features

An image file. Physical depth (µm along z) needs a known z step: `inspect_volume` gives
`spacing.z.source`; `metadata` (the file states it) or `manual` (someone typed it).

## Decision logic

1. `inspect_volume` first. Read `depth`, `frames`, `spacing`, `warnings`.
2. If the z step is missing and the question is physical (a distance, a depth, a volume,
   a section drawn to scale): ask the user for it and record it with `set_z_spacing`. Never
   assume a z step; never compute a µm depth without one.
3. Choose planes with `sample_z_planes`: `focus` for a sharp look, `intensity` for where
   the signal is, `uniform` to survey, `edges` to check the ends of the stack. Read its
   `profile`: a run of low-focus planes is a stack problem, not tissue.
4. Choose the picture:
   - a single plane (`view: "z12"`) for anything about cells, morphology or overlap;
   - a projection (`render_projection`, `view: "z4-19max"`) only for an overview of where
     signal is across planes -- never to conclude colocalization, contact, or that one
     cell is "inside" another;
   - when a projection's numbers will be read, measured or exported, `pin_projection`
     computes it exactly at full resolution and adds it as a layer. A zoomed-out
     projection on screen combines planes that were already shrunk; the pinned one does not.
5. Render with `render_region` (`view`) or `render_projection`. Read the manifest's `view`,
   `view_source` and `z_label` before the picture; a `default` view is still one plane of
   many.
6. For depth questions use `axis_profile`: `best_z` and `attenuation` are numbers, not
   impressions. `check_volume_quality` says which planes are out of focus, faded, empty or
   repeated -- read it before trusting planes near the ends of the stack.
7. To judge one object in depth (one cell or two stacked, cut off by the stack's ends),
   `render_sections` through its centre: XY, XZ and YZ at one scale and one window. For a
   measured cell, `render_object_views` does this by its id, with its outline in yellow
   among the others and its row (volume, sphericity, planes, clipped) beside the picture.
8. To measure cells in depth the project needs a label volume -- one id per cell across planes (`inspect_volume`'s
   `segmentation`); `measure_objects` writes one row per whole cell. A flat mask cannot
   give volumes or surfaces -- say so rather than measuring plane by plane.

## Tools

`inspect_volume`, `set_z_spacing`, `sample_z_planes`, `axis_profile`, `check_volume_quality`,
`render_region`, `render_projection`, `render_sections`, `render_object_views`,
`measure_objects`, `pin_projection`, `viewer_set_view`, `viewer_set_z`, `viewer_set_slab`.

## Evidence

Cite the artifact id and its `z_label` (`"z 12 / 40"`, `"MIP z 8–20"`). Report which planes a
conclusion rests on and whether the z step was stated, typed or missing.

## Uncertainty

- A projection merges planes: overlap in it is not colocalization, and a bright spot may
  be two cells at different depths. Its manifest carries this warning; repeat it.
- Planes far from `best_z` are blurred; dimmer deep planes may be attenuation, not biology.
- A flat mask drawn on a volume is one plane's segmentation shown on every plane.
- A profile reads planes at a coarse level (longest side about {{volume.profile_side}}
  pixels); it ranks planes, it does not measure cells.

## Mutation policy

`set_z_spacing` writes the project's z step; `measure_objects` writes an object table and,
when the project has no table, makes it the cell table; `pin_projection` adds a layer and
its file (removing the layer deletes the file). All are receipted. Viewer commands
change the open tab only.

## Provenance

Every render's manifest carries `view`, `view_source`, `z_label` and `representation` (the
planes, the operation, the spacing and whether the read was exact). Cite them.

## Done when

The user knows what the stack is, which planes or projection answer their question and
why, with cited renders that say which picture they are, and any missing calibration has
been raised.

## Failure modes

- `invalid_input` "single plane": not a volume; use visual-inspection.
- `too_large`: more than {{volume.max_planes}} planes in one profile; ask for a region or
  a frame.
- `conflict` from `set_z_spacing`: the file states the z step; it is not overridden.
- `representation_unsupported`: a tool that measures one plane was given a projection;
  give it a plane and say which.
- `inference_forbidden`: the conclusion cannot be drawn from that picture; say why.
