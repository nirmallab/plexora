"""One project, two apps' paths (server/models/sources.py).

The website and the desktop app can share one config.json. Each must read its
own paths for a project's files, and every save -- by either -- must keep the
other's: a desktop save that wrote D:/ paths over the website's /picked/ ones
would break the project on the website, and the reverse.
"""

import json

import pytest

from plexora.server.models import project as project_module
from plexora.server.models import sources

WEB = "/picked/ab12/slide.tif"
PYRAMID = "/derived/k1/slide.pyramid.ome.tiff"
HERE = "D:/data/slide.tif"


def _entry():
    return {
        "channelFile": WEB,
        "spatialLayers": [{"src": WEB, "label": "slide"}, {"src": "/picked/ab12/other.tif"}],
        "dataset": {"src": "/picked/cd34/cells.csv"},
        "sources": {
            WEB: {"name": "slide.tif", "size": 10, "at": {"website": WEB, "host:LAB": HERE}},
            "/picked/cd34/cells.csv": {"name": "cells.csv", "size": 5, "at": {"website": "/picked/cd34/cells.csv"}},
        },
    }


def test_each_app_reads_its_own_paths():
    seen = sources.localize(_entry(), "host:LAB")
    assert seen["channelFile"] == HERE
    assert seen["spatialLayers"][0]["src"] == HERE
    assert seen["spatialLayers"][1]["src"] == "/picked/ab12/other.tif"   # not found here yet
    assert seen["dataset"]["src"] == "/picked/cd34/cells.csv"
    assert seen["sources"] == _entry()["sources"]                        # the map is never rewritten
    assert sources.localize(_entry(), "website") == _entry()
    assert sources.localize(_entry(), "host:OTHER") == _entry()


def test_a_save_keeps_the_recorded_paths():
    for me in ("host:LAB", "website", "host:OTHER"):
        assert sources.record(sources.localize(_entry(), me), me) == _entry()


def test_the_website_records_what_it_has_not_yet(tmp_path):
    entry = {"channelFile": PYRAMID, "dataset": {"src": WEB}}
    facts = {WEB: {"name": "slide.tif", "size": 10}, PYRAMID: {"name": "slide.pyramid.ome.tiff", "size": 4,
                                                             "key": "k1", "from": WEB}}
    written = sources.record(entry, "website", identify=lambda path: facts.get(path))
    assert written["sources"][WEB] == {"name": "slide.tif", "size": 10, "at": {"website": WEB}}
    assert written["sources"][PYRAMID]["from"] == WEB
    again = sources.record(written, "website", identify=lambda path: pytest.fail("identified twice"))
    assert again == written


def test_config_json_round_trips_through_both_apps(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"slide": _entry()}), encoding="utf-8")
    monkeypatch.setattr(sources, "app_key", lambda: "host:LAB")
    config = project_module.read_config(path)
    assert config["slide"]["channelFile"] == HERE
    config["slide"]["title"] = "Renamed on the desktop"
    project_module.write_config(path, config)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["slide"]["channelFile"] == WEB
    assert stored["slide"]["title"] == "Renamed on the desktop"
    monkeypatch.setattr(sources, "app_key", lambda: "website")
    monkeypatch.setattr(sources, "identity", lambda p: None)
    assert project_module.read_config(path)["slide"]["channelFile"] == WEB


def test_identity_and_the_same_file(tmp_path):
    original = tmp_path / "a" / "slide.tif"
    original.parent.mkdir()
    original.write_bytes(b"x" * (3 << 20))
    facts = sources.file_identity(original)
    assert facts["name"] == "slide.tif" and facts["size"] == 3 << 20
    copy = tmp_path / "b" / "slide.tif"
    copy.parent.mkdir()
    copy.write_bytes(original.read_bytes())          # a copy: same bytes, new time
    assert sources.same_file(facts, copy)
    other = tmp_path / "c" / "slide.tif"
    other.parent.mkdir()
    other.write_bytes(b"y" * (3 << 20))               # same name and size, other bytes
    assert not sources.same_file(facts, other)
    assert not sources.same_file(facts, tmp_path / "missing.tif")
