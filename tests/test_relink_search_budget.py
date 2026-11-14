"""Looking for a project's missing files (server/utils/relink.py) keeps to
its time budget and does not search again for what it just failed to find.

`resolve` is handed a budget (`walk_seconds`: WALK_SECONDS at every open,
bundles.FIND_SECONDS on import), and `relink.ensure` runs inside
`data_model.load_config`, under `load_lock`, on every open of a project
whose `sources` name a file not found here. One query of the system's file
index is a PowerShell process on Windows (about 2 s), so a lookup that asks
the index once per missing file whatever the budget, and again at every
open, holds every tile request behind it. The clock and the index here are
fakes: the tests count queries, they never time anything.
"""

import json
import types

import pytest

from plexora.server.models import sources
from plexora.server.utils import relink


@pytest.fixture
def nowhere(tmp_path, monkeypatch):
    """A user with empty folders, and a clock that only the fake index moves."""
    documents = tmp_path / "Documents"
    for name in ("Documents", "Desktop", "Downloads", "Pictures"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(relink, "documents_dir", lambda: documents)
    monkeypatch.setattr(relink.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(relink, "_user_folders", lambda: [])
    clock = {"now": 1000.0}
    monkeypatch.setattr(relink, "time", types.SimpleNamespace(monotonic=lambda: clock["now"]))
    return clock


def _missing_entry(count):
    """An entry whose files were recorded on another computer and are not here."""
    return {"sources": {f"/elsewhere/run/slide{i}.tif": {"name": f"slide{i}.tif", "size": 5000 + i,
                                                          "mtime_ms": 1_700_000_000_000, "at": {}}
                        for i in range(count)}}


def test_index_queries_stop_at_the_budget(nowhere):
    """Ten missing files, an index query that takes 2 s and a 5 s budget: the
    lookup stops asking the index once the budget is spent (at most three
    queries), rather than asking for every file (20 s)."""
    asked = []

    def index(name):
        asked.append(name)
        nowhere["now"] += 2.0
        return []

    assert relink.resolve(_missing_entry(10), "host:LAB", walk_seconds=5.0, index=index) == {}
    assert len(asked) <= 3, f"{len(asked)} index queries ({2 * len(asked):.0f} s) for a 5 s budget"


def test_patching_the_module_index_reaches_resolve(nowhere, monkeypatch):
    """`resolve` looks up `_index` when it runs, so a test (or a caller) that
    replaces relink._index is the one asked -- not the PowerShell query bound
    when the module was imported."""
    asked = []
    monkeypatch.setattr(relink, "_index", lambda name: asked.append(name) or [])
    ran = []
    monkeypatch.setattr(relink.subprocess, "run", lambda *args, **kwargs: ran.append(args) or
                        types.SimpleNamespace(stdout="", returncode=0))
    relink.resolve(_missing_entry(1), "host:LAB", walk_seconds=0)
    assert asked == ["slide0.tif"]
    assert ran == [], "the real system-index query ran although relink._index was replaced"


def test_a_second_open_does_not_search_again_for_a_file_just_not_found(nowhere, monkeypatch, tmp_path):
    """`ensure` runs at every open (under load_lock). A file it searched for
    and did not find is not searched for again by the very next open: no
    second index query and no second walk of the user's folders."""
    root = tmp_path / "root"
    root.mkdir()
    entry = {"channelFile": "/elsewhere/run/slide0.tif", **_missing_entry(1)}
    (root / "config.json").write_text(json.dumps({"slide": entry}), encoding="utf-8")
    monkeypatch.setattr(sources, "app_key", lambda: "host:LAB")
    from plexora import paths
    from plexora.server.models.project import Project

    monkeypatch.setattr(Project, "root_for", staticmethod(lambda name: root))
    monkeypatch.setattr(paths, "config_path", lambda r=None: root / "config.json")
    monkeypatch.setattr(relink, "_project_folders", lambda me: [])
    asked, walked = [], []
    index = lambda name: asked.append(name) or []  # noqa: E731
    # Both ways resolve may find its index: the module attribute, and the
    # default it was defined with.
    monkeypatch.setattr(relink, "_index", index)
    if "index" in (relink.resolve.__kwdefaults__ or {}):
        monkeypatch.setitem(relink.resolve.__kwdefaults__, "index", index)
    real_walk = relink._walk
    monkeypatch.setattr(relink, "_walk", lambda roots, wanted, deadline: walked.append(set(wanted)) or
                        real_walk([], wanted, deadline))

    assert relink.ensure("slide") == 0
    first = (len(asked), len(walked))
    assert first[0] >= 1                                                            # it did look once
    assert relink.ensure("slide") == 0
    assert (len(asked), len(walked)) == first, (
        f"the second open searched again: index queries {first[0]} -> {len(asked)}, "
        f"walks {first[1]} -> {len(walked)}")
