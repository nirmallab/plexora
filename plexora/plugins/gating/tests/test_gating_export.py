"""The gates download: one file, a column saying what was thresholded, and one
saying who set it.

`thresholded` reads TRUE for a gated marker and is empty otherwise (a column
of FALSEs reads as data in a spreadsheet); `gated_by` says Plexora AI or User
for each gated marker.

The column used to be `gate_active`, which was true
for at most one marker -- the panel resets `selections` on every marker switch,
so a user who gated twenty markers and then clicked to a twenty-first
downloaded a file claiming one gate. What the file has to answer is "was this
marker ever thresholded", which is a comparison against each column's own full
range: the same test the marker dropdown's green dot and the save-to-AnnData
export already use.

And there used to be a second download behind the same icon -- the whole cell
table with each gated marker rewritten to 1/0 or to a kept-or-zeroed intensity,
chosen from an encoding picker. That is gone, so the route takes four form
fields and the test at the bottom sends exactly the four the client sends.
"""

import csv as csv_module
import io
import json

import numpy as np
import polars as pl
import pytest
import tifffile

import plexora
from plexora.server import plugins as plugin_registry
from plexora.server.models import data_model
from plexora.plugins.gating.server import model as gating_model
from tests.helpers import use_data_root

#: data_model keeps the loaded datasource in module globals; a route test that
#: loads a project would otherwise leave them pointing at its own tmp config
#: for whatever runs next. Same list, and the same reason, as the ROI routes.
_DATA_MODEL_GLOBALS = ("ball_tree", "source", "config", "seg", "zarray",
                       "channels", "metadata", "_loaded_source", "datasource")


