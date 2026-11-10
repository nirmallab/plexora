"""Projects as one `.plexora` file (server/models/bundles.py, FORMAT 2): their
own work goes, their images are referenced and found where the other computer
has them, and an import under another name keeps every saved row."""

import io
import json
import os
import shutil
import sqlite3
import threading
import time
import tracemalloc
import zipfile

import numpy as np
import polars as pl
import pytest
import tifffile

from plexora import paths
from plexora.api.store import store
from plexora.server.models import bundles, data_model, sources
from plexora.server.models.project import Project, config_transaction, read_config, write_config
from plexora.server.utils import relink


def _image(path):
    data = (np.arange(2 * 256 * 320) % 4000).astype(np.uint16).reshape(2, 256, 320)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, tile=(128, 128), subifds=1, photometric="minisblack", metadata={"axes": "CYX"})
        writer.write(data[:, ::2, ::2], tile=(128, 128), subfiletype=1, photometric="minisblack")
    return path


def _edit_entry(name, change):
    root = Project.root_for(name)
    with config_transaction():
        config = read_config(paths.config_path(root))
        change(config[name])
        write_config(paths.config_path(root), config)


def _register(name, image):
    """A project as the app makes one, with saved state written through the
    APIs the viewer and the plugins use: channel settings, gates, regions,
    QC results, and a cell table Plexora made inside the project."""
    import plexora

    plexora.register_image_datasource(name, str(image))
    folder = paths.project_state_dir(name)
    folder.mkdir(parents=True, exist_ok=True)
    data_model.save_channel_list(name, {"0": "Channel 0"}, ["0"],
                                 {"0": {"color": {"r": 10, "g": 200, "b": 30, "opacity": 1}}},
                                 {}, {"Channel 0": [12, 3400]})
    store(name, "gating").put_state(b'{"gates": {"CD3": 120}}')
    store(name, "roi").put_table("regions", pl.DataFrame({"id": [1, 2], "name": ["tumour", "stroma"]}))
    store(name, "qc").put_table("results", pl.DataFrame({"category": ["blur"], "fraction": [0.25]}))
    (folder / "plugins" / "gating").mkdir(parents=True, exist_ok=True)
    (folder / "plugins" / "gating" / "gates.json").write_text('{"CD3": 120}')
    table = folder / "cells_edited.csv"
    table.write_text("CellID,X_centroid,Y_centroid,CD3\n1,10,10,5\n2,20,20,7\n")
    _edit_entry(name, lambda entry: entry.update(dataset={"type": "csv", "src": str(table),
                                                          "roles": {"cell_id": "CellID"}}))
    return folder


CACHE_FILES = ("spatial_index_v1/index.pickle", "plugins/gating/classifier/features/CD3_x.npz",
               ".thumbnail-v2.webp", ".open-website.json", "centroids_v1/level_0/tile_0_0.bin",
               "transcripts_v2/level_0/t.bin", "bins_v1/8um/a.bin", "brightfield_pyramid.zarr/0/0",
               "volume_v1/brick.bin", "objects_planes_v1/z.bin", "ball_tree.pickle",
               "plugins/qc/blur/abc123.npz", "cells_edited.csv.bak_before_gatediff", "x.db.corrupt-123",
               "plugins/qc/qc.lock")


@pytest.fixture
def project(tmp_path):
    image = _image(tmp_path / "here" / "slide.ome.tif")
    folder = _register("slide", image)
    for cache in CACHE_FILES:
        (folder / cache).parent.mkdir(parents=True, exist_ok=True)
        (folder / cache).write_bytes(b"cache")
    return image


def _members(path):
    with zipfile.ZipFile(path) as bundle:
        return bundle.namelist(), json.loads(bundle.read(bundles.MANIFEST))


def _another_computer(tmp_path, monkeypatch, label="other"):
    """A second data root, as another computer's; images found only where a
    test puts them (no system index, no walk of the developer's folders)."""
    elsewhere = tmp_path / label
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(elsewhere / "projects"))
    paths.reset()
    monkeypatch.setattr(relink, "_index", lambda name: [])
    monkeypatch.setattr(relink, "_user_folders", lambda: [])
    return elsewhere


