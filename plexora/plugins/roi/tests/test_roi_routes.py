"""The HTTP surface, including the parts the client acts on differently.

The status codes are the contract here, not decoration:

    400  the request is wrong; retrying it unchanged fails the same way.
    409  the request was fine but somebody else saved first, and the caller has
         work worth keeping -- so the client asks rather than discarding.
    422  the image these annotations were drawn on is not the one loaded.

A route that answered 400 to all three would be describing every failure as the
client's fault, and the client would have no way to tell a conflict worth
prompting about from a malformed polygon.
"""

import json

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.server import plugins as plugin_registry
from plexora.server.models import data_model, database_model

#: The datasource data_model keeps in module globals. Every one of these has to
#: go through monkeypatch so pytest unwinds it: a test that loads a project
#: leaves `config` pointing at its own tmp config.json, and the next test to
#: read `data_model.config` for a project of its own gets the previous test's
#: dict. That is not hypothetical -- it is what made test_segmentation_mapping
#: fail only when the plugin suites ran before it.
_DATA_MODEL_GLOBALS = ("ball_tree", "source", "config", "seg", "zarray",
                       "channels", "metadata", "_loaded_source", "datasource")


def isolate_data_model(monkeypatch):
    """Take ownership of data_model's globals for the duration of one test."""
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)


TRIANGLE = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 0]]]}
#: Big enough to strictly contain the first two of the fixture's cells. The
#: TRIANGLE above has a vertex exactly on cell 0, and containment is strict --
#: a point on the boundary is not inside -- so it maps nothing.
QUADRANT = {"type": "Polygon", "coordinates": [
    [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]]}
IMAGE_WIDTH = IMAGE_HEIGHT = 256


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.json"
    image_path = tmp_path / "image.tif"
    csv_path = tmp_path / "cells.csv"

    tifffile.imwrite(image_path, np.zeros((2, IMAGE_HEIGHT, IMAGE_WIDTH), dtype=np.uint8))
    pl.DataFrame({
        "CellID": np.arange(4, dtype=np.uint32),
        "X_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "Y_centroid": np.linspace(10, 200, 4, dtype=np.float32),
        "MarkerA": np.linspace(0, 3, 4, dtype=np.float32),
    }).write_csv(csv_path)

    for module in (plexora, data_model, database_model):
        monkeypatch.setattr(module, "data_path", data_dir, raising=False)
        monkeypatch.setattr(module, "config_json_path", config_path, raising=False)
    isolate_data_model(monkeypatch)

    if plugin_registry.find(plexora.app, "roi") is None:  # pragma: no cover
        pytest.skip("roi is not installed")

    from plexora import datasource as datasource_module

    datasource_module.register_datasource(
        name="proj", image=image_path, features=csv_path,
        x="X_centroid", y="Y_centroid", segmentation=None, data_dir=data_dir,
    )
    return plexora.app.test_client()


def post(client, path, **body):
    return client.post(f"/plugins/roi/api/{path}", json={"datasource": "proj", **body})


def create(roi_id="r-1", geometry=None, name=None):
    """One region and the category it goes in, as a single operation.

    A project starts with no categories, so the category has to travel with the
    shape. bulk_create leaves an existing one alone, which keeps this callable
    more than once without the revision arithmetic below changing.
    """
    feature = {"id": roi_id, "category_id": "c-1", "geometry": geometry or TRIANGLE}
    if name is not None:
        feature["name"] = name
    return {"op": "roi.bulk_create", "image": "default",
            "categories": [{"id": "c-1", "label": "Tumor"}],
            "features": [feature]}


# -- state --------------------------------------------------------------

def test_state_gives_the_client_everything_it_draws_from(client):
    payload = client.get("/plugins/roi/api/state?datasource=proj").get_json()
    assert payload["success"] is True
    assert payload["revision"] == 0
    assert payload["features"] == []
    assert payload["image_size"] == [IMAGE_WIDTH, IMAGE_HEIGHT]
    assert payload["dimension_mismatch"] is False
    assert payload["categories"] == []


def test_an_unknown_datasource_is_a_bad_request_not_a_crash(client):
    """Stale bookmarks and a datasource deleted in another tab both land here."""
    assert client.get("/plugins/roi/api/state?datasource=nope").status_code == 400
    assert client.get("/plugins/roi/api/state").status_code == 400


# -- operations ---------------------------------------------------------

def test_an_accepted_batch_returns_the_new_revision(client):
    payload = post(client, "operations", base_revision=0, operations=[create()]).get_json()
    assert payload == {"success": True, "revision": 1}


def test_a_stale_write_is_409_with_the_revision_to_catch_up_to(client):
    post(client, "operations", base_revision=0, operations=[create("r-1")])
    response = post(client, "operations", base_revision=0, operations=[create("r-2")])

    assert response.status_code == 409
    assert response.get_json() == {"success": False, "error": "stale_revision", "revision": 1}


