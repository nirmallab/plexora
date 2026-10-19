"""The classifier's routes, as the panel calls them: annotate, run (a job the
panel follows), the calls bytes, the eraser, the downloads and the import."""

from __future__ import annotations

import gzip
import io
import json
import time

import numpy as np
import pytest

import plexora
from tests.agent_fixtures import make_synthetic_project


@pytest.fixture
def made(tmp_path):
    return make_synthetic_project(tmp_path)


@pytest.fixture
def client(made):
    return plexora.app.test_client()


def post(client, path, **body):
    return client.post(f"/plugins/gating/classifier/{path}",
                       json={"datasource": "synth", **body})


def get(client, path, **args):
    return client.get(f"/plugins/gating/classifier/{path}",
                      query_string={"datasource": "synth", **args})


def _follow(client, job_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/plugins/gating/jobs/{job_id}").get_json()["job"]
        if job["status"] in ("done", "failed", "cancelled", "interrupted"):
            return job
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def _label(client, made):
    for cell in made["positives"][:4]:
        assert post(client, "annotate", marker="CD8", cell_id=cell,
                    label="positive").status_code == 200
    for cell in made["negatives"][:4]:
        post(client, "annotate", marker="CD8", cell_id=cell, label="negative")


def test_the_whole_panel_flow(client, made):
    cell = made["cells"][1]
    picked = get(client, "cell_at", x=cell["x"], y=cell["y"]).get_json()
    assert picked["ok"] and picked["cell_id"] == cell["id"]

    _label(client, made)
    state = get(client, "state", marker="CD8").get_json()
    assert state["marker"]["can_run"] is True
    assert state["marker"]["examples"]["n_positive"] == 4

    started = post(client, "run", marker="CD8").get_json()
    assert started["ok"] and started["job_id"]
    job = _follow(client, started["job_id"])
    assert job["status"] == "done", job["error"]

    response = get(client, "calls", marker="CD8")
    assert response.headers["X-Classifier-State"] == "ready"
    codes = np.frombuffer(gzip.decompress(response.data), dtype=np.uint8)
    assert codes.size == len(made["cells"])
    assert int(response.headers["X-Classifier-Eligible"]) == len(made["cells"])
    assert int(response.headers["X-Classifier-Positive"]) >= len(made["positives"])

    binary = get(client, "export/binary.csv")
    assert binary.status_code == 200
    text = b"".join(binary.response).decode() if not binary.data else binary.data.decode()
    assert text.splitlines()[0] == "image_id,cell_id,CD8"
    zipped = get(client, "export/scores.csv", gz=1)
    assert gzip.decompress(zipped.data).decode().startswith("image_id,cell_id,CD8")
    document = get(client, "export/annotations.json").get_json()
    assert sorted(document["markers"]["CD8"]["negative"]) == sorted(made["negatives"][:4])

    reverted = post(client, "revert", marker="CD8").get_json()
    assert reverted["reverted"] is True
    after = get(client, "calls", marker="CD8")
    assert after.headers["X-Classifier-State"] == "not_run"
    assert get(client, "state", marker="CD8").get_json()["marker"]["examples"][
        "n_positive"] == 4

    cleared = post(client, "examples/clear").get_json()
    assert cleared["cleared"] == 8
    preview = client.post("/plugins/gating/classifier/annotations/preview", data={
        "datasource": "synth",
        "file": (io.BytesIO(json.dumps(document).encode()), "annotations.json")},
        content_type="multipart/form-data").get_json()
    assert preview["ok"] and preview["markers"]["CD8"]["valid_negative"] == 4
    applied = post(client, "annotations/apply", token=preview["token"], mode="replace")
    assert applied.get_json()["counts"]["CD8"] == {"positive": 4, "negative": 4}


def test_refusals_are_400s_with_the_panels_words(client, made):
    answer = post(client, "run", marker="CD8")
    assert answer.status_code == 400
    assert "3 positive" in answer.get_json()["error"]["message"]
    assert post(client, "annotate", marker="CD8", cell_id=10**7,
                label="positive").status_code == 400
    assert get(client, "export/binary.csv").status_code == 409
    bad = client.post("/plugins/gating/classifier/annotations/preview", data={
        "datasource": "synth", "file": (io.BytesIO(b"not json"), "x.json")},
        content_type="multipart/form-data")
    assert bad.status_code == 400


def test_the_mode_switch_is_remembered(client):
    assert get(client, "state").get_json()["mode"] == "threshold"
    assert post(client, "mode", mode="classifier").get_json()["mode"] == "classifier"
    assert get(client, "state").get_json()["mode"] == "classifier"


def test_a_job_that_is_not_gatings_is_not_served(client):
    assert client.get("/plugins/gating/jobs/nope").status_code == 404
