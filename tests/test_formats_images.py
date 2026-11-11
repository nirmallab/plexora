"""A microscope file holding several images, each its own sample.

A CZI's scenes, an ND2's positions, a LIF's images, an MCD's acquisitions, a
VSI's scan areas: `formats.open_reader(path, index)` opens image `index`, the
import offers every image as a sample of its own, registration records the
position as `imageIndex`, and everything that reads the project afterwards --
pixels, channel names, pixel size, the "already imported" check -- reads that
image and no other. A file holding one image records nothing new.

The package readers run over the fakes in tests/formats_fakes.py; the app
level runs on Olympus VSI files written by tests/formats_fixtures.py, whose
reader is Plexora's own, so a real node can read them too. Expected values
come from the fixtures' own arrays and the numbers written here.
"""

import io

import numpy as np
import pytest
from PIL import Image

import plexora
from plexora.server.utils import formats
from plexora.server.utils.formats import FormatUnreadable
from tests.formats_fakes import (FakeCziImage, FakeLifImage, fake_acquisition, install_czi,
                                 install_lif, install_nd2, install_readimc, lif_key, nd2_key,
                                 plane, touch)
from tests.formats_fixtures import VsiStack, vsi_expected, write_dv, write_vsi
from tests.node_harness import node_process  # noqa: F401 - fixture

# -- the package: one image by its position ---------------------------------------


def _czi_scenes():
    first = FakeCziImage(("C", "Y", "X", "S"), dict(C=2, Y=20, X=24, S=1), [(20, 24)],
                         bbox=(0, 0, 24, 20), channels={"DAPI": 0, "CD3": 1}, mpp=(0.5, 0.5))
    second = FakeCziImage(("C", "Y", "X", "S"), dict(C=1, Y=10, X=12, S=1), [(10, 12)],
                          bbox=(100, 50, 12, 10), channels={"Ki67": 0}, mpp=(0.25, 0.25),
                          data=[plane(77, 10, 12)[None, :, :, None]])
    return first, second


def test_each_czi_scene_is_an_image_of_its_own(monkeypatch, tmp_path):
    install_czi(monkeypatch, list(_czi_scenes()))
    path = touch(tmp_path, "scan.czi")

    second = formats.describe(path, 1)
    assert second.images == ["Scene 1", "Scene 2"]
    assert second.image_index == 1
    assert (second.height, second.width, second.channels) == (10, 12, 1)
    assert second.channel_names == ["Ki67"]
    assert formats.channel_names(path, index=1) == ["Ki67"]
    assert formats.pixel_size(path, index=1) == (0.25, 0.25)
    assert formats.physical_metadata(path, index=1)["physical_size_x"] == 0.25
    assert formats.detect_image_type(path, index=1).verdict == "fluorescence"
    # Cached by image as well as by file: the first is still the first.
    first = formats.describe(path)
    assert (first.height, first.width, first.channel_names) == (20, 24, ["DAPI", "CD3"])

    reader = formats.open_reader(path, 1)
    try:
        assert reader.image_index == 1
        np.testing.assert_array_equal(reader.read(0, 0, 0, 10, 0, 12), plane(77, 10, 12))
    finally:
        reader.close()
    pyramid = formats.open_image(path, index=1)
    try:
        assert pyramid.image_index == 1 and pyramid.level_shapes[0] == [10, 12]
    finally:
        pyramid.close()


@pytest.mark.parametrize("index", [2, -1, "two"])
def test_an_image_the_file_does_not_hold_is_refused(monkeypatch, tmp_path, index):
    install_czi(monkeypatch, list(_czi_scenes()))
    path = touch(tmp_path, "scan.czi")
    with pytest.raises(FormatUnreadable):
        formats.open_reader(path, index)
    with pytest.raises(FormatUnreadable):
        formats.describe(path, index)


def test_each_nd2_position_reads_its_own_frames(monkeypatch, tmp_path):
    module = install_nd2(monkeypatch, sizes={"P": 3, "C": 2, "Y": 6, "X": 8},
                         names=["DAPI", "GFP"])
    path = touch(tmp_path, "points.nd2")
    assert formats.image_names(path) == ["Position 1", "Position 2", "Position 3"]
    reader = formats.open_reader(path, 2)
    try:
        np.testing.assert_array_equal(reader.read(0, 1, 0, 6, 0, 8), plane(nd2_key(2, 1), 6, 8))
        assert module.made[-1].frames_read == [2]
    finally:
        reader.close()


