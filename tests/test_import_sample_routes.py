"""Registering a sample through the routes the dialog posts to.

Three routes and one rule: the record is written from what the FILES say, never
from a proposal handed back by a client. `/import/sample` re-inspects the paths
it is given, which is what makes "what would this be" and "what this is" the
same answer and keeps a stale or tampered proposal from writing anything.

What is asserted here is the order and the failure behaviour, because those are
what a half-written sample is made of: the dataset is validated before a byte is
written, a name collision is refused with something to do about it, and an
import that fails partway leaves nothing behind -- unless it was a re-import, in
which case what was already there is still better than nothing.
"""

import json

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import datasets, layer_jobs
from plexora.server.models.project import Project

from tests.helpers import use_data_root
from tests.spatial_fixtures import write_transcripts_parquet


@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    rng = np.random.default_rng(0)
    tifffile.imwrite(source / "slide.ome.tif",
                     rng.integers(0, 4000, (2, 128, 128)).astype(np.uint16))
    tifffile.imwrite(source / "slide_mask.tif",
                     (np.arange(128 * 128).reshape(128, 128) % 50).astype(np.uint32))
    layer_jobs.forget()
    yield plexora.app.test_client()
    layer_jobs.forget()


def _source(client):
    from plexora import paths

    return paths.data_root() / "source"


def test_one_image_registers_and_says_where_to_open_it(client):
    response = client.post("/import/sample", json={
        "paths": [str(_source(client) / "slide.ome.tif")]})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["name"] == "slide"
    assert payload["redirect"].endswith("/slide")
    assert payload["pending"] is False
    assert Project.load("slide").image.width == 128


def test_the_same_image_again_is_offered_as_the_existing_sample(client):
    """Inspection says so; registering it anyway is the user's call. What the
    dialog does with `existing` is offer "Open it" -- a second copy of one
    slide is almost never what somebody meant, and the two then diverge."""
    path = str(_source(client) / "slide.ome.tif")
    client.post("/import/sample", json={"paths": [path]})
    proposal = client.post("/import/inspect", json={"paths": [path]}).get_json()
    assert proposal["samples"][0]["existing"] == "slide"


def test_re_importing_rewrites_the_existing_sample_rather_than_copying_it(client):
    """"Re-import" on the already-imported banner.

    Detection improves -- which of a Xenium run's three morphology images is
    opened, whether a Z-stack is one channel or fourteen, where the pixel size
    comes from -- and a record written by the old answer is matched by the same
    files, so every later import of them lands back on it. Without a way to
    re-read, the old answer is the only one reachable short of deleting the
    sample.

    `replace` is sent as the name too: a name and a replacement that disagree
    are a request for a second copy under a deduplicated name, which is the
    opposite of what the button means.
    """
    path = str(_source(client) / "slide.ome.tif")
    client.post("/import/sample", json={"paths": [path], "name": "slide"})
    before = set(plexora.get_config_names())

    again = client.post("/import/sample", json={
        "paths": [path], "name": "slide", "replace": "slide"})

    assert again.status_code == 200
    assert again.get_json()["name"] == "slide"
    assert set(plexora.get_config_names()) == before, "no second copy"
    assert Project.load("slide").image.width == 128


def test_a_name_somebody_typed_is_not_quietly_changed(client):
    """409 with a free name, rather than filing their import under `slide_2`.

    A derived name is deduplicated silently -- nobody chose it. A TYPED one is
    an instruction, and two copies of one slide is how a workspace rots.
    """
    path = str(_source(client) / "slide.ome.tif")
    client.post("/import/sample", json={"paths": [path], "name": "melanoma"})

    clash = client.post("/import/sample",
                        json={"paths": [path], "name": "melanoma"})
    assert clash.status_code == 409
    body = clash.get_json()
    assert body["suggestion"] != "melanoma"
    assert "melanoma" in body["error"]


