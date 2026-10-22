"""A volume's views behind everything that is not the tile route.

Display statistics and mixture fits per view, the data-node API speaking
`view=`, `SourceImage(view=)` and the agent-facing `ImageHandle`. Two claims
are held to the standard the planar tests hold them to:

- a planar image's answers -- payloads, cache keys, the URLs a primary sends a
  node -- are exactly what they were before views existed;
- a view of a volume served by a data node is byte-for-byte the view a local
  read produces, because the primary forwards those bytes verbatim.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
import tifffile

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from tests import volume_fixtures as vf
from tests.node_harness import node_process, register  # noqa: F401 - fixture


@pytest.fixture()
def client():
    return plexora.app.test_client()


def _write_volume(tmp_path, name="vol", height=96, width=80):
    """3 channels x 5 planes of `vf.stack`, two levels, z step 2 over xy 0.5."""
    return vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=3,
                               depth=5, levels=2, height=height, width=width)


def _register_volume(tmp_path, name="vol"):
    path, data = _write_volume(tmp_path, name)
    entry = register_image_datasource(name, path)
    return entry, data, path


def _register_planar(tmp_path, name="flat"):
    path = tmp_path / f"{name}.ome.tif"
    rng = np.random.default_rng(3)
    planes = rng.poisson(50, (2, 64, 64)).astype(np.uint16)
    planes[:, 10:20, 10:20] += 3000
    tifffile.imwrite(path, planes, ome=True)
    return register_image_datasource(name, path), planes


def _names(entry):
    return [channel["fullname"] for channel in entry["imageData"]]


def _key(entry, index):
    return entry["imageData"][index]["src"].rstrip("/").rsplit("/", 1)[-1]


# -- statistics and mixture fits per view -----------------------------------


def test_a_planar_images_stats_are_unchanged_and_refuse_a_view(tmp_path, client):
    entry, _ = _register_planar(tmp_path)
    name = _names(entry)[0]
    plain = client.get(f"/get_image_channel_stats?channel={name}&datasource=flat")
    assert plain.status_code == 200
    # The cache key it has always had.
    assert ("flat", name) in data_model._image_stats_cache
    assert data_model.get_image_channel_stats(name, "flat", "") is \
        data_model._image_stats_cache[("flat", name)]

    for route in ("get_image_channel_stats", "get_channel_gmm"):
        refused = client.get(f"/{route}?channel={name}&datasource=flat&view=z1")
        assert refused.status_code == 400
        assert "single plane" in refused.get_json()["error"]


def test_a_plane_or_a_section_shares_the_channels_stats(tmp_path, client):
    entry, _, _ = _register_volume(tmp_path)
    name = _names(entry)[1]
    plain = data_model.get_image_channel_stats(name, "vol")
    for view in ("z4", "xzy10", "z2"):
        assert data_model.get_image_channel_stats(name, "vol", view) is plain
    route = client.get(f"/get_image_channel_stats?channel={name}&datasource=vol&view=z4")
    assert route.status_code == 200
    assert route.get_json()["qmax"] == plain["qmax"]
    assert not [key for key in data_model._image_stats_cache if len(key) == 3]


def test_a_slab_has_its_own_stats_over_its_own_overview(tmp_path, client):
    from plexora.server.models import views
    from plexora.server.providers.local import volume_overview

    entry, data, _ = _register_volume(tmp_path)
    name = _names(entry)[1]
    plane = data_model.get_image_channel_stats(name, "vol")
    mip = data_model.get_image_channel_stats(name, "vol", "z1-3max")

    # The slab's histogram is of the projection, not of the default plane...
    overview = volume_overview(views.plane_pyramid_for("vol", "z1-3max"))
    expected = data_model.channel_stats_of(overview[1], mip["qmin"], mip["qmax"])
    assert mip["image_max"] == expected["image_max"]
    assert mip["image_histogram"] == expected["image_histogram"]
    assert mip["image_max"] > plane["image_max"]
    # ...while its window is the volume's: the one its tiles are encoded with.
    assert (mip["qmin"], mip["qmax"]) == (0.0, float(data[0, 1].max()))
    assert ("vol", name, "z1-3max") in data_model._image_stats_cache

    std = data_model.get_image_channel_stats(name, "vol", "zstd")
    assert std["qmax"] == float(np.ceil(data[0, 1].max() / 2))

    route = client.get(
        f"/get_image_channel_stats?channel={name}&datasource=vol&view=z1-3max")
    assert route.status_code == 200
    assert route.get_json()["image_max"] == mip["image_max"]


def test_a_slab_has_its_own_mixture_fit(tmp_path, client):
    entry, data, _ = _register_volume(tmp_path)
    name = _names(entry)[0]
    fit = data_model.get_channel_gmm(name, "vol", "z0-4mean")
    assert ("vol", name, "z0-4mean") in data_model._gmm_cache
    assert (fit["qmin"], fit["qmax"]) == (0.0, float(data[0, 0].max()))
    assert {"vmin", "vmax", "image_gmm_1"} <= set(fit)
    # A plane's fit is the channel's fit, under the key it always had.
    assert data_model.get_channel_gmm(name, "vol", "z1") is \
        data_model.get_channel_gmm(name, "vol")

    route = client.get(f"/get_channel_gmm?channel={name}&datasource=vol&view=z0-4mean")
    assert route.status_code == 200
    assert route.get_json()["qmax"] == fit["qmax"]


def test_bad_views_are_a_400_on_both_routes(tmp_path, client):
    entry, _, _ = _register_volume(tmp_path)
    name = _names(entry)[0]
    for route in ("get_image_channel_stats", "get_channel_gmm"):
        for view in ("z9", "zz", "z0-3sum", "z7-9max"):
            response = client.get(f"/{route}?channel={name}&datasource=vol&view={view}")
            assert response.status_code == 400, (route, view)
            assert response.get_json()["error"]


# -- SourceImage(view=) -----------------------------------------------------


def test_source_image_reads_each_kind_of_view(tmp_path):
    from plexora.server.utils.source_image import SourceImage

    _, data, _ = _register_volume(tmp_path)
    box = (0, 0, 80, 96)
    with SourceImage("vol", view="z4") as source:
        plane, clipped = source.read(1, 0, box)
        assert clipped == box
        assert np.array_equal(plane, data[0, 1, 4])
        assert source.view == "z4"
    with SourceImage("vol", view="z1-3max") as source:
        mip, _ = source.read(2, 0, box)
        assert np.array_equal(mip, data[0, 2, 1:4].max(0))
        assert source.levels == 2
        exact = source.provenance(0, ["C"])
        assert exact.view == "z1-3max" and exact.exact
        assert exact.warnings and "colocalization" in exact.warnings[0]
        assert not source.provenance(1).exact
    with SourceImage("vol", view="xzy10") as source:
        rows = round(5 * 2.0 / 0.5)
        assert source.level_shape(0) == (rows, 80)
        section, _ = source.read(0, 0, (0, 0, 80, rows))
        assert section.shape == (rows, 80)
        assert np.array_equal(section[0], data[0, 0, 0, 10])


def test_source_image_reads_a_volumes_default_plane_without_a_view(tmp_path):
    from plexora.server.utils.source_image import SourceImage

    _, data, _ = _register_volume(tmp_path)
    with SourceImage("vol") as source:
        plane, _ = source.read(0, 0, (0, 0, 80, 96))
        assert np.array_equal(plane, data[0, 0, 2])
        assert source.view == "z2"


def test_source_image_refuses_a_view_of_a_planar_image(tmp_path):
    from plexora.server.utils.source_image import RenderError, SourceImage

    _, planes = _register_planar(tmp_path)
    with pytest.raises(RenderError, match="single plane"):
        SourceImage("flat", view="z1")
    with SourceImage("flat") as source:
        assert source.view is None and source.provenance(0) is None
        plane, _ = source.read(1, 0, (0, 0, 64, 64))
        assert np.array_equal(plane, planes[1])


def test_source_image_refuses_a_view_the_volume_lacks(tmp_path):
    from plexora.server.utils.source_image import RenderError, SourceImage

    _register_volume(tmp_path)
    with pytest.raises(RenderError, match="outside"):
        SourceImage("vol", view="z9")


# -- ImageHandle -------------------------------------------------------------


def test_the_image_handle_describes_a_volume(tmp_path):
    from plexora.api.dataset import project_data

    _register_volume(tmp_path)
    _register_planar(tmp_path)
    image = project_data("vol").image
    described = image.volume()
    assert image.is_volumetric
    assert described["depth"] == 5 and described["channels"] == 3
    assert described["default_view"] == "z2"
    assert described["spacing"]["z"]["value"] == 2.0
    flat = project_data("flat").image
    assert flat.volume() is None and not flat.is_volumetric


def test_the_image_handle_reads_a_view_of_a_local_volume(tmp_path):
    from plexora.api.dataset import project_data
    from plexora.server.providers.base import ResourceError, ResourceNotLocal

    _, data, _ = _register_volume(tmp_path)
    image = project_data("vol").image
    pixels, box = image.read_region(0, (0, 0, 500, 500), [0, 2], view="z1-3max")
    assert box == (0, 0, 80, 96)
    assert pixels.shape == (2, 96, 80)
    assert np.array_equal(pixels[1], data[0, 2, 1:4].max(0))
    with pytest.raises(ResourceError):
        image.read_region(0, (0, 0, 80, 96), [0, 1], max_pixels=100, view="z0")
    # Without a view, the old contract: a local image is opened directly.
    with pytest.raises(ResourceNotLocal):
        image.read_region(0, (0, 0, 8, 8), [0])


# -- the node provider's requests -------------------------------------------


def _binding(node="nd"):
    from plexora.server.models.project import ResourceBinding

    return ResourceBinding(kind="image", provider="node", node=node, resource_id="slide")


def _capture(monkeypatch, headers=None):
    """Every node request the provider makes, answered with `headers`."""
    from plexora.server.providers import http, node as node_providers

    sent = []
    response = SimpleNamespace(headers=dict(headers or {}), data=b"{}")

    def bytes_request(_node, method, path, **kwargs):
        from plexora.server.providers import wire

        sent.append((method, path, kwargs.get("body")))
        body = (wire.pack_array(np.zeros((1, 1, 1), dtype=np.uint16), box=[0, 0, 1, 1])
                if path.endswith("/region") else b"tile")
        return body, SimpleNamespace(headers={"Content-Type": "image/webp",
                                              **(headers or {})})

    def json_request(_node, method, path, **kwargs):
        sent.append((method, path, None))
        return {"qmin": 0.0, "qmax": 9.0}

    def request(_node, method, path, **kwargs):
        sent.append((method, path, None))
        return response

    monkeypatch.setattr(http, "bytes_request", bytes_request)
    monkeypatch.setattr(http, "json_request", json_request)
    monkeypatch.setattr(http, "request", request)
    monkeypatch.setattr(node_providers.NodeImageProvider, "node",
                        property(lambda self: SimpleNamespace(name=self._binding.node)))
    return sent


def _volume_geometry():
    """What a node answers geometry with for a 3 x 5 x 96 x 80 stack."""
    from plexora.server.volume.axes import spacing_from
    from plexora.server.volume.model import Volume, level_infos

    spacing = spacing_from({"physical_size_x": 0.5, "physical_size_z": 2.0}, depth=5)
    volume = Volume(channels=3, depth=5, frames=1, height=96, width=80, dtype="uint16",
                    spacing=spacing, levels=level_infos([(96, 80), (48, 40)]),
                    source_kind="ome_tiff")
    return {"levels": 2, "num_channels": 3, "width": 80, "height": 96,
            "volume": volume.to_dict()}


def test_a_planar_projects_node_requests_are_unchanged(monkeypatch):
    from plexora.server.providers.node import NodeImageProvider
    from tests.helpers import image_spec

    sent = _capture(monkeypatch)
    provider = NodeImageProvider(_binding()).with_channels(["A", "B"], 1024, 1024) \
        .with_volume(image_spec(channels=("A", "B")))
    provider.tile("slide_0", 0, "0_0", "webp")
    provider.tile("slide_0", 0, "0_0", "webp", view="")
    provider.channel_stats("B")
    provider.quantization_window("A")
    provider.read_region(0, (0, 0, 8, 8), [1])
    assert [path for _method, path, _body in sent] == [
        "/node/v1/image/slide/tile/slide_0/0/0_0?q=webp&tw=1024&th=1024",
        "/node/v1/image/slide/tile/slide_0/0/0_0?q=webp&tw=1024&th=1024",
        "/node/v1/image/slide/stats?channel_index=1",
        "/node/v1/image/slide/quantization?channel_index=0",
        "/node/v1/image/slide/region",
    ]
    assert sent[-1][2] == {"level": 0, "box": [0, 0, 8, 8], "channels": [1],
                           "max_pixels": 0}
    from plexora.server.volume.viewspec import BadView

    with pytest.raises(BadView, match="single plane"):
        provider.tile("slide_0", 0, "0_0", "webp", view="z1")


def _volumetric_spec(**changes):
    import dataclasses

    from tests.helpers import image_spec

    return dataclasses.replace(
        image_spec(channels=("A", "B", "C"), width=80, height=96),
        shape={"t": 1, "c": 3, "z": 5, "y": 96, "x": 80}, z_semantics="depth",
        **changes)


def test_a_volumes_node_requests_name_the_view_and_the_reading(monkeypatch):
    from plexora.server.providers.node import NodeImageProvider

    sent = _capture(monkeypatch, headers={"X-Plexora-View": "z2"})
    provider = NodeImageProvider(_binding()).with_channels(["A", "B", "C"], 1024, 1024) \
        .with_volume(_volumetric_spec())
    provider._geometry = _volume_geometry()
    provider.tile("slide_0", 0, "0_0", "webp")
    assert sent[-1][1].endswith("?q=webp&tw=1024&th=1024&view=z2&zs=depth")
    provider.overview("A")
    assert sent[-1][1].endswith("overview?channel_index=0&view=z2&zs=depth")
    provider.quantization_window("B", window_class="volume")
    assert sent[-1][1].endswith("quantization?channel_index=1&view=z2&zs=depth&wc=volume")


def test_a_volumes_mini_map_overview_asks_the_node_for_the_view_on_screen(monkeypatch):
    """The mini-map of a node-backed stack shows the plane or projection on
    screen -- in 3-D, the cut's maximum -- not always the default plane."""
    from plexora.server.providers.node import NodeImageProvider

    sent = _capture(monkeypatch, headers={"X-Plexora-View": "z0-3max"})
    provider = NodeImageProvider(_binding()).with_channels(["A", "B", "C"], 1024, 1024) \
        .with_volume(_volumetric_spec())
    provider._geometry = _volume_geometry()
    assert provider.overview("B", view="z0-3max") == b"tile"
    assert sent[-1][1].endswith("overview?channel_index=1&view=z0-3max&zs=depth")


