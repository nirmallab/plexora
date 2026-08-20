from __future__ import annotations

import numpy as np
import polars as pl

from .base import NormalizedDatasource

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
    entries = []
    for name in adata.obsm.keys():
        if not name:
            continue
        entry = {"name": str(name)}
        shape = getattr(adata.obsm[name], "shape", None)
        if shape is not None:
            entry["shape"] = [int(dim) for dim in shape]
        entries.append(entry)
    return entries


def _likely_image_identifier_columns(adata) -> list[str]:
    candidates = []
    for column in adata.obs.columns:
        if not is_likely_image_identifier_name(column):
            continue
        if adata.obs[column].nunique(dropna=True) > 1:
            candidates.append(column)
    return candidates


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
        # chosen feature source "looks" already transformed. Matches
        # data_model.logTransform()'s CSV behavior (pl.col(c).log1p()).
        self.apply_log_transform = bool(spec.is_transformed)

    def _read_adata(self):
        """The one format-specific step in load_table() -- everything after
        this point is plain AnnData manipulation and is shared verbatim by
        SpatialDataAdapter, which overrides only this method (see
        adapters/spatialdata_adapter.py)."""
        import anndata as ad

        return ad.read_h5ad(self.path)

    def load_table(self) -> NormalizedDatasource:
        adata = self._read_adata()

        subset_column = self.subset.get('column')
        if subset_column:
            if subset_column not in adata.obs.columns:
                raise ValueError(f"Subset column {subset_column!r} not found in adata.obs")
            subset_value = self.subset.get('value')
            mask = adata.obs[subset_column].astype(str).to_numpy() == str(subset_value)
            if not mask.any():
                raise ValueError(
                    f"No observations match {subset_column}={subset_value!r}"
                )
            adata = adata[mask].copy()
        else:
            named = self.image_id_column
            if named and named in adata.obs.columns:
                # The user told us which column identifies the image, so ask
                # that column rather than guessing which one to ask. This is
                # the check the name heuristic below cannot make: it only fires
                # for conventionally-named columns, so a table keyed on "roi"
                # or "core" loaded whole and drew several images' cells over
                # one image, with nothing said.
                images = adata.obs[named].astype(str).nunique(dropna=True)
                if images > 1:
                    raise ValueError(
                        f"Column {named!r} covers {images} images, but no "
                        "subset was specified -- loading them all would draw "
                        "several images' cells over one image. Choose which "
                        "image to load."
                    )
            else:
                ambiguous = _likely_image_identifier_columns(adata)
                if ambiguous:
                    raise ValueError(
                        "AnnData object has candidate image/sample identifier "
                        f"column(s) {ambiguous} with more than one distinct "
                        "value, but no subset was specified -- refusing to "
                        "silently load all observations. Set dataSource.subset "
                        "(or subset_by/subset_value) to pick one image/sample."
                    )

        n_obs = adata.n_obs
        if n_obs == 0:
            raise ValueError("Resolved AnnData subset has zero observations")

        x_values, y_values = self._resolve_coordinates(adata)
        if not np.isfinite(x_values).all() or not np.isfinite(y_values).all():
            raise ValueError("Resolved X/Y coordinates contain non-finite values")

        feature_matrix, feature_names = self._resolve_features(adata)

        if self.obs_id_field:
            if self.obs_id_field not in adata.obs.columns:
                raise ValueError(f"obs_id_field {self.obs_id_field!r} not found in adata.obs")
            if self.obs_id_field in _RESERVED_COLUMN_NAMES:
                raise ValueError(
                    f"obs_id_field {self.obs_id_field!r} collides with a reserved "
                    f"column name ({sorted(_RESERVED_COLUMN_NAMES)}) -- choose a "
                    "different observation ID column, or leave it unset to use "
                    "adata.obs_names."
                )
            column = adata.obs[self.obs_id_field]
            # A numeric obs column stays numeric. The usual reason to name one
            # here is that it holds the segmentation mask's own label values,
            # and the centroid cache packs the cell id into a uint32 -- a
            # stringified integer only survives that round trip by being parsed
            # back out again. adata.obs_names below has no such expectation and
            # is genuinely text, so it keeps the str cast.
            id_values = (column.to_numpy() if is_numeric_dtype(column.dtype)
                         else column.astype(str).to_numpy())
            id_field_name = self.obs_id_field
        else:
            id_values = np.asarray([str(v) for v in adata.obs_names])
            id_field_name = DEFAULT_ID_COLUMN

        feature_names = _deduplicate_names(feature_names)
        reserved = _RESERVED_COLUMN_NAMES | {id_field_name}
        collisions = [name for name in feature_names if name in reserved]
        if collisions:
            raise ValueError(f"Feature name(s) collide with reserved columns: {collisions}")

        columns = {
            "id": np.arange(n_obs, dtype=np.int64),
            "X": x_values,
            "Y": y_values,
            id_field_name: id_values,
        }
        for i, feature_name in enumerate(feature_names):
            values = feature_matrix[:, i].astype(np.float64)
            values = np.where(np.isneginf(values), 0.0, values)
            if self.apply_log_transform:
                values = np.log1p(values)
            columns[feature_name] = values

        celltype_column = None
        if self.celltype_column:
            if self.celltype_column not in adata.obs.columns:
                raise ValueError(f"celltype column {self.celltype_column!r} not found in adata.obs")
            if self.celltype_column in reserved:
                raise ValueError(
                    f"celltype column {self.celltype_column!r} collides with a "
                    f"reserved/ID column name -- choose a different column."
                )
            columns[self.celltype_column] = adata.obs[self.celltype_column].astype(str).to_numpy()
            celltype_column = self.celltype_column

        table = pl.DataFrame(columns)
        source_obs_ids = [str(v) for v in adata.obs_names]

        return NormalizedDatasource(
            table=table,
            id_column="id",
            source_obs_ids=source_obs_ids,
            x_column="X",
            y_column="Y",
            feature_columns=list(feature_names),
            celltype_column=celltype_column,
            obs_columns=[str(c) for c in adata.obs.columns],
            # anndata's Layers mapping can report a spurious `None` key
            # (observed with anndata 0.13.2) even when no such layer exists --
            # filtered out here the same way adapters/inspection.py does.
            layers=[str(k) for k in adata.layers.keys() if k],
            # Recorded so the coordinate question has candidates to offer
            # without reopening the file. Same codec as inspection's, so the
            # import path and the edit page describe an array identically.
            obsm=describe_obsm(adata),
        )

    def _resolve_coordinates(self, adata):
        source = self.coordinates.get('source')
        if source is None:
            if 'spatial' not in adata.obsm:
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
            if obsm_key not in adata.obsm:
                raise ValueError(f"obsm key {obsm_key!r} not found in adata.obsm")
            xy = np.asarray(adata.obsm[obsm_key])
            if xy.ndim != 2 or xy.shape[1] < 2:
                raise ValueError(f"adata.obsm[{obsm_key!r}] must be 2D with at least 2 columns")
            return xy[:, 0].astype(np.float64), xy[:, 1].astype(np.float64)

        if source == 'obs':
            x_col = self.coordinates.get('x_column')
            y_col = self.coordinates.get('y_column')
            if not x_col or not y_col:
                raise ValueError(
                    "dataSource.coordinates.x_column/y_column are required "
                    "when coordinates.source='obs'"
                )
            if x_col not in adata.obs.columns or y_col not in adata.obs.columns:
                raise ValueError(f"Coordinate columns {x_col!r}/{y_col!r} not found in adata.obs")
            x_values = adata.obs[x_col].to_numpy(dtype=np.float64)
            y_values = adata.obs[y_col].to_numpy(dtype=np.float64)
            return x_values, y_values

        raise ValueError(f"Unknown coordinates.source: {source!r}")

    def _resolve_features(self, adata):
        source = self.features.get('source', 'X')
        if source == 'X':
            matrix = adata.X
            names = [str(v) for v in adata.var_names]
        elif source == 'layer':
            layer = self.features.get('layer')
            if not layer:
                raise ValueError("dataSource.features.layer is required when features.source='layer'")
            if layer not in adata.layers:
                raise ValueError(f"Layer {layer!r} not found in adata.layers")
            matrix = adata.layers[layer]
            names = [str(v) for v in adata.var_names]
        elif source == 'obs':
            obs_columns = self.features.get('obs_columns') or []
            if not obs_columns:
                raise ValueError("dataSource.features.obs_columns is required when features.source='obs'")
            missing = [c for c in obs_columns if c not in adata.obs.columns]
            if missing:
                raise ValueError(f"obs feature columns not found in adata.obs: {missing}")
            matrix = adata.obs[obs_columns].to_numpy(dtype=np.float64)
            names = list(obs_columns)
        else:
            raise ValueError(f"Unknown features.source: {source!r}")

        if hasattr(matrix, 'toarray'):
            matrix = matrix.toarray()
        matrix = np.asarray(matrix, dtype=np.float64)
        return matrix, names