def test_a_dataset_that_has_gone_is_refused_before_anything_is_written(client):
    """The order that matters. A project is filed AFTER it exists, so a dataset
    validated afterwards would leave the user with the thing they asked for in
    a place they did not ask for and an error page about it."""
    before = set(json.loads((plexora.paths.config_path(
        plexora.paths.data_root())).read_text()))
    response = client.post("/import/sample", json={
        "paths": [str(_source(client) / "slide.ome.tif")],
        "dataset": {"id": "gone-forever"},
    })
    assert response.status_code == 400
    after = set(json.loads((plexora.paths.config_path(
        plexora.paths.data_root())).read_text()))
    assert after == before, "nothing may be registered when the filing is refused"


def test_a_new_dataset_is_made_and_the_sample_filed_in_it(client):
    response = client.post("/import/sample", json={
        "paths": [str(_source(client) / "slide.ome.tif")],
        "dataset": {"new": "Melanoma cohort"},
    })
    assert response.status_code == 200
    known = plexora.get_config_names()
    folder = datasets.find_by_name("Melanoma cohort", known=known)
    assert folder is not None
    assert response.get_json()["name"] in folder.projects


def test_a_run_registers_its_layers_and_reports_what_is_still_building(client,
                                                                      tmp_path):
    pytest.importorskip("pyarrow")
    run = _source(client) / "run"
    run.mkdir()
    tifffile.imwrite(run / "morphology.ome.tif",
                     np.random.default_rng(1).integers(
                         0, 4000, (2, 128, 128)).astype(np.uint16))
    write_transcripts_parquet(run / "transcripts.parquet", n=500,
                              width=400, height=400)
    (run / "experiment.xenium").write_text(
        json.dumps({"pixel_size": 0.2125}), encoding="utf-8")

    payload = client.post("/import/sample",
                          json={"paths": [str(run)]}).get_json()
    project = Project.load(payload["name"])
    assert [l.id for l in project.spatial_layers] == ["transcripts"]
    assert project.bundles[0]["format"] == "xenium"

    # Pending, because nothing in this test registered a transcripts builder --
    # which is the honest state of a core build, and is why the import is not
    # refused for it.
    assert payload["pending"] is True
    status = client.get(
        f"/import/status?sample={payload['name']}").get_json()
    assert status["layers"]["transcripts"]["status"] == "pending"
    # And it says WHY it is pending -- a build under way, or (in a core build
    # with no transcripts plugin) the plugin that would prepare it. Either is
    # something to act on; neither is a silent empty layer.
    assert status["layers"]["transcripts"]["message"]


def test_adding_layers_reports_a_reload_only_for_a_mask(client):
    """Because a mask inserts the "Area" placeholder into imageData, and every
    channel index on the open page is wired into the GL pass. Everything else
    is adopted in place, which is what keeps "+ Add Layer" from costing the
    user their pan, zoom, gates and open tools."""
    source = _source(client)
    client.post("/import/sample",
                json={"paths": [str(source / "slide.ome.tif")], "name": "demo"})

    with_mask = client.post("/import/layers", json={
        "sample": "demo", "paths": [str(source / "slide_mask.tif")]}).get_json()
    assert with_mask["reload"] is True

    tifffile.imwrite(source / "second.ome.tif",
                     np.random.default_rng(2).integers(
                         0, 4000, (1, 128, 128)).astype(np.uint16))
    with_layer = client.post("/import/layers", json={
        "sample": "demo", "paths": [str(source / "second.ome.tif")]}).get_json()
    assert with_layer["reload"] is False
    assert [entry["id"] for entry in with_layer["layers"]] == ["second"]

    layer = Project.load("demo").layer("second")
    # A picked image joins an existing scene as a LAYER. Promoting it would
    # re-express every other layer's transform, so it is a separate, explicit
    # act rather than a side effect of adding a file.
    assert layer.kind == "image"
    assert layer.channels[0]["src"] == "/generated/layer/demo/second/second_0/"