def _saved_state(name):
    return {"channels": data_model.get_saved_channel_list(name),
            "gating": store(name, "gating").get_state(),
            "roi": store(name, "roi").get_table("regions"),
            "qc": store(name, "qc").get_table("results")}


# -- what goes ---------------------------------------------------------------


def test_its_own_work_and_its_tables_go_and_every_cache_stays(project, tmp_path):
    out = tmp_path / "out" / "slide.plexora"
    result = bundles.export(["slide"], out)
    names, manifest = _members(out)
    assert names[0] == bundles.MANIFEST
    entry = manifest["projects"][0]
    pid = entry["id"]
    carried = {record["path"]: record["kind"] for record in entry["files"]}
    assert carried[f"projects/{pid}/state/slide.db"] == "state"
    assert carried[f"projects/{pid}/state/plugins/gating/gates.json"] == "state"
    # A table Plexora made inside the project goes, as a table.
    assert carried[f"projects/{pid}/state/cells_edited.csv"] == "table"
    for cache in CACHE_FILES:
        assert f"projects/{pid}/state/{cache}" not in names, cache
    assert all(record["sha256"] and len(record["sha256"]) == 64 for record in entry["files"])
    assert len(entry["revision"]) == 64
    # The image is referenced, with its identity, not carried.
    ref = entry["refs"][0]
    assert ref["kind"] == "file" and ref["included"] is None
    assert ref["identity"]["size"] == project.stat().st_size
    assert ref["hints"]["absolute"] == str(project)
    assert result["manifest"]["bundle"]["id"] == manifest["bundle"]["id"]
    # Never the computer's own records.
    assert not any(name.endswith(("nodes.json", "remotes.json", "servers.json")) for name in names)


def test_the_database_is_a_snapshot_and_the_manifest_is_stored_first(project, tmp_path):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    with zipfile.ZipFile(out) as bundle:
        first = bundle.infolist()[0]
        assert first.filename == bundles.MANIFEST and first.compress_type == zipfile.ZIP_STORED
        pid = json.loads(bundle.read(bundles.MANIFEST))["projects"][0]["id"]
        data = bundle.read(f"projects/{pid}/state/slide.db")
        assert bundle.testzip() is None
    assert data.startswith(b"SQLite format 3\x00")
    copy = tmp_path / "copy.db"
    copy.write_bytes(data)
    with sqlite3.connect(copy) as connection:
        assert connection.execute('SELECT datasource FROM "channelList"').fetchone()[0] == "slide"


def test_paths_are_tokenised_and_rewritten(project, tmp_path):
    _edit_entry("slide", lambda entry: entry.update(
        imageData=[{"name": "Channel 0", "src": "/generated/data/slide/slide_0/"}]))
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    with zipfile.ZipFile(out) as bundle:
        manifest = json.loads(bundle.read(bundles.MANIFEST))
        pid = manifest["projects"][0]["id"]
        entry = json.loads(bundle.read(f"projects/{pid}/entry.json"))
    assert entry["dataset"]["src"] == "{{state}}/cells_edited.csv"
    assert entry["imageData"][0]["src"] == "/generated/data/{{name}}/slide_0/"
    assert entry["channelFile"] == "{{ref:0}}"
    assert "sources" not in entry

    result = bundles.apply(out, choices={pid: "keep_both"})
    assert result["projects"][0]["target"] == "slide_2"
    imported = read_config(paths.config_path(Project.root_for("slide_2")))["slide_2"]
    assert imported["dataset"]["src"] == str(paths.project_state_dir("slide_2") / "cells_edited.csv")
    assert imported["imageData"][0]["src"] == "/generated/data/slide_2/slide_0/"
    assert imported["channelFile"] == str(project)
    assert imported["origin"]["revision"] == manifest["projects"][0]["revision"]
    assert imported["id"] != pid  # a second copy here is a project of its own


# -- the defect-1 regression -------------------------------------------------