def test_a_mini_map_overview_at_a_view_the_stack_lacks_is_the_default_plane(monkeypatch):
    """The map is decoration: a view the node cannot read is its default
    plane, never an error."""
    from plexora.server.providers.node import NodeImageProvider

    sent = _capture(monkeypatch, headers={"X-Plexora-View": "z2"})
    provider = NodeImageProvider(_binding()).with_channels(["A", "B", "C"], 1024, 1024) \
        .with_volume(_volumetric_spec())
    provider._geometry = _volume_geometry()
    assert provider.overview("A", view="z9") == b"tile"
    assert sent[-1][1].endswith("overview?channel_index=0&view=z2&zs=depth")


def test_the_primary_hands_a_node_backed_overview_its_view(monkeypatch):
    """`generate_channel_overview` forwards the view to the node provider
    rather than dropping it (the map then showed the default plane always)."""
    asked = []

    class Remote:
        def overview(self, channel, view=None):
            asked.append((channel, view))
            return b"webp"

    monkeypatch.setattr(data_model, "ensure_loaded", lambda name: None)
    monkeypatch.setattr(data_model, "_remote_image", lambda: Remote())
    assert data_model.generate_channel_overview("vol", "A", "z1-3max") == b"webp"
    assert asked == [("A", "z1-3max")]


