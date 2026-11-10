"""plexora/browser/tiff_ahead.py: a picked TIFF's directories read ahead of
tifffile's walk, a round of pieces at a time.

Under CPython there is no page, so the worker's reads of a picked file are
played here (`Worker`, as client workers/pythonWorker.js `blockReads` makes
them, scaled down so a small file spans many blocks): pieces a read ahead read
answer a read that starts in them, short where it runs past them; any other
read reads its block, one after another. tifffile reads through a buffered
reader as on the website (Python 3.14 buffers 128 KiB, 3.13 8 KiB; scaled as
the worker's sizes are), so it asks past what it needs. The walk reads only from pieces it asked for, leaves
tifffile's open no block to read, and takes fewer rounds than tifffile alone
made reads. The real files it was built for (Bio-Formats OME-TIFFs on the lab
share: 8 reads of 1 MiB, then 4 rounds of 64 KiB pieces) are in
docs/internal/bench/web/WEBSITE_PERF_2026-11-09.md.
"""

import io
import os
import shutil
import subprocess
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pytest
import tifffile

from plexora.browser import tiff_ahead
from plexora.server.utils import tiff_series

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "block_reads_ahead_probe.mjs"
#: The worker's 64 KiB pieces and 1 MiB blocks, scaled down alike.
PIECE = 1024
BLOCK = 16 * PIECE
KEEP = 64
PIECES = 256
#: Python's buffers, 128 KiB (3.14, the website's) and 8 KiB, scaled alike.
BUFFERS = (2048, 128)


