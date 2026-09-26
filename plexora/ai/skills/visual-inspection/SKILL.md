# Visual inspection

Look at the tissue with deliberate choices -- which region, which channels, which
windows, which overlays -- and describe only what the rendered pixels and the manifest
support.

## When to use

- "Show me", "look at", "what does X look like", "is there tumour here".
- Before concluding anything that depends on appearance (staining, morphology,
  segmentation fit).
- To produce a figure-ready view the user can adopt (each render is a stored artifact).

## When not to use

- Counting or statistics: use the table tools; an image is evidence, not a measurement.
- Checking a gate: use the visual-gating skill, which picks fields for you.

## Required features

An image (not a blank frame). Overlays need a mask (`segmentation.available`); gate
highlights and cell ids need a cell table.

## Decision logic

1. Know the project (`inspect_project`): size, channels, pixel size, mask.
2. Choose the region:
   - a specific area: `center` with `size_um` (calibrated) or `size_px`;
   - a drawn region: `roi` with the id from `list_rois`;
   - overview: no region (the whole image), then zoom in.
3. Choose channels with `list_channels`: at most 3-4 at once; nuclear in grey or blue,
   the marker of interest in yellow. Leave windows `auto` unless the user specifies.
4. Overlays: `segmentation: outlines` to judge cells, `none` to judge stain; `marker`
   to highlight a gate's positive cells; `cells.label_ids` to name cells.
5. Call `render_region`. Read the manifest before the picture: `level`, `channels`
   (windows and `window_source`), `segmentation.status`, `cells`, `not_rendered`.
6. Zoom: a second render of a sub-region beats squinting at the first.

## Tools

`inspect_project`, `list_channels`, `render_region`, `list_rois`, `get_artifact`.

## Evidence

Every claim about appearance cites the artifact id (`artifact.id`) it came from and the
channels/windows shown. Prefer two renders (context + close-up) over one ambiguous one.

## Uncertainty

- Auto windows stretch each channel to its own 1st-99.9th percentile: "bright" is
  relative. Compare markers only with explicit, equal windows.
- `segmentation.status` other than `rendered` means outlines were not drawn; say why.
- `not_rendered` lists layers the render did not include (other images, transcripts).
- Colours are additive: yellow over grey is not "co-expression".

## Mutation policy

Rendering changes nothing in the project. Renders are stored under the data directory's
`.agent/artifacts` so they can be cited later.

## Provenance

Cite `artifact.id` (or its `plexora://artifact/...` uri), the region in full-resolution
pixels (`bounds_fullres`), and the channels with windows.

## Done when

The user's visual question is answered with at least one cited render, and what the
render could not show (uncalibrated, no mask, layers not rendered) is stated.

## Failure modes

- `invalid_input` naming channels: the channel name was wrong; use one from the list.
- `precondition_missing` for `size_um`: the image is uncalibrated; use `size_px`.
- `unsupported_modality`: the sample has no image (a blank reference frame); say so.
- `too_large`: choose a smaller region or a smaller output.
