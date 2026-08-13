from __future__ import annotations

import numpy as np

from .anndata_adapter import is_likely_image_identifier_name

# Read-only structural inspection of not-yet-registered source files -- used
# by the standalone import wizard (a future Stage 3 endpoint) to populate a
# progressive-disclosure config UI, and reusable directly from Jupyter/tests.
# Deliberately separate from data_model.py's module-global-based caching:
# these operate on files that aren't loaded (or registered) yet.

_MAX_CANDIDATE_VALUES = 500


def _is_subset_candidate(series) -> bool:
    """A column is worth offering as an image/sample subset choice if it's
    non-numeric (categorical/string-like) with more than one, but not an
    unreasonably large number of, distinct values."""
    try:
        if np.issubdtype(series.dtype, np.number):
            return False
    except TypeError:
        pass
    n_unique = series.nunique(dropna=True)
    return 1 < n_unique <= _MAX_CANDIDATE_VALUES


def inspect_anndata(path) -> dict:
    """Structural inspection of an .h5ad file: obsm keys, layers, obs
    columns (flagging which look like image/sample subset candidates and,
    for those, their available values), var names, and observation count.
    """
    import anndata as ad

    adata = ad.read_h5ad(path, backed='r')
    try:
        obs_columns = []
        for column in adata.obs.columns:
            series = adata.obs[column]
            is_candidate = _is_subset_candidate(series)
            # Same name heuristic the adapter's own ambiguity guard enforces
            # (adapters/anndata_adapter.py) -- lets the UI reserve its
            # "this file may span multiple images" warning for genuine
            # identifier-shaped columns instead of every categorical column
            # (cell_type/cluster/condition columns are common and would
            # otherwise trigger a misleading warning on ordinary single-image data).
            likely_identifier = (
                is_candidate
                and is_likely_image_identifier_name(column)
                and series.nunique(dropna=True) > 1
            )
            # Every column gets its actual values so the subset picker can
            # always offer a dropdown of real categories instead of asking
            # the user to type a value freehand -- is_subset_candidate above
            # only gates the ambiguity-warning heuristic now, not whether a
            # column's values are shown at all.
            values = series.dropna().unique().tolist()
            entry = {
                "name": column,
                "dtype": str(series.dtype),
                "is_subset_candidate": is_candidate,
                "likely_multi_image_identifier": likely_identifier,
                "values": sorted(str(v) for v in values),
            }
            obs_columns.append(entry)

        return {
            "data_type": "anndata",
            "obs_count": int(adata.n_obs),
            # anndata's Layers/AxisArrays mappings can report a spurious
            # `None` key (observed with anndata 0.13.2) even when no real
            # layer/obsm entry of that name exists -- filter it out.
            "obsm_keys": [k for k in adata.obsm.keys() if k],
            "layers": [k for k in adata.layers.keys() if k],
            "obs_columns": obs_columns,
            "var_names": [str(v) for v in adata.var_names],
            "n_var": int(adata.n_vars),
        }
    finally:
        if adata.isbacked:
            adata.file.close()
