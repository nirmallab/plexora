"""Adapters for a table that is already in this process's memory.

The notebook case. `plexora.view(adata=...)` hands over a live AnnData, and the
kernel serves it to the viewer without writing an .h5ad first (see
`plexora/memory.py`). What reaches this module is the SNAPSHOT of that object --
a zarr group over an in-memory store, or a polars frame -- and the whole job
here is to make it readable by the adapters that already exist.

Both classes are one override each, which is the point:

- `MemoryAnnDataAdapter` replaces `_open_group()` and nothing else. Every read
  spec question -- the subset, the coordinate source, the layer, the id column,
  the blocked matrix stream -- is answered by `AnnDataAdapter` against the group
  it is handed, and `read_elem`/`sparse_dataset`/array slicing work identically
  on a MemoryStore-backed group and an h5py file. This is the same seam
  `SpatialDataAdapter` uses; a third reader was never needed.
- `MemoryFrameAdapter` replaces `_read_frame()` and nothing else, so a pandas
  DataFrame gets exactly the normalization a CSV gets -- the positional `id`,
  the -inf guard, the marker split, the optional log1p.

Deliberately NOT registered in `adapters._ADAPTERS`. A project's `spec.type`
stays `"anndata"` or `"csv"`, because that is what the table IS and what a user
would have to re-answer if it changed; where the bytes are living this session
is the node resource's business, not the project's. Nothing looks these up by
name -- `MemoryTableProvider` constructs them directly with the snapshot.
"""

from __future__ import annotations

from .anndata_adapter import AnnDataAdapter
from .csv_adapter import CsvAdapter


class MemoryAnnDataAdapter(AnnDataAdapter):
    """An AnnData read from a zarr group this process is holding open.

    `group` is a snapshot rather than the user's live AnnData, and that is a
    correctness requirement, not an optimization: the notebook goes on mutating
    its object between cells, and an adapter reading it directly could produce a
    table whose obs came from before an edit and whose X came from after.
    """

    def __init__(self, spec, group):
        super().__init__(spec)
        self._group = group

    def _open_group(self):
        """The snapshot group -- the ONE thing this adapter overrides.

        A `nullcontext` for the same reason SpatialDataAdapter uses one: there
        is no descriptor to close, and every caller uses `_open_group()` through
        `with`.
        """
        import contextlib

        return contextlib.nullcontext(self._group)

    def _read_adata(self):
        """The whole snapshot as an AnnData.

        Only reached by callers that genuinely want an eager object -- see
        `AnnDataAdapter._read_adata`. Nothing on the loading path comes here,
        because `plan()` and `stream()` both go through `_open_group` above.
        """
        import anndata as ad

        return ad.read_zarr(self._group.store)


class MemoryFrameAdapter(CsvAdapter):
    """A flat table this process is holding as a polars frame.

    What `plexora.view(table=df)` produces. The frame is a snapshot taken when
    the viewer was opened -- same reasoning as above -- and everything after the
    read is `CsvAdapter`'s, so a DataFrame and the CSV somebody would have
    written it to normalize identically.
    """

    def __init__(self, spec, frame):
        super().__init__(spec)
        self._frame = frame

    def _read_frame(self):
        return self._frame
