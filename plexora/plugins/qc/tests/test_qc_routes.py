"""The QC panel's routes: the user's own acts in the viewer, never licence-gated."""

import json

import pytest


@pytest.fixture
def client(tmp_path):
    import plexora
    from tests.qc_fixtures import make_qc_project

    make_qc_project(tmp_path, size=512, grid=20, artifacts=())
    return plexora.app.test_client()


def _post(client, path, body):
    return client.post(path, data=json.dumps(body))


def test_the_panel_reads_an_image_with_no_qc_yet(client):
    state = client.get("/plugins/qc/state?datasource=qcsynth").get_json()
    assert state["ok"] and state["result"] is None
    vocabulary = client.get("/plugins/qc/vocabulary").get_json()
    assert {c["id"] for c in vocabulary["classes"]} >= {"tissue_fold", "out_of_focus"}


def test_a_category_is_prepared_and_a_hand_drawn_region_flags_cells(client):
    from plexora.plugins.roi.server import service
    from plexora.agent import AgentSession

    made = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                     "class": "tissue_fold"}).get_json()
    assert made["ok"] and made["category_id"] == "qc_tissue_fold"
    ds = AgentSession().image_data("qcsynth")
    service.create_roi(ds, category=made["label"], points=[[50, 50], [250, 50], [250, 250],
                                                            [50, 250]])
    refreshed = _post(client, "/plugins/qc/refresh", {"datasource": "qcsynth"}).get_json()
    assert refreshed["ok"] and refreshed["sync"]["adopted"]
    state = client.get("/plugins/qc/state?datasource=qcsynth").get_json()
    assert state["regions"][0]["class"] == "tissue_fold"
    assert state["summary"]["cells"]["n_fail"] > 0
    csv_file = client.get("/plugins/qc/download/qcsynth?kind=cells.csv")
    assert csv_file.status_code == 200 and b"primary_reason" in csv_file.data
    strict = _post(client, "/plugins/qc/strictness", {"datasource": "qcsynth",
                                                      "preset": "strict"}).get_json()
    assert strict["ok"]
    refused = _post(client, "/plugins/qc/categories", {"datasource": "qcsynth",
                                                       "class": "not_a_class"})
    assert refused.status_code == 400


def test_a_missing_session_is_404(client):
    missing = _post(client, "/plugins/qc/agent_session/qs_nope/control", {"action": "pause"})
    assert missing.status_code == 404