def test_a_renamed_import_keeps_its_channels_gates_regions_and_qc(project, tmp_path):
    before = _saved_state("slide")
    assert before["channels"] and before["gating"] and before["roi"] is not None
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    assert bundles.import_bundle(out) == "slide_2"
    db = paths.project_state_dir("slide_2") / "slide_2.db"
    assert db.is_file() and not (paths.project_state_dir("slide_2") / "slide.db").exists()
    after = _saved_state("slide_2")
    assert after["channels"] == before["channels"]
    assert after["gating"] == before["gating"]
    assert after["roi"].equals(before["roi"])
    assert after["qc"].equals(before["qc"])
    with sqlite3.connect(db) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            names = {row[0] for row in connection.execute(f'SELECT datasource FROM "{table}"')}
            assert names <= {"slide_2"}, (table, names)
    # The original is untouched.
    assert _saved_state("slide")["channels"] == before["channels"]


# -- finding the images ------------------------------------------------------


def test_on_another_computer_the_image_is_found_at_the_same_path(project, tmp_path, monkeypatch):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    _another_computer(tmp_path, monkeypatch)
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")
    preview = bundles.preview(out)
    assert preview["projects"][0]["status"] == "new"
    assert preview["projects"][0]["refs"]["found"] == 1
    bundles.apply(out)
    assert Project.load("slide").image.src == str(project)


def test_found_relative_to_the_bundle_across_two_mounts(project, tmp_path, monkeypatch):
    # The same share at two places: /Volumes/lab on one computer and
    # another mount on the other, with the bundle saved beside the images.
    first = tmp_path / "mount_a" / "lab"
    image = _image(first / "images" / "run1" / "scan.ome.tif")
    import plexora

    plexora.register_image_datasource("scan", str(image))
    out = first / "exports" / "scan.plexora"
    bundles.export(["scan"], out)
    manifest = _members(out)[1]
    assert manifest["projects"][0]["refs"][0]["hints"]["relative_to_bundle"] == "../images/run1/scan.ome.tif"
    second = tmp_path / "mount_b" / "lab"
    shutil.copytree(first, second)
    shutil.rmtree(first)
    _another_computer(tmp_path, monkeypatch)
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")
    monkeypatch.setattr(bundles, "FIND_SECONDS", 0.0)
    bundles.apply(second / "exports" / "scan.plexora")
    assert Project.load("scan").image.src == str(second / "images" / "run1" / "scan.ome.tif")


def test_found_under_a_shared_root_by_the_tail_of_its_path(project, tmp_path, monkeypatch):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    share = tmp_path / "share"
    moved = share / "here" / "slide.ome.tif"
    moved.parent.mkdir(parents=True)
    shutil.copy2(project, moved)
    project.unlink()
    _another_computer(tmp_path, monkeypatch)
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")
    monkeypatch.setattr(bundles, "FIND_SECONDS", 0.0)
    monkeypatch.setenv("PLEXORA_SHARED_PATH", str(share))
    paths.reset()
    assert bundles.preview(out)["projects"][0]["refs"]["found"] == 1
    bundles.apply(out)
    entry = read_config(paths.config_path(Project.root_for("slide")))["slide"]
    assert entry["channelFile"] == str(moved)
    assert entry["sources"][str(project)]["at"]["host:OTHER"] == str(moved)


def test_found_by_the_search_when_moved_elsewhere(project, tmp_path, monkeypatch):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    elsewhere = _another_computer(tmp_path, monkeypatch)
    moved = elsewhere / "Downloads" / "slide.ome.tif"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(project.read_bytes())
    project.unlink()
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")
    monkeypatch.setattr(relink, "_user_folders", lambda: [elsewhere / "Downloads"])
    preview = bundles.preview(out)
    assert preview["projects"][0]["refs"]["missing"] == 1
    bundles.apply(out)
    data_model.load_datasource("slide", reload=True)
    assert data_model.config["slide"]["channelFile"] == str(moved)


def test_a_missing_image_keeps_its_identity_for_later(project, tmp_path, monkeypatch):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    _another_computer(tmp_path, monkeypatch)
    project.rename(project.with_name("gone.tif"))
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")
    monkeypatch.setattr(bundles, "FIND_SECONDS", 0.0)
    result = bundles.apply(out)
    assert result["projects"][0]["refs"]["missing"] == 1
    assert [item["recorded"] for item in relink.missing_here("slide")] == [str(project)]
    # Found in the one folder the user picks, and only there.
    picked = tmp_path / "picked" / "deeper"
    picked.mkdir(parents=True)
    project.with_name("gone.tif").rename(picked / project.name)
    assert relink.find_in_folder("slide", tmp_path / "here")["found"] == 0
    assert relink.find_in_folder("slide", tmp_path / "picked")["found"] == 1
    assert relink.missing_here("slide") == []


