"""A project as one `.plexora` file (server/models/bundles.py): its own work
goes, its images and caches do not, and on another computer it opens with its
images found there."""

import io
import json
import zipfile

import numpy as np
import pytest
import tifffile

from plexora import paths
from plexora.server.models import bundles, sources
from plexora.server.utils import relink


def _image(path):
    data = (np.arange(2 * 256 * 320) % 4000).astype(np.uint16).reshape(2, 256, 320)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, tile=(128, 128), subifds=1, photometric="minisblack", metadata={"axes": "CYX"})
        writer.write(data[:, ::2, ::2], tile=(128, 128), subfiletype=1, photometric="minisblack")
    return path


@pytest.fixture
def project(tmp_path):
    import plexora

    image = _image(tmp_path / "here" / "slide.ome.tif")
    plexora.register_image_datasource("slide", str(image))
    folder = paths.project_state_dir("slide")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "slide.db").write_bytes(b"database")
    (folder / "plugins" / "gating").mkdir(parents=True)
    (folder / "plugins" / "gating" / "gates.json").write_text('{"CD3": 120}')
    for cache in ("spatial_index_v1/index.pickle", "plugins/gating/classifier/features/CD3_x.npz",
                  ".thumbnail-v2.webp", ".open-website.json"):
        (folder / cache).parent.mkdir(parents=True, exist_ok=True)
        (folder / cache).write_bytes(b"cache")
    return image


def _members(data):
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        return sorted(bundle.namelist()), json.loads(bundle.read(bundles.MANIFEST))


def test_its_own_work_goes_and_caches_do_not(project):
    names, manifest = _members(bundles.export("slide"))
    assert names == ["plexora-project.json", "project/plugins/gating/gates.json", "project/slide.db"]
    assert manifest["name"] == "slide" and manifest["format"] == bundles.FORMAT
    assert manifest["entry"]["sources"][str(project)]["size"] == project.stat().st_size
    assert manifest["left_out"] == []


def test_imported_under_a_free_name_with_its_database_renamed(project):
    data = bundles.export("slide")
    assert bundles.import_bundle(data) == "slide_2"
    folder = paths.project_state_dir("slide_2")
    assert (folder / "slide_2.db").read_bytes() == b"database"
    assert json.loads((folder / "plugins" / "gating" / "gates.json").read_text()) == {"CD3": 120}
    from plexora.server.models.project import Project

    assert Project.load("slide_2").image.src == str(project)


def test_a_file_that_is_not_a_bundle_is_refused(tmp_path):
    with pytest.raises(bundles.BundleError, match="not a Plexora project file"):
        bundles.import_bundle(b"PK not really")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(bundles.MANIFEST, json.dumps({"format": 1, "name": "x", "entry": {}}))
        bundle.writestr("project/../../outside.txt", "no")
    with pytest.raises(bundles.BundleError, match="outside its project"):
        bundles.import_bundle(buffer.getvalue())


def test_on_another_computer_it_opens_with_its_image_found_there(project, tmp_path, monkeypatch):
    data = bundles.export("slide")
    # Another computer: its own projects, the same image somewhere else.
    elsewhere = tmp_path / "other"
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(elsewhere / "projects"))
    paths.reset()
    moved = elsewhere / "Downloads" / "slide.ome.tif"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(project.read_bytes())
    project.unlink()
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")
    monkeypatch.setattr(relink, "_index", lambda name: [])
    monkeypatch.setattr(relink, "_user_folders", lambda: [elsewhere / "Downloads"])
    assert bundles.import_bundle(data) == "slide"
    from plexora.server.models import data_model

    data_model.load_datasource("slide", reload=True)
    assert data_model.config["slide"]["channelFile"] == str(moved)


def test_the_routes_and_the_desktops_open(project, tmp_path):
    import plexora

    client = plexora.app.test_client()
    answer = client.get("/project/slide/bundle")
    assert answer.status_code == 200 and "slide.plexora" in answer.headers["Content-Disposition"]
    imported = client.post("/projects/bundle", data=answer.data, content_type="application/zip").get_json()
    assert imported["project"] == "slide_2"
    path = tmp_path / "sent.plexora"
    path.write_bytes(answer.data)
    opened = client.post("/desktop/open", json={"paths": [str(path)]}).get_json()
    assert opened["project"] == "slide_3" and opened["existing"] is False
    assert client.post("/projects/bundle", data=b"nope").status_code == 400


def test_the_command_line(project, tmp_path, capsys):
    from plexora import cli

    out = tmp_path / "x.plexora"
    assert cli.main(["project", "export", "slide", "--out", str(out)]) == 0
    assert cli.main(["project", "import", str(out), "--name", "copy"]) == 0
    from plexora.server.models.project import Project

    assert Project.load("copy") is not None
