"""config.json's project names, kept until the file changes (project.config_names).

Every page's context lists the projects (page_routes.template_data), and that
parsed every root's whole config.json twice a request: 5 ms for a 1.6 MB
config of 127 projects on the desktop, several times that in the website's
Python. The names are kept per file until this process writes a record or the
file's size or mtime moves -- never a stale list.
"""

import json
import os

import plexora
from plexora import paths
from plexora.server.models import project


def _config(root):
    return paths.config_path(root)


def test_a_write_here_is_seen_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path))
    project.write_config(_config(tmp_path), {"a": {}, "b": {}})
    assert project.config_names(_config(tmp_path)) == ["a", "b"]
    project.write_config(_config(tmp_path), {"a": {}, "c": {}})
    assert project.config_names(_config(tmp_path)) == ["a", "c"]
    assert plexora.get_config_names() == ["a", "c"]


def test_another_process_writing_the_file_is_seen(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path))
    path = _config(tmp_path)
    project.write_config(path, {"a": {}})
    assert project.config_names(path) == ["a"]
    # Same size, so only the mtime says it changed.
    path.write_text(json.dumps({"b": {}}, indent=4), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert project.config_names(path) == ["b"]


def test_unchanged_it_is_not_read_again(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path))
    path = _config(tmp_path)
    project.write_config(path, {"a": {}})
    project.config_names(path)
    reads = []
    real = project.read_config
    monkeypatch.setattr(project, "read_config", lambda p: reads.append(p) or real(p))
    names = project.config_names(path)
    names.append("mutated by a caller")
    assert project.config_names(path) == ["a"] and reads == []


def test_no_file_is_no_names(tmp_path):
    assert project.config_names(tmp_path / "config.json") == []


def test_one_entry_is_a_copy_and_follows_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path))
    path = _config(tmp_path)
    project.write_config(path, {"a": {"title": "first"}})
    entry = project.read_entry(path, "a")
    entry["title"] = "changed by a caller"
    assert project.read_entry(path, "a")["title"] == "first"
    project.write_config(path, {"a": {"title": "second"}})
    assert project.read_entry(path, "a")["title"] == "second"
    try:
        project.read_entry(path, "missing")
    except KeyError:
        pass
    else:
        raise AssertionError("an unknown name is a KeyError")
