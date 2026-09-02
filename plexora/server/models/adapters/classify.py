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
column names pays for numpy/polars/anndata.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# Conventional names for the column identifying which image/sample/region an
# observation belongs to. Also used by AnnDataAdapter to decide whether a file
# might span several images and therefore needs an explicit subset choice --
# deliberately not "any categorical column", since cell_type/cluster/condition
# are common in ordinary single-image data and must not trigger that.
_LIKELY_IMAGE_IDENTIFIER_NAMES = {
    "imageid", "image", "sample", "sampleid", "region", "regionid",
    "roi", "fov", "well", "slide", "core",
    # Vendor vocabulary for the same thing: Zeiss writes "scene", Akoya
    # "acquisition", and a tissue microarray names its cores off the "tma".
    # Read as words (see is_likely_image_identifier_name), so "tma_core" and
    # "acquisition_id" now resolve where they used to fall through.
    "scene", "acquisition", "tma", "specimen",
}

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
    # morphology
    "area", "convexarea", "filledarea", "bboxarea", "perimeter",
    "majoraxislength", "minoraxislength", "axismajorlength", "axisminorlength",
    "eccentricity", "solidity", "extent", "orientation", "circularity",
    "equivalentdiameter", "feretdiametermax", "eulernumber", "formfactor",
    "elongation", "compactness", "roundness", "aspectratio",
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

#: Role -> ordered patterns, most specific first. The first column matching the
#: earliest pattern wins, so "X_centroid" beats a bare "X" for the x role but a
#: table with only "X" still resolves.
_ROLE_PATTERNS = {
    "cell_id": (r"^cellid$", r"^cellindex$", r"^objectid$", r"^cell$", r"^label$", r"^id$"),
    "x": (r"^xcentroid$", r"^centroidx$", r"^xposition$", r"^xcoordinate$",
          r"^columncentroid$", r"^globalx$", r"^x$"),
    "y": (r"^ycentroid$", r"^centroidy$", r"^yposition$", r"^ycoordinate$",
          r"^rowcentroid$", r"^globaly$", r"^y$"),
    "celltype": (r"^phenotype$", r"^celltype$", r"^cluster$", r"^leiden$"),
    "image_id": tuple(rf"^{re.escape(name)}$" for name in sorted(_LIKELY_IMAGE_IDENTIFIER_NAMES)),
}

_NUMERIC_DTYPE = re.compile(r"int|float|double|number", re.IGNORECASE)


def normalize_column_name(name: Any) -> str:
    """Strip separators and case so the name vocabularies above stay short."""
    return re.sub(r"[\s_\-.]+", "", str(name)).lower()


#: Qualifiers a column name adds to the thing it identifies. Stripped before
#: the vocabulary lookup so "coreid" and "fovname" resolve to "core" and "fov"
#: rather than to nothing at all.
_IDENTIFIER_SUFFIXES = ("id", "name", "number", "num", "index", "idx", "key", "label")


def _name_tokens(name: Any) -> list[str]:
    """A column name split into its words, lowercased.

    Deliberately NOT `normalize_column_name`, which strips separators: the
    separators are the only word boundary a column name has, and losing them is
    what forces a substring test -- under which "score" ends with "core" and a
    marker column becomes an image identifier.
    """
    return [token for token in re.split(r"[\s_\-.]+", str(name).lower()) if token]


def is_likely_image_identifier_name(column_name: Any) -> bool:
    """Whether a column name looks like it identifies an image/sample/region.

    This decides whether the user is ASKED which image a multi-image table
    should be read as, so the two failure modes are wildly asymmetric: a name
    this does not recognise means sixty images' cells drawn over one image with
    nothing said (see AnnDataAdapter.load_table's guard), while a name it
    recognises wrongly means one extra dropdown on a form. Bias towards asking.

    Matching the whole separator-stripped name against the vocabulary was the
    entire test, and it let through every conventional name carrying a
    qualifier: `sample_id` normalizes to "sampleid" which happens to be listed,
    but `core_id`, `ROI_ID`, `slide_id`, `FOV_name` and `tma_core` all
    normalized to something absent from it. So the name is now read as words,
    and any one of them naming an image is enough.
    """
    normalized = normalize_column_name(column_name)
    if not normalized:
        return False
    if normalized in _LIKELY_IMAGE_IDENTIFIER_NAMES:
        return True
    for token in _name_tokens(column_name):
        if token in _LIKELY_IMAGE_IDENTIFIER_NAMES:
            return True
        # "coreid" written without a separator. The stem must be non-empty, or
        # a column literally called "id" would strip to nothing and match.
        for suffix in _IDENTIFIER_SUFFIXES:
            stem = token[: -len(suffix)] if token.endswith(suffix) else ""
            if stem and stem in _LIKELY_IMAGE_IDENTIFIER_NAMES:
                return True
    return False


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