def test_adding_the_same_layer_twice_replaces_rather_than_stacks(client):
    """A re-import after a corrected pixel size is a correction, not a second
    run. `with_layer` keeps a layer's position when it already exists."""
    source = _source(client)
    client.post("/import/sample",
                json={"paths": [str(source / "slide.ome.tif")], "name": "demo"})
    tifffile.imwrite(source / "second.ome.tif",
                     np.random.default_rng(2).integers(
                         0, 4000, (1, 128, 128)).astype(np.uint16))

    for _ in range(2):
        client.post("/import/layers", json={
            "sample": "demo", "paths": [str(source / "second.ome.tif")]})
    assert [l.id for l in Project.load("demo").spatial_layers] == ["second"]


def test_a_layer_already_registered_is_not_proposed_again(client):
    """Scoped inspection drops what the sample already has, so "+ Add Layer"
    pointed at the run it came from offers only what is NOT in it."""
    source = _source(client)
    client.post("/import/sample",
                json={"paths": [str(source / "slide.ome.tif")], "name": "demo"})
    scoped = client.post("/import/inspect", json={
        "paths": [str(source / "slide.ome.tif")], "sample": "demo"}).get_json()
    assert scoped["samples"] == [] or scoped["samples"][0]["layers"] == []


def test_nothing_readable_is_a_400_with_a_reason(client, tmp_path):
    (tmp_path / "notes.md").write_text("hello", encoding="utf-8")
    response = client.post("/import/sample",
                           json={"paths": [str(tmp_path / "notes.md")]})
    assert response.status_code == 400
    assert response.get_json()["error"]


def test_a_failed_registration_leaves_nothing_behind(client, monkeypatch):
    """A half-written sample appears in the library, opens onto an error and
    gives the user nothing to act on -- which is worse than no sample."""
    from plexora.server.models import import_sample as importer

    def explode(*args, **kwargs):
        raise RuntimeError("the mask job would not start")

    monkeypatch.setattr(importer, "attach_segmentation", explode, raising=False)
    monkeypatch.setattr(
        "plexora.server.routes.import_routes.attach_segmentation", explode)

    source = _source(client)
    response = client.post("/import/sample", json={
        "paths": [str(source / "slide.ome.tif"), str(source / "slide_mask.tif")],
        "name": "doomed",
    })
    # A 500, deliberately: this is a bug, not bad input, and reporting it as a
    # 400 would tell the user their files were wrong. What must NOT survive it
    # is the half-written project.
    assert response.status_code == 500
    assert Project.find("doomed") is None


# -- which mask wins -------------------------------------------------------

def _mask_candidate(src, **kwargs):
    from plexora.server.models.import_proposal import LayerProposal

    return LayerProposal(id="m", kind="labels", role="mask", src=str(src),
                         **kwargs)


def _boundary_table(path):
    import polars as pl

    pl.DataFrame({
        "cell_id": ["a-1"] * 4,
        "vertex_x": [0.0, 8.0, 8.0, 0.0],
        "vertex_y": [0.0, 0.0, 8.0, 8.0],
        "label_id": [1] * 4,
    }).write_parquet(path)
    return path


def test_a_raster_mask_outranks_a_runs_own_boundary_polygons(tmp_path):
    """Somebody who hands Plexora a mask has segmented this slide themselves.
    Quietly drawing the vendor's outlines over it because they happened to
    import the run folder too would replace their answer with one they did not
    ask for."""
    pytest.importorskip("polars")
    from plexora.server.models.import_sample import _preferred_mask

    boundaries = _mask_candidate(_boundary_table(tmp_path / "cell_boundaries.parquet"))
    raster = _mask_candidate(tmp_path / "theirs.tif")
    layers = [boundaries, raster]

    assert _preferred_mask(layers) is raster
    # The loser is not discarded -- the run still records what it shipped.
    assert boundaries.role == "layer"


def test_boundary_polygons_are_the_mask_when_nothing_else_is(tmp_path):
    pytest.importorskip("polars")
    from plexora.server.models.import_sample import _preferred_mask

    boundaries = _mask_candidate(_boundary_table(tmp_path / "cell_boundaries.parquet"))

    assert _preferred_mask([boundaries]) is boundaries
    assert boundaries.role == "mask"


def test_a_sample_with_no_mask_at_all_picks_none(tmp_path):
    from plexora.server.models.import_sample import _preferred_mask

    assert _preferred_mask([]) is None
