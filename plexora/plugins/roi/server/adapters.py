"""Writing annotations into the file the project came from.

Everything here is explicit -- a button the user presses, never a save -- and
non-destructive. That is not caution for its own sake: the target is the user's
own measurements, often the only copy, frequently on a share, and an annotation
export that rewrites an .h5ad's X or drops a SpatialData table is a data-loss
bug wearing a feature's clothes.

Three rules the implementations below follow:

**Write the subtree, never the file.** An .h5ad is opened in-place with h5py and
only `uns/plexora` is rewritten. A read-and-write-back round trip would rebuild
X, obs and var from whatever anndata's current version thinks they should look
like, changing chunking, compression and dtypes of data this plugin has no
business touching.

**Never overwrite something without being told to.** `sdata.shapes["roi"]` may be
somebody's segmentation boundaries; `uns["plexora"]["rois"]` may be a colleague's
annotation pass. Both destinations are NAMED by the user, and a name that is
already taken is refused rather than replaced -- the caller has to say
`replace`, and for SpatialData it is not offered at all (see
`save_to_spatialdata`). Refusal happens before anything is unlinked.

**Never re-consolidate a store root.** See `_open_group`: a SpatialData root is
zarr v3 and real stores mix v2 tables into it, so rebuilding the root index
silently drops them. The refresh here is confined to the group actually written.

The live annotation state stays in the plugin store either way. These are
destinations, not the working copy -- which is what lets the drawing tools
behave identically whether the project came from a CSV, an .h5ad, or nothing but
an image.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from plexora.plugins.roi.server import schema

#: Where annotations land inside an AnnData. Under a `plexora` group rather than
#: at the top of uns so Plexora owns one key in a namespace shared with whatever
#: else the user's pipeline put there.
UNS_GROUP = "plexora"

#: What the entry under that group is called unless the user names it something
#: else. One project can hold several: a second pass, a second annotator, a
#: version kept because the first one was worth keeping.
DEFAULT_UNS_KEY = "rois"

#: Default name for the shapes element. Prefixed, because `shapes["roi"]` is a
#: name a user's own pipeline could plausibly have used for something else.
DEFAULT_ELEMENT = "plexora_rois"

_GROUP_MARKERS = ("zarr.json", ".zgroup")


class DestinationExists(Exception):
    """The name the user gave is already in use in their file.

    Carries what is already there and a free name, so the caller can say which
    names are taken rather than only that this one is.
    """

    def __init__(self, message, existing, suggestion):
        super().__init__(message)
        self.existing = existing
        self.suggestion = suggestion


class ElementExists(DestinationExists):
    """The requested shapes element is already in the store."""

    def __init__(self, existing, suggestion):
        super().__init__(f"element already exists; try {suggestion!r}", existing, suggestion)


class KeyExists(DestinationExists):
    """The requested uns/plexora key is already in the file."""

    def __init__(self, existing, suggestion):
        super().__init__(f"key already exists; try {suggestion!r}", existing, suggestion)


class ColumnExists(DestinationExists):
    """One of the two ROI columns is already a column of the cell table.

    `existing` is every column name rather than just the colliding one: the two
    names are derived from a single prefix rather than chosen separately, so a
    user picking a new prefix is choosing against the whole table.
    """

    def __init__(self, existing, suggestion):
        super().__init__(
            f"a column of that name already exists; try {suggestion!r}",
            existing, suggestion)


# -- AnnData ------------------------------------------------------------


def save_to_anndata(dataset, state, plugin_version, key=None, replace=False):
    """Write `state` into the source file at uns/plexora/<key>.

    Stored as a JSON string rather than as a nested structure. AnnData's element
    codec writes mappings recursively, and an ROI document is heterogeneous all
    the way down -- ragged coordinate arrays, nullable strings, per-feature
    dicts with different keys -- which is exactly the shape that produces
    unreadable object arrays or an outright encoder error. One string always
    round-trips, in every language that can open the file, and
    `json.loads(adata.uns["plexora"]["rois"])` is a one-liner on the way out.

    `key` names the entry, so one file can hold several passes side by side
    without them being each other's history. Writing over one that is already
    there needs `replace`, and the refusal happens before `del uns[...]` -- an
    export that unlinks the group and then declines to write is the one failure
    mode here that destroys something.
    """
    if dataset.source_kind != "anndata":
        raise ValueError("this project's data did not come from an AnnData file")

    source = dataset.table.source
    if source is None or not source.path:
        raise ValueError("no AnnData file is recorded for this project")

    key = schema.element_name(key, DEFAULT_UNS_KEY)
    document = _snapshot(state, dataset.name, plugin_version)
    payload = json.dumps(document, separators=(",", ":"))

    try:
        from anndata.io import read_elem, write_elem  # anndata >= 0.10, public API
    except ImportError:  # pragma: no cover - older anndata fallback
        from anndata._io.specs import read_elem, write_elem

    with _open_group(source.path, writable=True) as handle:
        uns = handle.require_group("uns")

        existing = {}
        if UNS_GROUP in uns:
            existing = read_elem(uns[UNS_GROUP])
            if not isinstance(existing, dict):
                # Refused rather than replaced, and refused before anything is
                # written: whatever is under that key belongs to somebody, and
                # this plugin cannot tell what would be lost.
                raise ValueError(
                    f"adata.uns[{UNS_GROUP!r}] already exists and is not a mapping, "
                    "so these annotations have nowhere to go without overwriting it"
                )
            if key in existing and not replace:
                raise KeyExists(sorted(existing), _suggest(key, existing))
            del uns[UNS_GROUP]

        existing[key] = payload
        write_elem(uns, UNS_GROUP, existing)

    return {
        "path": source.path,
        "name": key,
        "key": f"uns/{UNS_GROUP}/{key}",
        "n_rois": len(document["images"].get(schema.DEFAULT_IMAGE, {}).get("features", [])),
        "n_categories": len(document["categories"]),
    }


def existing_anndata_keys(dataset):
    """Names already used under `uns/plexora` in this project's file.

    Asked on every panel open so a colliding name is visible before it is
    typed. Read-only and forgiving: a file another process is holding open, or
    one whose `uns/plexora` is not a mapping, produces an empty list rather
    than a panel that will not load. The write path checks again for real, and
    refuses there.
    """
    if dataset.source_kind != "anndata":
        return []
    source = dataset.table.source
    if source is None or not source.path:
        return []

    try:
        from anndata.io import read_elem
    except ImportError:  # pragma: no cover - older anndata fallback
        from anndata._io.specs import read_elem

    try:
        with _open_group(source.path) as handle:
            uns = handle["uns"] if "uns" in handle else None
            if uns is None or UNS_GROUP not in uns:
                return []
            stored = read_elem(uns[UNS_GROUP])
    except Exception:  # pragma: no cover - unreadable file; the panel still works
        return []
    return sorted(stored) if isinstance(stored, dict) else []


def _snapshot(state, datasource, plugin_version):
    """What gets written to a file: the annotations, without the revision.

    The revision is a fact about this project's store -- who last wrote and
    whether a client is stale -- and means nothing in a file somebody else
    opens. Carrying it would invite a reader to treat the file as authoritative
    about the live state, which it never is.
    """
    return {
        "schema_version": state["schema_version"],
        "plugin_version": plugin_version,
        "datasource": datasource,
        "categories": state["categories"],
        "images": state["images"],
    }


# -- SpatialData --------------------------------------------------------


def existing_shapes(dataset):
    """Names already used by shapes elements in this project's store."""
    store = _spatialdata_store(dataset)
    shapes_dir = Path(store) / "shapes"
    if not shapes_dir.is_dir():
        return []
    return sorted(entry.name for entry in shapes_dir.iterdir() if _is_group(entry))