def test_lif_images_count_only_planes_and_keep_their_own_channels(monkeypatch, tmp_path):
    install_lif(monkeypatch, [
        FakeLifImage("Overview", "CYX", {"C": 2, "Y": 8, "X": 10}, coords={"C": ["DAPI", "GFP"]}),
        FakeLifImage("Line profile", "CX", {"C": 1, "X": 10}),
        FakeLifImage("Zoom", "CYX", {"C": 3, "Y": 4, "X": 6}, coords={"C": ["a", "b", "c"]}),
    ])
    path = touch(tmp_path, "project.lif")
    assert formats.image_names(path) == ["Overview", "Zoom"]
    zoom = formats.describe(path, 1)
    assert (zoom.height, zoom.width, zoom.channel_names) == (4, 6, ["a", "b", "c"])
    reader = formats.open_reader(path, 1)
    try:
        np.testing.assert_array_equal(reader.read(0, 2, 0, 4, 0, 6),
                                      plane(lif_key(0, 2, 0, 0), 4, 6))
    finally:
        reader.close()


def test_mcd_acquisitions_count_across_slides(monkeypatch, tmp_path):
    third = fake_acquisition(7, "Tonsil B", height=5, width=7, metals=("Er170",),
                             data=np.full((1, 5, 7), 3.5, np.float32))
    install_readimc(monkeypatch, [[fake_acquisition(1, "ROI A"), fake_acquisition(2, "ROI B")],
                                  [third]])
    path = touch(tmp_path, "run.mcd")
    described = formats.describe(path, 2)
    assert described.images == ["ROI A", "ROI B", "Tonsil B"]
    assert (described.height, described.width, described.channels) == (5, 7, 1)
    reader = formats.open_reader(path, 2)
    try:
        assert np.all(reader.read(0, 0, 0, 5, 0, 7) == np.float32(3.5))
    finally:
        reader.close()


def test_a_file_of_one_image_has_no_second(tmp_path):
    path = write_dv(tmp_path / "one.dv", depth=1, times=1)
    assert formats.image_names(path) == ["one.dv"]
    formats.open_reader(path, 0).close()
    with pytest.raises(FormatUnreadable, match="no image 2"):
        formats.open_reader(path, 1)


def test_a_reader_that_takes_no_position_opens_only_the_first(monkeypatch, tmp_path):
    """A format added without `index` (a file of one image) is refused past
    the first rather than called with an argument it does not take."""
    from plexora.server.utils.formats import dv

    path = write_dv(tmp_path / "one.dv", depth=1, times=1)
    monkeypatch.setattr(dv, "reader", lambda path: dv.DvReader(path))
    formats.open_reader(path).close()
    with pytest.raises(FormatUnreadable, match="holds one image"):
        formats.open_reader(path, 1)


# -- the record ---------------------------------------------------------------


def test_image_index_round_trips_and_is_absent_for_a_file_of_one_image():
    from plexora.server.models.project import ImageSpec, Project

    several = ImageSpec.from_entry({"channelFile": "/data/scan.czi", "imageIndex": 2})
    assert (several.image_index, several.index) == (2, 2)
    assert several.to_entry()["imageIndex"] == 2
    first = ImageSpec.from_entry({"channelFile": "/data/scan.czi", "imageIndex": 0})
    assert first.to_entry()["imageIndex"] == 0 and first.index == 0
    one = ImageSpec.from_entry({"channelFile": "/data/slide.ome.tif"})
    assert one.image_index is None and one.index == 0
    assert "imageIndex" not in one.to_entry()
    for junk in (-1, "two", True):
        assert ImageSpec.from_entry({"channelFile": "x.czi", "imageIndex": junk}).index == 0

    project = Project.from_entry("p", {"channelFile": "/data/scan.czi", "imageIndex": 1})
    assert project.image.index == 1 and "imageIndex" not in project.extra
    assert project.to_entry()["imageIndex"] == 1


# -- the app: a VSI with two scan areas -----------------------------------------

SCAN_A = VsiStack(2, name="Scan A", height=300, width=400, tile=128, dims=[(4, 2)],
                  channel_names=("A1", "A2"), pixel_size=(0.5, 0.5))
