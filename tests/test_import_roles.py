"""What a file IS, and changing it: image or segmentation mask.

The report this answers: an image picked through the import dialog was read as
a segmentation mask, with no way to say otherwise. Three things changed, and
each is pinned here.

- **The pixels decide, the name only breaks a tie.** A 16-bit plane used to be
  a mask if its name held "nuclei" or "seg", or if its top-left 512x512 corner
  had a zero and fewer than 2048 distinct values -- a dark corner of almost
  any slide. Now a plane is ids when neighbouring pixels inside cells are
  equal in both directions AND each value fills a compact blob.
- **Every single-plane row can be switched.** `LayerProposal.switch` is the
  role menu's content, answered under the row's own `mask-or-image` id, so a
  confident detection is as correctable as an uncertain one.
- **After import, too.** `/import/reclassify` reads a sample's mask as an image
  layer, or an image layer as its mask, in place.
"""

import numpy as np
import pytest
import tifffile
import zarr

import plexora
from plexora.server.models import import_proposal, layer_jobs
from plexora.server.models.import_proposal import inspect_paths
from plexora.server.models.project import MASK_LAYER_ID, Project

from tests.helpers import use_data_root
from tests.ngff_fixtures import write_ngff


def _cells(size=512, cell=14, gap=3, seed=0):
    """A label plane: square cells of one id each over a background of zero,
    as a segmenter writes them."""
    rng = np.random.default_rng(seed)
    plane = np.zeros((size, size), dtype=np.uint32)
    label = 1
    for top in range(4, size - cell, cell + gap):
        for left in range(4, size - cell, cell + gap):
            if rng.random() < 0.8:
                plane[top:top + cell, left:left + cell] = label
                label += 1
    return plane


def _dim_channel(size=1024, seed=0):
    """One dim fluorescence channel with a dark corner: few distinct values,
    and zeros -- exactly what the old corner test read as a mask."""
    rng = np.random.default_rng(seed)
    plane = rng.poisson(20, (size, size)).astype(np.uint16)
    corner = np.add.outer(np.arange(size), np.arange(size)) < size // 3
    plane[corner] = 0
    return plane


# -- the pixels decide -------------------------------------------------------

def test_a_dim_channel_with_a_dark_corner_is_an_image_and_asks_nothing(tmp_path):
    """The reported bug. Under 2048 values and a zero in the corner was
    "a mask, no question" -- and that is most single channels."""
    path = tmp_path / "LSP11641_CD8.tif"
    tifffile.imwrite(path, _dim_channel(), tile=(256, 256))

    assert import_proposal.looks_like_label_image(path) is False
    sample = inspect_paths([str(path)]).samples[0]
    assert [layer.role for layer in sample.layers] == ["image"]
    assert not sample.questions


def test_a_name_never_decides_what_the_pixels_already_say(tmp_path):
    """`DAPI_nuclei.tif` is a stain, whatever its name says; a plane of ids
    named nothing in particular is still a mask."""
    stain = tmp_path / "DAPI_nuclei.tif"
    tifffile.imwrite(stain, np.random.default_rng(1).poisson(
        200, (512, 512)).astype(np.uint16))
    ids = tmp_path / "plane.tif"
    tifffile.imwrite(ids, _cells())

    assert import_proposal.looks_like_label_image(stain) is False
    assert import_proposal.looks_like_label_image(ids) is True


def test_a_gradient_is_not_ids_even_where_it_is_constant_one_way():
    """Equal down its columns, never across its rows: the smaller of the two
    directions is what counts."""
    gradient = np.tile(np.arange(512, dtype=np.uint16) % 300, (512, 1))
    assert import_proposal._window_verdict(gradient) is False


def test_the_windows_look_where_the_tissue_is_not_in_the_corner(tmp_path):
    """A slide's top-left corner is background. A mask whose cells are all in
    the middle is still found to be one."""
    plane = np.zeros((2048, 2048), dtype=np.uint32)
    plane[768:1280, 768:1280] = _cells(512)
    path = tmp_path / "segmentation_output.tif"
    tifffile.imwrite(path, plane, tile=(256, 256))
    assert import_proposal.looks_like_label_image(path) is True


# -- every single-plane row can be switched ------------------------------------

def test_a_single_plane_image_row_offers_the_mask_reading(tmp_path):
    path = tmp_path / "channel.tif"
    tifffile.imwrite(path, _dim_channel(512))
    layer = inspect_paths([str(path)]).samples[0].layers[0]

    assert layer.role == "image"
    assert layer.switch["value"] == "image"
    assert {o["value"] for o in layer.switch["options"]} == {"image", "mask"}
    assert layer.to_dict()["switch"] == layer.switch

    # Answering the switch's question is the switch: re-read as a mask.
    again = inspect_paths([str(path)], answers={layer.switch["question"]: "mask"})
    mask = again.samples[0].layers[0]
    assert mask.role == "mask"
    assert mask.switch["value"] == "mask"