def save_to_spatialdata(dataset, state, element_name=DEFAULT_ELEMENT,
                        image_key=schema.DEFAULT_IMAGE):
    """Write the annotations into the store as a shapes element.

    Coordinates go in untransformed, under an identity transformation into the
    store's own coordinate system. That is correct here for the same reason the
    importer reads tables as-is: Plexora's viewer lays the image out in its
    pixel grid, and this project's shapes were drawn on that grid, so pixel
    coordinates ARE the image element's coordinates. Attaching a scale nobody
    measured would be the guess.
    """
    store = _spatialdata_store(dataset)
    element_name = schema.element_name(element_name, DEFAULT_ELEMENT)
    taken = existing_shapes(dataset)
    if element_name in taken:
        # Overwriting an element in place is not offered. spatialdata's own
        # element writer refuses it, and the workaround -- delete then rewrite
        # -- has a window in which the user's data is gone and the replacement
        # is not yet there. A second name costs nothing and cannot lose
        # anything.
        raise ElementExists(taken, _suggest(element_name, taken))

    try:
        import geopandas as gpd
        import shapely.geometry as sgeom
        import spatialdata
        from spatialdata.models import ShapesModel
        from spatialdata.transformations import Identity
    except ImportError as exc:  # pragma: no cover - optional at runtime
        raise ValueError(f"SpatialData export needs {exc.name!r} to be installed") from exc

    entry = state["images"].get(image_key) or schema.empty_image()
    categories = {c["id"]: c for c in state["categories"]}
    if not entry["features"]:
        raise ValueError("there are no ROIs to export")

    rows, geometries = [], []
    for feature in entry["features"]:
        category = categories.get(feature["category_id"]) or schema.placeholder_category()
        geometries.append(_shapely(feature["geometry"], sgeom))
        rows.append({
            "roi_id": feature["id"],
            "name": feature.get("name") or "",
            "category_id": feature["category_id"],
            "category": category["label"],
            "category_color": category["color"],
            "locked": bool(feature.get("locked", False)),
        })

    frame = gpd.GeoDataFrame(rows, geometry=geometries)
    element = ShapesModel.parse(frame, transformations={"global": Identity()})

    # Only the shapes group: reading the tables would materialize every one of
    # them, which on a real store is hundreds of megabytes of embeddings nobody
    # asked for (the same trap the importer documents).
    sdata = spatialdata.read_zarr(store, selection=("shapes",))
    sdata.shapes[element_name] = element
    sdata.write_element(element_name)

    return {
        "path": str(store),
        "name": element_name,
        "element": f"shapes/{element_name}",
        "n_rois": len(rows),
        "coordinate_system": "global",
    }


