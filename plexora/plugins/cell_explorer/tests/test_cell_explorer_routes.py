"""The HTTP surface, against a really-registered datasource.

The unit tests either side of this drive the encoder and the repository
directly. What they cannot see is whether the three fit together through Flask:
whether the descriptor the panel is handed is the same one the values are
encoded against, whether the binary route sets the headers the client decodes
by, and whether the status codes mean what the client thinks.

Status codes are the contract, not decoration:

    400  the request is wrong; retrying it unchanged fails the same way.
    409  the request was fine but somebody else saved first, and the caller has
         preferences worth keeping -- so the client asks rather than discarding.
    422  the stored document is from a newer Plexora than this one.
"""

import gzip
import json

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.plugins.cell_explorer.server import values as encoder
from plexora.server import plugins as plugin_registry
from plexora.server.models import data_model, database_model

#: The datasource data_model keeps in module globals. Every one has to go
#: through monkeypatch so pytest unwinds it -- a test that loads a project
#: otherwise leaves the next file served its table. See SKILL.md.
_DATA_MODEL_GLOBALS = ("ball_tree", "source", "config", "seg", "zarray",
                       "channels", "metadata", "_loaded_source", "datasource")

N_CELLS = 12


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.json"
    image_path = tmp_path / "image.tif"
    csv_path = tmp_path / "cells.csv"

    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))
    pl.DataFrame({
        "CellID": np.arange(1, N_CELLS + 1, dtype=np.uint32),
        "X_centroid": np.linspace(10, 200, N_CELLS, dtype=np.float32),
        "Y_centroid": np.linspace(10, 200, N_CELLS, dtype=np.float32),
        "MarkerA": np.linspace(0, 1, N_CELLS, dtype=np.float32),
        "phenotype": (["Tumor", "CD8 T", "B cell"] * 4),
        "confidence": np.linspace(0.05, 0.99, N_CELLS, dtype=np.float64),
        "leiden": ([0, 1, 2, 3] * 3),
    }).write_csv(csv_path)

    for module in (plexora, data_model, database_model):
        monkeypatch.setattr(module, "data_path", data_dir, raising=False)
        monkeypatch.setattr(module, "config_json_path", config_path, raising=False)
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)
    monkeypatch.setattr(data_model, "_metadata_column_cache", {})
    monkeypatch.setattr(data_model, "_gmm_cache", {})

    if plugin_registry.find(plexora.app, "cell_explorer") is None:  # pragma: no cover
        pytest.skip("cell_explorer is not installed")

    from plexora import datasource as datasource_module
    from plexora.server.models.project import Project

    datasource_module.register_datasource(
        name="proj", image=image_path, features=csv_path,
        x="X_centroid", y="Y_centroid", segmentation=None, data_dir=data_dir,
    )
    # The split a CSV import confirms on /project/<name>/columns. Recorded here
    # because the predictor cannot draw it from a header alone -- it reads
    # "confidence" as a stain, which is exactly why that screen exists -- and
    # because what this plugin offers IS that recorded answer.
    Project.mutate("proj", lambda project: project.with_columns(
        ["MarkerA"],
        ["CellID", "X_centroid", "Y_centroid", "phenotype", "confidence", "leiden"],
    ))
    data_model.load_datasource("proj", reload=True)
    return plexora.app.test_client()


def variables(client):
    return client.get("/plugins/cell_explorer/api/variables?datasource=proj").get_json()


def descriptor_for(client, name):
    return next(v for v in variables(client)["variables"] if v["name"] == name)


# --------------------------------------------------------------------------
# /variables
# --------------------------------------------------------------------------

def test_variables_lists_the_annotation_columns(client):
    names = [v["name"] for v in variables(client)["variables"]]
    assert "phenotype" in names
    assert "confidence" in names


