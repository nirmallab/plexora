from __future__ import annotations

import numpy as np

from . import classify
from .anndata_adapter import (
    _LazyObs,
    _child,
    _child_keys,
    _describe_obsm_mapping,
    _matrix_shape,
    _read_elem,
    describe_obsm,
    is_likely_image_identifier_name,
)

# Read-only structural inspection of not-yet-registered source files -- used
# by the standalone import wizard (a future Stage 3 endpoint) to populate a
# progressive-disclosure config UI, and reusable directly from Jupyter/tests.
# Deliberately separate from data_model.py's module-global-based caching:
# these operate on files that aren't loaded (or registered) yet.

#: Past this many distinct values a column is never a usable subset choice --
#: a per-cell numeric id or a centroid coordinate is effectively unique per row
#: -- and listing its values anyway inflates the inspection payload to tens of
#: MB. A config page embeds that payload as a literal JavaScript object
#: ({{data|tojson}}, not JSON.parse'd), so a bloated one freezes the importing
#: browser tab outright. Confirmed against a real 686k-cell exemplar, whose
#: id/centroid columns alone produced a 30MB payload.
_MAX_CANDIDATE_VALUES = 500


def _is_numeric(series) -> bool:
    try:
        return bool(np.issubdtype(series.dtype, np.number))
    except TypeError:
        return False


def _distinct_values(series):
    """(count, sorted values) for one obs column, in a single pass.

    This used to be three passes over every column -- `nunique` inside the
    candidate test, `nunique` again for the count, then `dropna().unique()` for
    the values -- and on a table spanning sixty images those three passes are
    most of what the import form waits on. The field renders nothing while it
    waits, so the only way a user discovers the inspection is running is to
    press Save and be told to try again in a moment. `value_counts` answers
    both questions at once.

    The values are only materialized when there are few enough to be a usable
    dropdown; past that they are dropped rather than counted differently, which
    is what keeps the count honest for the ambiguity warning.
    """
    counts = series.value_counts(dropna=True)
    n_unique = int(len(counts))
    if n_unique > _MAX_CANDIDATE_VALUES:
        return n_unique, []
    return n_unique, sorted(str(value) for value in counts.index)


def _inspect_group(group) -> dict:
    """Everything the config UI needs about an AnnData, read on disk.

    Shared by `inspect_anndata` and `inspect_spatialdata_table`, because a
    SpatialData table IS an AnnData and its config page offers exactly the same
    choices. Neither loads the table: `read_h5ad(path, backed='r')` -- what this
    replaced -- keeps X on disk but still materializes obs -- including the observation index, which is
    1.2M strings and ~253 MB on a real multi-image table, and which nothing in
    this document needs. Reading the group directly means the only columns that
    cost anything are the ones being described, and the index costs nothing.

    Works for both formats: an h5py File and a zarr group answer `keys()`,
    `attrs` and slicing identically, which is the same property that lets
    SpatialDataAdapter override only `_open_group`.
    """
    obs = _LazyObs(group)
    obs_columns = []
    for column in obs.columns:
        series = obs[column]
        n_unique, values = _distinct_values(series)
        is_candidate = (not _is_numeric(series)) and 1 < n_unique <= _MAX_CANDIDATE_VALUES
        obs_columns.append({
            "name": column,
            "dtype": str(series.dtype),
            "is_subset_candidate": is_candidate,
            # Same name heuristic the adapter's own ambiguity guard enforces,
            # so the UI reserves its "this file may span multiple images"
            # warning for genuinely identifier-shaped columns rather than every
            # categorical one.
            "likely_multi_image_identifier": (
                is_candidate and is_likely_image_identifier_name(column) and n_unique > 1),
            "values": values,
        })

    var_names = []
    var_node = _child(group, "var")
    if var_node is not None:
        var_names = [str(name) for name in _read_elem(var_node).index]
    n_var = len(var_names)
    if not n_var:
        matrix = _child(group, "X")
        n_var = (_matrix_shape(matrix)[1] or 0) if matrix is not None else 0

    obsm = _describe_obsm_mapping(_child(group, "obsm"))
    return {
        "obs_count": int(obs.n_rows),
        # anndata's Layers/AxisArrays mappings can report a spurious `None` key
        # (observed with anndata 0.13.2) even when no real entry of that name
        # exists -- filter it out.
        "obsm_keys": [entry["name"] for entry in obsm],
        "obsm": obsm,
        "layers": [name for name in _child_keys(group, "layers") if name],
        "obs_columns": obs_columns,
        "var_names": var_names,
        "n_var": int(n_var),
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
    import h5py

    with h5py.File(path, "r") as handle:
        return {"data_type": "anndata", **_inspect_group(handle)}


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
    for why the whole store is deliberately not opened) -- and now only the
    parts of it this document describes. It used to materialize the table,
    excused by the user having picked a single one; that excuse does not
    survive a store whose chosen table is a 1536-dimensional embedding.
    """
    import zarr

    from .spatialdata_adapter import table_path

    # Path, not str: zarr v3 parses a string store as a URL, so a table name
    # containing '#' would be truncated (same reason as list_spatialdata_tables).
    group = zarr.open_group(table_path(store, table), mode="r")
    return {
        "data_type": "spatialdata",
        "store": str(store),
        "table": str(table),
        **_inspect_group(group),
    }