SCAN_B = VsiStack(3, name="Scan B", height=200, width=250, tile=128, dims=[(4, 3)],
                  channel_names=("B1", "B2", "B3"), pixel_size=(0.25, 0.25))


def _two_scans(folder):
    overview = VsiStack(1, name="Overview", height=100, width=120, tile=64, dims=[(4, 1)],
                        channel_names=("junk",), levels=1, pyramid=False)
    return write_vsi(folder, stem="slide", stacks=[overview, SCAN_A, SCAN_B])


@pytest.fixture
def client(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    return plexora.app.test_client()


def _inspect(paths, answers=None, sample=None):
    from plexora.server.models.import_proposal import inspect_paths

    return inspect_paths([str(p) for p in paths], answers=answers, sample=sample)


def _image_row(sample):
    return next(layer for layer in sample.layers if layer.reference)


def test_the_import_offers_each_image_as_a_sample(tmp_path):
    from plexora.server.models.import_proposal import NAME_JOIN, _clean_name

    path = _two_scans(tmp_path / "src")
    proposal = _inspect([path])

    assert len(proposal.samples) == 2
    names = [sample.name for sample in proposal.samples]
    assert names == [_clean_name(f"slide{NAME_JOIN}Scan A"),
                     _clean_name(f"slide{NAME_JOIN}Scan B")]
    keys = [sample.key for sample in proposal.samples]
    rows = [_image_row(sample) for sample in proposal.samples]
    assert len(set(keys)) == 2 and len({row.id for row in rows}) == 2
    assert [row.label for row in rows] == ["slide.vsi · Scan A", "slide.vsi · Scan B"]
    assert [row.src for row in rows] == [str(path), str(path)]
    assert [row.image_index for row in rows] == [0, 1]
    assert [row.to_dict()["imageIndex"] for row in rows] == [0, 1]
    # Each image's own size, channels and calibration.
    assert [(row.geometry["width"], row.geometry["height"], row.geometry["numChannels"])
            for row in rows] == [(400, 300, 2), (250, 200, 3)]
    assert [len(row.channels) for row in rows] == [2, 3]
    assert [row.pixel_size for row in rows] == [0.5, 0.25]
    # Every image is offered, so nothing says only one is opened.
    assert not [w for w in proposal.warnings if "holds" in w], proposal.warnings
    assert all(sample.existing is None for sample in proposal.samples)


def test_importing_the_second_image_records_and_serves_it(tmp_path, client):
    from plexora.server.models.project import Project
    from plexora.server.utils import pixel_scale

    path = _two_scans(tmp_path / "src")
    inspected = client.post("/import/inspect", json={"paths": [str(path)]}).get_json()
    second = inspected["samples"][1]
    response = client.post("/import/sample", json={"paths": [str(path)], "index": 1,
                                                   "key": second["key"]})
    assert response.status_code == 200, response.data[:400]
    name = response.get_json()["name"]
    assert name == second["name"]

    entry = client.get("/config").get_json()[name]
    assert entry["imageIndex"] == 1
    assert (entry["width"], entry["height"], entry["num_channels"]) == (250, 200, 3)
    assert [channel["fullname"] for channel in entry["imageData"]] == ["B1", "B2", "B3"]
    assert client.get(f"/init_database?datasource={name}").status_code == 200
    assert list(client.get(f"/get_channel_names?datasource={name}").get_json()) == [
        "B1", "B2", "B3"]
    tile = client.get(f"{entry['imageData'][2]['src']}0/0_0.png?q=hd")
    assert tile.status_code == 200, tile.data[:300]
    got = np.asarray(Image.open(io.BytesIO(tile.data)))
    assert got.shape[:2] == (200, 250)
    np.testing.assert_array_equal(got, vsi_expected(SCAN_B, 0, 2, 0, 0, 200, 0, 250))

    project = Project.load(name)
    assert project.image.index == 1
    assert pixel_scale.pixel_size(project)["value"] == pytest.approx(0.25)
    from plexora.server.models import data_model

    assert data_model.generate_thumbnail(name) is not None

    # Imported: that image is `existing`, and only that one.
    again = client.post("/import/inspect", json={"paths": [str(path)]}).get_json()
    assert [sample["existing"] for sample in again["samples"]] == [None, name]


def test_a_file_of_one_image_records_no_index(tmp_path):
    from plexora.datasource import register_image_datasource

    path = write_dv(tmp_path / "cells.dv", depth=1, times=1)
    entry = register_image_datasource("cells", path)
    assert "imageIndex" not in entry
    one_scan = write_vsi(tmp_path / "one", stem="one", stacks=[SCAN_A])
    assert "imageIndex" not in register_image_datasource("one", one_scan)


def test_reregistering_keeps_the_image(tmp_path):
    from plexora.datasource import register_image_datasource, reregister_image

    path = _two_scans(tmp_path / "src")
    entry = register_image_datasource("b", path, image_index=1)
    assert entry["imageIndex"] == 1 and (entry["width"], entry["height"]) == (250, 200)
    again = reregister_image("b").to_entry()
    assert again["imageIndex"] == 1 and (again["width"], again["height"]) == (250, 200)
    assert [channel["fullname"] for channel in again["imageData"]] == ["B1", "B2", "B3"]


def test_existing_is_found_per_image(tmp_path):
    from plexora.datasource import _find_existing_datasource_for_image

    path = str(_two_scans(tmp_path / "src"))
    config = {"first": {"channelFile": path},
              "second": {"channelFile": path, "imageIndex": 1}}
    assert _find_existing_datasource_for_image(path, config) == "first"
    assert _find_existing_datasource_for_image(path, config, index=0) == "first"
    assert _find_existing_datasource_for_image(path, config, index=1) == "second"
    assert _find_existing_datasource_for_image(path, config, index=2) is None


def test_the_pixel_size_is_read_per_image(tmp_path):
    from plexora.server.models.project import ImageSpec, Project
    from plexora.server.utils import pixel_scale

    path = str(_two_scans(tmp_path / "src"))
    sizes = [pixel_scale.pixel_size(Project(name=f"p{index}", image=ImageSpec(
        src=path, image_index=index)))["value"] for index in (0, 1, 0)]
    assert sizes == [pytest.approx(0.5), pytest.approx(0.25), pytest.approx(0.5)]


def test_caches_keyed_by_the_file_tell_its_images_apart(tmp_path):
    """Volume bricks and surface meshes are cached by what they were read
    from; two positions of one ND2 are two volumes, not one twice."""
    from plexora.server.models.project import ImageSpec, Project
    from plexora.server.surface import store
    from plexora.server.utils.source_image import ReaderShelf
    from plexora.server.volume import serve

    path = str(_two_scans(tmp_path / "src"))
    assert serve.source_identity(path) == serve.source_identity(path, 0)
    assert serve.source_identity(path, 1) != serve.source_identity(path)
    first, second = (Project(name=f"p{index}", image=ImageSpec(src=path, image_index=index))
                     for index in (0, 1))
    assert store._identity(first, "image") != store._identity(second, "image")

    from plexora.api.dataset import _project_data_for

    identities = {ReaderShelf.identity_of(_project_data_for(project))
                  for project in (first, second)}
    assert len(identities) == 2


def test_one_image_can_be_left_out(tmp_path):
    from plexora.server.models.import_proposal import _image_key_base

    path = _two_scans(tmp_path / "src")
    whole = _inspect([path])
    proposal = _inspect([path], answers={f"left-out:{path}": [f"{path}#0"]})
    assert len(proposal.samples) == 1
    only = proposal.samples[0]
    assert _image_row(only).image_index == 1
    assert only.key == whole.samples[1].key
    assert _image_key_base(only.key) == _image_key_base(whole.samples[0].key)


def test_the_dialog_leaves_one_image_out_the_way_the_server_reads_it():
    """The ✕ on one image's row (views/importSample.js `removeRow`) says
    `<src>#<image>`, which is `_left_out_image`'s spelling; another row's ✕
    still says its `src`."""
    from pathlib import Path

    from plexora.server.models.import_proposal import _left_out_image

    dialog = (Path(__file__).resolve().parent.parent / "plexora" / "client" / "src" / "js"
              / "views" / "importSample.js").read_text(encoding="utf-8")
    assert "`${layer.src}#${layer.imageIndex}`" in dialog
    assert _left_out_image("/data/scan.czi", 2) == "/data/scan.czi#2"


def test_a_mask_added_on_one_images_card_joins_that_image(tmp_path):
    import tifffile

    path = _two_scans(tmp_path / "src")
    labels = np.zeros((200, 250), np.uint16)
    labels[20:60, 20:60], labels[100:150, 120:200] = 1, 2
    mask = tmp_path / "src" / "cells.tif"
    tifffile.imwrite(mask, labels)
    key = _inspect([path]).samples[1].key

    proposal = _inspect([path, mask], answers={f"sample-for:{mask}": key,
                                               f"added-as:{mask}": "mask"})
    by_key = {sample.key: sample for sample in proposal.samples}
    assert len(proposal.samples) == 2
    assert [layer.role for layer in by_key[key].layers] == ["image", "mask"]
    assert [layer.role for layer in proposal.samples[0].layers] == ["image"]


def test_added_as_a_layer_it_is_the_first_image_and_says_so(tmp_path):
    from plexora.datasource import register_image_datasource

    register_image_datasource("cells", write_dv(tmp_path / "cells.dv", depth=1, times=1))
    path = _two_scans(tmp_path / "src")
    proposal = _inspect([path], sample="cells")
    assert len(proposal.samples) == 1
    assert any("holds 2 images" in warning and "Scan A" in warning
               for warning in proposal.warnings), proposal.warnings


# -- a data node ------------------------------------------------------------------


def test_a_node_serves_each_image_under_its_own_id(tmp_path, node_process):
    from plexora import nodes
    from plexora.server.models import data_model
    from plexora.server.models.import_sample import import_sample
    from plexora.server.models.project import Project
    from tests.node_harness import register

    node = node_process(dynamic=True)
    register("hms-o2", node)
    path = _two_scans(tmp_path / "src")
    pick = f"node://hms-o2/{path}"
    proposal = _inspect([pick])

    assert len(proposal.samples) == 2
    rows = [_image_row(sample) for sample in proposal.samples]
    assert [row.image_index for row in rows] == [0, 1]
    assert rows[1].src == f"node://hms-o2/{nodes.resource_id_for(str(path), 1)}"
    assert nodes.resource_id_for(str(path), 0) == nodes.resource_id_for(str(path))
    assert rows[0].src != rows[1].src

    result = import_sample([pick], index=1, key=proposal.samples[1].key)
    project = Project.load(result["name"])
    binding = project.resource("image")
    assert (binding.node, binding.resource_id) == ("hms-o2", nodes.resource_id_for(str(path), 1))
    assert (project.image.width, project.image.height) == (250, 200)
    assert project.image.channel_names == ["B1", "B2", "B3"]
    assert project.image.index == 1
    served = {entry["id"]: entry for entry in node.get("/node/v1/hello")["resources"]}
    assert served[binding.resource_id]["image_index"] == 1
    assert served[binding.resource_id]["images"] == ["Scan A", "Scan B"]

    data_model.load_datasource(result["name"], reload=True)
    key = project.image.channels[2]["src"].rstrip("/").rsplit("/", 1)[-1]
    tile, _ = data_model.encode_tile(result["name"], key, 0, "0_0", "webp")
    assert len(tile) > 0
    # And the same project found again, rather than imported twice.
    again = _inspect([pick])
    assert [sample.existing for sample in again.samples] == [None, result["name"]]


def test_a_node_manifest_keeps_which_image(tmp_path):
    from plexora.server.node import resources as node_resources

    path = _two_scans(tmp_path / "src")
    registry = node_resources.Registry()
    second = registry.add("image", "second", str(path), image_index=1)
    assert second.image_index == 1 and second.images == ("Scan A", "Scan B")
    assert second.provider.image_index == 1
    assert registry.add("image", "second", str(path), image_index=1) is second
    with pytest.raises(Exception, match="different resource"):
        registry.add("image", "second", str(path), image_index=0)
    with pytest.raises(Exception, match="no image 3"):
        registry.add("image", "third", str(path), image_index=2)
    registry.add("image", "first", str(path))
    manifest = tmp_path / "manifest.json"
    assert node_resources.save_manifest(manifest, registry)
    assert sorted(node_resources.load_manifest(manifest)) == [
        ("image", "first", str(path), 0), ("image", "second", str(path), 1)]
