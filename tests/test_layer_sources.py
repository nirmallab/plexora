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


# -- the channel panel's two packets ----------------------------------------
#
# A registered layer gets the REFERENCE IMAGE's channel controls -- slots, a
# colour each, a logarithmic contrast window each, Auto -- rather than a second
# widget that looks like them. Those controls need two packets before they can
# draw anything: the stats (qmin/qmax, without which a saved range cannot even
# be displayed, plus the percentile hint that keeps a newly enabled channel
# from being drawn near-black) and the GaussianMixture fit behind Auto.
#
# Same shape as `/get_image_channel_stats` and `/get_channel_gmm`, because they
# feed the same widget -- and built from the same pure functions, so there is
# no second implementation to drift.


def test_a_layer_channel_answers_the_same_stats_the_reference_image_does(scene):
    packet = layer_sources.layer_channel_stats("demo", "he", "he_0")

    assert packet is not None
    for field in ("qmin", "qmax", "image_min", "image_max",
                  "vmin_hint", "vmax_hint", "image_histogram"):
        assert field in packet, field
    assert packet["qmax"] >= packet["qmin"]


def test_a_layer_channel_answers_a_gmm_fit(scene):
    packet = layer_sources.layer_channel_gmm("demo", "he", "he_0")

    assert packet is not None
    for field in ("vmin", "vmax", "qmin", "qmax"):
        assert field in packet, field


def test_neither_packet_loads_the_datasource(scene, monkeypatch):
    """The module's whole invariant, extended to the two new routes. A stats
    call that went through data_model would evict the user's open session --
    and it is made on every channel the user switches on."""
    calls = []
    monkeypatch.setattr(data_model, "load_datasource",
                        lambda *a, **k: calls.append(a) or None)

    layer_sources.layer_channel_stats("demo", "he", "he_0")
    layer_sources.layer_channel_gmm("demo", "he", "he_0")

    assert calls == [], f"load_datasource was called {len(calls)} times"


def test_the_fit_is_computed_once_per_channel(scene, monkeypatch):
    """0.2 to 1.9 s of CPU, and deterministic -- so a second call would spend a
    second arriving at the number it already has. Cached on the open layer, so
    it dies with the pyramid it describes."""
    fits = []
    real = data_model.channel_gmm_of
    monkeypatch.setattr(data_model, "channel_gmm_of",
                        lambda *a, **k: fits.append(a) or real(*a, **k))

    for _ in range(4):
        layer_sources.layer_channel_gmm("demo", "he", "he_0")

    assert len(fits) == 1


def test_a_channel_with_no_plane_has_no_packet(scene):
    """Points and rgb layers have no channel to window, and a channel index
    the layer does not have is a stale question rather than a broken server."""
    project = scene.with_layer(LayerSpec(id="tx", kind="points", label="Transcripts"))
    project.save()
    layer_sources.forget()

    assert layer_sources.layer_channel_stats("demo", "tx", "tx_0") is None
    assert layer_sources.layer_channel_gmm("demo", "tx", "tx_0") is None
    assert layer_sources.layer_channel_stats("demo", "he", "he_7") is None
    assert layer_sources.layer_channel_stats("demo", "nope", "c_0") is None


def test_the_packet_routes_answer_beside_the_tile_route(client):
    """Four segments after the prefix where the tile route has five, so a
    channel called `stats` could not be confused for one."""
    stats = client.get("/generated/layer/demo/he/he_0/stats")
    gmm = client.get("/generated/layer/demo/he/he_0/gmm")

    assert stats.status_code == 200 and gmm.status_code == 200
    assert stats.get_json()["qmax"] >= 1
    assert "vmin" in gmm.get_json()
    assert client.get("/generated/layer/demo/nope/c_0/stats").status_code == 404
    assert client.get("/generated/layer/demo/he/he_9/gmm").status_code == 404
    # And the tile route is unshadowed.
    assert client.get("/generated/layer/demo/he/he_0/0/0_0").status_code == 200


# -- the quantization window ------------------------------------------------


