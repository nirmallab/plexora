"""One directory, as the explorer draws it -- and which rows are datasets.

Two modules, tested together because the second only exists to decorate the
first: `dataset_marks` says which folders are one thing to import (a `.zarr`
store, a SpatialData store, a Xenium or Visium run), and `dir_listing` decides
how much it can afford to find out about each row before the answer goes.

What is pinned here is the budget as much as the shape: names always, dates
and sizes when the folder is small enough, child looks only when there are
few children, and `partial` telling the explorer which is which.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plexora.server.utils import dataset_marks, dir_listing

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks and bits")
not_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                              reason="root can read anything")


def _names(found):
    return [entry["name"] for entry in found["entries"]]


def _row(found, name):
    return next(entry for entry in found["entries"] if entry["name"] == name)


# -- dataset marks: by name, by the names inside -------------------------------


def test_marks_a_zarr_by_its_name_alone():
    assert dataset_marks.store_by_name("cells.zarr") == {
        "type": "zarr", "label": "Zarr store"}
    assert dataset_marks.store_by_name("slide.OME.ZARR")["label"] == "OME-Zarr image"
    assert dataset_marks.store_by_name("study") is None
    # A zipped store is a file, not a directory to choose.
    assert dataset_marks.store_by_name("cells.zarr.zip") is None


@pytest.mark.parametrize("name, inside, kind", [
    ("plain", [".zattrs", ".zgroup", "0"], "zarr"),
    ("anything", ["zarr.json"], "zarr"),
    ("sd", [".zgroup", "tables"], "spatialdata"),
    ("sd.zarr", ["images", "points"], "spatialdata"),
    ("image.zarr", [".zattrs", "labels", "0"], "zarr"),
    ("run", ["experiment.xenium", "gene_panel.json"], "xenium"),
    ("copied", ["transcripts.parquet", "cells.parquet"], "xenium"),
    ("outs", ["spatial", "filtered_feature_bc_matrix.h5"], "visium"),
    ("outs", ["spatial", "raw_feature_bc_matrix"], "visium"),
    ("outs", ["binned_outputs", "spatial"], "visium_hd"),
    ("slide", ["1.dcm", "2.dcm"], "dicom"),
    ("export", ["DICOMDIR"], "dicom"),
])
def test_marks_a_folder_by_the_names_inside_it(name, inside, kind):
    assert dataset_marks.store_by_names(name, inside)["type"] == kind


def test_marks_never_claim_an_ordinary_project_folder():
    """`images/` and `tables/` are the commonest folder names on a lab share.
    Without the zarr itself there, a folder holding them is a folder -- marking
    it would turn "open" into "import" on one click."""
    assert dataset_marks.store_by_names("study", ["images", "tables", "notes.txt"]) is None
    assert dataset_marks.store_by_names("study", ["spatial"]) is None
    assert dataset_marks.store_by_names("study", ["cells.parquet"]) is None


def test_marks_follow_the_importers_order():
    """Visium HD before Visium, Xenium before the rest -- the importer's chain."""
    hd = dataset_marks.store_by_names(
        "outs", ["binned_outputs", "spatial", "filtered_feature_bc_matrix.h5"])
    assert hd["type"] == "visium_hd"
    xenium = dataset_marks.store_by_names(
        "run", ["experiment.xenium", ".zgroup", "tables"])
    assert xenium["type"] == "xenium"


def test_marks_child_names_are_capped_and_a_shut_folder_is_none(tmp_path):
    for index in range(10):
        (tmp_path / f"f{index}").write_text("")
    assert len(dataset_marks.child_names(tmp_path, 4)) == 4
    assert dataset_marks.child_names(tmp_path / "nowhere", 4) == []


@posix_only
@not_root
def test_marks_child_names_of_a_locked_folder_is_none(tmp_path):
    shut = tmp_path / "locked"
    shut.mkdir()
    shut.chmod(0o000)
    try:
        assert dataset_marks.child_names(shut, 4) is None
    finally:
        shut.chmod(0o755)


# -- dataset marks: the open folder ---------------------------------------------