def test_a_section_carries_the_anisotropy_the_primary_draws_it_at(monkeypatch):
    from plexora.server.providers.node import NodeImageProvider

    sent = _capture(monkeypatch, headers={"X-Plexora-View": "xzy10"})
    typed = {"value": 1.0, "unit": "µm", "source": "manual"}
    provider = NodeImageProvider(_binding()).with_channels(["A", "B", "C"]) \
        .with_volume(_volumetric_spec(pixel_size_z=typed))
    provider._geometry = _volume_geometry()
    provider.read_region(0, (0, 0, 80, 10), [0], view="xzy10")
    body = sent[-1][2]
    # Typed 1.0 over the file's 0.5 xy: the section is drawn at 2, not 4.
    assert body["view"] == "xzy10" and body["az"] == "2.0"


def test_a_node_too_old_for_volumes_is_refused_not_trusted(monkeypatch):
    from plexora.server.providers.node import NodeImageProvider
    from plexora.server.volume.viewspec import BadView

    # A node that answers geometry without volume keys: the project keeps its
    # default plane, served the old way, and views say why they cannot be.
    sent = _capture(monkeypatch)
    old = NodeImageProvider(_binding("old")).with_channels(["A", "B", "C"]) \
        .with_volume(_volumetric_spec())
    old._geometry = {"levels": 2, "num_channels": 3, "width": 80, "height": 96}
    assert old.volume() is None
    with pytest.raises(BadView, match="predate volumes"):
        old.tile("slide_0", 0, "0_0", "webp", view="z1")
    old.tile("slide_0", 0, "0_0", "webp")
    assert "view=" not in sent[-1][1]

    # A node that described a volume but then answers without serving the
    # view (restarted on an older Plexora): refused rather than captioned.
    sent = _capture(monkeypatch)
    stale = NodeImageProvider(_binding("stale")).with_channels(["A", "B", "C"]) \
        .with_volume(_volumetric_spec())
    stale._geometry = _volume_geometry()
    with pytest.raises(BadView, match="predates volumes"):
        stale.tile("slide_0", 0, "0_0", "webp", view="z1")
    with pytest.raises(BadView, match="predates volumes"):
        stale.channel_stats("A", view="z0-4max")


