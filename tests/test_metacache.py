"""Header reads kept per file while it is unchanged (server/utils/metacache.py).

A gating session asked one image's header for its pixel size and layout about
400 times (P9.md); these pin that a kept answer is only ever served for the
same bytes.
"""

import os
import time

import numpy as np
import tifffile

from plexora.server.utils import brightfield, metacache, pixel_scale


def _counting():
    calls = []

    @metacache.per_file
    def read(path):
        calls.append(path)
        return {"read": len(calls)}

    return read, calls


def test_a_file_is_read_once_while_it_is_unchanged(tmp_path):
    read, calls = _counting()
    path = tmp_path / "a.bin"
    path.write_bytes(b"one")
    assert read(path) == read(str(path)) == {"read": 1}
    assert len(calls) == 1


def test_a_rewritten_file_is_read_again(tmp_path):
    read, calls = _counting()
    path = tmp_path / "a.bin"
    path.write_bytes(b"one")
    read(path)
    path.write_bytes(b"three")  # another size
    read(path)
    later = time.time() + 5
    os.utime(path, (later, later))  # same size, another time
    read(path)
    assert len(calls) == 3


def test_a_directory_is_read_every_time(tmp_path):
    """Its mtime does not move when a file inside it is rewritten."""
    read, calls = _counting()
    read(tmp_path)
    read(tmp_path)
    assert len(calls) == 2


def test_what_is_not_a_file_here_is_read_as_before(tmp_path):
    read, calls = _counting()
    read("s3://bucket/slide.ome.tif")
    read(tmp_path / "missing.tif")
    assert len(calls) == 2


def test_a_caller_that_changes_its_answer_changes_nobody_elses(tmp_path):
    read, _calls = _counting()
    path = tmp_path / "a.bin"
    path.write_bytes(b"one")
    first = read(path)
    first["read"] = "changed"
    assert read(path) == {"read": 1}


def test_the_two_header_readers_keep_their_answers(tmp_path):
    path = tmp_path / "slide.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 64, 64), np.uint16), ome=True,
                     metadata={"axes": "CYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5})
    assert hasattr(pixel_scale._local_metadata, "forget")
    assert hasattr(brightfield._tiff_layout, "forget")
    first = brightfield._tiff_layout(path)
    assert brightfield._tiff_layout(path) == first
    assert pixel_scale._local_metadata(str(path)) is pixel_scale._local_metadata(str(path))