def test_an_invalid_operation_is_400_and_stores_nothing(client):
    response = post(client, "operations", base_revision=0, operations=[
        {"op": "roi.bulk_create", "image": "default",
         "categories": [{"id": "c-1", "label": "Tumor"}],
         "features": [{"id": "r-1", "category_id": "c-1",
                       "geometry": {"type": "Point", "coordinates": [1, 2]}}]}])

    assert response.status_code == 400
    assert "unsupported geometry type" in response.get_json()["error"]
    assert client.get("/plugins/roi/api/state?datasource=proj").get_json()["revision"] == 0


@pytest.mark.parametrize("body", [
    {"base_revision": 0},                                    # no operations
    {"base_revision": 0, "operations": "everything"},         # not a list
    {"operations": []},                                       # no base revision
])
def test_a_malformed_body_is_refused(client, body):
    assert post(client, "operations", **body).status_code == 400


def test_a_body_that_is_not_json_at_all_is_refused(client):
    response = client.post("/plugins/roi/api/operations", data="not json",
                           content_type="application/json")
    assert response.status_code == 400


# -- export -------------------------------------------------------------

def test_export_is_a_downloadable_geojson_document(client):
    post(client, "operations", base_revision=0, operations=[create()])
    response = client.get("/plugins/roi/api/export.geojson?datasource=proj")

    assert response.status_code == 200
    assert response.mimetype == "application/geo+json"
    assert "proj_rois.geojson" in response.headers["Content-disposition"]

    document = json.loads(response.get_data(as_text=True))
    assert document["type"] == "FeatureCollection"
    assert len(document["features"]) == 1
    assert document["plexora"]["coordinate_space"]["width"] == IMAGE_WIDTH


def test_export_works_on_a_project_with_no_annotations(client):
    """Export is the escape hatch; a route that refused the empty case would
    also refuse at the moments it is most needed."""
    document = json.loads(
        client.get("/plugins/roi/api/export.geojson?datasource=proj").get_data(as_text=True))
    assert document["features"] == []


# -- import -------------------------------------------------------------