@pytest.fixture
def project(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    image_path = tmp_path / "image.tif"
    csv_path = tmp_path / "cells.csv"

    tifffile.imwrite(image_path, np.zeros((2, 256, 256), dtype=np.uint8))
    pl.DataFrame(
        {
            "CellID": np.arange(8, dtype=np.uint32),
            "X_centroid": np.linspace(10, 200, 8, dtype=np.float32),
            "Y_centroid": np.linspace(10, 200, 8, dtype=np.float32),
            "MarkerA": np.linspace(0, 7, 8, dtype=np.float32),
            "MarkerB": np.linspace(0, 7, 8, dtype=np.float32),
            "MarkerC": np.linspace(0, 7, 8, dtype=np.float32),
        }
    ).write_csv(csv_path)

    use_data_root(monkeypatch, data_dir)
    for name in _DATA_MODEL_GLOBALS:
        if hasattr(data_model, name):
            monkeypatch.setattr(data_model, name, None)

    from plexora import datasource as datasource_module

    datasource_module.register_datasource(
        name="gate_export_sample",
        image=image_path,
        features=csv_path,
        x="X_centroid",
        y="Y_centroid",
        segmentation=None,
        data_dir=data_dir,
    )
    return "gate_export_sample"


def _by_channel(frame):
    return {row["channel"]: row for row in frame.iter_rows(named=True)}


def test_the_columns_are_thresholded_then_who_gated(project):
    frame = gating_model.download_gates(project, {}, {"MarkerA": [0.0, 7.0]})
    assert frame.columns == ["channel", "gate_start", "gate_end", "thresholded", "gated_by"]


def test_every_narrowed_marker_reads_as_thresholded(project):
    # The shape the panel actually sends: every marker the user has touched in
    # gating_channels, but only the one on screen in selections. The other two
    # narrowed markers used to come out with gate_active False.
    channels = {
        "MarkerA": [2.0, 5.0],
        "MarkerB": [0.0, 7.0],
        "MarkerC": [1.0, 7.0],
    }
    rows = _by_channel(gating_model.download_gates(project, {"MarkerC": [1.0, 7.0]}, channels))

    assert rows["MarkerA"]["thresholded"] == "TRUE"
    assert rows["MarkerC"]["thresholded"] == "TRUE"
    # Still at its own full data range -- browsed to, never narrowed: nothing
    # in either column, not FALSE.
    assert rows["MarkerB"]["thresholded"] is None
    assert rows["MarkerB"]["gated_by"] is None


def test_the_live_selection_still_wins_over_the_stored_range(project):
    rows = _by_channel(gating_model.download_gates(
        project, {"MarkerA": [3.0, 4.0]}, {"MarkerA": [0.0, 7.0]}))

    assert rows["MarkerA"]["gate_start"] == pytest.approx(3.0)
    assert rows["MarkerA"]["gate_end"] == pytest.approx(4.0)
    assert rows["MarkerA"]["thresholded"] == "TRUE"


def test_a_marker_the_description_does_not_know_reads_as_thresholded(project):
    rows = _by_channel(gating_model.download_gates(project, {}, {"Ghost": [1.0, 2.0]}))

    assert rows["Ghost"]["thresholded"] == "TRUE"


def test_who_gated_is_plexora_ai_until_somebody_moves_the_gate(project):
    from plexora.plugins.gating.server import provenance

    # An agent's session wrote A and B; C was set by hand; A was moved since.
    provenance.record(project, "MarkerA", status="accepted", method="ai_panel",
                      written_low=2.0, written_high=7.0, session_id="gs_1")
    provenance.record(project, "MarkerB", status="accepted", method="gmm",
                      written_low=3.0, written_high=7.0, session_id="gs_1")
    provenance.record(project, "MarkerC", status="accepted", method="manual",
                      written_low=1.0, written_high=7.0)
    channels = {"MarkerA": [2.5, 7.0], "MarkerB": [3.0, 7.0], "MarkerC": [1.0, 7.0]}
    rows = _by_channel(gating_model.download_gates(project, {}, channels))

    assert rows["MarkerB"]["gated_by"] == "Plexora AI"
    assert rows["MarkerA"]["gated_by"] == "User"
    assert rows["MarkerC"]["gated_by"] == "User"
    # No record at all: a gate set on the slider.
    rows = _by_channel(gating_model.download_gates(project, {}, {"Ghost": [1.0, 2.0]}))
    assert rows["Ghost"]["gated_by"] == "User"


@pytest.fixture
def client(project):
    if plugin_registry.find(plexora.app, "gating") is None:  # pragma: no cover
        pytest.skip("gating is not installed")
    return project, plexora.app.test_client()


def test_the_route_serves_the_file_from_the_four_fields_the_client_sends(client):
    """The whole request, as gatingApi.downloadGatingCSV now builds it.

    Four fields, not seven: `fullCsv`, `encoding` and `selection_ids` went with
    the per-cell export, and the route reads its form by subscript -- so a
    field left behind on either side is a KeyError and a 500, not a quiet
    fallback. This is the test that would catch the two halves disagreeing.
    """
    name, app_client = client
    response = app_client.post("/plugins/gating/download_gating_csv", data={
        "datasource": name,
        "filename": f"{name}_gated_channel_ranges",
        "filter": json.dumps({"MarkerA": [2.0, 5.0]}),
        "channels": json.dumps({"MarkerA": [2.0, 5.0], "MarkerB": [0.0, 7.0]}),
    })

    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == (
        f"attachment; filename={name}_gated_channel_ranges.csv")

    rows = list(csv_module.DictReader(io.StringIO(response.get_data(as_text=True))))
    assert list(rows[0]) == ["channel", "gate_start", "gate_end", "thresholded", "gated_by"]
    assert [row["channel"] for row in rows] == ["MarkerA", "MarkerB"]
    assert (rows[0]["thresholded"], rows[0]["gated_by"]) == ("TRUE", "User")
    assert (rows[1]["thresholded"], rows[1]["gated_by"]) == ("", "")


def test_a_downloaded_file_reads_back_as_the_panel_reads_an_upload(client):
    # The panel's upload reads the file with the same route the test reads it
    # through: TRUE is a gate, an empty cell is a marker left ungated.
    name, app_client = client
    response = app_client.post("/plugins/gating/download_gating_csv", data={
        "datasource": name, "filename": "g", "filter": json.dumps({}),
        "channels": json.dumps({"MarkerA": [2.0, 5.0], "MarkerB": [0.0, 7.0]})})
    upload = app_client.post("/plugins/gating/upload_gates", data={
        "datasource": name, "file": (io.BytesIO(response.get_data()), "g.csv")},
        content_type="multipart/form-data")
    assert upload.status_code == 200
    rows = {row["channel"]: row for row in app_client.get(
        f"/plugins/gating/get_uploaded_gating_csv_values?datasource={name}").get_json()}
    assert rows["MarkerA"]["thresholded"] is True
    assert not rows["MarkerB"]["thresholded"]