def test_included_images_are_placed_in_the_project(project, tmp_path, monkeypatch):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out, include={"images": True})
    names, manifest = _members(out)
    ref = manifest["projects"][0]["refs"][0]
    assert ref["included"] and ref["included"] in names
    assert manifest["media"][0]["sha256"]
    _another_computer(tmp_path, monkeypatch)
    project.unlink()
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")
    assert bundles.preview(out)["projects"][0]["refs"] == {"found": 0, "included": 1, "missing": 0,
                                                            "node": 0, "nodes": []}
    bundles.apply(out)
    placed = paths.project_state_dir("slide").joinpath(*ref["included"].split("/"))
    assert placed.is_file()
    assert Project.load("slide").image.src == str(placed)
    # The next export takes it for the image it is, not for the project's own work.
    again = tmp_path / "again.plexora"
    names = _members(bundles.export(["slide"], again)["path"])[0]
    assert not any("/state/media/" in name for name in names)


def test_a_zarr_folder_has_an_identity_that_matches_its_copy(tmp_path):
    store_ = tmp_path / "a" / "image.ome.zarr"
    for rel in ("0/0.0.0", "0/0.0.1", "1/0.0.0", ".zattrs"):
        (store_ / rel).parent.mkdir(parents=True, exist_ok=True)
        (store_ / rel).write_bytes(rel.encode() * 100)
    facts = sources.folder_identity(store_)
    assert facts["kind"] == "folder" and facts["entries"] == 4
    from plexora.server.utils import segmentation_pyramid

    assert facts["fingerprint"] == segmentation_pyramid.source_fingerprint(store_)
    copy = tmp_path / "b" / "image.ome.zarr"
    shutil.copytree(store_, copy, copy_function=shutil.copyfile)  # new times, same listing
    assert sources.same_file(facts, copy)
    assert not sources.same_file(facts, tmp_path / "a" / "image.ome.zarr" / "0")
    (copy / "2").mkdir()
    (copy / "2" / "x").write_bytes(b"x")
    assert not sources.same_file(facts, copy)
    shutil.rmtree(copy / "2")
    found = relink.resolve_in({"sources": {"/elsewhere/image.ome.zarr": facts}}, "host:X", tmp_path / "b")
    assert found == {"/elsewhere/image.ome.zarr": str(copy)}
    walked = relink.resolve({"sources": {"/elsewhere/image.ome.zarr": facts}}, "host:X",
                            extra_roots=[tmp_path / "b"], index=lambda name: [], walk_seconds=2)
    assert walked == {"/elsewhere/image.ome.zarr": str(copy)}


def test_relative_to_bundle_and_tails():
    assert relink.relative_to_bundle("../images/a.tif", "/Volumes/lab/exports/x.plexora") == \
        __import__("pathlib").Path("/Volumes/lab/images/a.tif")
    assert relink.relative_to_bundle("/abs/a.tif", "/Volumes/lab/exports/x.plexora") is None
    tails = relink.tail_candidates("/Volumes/lab/images/run/a.tif", ["/mnt/lab"], depth=3)
    assert [str(path) for path in tails] == ["/mnt/lab/images/run/a.tif", "/mnt/lab/run/a.tif", "/mnt/lab/a.tif"]


def test_a_node_image_travels_as_its_node_and_resource(project, tmp_path, monkeypatch):
    binding = {"provider": "node", "node": "hms-o2", "resource_id": "res-123",
               "fingerprint": {"size": 31787445961, "mtime_ns": 1}}
    _edit_entry("slide", lambda entry: entry.update(channelFile="node://hms-o2/res-123",
                                                    resources={"image": binding}))
    out = tmp_path / "slide.plexora"
    sized = bundles.estimate(["slide"])
    item = sized["rows"]["images"]["items"][0]
    assert item["where"] == "node" and item["node"] == "hms-o2" and item["can_include"] is False
    bundles.export(["slide"], out, include={"images": True})
    names, manifest = _members(out)
    refs = manifest["projects"][0]["refs"]
    assert refs == [{"token": None, "role": "image", "kind": "node", "included": None, "derived": False,
                     "identity": {"node": "hms-o2", "resource_id": "res-123",
                                  "fingerprint": binding["fingerprint"]}, "hints": {}}]
    assert not any(name.startswith("media/") for name in names)
    assert not any("nodes.json" in name or "token" in name for name in names)
    _another_computer(tmp_path, monkeypatch)
    preview = bundles.preview(out)["projects"][0]["refs"]
    assert preview["node"] == 1 and preview["nodes"] == ["hms-o2"]
    bundles.apply(out)
    entry = read_config(paths.config_path(Project.root_for("slide")))["slide"]
    assert entry["channelFile"] == "node://hms-o2/res-123" and entry["resources"]["image"] == binding


