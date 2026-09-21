"""Drawing a run's cell boundaries as the label mask the viewer serves.

A Xenium run states its segmentation as polygons in a parquet, not as pixels.
Until it is rasterized the sample has no Outlines, no Filled, no colour by
gating and no cell picking -- on a run whose whole point is that its cells are
segmented. What these pin is that the conversion produces an ordinary Plexora
mask: the run's own `label_id` on every pixel, the reference image's geometry,
and a file the existing staleness and serving machinery recognises as one of
ours.
"""

from pathlib import Path

import numpy as np
import polars as pl
import pytest

from plexora.server.models.project import ImageSpec, LayerSpec, Project
from plexora.server.utils import boundary_mask, segmentation_pyramid


def _square(label, x0, y0, size):
    return {
        "cell_id": [f"cell{label}-1"] * 4,
        "vertex_x": [float(x0), float(x0 + size), float(x0 + size), float(x0)],
        "vertex_y": [float(y0), float(y0), float(y0 + size), float(y0 + size)],
        "label_id": [label] * 4,
    }


def _boundaries(path, squares=((1, 2, 2, 8), (7, 20, 20, 10))):
    frames = [pl.DataFrame(_square(*square)) for square in squares]
    pl.concat(frames).write_parquet(path)
    return path


def _levels(path):
    import tifffile as tf
    import zarr

    with tf.TiffFile(str(path), is_ome=False) as reader:
        series = reader.series[0]
        group = zarr.open(series.aszarr())
        return [np.asarray(group[str(index)])
                for index in range(len(series.levels))]


# -- recognising one ----------------------------------------------------


def test_a_vertex_table_is_recognised_without_reading_a_row(tmp_path):
    table = _boundaries(tmp_path / "cell_boundaries.parquet")

    assert boundary_mask.is_boundary_table(table) is True


@pytest.mark.parametrize("columns", [
    # A cell table, which is also a parquet and also has a cell_id.
    {"cell_id": ["a"], "x_centroid": [1.0], "y_centroid": [2.0]},
    # Vertices with nothing to number them by: rasterizing this would invent
    # an order the cell table does not share.
    {"cell_id": ["a"], "vertex_x": [1.0], "vertex_y": [2.0]},
])
def test_other_parquets_are_not_boundary_tables(tmp_path, columns):
    path = tmp_path / "other.parquet"
    pl.DataFrame(columns).write_parquet(path)

    assert boundary_mask.is_boundary_table(path) is False


def test_a_file_that_is_not_there_is_not_a_boundary_table(tmp_path):
    assert boundary_mask.is_boundary_table(tmp_path / "gone.parquet") is False
    assert boundary_mask.is_boundary_table(None) is False


# -- reading it ---------------------------------------------------------


def test_polygons_come_back_in_reference_pixels(tmp_path):
    """Microns in, pixels out. The scale is the run's, and getting it wrong
    draws a plausible-looking mask at a fraction of its true size."""
    table = _boundaries(tmp_path / "cell_boundaries.parquet")

    polygons = boundary_mask.read_polygons(table, pixel_size=0.5)

    assert polygons.count == 2
    assert polygons.labels.tolist() == [1, 7]
    assert polygons.offsets.tolist() == [0, 4, 8]
    # The first square spans 2..10 microns, which is 4..20 pixels at 0.5 um/px.
    assert polygons.bounds[0].tolist() == [4.0, 4.0, 20.0, 20.0]


def test_the_layers_own_transform_wins_over_the_pixel_size(tmp_path):
    """The transform is the registration the importer composed, and it is what
    every OTHER layer of the sample is drawn through."""
    table = _boundaries(tmp_path / "cell_boundaries.parquet")

    polygons = boundary_mask.read_polygons(
        table, transform=(4.0, 0.0, 0.0, 4.0, 100.0, 0.0), pixel_size=0.5)

    assert polygons.bounds[0].tolist() == [108.0, 8.0, 140.0, 40.0]


def test_a_run_with_no_registration_at_all_is_refused(tmp_path):
    table = _boundaries(tmp_path / "cell_boundaries.parquet")

    with pytest.raises(ValueError, match="pixel size"):
        boundary_mask.read_polygons(table)


def test_a_label_of_zero_is_refused(tmp_path):
    """Zero is background in every mask Plexora reads, so a cell numbered
    zero would be drawn as a hole rather than as a cell."""
    table = _boundaries(tmp_path / "cell_boundaries.parquet",
                        squares=((0, 2, 2, 8),))

    with pytest.raises(ValueError, match="background"):
        boundary_mask.read_polygons(table, pixel_size=1.0)


