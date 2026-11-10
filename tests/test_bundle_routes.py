"""The `.plexora` routes (server/routes/bundle_routes.py), and the older ones
and the desktop's open that now lean on the same core."""

import json
import time
import zipfile

import pytest

from plexora import paths
from plexora.api.store import store
from plexora.server.models import bundles
from plexora.server.models.project import Project

from tests.test_bundles import _image, _register


@pytest.fixture
def project(tmp_path):
    image = _image(tmp_path / "here" / "slide.ome.tif")
    _register("slide", image)
    return image


@pytest.fixture
def client():
    import plexora

    return plexora.app.test_client()


def _wait(client, url, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(url).get_json()
        if status["status"] != "pending":
            return status
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


def test_the_estimate_by_name_or_id(project, client):
    by_name = client.get("/projects/export/estimate?ids=slide&include=images").get_json()
    pid = by_name["projects"][0]["id"]
    assert by_name["include"]["images"] is True and by_name["rows"]["images"]["items"][0]["where"] == "here"
    by_id = client.get(f"/projects/export/estimate?ids={pid}").get_json()
    assert by_id["projects"][0]["name"] == "slide"
    assert client.get("/projects/export/estimate?ids=nothing").status_code == 404
    assert client.get("/projects/export/estimate?ids=slide&include=bogus").status_code == 400


def test_export_job_then_the_file_with_ranges(project, client, tmp_path):
    started = client.post("/projects/export", json={"names": ["slide"]})
    assert started.status_code == 202
    job = started.get_json()["job"]
    status = _wait(client, f"/projects/export/{job}")
    assert status["status"] == "ready" and status["file"]["name"] == "slide.plexora"
    whole = client.get(status["file"]["url"])
    assert whole.status_code == 200 and whole.data[:2] == b"PK"
    assert "slide.plexora" in whole.headers["Content-Disposition"]
    part = client.get(status["file"]["url"], headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.data == whole.data[:10]
    # Written where the user chose, with no file to fetch.
    dest = tmp_path / "chosen" / "mine.plexora"
    job = client.post("/projects/export", json={"ids": ["slide"], "dest": str(dest)}).get_json()["job"]
    status = _wait(client, f"/projects/export/{job}")
    assert status["file"]["path"] == str(dest) and status["file"]["url"] is None and dest.is_file()
    assert client.delete(f"/projects/export/{job}").get_json()["cancelled"] is True
    assert client.get("/projects/export/nope").status_code == 404


def test_preview_by_path_upload_or_manifest_then_import(project, client, tmp_path):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    by_path = client.post("/projects/import/preview", json={"path": str(out)}).get_json()
    assert by_path["projects"][0]["status"] in ("already_here", "conflict")
    assert by_path["path"] == str(out) and by_path["upload"] is None

    with out.open("rb") as handle:
        uploaded = client.post("/projects/import/preview", data={"file": (handle, "slide.plexora")},
                               content_type="multipart/form-data").get_json()
    assert uploaded["upload"] and uploaded["projects"][0]["name"] == "slide"
    with zipfile.ZipFile(out) as bundle:
        manifest = json.loads(bundle.read(bundles.MANIFEST))
    by_manifest = client.post("/projects/import/preview", json={"manifest": manifest}).get_json()
    assert by_manifest["projects"][0]["id"] == manifest["projects"][0]["id"]

    pid = manifest["projects"][0]["id"]
    started = client.post("/projects/import", json={"upload": uploaded["upload"], "choices": {pid: "keep_both"}})
    assert started.status_code == 202
    status = _wait(client, f"/projects/import/{started.get_json()['job']}")
    assert status["status"] == "ready", status
    assert status["result"]["projects"][0]["target"] == "slide_2"
    assert not (bundles.staging_root() / f"upload-{uploaded['upload']}").exists()
    assert store("slide_2", "gating").get_state() == store("slide", "gating").get_state()

    assert client.post("/projects/import/preview", json={"path": str(tmp_path / "none.plexora")}).status_code == 400
    assert client.post("/projects/import/preview", data=b"PK nope",
                       content_type="application/zip").status_code == 400


def test_make_a_copy_and_find_missing(project, client, tmp_path):
    started = client.post("/projects/slide/copy", json={})
    assert started.status_code == 202
    status = _wait(client, f"/projects/import/{started.get_json()['job']}")
    assert status["status"] == "ready" and status["result"]["projects"][0]["target"] == "slide_2"
    assert "slide_2" in Project.load_all()
    assert client.post("/projects/nothing/copy", json={}).status_code == 404
    found = client.post("/projects/slide/find_missing", json={"folder": str(tmp_path)}).get_json()
    assert found == {"found": 0, "missing": []}
    assert client.post("/projects/slide/find_missing", json={"folder": "/no/such/folder"}).status_code == 400


def test_the_website_plan_and_manifest(project, client):
    plan = client.post("/projects/export/plan", json={"ids": ["slide"]}).get_json()
    assert {member["kind"] for member in plan["members"]} >= {"entry", "state", "table"}
    sums = {member["bundle_path"]: "0" * 64 for member in plan["members"]}
    finished = client.post("/projects/export/manifest", json={"manifest": plan["manifest"], "sums": sums}).get_json()
    assert finished["projects"][0]["entry"]["sha256"] == "0" * 64


def test_the_older_routes_and_the_desktops_open(project, client, tmp_path):
    answer = client.get("/project/slide/bundle")
    assert answer.status_code == 200 and "slide.plexora" in answer.headers["Content-Disposition"]
    with zipfile.ZipFile(__import__("io").BytesIO(answer.data)) as bundle:
        assert bundle.namelist()[0] == bundles.MANIFEST
    imported = client.post("/projects/bundle", data=answer.data, content_type="application/zip").get_json()
    assert imported["project"] == "slide_2"
    assert store("slide_2", "gating").get_state() == store("slide", "gating").get_state()
    path = tmp_path / "sent.plexora"
    path.write_bytes(answer.data)
    opened = client.post("/desktop/open", json={"paths": [str(path)]}).get_json()
    assert opened["open"] == "bundle" and opened["path"] == str(path)
    assert opened["url"].startswith("/open_project?bundle=") and opened["project"] is None
    assert "slide_3" not in Project.load_all()  # nothing imported blind
    assert client.post("/projects/bundle", data=b"nope").status_code == 400
    assert client.get("/project/nothing/bundle").status_code == 404
    assert paths.data_root().exists()