# -- ROI columns on the cells -------------------------------------------


def cell_column_names(prefix):
    """The two columns "Map to cells" writes, for one destination name.

    Derived from the name the user already types when saving, so several
    annotation passes can sit side by side in one table the same way several
    `uns/plexora/<key>` entries can. `rois` -- not DEFAULT_ELEMENT -- is the
    fallback for both formats: a SpatialData project would otherwise get
    `plexora_rois_category`, which is a column name nobody wants to type.
    """
    prefix = schema.element_name(prefix, DEFAULT_UNS_KEY)
    return f"{prefix}_category", f"{prefix}_name"


def write_cell_columns(dataset, labels, names, prefix=None, replace=False):
    """Write two ROI annotation columns onto this project's cells.

    `labels` and `names` are per-row values for the LOADED table, in its order
    -- what `mapping.assign` returns. Getting them onto the right rows of the
    file underneath is this function's whole job, and it is not a copy: the
    loaded table is frequently a subset of the file's cells, and the file may
    hold several images' worth of them.

    Two guarantees, in the module's existing spirit:

    Rows this project cannot see are never touched. A cell belonging to another
    image in the same shared .h5ad keeps whatever it had, or gets a null if the
    column is new -- so the same file can be annotated once per image without
    each pass erasing the last.

    That is also why blank and null mean different things here, and the
    difference is worth keeping. A cell of THIS image that fell in no region
    gets an empty string: it was tested, and the answer is "none". A cell of
    another image gets a null: it was never tested. Collapsing the two would
    make a half-annotated file indistinguishable from a fully annotated one in
    which nothing overlapped.

    An existing column is refused, not overwritten, and refused before anything
    is written. `replace` is the user's answer to being asked.
    """
    category_column, name_column = cell_column_names(prefix)
    kind = dataset.source_kind
    if kind == "csv":
        return _write_csv_columns(dataset, labels, names, category_column,
                                  name_column, replace)
    if kind in ("anndata", "spatialdata"):
        return _write_obs_columns(dataset, labels, names, category_column,
                                  name_column, replace)
    raise ValueError("this project has no cell-level data to annotate")