def test_every_metadata_column_is_offered_including_the_structural_ones(client):
    """Nothing is held back. Colouring by x draws a gradient across the slide,
    which is usually a picture of the coordinate system and occasionally the
    quickest way to see that the coordinates came in flipped. A filter here
    could not be argued with from the panel -- a column it decided against was
    simply absent, with nothing on screen to say why."""
    names = [v["name"] for v in variables(client)["variables"]]
    assert "CellID" in names
    assert "X_centroid" in names and "Y_centroid" in names


def test_the_cell_id_is_offered_but_flagged(client):
    """A name, not a variable: it would draw one colour per cell. Said in the
    descriptor so the dropdown can mark it, and so nothing picks it by
    itself."""
    assert descriptor_for(client, "CellID")["identifier_like"] is True
    assert descriptor_for(client, "phenotype")["identifier_like"] is False


def test_marker_columns_are_not_offered(client):
    """Marker expression is Thresholding's, and a different question: the values
    are log-normal and the scale depends on which matrix was read. This tool
    reads annotations."""
    assert "MarkerA" not in [v["name"] for v in variables(client)["variables"]]


def test_the_panel_learns_what_core_can_draw_cells_with(client):
    """So it can show "there is nothing to draw these on" before fetching a
    single value."""
    payload = variables(client)
    assert payload["can_draw"] == {
        "segmentation": False, "segmentation_pending": False, "centroids": True}


def test_each_kind_is_inferred_and_carries_its_own_payload(client):
    assert descriptor_for(client, "phenotype")["kind"] == "categorical"
    assert descriptor_for(client, "confidence")["kind"] == "continuous"
    assert descriptor_for(client, "confidence")["stats"]["p99"] > 0.9


def test_a_cluster_column_is_categorical_and_offers_the_override(client):
    leiden = descriptor_for(client, "leiden")
    assert leiden["kind"] == "categorical"
    assert leiden["ambiguous"] is True
    assert leiden["stats"], "both halves, so flipping the override needs no refetch"


def test_an_unknown_datasource_is_a_bad_request(client):
    assert client.get(
        "/plugins/cell_explorer/api/variables?datasource=nope").status_code == 400


# --------------------------------------------------------------------------
# /values
# --------------------------------------------------------------------------

def decode(response, dtype):
    raw = response.get_data()
    if response.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return np.frombuffer(raw, dtype=dtype)


def test_values_are_a_packed_buffer_with_the_headers_to_decode_it(client):
    response = client.get(
        "/plugins/cell_explorer/api/values?datasource=proj&column=phenotype")
    assert response.status_code == 200
    assert response.mimetype == "application/octet-stream"
    assert response.headers["X-Value-Kind"] == "categorical"
    assert response.headers["X-Cell-Count"] == str(N_CELLS)


def test_the_codes_match_the_dictionary_the_panel_was_given(client):
    """The one property that ties the two routes together. A second ordering
    computed anywhere else labels every cell as its neighbour."""
    order = [entry["value"] for entry in descriptor_for(client, "phenotype")["categories"]]
    record = decode(
        client.get("/plugins/cell_explorer/api/values?datasource=proj&column=phenotype"),
        encoder.CATEGORICAL_DTYPE)
    assert [order[code] for code in record["code"]] == ["Tumor", "CD8 T", "B cell"] * 4


def test_the_ids_are_the_ones_the_mask_would_carry(client):
    record = decode(
        client.get("/plugins/cell_explorer/api/values?datasource=proj&column=phenotype"),
        encoder.CATEGORICAL_DTYPE)
    assert list(record["id"]) == list(range(1, N_CELLS + 1))


def test_a_continuous_column_arrives_as_float32(client):
    record = decode(
        client.get("/plugins/cell_explorer/api/values?datasource=proj&column=confidence"),
        encoder.CONTINUOUS_DTYPE)
    assert record["value"][0] == pytest.approx(0.05, abs=1e-6)
    assert record["value"][-1] == pytest.approx(0.99, abs=1e-6)


