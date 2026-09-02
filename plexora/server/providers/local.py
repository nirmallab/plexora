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

    def load(self, reload: bool = False, stage=None, report=None):
        """Read the file into a NormalizedDatasource.

        `reload` is accepted and ignored: a local read never serves a cached
        copy, so re-reading is what every call already does. The parameter is
        in the signature because the node provider genuinely needs it -- the
        two must be callable through the same name.

        `stage`/`report` are the progress pair (data_model.table_progress).
        Both optional: a node calls this with neither, and so does every test.
        """
        from plexora.server.models.adapters import get_adapter

        self._loaded = get_adapter(self._spec.type)(self._spec).load_table(
            stage=stage, report=report)
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

    def _reads_colour(self) -> bool:
        from plexora.server.utils import brightfield

        return self._rgb or brightfield.is_rgb_layout(self._path)

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

    `pyramid` is the project's derived coarse levels for an OME-Zarr store that
    arrived without enough of its own (see server/utils/ome_zarr.py). It is
    threaded rather than rediscovered because only the project knows where its
    own derived files went.
    """

    is_local = True

    def __init__(self, path: str | None, pyramid: str | None = None,
                 rgb: bool = False):
        self._path = str(path) if path else None
        self._pyramid = str(pyramid) if pyramid else None
        #: Read this image as colour even though its own tags do not say so.
        #: The one thing about an image file that cannot be answered by looking
        #: at it: three `minisblack` planes are a legal way to write RGB and a
        #: legal way to write a 3-plex panel, and only the project (or the
        #: person who ran the scan) knows which. `is_rgb_layout` covers every
        #: file that DOES declare itself, so this is False for all of them.
        self._rgb = bool(rgb)

    @property
    def locator(self) -> ResourceLocator:
        return ResourceLocator(kind="image", provider=LOCAL, path=self._path)

    @property
    def path(self) -> str | None:
        return self._path

    def _missing_pyramid(self) -> str | None:
        """The recorded derived levels, rebuilding them if they have gone.

        They live under the project directory, which people clear out. Opening
        without them would leave the project claiming a `maxLevel` its pyramid
        no longer reaches -- every zoomed-out tile a 500 -- so the one-off cost
        of deriving them again is the better answer than serving a broken
        viewer. Nothing rebuilds a pyramid that is merely *stale*: that needs
        the source to have changed, which re-registering is the answer to.
        """
        from plexora.server.utils import brightfield, dicom_wsi, ome_zarr

        if not self._pyramid or Path(self._pyramid).exists():
            return self._pyramid
        try:
            if dicom_wsi.is_dicom_path(self._path):
                return dicom_wsi.build_extension(
                    dicom_wsi.open_image(self._path, rgb=self._rgb), self._pyramid)
            if self._reads_colour():
                return brightfield.build_extension(
                    brightfield.open_rgb(self._path), self._pyramid)
            return ome_zarr.build_extension(
                ome_zarr.open_image(self._path), self._pyramid)
        except Exception:
            # Read-only project directory, a disk that filled up. Fewer levels
            # is a worse viewer; refusing to open the image at all is no viewer.
            return None

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

        from plexora.server.utils import brightfield, dicom_wsi, ome_zarr

        # An OME-Zarr store is already the shape the tile route slices, so it
        # is opened directly -- the same move LocalSegmentationProvider has
        # always made for a .zarr mask, one level up.
        if ome_zarr.is_zarr_image_path(self._path):
            channels = ome_zarr.open_image(self._path,
                                           extension=self._missing_pyramid())
            return (channels, ome_zarr.overview_plane(channels),
                    ome_zarr.physical_metadata(channels))

        # Before the colour test below, not after: a DICOM H&E project carries
        # rgb=True, and `_reads_colour` would hand the slide to OpenSlide --
        # which reads DICOM, flattens it, and would quietly serve the wrong
        # thing. DICOM answers both questions itself (whether it is colour, and
        # how to read it), so it is dispatched on being DICOM at all.
        if dicom_wsi.is_dicom_path(self._path):
            channels = dicom_wsi.open_image(
                self._path, extension=self._missing_pyramid(), rgb=self._rgb)
            return (channels, dicom_wsi.overview_plane(channels),
                    dicom_wsi.physical_metadata(channels))

        # Dispatched on the file's storage layout, not on the project's
        # recorded kind: a node has no project, and an interleaved-RGB file
        # read by the branch below would record its own height as its channel
        # count. This is the reading a "Fluorescence" override gets too -- the
        # levels present as (channel, y, x) either way.
        if self._reads_colour():
            channels = brightfield.open_rgb(self._path,
                                            extension=self._missing_pyramid())
            return (channels, brightfield.overview_plane(channels),
                    brightfield.physical_metadata(self._path))

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

    def _reads_colour(self) -> bool:
        from plexora.server.utils import brightfield

        return self._rgb or brightfield.is_rgb_layout(self._path)

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


def image_geometry(path, pyramid=None, rgb=False) -> dict:
    """An image file's shape, without loading it into the module globals.

    The same facts `convertOmeTiff` derives at import time, read again here
    because a node has to be able to answer "how big is it" for a file the
    primary has never seen.

    Dispatching on the path rather than on the project's recorded kind is what
    lets a node serve an OME-Zarr store: a node has no project to have recorded
    anything, and this is the one place that has to tell the formats apart.
    """
    import tifffile as tf
    import zarr

    from plexora.server.utils import brightfield, dicom_wsi, ome_zarr

    if ome_zarr.is_zarr_image_path(path):
        return ome_zarr.geometry(ome_zarr.open_image(path, extension=pyramid))

    # Same order as `LocalImageProvider.open` above, and load-bearing for the
    # same reason: a DICOM slide must never reach the OpenSlide branch.
    if dicom_wsi.is_dicom_path(path):
        return dicom_wsi.geometry(
            dicom_wsi.open_image(path, extension=pyramid, rgb=rgb))

    if rgb or brightfield.is_rgb_layout(path):
        return brightfield.geometry(brightfield.open_rgb(path, extension=pyramid))

    channel_io = tf.TiffFile(str(path), is_ome=False)
    array = zarr.open(channel_io.series[0].aszarr())
    if hasattr(array, "shape"):
        levels, shape = 1, array.shape
        chunks = array.chunks
        level_shapes = [[int(shape[-2]), int(shape[-1])]]
    else:
        keys = sorted(list(array), key=lambda k: int(k))
        levels = len(keys)
        shape = array["0"].shape
        chunks = (1, 1024, 1024)
        # Every level's own dimensions, not width >> level. Real pyramids are
        # not all exact halvings -- an odd dimension rounds, and some writers
        # stop early -- and a caller clipping a read box against a computed
        # size would clip against the wrong rectangle.
        level_shapes = [[int(array[key].shape[-2]), int(array[key].shape[-1])]
                        for key in keys]
    return {
        "levels": levels,
        "num_channels": int(shape[0]),
        "height": int(shape[1]),
        "width": int(shape[2]),
        "tile_height": int(chunks[-2]),
        "tile_width": int(chunks[-1]),
        "level_shapes": level_shapes,
    }


def path_exists(path) -> bool:
    return bool(path) and Path(path).exists()
