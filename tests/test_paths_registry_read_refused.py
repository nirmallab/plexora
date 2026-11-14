"""A library project's state stays in the library past one refused registry read.

`paths.project_state_dir` answers from the library's config.json, cached by
the file's (mtime, size). On Windows a read of that file is refused for a few
milliseconds while somebody replaces it (project.write_config's os.replace, a
scanner holding it), which `project.read_config` waits out. A refusal is not
"this library holds no projects": once it clears, the project's state -- its
sqlite database of channel lists and plugin tables -- must still be found in
the library, not in a fresh, empty folder in the data root.
"""

import json
import pathlib

import pytest

from plexora import paths
from plexora.server.models import database_model


def _root(path, *names):
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps({name: {"channelFile": f"/picked/1/{name}.tif"}
                                                  for name in names}), encoding="utf-8")
    return path


@pytest.fixture
def library(tmp_path, monkeypatch):
    own = _root(tmp_path / "own", "desktop_project")
    lib = _root(tmp_path / "Documents" / "Plexora" / "projects", "refused_web_project")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(own))
    monkeypatch.setenv("PLEXORA_LIBRARY_PATH", str(lib))
    paths.reset()
    yield own.resolve(), lib.resolve()
    paths.reset()


def _refuse_once(monkeypatch, target):
    """Path.read_text refuses `target` once, as Windows does mid-replace."""
    original = pathlib.Path.read_text
    refused = []

    def read_text(self, *args, **kwargs):
        if not refused and pathlib.Path(self).resolve() == target:
            refused.append(self)
            raise PermissionError(13, "The process cannot access the file", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    return refused


def test_a_refused_read_of_the_library_registry_is_not_remembered(library, monkeypatch):
    own, lib = library
    refused = _refuse_once(monkeypatch, lib / "config.json")
    paths.project_state_dir("refused_web_project")            # meets the refusal
    assert refused, "the library's registry was never read"
    # The refusal has cleared and config.json has not changed: the project
    # is still the library's, and so is its state.
    assert paths.project_state_dir("refused_web_project") == lib / "refused_web_project"


def test_a_library_projects_database_is_found_after_a_refused_read(library, monkeypatch):
    own, lib = library
    saved = database_model.save_list(database_model.ChannelList, "refused_web_project", "[1, 2, 3]")
    assert saved.cells == "[1, 2, 3]"
    assert (lib / "refused_web_project" / "refused_web_project.db").is_file()
    # The library's registry is rewritten (a project opened, say), and the
    # first read of the new file is refused.
    _root(lib, "refused_web_project", "another_web_project")
    refused = _refuse_once(monkeypatch, lib / "config.json")
    paths.project_state_dir("refused_web_project")            # meets the refusal
    assert refused
    row = database_model.get(database_model.ChannelList, "refused_web_project")
    assert row is not None and row.cells == "[1, 2, 3]"
    assert not (own / "refused_web_project" / "refused_web_project.db").exists()
