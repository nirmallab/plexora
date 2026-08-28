from __future__ import annotations

import numpy as np

from . import classify
from .anndata_adapter import describe_obsm, is_likely_image_identifier_name

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


def _inspect_adata(adata) -> dict:
    """The format-independent half of inspection: everything the config UI
    needs about an already-opened AnnData. Shared by inspect_anndata() and
    inspect_spatialdata_table(), since a SpatialData table *is* an AnnData
    and its config page offers exactly the same choices."""
    obs_columns = []
    for column in adata.obs.columns:
        series = adata.obs[column]
        is_candidate = _is_subset_candidate(series)
        n_unique = series.nunique(dropna=True)
        # Same name heuristic the adapter's own ambiguity guard enforces
        # (adapters/anndata_adapter.py) -- lets the UI reserve its
        # "this file may span multiple images" warning for genuine
        # identifier-shaped columns instead of every categorical column
        # (cell_type/cluster/condition columns are common and would
        # otherwise trigger a misleading warning on ordinary single-image data).
        likely_identifier = is_candidate and is_likely_image_identifier_name(column) and n_unique > 1
        # Every column gets its actual values so the subset picker can
        # always offer a dropdown of real categories instead of asking the
        # user to type a value freehand -- is_subset_candidate above only
        # gates the ambiguity-warning heuristic, not whether a column's
        # values are shown at all. But a column with more distinct values
        # than _MAX_CANDIDATE_VALUES (e.g. a per-cell numeric ID or
        # centroid coordinate -- effectively unique per row) is never a
        # usable subset choice, and for a real hundreds-of-thousands-of-rows
        # table, listing it anyway inflates this inspection payload to tens
        # of MB. A config page embeds that payload as a literal
        # JavaScript object ({{data|tojson}}, not JSON.parse'd), so a
        # bloated payload freezes the importing browser tab outright --
        # capping here is what keeps that page loadable for large
        # single-cell tables (confirmed against a real 686k-cell exemplar,
        # whose id/centroid columns alone produced a 30MB payload).
        values = (
            sorted(str(v) for v in series.dropna().unique().tolist())
            if n_unique <= _MAX_CANDIDATE_VALUES
            else []
        )
        entry = {
            "name": column,
            "dtype": str(series.dtype),
            "is_subset_candidate": is_candidate,
            "likely_multi_image_identifier": likely_identifier,
            "values": values,
        }
        obs_columns.append(entry)

    return {
        "obs_count": int(adata.n_obs),
        # anndata's Layers/AxisArrays mappings can report a spurious
        # `None` key (observed with anndata 0.13.2) even when no real
        # layer/obsm entry of that name exists -- filter it out.
        "obsm_keys": [k for k in adata.obsm.keys() if k],
        # The same arrays with their shapes, which is what a user picking
        # between them actually needs: `spatial` and `X_umap` are routinely both
        # (n, 2) float32, so the name is the only thing separating a cell's
        # position from its embedding -- and the name is exactly what cannot be
        # trusted. Kept beside `obsm_keys` rather than replacing it so existing
        # callers of that key are untouched.
        "obsm": describe_obsm(adata),
        "layers": [k for k in adata.layers.keys() if k],
        "obs_columns": obs_columns,
        "var_names": [str(v) for v in adata.var_names],
        "n_var": int(adata.n_vars),
    }


def inspect_csv(path) -> dict:
    """Structural inspection of a flat CSV: its columns and their dtypes.

    Reads a small number of rows rather than the file: a real quantification
    table runs to millions of rows, and nothing here needs values -- only
    names, and enough of each column to tell a number from a label. Polars
    infers from the rows it is given, so a column that is empty at the top of
    the file can be misread; that is acceptable because the user confirms the
    split on the classification screen, which is the whole reason it exists.
    """
    import polars as pl

    frame = pl.read_csv(path, n_rows=_DTYPE_SAMPLE_ROWS)
    columns = [{"name": name, "dtype": str(dtype)} for name, dtype in frame.schema.items()]
    return {
        "data_type": "csv",
        "columns": columns,
        **classify.classify_columns(columns),
    }


#: Rows read to infer CSV dtypes. Enough to get past a sparse first row,
#: cheap enough to run on every keystroke-triggered inspection.
_DTYPE_SAMPLE_ROWS = 200

#: obsm keys that conventionally hold cell centroids, in preference order.
_SPATIAL_OBSM_KEYS = ("spatial", "X_spatial", "spatial_centroid")


def propose_read_spec(inspection: dict) -> dict:
    """How to read this file, worked out from its own structure.

    The import page asks for a path and nothing else, so everything the old
    two-step config form made the user choose has to be answered from the file
    itself. Almost all of it can be: `obsm['spatial']` is near-universal for
    centroids, `X` is the feature matrix by definition, and a positional cell
    id is the right default regardless (see the uint32-packing note in
    datasource.py).

    Returns the read spec plus `ambiguous`: the columns that make the file span
    several images, which is the one thing that genuinely cannot be guessed --
    picking an image for the user would silently load the wrong cells. The
    caller turns that into a subset choice on the form.

    An unresolved coordinate source is NOT an error here. It leaves the x/y
    roles unset, and whatever first needs them asks for them.
    """
    obsm_keys = inspection.get("obsm_keys") or []
    obs_columns = inspection.get("obs_columns") or []
    names = [c.get("name") for c in obs_columns if c.get("name")]

    coordinates = {}
    obsm_key = next((k for k in _SPATIAL_OBSM_KEYS if k in obsm_keys), None)
    if obsm_key:
        coordinates = {"source": "obsm", "obsm_key": obsm_key}
    else:
        guessed = classify.guess_roles(names)
        if guessed.get("x") and guessed.get("y"):
            coordinates = {"source": "obs", "x_column": guessed["x"], "y_column": guessed["y"]}

    return {
        "coordinates": coordinates,
        "features": {"source": "X"},
        "ambiguous": [
            c["name"] for c in obs_columns if c.get("likely_multi_image_identifier")
        ],
        **classify.classify_from_inspection(inspection),
    }


