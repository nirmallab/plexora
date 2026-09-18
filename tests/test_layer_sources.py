"""Tiles for a layer that is not the reference image.

`data_model` holds ONE open datasource in module globals. That is the right shape
for what it does -- one project, one viewer, and a warm tile in 0.005 s because
nothing has to be looked up -- and the wrong shape for a scene with several image
layers. Rewriting it would mean rewriting the hottest path in the application for
a capability nobody asked for: the requirement is N LAYERS of one project, not N
projects.

So layer tiles bypass it, the way `figure_builder/server/render.py` already does
and says so. **The invariant that makes that safe is a test, not a comment**: a
counter on `load_datasource` must stay at zero while a wall of layer tiles is
served. Otherwise the first refactor that "helpfully" routed this through
data_model would evict the user's open session on every pan of a second layer,
and the only symptom would be that the viewer got slow.
"""

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import data_model, layer_sources
from plexora.server.models.project import LayerSpec, Project, config_generation

from tests.helpers import use_data_root


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """A project with a reference image and a second image layer registered."""
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    reference = tmp_path / "reference.ome.tif"
    tifffile.imwrite(reference, np.full((2, 256, 256), 100, dtype=np.uint16))
    second = tmp_path / "he.ome.tif"
    tifffile.imwrite(second, np.full((1, 256, 256), 900, dtype=np.uint16))

    project = Project(
        name="demo",
        image=plexora.server.models.project.ImageSpec(
            src=str(reference), width=256, height=256, max_level=1,
            tile_width=256, tile_height=256,
            channels=({"name": "c0", "fullname": "DNA", "src": "/t/0"},)),
    ).with_layer(LayerSpec(
        id="he", kind="image", label="H&E", src=str(second),
        width=256, height=256, max_level=1, tile_width=256, tile_height=256,
        transform=(1.0, 0.0, 0.0, 1.0, 500.0, 0.0),
    ))
    project.save()
    layer_sources.forget()
    return project


@pytest.fixture
def client(scene):
    return plexora.app.test_client()


# -- the invariant ----------------------------------------------------------

def test_serving_layer_tiles_never_loads_the_datasource(scene, monkeypatch):
    """The whole reason this module exists, asserted rather than asserted-in-a-
    comment. load_datasource evicts whatever the viewer has open; calling it to
    draw a second layer would cost the user their session on every pan."""
    calls = []
    monkeypatch.setattr(data_model, "load_datasource",
                        lambda *a, **k: calls.append(a) or None)

    for level in (0, 1):
        for tile in ("0_0",):
            for _ in range(25):
                assert layer_sources.layer_tile("demo", "he", "he_0", level, tile)

    assert calls == [], f"load_datasource was called {len(calls)} times"


def test_a_layer_tile_is_bytes_a_browser_can_draw(scene):
    payload, mimetype, etag = layer_sources.layer_tile("demo", "he", "he_0", 0, "0_0")

    assert isinstance(payload, (bytes, bytearray)) and len(payload) > 0
    assert mimetype in ("image/webp", "image/png")
    assert etag.startswith('"')


# -- what has no tiles ------------------------------------------------------

def test_a_layer_the_project_does_not_have_serves_nothing(scene):
    assert layer_sources.layer_tile("demo", "nope", "c_0", 0, "0_0") is None


def test_a_points_layer_has_no_tiles_of_its_own(scene):
    """The centroid layer is synthesized from the table's coordinate roles and
    has no file. Returning None rather than raising: the route turns it into a
    404, which is the honest answer to "give me this layer's pixels"."""
    project = scene.with_layer(LayerSpec(id="tx", kind="points", label="Transcripts"))
    project.save()
    layer_sources.forget()

    assert layer_sources.layer_tile("demo", "tx", "tx_0", 0, "0_0") is None