def test_a_file_plexora_did_not_write_is_refused(client):
    """A bare GeoJSON is geometry in an unknown coordinate space -- pixels of
    some image, microns, or degrees. Importing it would mean guessing."""
    response = post(client, "import", base_revision=0, document={
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": TRIANGLE, "properties": {}}]})
    assert response.status_code == 400
    assert "not exported by Plexora" in response.get_json()["error"]


def test_import_round_trips_a_project_s_own_export(client):
    post(client, "operations", base_revision=0, operations=[create()])
    document = json.loads(
        client.get("/plugins/roi/api/export.geojson?datasource=proj").get_data(as_text=True))

    payload = post(client, "import", base_revision=1, document=document).get_json()
    assert payload["success"] is True
    assert payload["imported"] == 1

    state = client.get("/plugins/roi/api/state?datasource=proj").get_json()
    assert len(state["features"]) == 2
    geometries = [f["geometry"]["coordinates"] for f in state["features"]]
    assert geometries[0] == geometries[1]


def test_import_never_overwrites_an_existing_region(client):
    """An id collision is not a conflict to resolve -- the two regions are
    simply different regions numbered the same in two projects, and picking one
    destroys the other. So every import is additive and visibly so."""
    post(client, "operations", base_revision=0, operations=[create("r-1")])
    document = json.loads(
        client.get("/plugins/roi/api/export.geojson?datasource=proj").get_data(as_text=True))
    post(client, "import", base_revision=1, document=document)

    features = client.get("/plugins/roi/api/state?datasource=proj").get_json()["features"]
    assert len(features) == 2
    assert features[0]["id"] != features[1]["id"]
    assert features[1]["source_roi_id"] == "r-1"


def test_geometry_drawn_on_a_different_sized_image_asks_first(client):
    """Default is no. The regions would land somewhere entirely plausible and
    completely wrong, which is the kind of mistake nobody notices."""
    document = {
        "type": "FeatureCollection",
        "plexora": {"schema_version": 1, "categories": [],
                    "coordinate_space": {"width": 9999, "height": 8888}},
        "features": [{"type": "Feature", "id": "r-x", "geometry": TRIANGLE,
                      "properties": {"name": "elsewhere"}}],
    }
    payload = post(client, "import", base_revision=0, document=document).get_json()

    assert payload["success"] is False
    assert payload["warning"] == "dimension_mismatch"
    assert payload["found"] == [9999, 8888]
    assert payload["expected"] == [IMAGE_WIDTH, IMAGE_HEIGHT]
    assert client.get("/plugins/roi/api/state?datasource=proj").get_json()["features"] == []

    # ...and goes ahead when the user says so.
    payload = post(client, "import", base_revision=0, document=document,
                   accept_dimension_mismatch=True).get_json()
    assert payload["success"] is True


def test_import_is_one_revision_and_one_operation(client):
    """So that undoing an import of hundreds of regions is one step."""
    document = {
        "type": "FeatureCollection",
        "plexora": {"schema_version": 1, "categories": [],
                    "coordinate_space": {"width": IMAGE_WIDTH, "height": IMAGE_HEIGHT}},
        "features": [
            {"type": "Feature", "id": f"r-{i}", "geometry": TRIANGLE, "properties": {}}
            for i in range(5)
        ],
    }
    payload = post(client, "import", base_revision=0, document=document).get_json()
    assert payload["revision"] == 1
    assert payload["operation"]["op"] == "roi.bulk_create"
    assert len(payload["operation"]["features"]) == 5


# -- adapters -----------------------------------------------------------

def destination(client, datasource="proj"):
    return client.get(
        f"/plugins/roi/api/adapters/destination?datasource={datasource}").get_json()


def test_a_csv_project_is_offered_no_native_destination(client):
    """A CSV has no way to hold a polygon. The honest companion is the GeoJSON
    sidecar, and the panel shows no button rather than one that errors."""
    assert destination(client) == {
        "success": True, "source_kind": "csv", "kind": None,
        "label": "", "default_name": "", "existing": [], "remembered": "",
        # A CSV has no native destination and still has cells, which is what
        # "Map to cells" is gated on -- the two are separate questions.
        "has_table": True, "image_id": None,
    }


def test_the_destination_route_wants_a_project_that_exists(client):
    response = client.get("/plugins/roi/api/adapters/destination?datasource=nope")
    assert response.status_code == 400
    assert response.get_json()["error"] == "Unknown datasource"


def test_a_project_that_has_never_exported_remembers_nothing(client):
    """Blank rather than the default name: the panel fills in the default for
    whichever kind of file this is, and the store has no business knowing what
    the project was imported from."""
    assert destination(client)["remembered"] == ""


def test_writing_a_csv_project_to_anndata_is_refused(client):
    response = post(client, "adapters/anndata")
    assert response.status_code == 400
    assert "did not come from an AnnData" in response.get_json()["error"]


def test_a_key_that_is_a_path_is_refused(client):
    """Rejected on the name, before the source kind is even reached -- so the
    validation cannot be skipped by a project that has somewhere to write."""
    response = post(client, "adapters/anndata", key="nested/name")
    assert response.status_code == 400


# -- map to cells -------------------------------------------------------
#
# The one route that writes onto the user's rows rather than beside them, so the
# refusals matter more than the success. Two of them come back as `needs`, which
# the client turns into a requirements prompt and retries -- a contract the
# client depends on, and one that a reworded error message must not break.


def test_mapping_writes_two_columns_and_says_how_many_cells_landed(client, tmp_path):
    post(client, "operations", base_revision=0,
         operations=[create(geometry=QUADRANT, name="Tumor 1")])
    payload = post(client, "map_to_cells").get_json()

    assert payload["success"] is True
    assert payload["columns"] == ["rois_category", "rois_name"]
    assert payload["n_rois"] == 1
    assert payload["n_cells"] == 4
    # The fixture's cells run diagonally from (10, 10) to (200, 200), so two of
    # the four are inside the quadrant. The other two were tested and are blank,
    # which is the difference n_assigned reports.
    assert payload["n_assigned"] == 2

    written = pl.read_csv(tmp_path / "cells.csv")
    assert written["rois_category"].to_list() == ["Tumor", "Tumor", "", ""]
    assert written["rois_name"].to_list() == ["Tumor 1", "Tumor 1", "", ""]


def test_the_column_prefix_follows_the_name_the_user_gave(client, tmp_path):
    post(client, "operations", base_revision=0,
         operations=[create(geometry=QUADRANT)])
    payload = post(client, "map_to_cells", name="pass2").get_json()
    assert payload["columns"] == ["pass2_category", "pass2_name"]
    assert "pass2_name" in pl.read_csv(tmp_path / "cells.csv").columns


def test_mapping_nothing_is_refused(client):
    """A project with no regions would write two empty columns onto every cell,
    which is worse than a message saying there is nothing to map."""
    payload = post(client, "map_to_cells").get_json()
    assert payload["success"] is False
    assert "no ROIs" in payload["error"]


def test_an_existing_column_comes_back_with_a_free_name(client):
    post(client, "operations", base_revision=0, operations=[create()])
    post(client, "map_to_cells")

    payload = post(client, "map_to_cells").get_json()
    assert payload["success"] is False
    assert payload["error"] == "column_exists"
    assert payload["suggestion"] == "rois_2"
    assert "rois_category" in payload["columns"]


def test_replace_is_the_users_answer_and_the_route_takes_it(client):
    post(client, "operations", base_revision=0, operations=[create()])
    post(client, "map_to_cells")
    assert post(client, "map_to_cells", replace=True).get_json()["success"] is True


def test_an_unknown_datasource_is_a_bad_request(client):
    response = client.post("/plugins/roi/api/map_to_cells", json={"datasource": "nope"})
    assert response.status_code == 400