def test_a_volume_crosses_the_wire_intact():
    from plexora.server.volume.model import Volume

    described = _volume_geometry()["volume"]
    volume = Volume.from_dict(described)
    assert volume.to_dict() == described
    assert volume.spacing.anisotropy() == pytest.approx(4.0)


# -- a volume on a data node ------------------------------------------------


#: The node volume's size (see `volume_node`).
NODE_H, NODE_W = 512, 448


@pytest.fixture
def volume_node(tmp_path, node_process):  # noqa: F811 - the harness fixture
    """`here`, a volume registered locally, and `remote`, the same file
    registered locally and then moved onto a real node -- the ordinary way a
    project comes to read its image from another machine.

    Big enough to have a level of at least 200 px: a node opens its default
    reading of the file on first use, and the 2-D reader that reading takes
    for a stack folded into channels cannot yet open a pyramid with no such
    level (the base series opens as a zarr group, not an array)."""
    from plexora.nodes import attach_image

    path, data = _write_volume(tmp_path, "vol", height=NODE_H, width=NODE_W)
    planar = tmp_path / "flat.ome.tif"
    tifffile.imwrite(planar, np.full((2, 32, 32), 7, dtype=np.uint16), ome=True)
    node = node_process(f"image:slide={path}", f"image:flat={planar}")
    register("volnode", node)
    here = register_image_datasource("here", path)
    register_image_datasource("remote", path)
    attached = attach_image("remote", node="volnode", resource_id="slide",
                            channel_names=["A", "B", "C"])
    return SimpleNamespace(node=node, here=here, attached=attached, data=data, path=path)


