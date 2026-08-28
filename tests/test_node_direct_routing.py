"""Whether the browser fetches a node-backed layer from us, or from the node.

The server's half of that decision is a candidate, not a verdict: it says where
the browser COULD go and hands over what it would need. Whether the browser can
actually get there is a fact about the browser's own network, and it settles it
by probing.

So these tests pin two things. The candidate has to be complete and correct --
an address a browser could use, a token, the project's tile grid, and a tile
base that the viewer's existing URL builder can append `<level>/<x>_<y>.png` to
without branching. And the node has to actually answer that URL, with the CORS
headers a cross-origin page needs to read the result, or the whole thing
silently degrades to the proxy for a reason nobody can see.
"""

from __future__ import annotations

import numpy as np
import pytest
import tifffile

from tests.helpers import ALL_CONFIRMED, project
from tests.node_harness import node_process, register  # noqa: F401 - fixture


VIEWER_ORIGIN = "http://127.0.0.1:8000"


@pytest.fixture
def client():
    from plexora import app

    return app.test_client()


def _image_file(directory):
    path = directory / "slide.ome.tif"
    rng = np.random.default_rng(3)
    tifffile.imwrite(path, rng.integers(0, 4000, (2, 512, 512), dtype=np.uint16),
                     photometric="minisblack")
    return path


@pytest.fixture
def routed(tmp_path, node_process):
    """A project whose image is on a node that allows the viewer's origin."""
    from plexora.nodes import attach_image

    path = _image_file(tmp_path)
    node = node_process(f"image:slide={path}", allow_origins=[VIEWER_ORIGIN])
    register("o2", node)
    # The image's own geometry: `nodes._same_image` refuses to repoint a
    # project at an image of another size, and a placeholder would be one.
    project("split", channels=("A", "B"), confirmed=ALL_CONFIRMED,
            width=512, height=512).save()
    attach_image("split", node="o2", resource_id="slide",
                 channel_names=["A", "B"])
    return node


def test_an_ordinary_project_is_told_there_is_nothing_to_route(client, tmp_path):
    """The whole mechanism costs a single request that answers `{}`.

    Worth pinning rather than assuming: this route is called on every viewer
    open, and anything it did here would be paid for by every project that has
    never heard of a data node.
    """
    project("plain", channels=("A",), confirmed=ALL_CONFIRMED,
            src=str(_image_file(tmp_path))).save()

    assert client.get("/resource_routing?datasource=plain").get_json() == {"routes": {}}
    assert client.get("/resource_routing?datasource=nope").get_json() == {"routes": {}}


def test_the_candidate_carries_everything_a_browser_needs(client, routed):
    routes = client.get("/resource_routing?datasource=split").get_json()["routes"]

    assert set(routes) == {"image"}
    image = routes["image"]
    assert image["node"] == "o2"
    assert image["endpoint"] == routed.endpoint
    # The token, deliberately: the browser IS the user, and it rides as `?t=`
    # so a tile request stays a CORS-simple GET with no preflight.
    assert f"t={routed.token}" in image["query"]
    # The project's tile grid, because that is the project's fact and not the
    # node's -- the node answers in whatever grid it is asked about, so the two
    # agreeing is what makes a tile land where the viewer put it.
    from plexora.server.models.project import Project

    recorded = Project.load("split").image
    assert f"tw={recorded.tile_width}" in image["query"]
    assert f"th={recorded.tile_height}" in image["query"]
    assert image["health"].startswith(routed.endpoint)
    assert image["append_key"] is True


def test_the_tile_base_is_what_the_viewer_appends_to(client, routed):
    """`getTileUrl` does `${src}${level}/${x}_${y}.png`, whichever server it is
    talking to. If these two shapes ever stop being interchangeable, every tile
    404s and the viewer just looks empty."""
    import urllib.request

    routes = client.get("/resource_routing?datasource=split").get_json()["routes"]
    image = routes["image"]

    # Exactly what main.js builds: base + channel key + "/", then what
    # getTileUrl appends, then the query.
    url = f"{image['tile_base']}slide_0/0/0_0.png?{image['query']}"
    with urllib.request.urlopen(url, timeout=60) as response:
        body = response.read()
        assert response.status == 200
        assert response.headers["Content-Type"] == "image/webp"
    assert len(body) > 0


