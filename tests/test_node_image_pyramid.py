"""Pyramidized copies of flat images that live on a data node.

The node holds the original, so the node builds the copy -- beside the
original, or under its own data root -- and the primary waits for it before
recording the image, because the copy's level count is what it records. The
same offer, the same six steps on the rail, the same copy adopted next time.
"""

from __future__ import annotations

import time
import types
from pathlib import Path

import numpy as np
import pytest
import tifffile as tf

from plexora.server.utils import image_pyramid

TOKEN_HEADER = {"X-Plexora-Node-Token": "x"}


def _quiet(*_args, **_kwargs):
    """A node builder's log, silenced."""


@pytest.fixture
def small_threshold(monkeypatch):
    monkeypatch.setattr(image_pyramid, "FLAT_IMAGE_MAX_SIDE", 300)


def _flat(path, shape=(2, 1500, 1200)):
    pixels = np.random.default_rng(0).integers(0, 60000, shape, dtype=np.uint16)
    tf.imwrite(path, pixels)
    return pixels


def _node(dynamic=True, serve=()):
    from plexora.server.node.app import create_node_app

    return create_node_app(list(serve), token="x", dynamic=dynamic, log=_quiet)


def _wait(client, resource_id, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/node/v1/resources/{resource_id}/status",
                           headers=TOKEN_HEADER).get_json()["resource"]
        if state["state"] != "preparing":
            return state
        time.sleep(0.05)
    raise AssertionError("the build never finished")


def test_a_shared_flat_image_is_offered_a_copy(tmp_path, small_threshold):
    _flat(tmp_path / "slide.tif")
    app = _node()
    resource = app.config["PLEXORA_NODE_RESOURCES"].add(
        "image", "slide", str(tmp_path / "slide.tif"))
    offer = resource.describe()["pyramid"]
    assert offer["channels"] == 2 and offer["levels"] == 2
    assert offer["output_name"] == "slide.pyramid.ome.tiff"


def test_the_node_builds_the_copy_and_serves_it(tmp_path, small_threshold):
    pixels = _flat(tmp_path / "slide.tif")
    app = _node()
    resource = app.config["PLEXORA_NODE_RESOURCES"].add(
        "image", "slide", str(tmp_path / "slide.tif"))
    client = app.test_client()

    answer = client.post("/node/v1/resources/slide/pyramidize", headers=TOKEN_HEADER)
    assert answer.status_code == 200, answer.get_data(as_text=True)
    state = _wait(client, "slide")
    assert state["state"] == "ready", state.get("error")
    assert state["pyramid"] is None and state["progress"] is None

    copy = tmp_path / "slide.pyramid.ome.tiff"
    assert Path(resource.path) == copy
    assert resource.source_path == str(tmp_path / "slide.tif")
    geometry = client.get("/node/v1/image/slide/geometry",
                          headers=TOKEN_HEADER).get_json()
    assert geometry["levels"] == 2
    with tf.TiffFile(copy, is_ome=False) as tiff:
        np.testing.assert_array_equal(tiff.series[0].levels[0].asarray(), pixels)
    tile = client.get("/node/v1/image/slide/tile/ch_0/1/0_0", headers=TOKEN_HEADER)
    assert tile.status_code == 200


def test_a_read_only_folder_puts_the_copy_under_the_nodes_data_root(
        tmp_path, monkeypatch, small_threshold):
    from plexora import paths
    from plexora.server.node import resources as node_resources

    _flat(tmp_path / "slide.tif")
    real = paths.is_writable
    monkeypatch.setattr(paths, "is_writable",
                        lambda root: Path(root) != tmp_path and real(root))
    app = _node()
    resource = app.config["PLEXORA_NODE_RESOURCES"].add(
        "image", "slide", str(tmp_path / "slide.tif"))
    client = app.test_client()
    client.post("/node/v1/resources/slide/pyramidize", headers=TOKEN_HEADER)
    assert _wait(client, "slide")["state"] == "ready"
    assert Path(resource.path).parent == node_resources.node_image_dir("slide")
    assert not (tmp_path / "slide.pyramid.ome.tiff").exists()


def test_a_restarted_node_serves_the_copy_it_built(tmp_path, small_threshold):
    _flat(tmp_path / "slide.tif")
    image_pyramid.pyramidize_image(tmp_path / "slide.tif",
                                   tmp_path / "slide.pyramid.ome.tiff", tile_size=256)
    resource = _node().config["PLEXORA_NODE_RESOURCES"].add(
        "image", "slide", str(tmp_path / "slide.tif"))
    assert Path(resource.path) == tmp_path / "slide.pyramid.ome.tiff"
    assert resource.describe()["pyramid"] is None