def test_attaching_a_volume_keeps_its_reading(volume_node):
    image = volume_node.attached.image
    assert image.num_channels == 3
    assert image.z_semantics == "depth"
    assert image.is_volumetric and image.default_view == "z2"
    assert volume_node.attached.resource("image").node == "volnode"


def test_a_fresh_project_attached_to_a_z_stack_records_a_volume(tmp_path, node_process):  # noqa: F811
    """A project made on the primary with nothing but a node's image learns
    it is a volume from the node, and serves its planes."""
    from plexora.nodes import attach_image
    from tests.helpers import ALL_CONFIRMED, project

    path, data = vf.write_ome_volume(tmp_path / "zyx.ome.tif", axes="ZYX", channels=1,
                                     depth=4, levels=2, height=NODE_H, width=NODE_W)
    node = node_process(f"image:stack={path}")
    register("stacknode", node)
    project("fresh", channels=("DNA",), confirmed=ALL_CONFIRMED,
            width=NODE_W, height=NODE_H).save()
    attached = attach_image("fresh", node="stacknode", resource_id="stack")
    assert attached.image.is_volumetric
    assert attached.image.shape["z"] == 4 and attached.image.default_view == "z2"
    assert attached.image.z_semantics == "depth"

    data_model.load_datasource("fresh", reload=True)
    from PIL import Image
    import io

    payload, _ = data_model.encode_tile("fresh", "stack_0", 0, "0_0", "hd", "z1")
    assert np.array_equal(np.asarray(Image.open(io.BytesIO(payload))), data[0, 0, 1])


