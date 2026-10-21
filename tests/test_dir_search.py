"""The explorer's search: names under one folder, nearest first, within a budget.

What is pinned is mostly restraint. A search cannot be cancelled once it has
started, so it has to end on its own -- at a number of matches, a number of
milliseconds or a depth -- and say which one ended it. It never follows a link
into a folder (or a link back up the tree would never end), never walks into a
`.zarr` store's chunks, and two run at once at most.
"""

from __future__ import annotations

import itertools
import os
import threading

import pytest

from plexora.server.utils import dir_listing, dir_search

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")


def _tree(root):
    """root/{hit.csv, a/{hit.txt, b/{hit.tif, c/{hit.h5ad}}}}"""
    deepest = root / "a" / "b" / "c"
    deepest.mkdir(parents=True)
    (root / "hit.csv").write_text("")
    (root / "a" / "hit.txt").write_text("")
    (root / "a" / "b" / "hit.tif").write_text("")
    (deepest / "hit.h5ad").write_text("")


def _found(answer):
    return [match["rel"] for match in answer["matches"]]


def test_matches_come_nearest_first(tmp_path):
    _tree(tmp_path)

    answer = dir_search.search(str(tmp_path), "HIT")

    assert _found(answer) == [
        "hit.csv", os.path.join("a", "hit.txt"), os.path.join("a", "b", "hit.tif"),
        os.path.join("a", "b", "c", "hit.h5ad")]
    assert [match["depth"] for match in answer["matches"]] == [1, 2, 3, 4]
    assert answer["matches"][1]["path"] == str(tmp_path / "a" / "hit.txt")
    assert answer["matches"][0]["size"] is None and answer["matches"][0]["mtime"] is None
    assert answer["visited"] == 4 and answer["stopped"] is None
    assert answer["path"] == str(tmp_path) and answer["query"] == "HIT"


def test_a_search_stops_at_its_limit_and_says_so(tmp_path):
    _tree(tmp_path)

    answer = dir_search.search(str(tmp_path), "hit", limit=2)

    assert len(answer["matches"]) == 2
    assert answer["stopped"] == "limit"


def test_a_search_stops_at_its_depth_and_says_so(tmp_path):
    _tree(tmp_path)

    answer = dir_search.search(str(tmp_path), "hit", max_depth=2)

    assert _found(answer) == ["hit.csv", os.path.join("a", "hit.txt")]
    assert answer["stopped"] == "depth"


def test_a_search_stops_on_its_budget_and_says_so(tmp_path, monkeypatch):
    _tree(tmp_path)
    clock = itertools.count(0.0, 0.4)
    monkeypatch.setattr(dir_search, "_now", lambda: next(clock))

    answer = dir_search.search(str(tmp_path), "hit", budget_ms=1000)

    assert answer["stopped"] == "budget"
    assert 0 < answer["visited"] < 4


def test_the_bounds_are_clamped_to_the_caps(tmp_path, monkeypatch):
    seen = {}

    def _walk(directory, query, show_hidden, limit, deadline, max_depth):
        seen.update(limit=limit, max_depth=max_depth)
        return {}

    monkeypatch.setattr(dir_search, "_walk", _walk)
    dir_search.search(str(tmp_path), "x", limit=10_000, max_depth=99)

    assert seen == {"limit": dir_search.SEARCH_LIMIT,
                    "max_depth": dir_search.SEARCH_MAX_DEPTH}


@posix_only
def test_a_link_to_a_folder_is_a_match_but_never_a_way_in(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "inside.csv").write_text("")
    os.symlink(tmp_path / "real", tmp_path / "linked")
    # And a link back up the tree, which a walk that followed links would
    # circle forever.
    os.symlink(tmp_path, tmp_path / "real" / "loop")

    answer = dir_search.search(str(tmp_path), "i")

    assert _found(answer) == ["linked", os.path.join("real", "inside.csv")]
    assert answer["matches"][0]["kind"] == "link"
    assert answer["matches"][0]["is_dir"] is True


def test_a_store_is_a_match_but_its_chunks_are_not_walked(tmp_path):
    store = tmp_path / "cells.zarr"
    (store / "0" / "0").mkdir(parents=True)
    (store / "0" / "0" / "cells-chunk").write_text("")

    answer = dir_search.search(str(tmp_path), "cells")

    assert _found(answer) == ["cells.zarr"]
    assert answer["matches"][0]["store"]["type"] == "zarr"
    assert answer["visited"] == 1


def test_hidden_names_are_neither_matched_nor_walked_unless_asked(tmp_path):
    (tmp_path / ".snakemake" / "log").mkdir(parents=True)
    (tmp_path / ".snakemake" / "log" / "run.log").write_text("")
    (tmp_path / ".run-notes").write_text("")

    assert _found(dir_search.search(str(tmp_path), "run")) == []
    assert _found(dir_search.search(str(tmp_path), "run", show_hidden=True)) == [
        ".run-notes", os.path.join(".snakemake", "log", "run.log")]


def test_a_query_with_wildcards_is_a_glob(tmp_path):
    for name in ("a.ome.tif", "b.tif", "c.ome.tiff", "notes.txt"):
        (tmp_path / name).write_text("")

    assert _found(dir_search.search(str(tmp_path), "*.OME.tif")) == ["a.ome.tif"]
    assert _found(dir_search.search(str(tmp_path), "?.tif")) == ["b.tif"]
    # Without one it is a substring, not a glob.
    assert len(_found(dir_search.search(str(tmp_path), "tif"))) == 3


def test_a_third_search_at_once_is_busy(tmp_path, monkeypatch):
    slots = threading.BoundedSemaphore(2)
    slots.acquire()
    slots.acquire()
    monkeypatch.setattr(dir_search, "_slots", slots)

    with pytest.raises(dir_search.SearchBusy):
        dir_search.search(str(tmp_path), "x")


def test_a_search_gives_its_slot_back(tmp_path):
    for _ in range(5):
        dir_search.search(str(tmp_path), "x")
    assert dir_search._slots.acquire(blocking=False)
    dir_search._slots.release()


@pytest.mark.parametrize("query", ["", "   ", None, "x" * 257, 7])
def test_a_query_is_short_and_says_something(tmp_path, query):
    with pytest.raises(ValueError):
        dir_search.search(str(tmp_path), query)


def test_a_256_character_query_is_fine(tmp_path):
    assert dir_search.search(str(tmp_path), "x" * 256)["matches"] == []


def test_a_folder_that_is_not_there_is_the_listing_s_sentence(tmp_path):
    with pytest.raises(dir_listing.ListingError, match="Not a folder"):
        dir_search.search(str(tmp_path / "gone"), "x")