def test_a_static_node_will_not_write_one(tmp_path, small_threshold):
    _flat(tmp_path / "slide.tif")
    app = _node(dynamic=False, serve=[f"image:slide={tmp_path / 'slide.tif'}"])
    answer = app.test_client().post("/node/v1/resources/slide/pyramidize",
                                    headers=TOKEN_HEADER)
    assert answer.status_code == 403


def test_a_failed_build_says_why(tmp_path, monkeypatch, small_threshold):
    _flat(tmp_path / "slide.tif")
    monkeypatch.setattr(image_pyramid, "_free_bytes", lambda directory: 0)
    app = _node()
    resource = app.config["PLEXORA_NODE_RESOURCES"].add(
        "image", "slide", str(tmp_path / "slide.tif"))
    client = app.test_client()
    client.post("/node/v1/resources/slide/pyramidize", headers=TOKEN_HEADER)
    state = _wait(client, "slide")
    assert state["state"] == "error"
    assert "nowhere to write a pyramidized copy" in state["error"]
    assert Path(resource.path) == tmp_path / "slide.tif"


# -- the primary ------------------------------------------------------------

@pytest.fixture
def fake_node(monkeypatch):
    """`plexora.nodes` answering from a script of states."""
    from plexora import nodes
    from plexora.server.models import import_sample

    monkeypatch.setattr(import_sample, "NODE_PYRAMID_POLL_S", 0)
    script = {"states": [], "started": []}

    def status(node, resource_id, timeout=30.0):
        return dict(script["states"].pop(0))

    def start(node, resource_id, timeout=30.0):
        script["started"].append(resource_id)
        return {"state": "preparing"}

    monkeypatch.setattr(nodes, "resource_status", status)
    monkeypatch.setattr(nodes, "pyramidize_on_node", start)
    return script


def test_registration_waits_for_the_node_and_shows_its_steps(fake_node, monkeypatch):
    from plexora.server.models import import_sample, layer_jobs

    fake_node["states"] = [
        {"state": "ready", "pyramid": {"levels": 4}},
        {"state": "preparing", "progress": {"stage": "opening", "percent": 0, "label": "Opening the source image (0%)"}},
        {"state": "preparing", "progress": {"stage": "building", "percent": 40, "label": "Building pyramid levels, level 1 of 4 (40%)"}},
        {"state": "preparing", "progress": {"stage": "writing", "percent": 90, "label": "Finalizing the file (90%)"}},
        {"state": "ready", "pyramid": None},
    ]
    seen = []
    monkeypatch.setattr(layer_jobs, "registration_progress",
                        lambda done, total, label=None, *, stage=None: seen.append(stage))
    import_sample._pyramidize_on_node("o2", "slide", {}, "node://o2/slide")
    assert fake_node["started"] == ["slide"]
    assert seen == ["opening", "building", "writing", "registering"]


def test_import_as_is_asks_the_node_for_nothing(fake_node):
    from plexora.server.models import import_sample

    import_sample._pyramidize_on_node(
        "o2", "slide", {"pyramidize:node://o2/slide": "no"}, "node://o2/slide")
    assert fake_node["started"] == []


def test_an_image_with_no_offer_costs_one_status_request(fake_node):
    from plexora.server.models import import_sample

    fake_node["states"] = [{"state": "ready", "pyramid": None}]
    import_sample._pyramidize_on_node("o2", "slide", {}, "node://o2/slide")
    assert fake_node["started"] == [] and fake_node["states"] == []


def test_a_build_that_fails_on_the_node_is_its_sentence(fake_node):
    from plexora.server.models import import_sample

    fake_node["states"] = [
        {"state": "ready", "pyramid": {"levels": 4}},
        {"state": "error", "error": "The disk holding /n/data ran out of space."},
    ]
    with pytest.raises(ValueError, match="ran out of space"):
        import_sample._pyramidize_on_node("o2", "slide", {}, "node://o2/slide")


def test_the_node_image_row_carries_the_offer(monkeypatch):
    from plexora import nodes
    from plexora.server.models import import_proposal

    monkeypatch.setattr(nodes, "node_resources", lambda node: [{
        "id": "slide", "kind": "image", "state": "ready",
        "pyramid": {"channels": 3, "levels": 5, "output_name": "slide.pyramid.ome.tiff"},
    }])
    layers, questions, _bundle, _warnings = import_proposal._detect_node(
        "node://o2/slide", None, {})
    question = next(q for q in questions if q.id == "pyramidize:node://o2/slide")
    assert question.kind == "confirm" and question.default == "yes"
    assert question.scope == f"layer:{layers[0].id}"
    assert layers[0].render["pyramid"]["location"].startswith("on o2")
