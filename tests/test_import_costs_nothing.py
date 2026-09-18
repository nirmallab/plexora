"""What an ordinary project pays for the layer model. Nothing.

The whole rework is additive, and "additive" is a claim that rots quietly: a
branch added to the tile encoder, a poll started at boot, a key written into
every config.json. None of those would fail a functional test, and all three
would show up as a slower viewer on a project that has no layers at all.

So the guarantees are pinned here, in the terms they were made in:

  the channel tile path never learns the new kinds exist
  a project with nothing pending starts no poll
  a project with no layers writes no new keys and gets no new world items

The frame-time measurements those guarantees exist to protect live in the
browser harness (SKILL.md, "Validating rendering changes"); what is here is the
structural half, which is the half that can be checked in a second and is the
half that actually regresses.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models.project import LayerSpec, Project

from tests.helpers import use_data_root

CLIENT = Path(plexora.__file__).parent / "client"


def source(*parts):
    return (CLIENT.joinpath(*parts)).read_text(encoding="utf-8")


@pytest.fixture
def plain(tmp_path, monkeypatch):
    """A project as they all were: one image, no layers, nothing pending."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    tifffile.imwrite(tmp_path / "slide.ome.tif",
                     np.random.default_rng(0).integers(
                         0, 3000, (2, 128, 128)).astype(np.uint16))
    plexora.datasource.register_image_datasource("plain", tmp_path / "slide.ome.tif")
    return Project.load("plain")


def test_the_channel_tile_path_never_learns_the_new_kinds(plain):
    """`generate_png` is called once per tile per channel per pan. A blank
    frame has its own route and a registered layer has another, so neither
    costs the hot path a comparison for a case it can never be in."""
    from plexora.server.models import data_model
    from plexora.server.routes import data_routes

    hot = [data_routes.generate_png, data_model.encode_tile,
           data_model.read_tile, data_model.encode_tile_array]
    import inspect

    for function in hot:
        body = inspect.getsource(function)
        assert "blank" not in body.lower(), (
            f"{function.__name__} grew a branch for the blank frame")
        assert "spatial_layers" not in body and "layer_sources" not in body, (
            f"{function.__name__} reaches into the layer model")


def test_a_project_with_no_layers_writes_no_new_keys(plain):
    """Every key the rework added is dropped at its default, so a config.json
    written before any of this existed comes back byte for byte."""
    entry = plain.to_entry()
    added = {"imageModality", "bundles", "spatialLayers"}
    assert added.isdisjoint(entry), sorted(added & set(entry))

    # And the round trip is exact, which is the check that catches a default
    # that stopped matching what absence means.
    assert Project.from_entry("plain", entry).to_entry() == entry


def test_config_grows_only_for_a_project_that_has_layers(plain):
    """`/config` reaches the client on every viewer boot, for every project the
    user has. A layer list that grew for all of them would be paid for by all
    of them."""
    client = plexora.app.test_client()
    served = json.loads(client.get("/config").data)["plain"]

    # One computed key, and it describes what this project already drew.
    assert set(served) - set(plain.to_entry()) == {"layers"}
    assert [layer["id"] for layer in served["layers"]] == ["__image__"]
    assert "status" not in served["layers"][0], "ready is written as absence"


def test_nothing_polls_for_a_project_with_nothing_pending(plain):
    """The poll is gated on something actually being unfinished.

    It used to be gated on `segmentation_status === 'pending'` and now covers
    every layer too, which is exactly the kind of widening that turns "one
    request when a mask is converting" into "one request every 1.5 s, forever,
    for everybody".
    """
    main = source("src", "js", "main.js")
    assert "if (watchingLayers || !anythingPending()) return;" in main
    gate = main.split("function anythingPending()")[1].split("}")[0]
    assert "config.segmentation_status === 'pending'" in gate
    assert "layer.status === 'pending'" in gate
    # And it stops as soon as nothing is outstanding, rather than running the
    # silent counter out.
    assert "if (!document_.pending) {" in main


def test_a_layer_still_building_is_not_drawn(plain):
    """So the viewer never asks for tiles that do not exist yet -- which would
    be a wall of 404s and a rectangle of nothing."""
    manager = source("src", "js", "views", "viewerManager.js")
    assert 'if (spec.status && spec.status !== "ready") continue;' in manager


def test_a_hidden_layer_is_removed_rather_than_faded(plain):
    """The whole performance argument for registered layers. An OSD TiledImage
    at opacity 0 still requests, decodes and draws every tile in view."""
    manager = source("src", "js", "views", "viewerManager.js")
    visible = manager.split("setVisible(visible) {")[1].split("},")[0]
    assert "drop()" in visible and "opacity" not in visible


def test_layer_tiles_do_not_share_the_datasource_cache(plain):
    """`data_model` holds ONE open project in module globals, which is right
    for the reference image and wrong for a scene. Routing layer tiles through
    it would evict the user's session on every pan of a second layer."""
    import ast
    import inspect

    from plexora.server.models import layer_sources

    # The CALLS, not the word: the module docstring explains at length why it
    # does not call this, and a substring check would be satisfied by deleting
    # the explanation.
    tree = ast.parse(inspect.getsource(layer_sources))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)}
    assert "load_datasource" not in called
    assert "ensure_loaded" not in called
    # Its own bounded cache, so a scene cannot pin unbounded file handles.
    assert layer_sources.MAX_OPEN_LAYERS <= 8


def test_a_registered_layer_adds_no_world_item_to_a_project_without_one(plain):
    """The list is empty, so the loop does nothing -- but `syncLayerImages` is
    called unconditionally at boot, so "does nothing" has to be free rather
    than merely correct."""
    assert plain.spatial_layers == ()
    assert [layer.id for layer in plain.all_layers] == ["__image__"]


def test_adding_a_layer_does_not_touch_the_image(plain):
    """A layer is written beside the image, never through it. The channel list,
    the GL pass and every open plugin are keyed on `imageData` indices, and a
    layer that shifted them would be a layer that broke the picture."""
    before = plain.image.to_entry()
    after = plain.with_layer(LayerSpec(id="he", kind="image", src="/x.tif"))
    assert after.image.to_entry() == before