def test_a_confident_mask_can_still_be_read_as_an_image(tmp_path):
    path = tmp_path / "cells.tif"
    tifffile.imwrite(path, _cells())
    layer = inspect_paths([str(path)]).samples[0].layers[0]
    assert layer.role == "mask"

    image = inspect_paths([str(path)], answers={layer.switch["question"]: "image"})
    assert image.samples[0].layers[0].role == "image"


def test_a_stack_of_channels_has_no_mask_reading(tmp_path):
    """A mask is one plane of ids. Offering it for a 2-channel stack would
    offer a cell-id lookup over a stain."""
    path = tmp_path / "panel.ome.tif"
    tifffile.imwrite(path, np.random.default_rng(0).integers(
        0, 3000, (2, 256, 256)).astype(np.uint16))
    layer = inspect_paths([str(path)]).samples[0].layers[0]
    assert layer.switch is None


def test_a_mask_found_in_a_folder_can_be_turned_down_from_its_badge(tmp_path):
    """Found, not picked -- so the folder's own ✕ would take the slide too."""
    tifffile.imwrite(tmp_path / "LSP1.ome.tif", np.random.default_rng(0).integers(
        0, 3000, (2, 512, 512)).astype(np.uint16))
    (tmp_path / "segmentation").mkdir()
    tifffile.imwrite(tmp_path / "segmentation" / "cell.tif", _cells())

    sample = inspect_paths([str(tmp_path)]).samples[0]
    mask = next(layer for layer in sample.layers if layer.role == "mask")
    assert "none" in {o["value"] for o in mask.switch["options"]}

    refused = inspect_paths([str(tmp_path)],
                            answers={mask.switch["question"]: "none"})
    assert not [l for l in refused.samples[0].layers if l.role == "mask"]


# -- OME-Zarr says what it is ------------------------------------------------

def _label_store(path, plane, *, declared=True):
    group = zarr.open_group(str(path), mode="w", zarr_format=2)
    array = group.create_array("0", shape=plane.shape, dtype="uint32",
                               chunks=(128, 128))
    array[:] = plane
    group.attrs["multiscales"] = [{
        "version": "0.4",
        "datasets": [{"path": "0", "coordinateTransformations": [
            {"type": "scale", "scale": [1.0, 1.0]}]}],
        "axes": [{"name": "y", "type": "space"}, {"name": "x", "type": "space"}]}]
    if declared:
        group.attrs["image-label"] = {"version": "0.4"}
    return path


def test_an_ome_zarr_label_image_is_a_mask(tmp_path):
    """NGFF writes `image-label` into a label image's attributes. It used to
    be ignored, so a picked label store was an image unless the card's own
    "+ Add segmentation mask" said otherwise."""
    store = _label_store(tmp_path / "cells.zarr", _cells())
    layer = inspect_paths([str(store)]).samples[0].layers[0]
    assert layer.role == "mask"
    assert layer.switch["value"] == "mask"


def test_an_ome_zarr_with_no_label_attribute_is_read_by_its_pixels(tmp_path):
    ids = _label_store(tmp_path / "ids.zarr", _cells(), declared=False)
    assert import_proposal.looks_like_label_image(ids) is True
    image = write_ngff(tmp_path / "image.ome.zarr", shape=(1, 256, 256), levels=1)
    assert import_proposal.looks_like_label_image(image) is False


def test_an_ome_zarr_image_brings_its_own_labels(tmp_path):
    """`image.zarr/labels/cells` is the segmentation of `image.zarr`, filed
    inside it by the writer. Picking the image offers it as the mask, as a
    web store's labels always were."""
    store = write_ngff(tmp_path / "image.ome.zarr", shape=(2, 512, 512), levels=1)
    labels = zarr.open_group(str(store / "labels"), mode="w", zarr_format=2)
    labels.attrs["labels"] = ["cells"]
    _label_store(store / "labels" / "cells", _cells())

    sample = inspect_paths([str(store)]).samples[0]
    roles = {layer.role: layer for layer in sample.layers}
    assert roles["image"].src == str(store)
    assert roles["mask"].src == str(store / "labels" / "cells")

    none = inspect_paths([str(store)], answers={"labels@" + str(store): "none"})
    assert {layer.role for layer in none.samples[0].layers} == {"image"}


# -- after import -------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    tifffile.imwrite(source / "slide.ome.tif", np.random.default_rng(0).integers(
        0, 4000, (2, 512, 512)).astype(np.uint16))
    tifffile.imwrite(source / "slide_cells.tif", _cells())
    tifffile.imwrite(source / "slide_DAPI.tif", _dim_channel(512))
    layer_jobs.forget()
    yield plexora.app.test_client(), source
    layer_jobs.forget()