def test_marks_dataset_of_an_open_zarr(tmp_path):
    store = tmp_path / "cells.zarr"
    store.mkdir()
    (store / ".zgroup").write_text("{}")

    found = dataset_marks.dataset_of(store, [".zgroup", "0"])

    assert found == {"type": "zarr", "label": "Zarr store", "licensed": True}


def test_marks_dataset_of_a_spatialdata_root(tmp_path):
    store = tmp_path / "sample"
    (store / "tables").mkdir(parents=True)
    (store / "images").mkdir()
    (store / "zarr.json").write_text("{}")

    found = dataset_marks.dataset_of(store, ["zarr.json", "tables", "images"])

    assert found["type"] == "spatialdata"
    assert found["licensed"] is True


def test_marks_dataset_of_a_xenium_run_says_when_it_is_not_licensed(
        tmp_path, monkeypatch):
    """Marked either way -- it IS a Xenium run -- but the explorer has to be
    able to say that this licence will not import it, rather than offer a
    folder the importer then refuses without a word."""
    from plexora.server.models import import_proposal

    run = tmp_path / "output-XETG"
    run.mkdir()
    (run / "experiment.xenium").write_text("{}")

    monkeypatch.setattr(import_proposal, "_licensed", lambda entitlement: False)
    assert dataset_marks.dataset_of(run, ["experiment.xenium"]) == {
        "type": "xenium", "label": "Xenium run", "licensed": False}

    monkeypatch.setattr(import_proposal, "_licensed", lambda entitlement: True)
    assert dataset_marks.dataset_of(run, ["experiment.xenium"])["licensed"] is True


def test_marks_dataset_of_asks_the_detector_only_when_the_names_say_so(
        tmp_path, monkeypatch):
    """`is_visium_hd_run` reads a scale-factor file. A home directory must not
    pay for it, and a folder whose names merely look right is confirmed."""
    from plexora.server.utils import spatial_scene

    asked = []
    monkeypatch.setattr(spatial_scene, "is_visium_hd_run",
                        lambda path: asked.append(path) or False)

    assert dataset_marks.dataset_of(tmp_path, ["notes.txt", "data"]) is None
    assert asked == []
    # The names say Visium HD; the detector says no, and the detector wins.
    assert dataset_marks.dataset_of(tmp_path, ["binned_outputs"]) is None
    assert asked == [tmp_path]


def test_marks_dataset_of_finds_dicom_one_level_down_by_names(tmp_path):
    """The HTAN layout puts instances under study and series folders. One level
    of names the listing already read is as deep as this goes."""
    found = dataset_marks.dataset_of(tmp_path, ["series-1"],
                                     children={"series-1": ["a.dcm", "b.dcm"]})
    assert found["type"] == "dicom"
    assert dataset_marks.dataset_of(tmp_path, ["series-1"],
                                    children={"series-1": None}) is None


# -- the listing --------------------------------------------------------------


def test_every_row_carries_its_date_and_kind(tmp_path):
    (tmp_path / "cells.csv").write_bytes(b"a,b\n1,2\n")
    (tmp_path / "study").mkdir()
    os.utime(tmp_path / "cells.csv", (1_700_000_000, 1_700_000_000))

    found = dir_listing.listing(str(tmp_path))

    cells, study = _row(found, "cells.csv"), _row(found, "study")
    assert set(cells) == {"name", "is_dir", "size", "path", "mtime", "kind", "store"}
    assert cells["mtime"] == 1_700_000_000
    assert cells["size"] == 8 and cells["kind"] == "file"
    # Folders get a date too -- "modified" is how somebody finds last week's run.
    assert isinstance(study["mtime"], int) and study["size"] is None
    assert study["kind"] == "dir"
    assert found["total"] == 2
    assert found["partial"] is False and found["truncated"] is False
    assert found["dataset"] is None
    assert found["platform"] in ("macos", "linux", "windows")