# -- drawing it ---------------------------------------------------------


def test_each_cell_is_drawn_with_its_own_label(tmp_path):
    table = _boundaries(tmp_path / "cell_boundaries.parquet")
    destination = tmp_path / "mask.ome.tiff"

    boundary_mask.build(table, destination, width=128, height=96,
                        pixel_size=0.5, tile_size=32)

    plane = _levels(destination)[0]
    assert plane.shape == (96, 128)
    assert plane.dtype == np.uint32
    assert sorted(np.unique(plane).tolist()) == [0, 1, 7]
    first = np.argwhere(plane == 1)
    assert first.min(axis=0).tolist() == [4, 4]
    assert first.max(axis=0).tolist() == [20, 20]


def test_the_file_is_one_the_rest_of_plexora_recognises_as_a_mask(tmp_path):
    """Not an incidental property. `generated_mask_kind` is how a load decides
    whether a derived file is ours and still current, and
    `is_servable_label_pyramid` is what lets it be served untouched."""
    table = _boundaries(tmp_path / "cell_boundaries.parquet")
    destination = tmp_path / "mask.ome.tiff"

    boundary_mask.build(table, destination, width=128, height=96,
                        pixel_size=0.5, tile_size=32)

    assert segmentation_pyramid.generated_mask_kind(destination) == \
        segmentation_pyramid.MODE_FILLED
    assert segmentation_pyramid.is_servable_label_pyramid(destination) is True


def test_a_cell_that_has_shrunk_below_a_pixel_is_still_drawn(tmp_path):
    """The whole-slide view is the one where every cell is sub-pixel. Filling
    a polygon whose vertices all round to one pixel draws nothing at all, so
    a mask built by fill alone comes back empty at exactly the zoom where the
    user is looking at the whole section."""
    table = _boundaries(tmp_path / "cell_boundaries.parquet")
    destination = tmp_path / "mask.ome.tiff"

    boundary_mask.build(table, destination, width=128, height=96,
                        pixel_size=0.5, tile_size=16)

    coarsest = _levels(destination)[-1]
    assert max(coarsest.shape) <= 16
    assert sorted(np.unique(coarsest).tolist()) == [0, 1, 7]


def test_the_mask_gets_as_many_levels_as_the_image_has(tmp_path):
    """The mask layer is served at the IMAGE's level count, so a mask one
    level short raises on the tile the whole-slide view asks for first."""
    table = _boundaries(tmp_path / "cell_boundaries.parquet")
    destination = tmp_path / "mask.ome.tiff"

    boundary_mask.build(table, destination, width=128, height=96,
                        pixel_size=0.5, tile_size=64, levels=5)

    levels = _levels(destination)
    assert len(levels) == 5
    assert [plane.shape for plane in levels] == [
        (96, 128), (48, 64), (24, 32), (12, 16), (6, 8)]


def test_a_polygon_straddling_a_tile_edge_is_drawn_on_both_sides(tmp_path):
    """Each tile is filled independently from the same polygons, so the seam
    is the one place a rasterizer can disagree with itself."""
    table = _boundaries(tmp_path / "cell_boundaries.parquet",
                        squares=((3, 24, 8, 16),))
    destination = tmp_path / "mask.ome.tiff"

    boundary_mask.build(table, destination, width=64, height=64,
                        pixel_size=1.0, tile_size=32)

    plane = _levels(destination)[0]
    rows = plane[12, :]
    assert rows[31] == 3 and rows[32] == 3
    drawn = np.argwhere(plane == 3)
    assert drawn[:, 1].min() == 24 and drawn[:, 1].max() == 40


# -- where it goes ------------------------------------------------------


def _project(tmp_path, **image):
    spec = dict(src=str(tmp_path / "image.ome.tif"), width=200, height=100,
                max_level=3, tile_width=64)
    spec.update(image)
    return Project(name="sample", image=ImageSpec(**spec))


def test_the_geometry_comes_from_the_reference_image(tmp_path):
    table = _boundaries(tmp_path / "cell_boundaries.parquet")
    project = _project(tmp_path, pixel_size={"value": 0.5, "unit": "µm"})

    geometry = boundary_mask.geometry_for(project, table)

    assert geometry["width"] == 200 and geometry["height"] == 100
    assert geometry["levels"] == 3
    assert geometry["tile_size"] == 64
    assert geometry["pixel_size"] == 0.5