def _write_obs_columns(dataset, labels, names, category_column, name_column, replace):
    """The AnnData/SpatialData path: rewrite `obs`, and nothing else.

    `obs` is read through anndata's element codec, modified in memory and
    written back over itself. That is one subtree: X, var, obsm, uns and every
    other table in a SpatialData store are never opened, let alone rebuilt --
    the property the module docstring insists on, and the reason this is not
    `ad.read_h5ad()` plus `write_h5ad()`.
    """
    try:
        from anndata.io import read_elem, write_elem  # anndata >= 0.10, public API
    except ImportError:  # pragma: no cover - older anndata fallback
        from anndata._io.specs import read_elem, write_elem

    path = _table_path(dataset)
    source = dataset.table.source

    with _open_group(path) as handle:
        obs = read_elem(handle["obs"])

    taken = [c for c in (category_column, name_column) if c in obs.columns]
    if taken and not replace:
        raise ColumnExists(sorted(obs.columns), _suggest_prefix(category_column, obs.columns))

    mask = _project_rows(dataset, obs)
    selected = int(mask.sum())
    if selected != len(labels):
        # The file has changed under the loaded table -- rows added, removed or
        # reordered since it was read. Assigning anyway would put every label on
        # the wrong cell, which is invisible in the file and wrong forever.
        raise ValueError(
            f"this project's table has {len(labels)} cells but the file now has "
            f"{selected} matching rows; reopen the project and try again"
        )

    obs = _assign_column(obs, mask, category_column, labels,
                         dataset, source, categorical=True)
    obs = _assign_column(obs, mask, name_column, names,
                         dataset, source, categorical=False)

    with _open_group(path, writable=True) as handle:
        # Unlinked and rewritten rather than patched in place: obs is a group
        # whose column layout lives in its attributes, and a codec that writes
        # the frame as a whole is the only one that keeps those honest.
        del handle["obs"]
        write_elem(handle, "obs", obs)

    return {
        "path": str(path),
        "columns": [category_column, name_column],
        "n_cells": len(labels),
        "n_assigned": sum(1 for value in names if value),
    }


def _assign_column(obs, mask, column, values, dataset, source, categorical):
    """One column written onto the masked rows, aligned by cell id.

    Alignment is by identifier wherever the project has one, never by position:
    a project is routinely a subset of its file's obs, and a positional write
    against the wrong offset shifts every label by the size of whatever came
    before it. Row numbers are the fallback precisely because they are the case
    where the user told us there is no identifier to align on.
    """
    import numpy as np
    import pandas as pd

    id_field = getattr(dataset.project.dataset, "obs_id_field", None)
    frame = dataset.table.frame()
    cell_id = dataset.schema.cell_id if dataset.schema else None

    series = obs[column] if column in obs.columns else pd.Series(
        [None] * len(obs), index=obs.index, dtype="object")
    series = series.astype("object")

    positions = np.flatnonzero(np.asarray(mask))
    if id_field and id_field in obs.columns and frame is not None and cell_id in frame.columns:
        wanted = {str(key): value
                  for key, value in zip(frame[cell_id].to_list(), values)}
        keys = obs[id_field].astype(str).to_numpy()
        for position in positions:
            if keys[position] in wanted:
                series.iloc[position] = wanted[keys[position]]
    else:
        # No identifier column: the loaded table is the masked rows in file
        # order, which the length check above has already confirmed.
        for offset, position in enumerate(positions):
            series.iloc[position] = values[offset]

    # Not a plain object column: anndata's codec refuses one holding both
    # strings and None ("Can't implicitly convert non-string objects to
    # strings"), and rows belonging to another image are exactly the Nones. The
    # category column becomes a pandas categorical, which is what obs columns of
    # labels are everywhere else and what makes scanpy plot it without being
    # asked twice; names stay free text as a nullable string array.
    obs[column] = (series.astype("string").astype("category") if categorical
                   else series.astype("string"))
    return obs