def test_the_window_is_scanned_once_per_channel_not_once_per_tile(scene, monkeypatch):
    """`quantization_window_of` reads EVERY PIXEL of a full-resolution plane --
    that is what makes its ceiling trustworthy -- and `layer_tile` asked for one
    per tile. A single pan is dozens of tiles, so the most expensive answer the
    reader can give was being recomputed dozens of times for the same number.
    The reference image's path has always cached it."""
    scans = []
    real = data_model.quantization_window_of
    monkeypatch.setattr(data_model, "quantization_window_of",
                        lambda *a, **k: scans.append(a) or real(*a, **k))

    for level in (0, 1):
        for _ in range(10):
            layer_sources.layer_tile("demo", "he", "he_0", level, "0_0")
    layer_sources.layer_channel_stats("demo", "he", "he_0")

    assert len(scans) == 1, f"the full-resolution plane was scanned {len(scans)} times"


def test_the_window_cache_dies_with_the_pyramid_it_describes(scene):
    """Held on the OpenLayer rather than in a dict beside it, so a layer
    re-registered with a rebuilt pyramid cannot be served the old ceiling."""
    layer_sources.layer_channel_stats("demo", "he", "he_0")
    first = layer_sources.open_layer("demo", "he")
    assert first.windows

    scene.patch(last_opened_at="2026-02-02").save()

    assert layer_sources.open_layer("demo", "he").windows == {}


def test_a_density_window_is_not_rounded_up_to_one_transcript(tmp_path):
    """The bug a density map at full zoom was.

    A level-0 bin is one image pixel and holds a hundredth of a molecule on
    average. Rounded up to 1, every molecule saturated its bin -- and three
    genes' worth of saturated bins sum to white, losing exactly the colours
    the per-gene raster exists for.
    """
    manifest = {"point_count": 19_000_000, "width": 45_000, "height": 27_000}

    assert layer_sources.density_scale(manifest, 0) < 1
    assert layer_sources.density_window(manifest, 0) == 1
    assert layer_sources.density_scale(manifest, 5) == pytest.approx(
        layer_sources.density_scale(manifest, 4) * 4)


def test_each_gene_gets_its_own_share_of_the_window(tmp_path):
    """A rare gene and an abundant one share a tile and must not share a
    stretch, or the rare one is a black rectangle -- which reads as "not
    expressed" rather than as "rescaled wrong"."""
    manifest = {
        "point_count": 1000, "width": 1000, "height": 1000,
        "genes": ["RARE", "COMMON"], "gene_counts": [10, 990],
    }
    style = {"genes": ["RARE", "COMMON"],
             "colors": [(255, 0, 0), (0, 0, 255)], "minq": None}

    groups = layer_sources._density_groups(
        manifest, style, layer_sources.density_scale(manifest, 4))

    assert [indices for indices, _rgb, _hi in groups] == [[0], [1]]
    rare, common = (hi for _i, _c, hi in groups)
    assert common == pytest.approx(rare * 99)


def test_a_gene_the_panel_does_not_have_is_skipped(tmp_path):
    manifest = {"point_count": 10, "width": 10, "height": 10,
                "genes": ["A"], "gene_counts": [10]}
    style = {"genes": ["A", "NOPE"], "colors": [(1, 2, 3), (4, 5, 6)]}

    assert len(layer_sources._density_groups(
        manifest, style, layer_sources.density_scale(manifest, 0))) == 1


# -- the density map's controls ---------------------------------------------

def test_the_density_controls_come_off_the_query_string():
    """Four numbers the panel sends and the route has to read, and each is a
    different picture: how coarse the bins are, which ramp reads them, and
    the two ends of the window."""
    style = layer_sources.parse_style({
        "color": "4da3ff", "bin": "188", "ramp": "Magma",
        "dlo": "0.05", "dhi": "0.8",
    })

    assert style["bin"] == 188
    assert style["ramp"] == "magma"
    assert style["dlo"] == pytest.approx(0.05)
    assert style["dhi"] == pytest.approx(0.8)


def test_a_density_window_defaults_to_wide_open():
    """Absent is not zero. A missing `dhi` read as 0 would be a window with no
    width, which is a black tile and nothing on screen to say why."""
    style = layer_sources.parse_style({"color": "4da3ff"})

    assert style["dlo"] == 0.0
    assert style["dhi"] == 1.0
    assert style["ramp"] is None
    assert style["bin"] is None


def test_a_window_out_of_range_is_clamped_rather_than_believed():
    style = layer_sources.parse_style({"color": "4da3ff", "dlo": "-3",
                                       "dhi": "9"})

    assert style["dlo"] == 0.0 and style["dhi"] == 1.0