def test_a_sibling_layer_of_the_same_run_states_the_registration(tmp_path):
    """Everything a Xenium run ships is in one frame, so the transcripts'
    registration is the boundaries' registration -- and it outranks the pixel
    size, which is only the number that transform was built from."""
    table = _boundaries(tmp_path / "cell_boundaries.parquet")
    project = _project(tmp_path, pixel_size={"value": 0.5, "unit": "µm"})
    project = project.with_layer(LayerSpec(
        id="transcripts", kind="points", src=str(tmp_path / "transcripts.parquet"),
        transform=(8.0, 0.0, 0.0, 8.0, 0.0, 0.0),
        source={"bundle": "xenium:run", "format": "xenium", "root": str(tmp_path)}))

    geometry = boundary_mask.geometry_for(project, table)

    assert geometry["transform"] == (8.0, 0.0, 0.0, 8.0, 0.0, 0.0)
    assert "pixel_size" not in geometry


def test_nothing_saying_where_the_cells_go_is_an_error(tmp_path):
    """Rather than an identity transform. A mask drawn at one pixel per micron
    is not approximately right -- it is a fifth of the slide in the corner."""
    table = _boundaries(tmp_path / "cell_boundaries.parquet")
    project = _project(tmp_path)

    with pytest.raises(ValueError, match="pixel size"):
        boundary_mask.geometry_for(project, table)


def test_an_already_drawn_mask_is_adopted_rather_than_redrawn(tmp_path):
    """Importing a second project from a run a first one already converted is
    an ordinary thing to do, and it used to cost the full conversion again."""
    table = _boundaries(tmp_path / "cell_boundaries.parquet")
    geometry = {"width": 128, "height": 96, "pixel_size": 0.5, "tile_size": 32}

    first = boundary_mask.resolve_mask(table, tmp_path, geometry=geometry)
    stamp = Path(first).stat().st_mtime_ns
    second = boundary_mask.resolve_mask(table, tmp_path, geometry=geometry)

    assert second == first
    assert Path(second).stat().st_mtime_ns == stamp


# -- the funnel every mask goes through ---------------------------------

def test_the_mask_conversion_dispatches_on_the_source_being_a_table(tmp_path):
    """`convertOmeTiff(isLabelImg=True)` is the one funnel: import, the edit
    page and the requirements modal all reach the mask through it. A boundary
    table has to be recognised THERE, or each of the three would need its own
    branch -- and the reload path, which has none, would get neither."""
    from plexora.server.models import data_model

    table = _boundaries(tmp_path / "cell_boundaries.parquet")

    result = data_model.convertOmeTiff(
        table, dataDirectory=str(tmp_path), isLabelImg=True,
        label_geometry={"width": 128, "height": 96, "pixel_size": 0.5,
                        "tile_size": 32})

    assert sorted(np.unique(_levels(result["segmentation"])[0])) == [0, 1, 7]


def test_converting_a_table_without_a_frame_says_what_is_missing(tmp_path):
    from plexora.server.models import data_model

    table = _boundaries(tmp_path / "cell_boundaries.parquet")

    with pytest.raises(ValueError, match="cell boundaries, not"):
        data_model.convertOmeTiff(table, dataDirectory=str(tmp_path),
                                  isLabelImg=True)


def test_the_progress_panel_says_what_is_being_drawn(tmp_path):
    """A panel that says "Opening the segmentation mask" for two minutes on a
    file the user knows is a table gives them no way to tell a wrong guess
    from a hang."""
    from plexora.server.models.data_model import describe_segmentation_work

    table = _boundaries(tmp_path / "cell_boundaries.parquet")

    assert describe_segmentation_work(table, "filled") == \
        "Drawing cell boundaries into a label mask"


def test_a_job_resolves_the_frame_from_the_project_it_was_started_for(
        tmp_path, monkeypatch):
    """Not from the caller. The reload path -- `refresh_segmentation_mapping`
    finding a stale mask -- has no proposal to carry a geometry, and it is the
    path a corrected run takes."""
    from tests.helpers import use_data_root

    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    from plexora.server.models.data_model import _label_geometry_for

    table = _boundaries(tmp_path / "cell_boundaries.parquet")
    Project(name="sample", image=ImageSpec(
        src=str(tmp_path / "image.ome.tif"), width=200, height=100,
        max_level=3, pixel_size={"value": 0.5, "unit": "µm"})).save()

    assert _label_geometry_for("sample", table) == {
        "width": 200, "height": 100, "levels": 3,
        "tile_size": boundary_mask.TILE_SIZE, "pixel_size": 0.5}
    # An ordinary raster mask carries its own geometry and needs none of this.
    assert _label_geometry_for("sample", tmp_path / "mask.tif") is None
