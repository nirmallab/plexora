"""A project's files opened again on a later visit to the website, or found by
the user (client services/reopenFiles.js over services/pickedFiles.js).

The dialogs and the picks are in tests/js/reopen_files_probe.mjs, run in node
below. What is left here is Python's half (server/utils/relink.py and the
`/project/<name>/files_here` routes): what the page is told about each
missing file -- where the website last had it, what it is to the project --
and what it may record: the same file under another name, and a different
file only when the user chose to use it anyway, which then becomes the file
every app looks for.
"""

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import tifffile

import plexora
from plexora import paths
from plexora.server.models import sources
from plexora.server.models.project import Project, config_transaction, read_config, write_config
from plexora.server.utils import relink

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "reopen_files_probe.mjs"

IMAGE = "/picked/ab12/slide.ome.tif"
MASK = "/picked/cd34/slide_mask.tif"
TABLE = "/picked/ef56/cells.csv"
DESKTOP = "/Users/someone/Data/LSP/layer.tif"

#: Every line the probe prints, so a check deleted or renamed fails here
#: rather than passing by not running.
CHECKS = (
    "status says why a pick is not open: prompt, denied, moved, no handle, none",
    "reconnect asks only for the ids given, and one refusal stops the asking",
    "reconnect without asking mounts only what the browser grants already",
    "two reconnects at once run one after the other, and status waits for them",
    "find takes a renamed file by its size and date, never a different one",
    "find answers null when its dialog is cancelled",
    "chooseFile says whether the chosen file is the same one, and keep mounts it",
    "the browser grants the files already: they open with no dialog",
    "it asks again: one Allow access, and every file the browser lists opens",
    "Allow access refused: the files are found by the user instead, said why",
    "Choose the files myself goes straight to finding them",
    "Firefox and Safari: one file described by what was recorded, found in the file dialog",
    "a renamed file is taken, a different one is shown beside the original",
    "Use this file anyway records it in the original's place",
    "several files: a folder search, then Locate for the one still missing",
    "a desktop project's files show where the desktop app had them",
    "the X and Escape open the project without what is missing",
    "a pyramid's folder that waits is opened by the same Allow access",
    "nothing is drawn when nothing is missing, and names are text, not markup",
)


def _image(path, value=0):
    data = ((np.arange(64 * 64) + value) % 4000).astype(np.uint16).reshape(1, 64, 64)
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, data, photometric="minisblack")
    return path


def _facts(path, name=None):
    facts = sources.file_identity(path)
    if name:
        facts["name"] = name
    return facts


@pytest.fixture
def website_project(tmp_path, monkeypatch):
    """A project the website made: its image, mask and cell table as the page
    knew them (`/picked/<id>/...`), with the identities it recorded; the files
    themselves on this disk under tmp_path, which the website's Python does not
    see at those paths."""
    image = _image(tmp_path / "disk" / "slide.ome.tif")
    mask = _image(tmp_path / "disk" / "slide_mask.tif", value=7)
    table = tmp_path / "disk" / "cells.csv"
    table.write_text("CellID,X,Y\n1,2,3\n")
    plexora.register_image_datasource("slide", str(image))
    monkeypatch.setattr(sources, "app_key", lambda: "website")
    root = Project.root_for("slide")
    with config_transaction():
        config = read_config(paths.config_path(root))
        entry = config["slide"]
        entry.update(channelFile=IMAGE, segmentation=MASK,
                     dataset={"type": "csv", "src": TABLE}, title="LSP cohort")
        entry["sources"] = {IMAGE: {**_facts(image), "at": {"website": IMAGE}},
                            MASK: {**_facts(mask), "at": {"website": MASK}},
                            TABLE: {**_facts(table), "at": {"website": TABLE}}}
        write_config(paths.config_path(root), config)
    return {"image": image, "mask": mask, "table": table, "dir": tmp_path / "disk"}


def _sources():
    root = Project.root_for("slide")
    return read_config(paths.config_path(root))["slide"]["sources"]


def test_each_missing_file_says_where_the_website_had_it_and_what_it_is(website_project):
    missing = {item["recorded"]: item for item in relink.missing_here("slide")}
    assert set(missing) == {IMAGE, MASK, TABLE}
    assert missing[IMAGE]["here"] == IMAGE
    assert (missing[IMAGE]["role"], missing[MASK]["role"], missing[TABLE]["role"]) == ("image", "mask", "table")
    assert missing[IMAGE]["kind"] == "file" and missing[IMAGE]["made"] is False
    assert missing[IMAGE]["size"] == website_project["image"].stat().st_size
    assert relink.title_of("slide") == "LSP cohort"


def test_a_desktop_path_and_a_pyramid_say_so(website_project, tmp_path):
    root = Project.root_for("slide")
    pyramid = "/derived/k1/slide.pyramid.ome.tiff"
    with config_transaction():
        config = read_config(paths.config_path(root))
        entry = config["slide"]
        entry["spatialLayers"] = [{"id": "l1", "kind": "image", "path": DESKTOP}]
        entry["imagePyramid"] = pyramid
        entry["sources"][DESKTOP] = {"name": "layer.tif", "size": 10, "mtime_ms": 1, "at": {}}
        entry["sources"][pyramid] = {"name": "slide.pyramid.ome.tiff", "size": 10, "mtime_ms": 1,
                                     "from": IMAGE, "key": "k1", "at": {"website": pyramid}}
        write_config(paths.config_path(root), config)
    missing = {item["recorded"]: item for item in relink.missing_here("slide")}
    assert missing[DESKTOP]["here"] is None and missing[DESKTOP]["role"] == "layer"
    assert missing[pyramid]["made"] is True and missing[pyramid]["from_name"] == "slide.ome.tif"
    assert missing[pyramid]["role"] == "image"


