"""The New Dataset dialog's routes, end to end: scan, correct, save, create."""

import json
import time

import pytest

import plexora
from plexora.server.models import dataset_build, dataset_scan, layer_jobs
from tests import dataset_fixtures as fx
from tests.helpers import use_data_root


@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path / "root")
    (tmp_path / "root" / "config.json").write_text("{}", encoding="utf-8")
    dataset_scan.forget()
    dataset_build.forget()
    yield plexora.app.test_client()
    for owner, task_id in layer_jobs.running_tasks():
        if owner in (dataset_scan.TASK_OWNER, dataset_build.TASK_OWNER):
            layer_jobs.stop_task(owner, task_id)
    dataset_scan.forget()
    dataset_build.forget()


def _poll(client, url, done, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(url)
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        if done(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"{url} did not finish")


def _scan(client, root):
    response = client.post("/datasets/scan", json={"paths": [str(root)]})
    assert response.status_code == 202, response.get_json()
    token = response.get_json()["token"]
    return token, _poll(client, f"/datasets/scan/{token}", lambda b: not b["pending"])


def test_scan_correct_and_create(client, tmp_path):
    root = fx.layout_e(tmp_path / "cohort")
    token, body = _scan(client, root)
    assert body["status"] == "ready"
    assert [p["name"] for p in body["projects"]] == ["S1", "S2"]
    [stray] = body["unassigned"]

    corrected = client.post(f"/datasets/scan/{token}/plan", json={"overrides": {
        "assign": [{"path": stray["path"], "project": "p:s_2", "slot": "mask"}],
        "rename": {"p:s_1": "First"}}})
    assert corrected.status_code == 200
    plan = corrected.get_json()
    assert [p["name"] for p in plan["projects"]] == ["First", "S2"]
    assert plan["projects"][1]["mask"]["confidence"] == "user"
    # Kept on the scan: the next poll sees the same plan.
    again = client.get(f"/datasets/scan/{token}").get_json()
    assert again["projects"][1]["mask"]["path"] == stray["path"]

    spec = client.get(f"/datasets/scan/{token}/spec?name=Cohort E")
    document = json.loads(spec.data)
    assert document["name"] == "Cohort E"
    assert [p["name"] for p in document["projects"]] == ["First", "S2"]

    started = client.post(f"/datasets/scan/{token}/create", json={"name": "Cohort E"})
    assert started.status_code == 202, started.get_json()
    built = _poll(client, f"/datasets/scan/{token}/create",
                  lambda b: b["status"] != "pending")
    assert built["status"] == "ready"
    assert [p["status"] for p in built["projects"]] == ["created", "created"]
    listing = client.get("/datasets").get_json()["datasets"]
    [dataset] = [d for d in listing if d["name"] == "Cohort E"]
    assert sorted(dataset["projects"]) == ["First", "S2"]


def test_post_datasets_scan_is_the_scan_route_not_a_dataset(client, tmp_path):
    rule = next(r for r in plexora.app.url_map.iter_rules()
                if r.rule == "/datasets/scan" and "POST" in r.methods)
    assert rule.endpoint == "dataset_scan_start"
    response = client.post("/datasets/scan", json={"paths": [str(tmp_path / "none")]})
    assert response.status_code == 400
    assert "is not a folder" in response.get_json()["error"]


def test_unknown_and_refused(client, tmp_path):
    assert client.get("/datasets/scan/scan-nope").status_code == 404
    assert client.post("/datasets/scan/scan-nope/plan", json={}).status_code == 404
    assert client.get("/datasets/scan/scan-nope/create").status_code == 404
    assert client.post("/datasets/scan", json={"paths": []}).status_code == 400
    assert client.post("/datasets/scan", json={"paths": ["/x"], "node": "o2"}
                       ).status_code == 400
    root = fx.layout_a(tmp_path / "cohort")
    token, _body = _scan(client, root)
    bad = client.post(f"/datasets/scan/{token}/plan",
                      json={"overrides": {"assign": [{"path": "x"}]}})
    assert bad.status_code == 400


def test_a_taken_name_is_refused_with_409(client, tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    token, _body = _scan(client, root)
    plexora.create_dataset("Taken")
    response = client.post(f"/datasets/scan/{token}/create", json={"name": "Taken"})
    assert response.status_code == 409 and response.get_json()["taken"]
    assert client.post(f"/datasets/scan/{token}/create", json={"name": ""}
                       ).status_code == 400


def test_add_a_folder_to_a_scan(client, tmp_path):
    first = fx.layout_a(tmp_path / "one")
    second = tmp_path / "two"
    fx.image(second / "D.ome.tiff")
    token, _body = _scan(client, first)
    response = client.post("/datasets/scan", json={"paths": [str(second)], "token": token})
    assert response.status_code == 202
    body = _poll(client, f"/datasets/scan/{token}", lambda b: not b["pending"])
    assert [p["name"] for p in body["projects"]] == ["A", "B", "C", "D"]
    assert body["roots"] == [str(first.resolve()), str(second.resolve())]


def test_cancel_a_scan(client, tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    token, _body = _scan(client, root)
    response = client.post(f"/datasets/scan/{token}/cancel")
    assert response.get_json()["cancelled"] is True
    assert client.post(f"/datasets/scan/{token}/create/cancel").status_code == 200


def test_retry_only_the_failed(client, tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    token, _body = _scan(client, root)
    response = client.post(f"/datasets/scan/{token}/create",
                           json={"name": "Partial", "only": ["p:b"]})
    assert response.status_code == 202
    built = _poll(client, f"/datasets/scan/{token}/create",
                  lambda b: b["status"] != "pending")
    assert [p["name"] for p in built["projects"]] == ["B"]
    assert built["dataset"]["projects"] == ["B"]


def test_a_spec_keeps_only_the_samples_ticked(client, tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    token, _body = _scan(client, root)
    spec = client.get(f"/datasets/scan/{token}/spec?name=Some&only=p:a&only=p:c")
    assert [p["name"] for p in json.loads(spec.data)["projects"]] == ["A", "C"]
