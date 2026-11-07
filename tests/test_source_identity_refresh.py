"""After a save into a source file, the projects that name it know the file it
now is (server/models/sources.py `refresh`, from server/utils/source_writes.py).

Both apps record each project file's identity -- size, modification time,
the first and last MiB -- so the other app finds the same file on this
computer (server/utils/relink.py) or in a folder the user grants. Saving into
the file (gates into an .h5ad, ROI or QC columns onto a table) changes all of
that: without a fresh record the other app took the updated file for a
different one and refused it.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from plexora import paths
from plexora.server.models import sources
from plexora.server.utils import source_writes

WEB = "/picked/ab12/cells.h5ad"


def _facts(path, at):
    facts = sources.file_identity(path)
    facts["at"] = at
    return facts


def _append(path, data=b"gates"):
    with open(path, "ab") as handle:
        handle.write(data)


def test_refresh_makes_the_identity_again_where_this_app_knows_the_file(tmp_path):
    cells = tmp_path / "cells.h5ad"
    cells.write_bytes(b"a" * 3000)
    other = tmp_path / "other.h5ad"
    other.write_bytes(b"b" * 10)
    config = {"p": {"dataset": {"src": str(cells)}, "sources": {
        WEB: _facts(cells, {"website": WEB, "host:LAB": str(cells)}),
        "/picked/cd34/other.h5ad": _facts(other, {"website": "/picked/cd34/other.h5ad", "host:LAB": str(other)})}}}
    before = json.loads(json.dumps(config))
    _append(cells)
    assert not sources.same_file(config["p"]["sources"][WEB], cells)
    assert sources.refresh(config, str(cells), "host:LAB")
    assert sources.same_file(config["p"]["sources"][WEB], cells)
    assert config["p"]["sources"][WEB]["at"] == before["p"]["sources"][WEB]["at"]
    assert config["p"]["sources"]["/picked/cd34/other.h5ad"] == before["p"]["sources"]["/picked/cd34/other.h5ad"]
    assert not sources.refresh(config, str(cells), "host:LAB")                 # nothing new
    assert not sources.refresh(config, str(tmp_path / "unnamed.h5ad"), "host:LAB")
    assert not sources.refresh(config, str(cells), "host:OTHER")                # not this computer's path
    _append(cells)
    assert sources.refresh(config, cells.as_posix(), "host:LAB")              # either spelling of the path
    assert sources.same_file(config["p"]["sources"][WEB], cells)


def test_the_website_refreshes_by_its_own_path(tmp_path):
    cells = tmp_path / "cells.h5ad"
    cells.write_bytes(b"a" * 3000)
    config = {"p": {"sources": {WEB: _facts(cells, {"website": WEB})}}}
    _append(cells)
    assert sources.refresh(config, WEB, "website", identify=lambda path: sources.file_identity(cells))
    assert config["p"]["sources"][WEB]["size"] == 3005


@pytest.fixture
def library(tmp_path, monkeypatch):
    """The user's own data root and the website's library beside it."""
    own = tmp_path / "own"
    lib = tmp_path / "Documents" / "Plexora" / "projects"
    for root in (own, lib):
        root.mkdir(parents=True)
        (root / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(own))
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(lib))
    paths.reset()
    yield own, lib
    paths.reset()


def test_a_save_on_the_desktop_updates_the_shared_library(tmp_path, library):
    _, lib = library
    cells = tmp_path / "data" / "cells.h5ad"
    cells.parent.mkdir()
    cells.write_bytes(b"x" * 5000)
    (lib / "config.json").write_text(json.dumps({"p": {"dataset": {"src": WEB}, "sources": {
        WEB: _facts(cells, {"website": WEB, sources.app_key(): str(cells)})}}}), encoding="utf-8")
    with source_writes.writable(str(cells)) as target:
        assert target == str(cells)
        _append(target)
    stored = json.loads((lib / "config.json").read_text(encoding="utf-8"))
    assert sources.same_file(stored["p"]["sources"][WEB], cells)
    assert stored["p"]["dataset"]["src"] == WEB                       # the recorded paths kept
    assert stored["p"]["sources"][WEB]["at"] == {"website": WEB, sources.app_key(): str(cells)}


def test_a_failed_save_records_nothing(tmp_path, library):
    _, lib = library
    cells = tmp_path / "cells.h5ad"
    cells.write_bytes(b"x" * 5000)
    recorded = {"p": {"sources": {WEB: _facts(cells, {"website": WEB, sources.app_key(): str(cells)})}}}
    (lib / "config.json").write_text(json.dumps(recorded), encoding="utf-8")
    with pytest.raises(RuntimeError):
        with source_writes.writable(str(cells)):
            raise RuntimeError("the writer failed before writing")
    assert json.loads((lib / "config.json").read_text(encoding="utf-8")) == recorded


def test_a_save_on_the_website_updates_the_project_when_the_page_has_it(tmp_path, library, monkeypatch):
    """The worker swaps the saved bytes in for the picked file (here: the
    page's stand-in writes them there); the record then describes them."""
    own, _ = library
    picked = tmp_path / "picked"
    original = picked / "ab12" / "cells.h5ad"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"y" * 4000)
    os.chmod(original, stat.S_IREAD)
    path = original.as_posix()          # the website's paths: always forward slashes
    (own / "config.json").write_text(json.dumps({"p": {"sources": {
        path: _facts(original, {"website": path})}}}), encoding="utf-8")
    monkeypatch.setattr(source_writes, "on_website", lambda: True)
    monkeypatch.setattr(sources, "app_key", lambda: "website")

    def deliver(source, staged):
        os.chmod(source, stat.S_IWRITE | stat.S_IREAD)
        Path(source).write_bytes(Path(staged).read_bytes())
        return True

    source_writes.configure(root=str(picked), ask=lambda kind, args: True, deliver=deliver)
    try:
        with source_writes.batch():
            with source_writes.writable(path) as staged:
                assert staged != path
                _append(staged)
        stored = json.loads((own / "config.json").read_text(encoding="utf-8"))
        assert stored["p"]["sources"][path]["size"] == 4005
        assert sources.same_file(stored["p"]["sources"][path], original)
    finally:
        source_writes.reset()