def test_a_store_row_is_marked_and_the_open_store_is_a_dataset(tmp_path):
    store = tmp_path / "cells.zarr"
    store.mkdir()
    (store / ".zgroup").write_text("{}")
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "experiment.xenium").write_text("{}")

    outer = dir_listing.listing(str(tmp_path))
    assert _row(outer, "cells.zarr")["store"] == {"type": "zarr", "label": "Zarr store"}
    assert _row(outer, "run")["store"]["type"] == "xenium"

    # The markers are dotfiles and are read whether or not they are shown.
    inner = dir_listing.listing(str(store))
    assert _names(inner) == []
    assert inner["dataset"]["type"] == "zarr"


@posix_only
def test_links_are_told_apart_from_what_they_point_at(tmp_path):
    (tmp_path / "real.csv").write_text("x")
    (tmp_path / "folder").mkdir()
    os.symlink(tmp_path / "real.csv", tmp_path / "to-file")
    os.symlink(tmp_path / "folder", tmp_path / "to-folder")
    os.symlink(tmp_path / "gone", tmp_path / "dangling")

    found = dir_listing.listing(str(tmp_path))

    assert _row(found, "to-file")["kind"] == "link"
    assert _row(found, "to-file")["size"] == 1
    # A link to a folder is still a folder to open.
    assert _row(found, "to-folder")["kind"] == "link"
    assert _row(found, "to-folder")["is_dir"] is True
    dangling = _row(found, "dangling")
    assert dangling["kind"] == "broken" and dangling["mtime"] is None
    # A broken link is not something left to stat, so it does not make the
    # listing partial.
    assert found["partial"] is False


@posix_only
@not_root
def test_a_folder_that_cannot_be_entered_shows_a_lock(tmp_path):
    shut = tmp_path / "theirs"
    shut.mkdir()
    shut.chmod(0o000)
    try:
        found = dir_listing.listing(str(tmp_path))
        assert _row(found, "theirs")["kind"] == "denied"
        assert _row(found, "theirs")["is_dir"] is True
    finally:
        shut.chmod(0o755)


@posix_only
@not_root
def test_child_folders_are_not_looked_inside_when_there_are_many(
        tmp_path, monkeypatch):
    """Two thousand sample folders are browsed, not imported: past the limit
    no child is opened, so nothing is marked by its insides and no lock is
    found until the folder is clicked."""
    monkeypatch.setattr(dir_listing, "CHILD_MARKER_LIMIT", 2)
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    (tmp_path / "a" / ".zgroup").write_text("{}")
    (tmp_path / "c").chmod(0o000)
    try:
        found = dir_listing.listing(str(tmp_path))
        assert _row(found, "a")["store"] is None
        assert _row(found, "c")["kind"] == "dir"

        monkeypatch.setattr(dir_listing, "CHILD_MARKER_LIMIT", 3)
        found = dir_listing.listing(str(tmp_path))
        assert _row(found, "a")["store"]["type"] == "zarr"
        assert _row(found, "c")["kind"] == "denied"
    finally:
        (tmp_path / "c").chmod(0o755)


def test_the_cut_keeps_count_of_what_it_left_out(tmp_path):
    for name in ("b.csv", "c.csv", "a.csv", ".hidden"):
        (tmp_path / name).write_text("")

    found = dir_listing.listing(str(tmp_path), limit=2)

    assert _names(found) == ["a.csv", "b.csv"]
    assert found["truncated"] is True
    # Counted as shown: the hidden file is not in the total it was hidden from.
    assert found["total"] == 3


def test_reading_stops_at_the_hard_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(dir_listing, "SCAN_HARD_CAP", 3)
    for index in range(6):
        (tmp_path / f"f{index}.csv").write_text("")

    found = dir_listing.listing(str(tmp_path))

    assert found["truncated"] is True
    assert found["total"] == 3


def test_a_big_folder_leaves_its_dates_to_be_asked_for(tmp_path, monkeypatch):
    monkeypatch.setattr(dir_listing, "INLINE_STAT_LIMIT", 1)
    (tmp_path / "a.csv").write_text("x")
    (tmp_path / "b.csv").write_text("xy")

    found = dir_listing.listing(str(tmp_path))

    assert found["partial"] is True
    assert all(entry["mtime"] is None and entry["size"] is None
               for entry in found["entries"])


