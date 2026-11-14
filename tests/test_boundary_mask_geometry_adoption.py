"""A drawn boundary mask is only reused for the frame it was drawn in.

A cell-boundary table is microns; the mask the viewer serves is pixels of one
reference image, drawn through that sample's registration (`geometry_for`).
`resolve_mask` reuses a mask it already drew, found by the table's name beside
the source. What these pin is that the reuse respects the geometry it was asked
for: after a pixel size is corrected and the boundaries reattached, or when a
second project registers the same run to a different image, the cells must land
where THAT geometry puts them -- not where the first drawing put them.
"""

import numpy as np
import polars as pl

from plexora.server.utils import boundary_mask, segmentation_pyramid


# Cell 1 spans 10..30 microns on both axes; cell 7 is well away from it.
_SQUARES = ((1, 10.0, 10.0, 20.0), (7, 100.0, 10.0, 20.0))


def _boundaries(path):
    frames = []
    for label, x0, y0, size in _SQUARES:
        frames.append(pl.DataFrame({
            "cell_id": [f"cell{label}-1"] * 4,
            "vertex_x": [x0, x0 + size, x0 + size, x0],
            "vertex_y": [y0, y0, y0 + size, y0 + size],
            "label_id": [label] * 4,
        }))
    pl.concat(frames).write_parquet(path)
    return path


def _level_zero(path):
    import tifffile as tf
    import zarr

    with tf.TiffFile(str(path), is_ome=False) as reader:
        group = zarr.open(reader.series[0].aszarr())
        return np.asarray(group["0"])


def _cell_box(path, label):
    """Cell `label`'s extent in IMAGE pixels: (x0, y0, x1, y1), inclusive --
    the scanline fill covers both of a polygon's edges."""
    scale = segmentation_pyramid.generated_mask_scale(path) or 1
    plane = _level_zero(path)
    rows, cols = np.nonzero(plane == label)
    assert rows.size, f"cell {label} is not drawn at all"
    return (int(cols.min()) // scale, int(rows.min()) // scale,
            int(cols.max()) // scale, int(rows.max()) // scale)


def _geometry(pixel_size):
    return {"width": 512, "height": 512, "tile_size": 64,
            "pixel_size": pixel_size}


def test_a_corrected_pixel_size_redraws_the_mask_rather_than_reusing_it(tmp_path):
    """The remedy `geometry_for` itself suggests -- set the right pixel size
    and reattach the mask -- has to change where the cells are drawn."""
    run = tmp_path / "run"
    run.mkdir()
    table = _boundaries(run / "cell_boundaries.parquet")
    project = tmp_path / "derived" / "sample"
    project.mkdir(parents=True)

    first = boundary_mask.resolve_mask(table, project, geometry=_geometry(0.5))
    # 10..30 microns at 0.5 um/px.
    assert _cell_box(first, 1) == (20, 20, 60, 60)

    corrected = boundary_mask.resolve_mask(table, project,
                                           geometry=_geometry(0.25))

    # 10..30 microns at 0.25 um/px.
    assert _cell_box(corrected, 1) == (40, 40, 120, 120)


def test_a_second_project_in_another_frame_gets_its_own_drawing(tmp_path):
    """The same run imported into a second sample registered to a different
    image (a post-run H&E at twice the resolution) must not be served the
    first sample's mask just because the file beside the run has the name."""
    run = tmp_path / "run"
    run.mkdir()
    table = _boundaries(run / "cell_boundaries.parquet")
    project_a = tmp_path / "derived" / "sample_a"
    project_b = tmp_path / "derived" / "sample_b"
    project_a.mkdir(parents=True)
    project_b.mkdir(parents=True)

    drawn_a = boundary_mask.resolve_mask(table, project_a,
                                         geometry=_geometry(0.5))
    drawn_b = boundary_mask.resolve_mask(table, project_b,
                                         geometry=_geometry(0.25))

    assert _cell_box(drawn_a, 1) == (20, 20, 60, 60)
    assert _cell_box(drawn_b, 1) == (40, 40, 120, 120)