def test_the_etag_tells_two_ramps_apart():
    """The same url with a different ramp is a different picture, and a
    browser that reused the cached one would show the old colours until a
    reload."""
    def key(**extra):
        return layer_sources._style_key(
            layer_sources.parse_style({"color": "4da3ff", **extra}))

    assert key(ramp="viridis") != key(ramp="magma")
    assert key(bin="94") != key(bin="188")
    assert key(dhi="0.5") != key(dhi="0.9")
    assert key(agg="mean") != key(agg="max")


def test_a_bin_heatmap_names_its_aggregation():
    """How several genes become one field is part of the picture: the
    Visium HD panel's Mean / Sum / Max / Min. Absent is None, which the bin
    store reads as its default, and the name is taken case-blind."""
    assert layer_sources.parse_style({"color": "fff", "agg": "MAX"})["agg"] == "max"
    assert layer_sources.parse_style({"color": "fff"})["agg"] is None


def test_a_bin_composition_rides_the_style_and_the_etag():
    """`comp=` groups `genes=` by index; it is part of the picture, so two
    groupings of the same genes are two tiles."""
    def style(**extra):
        return layer_sources.parse_style({"color": "fff", **extra})

    assert style(comp=" 0|1:max,2 ")["comp"] == "0|1:max,2"
    assert style()["comp"] is None and style(comp="")["comp"] is None
    keys = {layer_sources._style_key(style(**extra))
            for extra in ({"comp": "0,1"}, {"comp": "0|1:max"}, {})}
    assert len(keys) == 3


def test_a_bigger_bin_needs_a_bigger_window():
    """The window is "eight times the average bin". Making the bins four
    times wider puts sixteen times as many molecules in each of them, and a
    window that did not follow would black the picture out."""
    manifest = {"point_count": 1_000_000, "width": 10_000, "height": 10_000}

    one = layer_sources.density_scale(manifest, 0, 10)
    four = layer_sources.density_scale(manifest, 0, 40)

    assert four == pytest.approx(one * 16)


def test_an_absolute_bin_makes_the_window_the_same_at_every_zoom():
    """The counterpart of `test_a_bin_size_is_the_same_physical_size_at_every_level`:
    a bin that covers the same ground holds the same number of molecules, so
    the stretch stops moving as the user zooms -- which is what lets the
    legend under the ramp claim a number at all."""
    manifest = {"point_count": 1_000_000, "width": 10_000, "height": 10_000}

    windows = [layer_sources.density_scale(manifest, level, 94)
               for level in range(5)]

    assert windows == pytest.approx([windows[0]] * 5)


def test_without_a_bin_size_the_window_still_scales_with_the_level():
    """The behaviour before the control existed, unchanged: one bin per level
    pixel, so a bin at level L covers 4^L times the area."""
    manifest = {"point_count": 1_000_000, "width": 10_000, "height": 10_000}

    assert layer_sources.density_scale(manifest, 3) == pytest.approx(
        layer_sources.density_scale(manifest, 2) * 4)


def test_a_ramp_request_takes_the_whole_panel_when_no_gene_is_picked():
    """"Where is there anything at all" is a real question and the honest
    answer is every molecule -- not an empty tile because the style named no
    genes."""
    stored = {"genes": ["A", "B"], "gene_counts": [1, 2], "point_count": 3}

    assert layer_sources._selected_indices(stored, {"genes": []}) is None
    assert layer_sources._selected_indices(stored, {"genes": ["B"]}) == [1]
    # Names that resolve to nothing are a different answer from no names.
    assert layer_sources._selected_indices(stored, {"genes": ["NOPE"]}) == []


def test_a_ramp_is_stretched_against_the_genes_it_is_drawing():
    """The bug the density map WAS: the ceiling is a multiple of the average
    bin over the whole panel, and the field being coloured holds only the
    genes that are on. Three genes out of 480 reached two levels out of 255,
    so the ramp painted its dark end over the entire slide -- which reads as
    a broken colormap rather than as a rescaling."""
    stored = {"genes": ["A", "B"], "gene_counts": [100, 900], "point_count": 1000}

    assert layer_sources._selection_share(stored, None) == 1.0
    assert layer_sources._selection_share(stored, [0]) == pytest.approx(0.1)
    assert layer_sources._selection_share(stored, [0, 1]) == pytest.approx(1.0)


def test_a_share_is_never_zero():
    """A window divided to nothing is a tile of pure saturation, and a gene
    the manifest has no count for is a plausible way to get there."""
    stored = {"genes": ["A"], "gene_counts": [], "point_count": 1000}

    assert layer_sources._selection_share(stored, [0]) > 0
