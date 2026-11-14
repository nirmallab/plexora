"""A data node's provisional window must not outlive its scan on the primary.

A node asked about a planar channel it has never scanned answers at once with
a provisional window (the pooled overview's max, `node/api._provisional_window`)
and queues the full-resolution scan. On the node itself everything made with
that guess is transient: its tiles go out `no-store`, and its stats and fit are
keyed by the window, so the exact answers replace them once the scan lands.

These tests put the primary in front of such a node -- a real node app, in
process, its scan queue drained by hand -- and ask the primary's own entry
points before and after the scan. Whatever the primary hands the viewer while
the window is provisional must be replaced by the exact answer once the node
has it: the stats packet (its qmin/qmax convert the viewer's saved raw ranges),
the quantization window, the contrast fit, and a tile forwarded through
/generated/data. A guess filed for the whole load, or a tile sent with a
year-long max-age, keeps the clipped picture and the wrong units on screen.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import data_model
from plexora.server.node import api as node_api
from plexora.server.node.app import create_node_app

TOKEN_HEADER = {"X-Plexora-Node-Token": "x"}
DATASOURCE = "audit_node_planar_provisional"
CHANNELS = ("A", "B")
SIZE = 256
#: The forced guess, far below the image's true ceiling (several thousand), so
#: a provisional answer and the exact one cannot coincide by accident.
GUESS = (0.0, 100.0)


def _quiet(*_args, **_kwargs):
    pass


def _image_file(directory):
    rng = np.random.default_rng(5)
    data = np.zeros((len(CHANNELS), SIZE, SIZE), dtype=np.uint16)
    for index in range(len(CHANNELS)):
        data[index] = rng.poisson(40 * (index + 1), (SIZE, SIZE)).astype(np.uint16)
        data[index, 60:90, 60:90] += 4000 * (index + 1)
    path = directory / "planar.ome.tif"
    tifffile.imwrite(path, data, photometric="minisblack")
    return path


def _clear_primary_caches():
    for key in [k for k in data_model._image_stats_cache if k[0] == DATASOURCE]:
        data_model._image_stats_cache.pop(key, None)
    for key in [k for k in list(data_model._gmm_cache)
                if isinstance(k, tuple) and DATASOURCE in k]:
        data_model._gmm_cache.pop(key, None)
    from plexora.server.routes import data_routes

    with data_routes._tile_png_cache_lock:
        for key in [k for k in data_routes._tile_png_cache if DATASOURCE in k]:
            data_routes._tile_png_cache.pop(key, None)


@pytest.fixture()
def primary_on_a_scanning_node(tmp_path, monkeypatch):
    """A primary whose loaded planar project reads its image from a node that
    has not scanned any channel yet. Yields the node resource."""
    from plexora.server.providers import http
    from plexora.server.providers import node as node_providers
    from plexora.server.providers.node import NodeImageProvider
    from tests.helpers import image_spec

    # The node as in production: a cold channel is answered with the guess and
    # its scan only queued; the test drains the queue when it wants the scan.
    monkeypatch.setattr(node_api, "_WINDOW_SCANS_INLINE", False)
    monkeypatch.setattr(node_api, "_ensure_window_scanner", lambda: None)
    monkeypatch.setattr(node_api, "_provisional_window", lambda r, i: GUESS)
    node_api._tile_cache.clear()

    image = _image_file(tmp_path)
    app = create_node_app([f"image:slide={image}"], token="x", log=_quiet)
    resource = app.config["PLEXORA_NODE_RESOURCES"].get("slide")
    node_client = app.test_client()

    def request(_node, method, path, **_kwargs):
        assert method == "GET", path
        answer = node_client.get(path, headers=TOKEN_HEADER)
        assert answer.status_code == 200, (path, answer.status_code, answer.data[:200])
        return answer

    monkeypatch.setattr(http, "request", request)
    monkeypatch.setattr(node_providers.NodeImageProvider, "node",
                        property(lambda self: SimpleNamespace(name="nd")))

    from plexora.server.models.project import ResourceBinding

    binding = ResourceBinding(kind="image", provider="node", node="nd", resource_id="slide")
    provider = NodeImageProvider(binding).with_channels(list(CHANNELS), SIZE, SIZE) \
        .with_volume(image_spec(channels=CHANNELS, width=SIZE, height=SIZE))

    generation = data_model.load_generation
    monkeypatch.setattr(data_model, "ensure_loaded", lambda name: generation)
    monkeypatch.setattr(data_model, "_remote", True)
    monkeypatch.setattr(data_model, "_remote_image", lambda: provider)
    monkeypatch.setattr(data_model, "volume_source", None)
    # No window banked on the primary's side for this project.
    monkeypatch.setattr(data_model, "_quantization_windows", lambda name: {})

    _clear_primary_caches()
    try:
        yield resource
    finally:
        _clear_primary_caches()
        node_api._tile_cache.clear()


def _exact_window(resource, index):
    return tuple(resource.derived[("qwindow", index)])


def test_a_nodes_stats_packet_is_asked_again_once_its_window_is_exact(
        primary_on_a_scanning_node):
    """The packet the viewer converts saved raw ranges with: made with the
    node's guess before the scan, with the exact window after it."""
    resource = primary_on_a_scanning_node
    early = data_model.get_image_channel_stats("A", DATASOURCE)
    assert early["qmax"] == pytest.approx(GUESS[1]), "precondition: the node is still guessing"

    node_api._drain_window_scans()
    exact = _exact_window(resource, 0)
    assert exact[1] > GUESS[1]

    later = data_model.get_image_channel_stats("A", DATASOURCE)
    assert later["qmax"] == pytest.approx(exact[1]), (
        "the primary kept the stats packet made with the node's provisional window "
        f"(qmax {later['qmax']}) after the node's exact window ({exact[1]}) landed")