def test_the_same_file_under_another_name_is_recorded(website_project, tmp_path):
    renamed = tmp_path / "elsewhere" / "renamed.tif"
    renamed.parent.mkdir()
    shutil.copy2(website_project["image"], renamed)
    assert relink.record_found("slide", {IMAGE: str(renamed)}) == 1
    assert _sources()[IMAGE]["at"]["website"] == str(renamed)
    # The project's identity is the original's still: nothing was replaced.
    assert _sources()[IMAGE]["name"] == "slide.ome.tif"
    assert IMAGE not in {item["recorded"] for item in relink.missing_here("slide")}


def test_a_different_file_only_when_the_user_says_so_and_then_it_is_the_one(website_project, tmp_path):
    other = _image(tmp_path / "elsewhere" / "slide.ome.tif", value=99)
    os.utime(other, (1_700_000_000, 1_700_000_000))
    assert relink.record_found("slide", {IMAGE: str(other)}) == 0
    assert _sources()[IMAGE]["at"] == {"website": IMAGE}
    # Desktop's path for it too, to see it dropped.
    root = Project.root_for("slide")
    with config_transaction():
        config = read_config(paths.config_path(root))
        config["slide"]["sources"][IMAGE]["at"]["host:LAB"] = "D:/slide.ome.tif"
        write_config(paths.config_path(root), config)
    assert relink.record_found("slide", {IMAGE: str(other)}, anyway=[IMAGE]) == 1
    info = _sources()[IMAGE]
    assert info["at"] == {"website": str(other)}
    assert info["size"] == other.stat().st_size and info["mtime_ms"] == 1_700_000_000_000
    assert info["head"] == sources.file_identity(other)["head"]
    assert IMAGE not in {item["recorded"] for item in relink.missing_here("slide")}


def _raw_entry():
    import json
    return json.loads(paths.config_path(Project.root_for("slide")).read_text())["slide"]


def test_a_file_found_a_second_time_is_the_one_the_project_names(website_project, tmp_path):
    """Found once at A, then at B: the entry's values stay the recorded path on
    disk and read as B. (read_config gives this app's paths; writing with `at`
    moved from A to B used to leave A in the values -- a project naming a file
    that was no longer there. Seen on the website: renamed copy, then "Use this
    file anyway", and the viewer opened the renamed copy's old path.)"""
    first = tmp_path / "first" / "slide.ome.tif"
    first.parent.mkdir()
    shutil.copy2(website_project["image"], first)
    assert relink.record_found("slide", {IMAGE: str(first)}) == 1
    root = Project.root_for("slide")
    assert read_config(paths.config_path(root))["slide"]["channelFile"] == str(first)
    second = tmp_path / "second" / "renamed.tif"
    second.parent.mkdir()
    shutil.copy2(website_project["image"], second)
    assert relink.record_found("slide", {IMAGE: str(second)}) == 1
    assert _raw_entry()["channelFile"] == IMAGE
    assert read_config(paths.config_path(root))["slide"]["channelFile"] == str(second)
    other = _image(tmp_path / "third" / "slide.ome.tif", value=42)
    os.utime(other, (1_600_000_000, 1_600_000_000))
    assert relink.record_found("slide", {IMAGE: str(other)}, anyway=[IMAGE]) == 1
    assert _raw_entry()["channelFile"] == IMAGE
    assert read_config(paths.config_path(root))["slide"]["channelFile"] == str(other)


def test_the_routes_answer_the_title_and_what_was_refused(website_project, tmp_path):
    other = _image(tmp_path / "elsewhere" / "slide_mask.tif", value=50)
    os.utime(other, (1_600_000_000, 1_600_000_000))
    client = plexora.app.test_client()
    answer = client.get("/project/slide/files_here").get_json()
    assert answer["title"] == "LSP cohort"
    assert {item["recorded"] for item in answer["missing"]} == {IMAGE, MASK, TABLE}
    done = client.post("/project/slide/files_here",
                       json={"found": {IMAGE: str(website_project["image"]), MASK: str(other)}}).get_json()
    assert done["recorded"] == 1
    assert set(done["refused"]) == {MASK}
    assert done["refused"][MASK]["size"] == other.stat().st_size
    assert {item["recorded"] for item in done["missing"]} == {MASK, TABLE}
    anyway = client.post("/project/slide/files_here",
                         json={"found": {MASK: str(other)}, "anyway": [MASK]}).get_json()
    assert anyway["recorded"] == 1 and anyway["refused"] == {}
    assert client.post("/project/slide/files_here", json={"found": [], "anyway": []}).status_code == 400


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_page_half_in_node():
    result = subprocess.run(["node", str(PROBE)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    printed = [line.strip()[4:].strip() for line in result.stdout.splitlines() if line.strip().startswith("ok  ")]
    assert printed == list(CHECKS)