def test_a_nodes_geometry_names_a_volume_and_only_a_volume(volume_node):
    node = volume_node.node
    volume = node.get("/node/v1/image/slide/geometry?zs=depth")
    assert volume["num_channels"] == 3
    assert (volume["depth"], volume["frames"], volume["axes"]) == (5, 1, "czyx")
    assert volume["default_view"] == "z2"
    assert volume["spacing"]["z"]["value"] == 2.0
    assert volume["volume"]["levels"][1]["width"] == NODE_W // 2

    flat = node.get("/node/v1/image/flat/geometry")
    for key in ("depth", "frames", "axes", "spacing", "default_view", "volume"):
        assert key not in flat
    assert flat["num_channels"] == 2


def test_a_node_serves_planes_and_slabs_byte_identical_to_a_local_read(volume_node):
    key = _key(volume_node.here, 0)
    expected = {}
    data_model.load_datasource("here", reload=True)
    for view in ("", "z3", "z0-4max", "xzy10"):
        for quality in ("webp", "hd"):
            expected[view, quality] = data_model.encode_tile(
                "here", key, 0, "0_0", quality, view)
    overview_here = data_model.generate_channel_overview("here", _names(volume_node.here)[0])

    data_model.load_datasource("remote", reload=True)
    for (view, quality), local in expected.items():
        remote = data_model.encode_tile("remote", "slide_0", 0, "0_0", quality, view)
        assert remote == local, (view, quality)
    assert data_model.generate_channel_overview("remote", "A") == overview_here


def test_a_nodes_slab_statistics_match_a_local_read(volume_node):
    name = _names(volume_node.here)[1]
    data_model.load_datasource("here", reload=True)
    local = data_model.get_image_channel_stats(name, "here", "z0-4max")
    local_window = data_model.get_channel_quantization_window(name, "here")
    local_std = data_model.get_channel_quantization_window(name, "here", "std")

    data_model.load_datasource("remote", reload=True)
    remote = data_model.get_image_channel_stats("B", "remote", "z0-4max")
    assert remote["qmax"] == local["qmax"]
    assert remote["image_max"] == pytest.approx(local["image_max"])
    assert remote["vmax_hint"] == pytest.approx(local["vmax_hint"])
    assert len(remote["image_histogram"]) == len(local["image_histogram"])
    assert data_model.get_channel_quantization_window("B", "remote") == local_window
    assert data_model.get_channel_quantization_window("B", "remote", "std") == local_std
    fit = data_model.get_channel_gmm("B", "remote", "z0-4max")
    assert fit["qmax"] == local["qmax"]


def test_the_viewer_routes_serve_a_nodes_views(volume_node, client):
    data_model.load_datasource("remote", reload=True)
    info = client.get("/volume/remote/info").get_json()
    assert info["volumetric"] and info["depth"] == 5

    response = client.get("/generated/data/remote/slide_0/0/0_0.png?q=hd&view=z3")
    assert response.status_code == 200
    assert response.headers["ETag"].endswith('-hd-z3"')
    from PIL import Image
    import io

    assert np.array_equal(np.asarray(Image.open(io.BytesIO(response.data))),
                          volume_node.data[0, 0, 3])
    assert client.get("/generated/data/remote/slide_0/0/0_0.png?view=z9").status_code == 400


def test_a_nodes_mini_map_shows_the_plane_and_the_cut_on_screen(volume_node, client):
    """The mini-map of a node-backed stack: a plane's own overview (not the
    default plane's, which is what a plane's statistics use), and a cut's
    maximum -- as a local stack's map is drawn."""
    data_model.load_datasource("remote", reload=True)
    one = client.get("/generated/overview/remote/A?view=z1")
    three = client.get("/generated/overview/remote/A?view=z3")
    cut = client.get("/generated/overview/remote/A?view=z0-4max")
    assert one.status_code == three.status_code == cut.status_code == 200
    assert one.data != three.data
    assert one.headers["ETag"] != three.headers["ETag"]
    assert cut.data not in (one.data, three.data)
    # Scrubbing through the stack keeps a few planes, not all of them.
    for z in range(5):
        assert client.get(f"/generated/overview/remote/A?view=z{z}").status_code == 200


