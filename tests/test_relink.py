"""The desktop app finds a website project's files on this computer
(server/utils/relink.py) -- without asking, and never the wrong file."""

import json
import os
import shutil

import pytest

from plexora.server.models import sources
from plexora.server.utils import relink


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A user's folders, with no system index."""
    documents = tmp_path / "Documents"
    for name in ("Documents", "Desktop", "Downloads", "Pictures"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(relink, "documents_dir", lambda: documents)
    monkeypatch.setattr(relink.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _picked(home, folder, name, body):
    path = home / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _entry(*files):
    known = {}
    for recorded, facts in files:
        known[recorded] = {**facts, "at": {"website": recorded}}
    return {"sources": known}


def test_found_by_a_walk_and_then_beside_it(home):
    image = _picked(home, "Downloads/run7", "slide.tif", b"a" * 5000)
    table = _picked(home, "Downloads/run7", "cells.csv", b"b" * 300)
    entry = _entry(("/picked/1/slide.tif", sources.file_identity(image)),
                   ("/picked/1/cells.csv", sources.file_identity(table)))
    found = relink.resolve(entry, "host:LAB", index=lambda name: [])
    assert found == {"/picked/1/slide.tif": str(image), "/picked/1/cells.csv": str(table)}


def test_the_index_first(home):
    image = _picked(home, "elsewhere/deep", "slide.tif", b"a" * 5000)   # outside the user folders
    entry = _entry(("/picked/1/slide.tif", sources.file_identity(image)))
    assert relink.resolve(entry, "host:LAB", index=lambda name: [image]) == {"/picked/1/slide.tif": str(image)}
    assert relink.resolve(entry, "host:LAB", index=lambda name: [], walk_seconds=1) == {}


def test_a_file_of_the_same_name_that_is_not_it_is_never_taken(home):
    real = home / "scratch.tif"
    real.write_bytes(b"a" * 5000)
    facts = sources.file_identity(real)
    real.unlink()
    _picked(home, "Documents", "slide.tif", b"z" * 5000)                 # same size, other bytes
    entry = _entry(("/picked/1/slide.tif", {**facts, "name": "slide.tif", "mtime_ms": 1}))
    assert relink.resolve(entry, "host:LAB", index=lambda name: []) == {}


def test_a_pyramid_the_website_kept_in_plexoras_folder(home):
    source = _picked(home, "Pictures", "slide.tif", b"a" * 5000)
    copy = _picked(home, "Documents/Plexora/pyramids/k1", "slide.pyramid.ome.tiff", b"p" * 900)
    entry = _entry(("/picked/1/slide.tif", sources.file_identity(source)),
                   ("/derived/k1/slide.pyramid.ome.tiff", {**sources.file_identity(copy), "key": "k1",
                                                            "from": "/picked/1/slide.tif"}))
    found = relink.resolve(entry, "host:LAB", index=lambda name: [])
    assert found["/derived/k1/slide.pyramid.ome.tiff"] == str(copy)


def test_a_pyramid_saved_beside_its_original(home):
    source = _picked(home, "Pictures/run", "slide.tif", b"a" * 5000)
    copy = _picked(home, "Pictures/run", "slide.pyramid.ome.tiff", b"p" * 900)
    entry = _entry(("/picked/1/slide.tif", sources.file_identity(source)),
                   ("/derived/k1/slide.pyramid.ome.tiff", {**sources.file_identity(copy), "key": "k1",
                                                            "from": "/picked/1/slide.tif"}))
    # The source by the index; the pyramid beside it, with no walk at all.
    found = relink.resolve(entry, "host:LAB", index=lambda name: [source] if name == "slide.tif" else [],
                           walk_seconds=0)
    assert found["/derived/k1/slide.pyramid.ome.tiff"] == str(copy)


def test_ensure_records_where_and_the_next_read_uses_it(home, monkeypatch, tmp_path):
    image = _picked(home, "Downloads", "slide.tif", b"a" * 5000)
    root = tmp_path / "root"
    root.mkdir()
    entry = {"channelFile": "/picked/1/slide.tif", **_entry(("/picked/1/slide.tif", sources.file_identity(image)))}
    (root / "config.json").write_text(json.dumps({"slide": entry}), encoding="utf-8")
    monkeypatch.setattr(sources, "app_key", lambda: "host:LAB")
    monkeypatch.setattr(relink, "_index", lambda name: [])
    from plexora.server.models.project import Project, read_config

    monkeypatch.setattr(Project, "root_for", staticmethod(lambda name: root))
    monkeypatch.setattr(relink.paths if hasattr(relink, "paths") else __import__("plexora").paths,
                        "config_path", lambda r=None: root / "config.json")
    assert relink.ensure("slide") == 1
    assert read_config(root / "config.json")["slide"]["channelFile"] == str(image)
    stored = json.loads((root / "config.json").read_text(encoding="utf-8"))
    assert stored["slide"]["channelFile"] == "/picked/1/slide.tif"                # the website's, kept
    assert stored["slide"]["sources"]["/picked/1/slide.tif"]["at"]["host:LAB"] == str(image)
    assert relink.ensure("slide") == 0                                              # nothing left to find


def test_never_on_the_website(monkeypatch):
    monkeypatch.setattr(sources, "app_key", lambda: "website")
    assert relink.ensure("anything") == 0