def _project_rows(dataset, obs):
    """A boolean mask over `obs` selecting the cells this project can see.

    Two filters, and both matter. The registration subset is how a project was
    narrowed to one image at import. The image-id column is the answer the user
    gave later, through the requirements modal, and it is the one that makes
    running this once per image against one shared file safe -- without it a
    second pass would write over the first image's labels.
    """
    import numpy as np

    from plexora.plugins.roi.server import mapping

    mask = np.ones(len(obs), dtype=bool)

    subset = dict((dataset.table.source.subset if dataset.table.source else None) or {})
    column = subset.get("column")
    if column:
        if column not in obs.columns:
            raise ValueError(f"subset column {column!r} is no longer in this file's obs")
        mask &= obs[column].astype(str).to_numpy() == str(subset.get("value"))

    image_id = mapping.current_image_id(dataset)
    image_column = dataset.schema.image_id if dataset.schema else None
    if image_id is not None and image_column and image_column in obs.columns:
        mask &= obs[image_column].astype(str).to_numpy() == str(image_id)
    return mask


def _write_csv_columns(dataset, labels, names, category_column, name_column, replace):
    """The CSV path: the whole file, because a CSV has no subtree.

    Written to a temporary file in the same directory and renamed over the
    original, so a reader never sees a half-written table and a failure partway
    through leaves the original intact. That is the same guarantee
    `project.write_config()` gives config.json, and for the same reason: this is
    somebody's measurements and often the only copy.
    """
    import os
    import tempfile

    import polars as pl

    source = dataset.table.source
    if source is None or not source.path:
        raise ValueError("no CSV file is recorded for this project")

    frame = pl.read_csv(source.path)
    taken = [c for c in (category_column, name_column) if c in frame.columns]
    if taken and not replace:
        raise ColumnExists(sorted(frame.columns),
                           _suggest_prefix(category_column, frame.columns))
    if frame.height != len(labels):
        raise ValueError(
            f"this project's table has {len(labels)} cells but the file now has "
            f"{frame.height} rows; reopen the project and try again"
        )

    frame = frame.with_columns([
        pl.Series(category_column, labels, dtype=pl.Utf8),
        pl.Series(name_column, names, dtype=pl.Utf8),
    ])

    directory = os.path.dirname(os.path.abspath(source.path)) or "."
    handle, temporary = tempfile.mkstemp(suffix=".csv", dir=directory)
    os.close(handle)
    try:
        frame.write_csv(temporary)
        os.replace(temporary, source.path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise

    return {
        "path": source.path,
        "columns": [category_column, name_column],
        "n_cells": len(labels),
        "n_assigned": sum(1 for value in names if value),
    }


def _table_path(dataset):
    """Where this project's AnnData group actually lives.

    A SpatialData store is narrowed to the one table this project reads --
    writing at the store root would put obs somewhere no reader looks, and
    `_open_group`'s re-consolidation is only safe on the group it wrote.
    """
    source = dataset.table.source
    if source is None or not source.path:
        raise ValueError("no data file is recorded for this project")
    if dataset.source_kind == "spatialdata":
        from plexora.server.models.adapters.spatialdata_adapter import table_path

        return str(table_path(source.path, source.table))
    return source.path


def _suggest_prefix(category_column, taken):
    """A free destination name, given that `<name>_category` is taken."""
    base = category_column[: -len("_category")]
    return _suggest(base, {str(c) for c in taken})


def _shapely(geometry, sgeom):
    """A stored geometry as a shapely object, holes and all."""
    kind = geometry.get("type")
    if kind == "MultiPolygon":
        return sgeom.MultiPolygon([_polygon(part, sgeom) for part in geometry["coordinates"]])
    return _polygon(geometry["coordinates"], sgeom)


def _polygon(rings, sgeom):
    outer, *holes = rings
    return sgeom.Polygon(outer, holes)


def _suggest(name, taken):
    """The next free `name_2`, `name_3`, ... """
    base = name.rstrip("0123456789_") or name
    for index in range(2, 1000):
        candidate = f"{base}_{index}"
        if candidate not in taken:
            return candidate
    return f"{base}_{len(taken) + 1}"  # pragma: no cover - a thousand collisions


def _spatialdata_store(dataset):
    if dataset.source_kind != "spatialdata":
        raise ValueError("this project's data did not come from a SpatialData store")
    source = dataset.table.source
    if source is None or not source.path:
        raise ValueError("no SpatialData store is recorded for this project")
    if not _is_group(Path(source.path)):
        raise ValueError(f"{source.path!r} is not a zarr store")
    return source.path


# -- opening an on-disk AnnData -----------------------------------------


def _is_group(path: Path) -> bool:
    return path.is_dir() and any((path / marker).exists() for marker in _GROUP_MARKERS)


def _consolidated_format(path: Path):
    """Which zarr format's consolidated index this group carries, if any.

    A consolidated index is a cached copy of every child's metadata kept beside
    the group -- `.zmetadata` in v2, a `consolidated_metadata` key inside
    `zarr.json` in v3. Readers that find one trust it completely and never list
    the directory, which is both why a write is refused and why a write has to
    be followed by a refresh.
    """
    if (path / ".zmetadata").is_file():
        return 2
    metadata = path / "zarr.json"
    if metadata.is_file():
        try:
            document = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # pragma: no cover - unreadable metadata
            return None
        if document.get("consolidated_metadata") is not None:
            return 3
    return None


@contextlib.contextmanager
def _open_group(path: str, writable: bool = False):
    """The root group of an on-disk AnnData, for either backend.

    anndata's element codec is storage-agnostic, so the caller works unchanged
    against an h5py group from an .h5ad or a zarr group from a store. A .zarr is
    a directory, which is what tells the two apart.

    The consolidated-zarr dance is the same one gating's anndata_gates._open_group
    documents at length, and for the same reasons: anndata refuses to write to a
    group whose metadata is consolidated, and skipping the rebuild afterwards
    loses the write silently because every reader consults the stale index. The
    refresh is confined to the group written -- re-consolidating a SpatialData
    ROOT drops every v2 table from a v3 index, and real stores mix the two.

    Duplicated rather than imported from the gating plugin on purpose: a plugin
    that reaches into another plugin's internals stops working the moment the
    user's build does not ship that plugin.
    """
    if Path(path).is_dir():
        import zarr

        # Path (not str) deliberately: zarr v3 parses a string store as a URL,
        # mangling names containing characters like '#'.
        location = Path(path)
        if not writable:
            yield zarr.open_group(location, mode="r")
            return

        consolidated = _consolidated_format(location)
        yield zarr.open_group(location, mode="a", use_consolidated=False)
        if consolidated is not None:
            zarr.consolidate_metadata(
                zarr.storage.LocalStore(location), zarr_format=consolidated
            )
        return

    import h5py

    with h5py.File(path, "r+" if writable else "r") as handle:
        yield handle