def test_a_node_refuses_a_view_it_does_not_have(volume_node):
    import urllib.error

    node = volume_node.node
    for path in ("/node/v1/image/slide/tile/slide_0/0/0_0?view=z9&zs=depth",
                 "/node/v1/image/slide/tile/slide_0/0/0_0?view=z0-4sum&zs=depth",
                 "/node/v1/image/slide/stats?channel_index=0&view=z1&zs=nonsense",
                 "/node/v1/image/flat/tile/flat_0/0/0_0?view=z1"):
        with pytest.raises(urllib.error.HTTPError) as refused:
            node.get(path, raw=True)
        assert refused.value.code == 400, path
    # And says which view it served when it serves one.
    import urllib.request

    request = urllib.request.Request(
        node.url("/node/v1/image/slide/tile/slide_0/0/0_0?view=z0-4max&zs=depth"))
    request.add_header("X-Plexora-Node-Token", node.token)
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.headers["X-Plexora-View"] == "z0-4max"


def test_region_reads_of_a_nodes_views_match_a_local_read(volume_node):
    from plexora.api.dataset import project_data
    from plexora.server.utils.source_image import SourceImage

    data = volume_node.data
    remote = project_data("remote").image
    assert remote.is_volumetric and remote.volume()["depth"] == 5
    pixels, box = remote.read_region(0, (0, 0, NODE_W, NODE_H), [0, 2], view="z0-4max")
    assert box == (0, 0, NODE_W, NODE_H)
    assert np.array_equal(pixels[1], data[0, 2].max(0))
    with SourceImage("remote", view="z3") as source:
        plane, _ = source.read(1, 0, (0, 0, NODE_W, NODE_H))
        assert np.array_equal(plane, data[0, 1, 3])
        assert source.provenance(0, ["B"]).view == "z3"
    with SourceImage("remote", view="xzy10") as source:
        rows = round(5 * 2.0 / 0.5)
        assert source.level_shape(0) == (rows, NODE_W)
        section, _ = source.read(0, 0, (0, 0, NODE_W, rows))
        assert np.array_equal(section[0], data[0, 0, 0, 10])


# -- a node's 3-D levels ------------------------------------------------------


_LEVEL_HEADERS = ("X-Plexora-Shape", "X-Plexora-Window", "X-Plexora-Level",
                  "X-Plexora-ZPool", "X-Plexora-Box", "X-Plexora-Z", "X-Plexora-Format")


def _level(client, datasource, channel, query=""):
    response = client.get(f"/volume/{datasource}/level?channel={channel}{query}")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response


def test_a_nodes_3d_level_is_a_local_reads_bytes(volume_node, client):
    """The 3-D view's levels of a node's volume are built there by the same
    functions (server/volume/serve.py), with the primary's choices sent along:
    the bytes and every header a local read gives -- whole levels, HD, an area
    through some planes under a named window, and a pooled level."""
    from plexora.server.volume import serve

    queries = ("&level=0", "&level=1&format=f16",
               "&level=0&box=40,64,200,180&z=1,3&w=0,3000", "&level=0&pool=2", "")
    serve.clear()
    name = _names(volume_node.here)[1]
    data_model.load_datasource("here", reload=True)
    local = {query: _level(client, "here", name, query) for query in queries}
    data_model.load_datasource("remote", reload=True)
    for query, expected in local.items():
        remote = _level(client, "remote", "B", query)
        assert remote.data == expected.data, query
        for header in _LEVEL_HEADERS:
            assert remote.headers.get(header) == expected.headers.get(header), (query, header)
    assert local["&level=0&pool=2"].headers["X-Plexora-Shape"] == f"3,{NODE_H},{NODE_W}"


