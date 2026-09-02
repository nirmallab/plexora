from __future__ import annotations

import numpy as np
import polars as pl

from .base import MetadataColumn, NormalizedDatasource, TablePlan

# Column name used to hold the resolved observation ID (adata.obs_names, or a
# custom obs column when `obs_id_field` is set) in the materialized table,
# when the caller doesn't provide an explicit obs_id_field. data_model.py's
# gating/query code (e.g. get_channel_cells) treats config['featureData'][0]
# ['idField'] as a literal column name that must exist in the loaded table --
# unlike CSV, AnnData has no single "the ID column" without this fallback.
DEFAULT_ID_COLUMN = "obs_id"

# These three column names are load-bearing: 'id' is the positional row index
# every downstream gating/query function keys off, 'X'/'Y' are the resolved
# coordinate columns. If a real obs column (e.g. a literal "id" column, seen
# in real exemplar data) were used as obs_id_field/celltype without this
# guard, it would silently overwrite one of these dict keys when the table is
# built below -- corrupting the positional id or coordinates rather than
# raising. See _reject_reserved_collisions().
_RESERVED_COLUMN_NAMES = {"id", "X", "Y"}

# The "does this column identify an image/sample/region" heuristic lives in
# classify.py, which owns every column-name vocabulary in one place. Re-exported
# here because this module is where the ambiguity guard below enforces it, and
# adapters/inspection.py has always imported it from this name.
from .classify import is_likely_image_identifier_name  # noqa: F401
from .classify import is_numeric_dtype


def _read_elem(node):
    """anndata's reader for one on-disk element, public path preferred.

    The private import is the fallback for older anndata, matching what the ROI
    plugin's exporter does against the same files."""
    try:
        from anndata.io import read_elem  # anndata >= 0.10, public API
    except ImportError:  # pragma: no cover - older anndata
        from anndata._io.specs import read_elem

    return read_elem(node)


#: Rows read from the matrix at a time. Sized so one block is a working set of
#: tens of MB whatever the panel width -- that product, not the file, is what
#: bounds the build's memory. Clamped low so a 1500-plex panel still gets
#: blocks worth reading, and high so a 3-marker table is not read in dribbles.
_BLOCK_TARGET_BYTES = 64 * 1024 * 1024
_MIN_BLOCK_ROWS = 1024
_MAX_BLOCK_ROWS = 65536