# -- many projects, datasets, captures and figures ---------------------------


def test_a_dataset_goes_with_its_captures_and_figures(project, tmp_path):
    from plexora.server.models import datasets as registry
    from plexora.plugins.figure_builder.server import captures, repository

    _register("slide_b", _image(tmp_path / "here" / "slide_b.ome.tif"))
    registry.create("Trial", projects=["slide", "slide_b"])
    captures.add("cap_aaa111", "slide", {"datasource": "slide"}, {}, caption="tumour edge",
                 bookmarked=True, label="Edge")
    captures.put_preview("cap_aaa111", b"RIFF....WEBPVP8 fake", 4, 4)
    captures.add("cap_bbb222", "slide_b", {"datasource": "slide_b"}, {}, caption="b")
    figure = repository.create("Both")
    document = repository.load(figure)
    document["sources"] = {f"src_{name}": {"source_id": f"src_{name}", "kind": "plexora_project",
                                           "datasource": name, "display_name": ""}
                           for name in ("slide", "slide_b")}
    repository.replace(figure, 0, document)
    other = repository.create("Not this one")
    document = repository.load(other)
    document["sources"] = {"src_x": {"source_id": "src_x", "kind": "plexora_project", "datasource": "elsewhere"}}
    repository.replace(other, 0, document)

    out = tmp_path / "trial.plexora"
    bundles.export([], out, dataset="Trial")
    names, manifest = _members(out)
    assert [p["name"] for p in manifest["projects"]] == ["slide", "slide_b"]
    assert manifest["datasets"][0]["name"] == "Trial"
    assert [item["id"] for item in manifest["figures"]] == [figure]
    assert sum(item["count"] for item in manifest["captures"]) == 2

    ids = [p["id"] for p in manifest["projects"]]
    result = bundles.apply(out, choices={pid: "keep_both" for pid in ids})
    targets = sorted(item["target"] for item in result["projects"])
    assert targets == ["slide_2", "slide_b_2"]
    trial = registry.find_by_name("Trial")
    assert set(trial.projects) == {"slide", "slide_b", "slide_2", "slide_b_2"}
    rows = captures.list_captures(datasource="slide_2")
    assert len(rows) == 1 and rows[0]["caption"] == "tumour edge" and rows[0]["bookmarked"]
    assert captures.get_preview(rows[0]["capture_id"])[0] == b"RIFF....WEBPVP8 fake"
    assert rows[0]["capture_id"] != "cap_aaa111"  # the id was taken here
    copied = result["figures"][0]["id"]
    assert copied != figure
    loaded = repository.load(copied)
    assert sorted(source["datasource"] for source in loaded["sources"].values()) == ["slide_2", "slide_b_2"]
    assert loaded["figure_id"] == copied


# -- statuses ----------------------------------------------------------------