class Worker:
    """One picked file, read as the website's worker reads it."""

    def __init__(self, path, piece=PIECE, block=BLOCK):
        self.path, self.piece, self.block = path, piece, block
        self.size = os.path.getsize(path)
        self.pieces = {}
        self.blocks = OrderedDict()
        #: Blocks read on a miss, one after another.
        self.alone = []
        #: Pieces read ahead, a round at a time.
        self.rounds = []

    def _bytes(self, start, end):
        with open(self.path, "rb") as fh:
            fh.seek(start)
            return fh.read(end - start)

    def ahead(self, indexes):
        wanted = sorted({i for i in indexes if i >= 0 and i * self.piece < self.size and i not in self.pieces})
        wanted = wanted[:PIECES]
        for index in wanted:
            self.pieces[index] = self._bytes(index * self.piece, (index + 1) * self.piece)
        self.rounds.append(wanted)
        return len(wanted)

    def read(self, position, length):
        """One read() of the file: pieces first, short; else blocks."""
        end = min(self.size, position + length)
        out = bytearray()
        at = position
        while at < end and at // self.piece in self.pieces:
            data = self.pieces[at // self.piece]
            take = data[at - at // self.piece * self.piece:][:end - at]
            if not take:
                break
            out += take
            at += len(take)
        if out or position >= end:
            return bytes(out)
        if end - position > 4 * self.block:
            self.alone.append(("straight", position))
            return self._bytes(position, end)
        while at < end:
            index = at // self.block
            data = self.blocks.pop(index, None)
            if data is None:
                data = self._bytes(index * self.block, (index + 1) * self.block)
                self.alone.append(index)
            self.blocks[index] = data
            while len(self.blocks) > KEEP:
                self.blocks.popitem(last=False)
            take = data[at - index * self.block:][:end - at]
            out += take
            at += len(take)
        return bytes(out)

    def raw(self):
        return _Raw(self)


class _Raw(io.RawIOBase):
    """The file as Emscripten's FS gives it to Python: one read() per call."""

    def __init__(self, worker):
        self.worker, self.pos = worker, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.worker.size + offset
        return self.pos

    def readinto(self, buffer):
        data = self.worker.read(self.pos, len(buffer))
        buffer[:len(data)] = data
        self.pos += len(data)
        return len(data)


def _open(worker, buffer):
    """tifffile's open as the website's image provider makes it, through a
    buffered reader: -> its levels and dtype."""
    tiff = tifffile.TiffFile(io.BufferedReader(worker.raw(), buffer_size=buffer), is_ome=False, name="picked.tif")
    try:
        series = tiff_series.channel_series(tiff)
        return [tuple(level.shape) for level in series.levels], str(series.dtype)
    finally:
        tiff.close()


def _walk(worker):
    return tiff_ahead.walk(worker.raw(), worker.size, worker.ahead, worker.piece)


def _pyramid(tmp_path):
    """A BigTIFF pyramid as tifffile writes one: 3 channels, 2 SubIFD levels
    each, every IFD beside its own plane."""
    rng = np.random.default_rng(5)
    planes = rng.integers(0, 4000, (3, 512, 640), dtype=np.uint16)
    path = tmp_path / "pyramid.ome.tif"
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as tif:
        tif.write(planes, subifds=2, tile=(256, 256), metadata={"axes": "CYX"})
        for factor in (2, 4):
            tif.write(planes[:, ::factor, ::factor], subfiletype=1, tile=(256, 256))
    return path


def _stack(tmp_path, pages=6, spread=False, side=96):
    """A classic TIFF stack of strips, no SubIFDs: written at once, its IFDs
    after the first sit together at the end; written a page at a time
    (`spread`), each sits beside its own plane."""
    rng = np.random.default_rng(7)
    planes = rng.integers(0, 255, (pages, side, side), dtype=np.uint8)
    path = tmp_path / f"stack{pages}.tif"
    with tifffile.TiffWriter(path) as tif:
        for plane in (planes if spread else [planes]):
            tif.write(plane, photometric="minisblack", contiguous=not spread)
    return path


def _packed(tmp_path):
    """A spread stack whose IFDs sit 9 KB apart: a main chain read ahead."""
    return _stack(tmp_path, pages=12, spread=True)


@pytest.mark.parametrize("buffer", BUFFERS)
@pytest.mark.parametrize("make", [_pyramid, _stack, _packed])
def test_the_walk_leaves_tifffile_no_block_to_read(tmp_path, make, buffer):
    path = make(tmp_path)
    bare = Worker(path)
    opened = _open(bare, buffer)
    walked = Worker(path)
    done = _walk(walked)
    assert walked.alone == [], "the walk read only from pieces it had asked for"
    assert _open(walked, buffer) == opened
    assert walked.alone == [], "tifffile found everything it read"
    assert done["rounds"] == len(walked.rounds) and done["pieces"] == sum(map(len, walked.rounds))
    # No more rounds than tifffile alone made reads (a stack whose IFDs sit at
    # its two ends: two either way), and less read than its blocks.
    assert done["rounds"] <= len(bare.alone)
    assert done["pieces"] * PIECE < len(bare.alone) * BLOCK
    if make is _pyramid:
        # Each main IFD a round, its levels in the next beside the next IFD:
        # 42 KiB in 4 rounds, where tifffile alone read 160 KiB in 10.
        assert (done["rounds"], done["pieces"], done["ifds"], len(bare.alone)) == (4, 42, 3, 10)
    if make is _packed:
        # 12 IFDs 9 KB apart: one, then twice as far as the chain had come.
        assert (done["rounds"], done["ifds"], len(bare.alone)) == (3, 12, 7)


def test_a_walk_past_its_budget_leaves_the_rest_to_tifffile(tmp_path):
    """A chain whose IFDs sit a plane apart is not read ahead past each next
    IFD, so it ends at `ROUNDS` (the first holding the first IFD as well):
    tifffile reads the rest as before."""
    path = _stack(tmp_path, pages=40, spread=True, side=300)
    bare = Worker(path)
    opened = _open(bare, BUFFERS[0])
    walked = Worker(path)
    done = _walk(walked)
    assert done["rounds"] == tiff_ahead.ROUNDS and done["ifds"] == tiff_ahead.ROUNDS
    assert _open(walked, BUFFERS[0]) == opened
    assert walked.alone, "tifffile read the rest"
    assert len(walked.rounds) + len(walked.alone) <= len(bare.alone) + 1


def test_what_is_not_a_tiff_is_not_walked(tmp_path):
    path = tmp_path / "table.csv"
    path.write_text("x,y\n" * 5000)
    worker = Worker(path)
    assert _walk(worker) == {"rounds": 1, "pieces": 2, "ifds": 0}


def test_tifffile_opens_a_file_after_its_walk(tmp_path, monkeypatch):
    """`install` walks each file tifffile opens to read, before tifffile
    reads it; a file the page does not hold (a negative answer), a file
    written and a walk that fails leave tifffile as it was."""
    monkeypatch.setattr(tifffile.FileHandle, "open", tifffile.FileHandle.open)
    monkeypatch.setattr(tiff_ahead, "walks", tiff_ahead.deque(maxlen=8))
    path = os.path.realpath(_pyramid(tmp_path))
    size = os.path.getsize(path)
    answer = {"value": None}
    asked = []

    def read_ahead(where, pieces):
        asked.append((where, list(pieces)))
        if isinstance(answer["value"], Exception):
            raise answer["value"]
        return len(pieces) if answer["value"] is None else answer["value"]

    assert tiff_ahead.install(read_ahead, PIECE) is True
    assert tiff_ahead.install(read_ahead, PIECE) is True  # once
    with tifffile.TiffFile(path, is_ome=False) as tif:
        shape = tif.series[0].shape
    assert asked[0] == (path, [0, (size - 1) // PIECE])
    assert len(asked) == 4
    assert [walk["path"] for walk in tiff_ahead.walks] == [path]

    for value in (-1, OSError("the file changed")):
        asked.clear()
        answer["value"] = value
        with tifffile.TiffFile(path, is_ome=False) as tif:
            assert tif.series[0].shape == shape
        assert len(asked) == 1

    asked.clear()
    tifffile.imwrite(tmp_path / "written.tif", np.zeros((8, 8), np.uint8))
    assert asked == []


def test_the_worker_reads_ahead_and_answers_from_what_it_read():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert done.returncode == 0, done.stderr[-3000:]
    assert '"ok":true' in done.stdout