def test_an_ambiguous_column_can_be_asked_for_the_other_way(client):
    """The override is a client-side decision, so the encoding has to follow it
    -- otherwise "treat as continuous" returns category codes."""
    response = client.get(
        "/plugins/cell_explorer/api/values?datasource=proj&column=leiden&kind=continuous")
    assert response.headers["X-Value-Kind"] == "continuous"
    record = decode(response, encoder.CONTINUOUS_DTYPE)
    assert list(record["value"]) == [0, 1, 2, 3] * 3


def test_a_column_that_was_never_ambiguous_cannot_be_re_encoded(client):
    """Not a supported override, and honouring it would ask the encoder for a
    dictionary that was never computed."""
    assert client.get(
        "/plugins/cell_explorer/api/values?datasource=proj"
        "&column=phenotype&kind=continuous").status_code == 400


def test_an_unknown_column_is_a_bad_request_not_an_empty_buffer(client):
    """An empty buffer decodes fine and paints nothing, which looks like a
    column where every cell is missing."""
    response = client.get(
        "/plugins/cell_explorer/api/values?datasource=proj&column=nope")
    assert response.status_code == 400
    assert "nope" in response.get_json()["error"]


def test_a_column_the_panel_never_offered_is_refused(client):
    """The eligible list is the contract. A marker column reachable by URL would
    be a second, quieter way to read expression values through this tool."""
    assert client.get(
        "/plugins/cell_explorer/api/values?datasource=proj&column=MarkerA"
    ).status_code == 400


# --------------------------------------------------------------------------
# /state
# --------------------------------------------------------------------------

def get_state(client):
    return client.get("/plugins/cell_explorer/api/state?datasource=proj").get_json()


def post_state(client, revision, settings):
    return client.post("/plugins/cell_explorer/api/state",
                       json={"datasource": "proj", "revision": revision,
                             "settings": settings})


def test_a_fresh_project_has_nothing_stored(client):
    payload = get_state(client)
    assert payload["success"] is True
    assert payload["revision"] == 0 and payload["selected"] is None


def test_preferences_survive_a_round_trip(client):
    saved = post_state(client, 0, {
        "selected": "phenotype",
        "display": {"mode": "outlines", "opacity": 0.4},
        "categorical": {"phenotype": {"colors": {"Tumor": "#ff0000"},
                                      "hidden": ["B cell"]}},
    }).get_json()
    assert saved["revision"] == 1

    restored = get_state(client)
    assert restored["selected"] == "phenotype"
    assert restored["display"]["opacity"] == 0.4
    assert restored["categorical"]["phenotype"]["colors"] == {"Tumor": "#ff0000"}
    assert restored["categorical"]["phenotype"]["hidden"] == ["B cell"]


def test_a_stale_save_conflicts_rather_than_clobbering(client):
    post_state(client, 0, {"selected": "phenotype"})
    response = post_state(client, 0, {"selected": "confidence"})
    assert response.status_code == 409
    assert response.get_json()["revision"] == 1
    assert get_state(client)["selected"] == "phenotype"


def test_a_conflict_is_not_reported_as_a_bad_request(client):
    """The client acts on these differently: a 400 means the request was wrong
    and retrying is pointless, a 409 means there is work worth keeping on both
    sides and the user should choose."""
    post_state(client, 0, {"selected": "a"})
    assert post_state(client, 0, {"selected": "b"}).status_code == 409
    assert client.post("/plugins/cell_explorer/api/state",
                       json={"datasource": "nope", "revision": 0,
                             "settings": {}}).status_code == 400


def test_a_newer_stored_document_is_refused_rather_than_overwritten(client):
    from plexora import api

    api.store("proj", "cell_explorer").put_state(
        json.dumps({"schema_version": 99}).encode("utf-8"))
    response = client.get("/plugins/cell_explorer/api/state?datasource=proj")
    assert response.status_code == 422
    assert response.get_json()["schema_version"] == 99


def test_a_body_that_is_not_an_object_is_refused(client):
    assert client.post("/plugins/cell_explorer/api/state",
                       json=["nope"]).status_code == 400