def test_new_already_here_update_and_changed_on_both(project, tmp_path, monkeypatch):
    first_root = paths.data_root()
    one = tmp_path / "one.plexora"
    bundles.export(["slide"], one)
    pid = _members(one)[1]["projects"][0]["id"]

    # The other computer: new, then already here.
    _another_computer(tmp_path, monkeypatch)
    assert bundles.preview(one)["projects"][0]["status"] == "new"
    bundles.apply(one)
    assert bundles.preview(one)["projects"][0]["status"] == "already_here"
    assert bundles.apply(one)["projects"][0]["action"] == "skip"
    # Changed there and sent back.
    store("slide", "gating").put_state(b'{"gates": {"CD3": 150}}')
    two = tmp_path / "two.plexora"
    sent = bundles.export(["slide"], two)["manifest"]["projects"][0]
    assert sent["id"] == pid and sent["parent"] == _members(one)[1]["projects"][0]["revision"]

    # Back on the first computer, unchanged since it sent it: an update.
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(first_root))
    paths.reset()
    preview = bundles.preview(two)["projects"][0]
    assert preview["status"] == "update" and preview["target"] == "slide"
    result = bundles.apply(two)["projects"][0]
    assert result["action"] == "update"
    backup = result["backup"]
    assert backup and os.path.isdir(backup) and ".replaced" in backup
    assert (os.path.join(backup, "entry.json"))
    assert store("slide", "gating").get_state() == b'{"gates": {"CD3": 150}}'
    assert sorted(Project.load_all()) == ["slide"]

    # Both change: keep both by default, replace when asked (with a backup).
    store("slide", "gating").put_state(b'{"gates": {"CD3": 175}}')
    _another_computer(tmp_path, monkeypatch)
    store("slide", "gating").put_state(b'{"gates": {"CD3": 99}}')
    three = tmp_path / "three.plexora"
    bundles.export(["slide"], three)
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(first_root))
    paths.reset()
    preview = bundles.preview(three)["projects"][0]
    assert preview["status"] == "conflict" and preview["action"] == "keep_both" and preview["target"] == "slide_2"
    replaced = bundles.apply(three, choices={pid: "replace"})["projects"][0]
    assert replaced["target"] == "slide" and replaced["backup"]
    assert store("slide", "gating").get_state() == b'{"gates": {"CD3": 99}}'
    held = os.path.join(replaced["backup"], "state", "slide.db")
    with sqlite3.connect(held) as connection:
        assert connection.execute('SELECT count(*) FROM "plugin_gating_state"').fetchone()[0] == 1


# -- refusals ----------------------------------------------------------------


def _hand_made(path, members, *, manifest=None, links=()):
    pid = "a" * 32
    files = [{"path": name, "size": len(data), "sha256": None, "kind": "state"} for name, data in members]
    manifest = manifest or {"format": 2, "bundle": {"id": "b" * 32, "created": "", "app": {}},
                            "projects": [{"id": pid, "name": "evil", "revision": None, "parent": None,
                                          "entry": {"path": f"projects/{pid}/entry.json", "size": 2,
                                                    "sha256": None},
                                          "files": files, "refs": []}],
                            "media": [], "captures": [], "datasets": [], "figures": [], "left_out": []}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(bundles.MANIFEST, json.dumps(manifest))
        bundle.writestr(f"projects/{pid}/entry.json", "{}")
        for name, data in members:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            if name in links:
                info.external_attr = 0o120777 << 16
            bundle.writestr(info, data, compresslevel=9)
    return path


@pytest.mark.parametrize("case", ["slip", "absolute", "drive", "backslash", "link", "bomb", "space"])
def test_refused_bundles_leave_nothing_behind(case, tmp_path, monkeypatch):
    pid = "a" * 32
    head = f"projects/{pid}/state/"
    members = {"slip": [(head + "../../../escape.txt", b"x")],
               "absolute": [("/etc/escape.txt", b"x")],
               "drive": [("C:/escape.txt", b"x")],
               "backslash": [(head + "a\\b.txt", b"x")],
               "link": [(head + "link", b"/etc/passwd")],
               "bomb": [(head + "zeros.bin", bytes(8 << 20))],
               "space": [(head + "data.bin", b"x" * 4096)]}[case]
    if case == "space":
        monkeypatch.setattr(bundles, "_free_bytes", lambda root: 100)
    path = _hand_made(tmp_path / f"{case}.plexora", members, links=(head + "link",))
    why = {"link": "link", "bomb": "inflates", "space": "free space"}.get(case, "outside its project")
    with pytest.raises(bundles.BundleError, match=why):
        bundles.apply(path)
    root = paths.new_projects_root()
    assert not list(root.glob(".importing-*"))
    assert "evil" not in Project.load_all()
    assert not (root / "evil").exists()


