"""What a figure leaves on disk: exports from earlier runs, and a half-delete.

* Export jobs are process-local, so a job's folder under ``exports/`` was only
  ever swept by the process that made it. Every export made before a restart
  stayed on disk for good, and Duplicate copied them into the copy too.

* Deleting a figure was a bare ``rmtree``. On Windows a file that is open (a
  preview being served, an export reading an asset) cannot be deleted, and
  rmtree stops there: the imported assets were gone, ``figure.db`` was not, so
  the figure was still listed -- and the request was a 500. The directory is
  now renamed out of the library first, which on Windows fails as a whole while
  anything inside is open, so a delete is all or nothing.
"""

import os
import time

import pytest

from plexora.plugins.figure_builder.server import export_jobs, repository


@pytest.fixture
def figures(tmp_path):
    return tmp_path


def _old(path, hours=48):
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def test_a_duplicate_does_not_copy_the_originals_exports(figures):
    figure_id = repository.create("Figure 1")
    exports = repository.figure_dir(figure_id) / "exports" / "job_0123456789ab"
    exports.mkdir(parents=True)
    (exports / "figure.pdf").write_bytes(b"%PDF")

    copy = repository.duplicate(figure_id)

    assert not (repository.figure_dir(copy) / "exports").exists()
    assert (exports / "figure.pdf").is_file()     # the original's own are untouched


def test_exports_left_by_an_earlier_run_are_swept(figures):
    figure_id = repository.create("Figure 1")
    root = repository.figure_dir(figure_id) / "exports"
    stale = root / "job_000000000001"
    stale.mkdir(parents=True)
    (stale / "figure.pdf").write_bytes(b"%PDF")
    _old(stale)
    # Recent and unknown: possibly another Plexora on the same data root,
    # mid-export. Left alone.
    recent = root / "job_000000000002"
    recent.mkdir()

    export_jobs._sweep_orphans(figure_id)

    assert not stale.exists()
    assert recent.is_dir()


def test_a_delete_refused_by_an_open_file_deletes_nothing(figures, monkeypatch):
    figure_id = repository.create("Figure 1")
    directory = repository.figure_dir(figure_id)
    assets = directory / "assets"
    assets.mkdir()
    (assets / "ast_1.png").write_bytes(b"png")

    # Windows, with a file inside held open: the rename of the folder fails,
    # and an rmtree gets as far as the assets before it hits the open file.
    def refuse(*args, **kwargs):
        raise PermissionError(32, "The process cannot access the file")

    def partial_rmtree(path, ignore_errors=False, **kwargs):
        for item in (path / "assets").iterdir():
            item.unlink()
        if not ignore_errors:
            raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(repository.os, "replace", refuse)
    monkeypatch.setattr(repository.os, "rename", refuse)
    monkeypatch.setattr(repository.shutil, "rmtree", partial_rmtree)

    with pytest.raises(repository.FigureInUse):
        repository.delete(figure_id)

    assert (assets / "ast_1.png").is_file()
    assert repository.load(figure_id)["title"] == "Figure 1"


def test_a_delete_removes_the_figure_and_leaves_nothing_listed(figures):
    figure_id = repository.create("Figure 1")
    repository.delete(figure_id)

    assert not repository.exists(figure_id)
    assert repository.list_figures() == []
    assert [p.name for p in repository.figures_root().iterdir()] == []