def test_a_mask_is_read_as_an_image_layer_in_place(client):
    client, source = client
    client.post("/import/sample", json={
        "paths": [str(source / "slide.ome.tif"), str(source / "slide_cells.tif")],
        "name": "demo"})
    assert Project.load("demo").segmentation.source

    answer = client.post("/import/reclassify", json={
        "sample": "demo", "layer": MASK_LAYER_ID, "as": "image"})
    assert answer.status_code == 200, answer.get_json()
    assert answer.get_json()["reload"] is True

    project = Project.load("demo")
    assert not project.segmentation.source
    assert [layer.src for layer in project.spatial_layers
            if layer.kind == "image"] == [str(source / "slide_cells.tif")]


def test_an_image_layer_is_read_as_the_mask_in_place(client):
    client, source = client
    client.post("/import/sample", json={
        "paths": [str(source / "slide.ome.tif")], "name": "demo"})
    client.post("/import/layers", json={
        "sample": "demo", "paths": [str(source / "slide_DAPI.tif")]})
    [layer] = [l for l in Project.load("demo").spatial_layers if l.kind == "image"]

    answer = client.post("/import/reclassify", json={
        "sample": "demo", "layer": layer.id, "as": "mask"})
    assert answer.status_code == 200, answer.get_json()

    project = Project.load("demo")
    assert project.segmentation.source == str(source / "slide_DAPI.tif")
    assert not [l for l in project.spatial_layers if l.id == layer.id]


def test_a_stack_is_refused_as_a_mask_and_left_where_it_was(client):
    client, source = client
    tifffile.imwrite(source / "second.ome.tif", np.random.default_rng(2).integers(
        0, 4000, (3, 512, 512)).astype(np.uint16))
    client.post("/import/sample", json={
        "paths": [str(source / "slide.ome.tif")], "name": "demo"})
    client.post("/import/layers", json={
        "sample": "demo", "paths": [str(source / "second.ome.tif")]})
    [layer] = [l for l in Project.load("demo").spatial_layers if l.kind == "image"]

    answer = client.post("/import/reclassify", json={
        "sample": "demo", "layer": layer.id, "as": "mask"})
    assert answer.status_code == 400
    assert "3 channels" in answer.get_json()["error"]
    assert [l.id for l in Project.load("demo").spatial_layers] == [layer.id]
    assert not Project.load("demo").segmentation.source


def test_cell_outlines_are_not_offered_as_an_image(client):
    """Polygons have no pixels: as an image layer they would draw nothing,
    with the mask gone and nothing to turn back into one."""
    from dataclasses import replace

    client, source = client
    outlines = source / "cell_segmentations.geojson"
    outlines.write_text('{"type": "FeatureCollection", "features": [{"type": '
                        '"Feature", "properties": {"cell_id": 1}, "geometry": '
                        '{"type": "Polygon", "coordinates": [[[0, 0], [9, 0], '
                        '[9, 9], [0, 0]]]}}]}', encoding="utf-8")
    client.post("/import/sample", json={
        "paths": [str(source / "slide.ome.tif")], "name": "demo"})
    Project.mutate("demo", lambda project: project.patch(segmentation=replace(
        project.segmentation, source=str(outlines))))

    answer = client.post("/import/reclassify", json={
        "sample": "demo", "layer": MASK_LAYER_ID, "as": "image"})
    assert answer.status_code == 400
    assert "polygons" in answer.get_json()["error"]
    assert Project.load("demo").segmentation.source == str(outlines)
    assert not Project.load("demo").spatial_layers


def test_a_layer_that_will_not_become_the_mask_keeps_its_place(client, monkeypatch):
    from plexora.server.models import import_sample

    client, source = client
    tifffile.imwrite(source / "slide_CD8.tif", _dim_channel(512))
    client.post("/import/sample", json={
        "paths": [str(source / "slide.ome.tif")], "name": "demo"})
    for name in ("slide_DAPI.tif", "slide_CD8.tif"):
        client.post("/import/layers", json={"sample": "demo",
                                             "paths": [str(source / name)]})
    before = [layer.id for layer in Project.load("demo").spatial_layers]
    assert len(before) == 2

    def refused(*args, **kwargs):
        raise import_sample.ImportError_("not a mask after all")

    monkeypatch.setattr(import_sample, "add_layers", refused)
    answer = client.post("/import/reclassify", json={
        "sample": "demo", "layer": before[0], "as": "mask"})
    assert answer.status_code == 400
    assert [layer.id for layer in Project.load("demo").spatial_layers] == before


def test_the_scoped_answer_says_when_a_table_was_added(client, tmp_path):
    client, source = client
    (source / "cells.csv").write_text(
        "CellID,X_centroid,Y_centroid,CD3\n1,10,10,5\n2,20,20,6\n", encoding="utf-8")
    client.post("/import/sample", json={
        "paths": [str(source / "slide.ome.tif")], "name": "demo"})
    added = client.post("/import/layers", json={
        "sample": "demo", "paths": [str(source / "cells.csv")]}).get_json()
    assert added["table"] is True and added["reload"] is False