def test_a_slow_folder_stops_statting_on_its_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(dir_listing, "LISTING_BUDGET_S", 0)
    (tmp_path / "a.csv").write_text("x")
    (tmp_path / "study").mkdir()
    (tmp_path / "study" / ".zgroup").write_text("{}")

    found = dir_listing.listing(str(tmp_path))

    assert found["partial"] is True
    assert _row(found, "a.csv")["mtime"] is None
    # No child look either: the deadline covers both.
    assert _row(found, "study")["store"] is None


def test_a_big_folder_still_dates_the_folders_it_looked_inside(
        tmp_path, monkeypatch):
    """Their `denied` came from the look, and a later `stat_entries` would call
    them plain directories -- so they are stat'ed with it and never asked."""
    monkeypatch.setattr(dir_listing, "INLINE_STAT_LIMIT", 1)
    (tmp_path / "study").mkdir()
    (tmp_path / "a.csv").write_text("x")

    found = dir_listing.listing(str(tmp_path))

    assert isinstance(_row(found, "study")["mtime"], int)
    assert _row(found, "a.csv")["mtime"] is None
    assert found["partial"] is True


def test_the_old_sentences_and_defaults_survive(tmp_path):
    (tmp_path / "cells.csv").write_text("x")
    assert dir_listing.listing(str(tmp_path / "cells.csv"))["path"] == str(tmp_path)
    assert dir_listing.listing("")["path"] == str(Path.home().resolve())
    with pytest.raises(dir_listing.ListingError, match="Not a folder"):
        dir_listing.listing(str(tmp_path / "gone"))


# -- stat_entries -------------------------------------------------------------


def test_stat_entries_answers_for_the_names_it_is_given(tmp_path):
    (tmp_path / "cells.csv").write_bytes(b"12345")
    (tmp_path / "study").mkdir()

    found = dir_listing.stat_entries(str(tmp_path), ["cells.csv", "study", "gone.csv"])

    assert found["path"] == str(tmp_path)
    assert found["entries"]["cells.csv"]["size"] == 5
    assert found["entries"]["cells.csv"]["kind"] == "file"
    assert found["entries"]["study"] == {
        "size": None, "mtime": found["entries"]["study"]["mtime"],
        "kind": "dir", "is_dir": True}
    # Deleted since the listing: left out, not an error.
    assert "gone.csv" not in found["entries"]


@posix_only
def test_stat_entries_reports_links_and_broken_links(tmp_path):
    (tmp_path / "real.csv").write_text("abc")
    os.symlink(tmp_path / "real.csv", tmp_path / "to-file")
    os.symlink(tmp_path / "gone", tmp_path / "dangling")

    found = dir_listing.stat_entries(str(tmp_path), ["to-file", "dangling"])["entries"]

    assert found["to-file"]["kind"] == "link" and found["to-file"]["size"] == 3
    assert found["dangling"] == {"size": None, "mtime": None,
                                 "kind": "broken", "is_dir": False}


@pytest.mark.parametrize("names", [
    ["a/b"], [".."], ["."], [""], [7], "cells.csv",
])
def test_stat_entries_takes_bare_names_only(tmp_path, names):
    """It answers about one folder. A path, a parent or a non-name would make
    it a way to stat the rest of the disk."""
    with pytest.raises(dir_listing.ListingError):
        dir_listing.stat_entries(str(tmp_path), names)


def test_stat_entries_is_bounded(tmp_path):
    names = [f"f{index}" for index in range(dir_listing.STAT_ENTRIES_LIMIT + 1)]
    with pytest.raises(dir_listing.ListingError, match="At most 500"):
        dir_listing.stat_entries(str(tmp_path), names)
    assert dir_listing.stat_entries(str(tmp_path), names[:-1])["entries"] == {}


def test_a_listing_is_plain_json(tmp_path):
    (tmp_path / "x.zarr").mkdir()
    json.dumps(dir_listing.listing(str(tmp_path)))
