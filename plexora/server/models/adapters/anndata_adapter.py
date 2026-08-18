from __future__ import annotations

import re

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

# Conventional obs-column names that plausibly identify which image/sample/
# region an observation belongs to. Used only to decide whether an AnnData
# object *might* span multiple images and therefore requires an explicit
# subset choice (requirements §5.4/§10) -- deliberately not "any categorical
# column", since ordinary annotations like cell_type/cluster/condition are
# common in a single-image AnnData and must not trigger a false ambiguity error.
# Matched against a separator-normalized column name (see _normalize_column_name)
# so "image_id", "imageid", "Image ID" etc. all match the same "imageid" entry --
# real exemplar data was found using "imageid" (no separator).
_LIKELY_IMAGE_IDENTIFIER_NAMES = {
    "imageid", "image", "sample", "sampleid", "region", "regionid",
    "roi", "fov", "well", "slide", "core",
}


def _normalize_column_name(name) -> str:
    return re.sub(r'[\s_-]+', '', str(name)).lower()


def is_likely_image_identifier_name(column_name) -> bool:
    """Shared with adapters/inspection.py so the standalone UI's "this file
    may span multiple images" hint uses the exact same name heuristic the
    adapter itself enforces, rather than a second, potentially-drifting copy.
    """
    return _normalize_column_name(column_name) in _LIKELY_IMAGE_IDENTIFIER_NAMES


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

    `feature_config` is the same shape data_model.py already passes to every
    adapter (config['featureData'][0]) -- for AnnData it additionally carries
    a 'dataSource' block (format/path/coordinates/features/obs_id_field/
    subset) recording how the resolved xCoordinate='X'/yCoordinate='Y'/
    idField values were derived. See adapters/base.py and datasource.py's
    register_anndata_datasource() for the config shape this expects.
    """

    def __init__(self, feature_config: dict):
        self.feature_config = feature_config
        data_source = feature_config.get('dataSource') or {}
        self.path = data_source.get('path') or feature_config.get('src')
        self.coordinates = data_source.get('coordinates') or {}
        self.features = data_source.get('features') or {'source': 'X'}
        self.obs_id_field = data_source.get('obs_id_field')
        self.subset = data_source.get('subset') or {}
        self.celltype_column = feature_config.get('celltype')
        # Explicit opt-in only -- no heuristic guessing at whether the
        # chosen feature source "looks" already transformed. Matches
        # data_model.logTransform()'s CSV behavior (pl.col(c).log1p()).
        self.apply_log_transform = bool(data_source.get('apply_log_transform', False))

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
            id_values = adata.obs[self.obs_id_field].astype(str).to_numpy()
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