def test_the_same_tile_comes_back_whichever_way_it_is_fetched(client, routed):
    """Direct and proxied have to be the same bytes.

    They are the same encode on the same machine -- the primary forwards rather
    than re-encodes -- so anything else would mean the proxy had started doing
    work of its own, which is the thing it must never do.
    """
    import urllib.request

    from plexora.server.models import data_model

    data_model.load_datasource("split", reload=True)
    routes = client.get("/resource_routing?datasource=split").get_json()["routes"]
    image = routes["image"]

    direct_url = f"{image['tile_base']}slide_0/0/0_0.png?{image['query']}"
    with urllib.request.urlopen(direct_url, timeout=60) as response:
        direct = response.read()

    proxied = client.get("/generated/data/split/slide_0/0/0_0.png")
    assert proxied.status_code == 200
    assert proxied.data == direct


def test_a_cross_origin_browser_is_allowed_to_read_the_answer(routed):
    """Without these headers the browser fetches the tile and is not permitted
    to look at it -- which reads as an empty viewer with a console warning, and
    is exactly what a node started without --allow-origin should produce."""
    import urllib.request

    request = urllib.request.Request(
        f"{routed.endpoint}/node/v1/health?t={routed.token}",
        headers={"Origin": VIEWER_ORIGIN})
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.headers["Access-Control-Allow-Origin"] == VIEWER_ORIGIN
        assert response.headers["Vary"] == "Origin"
        exposed = response.headers["Access-Control-Expose-Headers"]
    # The headers a caller acts on, not just the body: the value kind a buffer
    # decodes as, the generation a cache is keyed on, the ETag a conditional
    # request needs.
    for header in ("X-Value-Kind", "X-Plexora-Generation", "ETag"):
        assert header in exposed


def test_an_origin_that_was_not_allowed_gets_no_permission(routed):
    """An exact-origin echo, never `*`. A node holds somebody's data, and a
    wildcard would let any page in that browser read it with a token it found
    in a URL."""
    import urllib.request

    request = urllib.request.Request(
        f"{routed.endpoint}/node/v1/health?t={routed.token}",
        headers={"Origin": "http://evil.example"})
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.headers.get("Access-Control-Allow-Origin") is None


def test_a_forgotten_node_offers_the_browser_no_address(client, routed):
    """Better no candidate than one this machine cannot back up: the proxy will
    fail too, and `/resource_status` is what says why."""
    from plexora.nodes import forget_node

    forget_node("o2")
    assert client.get("/resource_routing?datasource=split").get_json() == {"routes": {}}


def test_a_mask_route_names_no_channel(client, tmp_path, node_process):
    """An image serves many channels from one resource and says which in the
    path; a mask has one plane and has nothing to name. `append_key` is that
    difference, and getting it backwards 404s every label tile."""
    from plexora.nodes import attach_segmentation
    from plexora.server.utils import segmentation_pyramid

    labels = np.zeros((512, 512), dtype=np.uint32)
    labels[10:40, 10:40] = 7
    flat = tmp_path / "mask.tif"
    tifffile.imwrite(flat, labels)
    mask = segmentation_pyramid.pyramidize_segmentation_mask(
        flat, tmp_path / "mask_pyramid.ome.tif", overwrite=True, outline=False)

    node = node_process(f"segmentation:mask={mask}")
    register("masks", node)
    project("m", channels=("A",), confirmed=ALL_CONFIRMED,
            src=str(_image_file(tmp_path))).save()
    attach_segmentation("m", node="masks", resource_id="mask")

    route = client.get("/resource_routing?datasource=m").get_json()["routes"]["segmentation"]
    assert route["append_key"] is False

    import urllib.request

    url = f"{route['tile_base']}0/0_0.png?{route['query']}"
    with urllib.request.urlopen(url, timeout=60) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "image/png"