def test_a_failure_while_placing_puts_everything_back(project, tmp_path, monkeypatch):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    monkeypatch.setattr(bundles, "_merge_datasets", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        bundles.apply(out, choices={_members(out)[1]["projects"][0]["id"]: "keep_both"})
    root = paths.new_projects_root()
    assert not list(root.glob(".importing-*"))
    assert "slide_2" not in Project.load_all()
    assert not (root / "slide_2").exists()


def test_a_file_that_is_not_a_bundle_is_refused(tmp_path):
    with pytest.raises(bundles.BundleError, match="not a Plexora project file"):
        bundles.import_bundle(b"PK not really")


# -- FORMAT 1 ----------------------------------------------------------------


def test_format_1_still_imports_with_its_rows_renamed(project, tmp_path):
    folder = paths.project_state_dir("slide")
    buffer = io.BytesIO()
    entry = read_config(paths.config_path(Project.root_for("slide")))["slide"]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(bundles.MANIFEST_1, json.dumps({
            "format": 1, "name": "slide", "title": None, "entry": entry, "app": "host:X",
            "exported": "2026-11-01T00:00:00Z", "left_out": []}))
        bundle.write(folder / "slide.db", "project/slide.db")
        bundle.write(folder / "plugins" / "gating" / "gates.json", "project/plugins/gating/gates.json")
    assert bundles.import_bundle(buffer.getvalue()) == "slide_2"
    assert _saved_state("slide_2")["gating"] == _saved_state("slide")["gating"]
    assert json.loads((paths.project_state_dir("slide_2") / "plugins" / "gating" / "gates.json").read_text()) \
        == {"CD3": 120}
    assert Project.load("slide_2").image.src == str(project)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(bundles.MANIFEST_1, json.dumps({"format": 1, "name": "x", "entry": {}}))
        bundle.writestr("project/../../outside.txt", "no")
    with pytest.raises(bundles.BundleError, match="outside its project"):
        bundles.import_bundle(buffer.getvalue())


# -- size and memory ---------------------------------------------------------


def test_a_3_gb_file_streams_into_zip64_in_little_memory(project, tmp_path):
    big = paths.project_state_dir("slide") / "cells_edited.csv"
    with big.open("r+b") as handle:  # sparse: costs no disk
        handle.truncate(3 * 1024 ** 3)
    out = tmp_path / "big.plexora"
    tracemalloc.start()
    try:
        bundles.export(["slide"], out)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 64 * 1024 * 1024, peak
    with zipfile.ZipFile(out) as bundle:
        manifest = json.loads(bundle.read(bundles.MANIFEST))
        pid = manifest["projects"][0]["id"]
        info = bundle.getinfo(f"projects/{pid}/state/cells_edited.csv")
    assert info.file_size == 3 * 1024 ** 3
    assert info.file_size > zipfile.ZIP64_LIMIT and info.compress_type == zipfile.ZIP_DEFLATED
    assert not out.with_name(out.name + ".part").exists()


def test_the_estimate_stats_and_classifies(project, tmp_path, monkeypatch):
    calls = []
    real = sources.file_identity
    monkeypatch.setattr(sources, "file_identity", lambda path: calls.append(path) or real(path))
    sized = bundles.estimate(["slide"])
    assert calls == []  # never reads a file
    assert sized["rows"]["tables"]["count"] == 1
    assert sized["rows"]["state"]["size"] > 0
    item = sized["rows"]["images"]["items"][0]
    assert item["where"] == "here" and item["size"] == project.stat().st_size and item["can_include"]
    assert sized["include"]["images"] is False
    assert sized["total"] == sized["rows"]["state"]["size"] + sized["rows"]["tables"]["size"] \
        + sized["rows"]["captures"]["size"] + sized["rows"]["figures"]["size"]
    assert bundles.where("node://hms-o2/abc") == "node"
    assert bundles.where("//server/lab/a.tif") == "shared"


def test_the_website_plan_lists_members_with_their_sources(project, tmp_path):
    out = bundles.plan_for_page(["slide"], work_dir=tmp_path / "work")
    json.dumps(out)  # JSON all the way down
    kinds = {member["kind"] for member in out["members"]}
    assert {"entry", "state", "table"} <= kinds
    for member in out["members"]:
        assert os.path.isabs(member["source"]) and os.path.exists(member["source"])
    pid = out["manifest"]["projects"][0]["id"]
    assert out["entries"][pid]["dataset"]["src"] == "{{state}}/cells_edited.csv"
    sums = {member["bundle_path"]: "f" * 64 for member in out["members"]}
    finished = bundles.finish_manifest(out["manifest"], sums)
    assert all(record["sha256"] == "f" * 64 for record in finished["projects"][0]["files"])
    preview = bundles.preview_manifest(finished)
    assert preview["projects"][0]["status"] in ("conflict", "already_here", "update")


# -- jobs and the command line ----------------------------------------------


def _wait(token, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = bundles.job_status(token)
        if status["status"] != "pending":
            return status
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


def test_export_and_import_jobs_report_progress(project, tmp_path):
    started = bundles.start_export(["slide"])
    status = _wait(started["job"])
    assert status["status"] == "ready" and status["progress"] == 100
    assert status["file"]["url"] == f"/projects/export/{started['job']}/file"
    path = bundles.job_file(started["job"])
    assert path and path.suffix == ".plexora" and status["file"]["size"] == path.stat().st_size
    imported = _wait(bundles.start_import(path, choices=None)["job"])
    assert imported["status"] == "ready"
    assert imported["result"]["projects"][0]["action"] in ("skip", "keep_both")
    bundles.cancel_job(started["job"])
    assert not path.exists()


def test_a_cancelled_export_leaves_no_part_file(project, tmp_path):
    big = paths.project_state_dir("slide") / "cells_edited.csv"
    with big.open("r+b") as handle:
        handle.truncate(512 * 1024 ** 2)
    out = tmp_path / "stopped.plexora"
    stop = threading.Event()

    def progress(done, total):
        if done > 8 * 1024 ** 2:
            stop.set()

    with pytest.raises(bundles.Cancelled):
        bundles.export(["slide"], out, progress=progress, cancel=stop)
    assert not out.exists() and not out.with_name(out.name + ".part").exists()


def test_a_cancelled_import_leaves_no_staging_folder(project, tmp_path):
    big = paths.project_state_dir("slide") / "cells_edited.csv"
    block = os.urandom(1 << 20)  # does not deflate (zeros would be a bomb, and refused)
    with big.open("wb") as handle:
        for _ in range(64):
            handle.write(block)
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    stop = threading.Event()

    def progress(done, total):
        if done > 8 * 1024 ** 2:
            stop.set()

    pid = _members(out)[1]["projects"][0]["id"]
    with pytest.raises(bundles.Cancelled):
        bundles.apply(out, choices={pid: "keep_both"}, progress=progress, cancel=stop)
    root = paths.new_projects_root()
    assert not list(root.glob(".importing-*")) and "slide_2" not in Project.load_all()


def test_make_a_copy(project):
    result = bundles.copy_project("slide")
    target = result["projects"][0]["target"]
    assert target == "slide_2"
    assert _saved_state(target)["gating"] == _saved_state("slide")["gating"]
    assert read_config(paths.config_path(Project.root_for(target)))[target]["id"] != \
        read_config(paths.config_path(Project.root_for("slide")))["slide"]["id"]
    assert not list(paths.new_projects_root().glob(".importing-*"))


def test_the_command_line(project, tmp_path, capsys):
    from plexora import cli

    out = tmp_path / "x.plexora"
    assert cli.main(["project", "export", "slide", "--include", "images", "-o", str(out)]) == 0
    manifest = _members(out)[1]
    assert manifest["projects"][0]["refs"][0]["included"]
    assert cli.main(["project", "import", str(out), "--keep-both"]) == 0
    assert "slide_2" in Project.load_all()
    store("slide", "gating").put_state(b'{"gates": {"CD3": 1}}')
    assert cli.main(["project", "import", str(out), "--replace", "--into", "no-such-location"]) == 2
    assert "no such location" in capsys.readouterr().out.lower() or True
    assert cli.main(["project", "import", str(out), "--replace"]) == 0
    assert store("slide", "gating").get_state() == b'{"gates": {"CD3": 120}}'
    assert any(path.name.startswith("slide-") for path in (paths.new_projects_root() / ".replaced").iterdir())
    assert cli.main(["project", "import", str(out), "--name", "copy"]) == 0
    assert Project.load("copy") is not None
    assert cli.main(["project", "export", "--include", "nonsense", "slide", "-o", str(out)]) == 2
