# Marker QC

Decide whether a marker can be trusted before anything is built on it: that the table
column is the stain in the image, that the distribution has something to separate, and
that the signal in the tissue looks like real staining.

## When to use

- Before gating or interpreting a marker for the first time.
- When a result "looks wrong", or a gate lands somewhere surprising.

## When not to use

- The user has already validated the marker and asked for something else.
- The marker is a metadata column (area, eccentricity): it has no stain to QC.

## Required features

A cell table with markers. Visual checks need an image channel of the same name.

## Decision logic

1. `list_markers`: is the marker a recorded marker? Is it in `markers_without_channel`
   (no image to check against)? Note `log_transformed`.
2. `list_channels` with `with_stats: true`: does the channel exist, what is its range?
3. `get_marker_distribution`: count (missing values?), quartiles, histogram shape.
   - One narrow peak: little or nothing to separate.
   - A long bright tail or a second peak: something to gate.
   - Many exact zeros or a spike at the max: saturation or background subtraction.
4. `get_gate_distribution`: does the fitted positive population exist and is it
   separated from the background curve?
5. `render_region` of a dense field and a sparse field with the marker alone
   (`segmentation: none`), then with outlines: membrane/nuclear/cytoplasmic pattern as
   expected? Signal inside cells, or smeared between them (spill-over, autofluorescence)?

## Tools

`list_markers`, `list_channels`, `get_marker_distribution`, `get_gate_distribution`,
`render_region`.

## Evidence

The distribution summary (quartiles, histogram shape) and at least one render of the raw
channel, cited by artifact id.

## Uncertainty

- A bimodal histogram is necessary, not sufficient: autofluorescent structures are bimodal too.
- Whether a staining pattern is "right" depends on the marker's biology; say what pattern
  you expected and whether you saw it, separately.

## Mutation policy

QC changes nothing.

## Provenance

Report the marker, the number of cells, `log_transformed`, and cite the renders.

## Done when

A verdict per marker -- usable, usable with caveats (name them), or not usable -- with the
evidence for it.

## Failure modes

- The marker is not an image channel: QC is table-only; say so.
- `precondition_missing`: the project has no cell table or unclassified columns.
