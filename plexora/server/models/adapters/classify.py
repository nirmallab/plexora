"""Which columns are markers, and which are metadata.

This used to be answered in five places that disagreed: three hardcoded
denylists (`import_routes.listNotMarkers`, `datasource._default_marker_columns`,
`channelMatch.js markers_notToTransform`) and two independent "numeric column
that produced a histogram" derivations (`api/dataset.TableHandle.markers`,
`datasetContext.js`). Adding a morphology column meant remembering all of them.

It is one question about the imported data, so it gets one answer, computed
once at import and stored on the project (`DataSpec.columns`). Everything
downstream reads the stored answer rather than re-deriving it.

The prediction is a starting point, not a verdict -- for CSV the user confirms
it on the classification screen and drags anything we got wrong. Being roughly
right matters much more than being subtly clever, so the rules are blunt and
name-based, and deliberately biased towards calling a column metadata: a
metadata column wrongly offered as a marker shows up as a nonsense histogram in
every plugin, while a marker wrongly filed as metadata is one drag away.

Stdlib only, on purpose. `anndata_adapter` imports the name heuristics from
here rather than the other way round, so nothing that merely wants to classify
column names pays for numpy/polars/anndata. The image-identifier rule is
`spatialbridge.grouping.match`'s (also stdlib only), shared with SCIMAP Pro.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from spatialbridge.grouping.match import (  # noqa: F401
    _IDENTIFIER_SUFFIXES,
    IMAGE_ID_COLUMN_WORDS as _LIKELY_IMAGE_IDENTIFIER_NAMES,
    _name_tokens,
    is_likely_image_identifier_name,
    normalize_column_name,
)

# Non-marker columns a quantification table conventionally carries. Matched
# against the normalized name, so "X_centroid", "x centroid" and "XCentroid"
# are one entry. Grouped by what they are, because that is how they get
# extended.
_METADATA_NAMES = {
    # identity
    "id", "cellid", "cellindex", "objectid", "object", "label", "index",
    # position
    "x", "y", "xcentroid", "ycentroid", "centroidx", "centroidy",
    "xposition", "yposition", "xcoordinate", "ycoordinate",
    "columncentroid", "rowcentroid", "column", "row", "globalx", "globaly",
    # position in depth and time: a z-stack's object table (server/volume)
    "z", "zcentroid", "centroidz", "zposition", "zcoordinate", "globalz",
    "zplane", "zslice", "zindex", "plane",
    "t", "frame", "xum", "yum", "zum",
    # morphology
    "area", "convexarea", "filledarea", "bboxarea", "perimeter",
    "majoraxislength", "minoraxislength", "axismajorlength", "axisminorlength",
    "eccentricity", "solidity", "extent", "orientation", "circularity",
    "equivalentdiameter", "feretdiametermax", "eulernumber", "formfactor",
    "elongation", "compactness", "roundness", "aspectratio",
    # morphology in 3-D (server/volume/measure.py's columns among them)
    "volume", "volumevox", "volumeum3", "surfacearea", "surfaceareaum2",
    "sphericity", "nplanes", "toucheszbounds", "touchesxybounds",
    # annotation
    "phenotype", "celltype", "cluster", "leiden", "louvain", "condition",
    "treatment", "patient", "donor", "batch", "replicate", "timepoint",
} | _LIKELY_IMAGE_IDENTIFIER_NAMES

# Suffixes that mark a column as a derived measurement of something rather
# than an intensity: "CD3_area", "DNA1_orientation". Checked after the exact
# names above so a marker literally called "Area" still resolves.
_METADATA_SUFFIXES = (
    "centroid", "area", "perimeter", "eccentricity", "solidity", "extent",
    "orientation", "axislength", "diameter", "circularity",
)

# Prefixes of the same kind: a bounding box's corners ("bbox_z0"), an object's
# contact with the image edge ("touches_z_bounds").
_METADATA_PREFIXES = ("bbox", "touches")

#: How Plexora names a derived per-channel value: "<channel>__<statistic>"
#: ("CD8__integrated", "CD8__p90"). The channel's own column is its mean and
#: the marker; these sit beside it and are never a second marker.
DERIVED_SEPARATOR = "__"

#: Role -> ordered patterns, most specific first. The first column matching the
#: earliest pattern wins, so "X_centroid" beats a bare "X" for the x role but a
#: table with only "X" still resolves.
_ROLE_PATTERNS = {
    "cell_id": (r"^cellid$", r"^cellindex$", r"^objectid$", r"^cell$", r"^label$", r"^id$"),
    "x": (r"^xcentroid$", r"^centroidx$", r"^xposition$", r"^xcoordinate$",
          r"^columncentroid$", r"^globalx$", r"^x$"),
    "y": (r"^ycentroid$", r"^centroidy$", r"^yposition$", r"^ycoordinate$",
          r"^rowcentroid$", r"^globaly$", r"^y$"),
    "z": (r"^zcentroid$", r"^centroidz$", r"^zposition$", r"^zcoordinate$",
          r"^globalz$", r"^z$", r"^zplane$", r"^zslice$", r"^zindex$", r"^plane$"),
    "celltype": (r"^phenotype$", r"^celltype$", r"^cluster$", r"^leiden$"),
    "image_id": tuple(rf"^{re.escape(name)}$" for name in sorted(_LIKELY_IMAGE_IDENTIFIER_NAMES)),
}

_NUMERIC_DTYPE = re.compile(r"int|float|double|number", re.IGNORECASE)


def is_numeric_dtype(dtype: Any) -> bool:
    """Whether a reported dtype string is numeric.

    Takes the string rather than the dtype object so this works for polars,
    numpy and pandas alike, and so a caller with only a JSON payload (the
    client sends dtypes as strings) gets the same answer.
    """
    return bool(_NUMERIC_DTYPE.search(str(dtype or "")))


def looks_like_metadata(name: Any, dtype: Any = None) -> bool:
    """Whether one column should start life in the metadata box."""
    normalized = normalize_column_name(name)
    if not normalized:
        return True
    if dtype is not None and not is_numeric_dtype(dtype):
        # A marker is an intensity. Anything non-numeric is an annotation,
        # whatever it is called.
        return True
    if normalized in _METADATA_NAMES:
        return True
    if DERIVED_SEPARATOR in str(name).strip(DERIVED_SEPARATOR[0]):
        return True
    if any(normalized.startswith(prefix) for prefix in _METADATA_PREFIXES):
        return True
    return any(normalized.endswith(suffix) for suffix in _METADATA_SUFFIXES)


def guess_roles(columns: Iterable[Any]) -> dict:
    """Best guess at which column fills each role.

    Returns only the roles it is confident about; an unresolved role is simply
    absent, which the project record stores as None and something may later ask
    the user to fill in.
    """
    names = [str(c) for c in columns]
    by_normalized = {}
    for name in names:
        by_normalized.setdefault(normalize_column_name(name), name)

    roles = {}
    taken = set()
    for role, patterns in _ROLE_PATTERNS.items():
        for pattern in patterns:
            match = next(
                (original for normalized, original in by_normalized.items()
                 if original not in taken and re.match(pattern, normalized)),
                None,
            )
            if match is not None:
                roles[role] = match
                taken.add(match)
                break
    return roles


def classify_columns(columns: Iterable[Mapping[str, Any] | str]) -> dict:
    """Split columns into markers and metadata, and guess the column roles.

    `columns` is either bare names or `{"name": ..., "dtype": ...}` mappings;
    supplying dtypes makes the split markedly better, since every non-numeric
    column is metadata regardless of its name.

    Returns `{"markers": [...], "metadata": [...], "roles": {...}}` with the
    input order preserved inside each group -- a table's own column order is
    usually meaningful to whoever produced it, and reordering makes the
    classification screen harder to check.
    """
    names, dtypes = [], {}
    for column in columns or ():
        if isinstance(column, Mapping):
            name = column.get("name")
            if name is None:
                continue
            names.append(str(name))
            dtypes[str(name)] = column.get("dtype")
        else:
            names.append(str(column))
            dtypes[str(column)] = None

    metadata = [n for n in names if looks_like_metadata(n, dtypes.get(n))]
    metadata_set = set(metadata)
    markers = [n for n in names if n not in metadata_set]
    return {"markers": markers, "metadata": metadata, "roles": guess_roles(names)}


def classify_from_inspection(inspection: Mapping[str, Any]) -> dict:
    """The split for an AnnData or SpatialData table, taken from its structure.

    `var_names` are markers and `obs` columns are metadata by construction --
    the file already draws the line this screen exists to draw for CSV, so the
    user is never asked to confirm it. Roles are still guessed from the obs
    names, since which obs column holds the cell id is not structural.
    """
    obs_columns = inspection.get("obs_columns") or []
    obs_names = [c.get("name") for c in obs_columns if isinstance(c, Mapping) and c.get("name")]
    return {
        "markers": [str(v) for v in (inspection.get("var_names") or ())],
        "metadata": [str(n) for n in obs_names],
        "roles": guess_roles(obs_names),
    }