def test_a_nodes_quantization_window_is_asked_again_once_it_is_exact(
        primary_on_a_scanning_node):
    """The window the primary's tile path and its readers quantize with."""
    resource = primary_on_a_scanning_node
    early = data_model.get_channel_quantization_window("A", DATASOURCE)
    assert early[1] == pytest.approx(GUESS[1]), "precondition: the node is still guessing"

    node_api._drain_window_scans()
    exact = _exact_window(resource, 0)

    later = data_model.get_channel_quantization_window("A", DATASOURCE)
    assert later[1] == pytest.approx(exact[1]), (
        f"the primary kept the node's provisional window {later} after the exact "
        f"window {exact} landed")


def test_a_nodes_contrast_fit_is_asked_again_once_its_window_is_exact(
        primary_on_a_scanning_node):
    """The fit carries the window it was made with (qmin/qmax)."""
    resource = primary_on_a_scanning_node
    early = data_model.get_channel_gmm("A", DATASOURCE)
    assert early["qmax"] == pytest.approx(GUESS[1]), "precondition: the node is still guessing"

    node_api._drain_window_scans()
    exact = _exact_window(resource, 0)

    later = data_model.get_channel_gmm("A", DATASOURCE)
    assert later["qmax"] == pytest.approx(exact[1]), (
        "the primary kept the contrast fit made with the node's provisional window "
        f"(qmax {later['qmax']}) after the exact window ({exact[1]}) landed")


def test_a_proxied_tile_drawn_with_a_guess_is_not_kept(primary_on_a_scanning_node):
    """A tile forwarded through the primary while the node's window is
    provisional is as uncachable as the node made it, and the next fetch after
    the scan is the exact rendering, not the kept guess."""
    resource = primary_on_a_scanning_node
    client = plexora.app.test_client()
    url = f"/generated/data/{DATASOURCE}/slide_0/0/0_0.png"

    guessed = client.get(url)
    assert guessed.status_code == 200
    assert guessed.headers["Cache-Control"] == "no-store", (
        "the primary sent a tile the node rendered with its provisional window as "
        f"{guessed.headers['Cache-Control']!r}")

    node_api._drain_window_scans()
    assert ("qwindow", 0) in resource.derived

    settled = client.get(url)
    assert settled.status_code == 200
    assert settled.data != guessed.data, (
        "after the node's exact window landed the primary still served the tile "
        "it rendered with the provisional window")