def spec_from_inspection(document: dict) -> dict:
    """A DataSpec-ready dict for a table that can only be inspected remotely.

    A node's /inspect document (which embeds a `propose_read_spec` result as
    `proposed`) is turned into the same read spec `register_anndata_datasource`
    or the CSV import would have built from a local copy of the file. The
    parity that matters: a table attached from a data node must come out shaped
    exactly as the same file would have, imported locally -- the synthesized
    X/Y roles included, because the adapter emits columns under those literal
    names whatever machine it runs on.
    """
    data_type = document.get("data_type")
    proposal = document.get("proposed") or propose_read_spec(document)
    if data_type == "csv":
        # The predictor's guesses, exactly as _register_csv forwards them; the
        # classification screen remains the place they get confirmed.
        roles = {k: v for k, v in (document.get("roles") or {}).items() if v}
        return {"type": "csv", "roles": roles}
    fields = {
        "type": data_type or "anndata",
        "coordinates": dict(proposal.get("coordinates") or {}),
        "features": dict(proposal.get("features") or {"source": "X"}),
        # The adapter synthesizes X/Y columns and a positional id under these
        # literal names -- the same roles register_anndata_datasource writes.
        "roles": {"x": "X", "y": "Y", "cell_id": "id"},
    }
    if document.get("table"):
        fields["table"] = document["table"]
    return fields


def inspect_anndata(path) -> dict:
    """Structural inspection of an .h5ad file: obsm keys, layers, obs
    columns (flagging which look like image/sample subset candidates and,
    for those, their available values), var names, and observation count.
    """
    import anndata as ad

    adata = ad.read_h5ad(path, backed='r')
    try:
        return {"data_type": "anndata", **_inspect_adata(adata)}
    finally:
        if adata.isbacked:
            adata.file.close()


def source_layers(spec) -> list[str]:
    """The extra expression matrices a project's source file carries.

    Recorded at import (DataSpec.layers), so this is only reached for a project
    imported before that was recorded -- but it has to be reached, because the
    one control that can fix a project reading the wrong matrix would otherwise
    be the one control it never offers.

    Cheaper than a full inspection on purpose: a zarr group listing, or a backed
    h5ad open, so nothing materializes a second matrix just to learn its name.
    Best-effort -- an unreadable file means no picker, which is what every
    surface here did before there was one.
    """
    if spec is None or spec.type == "csv":
        return []
    if spec.layers:
        return list(spec.layers)
    try:
        if spec.type == "spatialdata":
            from .spatialdata_adapter import list_table_layers

            return list_table_layers(spec.src, spec.table)
        import anndata as ad

        adata = ad.read_h5ad(spec.src, backed="r")
        try:
            return [str(name) for name in adata.layers.keys() if name]
        finally:
            if adata.isbacked:
                adata.file.close()
    except Exception:
        return []


def source_obsm(spec) -> list[dict]:
    """The obsm arrays a project's source file carries, with their shapes.

    The coordinate question's counterpart to `source_layers`, and reached for
    the same reason: a project imported before obsm was recorded would
    otherwise be offered no candidates at all, and that is precisely the
    project whose coordinate source was picked by a name heuristic nobody was
    shown.

    Best-effort in the same way -- an unreadable file means no obsm choices,
    which is what every surface offered before this existed.
    """
    if spec is None or spec.type == "csv":
        return []
    if spec.obsm:
        return [dict(entry) for entry in spec.obsm]
    try:
        if spec.type == "spatialdata":
            from .spatialdata_adapter import read_spatialdata_table

            return describe_obsm(read_spatialdata_table(spec.src, spec.table))
        import anndata as ad

        adata = ad.read_h5ad(spec.src, backed="r")
        try:
            return describe_obsm(adata)
        finally:
            if adata.isbacked:
                adata.file.close()
    except Exception:
        return []


def inspect_spatialdata_table(store, table) -> dict:
    """Same inspection, for one table inside a SpatialData (.zarr) store.

    Reads only the named table (see spatialdata_adapter.read_spatialdata_table
    for why the whole store is deliberately not opened). There's no backed
    mode for a zarr-backed AnnData, so unlike inspect_anndata this
    materializes the table -- acceptable because the import form has the
    user pick a single table first, and its shape is shown there.
    """
    from .spatialdata_adapter import read_spatialdata_table

    adata = read_spatialdata_table(store, table)
    return {
        "data_type": "spatialdata",
        "store": str(store),
        "table": str(table),
        **_inspect_adata(adata),
    }
