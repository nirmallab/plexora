"""Which cells of a 3-D mask are on which plane: the per-plane cell index.

The viewer draws a z-stack one plane at a time, and a 3-D mask's outlines are
that plane's (a label tile is read from the plane on screen:
server/volume/labels.py), so only the cells with voxels on it are drawn. What
the thresholding panel says under its histogram -- "z 12 . 41 of 312
positive" -- has to count exactly those cells. A cell's box in depth
(`bbox_z0..bbox_z1`) is not the same thing: a concave cell can have no voxels
on a plane inside its box.

So the pass that measures the objects (`measure.measure`) also writes, for
every plane, the cells with voxels on it and how many voxels each has there
-- one row per cell per plane it crosses, from the same reads. Gates are
always applied to the whole-cell values in `objects_v1`; this index only says
WHICH cells a plane shows.

STORED AS PLAIN COLUMN FILES beside the object table (`objects_planes_v1/`:
`label`, `z`, `t`, `voxels`), with `index.json` holding the first row of every
(frame, plane): rows are written in (t, z, label) order, so any run of planes
of one frame is one contiguous slice. Read memory-mapped, so an index larger
than memory is read a plane at a time, and nothing outside numpy is needed
(pyarrow is an optional extra). Written under a `.tmp` name and moved into
place when complete: a pass that is cancelled or fails leaves the previous
index, never half of a new one.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path

import numpy as np

FOLDER = "objects_planes_v1"
INDEX = "index.json"
VERSION = 1


class PlaneWriter:
    """Rows in (t, z, label) order, one `add` per plane; `close` publishes."""

    def __init__(self, folder, *, depth, frames=1):
        self.folder = Path(folder)
        self.depth = int(depth)
        self.frames = max(1, int(frames))
        self.tmp = self.folder.with_name(self.folder.name + ".tmp")
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True)
        self._files = {name: open(self.tmp / name, "wb") for name in ("label", "z", "voxels")}
        #: First row of plane (t, z), flat over t * depth + z; one past the end last.
        self._starts = np.zeros(self.frames * self.depth + 1, np.int64)
        self._next = 0
        self.rows = 0

    def add(self, t, z, labels, voxels):
        """Plane z of frame t: its cells' `labels` (ascending) and their
        `voxels` on it. Planes come in order; one with no cells may be skipped."""
        slot = int(t) * self.depth + int(z)
        if slot < self._next:
            raise ValueError(f"plane {z} of frame {t} was already written")
        self._starts[self._next:slot + 1] = self.rows
        self._next = slot + 1
        k = int(len(labels))
        if not k:
            return
        self._files["label"].write(np.asarray(labels, np.uint32).tobytes())
        self._files["z"].write(np.full(k, int(z), np.int32).tobytes())
        self._files["voxels"].write(np.asarray(voxels, np.uint32).tobytes())
        self.rows += k

    def close(self, **meta):
        """Write the index and move the folder into place; returns its path."""
        self._starts[self._next:] = self.rows
        for handle in self._files.values():
            handle.close()
        # The frame of a row is implied by the offsets: written once here.
        counts = np.diff(self._starts).reshape(self.frames, self.depth).sum(axis=1)
        np.repeat(np.arange(self.frames, dtype=np.int32), counts).tofile(self.tmp / "t")
        index = {"version": VERSION, "rows": int(self.rows), "depth": self.depth,
                 "frames": self.frames, "starts": self._starts.tolist(), **meta}
        (self.tmp / INDEX).write_text(json.dumps(index), encoding="utf-8")
        if self.folder.exists():
            shutil.rmtree(self.folder)
        self.tmp.replace(self.folder)
        return self.folder

    def abort(self):
        for handle in self._files.values():
            try:
                handle.close()
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def _read(path, dtype, rows):
    if rows == 0:
        return np.zeros(0, dtype)
    return np.memmap(path, dtype=dtype, mode="r", shape=(rows,))


class PlaneIndex:
    """A written index, read memory-mapped. `open` returns None when there is
    none (or it is from another version)."""

    def __init__(self, folder, index):
        self.folder = Path(folder)
        self.rows = int(index["rows"])
        self.depth = int(index["depth"])
        self.frames = int(index["frames"])
        self.starts = np.asarray(index["starts"], np.int64)
        self.label = _read(self.folder / "label", np.uint32, self.rows)
        self.z = _read(self.folder / "z", np.int32, self.rows)
        self.t = _read(self.folder / "t", np.int32, self.rows)
        self.voxels = _read(self.folder / "voxels", np.uint32, self.rows)

    @classmethod
    def open(cls, folder):
        folder = Path(folder)
        try:
            index = json.loads((folder / INDEX).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if index.get("version") != VERSION:
            return None
        return cls(folder, index)

    def span(self, t, z0, z1) -> slice:
        """The rows of planes z0..z1 (inclusive, clipped) of frame t."""
        t = min(max(int(t), 0), self.frames - 1)
        z0 = min(max(int(z0), 0), self.depth - 1)
        z1 = min(max(int(z1), z0), self.depth - 1)
        base = t * self.depth
        return slice(int(self.starts[base + z0]), int(self.starts[base + z1 + 1]))

    def cells_on(self, t, z):
        """`(labels, voxels)` of the cells with voxels on plane `z` of frame
        `t`, labels ascending."""
        span = self.span(t, z, z)
        return np.asarray(self.label[span]), np.asarray(self.voxels[span])


def index_for(table_path):
    """The index beside an object table (`objects_v1.parquet`), or None."""
    return PlaneIndex.open(folder_for(table_path))


def folder_for(table_path) -> Path:
    return Path(table_path).parent / FOLDER


# -- moving an index between machines (a data node measures, the primary keeps it) --


def pack(folder) -> bytes:
    """A written index as one uncompressed tar."""
    folder = Path(folder)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(folder.iterdir()):
            archive.add(path, arcname=path.name)
    return buffer.getvalue()


def unpack(data, folder) -> Path:
    """`pack`'s output -- its bytes, or a path to them on disk -- written as
    the index `folder` (replacing one there)."""
    folder = Path(folder)
    tmp = folder.with_name(folder.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    opened = (tarfile.open(fileobj=io.BytesIO(data), mode="r")
              if isinstance(data, (bytes, bytearray)) else tarfile.open(Path(data), mode="r"))
    with opened as archive:
        archive.extractall(tmp, filter="data")
    if PlaneIndex.open(tmp) is None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError("the plane index sent is not one this version reads")
    if folder.exists():
        shutil.rmtree(folder)
    tmp.replace(folder)
    return folder