def test_a_nodes_level_is_fetched_once_and_revalidated_here(volume_node, client, monkeypatch):
    from plexora.server.providers.node import NodeImageProvider
    from plexora.server.volume import serve

    serve.clear()
    data_model.load_datasource("remote", reload=True)
    calls = []
    original = NodeImageProvider.level

    def counting(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(NodeImageProvider, "level", counting)
    first = _level(client, "remote", "B", "&level=0")
    again = _level(client, "remote", "B", "&level=0")
    assert again.data == first.data and len(calls) == 1, "kept here like a local payload"
    serve.clear()
    revalidated = client.get("/volume/remote/level?channel=B&level=0",
                             headers={"If-None-Match": first.headers["ETag"]})
    assert revalidated.status_code == 304 and len(calls) == 1, \
        "a revalidation is answered here, before the node is asked"


def test_a_nodes_tile_is_quantized_with_the_window_it_names(volume_node):
    """A volume's 8-bit tile names its window (`w=`), fetched through the
    primary or straight off the node: the node quantizes with that window."""
    key = _key(volume_node.here, 1)
    data_model.load_datasource("here", reload=True)
    named = data_model.encode_tile("here", key, 0, "0_0", "webp", "z3", window=(0.0, 900.0))
    assert named != data_model.encode_tile("here", key, 0, "0_0", "webp", "z3")
    data_model.load_datasource("remote", reload=True)
    assert data_model.encode_tile("remote", "slide_1", 0, "0_0", "webp", "z3",
                                  window=(0.0, 900.0)) == named


def test_a_nodes_section_pyramid_is_built_once_per_view(tmp_path):
    """A section's tiles on a node share one derived pyramid (and what its
    levels have read) instead of building one per tile; a scrub through cuts
    keeps only the most recent few."""
    from plexora.server.node import api
    from plexora.server.volume.sources import open_volume
    from plexora.server.volume.viewspec import ViewSpec

    path, _ = _write_volume(tmp_path)
    source = open_volume(str(path), z_semantics="depth")
    assert source is not None
    try:
        resource = SimpleNamespace(derived={})

        def view(token):
            return api._VolumeView(source, source, ViewSpec.parse(token, source.volume),
                                   None, None, None)

        first = api._view_pyramid(resource, view("xzy10"))
        assert api._view_pyramid(resource, view("xzy10")) is first
        for y in range(api._VIEW_PYRAMIDS):
            api._view_pyramid(resource, view(f"xzy{20 + y}"))
        assert len(resource.derived["view-pyramids"]) == api._VIEW_PYRAMIDS
        assert api._view_pyramid(resource, view("xzy10")) is not first
    finally:
        source.close()


def test_a_nodes_volume_opens_on_a_provisional_window(tmp_path, node_process,  # noqa: F811
                                                       monkeypatch):
    """A node scanning in the background (as in production) answers a
    volume's window and stats packet at once: the provisional window a local
    read would start with, until the exact one is known -- which is then the
    local exact window. Nothing provisional is kept on the primary."""
    import time

    from plexora.nodes import attach_image
    from plexora.server.volume.sources import open_volume
    from plexora.server.volume.windows import provisional_window

    path, _ = _write_volume(tmp_path, "vol", height=NODE_H, width=NODE_W)
    node = node_process(f"image:slide={path}", env={"PLEXORA_WINDOW_SCANS_INLINE": "0"})
    register("lazynode", node)
    here = register_image_datasource("here", path)
    register_image_datasource("remote", path)
    attach_image("remote", node="lazynode", resource_id="slide",
                 channel_names=["A", "B", "C"])
    data_model.load_datasource("here", reload=True)
    exact = data_model._volume_quantization_window(_names(here)[1], "here")
    local = open_volume(str(path), z_semantics="depth")
    try:
        guess = provisional_window(local, 1)[0]
    finally:
        local.close()

    monkeypatch.setattr(data_model, "_WINDOW_SCANS_INLINE", False)
    data_model.load_datasource("remote", reload=True)
    window, is_exact = data_model.volume_window_state("B", "remote")
    if not is_exact:
        assert window == pytest.approx(guess)
        assert ("qwindow", "remote", "B", "volume") not in data_model._gmm_cache
    deadline = time.monotonic() + 60
    while not is_exact and time.monotonic() < deadline:
        time.sleep(0.2)
        window, is_exact = data_model.volume_window_state("B", "remote")
    assert is_exact and window == pytest.approx(exact)
    stats = data_model.get_image_channel_stats("B", "remote")
    assert stats["window_exact"] is True and stats["qmax"] == pytest.approx(exact[1])
