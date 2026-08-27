"""Providers for resources on this machine's own filesystem.

These are the incumbent behaviour with a seam cut through it. Every method is
either the code `data_model.load_datasource()` used to run inline, or a call
back into a `data_model` helper that is unchanged -- so a single-server project
reads exactly the same bytes through exactly the same libraries as before, and
the module globals end up holding the identical objects.

They are also what a **node** runs. A node process serves several resources at
once and therefore cannot use data_model's single-loaded-datasource globals, so
nothing here reads or writes them: every method takes what it needs from the
spec it was constructed with, or from a frame handed to it. That constraint is
the reason the computations these delegate to (`_describe_frame`,
`_all_cells_from_frame`, ...) are pure functions of a frame rather than of the
module state -- see `plexora/server/models/data_model.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from plexora.server.providers.base import (
    LOCAL,
    Fingerprint,
    ResourceLocator,
)
from plexora.server.providers.operations import run_table_operation


class LocalTableProvider:
    """The cell table, read from a file this process can open.

    Keeps a reference to whatever `load()` produced, so the read methods below
    have a frame to answer from without being handed one. That is not a second
    copy: it is the same polars DataFrame object the caller ends up holding
    (`data_model.datasource` on the primary, the resource record on a node),
    and it is replaced wholesale by the next `load()`.

    On the primary only `load()`, `fingerprint()` and `run()` are ever called
    -- the read methods exist for the node, whose request handlers use this
    exact class. One implementation, two transports.
    """

    is_local = True

    def __init__(self, spec, name: str | None = None):
        self._spec = spec
        self._name = name
        self._loaded = None

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind="table", provider=LOCAL,
                               path=self._spec.src if self._spec else None)

    @property
    def spec(self):
        return self._spec

    @property
    def frame(self):
        """The loaded table, or None before the first `load()`."""
        return self._loaded.table if self._loaded is not None else None

    # -- reading ---------------------------------------------------------

    def load(self, reload: bool = False):
        """Read the file into a NormalizedDatasource.

        `reload` is accepted and ignored: a local read never serves a cached
        copy, so re-reading is what every call already does. The parameter is
        in the signature because the node provider genuinely needs it -- the
        two must be callable through the same name.
        """
        from plexora.server.models.adapters import get_adapter

        self._loaded = get_adapter(self._spec.type)(self._spec).load_table()
        return self._loaded

    def read_obs_column(self, column: str):
        """One annotation column the loaded table does not carry, or None."""
        from plexora.server.models.adapters import get_adapter

        adapter = get_adapter(self._spec.type)(self._spec)
        read = getattr(adapter, "read_obs_column", None)
        return read(column) if read is not None else None

    def describe(self) -> dict:
        from plexora.server.models import data_model

        return data_model._describe_frame(self.frame)

    def all_cells(self, columns, data_type):
        from plexora.server.models import data_model

        return data_model._all_cells_from_frame(self.frame, columns, data_type)

    def filter_columns(self, columns) -> dict:
        from plexora.server.models import data_model

        return data_model._filter_columns_from_frame(self.frame, columns)

    def metadata_column(self, column):
        """One column's values, from the frame when it has them and from the
        source file's obs when it does not -- the same two-place lookup
        `data_model.get_metadata_column` documents, including the length check
        that refuses a column whose values do not line up with the table."""
        from plexora.server.models import data_model

        frame = self.frame
        if frame is not None and column in frame.columns:
            return data_model._frame_metadata_column(column, frame[column])
        result = self.read_obs_column(column)
        if result is None:
            raise KeyError(column)
        if frame is not None and len(result.values) != frame.height:
            raise ValueError(
                f"metadata column {column!r} has {len(result.values)} values but "
                f"the loaded table has {frame.height} rows"
            )
        return result

    def rows(self, ids) -> list:
        from plexora.server.models import data_model

        return data_model._rows_by_id(self.frame, ids)

    def geometry(self, columns) -> bytes:
        """The named columns as Arrow IPC bytes, dtypes intact.

        The compact copy a primary keeps of a node's table. Arrow rather than
        the packed float32 frame the range queries use, because two of these
        columns are routinely text -- the image id and the cell type -- and a
        float cast would turn either into NaN without saying so.
        """
        import io

        frame = self.frame
        wanted = [name for name in columns if frame is not None and name in frame.columns]
        buffer = io.BytesIO()
        (frame.select(wanted) if wanted else frame.head(0)).write_ipc(buffer)
        return buffer.getvalue()

    # -- identity --------------------------------------------------------

    def fingerprint(self) -> Fingerprint | None:
        """Size, mtime and the facts that make a mismatched pairing visible.

        The row count and the cell-id range travel with the file's stat
        because they are what a mask or a value buffer is checked against: an
        image with 900k cells and a table with 1.1M of them opens fine and
        colours the wrong cells, and nothing downstream can tell.
        """
        identity: dict[str, Any] = {"spec": _spec_hash(self._spec)}
        frame = self.frame
        if frame is not None:
            identity.update(_frame_identity(frame))
        return Fingerprint.of_path(self._spec.src, identity)

    # -- work that has to happen here -------------------------------------

    def run(self, operation: str, payload: Mapping[str, Any], dataset=None) -> Any:
        return run_table_operation(operation, dataset, payload)


class LocalSegmentationProvider:
    """The label mask, opened from this machine's filesystem.

    `derived` is what gets opened, never `source`: the pyramid Plexora built
    (or adopted) is the only thing the tile route can serve at every zoom
    level, and `refresh_segmentation_mapping` has already decided which file
    that is by the time this is constructed.
    """

    is_local = True

    def __init__(self, path: str | None):
        self._path = str(path) if path else None

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind="segmentation", provider=LOCAL, path=self._path)

    @property
    def path(self) -> str | None:
        return self._path

    def open(self):
        """The mask as a zarr array/group, or None when there is no mask.

        Verbatim from `load_datasource`: a `.zarr` store opens directly, and
        anything else goes through tifffile's zarr view. `is_ome=False` matters
        -- an OME reader would try to interpret a label image's metadata as a
        channel description.
        """
        if not self._path:
            return None
        import tifffile as tf
        import zarr

        if str(self._path).endswith('.zarr'):
            return zarr.open(self._path)
        seg_io = tf.TiffFile(self._path, is_ome=False)
        return zarr.open(seg_io.series[0].aszarr())

    def fingerprint(self) -> Fingerprint | None:
        if not self._path:
            return None
        return Fingerprint.of_path(self._path)


class LocalImageProvider:
    """The channel image, opened from this machine's filesystem.

    `open()` is the block `load_datasource` used to run inline, moved whole and
    otherwise untouched -- including the overview-level heuristic and the
    `block_reduce` step, whose comments explain why materializing that one
    level is bounded regardless of the source image's size.
    """

    is_local = True

    def __init__(self, path: str | None):
        self._path = str(path) if path else None

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind="image", provider=LOCAL, path=self._path)

    @property
    def path(self) -> str | None:
        return self._path

    def open(self):
        """(channels, zarray, metadata) -- the three globals, in one read.

        Returned together rather than as three methods because they come from
        one open file handle and one pyramid walk; splitting them would reopen
        a multi-gigabyte TIFF three times per load.
        """
        import numpy as np
        import tifffile as tf
        import zarr
        from ome_types import from_xml
        from skimage.measure import block_reduce

        channel_io = tf.TiffFile(self._path, is_ome=False)
        try:
            xml = channel_io.pages[0].tags['ImageDescription'].value
            metadata = from_xml(xml).images[0].pixels
        except:
            metadata = {}
        channels = zarr.open(channel_io.series[0].aszarr())

        level_series = next(
            level for level in reversed(channel_io.series[0].levels)
            if all(d >= 200 for d in level.shape[1:])
        )
        zarray = zarr.open(level_series.aszarr())
        if zarray.shape[1] > 400 or zarray.shape[2] > 400:
            x_reduce = zarray.shape[1] // 200
            y_reduce = zarray.shape[2] // 200
            reduce = np.min([x_reduce, y_reduce])
            # block_reduce needs a real strided numpy array -- zarray here is a
            # lazy zarr.Array, which has no .strides. This is already the
            # smallest pyramid level with both dims >= 200, so materializing it
            # is bounded regardless of the source image's full resolution.
            zarray = block_reduce(np.asarray(zarray), (1, reduce, reduce), np.mean)
        return channels, zarray, metadata

    def fingerprint(self) -> Fingerprint | None:
        if not self._path:
            return None
        return Fingerprint.of_path(self._path)


# -- identity helpers ----------------------------------------------------


def _spec_hash(spec) -> str:
    """A stable digest of the read spec a table was loaded under.

    Part of the table's identity because two projects can read the same .h5ad
    into completely different tables -- a different matrix, a different subset,
    a different id column -- and a cache keyed on the file alone would serve
    one project's columns to the other.
    """
    import hashlib
    import json

    if spec is None:
        return ""
    payload = json.dumps(spec.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _frame_identity(frame) -> dict:
    """What a loaded table claims about the cells it describes.

    Checked against the image's dimensions and the mask's label ceiling when a
    resource is attached, so a mismatched pairing is caught while the user is
    still looking at the attach screen rather than three panels later.
    """
    identity: dict[str, Any] = {"row_count": int(frame.height)}
    if "id" in frame.columns:
        column = frame["id"]
        identity["id_dtype"] = str(column.dtype)
        try:
            identity["id_min"] = int(column.min())
            identity["id_max"] = int(column.max())
        except (TypeError, ValueError):
            pass
    return identity


def image_geometry(path) -> dict:
    """An image file's shape, without loading it into the module globals.

    The same facts `convertOmeTiff` derives at import time, read again here
    because a node has to be able to answer "how big is it" for a file the
    primary has never seen.
    """
    import tifffile as tf
    import zarr

    channel_io = tf.TiffFile(str(path), is_ome=False)
    array = zarr.open(channel_io.series[0].aszarr())
    if hasattr(array, "shape"):
        levels, shape = 1, array.shape
        chunks = array.chunks
    else:
        levels = len(list(array))
        shape = array["0"].shape
        chunks = (1, 1024, 1024)
    return {
        "levels": levels,
        "num_channels": int(shape[0]),
        "height": int(shape[1]),
        "width": int(shape[2]),
        "tile_height": int(chunks[-2]),
        "tile_width": int(chunks[-1]),
    }


def path_exists(path) -> bool:
    return bool(path) and Path(path).exists()
