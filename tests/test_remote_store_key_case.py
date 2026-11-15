"""Cache value paths must stay distinct on a case-insensitive disk.

The chunk cache keeps one file per store key under `files/<store>/<key>`.
macOS (APFS by default) and Windows fold case, so two keys that differ only in
case -- a label group `Cells` beside `cells`, `A.tif` beside `a.tif` on a Linux
server -- used to land on ONE file, and whichever was written last was served
for both. `_key_path` now spells upper-case letters as percent escapes, so the
on-disk names differ even after folding, and a file in the old spelling is not
adopted back as a cached value by reconcile.
"""

from pathlib import Path

from plexora.server.utils import remote_store


def test_keys_differing_only_in_case_get_paths_that_differ_after_folding():
    pairs = [("Cells/0/0", "cells/0/0"), ("A.tif", "a.tif"),
             ("labels/Nuclei/.zattrs", "labels/nuclei/.zattrs")]
    for upper, lower in pairs:
        a, b = remote_store._key_path(upper), remote_store._key_path(lower)
        assert a.casefold() != b.casefold(), (upper, lower, a, b)


def test_ordinary_keys_keep_their_readable_spelling():
    assert remote_store._key_path("0/c/0/1/2") == "0/c/0/1/2"
    assert remote_store._key_path(".zattrs") == ".zattrs"


def test_value_paths_round_trip_through_reconcile_parsing(tmp_path):
    directory = tmp_path / "store"
    for key in ("OME/METADATA.ome.xml", "Cells/0/0", "a b:c?", "x%41"):
        path = directory / remote_store._key_path(key)
        assert remote_store.CacheIndex._parse_value_path(directory, path) == (key, -1, -1)
        ranged = (directory / remote_store._key_path(key)).with_name(
            Path(remote_store._key_path(key)).name + ".ranges") / "0_10"
        assert remote_store.CacheIndex._parse_value_path(directory, ranged) == (key, 0, 10)


def test_a_file_in_the_old_spelling_is_not_adopted(tmp_path):
    directory = tmp_path / "store"
    # Written before the fix: the upper-case letters literally on disk. On a
    # folding disk it may hold the bytes of `cells/0/0`, so it is not trusted.
    assert remote_store.CacheIndex._parse_value_path(
        directory, directory / "Cells" / "0" / "0") is None