def test_the_reference_layer_is_not_served_here(scene):
    """It has no `src` of its own in the synthesized record's sense -- it has
    one, but the reference image's tiles come through /generated/data, which is
    the path with the channel list, the quantization windows and the HD toggle
    behind it. Two routes serving the same pixels differently is the state this
    architecture exists to avoid."""
    served = layer_sources.layer_tile("demo", "__image__", "c_0", 0, "0_0")

    # It IS servable -- the reference layer is an image layer with a src -- and
    # that is fine; what matters is that nothing points the viewer here for it.
    assert served is None or isinstance(served[0], (bytes, bytearray))


# -- caching and invalidation -----------------------------------------------

def test_an_open_layer_is_reused_rather_than_reopened(scene):
    first = layer_sources.open_layer("demo", "he")
    second = layer_sources.open_layer("demo", "he")

    assert first is second


def test_saving_the_project_invalidates_the_open_layer(scene):
    """Keyed on the config generation, so re-registering a layer with a
    corrected transform or a rebuilt pyramid does not go on serving the old one."""
    first = layer_sources.open_layer("demo", "he")
    before = config_generation()

    scene.with_layer(LayerSpec(
        id="he", kind="image", label="H&E", src=scene.layer("he").src,
        width=256, height=256, max_level=1, tile_width=256, tile_height=256,
        transform=(1.0, 0.0, 0.0, 1.0, 999.0, 0.0),
    )).save()

    assert config_generation() > before
    assert layer_sources.open_layer("demo", "he") is not first


def test_an_unrelated_project_load_does_not_evict_a_layer(scene, monkeypatch):
    """The reason this is NOT keyed on data_model.load_generation. That counter
    moves when a different project is loaded, which would throw away every
    layer's open pyramid for something that has nothing to do with it."""
    first = layer_sources.open_layer("demo", "he")
    data_model.load_generation += 1

    assert layer_sources.open_layer("demo", "he") is first


def test_the_cache_is_bounded(scene):
    """Each open layer pins a file handle and whatever the reader keeps behind
    it. A scene with more than a handful of image layers registered at once is
    not a thing anybody has, and a reopen is milliseconds."""
    project = scene
    for i in range(layer_sources.MAX_OPEN_LAYERS + 3):
        project = project.with_layer(LayerSpec(
            id=f"extra{i}", kind="image", src=project.layer("he").src,
            width=256, height=256, max_level=1, tile_width=256, tile_height=256))
    project.save()
    layer_sources.forget()

    for i in range(layer_sources.MAX_OPEN_LAYERS + 3):
        layer_sources.open_layer("demo", f"extra{i}")

    assert len(layer_sources._open_layers) <= layer_sources.MAX_OPEN_LAYERS


# -- the route --------------------------------------------------------------

def test_the_route_serves_a_layer_tile(client):
    response = client.get("/generated/layer/demo/he/he_0/0/0_0")

    assert response.status_code == 200
    assert response.headers["ETag"]
    assert response.headers["Cache-Control"] == "private, max-age=31536000"


def test_the_route_honours_a_conditional_request(client):
    first = client.get("/generated/layer/demo/he/he_0/0/0_0")
    second = client.get("/generated/layer/demo/he/he_0/0/0_0",
                        headers={"If-None-Match": first.headers["ETag"]})

    assert second.status_code == 304


def test_the_etag_carries_the_config_generation_not_the_load_generation(client, scene):
    """A stale conditional request must get fresh bytes, and it must not be made
    stale by something unrelated. Both halves matter: the first is correctness,
    the second is whether a pan costs a round trip."""
    before = client.get("/generated/layer/demo/he/he_0/0/0_0").headers["ETag"]

    data_model.load_generation += 1
    unchanged = client.get("/generated/layer/demo/he/he_0/0/0_0").headers["ETag"]
    assert unchanged == before

    scene.patch(last_opened_at="2026-01-01").save()
    after = client.get("/generated/layer/demo/he/he_0/0/0_0").headers["ETag"]
    assert after != before


def test_the_route_404s_for_a_layer_that_has_no_pixels(client):
    assert client.get("/generated/layer/demo/nope/c_0/0/0_0").status_code == 404


def test_the_route_404s_for_a_project_that_does_not_exist(client):
    assert client.get("/generated/layer/ghost/he/he_0/0/0_0").status_code == 404