def _block_rows(n_features: int, itemsize: int = 4) -> int:
    width = max(1, int(n_features)) * max(1, int(itemsize))
    return int(min(_MAX_BLOCK_ROWS, max(_MIN_BLOCK_ROWS, _BLOCK_TARGET_BYTES // width)))


# -- on-disk node helpers -----------------------------------------------------
#
# All of these work identically against an h5py File/Group and a zarr Group,
# which is what lets SpatialDataAdapter override `_open_group` and nothing else.


def _child(group, name):
    """One child of an on-disk group, or None. `in` rather than `.get`, because
    h5py Groups and zarr Groups disagree about `.get`'s default handling."""
    try:
        return group[name] if name in group else None
    except (KeyError, TypeError):
        return None


def _child_keys(group, name) -> list[str]:
    node = _child(group, name)
    if node is None:
        return []
    try:
        return [str(key) for key in node.keys()]
    except (AttributeError, TypeError):
        return []


def _encoding_of(node) -> str:
    """anndata's own label for how a node is stored: '', 'csr_matrix', 'csc_matrix'."""
    try:
        return str(node.attrs.get("encoding-type") or "")
    except (AttributeError, TypeError):
        return ""


def _matrix_shape(node):
    """(rows, columns) of an on-disk matrix, from metadata only.

    A dense matrix carries its shape on the array; a sparse one is a GROUP and
    carries it in an attr. Neither read costs a value.
    """
    shape = getattr(node, "shape", None)
    if shape is None:
        try:
            shape = node.attrs.get("shape")
        except (AttributeError, TypeError):
            shape = None
    if shape is None or len(shape) < 2:
        return None, None
    return int(shape[0]), int(shape[1])


def _sparse_dataset(node):
    """anndata's backed sparse wrapper, which slices rows off disk via indptr.

    `should_cache_indptr` is left at its default True: the group is opened once
    and read many times, which is exactly the case that default is for.
    """
    try:
        from anndata.io import sparse_dataset  # anndata >= 0.11, public API
    except ImportError:  # pragma: no cover - older anndata
        from anndata._core.sparse_dataset import sparse_dataset

    return sparse_dataset(node)


def _dense_block(dataset, row_indices, start, stop):
    """Rows [start, stop) of the planned table, dense, as a 2-D array.

    Three shapes of read, and the distinction is worth the branch: no subset at
    all is a plain slab; a subset whose rows happen to be contiguous -- which is
    the ordinary case, since one image's cells are written together -- is also a
    slab, just an offset one; only a genuinely scattered subset pays for fancy
    indexing. h5py requires the index list to be ascending, which
    `np.flatnonzero` guarantees.
    """
    if row_indices is None:
        block = dataset[start:stop]
    else:
        wanted = row_indices[start:stop]
        first, last = int(wanted[0]), int(wanted[-1])
        if last - first + 1 == len(wanted):
            block = dataset[first:last + 1]
        else:
            block = dataset[wanted]
    if hasattr(block, "toarray"):
        block = block.toarray()
    return np.asarray(block)


def _finish_features(values, apply_log_transform):
    """The two transforms every feature column gets, in order, as float32.

    -inf is scrubbed to 0 before the log, not after: log1p(-inf) is nan, and a
    nan marker silently drops the cell out of every gate rather than gating it
    at zero.
    """
    values = np.asarray(values, dtype=np.float32)
    values = np.where(np.isneginf(values), np.float32(0.0), values)
    if apply_log_transform:
        values = np.log1p(values)
    return np.ascontiguousarray(values, dtype=np.float32)


def _describe_obsm_mapping(obsm) -> list[dict]:
    """`describe_obsm` against a bare mapping rather than an AnnData."""
    entries = []
    if obsm is None:
        return entries
    try:
        keys = list(obsm.keys())
    except (AttributeError, TypeError):
        return entries
    for name in keys:
        if not name:
            continue
        entry = {"name": str(name)}
        shape = getattr(obsm[name], "shape", None)
        if shape is not None:
            entry["shape"] = [int(dim) for dim in shape]
        entries.append(entry)
    return entries


def describe_obsm(adata) -> list[dict]:
    """Each obsm array as {"name", "shape"}.

    Lives here rather than in inspection.py because both this adapter and that
    module need it, and inspection already imports from this direction --
    the reverse would be a cycle.

    Shape is read off the array's own metadata, never by materializing it: a
    backed h5ad and a zarr group both report it without a read, and an
    embedding on a million-cell table is not something to load in order to
    label a dropdown. An entry whose shape cannot be determined still appears,
    without one -- leaving it out would hide a candidate, which is the failure
    this list exists to prevent.
    """
    return _describe_obsm_mapping(adata.obsm)


def _likely_image_identifier_columns(obs) -> list[str]:
    """Obs columns whose NAME says image and whose VALUES say more than one.

    Takes the obs frame rather than an AnnData, because the guard now runs
    before anything opens the matrix -- which is the whole point of it.
    """
    candidates = []
    for column in obs.columns:
        if not is_likely_image_identifier_name(column):
            continue
        if _distinct_count(obs[column]) > 1:
            candidates.append(column)
    return candidates


def _distinct_count(column) -> int:
    """How many distinct values a column holds, read off its categories when it
    has them -- which is free, where `nunique()` on a stringified copy is one
    Python object per row."""
    category = getattr(column, "cat", None)
    if category is not None:
        codes = np.asarray(category.codes)
        return int(np.unique(codes[codes >= 0]).size)
    return int(column.nunique(dropna=True))


def _deduplicate_names(names: list[str]) -> list[str]:
    """Real multiplexed-imaging panels commonly re-stain/re-image the same
    marker across cycles (e.g. PTPRC/CD45 twice), producing duplicate
    adata.var_names -- confirmed against real exemplar data. Auto-suffixing
    (matching anndata's own var_names_make_unique() convention) is more
    useful than hard-failing on a very ordinary occurrence.
    """
    seen: dict[str, int] = {}
    result = []
    for name in names:
        if name not in seen:
            seen[name] = 0
            result.append(name)
        else:
            seen[name] += 1
            result.append(f"{name}_{seen[name]}")
    return result



class _LazyObs:
    """The obs frame, read one column at a time.

    `read_elem(group["obs"])` materializes EVERY annotation column plus the
    whole index -- measured at ~380 MB for a 1.2M-cell table, which became the
    floor under a load that no longer reads the matrix at all. Almost nothing
    wants the whole frame: the multi-image guard asks a couple of
    identifier-shaped columns for their distinct counts, and the plan wants the
    subset column, the coordinates, the identifier and the celltype. So a
    column is read when it is asked for and cached after, and a column nobody
    asks for is never read at all.

    Presents just enough of the pandas surface that `_subset_mask` and
    `_likely_image_identifier_columns` work against either this or a real
    DataFrame -- both are also called with a real one from elsewhere.
    """

    def __init__(self, group):
        self._node = group["obs"]
        self._index_key = str(self._node.attrs.get("_index", "_index"))
        # The file's own column order when it records one, which is what a user
        # picking from a dropdown expects to see; the group's key order is the
        # fallback and is arbitrary.
        order = None
        try:
            order = self._node.attrs.get("column-order")
        except (AttributeError, TypeError):
            order = None
        if order is not None and len(order):
            self.columns = [str(name) for name in order]
        else:
            self.columns = [str(key) for key in self._node.keys()
                            if str(key) != self._index_key]
        self._cache = {}

    def take(self, name, row_indices):
        """One obs column, restricted to the planned rows, read on disk.

        The subset-aware counterpart to `__getitem__`, for the columns the plan
        only ever needs the kept rows of -- the coordinates, the celltype, the
        identifier. `__getitem__` still reads the whole column, because the two
        callers that need one (the subset mask and the multi-image guard) are
        asking a question ABOUT every row.
        """
        key = str(name)
        if key in self._cache:
            return _take(self._cache[key], row_indices)
        taken = _node_take(self._node[key], row_indices)
        if taken is None:
            return _take(self[key], row_indices)
        return taken

    def __contains__(self, name):
        return str(name) in self.columns

    def __getitem__(self, name):
        import pandas as pd

        key = str(name)
        if key not in self._cache:
            self._cache[key] = pd.Series(_read_elem(self._node[key]))
        return self._cache[key]

    def index_take(self, row_indices):
        """The observation names for the planned rows, as text.

        Sliced ON DISK, which is the whole point. `read_elem` on the index
        builds a pandas string array covering every source row -- measured at
        **253 MB** for a 1.2M-cell table, and it was the single largest
        allocation left in a load that no longer reads the matrix at all, paid
        in full to keep twenty thousand names. h5py and zarr both take an
        ascending index list and return only those elements.

        The fallback covers an index that is not a plain array (a categorical
        one, encoded as a group), where `read_elem` is the only reader that
        understands the encoding.
        """
        taken = _node_take(self._node[self._index_key], row_indices)
        if taken is None:
            taken = _take(_read_elem(self._node[self._index_key]), row_indices)
        return _as_text(np.asarray(taken))

    @property
    def n_rows(self) -> int:
        """Row count from array metadata, without reading a column."""
        for key in ([self._index_key] + self.columns):
            try:
                node = self._node[key]
            except (KeyError, TypeError):
                continue
            shape = getattr(node, "shape", None)
            if shape:
                return int(shape[0])
            for child in ("codes", "values", "data"):
                child_shape = getattr(_child(node, child), "shape", None)
                if child_shape:
                    return int(child_shape[0])
        return 0


def _take(values, row_indices):
    """`values` restricted to the planned rows, or unchanged when all are kept.

    Each branch slices the container in its OWN representation. Going through
    `np.asarray` first would be correct and ruinous: on a pandas string array
    it materializes every source row as a Python object before throwing all but
    the subset away, which is the cost the subset exists to avoid.
    """
    if row_indices is None:
        return values
    if hasattr(values, "iloc"):
        return values.iloc[row_indices]
    if hasattr(values, "take"):           # pandas/Arrow extension array
        return values.take(row_indices)
    return np.asarray(values)[row_indices]


def _node_take(node, row_indices):
    """Rows of one on-disk obs element, read WITHOUT materializing the element.

    `read_elem` is the general reader and it always reads everything: on a
    1.2M-cell table, pulling the observation index that way cost **253 MB** and
    was the largest allocation left in a load that no longer touches the matrix
    at all -- paid in full to keep the twenty thousand rows of one image.

    So the three encodings that actually occur are sliced at the array level
    instead. Which one a column gets depends on its cardinality, not its dtype:
    anndata wrote a three-value string column as `categorical` and a
    1.2M-unique string index as `nullable-string-array` in the same file.

    Returns None for anything else, which is the caller's signal to fall back to
    `read_elem`. Correctness first: an encoding this does not recognise is read
    the slow, known-good way rather than guessed at.
    """
    import pandas as pd

    def rows(child):
        return child[row_indices] if row_indices is not None else child[:]

    if getattr(node, "shape", None) is not None:
        return pd.Series(rows(node))

    encoding = _encoding_of(node)
    if encoding == "categorical":
        codes = _child(node, "codes")
        categories = _child(node, "categories")
        if codes is None or categories is None:
            return None
        # Categories are the distinct levels, not one entry per row, so they are
        # read whole -- that is the point of the encoding.
        return pd.Series(pd.Categorical.from_codes(
            np.asarray(rows(codes)),
            categories=[_text(value) for value in _read_elem(categories)]))

    if encoding.startswith("nullable-"):
        values = _child(node, "values")
        if values is None:
            return None
        taken = np.asarray(rows(values), dtype=object)
        mask = _child(node, "mask")
        if mask is not None:
            # pandas' masked convention, which anndata mirrors: True is missing.
            missing = np.asarray(rows(mask), dtype=bool)
            if missing.any():
                taken = taken.copy()
                taken[missing] = None
        return pd.Series([None if value is None else _text(value) for value in taken]
                         if encoding == "nullable-string-array" else taken)
    return None


def _text(value):
    """One value as text, decoding HDF5's bytes (zarr hands back str already)."""
    return value.decode() if isinstance(value, bytes) else str(value)


def _as_text(values) -> np.ndarray:
    """A string array, decoding HDF5's bytes.

    h5py hands back `bytes` for a variable-length string dataset where zarr
    hands back `str`; `str(b"c0")` is "b'c0'", so the decode is not optional.
    """
    return np.asarray([_text(value) for value in values], dtype=object)


def _string_equals(column, value) -> np.ndarray:
    """`column.astype(str) == str(value)`, without stringifying the column.

    `astype(str).to_numpy()` builds one Python string per row -- on a 1.2M-cell
    multi-image table that was most of what choosing an image cost, and it was
    paid before a single intensity had been read. An image-id column is
    essentially always categorical, and a categorical answers exactly this
    question from its integer codes.
    """
    target = str(value)
    category = getattr(column, "cat", None)
    if category is not None:
        levels = [str(level) for level in category.categories]
        try:
            code = levels.index(target)
        except ValueError:
            return np.zeros(len(column), dtype=bool)
        return np.asarray(category.codes) == code
    return column.astype(str).to_numpy() == target


class AnnDataAdapter:
    """Adapter for AnnData (.h5ad)-backed datasources.

    Takes the project's DataSpec (server/models/project.py). `coordinates`,
    `features` and `subset` are the read spec -- how to get from the file to a
    table -- and are the adapter's own vocabulary; the project record stores
    them without interpreting them. Roles describe the table that comes out.

    The table this produces always has a positional 'id' column plus 'X'/'Y',
    which is why those three names are reserved below.
    """

    def __init__(self, spec):
        self.spec = spec
        self.path = spec.src
        self.coordinates = dict(spec.coordinates or {})
        self.features = dict(spec.features or {}) or {'source': 'X'}
        self.subset = dict(spec.subset or {})
        self.celltype_column = spec.roles.celltype
        # The obs column the user named as the image identifier, if they did.
        # An answer beats the name heuristic below: it is the only way to catch
        # a table keyed on a column called "roi" or "core", which the heuristic
        # does not recognise and would wave through.
        self.image_id_column = spec.roles.image_id
        # Which obs column supplies the identifier, or None for the positional
        # row index (the default -- see the uint32-packing note in
        # datasource.py). Deliberately read from the read spec rather than
        # inferred from the cell_id role: an obs column literally named "id"
        # exists in real data, and it must hit the reserved-name guard below
        # rather than being mistaken for "just number the rows".
        self.obs_id_field = spec.obs_id_field
        # Explicit opt-in only -- no heuristic guessing at whether the
        # chosen feature source "looks" already transformed. Same contract and
        # same transform as CsvAdapter, so a threshold means the same thing
        # whichever format the project was imported from.
        self.apply_log_transform = bool(spec.is_transformed)

    def _open_group(self):
        """The on-disk AnnData as a mapping, open for reading.

        THE format-specific step, and the only one: `sparse_dataset`,
        `read_elem` and plain array slicing all work identically against an
        h5py File and a zarr group, so everything downstream of this is shared
        verbatim with SpatialDataAdapter, which overrides only this method.

        A context manager, because h5py owns a file descriptor that has to be
        closed and zarr does not -- see the nullcontext in
        adapters/spatialdata_adapter.py.
        """
        import h5py

        return h5py.File(self.path, "r")

    def _read_adata(self):
        """The whole thing, in memory.

        Only two callers remain, both deliberate: `load_table()`, which exists
        for code (and tests) that genuinely want an eager frame, and `stream()`'s
        fallback for an adapter that has not implemented `_open_group`. Nothing
        on the loading path should reach for this -- that is what `plan()` and
        `stream()` are for.
        """
        import anndata as ad

        return ad.read_h5ad(self.path)

    def _read_obs(self):
        """adata.obs on its own, as a pandas DataFrame.

        Deliberately not `self._read_adata().obs`: that materializes X, which is
        the expensive part of the file and the one part a metadata column can
        never need. Reading the obs element alone turns "colour cells by a
        phenotype column" from a full re-import into a directory read.

        `anndata.io.read_elem` is the public reader for one element of an
        on-disk AnnData; the private path is the fallback for older versions,
        matching what the ROI plugin's exporter does against the same files.
        """
        with self._open_group() as group:
            return _read_elem(group["obs"])

    def _subset_mask(self, obs):
        """The rows load_table() keeps, or None when it keeps every row.

        The single source of truth for the subset, so anything reading obs
        outside load_table() lands on the same cells. Getting this wrong is not
        a visible error -- the values simply belong to different cells than the
        ones on screen, shifted by however many rows the other image contributed.
        """
        column = self.subset.get('column')
        if not column:
            return None
        if column not in obs.columns:
            raise ValueError(f"Subset column {column!r} not found in adata.obs")
        value = self.subset.get('value')
        mask = _string_equals(obs[column], value)
        if not mask.any():
            raise ValueError(f"No observations match {column}={value!r}")
        return mask

    def read_obs_column(self, name: str) -> MetadataColumn:
        """One obs column, subset the same way the loaded table was.

        Raises KeyError for a column the file does not have, which is how the
        caller tells "no such column" from "this format has nothing to add"
        (CsvAdapter returns None for the latter).
        """
        with self._open_group() as group:
            obs = _LazyObs(group)
            if name not in obs.columns:
                raise KeyError(name)
            mask = self._subset_mask(obs)
            column = obs[name]
            if mask is not None:
                column = column[mask]

        categories = None
        if hasattr(column, "cat"):
            # The file's own level order (see MetadataColumn.categories). Taken
            # before the values are flattened to strings, which is the only
            # moment it still exists.
            categories = tuple(str(level) for level in column.cat.categories)
            column = column.astype(object)
        values = column.to_numpy()
        return MetadataColumn(name=name, values=values, categories=categories)

    # -- planning: everything that needs no matrix read ---------------------

    def plan(self) -> TablePlan:
        """See TablePlan. Reads obs and var; never opens the matrix.

        This is where every read-spec error is raised, which is what lets a
        route validate a user's answer -- and restore the previous project when
        it is wrong -- in milliseconds rather than behind a full file read.
        """
        with self._open_group() as group:
            obs = _LazyObs(group)
            row_indices = self._plan_rows(obs)
            n_rows = int(len(row_indices) if row_indices is not None else obs.n_rows)
            if n_rows == 0:
                raise ValueError("Resolved AnnData subset has zero observations")

            x_values, y_values = self._resolve_coordinates(group, obs, row_indices)
            if not np.isfinite(x_values).all() or not np.isfinite(y_values).all():
                raise ValueError("Resolved X/Y coordinates contain non-finite values")

            names, source, obs_values = self._plan_features(group, obs, row_indices)
            id_field_name, id_values = self._resolve_identifier(obs, row_indices)

            names = _deduplicate_names(names)
            reserved = _RESERVED_COLUMN_NAMES | {id_field_name}
            collisions = [name for name in names if name in reserved]
            if collisions:
                raise ValueError(f"Feature name(s) collide with reserved columns: {collisions}")

            columns = {
                "id": np.arange(n_rows, dtype=np.int64),
                # Coordinates stay float64 while the markers narrow to float32.
                # There are two of them against forty of those, so the saving
                # would be noise -- and they are positions: a marker rounded in
                # the seventh digit is the same marker, a centroid rounded in
                # the seventh digit is a cell drawn somewhere else.
                "X": x_values,
                "Y": y_values,
                id_field_name: id_values,
            }
            columns.update(obs_values)

            celltype_column = None
            if self.celltype_column:
                if self.celltype_column not in obs.columns:
                    raise ValueError(
                        f"celltype column {self.celltype_column!r} not found in adata.obs")
                if self.celltype_column in reserved:
                    raise ValueError(
                        f"celltype column {self.celltype_column!r} collides with a "
                        f"reserved/ID column name -- choose a different column.")
                columns[self.celltype_column] = obs.take(
                    self.celltype_column, row_indices).astype(str).to_numpy()
                celltype_column = self.celltype_column

            return TablePlan(
                rows=n_rows,
                row_indices=row_indices,
                columns=columns,
                feature_columns=list(names),
                feature_source=source,
                id_column="id",
                obs_id_column=id_field_name,
                x_column="X",
                y_column="Y",
                celltype_column=celltype_column,
                obs_columns=list(obs.columns),
                # anndata's Layers mapping can report a spurious `None` key
                # (observed with anndata 0.13.2) even when no such layer exists
                # -- filtered out here the same way adapters/inspection.py does.
                layers=[str(k) for k in _child_keys(group, "layers") if k],
                # Recorded so the coordinate question has candidates to offer
                # without reopening the file. Same codec as inspection's, so the
                # import path and the edit page describe an array identically.
                obsm=_describe_obsm_mapping(_child(group, "obsm")),
            )

    def _plan_rows(self, obs):
        """Which source rows the table keeps, ascending, or None for all.

        The multi-image guard lives here rather than in `load_table` because
        this is now the first thing that looks at the file, and refusing to
        load sixty images onto one image has to happen before anything
        expensive -- not after the read that the refusal was supposed to avoid.
        """
        if self.subset.get('column'):
            return np.flatnonzero(self._subset_mask(obs))

        named = self.image_id_column
        if named and named in obs.columns:
            # The user told us which column identifies the image, so ask that
            # column rather than guessing which one to ask. This is the check
            # the name heuristic below cannot make: it only fires for
            # conventionally-named columns, so a table keyed on an unrecognised
            # name loaded whole and drew several images' cells over one image,
            # with nothing said.
            images = _distinct_count(obs[named])
            if images > 1:
                raise ValueError(
                    f"Column {named!r} covers {images} images, but no "
                    "subset was specified -- loading them all would draw "
                    "several images' cells over one image. Choose which "
                    "image to load."
                )
        else:
            ambiguous = _likely_image_identifier_columns(obs)
            if ambiguous:
                raise ValueError(
                    "AnnData object has candidate image/sample identifier "
                    f"column(s) {ambiguous} with more than one distinct "
                    "value, but no subset was specified -- refusing to "
                    "silently load all observations. Set dataSource.subset "
                    "(or subset_by/subset_value) to pick one image/sample."
                )
        return None

    def _resolve_identifier(self, obs, row_indices):
        """(column name, values) for the observation identifier."""
        if self.obs_id_field:
            if self.obs_id_field not in obs.columns:
                raise ValueError(f"obs_id_field {self.obs_id_field!r} not found in adata.obs")
            if self.obs_id_field in _RESERVED_COLUMN_NAMES:
                raise ValueError(
                    f"obs_id_field {self.obs_id_field!r} collides with a reserved "
                    f"column name ({sorted(_RESERVED_COLUMN_NAMES)}) -- choose a "
                    "different observation ID column, or leave it unset to use "
                    "adata.obs_names."
                )
            column = obs.take(self.obs_id_field, row_indices)
            # A numeric obs column stays numeric. The usual reason to name one
            # here is that it holds the segmentation mask's own label values,
            # and the centroid cache packs the cell id into a uint32 -- a
            # stringified integer only survives that round trip by being parsed
            # back out again. The index below has no such expectation and is
            # genuinely text, so it keeps the str cast.
            values = (column.to_numpy() if is_numeric_dtype(column.dtype)
                      else column.astype(str).to_numpy())
            return self.obs_id_field, values
        # Sliced on disk, not after: the subset is what the table holds, and
        # reading every source row's name first is the difference between
        # twenty thousand strings and one and a half million.
        return DEFAULT_ID_COLUMN, obs.index_take(row_indices)

    def _plan_features(self, group, obs, row_indices):
        """(names, source, obs-sourced values) for the marker columns.

        Names and the matrix's width come from `var` and the matrix's own shape
        attribute -- metadata on both counts. Only the 'obs' source produces
        values here, because those are annotation columns already in hand and
        there is no matrix to stream.
        """
        source = self.features.get('source', 'X')
        if source == 'obs':
            obs_columns = self.features.get('obs_columns') or []
            if not obs_columns:
                raise ValueError(
                    "dataSource.features.obs_columns is required when features.source='obs'")
            missing = [c for c in obs_columns if c not in obs.columns]
            if missing:
                raise ValueError(f"obs feature columns not found in adata.obs: {missing}")
            values = {}
            for name in obs_columns:
                values[name] = _finish_features(
                    obs.take(name, row_indices).to_numpy(dtype=np.float64),
                    self.apply_log_transform)
            return list(obs_columns), ('obs', None), values

        if source == 'X':
            node, layer = self._matrix_node(group, None), None
        elif source == 'layer':
            layer = self.features.get('layer')
            if not layer:
                raise ValueError(
                    "dataSource.features.layer is required when features.source='layer'")
            node = self._matrix_node(group, layer)
        else:
            raise ValueError(f"Unknown features.source: {source!r}")

        var = _read_elem(group["var"])
        names = [str(v) for v in var.index]
        width = _matrix_shape(node)[1]
        if width is not None and width != len(names):
            raise ValueError(
                f"The feature matrix has {width} columns but var names {len(names)} "
                "-- the file's var index does not describe its matrix.")
        return names, ('layer' if layer else 'X', layer), {}

    def _matrix_node(self, group, layer):
        """The on-disk node holding a feature matrix, unread."""
        if layer is None:
            node = _child(group, "X")
            if node is None:
                raise ValueError(
                    "This file has no X matrix -- choose a layer, or a set of "
                    "obs columns, to read the marker values from.")
            return node
        layers = _child(group, "layers")
        if layers is None or layer not in _child_keys(group, "layers"):
            raise ValueError(f"Layer {layer!r} not found in adata.layers")
        return layers[layer]

    # -- streaming: the only part that touches the matrix --------------------

    def stream(self, plan: TablePlan, sink, progress=None) -> None:
        """Read the marker values in row blocks and hand them to `sink`.

        Peak memory is one block -- `block_rows x n_markers x 4 bytes` -- not
        one dataset, whatever the file's size. That is the whole reason this
        method is separate from `plan()`.
        """
        kind, layer = plan.feature_source
        if kind == 'obs':
            return  # already values on the plan; there is no matrix to read

        names = plan.feature_columns
        if not names:
            return

        with self._open_group() as group:
            node = self._matrix_node(group, layer)
            encoding = _encoding_of(node)

            if encoding == 'csc_matrix':
                # Row-block access to a column-major matrix touches every
                # column's indptr for every block, which is pathological. The
                # sink is per column anyway, so invert the loop: one full
                # source column at a time, take the rows we keep, discard.
                # Peak is one source column, still bounded by the row count
                # rather than the panel.
                self._stream_by_column(node, plan, names, sink, progress)
                return
            self._stream_by_block(node, plan, names, sink, progress, encoding)

    def _stream_by_block(self, node, plan, names, sink, progress, encoding):
        dataset = _sparse_dataset(node) if encoding == 'csr_matrix' else node
        itemsize = getattr(getattr(node, "dtype", None), "itemsize", 4) or 4
        block = _block_rows(len(names), itemsize)
        total = (plan.rows + block - 1) // block
        for step, start in enumerate(range(0, plan.rows, block)):
            stop = min(start + block, plan.rows)
            values = _dense_block(dataset, plan.row_indices, start, stop)
            values = _finish_features(values, self.apply_log_transform)
            for index, name in enumerate(names):
                sink(name, start, values[:, index])
            if progress is not None:
                progress(step + 1, total)

    def _stream_by_column(self, node, plan, names, sink, progress):
        dataset = _sparse_dataset(node)
        rows = plan.row_indices
        for index, name in enumerate(names):
            column = dataset[:, index]
            column = np.asarray(getattr(column, "toarray", lambda: column)()).reshape(-1)
            if rows is not None:
                column = column[rows]
            sink(name, 0, _finish_features(column, self.apply_log_transform))
            if progress is not None:
                progress(index + 1, len(names))

    # -- the eager frame, now assembled from the two halves above ------------

    def load_table(self, stage=None, report=None) -> NormalizedDatasource:
        """The whole table in memory, for callers that genuinely want one.

        Kept as a thin composition of `plan()` and `stream()` rather than as a
        second implementation, so there is exactly one description of what this
        format's table means. The store (server/models/feature_store.py) uses
        the same two halves and never builds this.
        """
        if stage is not None:
            stage("metadata")
        plan = self.plan()
        if stage is not None:
            stage("preparing")
        values = {name: np.empty(plan.rows, dtype=np.float32)
                  for name in plan.feature_columns
                  if name not in plan.columns}

        def sink(name, start, block):
            values[name][start:start + len(block)] = block

        self.stream(plan, sink, progress=report)

        columns = {}
        for name in ("id", "X", "Y", plan.obs_id_column):
            columns[name] = plan.columns[name]
        for name in plan.feature_columns:
            columns[name] = plan.columns.get(name)
            if columns[name] is None:
                columns[name] = values[name]
        if plan.celltype_column:
            columns[plan.celltype_column] = plan.columns[plan.celltype_column]

        return NormalizedDatasource(
            table=pl.DataFrame(columns),
            id_column=plan.id_column,
            x_column=plan.x_column,
            y_column=plan.y_column,
            feature_columns=list(plan.feature_columns),
            celltype_column=plan.celltype_column,
            obs_columns=list(plan.obs_columns),
            layers=list(plan.layers),
            obsm=list(plan.obsm),
        )

    def _resolve_coordinates(self, group, obs, row_indices):
        source = self.coordinates.get('source')
        obsm_keys = _child_keys(group, "obsm")
        if source is None:
            if 'spatial' not in obsm_keys:
                raise ValueError(
                    "No coordinate source specified and adata.obsm['spatial'] "
                    "is absent -- set dataSource.coordinates (coordinate_source"
                    "='obsm'/'obs' plus the relevant keys/columns) explicitly."
                )
            source, obsm_key = 'obsm', 'spatial'
        else:
            obsm_key = self.coordinates.get('obsm_key')

        if source == 'obsm':
            obsm_key = obsm_key or 'spatial'
            if obsm_key not in obsm_keys:
                raise ValueError(f"obsm key {obsm_key!r} not found in adata.obsm")
            node = _child(group, "obsm")[obsm_key]
            shape = getattr(node, "shape", None)
            if shape is None or len(shape) != 2 or shape[1] < 2:
                raise ValueError(f"adata.obsm[{obsm_key!r}] must be 2D with at least 2 columns")
            # Only the two columns that are coordinates, never the rest: this
            # array is routinely an embedding whose other 1534 dimensions
            # nobody asked for.
            xy = np.asarray(node[:, :2])
            if row_indices is not None:
                xy = xy[row_indices]
            return xy[:, 0].astype(np.float64), xy[:, 1].astype(np.float64)

        if source == 'obs':
            x_col = self.coordinates.get('x_column')
            y_col = self.coordinates.get('y_column')
            if not x_col or not y_col:
                raise ValueError(
                    "dataSource.coordinates.x_column/y_column are required "
                    "when coordinates.source='obs'"
                )
            if x_col not in obs.columns or y_col not in obs.columns:
                raise ValueError(f"Coordinate columns {x_col!r}/{y_col!r} not found in adata.obs")
            return (obs.take(x_col, row_indices).to_numpy(dtype=np.float64),
                    obs.take(y_col, row_indices).to_numpy(dtype=np.float64))

        raise ValueError(f"Unknown coordinates.source: {source!r}")
