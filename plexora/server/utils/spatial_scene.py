"""What a spatial store holds, and where each piece sits.

`resolve_image_path` answers "which single image do I open" for a store that
holds one. This answers the question a scene asks instead: what is in here, and
how do the pieces line up against each other. It is the last step of the layer
model -- by the time anything calls it, every consumer already exists, and the
importer only fills in a model that is already proven.

Two sources, one shape out:

**A SpatialData store.** Elements under `images/`, `labels/`, `points/`,
`shapes/`, each declaring the coordinate systems it lands in. Read as plain JSON,
without importing `spatialdata` -- the metadata IS JSON, and a core build must
not grow a dependency to read it. See `ngff_transform`.

**A Xenium run directory.** A fixed set of filenames beside each other:
`morphology.ome.tif`, `transcripts.parquet`, `cell_boundaries.parquet`,
`nucleus_boundaries.parquet`, `experiment.xenium`. Everything is in MICRONS in a
common frame, so the transform is the reference image's pixel size -- which the
experiment file states.

What comes out is a list of `LayerSpec`s ready for `Project.with_layer`, with the
FIRST image chosen as the reference and every other element's transform composed
through it. Nothing is opened, nothing is converted, and no pixel is read: a
scene with 300 GB of morphology in it is enumerated in milliseconds.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from plexora.server.models.project import LayerSpec, normalize_transform
from plexora.server.utils import ngff_transform

#: The SpatialData element groups, and the layer kind each maps to. `labels` are
#: a raster of cell ids -- the same thing Plexora's segmentation mask is -- and
#: `shapes` are polygons, which is boundaries. One line each, because the kind
#: system is a rendering strategy and these are exactly four of them.
SPATIALDATA_KINDS = {
    "images": "image",
    "labels": "labels",
    "points": "points",
    "shapes": "shapes",
}

#: What a Xenium run directory is made of. Name -> (layer id, kind, modality).
#: Recognised by name because Xenium's output layout is fixed; a directory
#: missing all of them is simply not a Xenium run.
#:
#: `morphology_focus` is a DIRECTORY from XOA 2.0 on: the focus stack is written
#: as a multi-file OME series, `morphology_focus_0000.ome.tif` and its
#: siblings. Kept in the same table as the single files because to everything
#: downstream it is one image layer either way -- see `_xenium_path`.
#:
#: **Order is preference**, and it is the difference between a viewer that
#: works and one that does not. `morphology.ome.tif` is a Z-STACK: fourteen
#: focal depths of DAPI, each autofocused per field of view, so the planes are
#: not aligned with each other and no one of them covers the whole section.
#: The focus image is the 2-D composite the instrument built out of exactly
#: those planes, and it is what Xenium Explorer draws. The stack is the last
#: resort -- a run that shipped without a focus image at all -- and reaching
#: it means `channel_series` pins its middle plane.
XENIUM_FILES = {
    "morphology_focus": ("morphology", "image", "xenium_morphology"),
    "morphology_focus.ome.tif": ("morphology", "image", "xenium_morphology"),
    "morphology_mip.ome.tif": ("morphology", "image", "xenium_morphology"),
    "morphology.ome.tif": ("morphology", "image", "xenium_morphology"),
    "transcripts.parquet": ("transcripts", "points", "transcripts"),
    "cell_boundaries.parquet": ("cell_boundaries", "shapes", "cell_boundaries"),
    "nucleus_boundaries.parquet": ("nucleus_boundaries", "shapes", "nucleus_boundaries"),
}

#: The cell table a Xenium run ships, and the expression matrix beside it.
#: Separate from XENIUM_FILES because these are not spatial LAYERS -- one
#: becomes the sample's feature table and the other is recorded and not read
#: yet -- and folding them in would put them in front of `layers_for`.
XENIUM_TABLES = {
    "cells.parquet": "cells",
    "cell_feature_matrix.h5": "expression",
}

#: What Visium's spatial folder holds. A run is recognised by the three of them
#: together: the positions alone are a CSV, and the scale factors alone are a
#: JSON file that says nothing about where it belongs.
VISIUM_SPATIAL = "spatial"
VISIUM_SCALEFACTORS = "scalefactors_json.json"

#: The transcript columns every adapter here recognises. Held in CORE rather
#: than in the transcripts plugin, and that is not an accident: the importer has
#: to be able to say "this parquet is transcripts" while proposing an import,
#: and `tests/test_plugin_boundary.py` pins that core imports no plugin. The
#: plugin re-exports these so its own reader still owns the names.
TRANSCRIPT_COLUMNS = ("feature_name", "x_location", "y_location")


@dataclass(frozen=True)
class SceneElement:
    """One thing found in a store, before it becomes a layer."""

    id: str
    kind: str
    path: Path
    #: What this element IS, as opposed to how it is drawn. `kind` picks the
    #: renderer and is one of four; this is open and is what a plugin matches
    #: on. Carried from here to `LayerSpec.modality` unchanged.
    modality: str = ""
    #: Where this element lands in the shared coordinate system, or None when it
    #: does not declare one. None is information: it means nobody registered
    #: this against anything, which the Layer Manager says out loud.
    to_system: tuple[float, ...] | None = None
    label: str = ""


#: The group every SpatialData store keeps its AnnData tables under. Not in
#: SPATIALDATA_KINDS because a table is not a spatial LAYER -- nothing draws it
#: -- but it is every bit as much a sign that this directory is a store, and a
#: store that holds only tables is the ordinary shape of a quantified run.
SPATIALDATA_TABLES = "tables"


def is_spatialdata_store(path) -> bool:
    """Whether this directory is laid out the way SpatialData writes one.

    THE one answer. There used to be three that disagreed: the adapters asked
    for `tables/`, this asked for any of the four element groups, and
    `resolve_image_path` had a third notion again -- so a store holding a
    morphology image and no table was called AnnData and handed to a reader
    that cannot open it, while a store holding only tables was not recognised
    as a store at all.

    Structural and cheap: nothing is opened, so a store with thousands of
    chunks costs a handful of stats.
    """
    root = Path(path)
    if not root.is_dir():
        return False
    groups = (*SPATIALDATA_KINDS, SPATIALDATA_TABLES)
    return any((root / group).is_dir() for group in groups)


def _xenium_path(root, name):
    """The path to open for one Xenium output, or None when it is absent.

    A directory entry (`morphology_focus/` from XOA 2.0 on) resolves to the
    FOLDER when it holds several channel files and to the single file when it
    holds one. Both are one image layer; the split is about which reader gets
    it. Several files need `xenium_focus`, which composes them into one
    multi-channel pyramid; one file is an ordinary single-channel OME-TIFF and
    goes down the path every other import in Plexora already takes.

    One place that knows the difference, so nothing downstream has to.
    """
    from plexora.server.utils import xenium_focus

    candidate = Path(root) / name
    if candidate.is_file():
        return candidate
    if candidate.is_dir():
        if xenium_focus.is_focus_dir(candidate):
            return candidate
        series = (xenium_focus.focus_files(candidate)
                  or sorted(candidate.glob("*.ome.tif"))
                  or sorted(candidate.glob("*.tif")))
        return series[0] if series else None
    return None


def xenium_manifest(path):
    """`experiment.xenium` as a dict, or `{}`.

    One read, shared by everything that asks the run about itself. A missing
    or unreadable manifest is `{}` rather than an error: a directory somebody
    copied four files out of is still a Xenium run to `is_xenium_run`, and
    every caller here has a fallback that does not need the manifest.
    """
    manifest = Path(path) / "experiment.xenium"
    if not manifest.is_file():
        return {}
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def xenium_image_path(path):
    """Which of a run's morphology images is the one to draw, or None.

    The run's own manifest first. `experiment.xenium` names its images under
    `images` -- `morphology_focus_filepath` and `morphology_filepath` -- and a
    path the instrument wrote beats a filename this module guessed, which
    matters for a run whose outputs were reorganised or whose focus image is
    somewhere the glob would not look.

    The preference between them is ours and is stated in `XENIUM_FILES`: the
    focus composite, then the maximum projection, then the raw Z-stack. The
    manifest says where each image IS, not which one a viewer should open.
    """
    root = Path(path)
    images = xenium_manifest(root).get("images")
    if isinstance(images, dict):
        for key in ("morphology_focus_filepath", "morphology_mip_filepath",
                    "morphology_filepath"):
            relative = images.get(key)
            if not isinstance(relative, str) or not relative:
                continue
            named = root / relative
            if named.is_dir():
                resolved = _xenium_path(named.parent, named.name)
                if resolved is not None:
                    return resolved
            if not named.is_file():
                continue
            # A manifest that points INTO a focus folder still describes the
            # folder: the other stains are its siblings, and opening the one
            # file the manifest happened to name would drop three channels.
            resolved = _xenium_path(named.parent.parent, named.parent.name)
            if resolved is not None and Path(resolved).is_dir():
                return resolved
            return named

    for name, (layer_id, kind, _modality) in XENIUM_FILES.items():
        if kind != "image" or layer_id != "morphology":
            continue
        candidate = _xenium_path(root, name)
        if candidate is not None:
            return candidate
    return None


#: What each of a run's morphology outputs IS, in the few words an import row
#: has for it. Keyed by the name `XENIUM_FILES` knows it by, so the preference
#: order above and the sentence under the row cannot drift apart.
#:
#: There is no entry for the Z-stack's plane count: that is in the file, not in
#: its name, and the importer says it (`import_proposal._focal_note`) once it
#: has the header open anyway.
MORPHOLOGY_NOTES = {
    "morphology_focus": "focus composite",
    "morphology_focus.ome.tif": "focus composite",
    "morphology_mip.ome.tif": "maximum projection",
    "morphology.ome.tif": "focus stack",
}


def xenium_image_note(path) -> str:
    """Which morphology output `path` is, in words, or `""`.

    Said out loud because the choice is otherwise invisible: a run ships up to
    three pictures of the same tissue, Plexora opens exactly one of them, and a
    user who cannot see which one has no way to tell a correct import from an
    import of the wrong image. The row reads "focus composite" and the question
    does not arise.

    Matched on the name, and on the PARENT's name for a file resolved out of a
    `morphology_focus/` folder -- `morphology_focus_0002.ome.tif` is a focus
    composite because of the folder it is in, which is the same reasoning
    `_xenium_path` applies to get there.
    """
    if path is None:
        return ""
    named = Path(path)
    note = MORPHOLOGY_NOTES.get(named.name)
    if note is None and named.is_file():
        note = MORPHOLOGY_NOTES.get(named.parent.name)
    return note or ""


def is_xenium_run(path) -> bool:
    """Whether this directory is a Xenium output bundle.

    By the experiment file OR by two of the known outputs sitting together: a
    user who copied three files out of a run still has a Xenium run as far as
    this is concerned, and refusing it for a missing manifest would be pedantry.
    """
    root = Path(path)
    if not root.is_dir():
        return False
    if (root / "experiment.xenium").is_file():
        return True
    found = sum(1 for name in XENIUM_FILES if _xenium_path(root, name))
    found += sum(1 for name in XENIUM_TABLES if (root / name).is_file())
    return found >= 2


def is_visium_run(path) -> bool:
    """Whether this directory is a Space Ranger output for a Visium slide.

    The three together: spot positions, the scale factors that put them in the
    picture's pixels, and a feature matrix. Any one alone is a file that could
    have come from anywhere, and a folder that has all three is unambiguous.
    """
    root = Path(path)
    if not root.is_dir():
        return False
    spatial = root / VISIUM_SPATIAL
    if not spatial.is_dir():
        return False
    if not (spatial / VISIUM_SCALEFACTORS).is_file():
        return False
    positions = any(spatial.glob("tissue_positions*.csv"))
    matrix = any(root.glob("*feature_bc_matrix.h5")) or any(
        root.glob("*feature_bc_matrix"))
    return bool(positions and matrix)


# -- Visium HD ----------------------------------------------------------------
#
# A Space Ranger 3/4 run of a Visium HD slide. Not a Visium run with smaller
# spots: the positions are a parquet per bin size under
# `binned_outputs/square_XXXum/spatial/`, there is no positions file where
# `is_visium_run` looks, and the grid is 30 million squares -- so detection,
# the proposal and the rendering are all their own. Checked BEFORE
# `is_visium_run` everywhere, because a lone `square_008um/` folder has the
# `spatial/scalefactors_json.json` both look for.

VISIUM_HD_BINNED = "binned_outputs"
VISIUM_HD_SEGMENTED = "segmented_outputs"
VISIUM_HD_POSITIONS = "tissue_positions.parquet"
VISIUM_HD_MATRIX = "filtered_feature_bc_matrix.h5"
VISIUM_HD_CELL_MATRIX = "filtered_feature_cell_matrix.h5"
VISIUM_HD_CELLS = "cell_segmentations.geojson"
VISIUM_HD_NUCLEI = "nucleus_segmentations.geojson"
VISIUM_HD_BIN_RE = re.compile(r"^square_(\d{3})um$")

#: The files 10x publishes BESIDE a run's `outs/` -- a download of a public
#: dataset is these plus the tarballs, all prefixed with the sample name. None
#: of them is a layer or a table: `barcode_mappings.parquet` maps 2 µm squares
#: to cells, `metrics_summary.csv` is one row of QC numbers that reads as a
#: one-cell table. Matched on the name's ending, because the prefix is the
#: sample's.
SPACERANGER_SIDE_FILES = (
    "barcode_mappings.parquet", "metrics_summary.csv", "web_summary.html",
    "molecule_info.h5", "feature_slice.h5", ".cloupe",
)


def is_spaceranger_side_file(path) -> bool:
    """Whether `path` is one of the run-level files 10x ships beside `outs/`."""
    return Path(path).name.lower().endswith(SPACERANGER_SIDE_FILES)


#: How many squares the detection-time registration fit reads. The affine is
#: global, so one record batch registers the layer to well under a pixel, and
#: detection must not read 30 million rows to propose an import.
VISIUM_HD_FIT_ROWS = 200_000


@dataclass(frozen=True)
class BinLevel:
    """One `square_XXXum` folder of a Visium HD run."""

    size_um: float
    root: Path
    matrix: Path | None
    positions: Path
    scalefactors_path: Path
    scalefactors: dict
    hires: Path | None
    lowres: Path | None
    clusters: tuple = ()
    umap: Path | None = None


def _hd_scalefactors(level_dir):
    """A level folder's scale factors when they are Visium HD's, else None.

    `bin_size_um` is the marker: standard Visium's scale factors do not carry
    it, and it is what says this folder is a bin level at all.
    """
    path = Path(level_dir) / VISIUM_SPATIAL / VISIUM_SCALEFACTORS
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and "bin_size_um" in doc else None


def _is_hd_level(path) -> bool:
    root = Path(path)
    if not root.is_dir() or _hd_scalefactors(root) is None:
        return False
    if not (root / VISIUM_SPATIAL / VISIUM_HD_POSITIONS).is_file():
        return False
    return (root / VISIUM_HD_MATRIX).is_file() or any(
        root.glob("*feature_bc_matrix.h5"))


def _hd_level_dirs(path) -> list[Path]:
    root = Path(path)
    if not root.is_dir():
        return []
    if _is_hd_level(root):
        return [root]
    binned = root / VISIUM_HD_BINNED if (root / VISIUM_HD_BINNED).is_dir() else (
        root if root.name == VISIUM_HD_BINNED else None)
    if binned is None:
        return []
    try:
        children = sorted(binned.iterdir())
    except OSError:
        return []
    return [child for child in children
            if VISIUM_HD_BIN_RE.match(child.name) and _is_hd_level(child)]


def is_visium_hd_run(path) -> bool:
    """Whether this is a Visium HD `outs/`, its `binned_outputs/`, or one level."""
    return bool(_hd_level_dirs(path))


def visium_hd_root(path) -> Path | None:
    """The run folder levels and segmentation are found under, or None.

    The `outs/` for a whole run; the level folder itself when only one
    `square_XXXum/` was picked.
    """
    levels = _hd_level_dirs(path)
    if not levels:
        return None
    root = Path(path)
    if root.name == VISIUM_HD_BINNED:
        return root.parent
    return root


def _analysis_of(level_dir):
    analysis = Path(level_dir) / "analysis"
    clusters = tuple(sorted(analysis.glob("clustering/*/clusters.csv")))
    umap = next(iter(sorted(analysis.glob("umap/*/projection.csv"))), None)
    return clusters, umap


def visium_hd_levels(path) -> list[BinLevel]:
    """Every bin level of the run, finest first."""
    found = []
    for level_dir in _hd_level_dirs(path):
        scalefactors = _hd_scalefactors(level_dir) or {}
        spatial = level_dir / VISIUM_SPATIAL
        matrix = level_dir / VISIUM_HD_MATRIX
        if not matrix.is_file():
            matrix = next(iter(sorted(level_dir.glob("*feature_bc_matrix.h5"))),
                          None)
        clusters, umap = _analysis_of(level_dir)
        hires = spatial / "tissue_hires_image.png"
        lowres = spatial / "tissue_lowres_image.png"
        found.append(BinLevel(
            size_um=float(scalefactors.get("bin_size_um") or 0.0),
            root=level_dir, matrix=matrix,
            positions=spatial / VISIUM_HD_POSITIONS,
            scalefactors_path=spatial / VISIUM_SCALEFACTORS,
            scalefactors=scalefactors,
            hires=hires if hires.is_file() else None,
            lowres=lowres if lowres.is_file() else None,
            clusters=clusters, umap=umap))
    return sorted(found, key=lambda level: level.size_um)


def visium_hd_segmentation(path) -> dict | None:
    """Space Ranger 4's `segmented_outputs/`, or None.

    Cell polygons are in the run's FULL-RESOLUTION microscope pixels (the
    same frame as the bin positions), one `Polygon` per cell with an integer
    `properties.cell_id` that the cell matrix spells `cellid_%09d-1`.
    """
    root = visium_hd_root(path)
    if root is None:
        return None
    segmented = root / VISIUM_HD_SEGMENTED
    cells = segmented / VISIUM_HD_CELLS
    if not cells.is_file():
        return None
    nuclei = segmented / VISIUM_HD_NUCLEI
    matrix = segmented / VISIUM_HD_CELL_MATRIX
    scalefactors = segmented / VISIUM_SPATIAL / VISIUM_SCALEFACTORS
    clusters, umap = _analysis_of(segmented)
    return {
        "root": segmented,
        "cells": cells,
        "nuclei": nuclei if nuclei.is_file() else None,
        "matrix": matrix if matrix.is_file() else None,
        "scalefactors": scalefactors if scalefactors.is_file() else None,
        "clusters": clusters,
        "umap": umap,
    }


def fit_similarity(src, dst):
    """The exact similarity (reflection allowed) taking `src` to `dst`.

    Umeyama's least squares, with the reflection left in: Visium HD's grid is
    mirrored against the microscope image, and a fit forced to be a proper
    rotation would put every square on the wrong side of the slide. A
    similarity BY CONSTRUCTION -- uniform scale times an orthogonal matrix --
    so fit noise can never produce the shear or anisotropy the viewer
    refuses (`ngff_transform.TOLERANCE`).

    @returns `(transform, residual_px)` in canvas order, with the RMS of what
             the fit leaves over.
    """
    import numpy as np

    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    mu_s, mu_d = src.mean(0), dst.mean(0)
    xs, xd = src - mu_s, dst - mu_d
    covariance = xd.T @ xs / len(src)
    u, sigma, vt = np.linalg.svd(covariance)
    rotation = u @ vt
    variance = (xs ** 2).sum(1).mean()
    scale = sigma.sum() / variance if variance else 1.0
    matrix = scale * rotation
    shift = mu_d - matrix @ mu_s
    fitted = src @ matrix.T + shift
    residual = float(np.sqrt(((fitted - dst) ** 2).sum(1).mean()))
    transform = (float(matrix[0, 0]), float(matrix[1, 0]), float(matrix[0, 1]),
                 float(matrix[1, 1]), float(shift[0]), float(shift[1]))
    return transform, residual


def visium_hd_grid_shape(level: BinLevel):
    """`(rows, columns)` of a level's grid, out of the positions' footer."""
    bounds = parquet_bounds(level.positions, x="array_col", y="array_row")
    if bounds is None:
        return None
    return int(bounds[1]) + 1, int(bounds[0]) + 1


def visium_hd_bin_transform(level: BinLevel):
    """The level's grid -> FULL-RES pixel similarity, and its fit residual.

    Square `(c, r)` is drawn as the unit square `[c, c+1) x [r, r+1)`, and the
    parquet states square CENTRES -- hence the half-square offset, which is
    what keeps the grid from sitting half a square off the tissue.

    Fit on one record batch of the positions: the transform is global, and
    detection has to stay at tens of milliseconds. On the 11 mm pancreas run
    the batch fit agrees with `feature_slice.h5`'s own
    `spot_colrow_to_microscope_colrow` (which maps centres) to 0.05 px once
    the half square is accounted for, so the instrument's matrix is not read.
    """
    import numpy as np
    import pyarrow.parquet as pq

    handle = pq.ParquetFile(str(level.positions))
    columns = ["array_row", "array_col", "pxl_row_in_fullres",
               "pxl_col_in_fullres"]
    batch = next(handle.iter_batches(batch_size=VISIUM_HD_FIT_ROWS,
                                     columns=columns))
    row = batch.column("array_row").to_numpy().astype(np.float64)
    col = batch.column("array_col").to_numpy().astype(np.float64)
    src = np.column_stack((col + 0.5, row + 0.5))
    dst = np.column_stack((batch.column("pxl_col_in_fullres").to_numpy(),
                           batch.column("pxl_row_in_fullres").to_numpy()))
    return fit_similarity(src, dst)


def read_visium_hd_scene(path) -> list[SceneElement]:
    """A Visium HD run's hires image, its bins and (SR 4) its cell outlines."""
    levels = visium_hd_levels(path)
    if not levels:
        return []
    finest = levels[0]
    found = []
    hires = finest.hires or finest.lowres
    if hires is not None:
        found.append(SceneElement(id="tissue_image", kind="image", path=hires,
                                  modality="he", label="Tissue image"))
    if finest.matrix is not None:
        found.append(SceneElement(id="bins", kind="points", path=finest.matrix,
                                  modality="visium_bins",
                                  label="Visium HD bins"))
    segmentation = visium_hd_segmentation(path)
    if segmentation:
        found.append(SceneElement(id="cell_boundaries", kind="shapes",
                                  path=segmentation["cells"],
                                  modality="cell_boundaries",
                                  label="Cell boundaries"))
        if segmentation["nuclei"]:
            found.append(SceneElement(id="nucleus_boundaries", kind="shapes",
                                      path=segmentation["nuclei"],
                                      modality="nucleus_boundaries",
                                      label="Nucleus boundaries"))
    return found


def is_xenium_transcripts(path) -> bool:
    """Whether this parquet carries a transcript table's columns.

    By its COLUMNS rather than by its name: `transcripts.parquet` is what
    Xenium calls it, but a file copied out of a run directory can be called
    anything, and a file called that from another platform is not this.

    In core rather than in the transcripts plugin because the IMPORTER has to
    answer it -- proposing a Xenium run means saying that this parquet is the
    transcripts -- and a core build may not import a plugin. The plugin's own
    reader re-exports this, so there is still one implementation.
    """
    path = Path(path)
    if path.suffix.lower() != ".parquet" or not path.is_file():
        return False
    try:
        import pyarrow.parquet as pq

        names = set(pq.ParquetFile(str(path)).schema_arrow.names)
    except Exception:
        return False
    return set(TRANSCRIPT_COLUMNS).issubset(names)


def peek_parquet(path):
    """A parquet's row count and columns, out of its footer.

    A few kilobytes rather than the whole file, which is what lets the import
    screen say "8.4 million transcripts, 313 genes" about a 6 GB table without
    reading a row of it. None when pyarrow is not importable -- the row that
    would have shown the count shows the install line instead.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return None
    try:
        handle = pq.ParquetFile(str(path))
    except Exception:
        return None
    names = list(handle.schema_arrow.names)
    return {
        "rows": handle.metadata.num_rows,
        "columns": names,
        "is_transcripts": set(TRANSCRIPT_COLUMNS).issubset(set(names)),
    }


def parquet_bounds(path, x="x_location", y="y_location"):
    """`(max_x, max_y)` from a parquet's own row-group statistics, or None.

    Out of the FOOTER, not the data: every row group records the min and max of
    each column it holds, so the extent of a 50-million-row transcript table
    costs a few kilobytes. That is what lets a transcripts-only sample get a
    blank frame the right size instead of an arbitrary square -- and a frame
    the wrong size is not cosmetic, it is the coordinate system every layer
    registered against it is expressed in.

    None when the file records no statistics (some writers do not), which the
    caller turns into a placeholder rather than a wrong number.
    """
    try:
        import pyarrow.parquet as pq

        handle = pq.ParquetFile(str(path))
    except Exception:
        return None
    names = list(handle.schema_arrow.names)
    if x not in names or y not in names:
        return None
    ix, iy = names.index(x), names.index(y)
    top_x = top_y = None
    try:
        for group in range(handle.metadata.num_row_groups):
            meta = handle.metadata.row_group(group)
            for index, keep in ((ix, "x"), (iy, "y")):
                stats = meta.column(index).statistics
                if stats is None or not stats.has_min_max:
                    return None
                value = float(stats.max)
                if keep == "x":
                    top_x = value if top_x is None else max(top_x, value)
                else:
                    top_y = value if top_y is None else max(top_y, value)
    except Exception:
        return None
    if top_x is None or top_y is None:
        return None
    return top_x, top_y


def looks_like_cells_parquet(path) -> bool:
    """Whether this parquet is a per-cell table rather than per-transcript."""
    peek = peek_parquet(path)
    if not peek:
        return False
    names = set(peek["columns"])
    return "cell_id" in names and {"x_centroid", "y_centroid"}.issubset(names)


def read_spatialdata_scene(path, system="global") -> list[SceneElement]:
    """Every element in a SpatialData store, with its transform into `system`.

    Sorted so the result is stable: two runs over the same store must produce
    the same layer order, or a re-import silently restacks somebody's scene.
    """
    root = Path(path)
    found = []
    for group, kind in SPATIALDATA_KINDS.items():
        directory = root / group
        if not directory.is_dir():
            continue
        for element in sorted(p for p in directory.iterdir() if p.is_dir()):
            attrs = ngff_transform.read_attrs(element)
            found.append(SceneElement(
                id=f"{group}/{element.name}",
                kind=kind,
                path=element,
                to_system=ngff_transform.element_transform(attrs, system),
                label=element.name.replace("_", " "),
                # The element's own name, lowercased. A store's author called
                # it something, and that is a better guess at what it means
                # than anything core could invent -- a plugin matching on
                # `modality` sees `he` for `images/he` and can act on it.
                modality=element.name.lower(),
            ))
    return found


def read_xenium_scene(path) -> list[SceneElement]:
    """Every output of a Xenium run that Plexora can draw.

    No transforms are read: a Xenium run is already in one frame, in microns, so
    what registers the pieces against each other is the morphology image's pixel
    size -- and that belongs to the caller, which is the only thing that knows
    whether this is the project's reference image or a layer beside one.
    """
    root = Path(path)
    found, seen = [], set()
    primary = xenium_image_path(root)
    if primary is not None:
        # Chosen by `xenium_image_path` rather than by the walk below, so the
        # run's own manifest gets a say and the preference order is stated in
        # one place. Everything else is recognised by name.
        seen.add("morphology")
        found.append(SceneElement(
            id="morphology", kind="image", path=primary,
            modality="xenium_morphology", label="morphology"))
    for name, (layer_id, kind, modality) in XENIUM_FILES.items():
        candidate = _xenium_path(root, name)
        # A run can carry both `morphology_focus.ome.tif` and a
        # `morphology_focus/` folder of the same thing. One layer id, first
        # match wins, rather than two cards for one image.
        if candidate is None or layer_id in seen:
            continue
        seen.add(layer_id)
        found.append(SceneElement(
            id=layer_id, kind=kind, path=candidate, modality=modality,
            label=layer_id.replace("_", " ")))
    # Stable order, and images first so `reference_of` picks one.
    order = {"image": 0, "labels": 1, "shapes": 2, "points": 3}
    return sorted(found, key=lambda e: (order.get(e.kind, 9), e.id))


def xenium_tables(path) -> list[tuple[str, Path]]:
    """`(role, path)` for the tables a Xenium run ships.

    Not layers: `cells` becomes the sample's feature table, and `expression` is
    recorded so the import screen can say it is there and not read yet. Kept
    apart from `read_xenium_scene` so nothing that walks layers has to skip
    them.
    """
    root = Path(path)
    return [(role, root / name) for name, role in XENIUM_TABLES.items()
            if (root / name).is_file()]


def visium_scalefactors(path):
    """A Visium run's scale factors, or None.

    `tissue_hires_scalef` is what puts a spot coordinate -- recorded in the
    FULL-resolution slide's pixels -- into the hires picture Space Ranger
    writes, which is the image Plexora draws. Getting it wrong puts every spot
    off by a factor of about six while looking entirely plausible.
    """
    manifest = Path(path) / VISIUM_SPATIAL / VISIUM_SCALEFACTORS
    if not manifest.is_file():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_visium_scene(path) -> list[SceneElement]:
    """A Visium run's hires image and its spots.

    The hires PNG is the reference and the spots are a points layer over it.
    The expression matrix is NOT read here -- see `import_proposal`, which
    records it and says so on the row rather than pretending it was imported.
    """
    root = Path(path)
    spatial = root / VISIUM_SPATIAL
    found = []
    image = next((p for name in ("tissue_hires_image.png", "tissue_lowres_image.png")
                  for p in [spatial / name] if p.is_file()), None)
    if image is not None:
        found.append(SceneElement(
            id="tissue_image", kind="image", path=image, modality="he",
            label="Tissue image"))
    positions = next(iter(sorted(spatial.glob("tissue_positions*.csv"))), None)
    if positions is not None:
        found.append(SceneElement(
            id="spots", kind="points", path=positions, modality="visium_spots",
            label="Spots"))
    return found


def read_scene(path, system="global") -> list[SceneElement]:
    """Whichever kind of store this is, as elements."""
    if is_spatialdata_store(path):
        return read_spatialdata_scene(path, system)
    if is_visium_hd_run(path):
        return read_visium_hd_scene(path)
    if is_xenium_run(path):
        return read_xenium_scene(path)
    return []


def reference_of(elements) -> SceneElement | None:
    """Which element the rest are registered against.

    The first IMAGE, because that is what the viewer draws in the pixel grid of
    and what every stored ROI is already expressed in. A scene with no image at
    all has no reference and every element is unregistered -- which is honest,
    and is the state a pure-transcript sample is genuinely in.
    """
    for element in elements:
        if element.kind == "image":
            return element
    return None


def xenium_pixel_size(path) -> float | None:
    """Microns per pixel, from a Xenium run's own manifest.

    Read rather than assumed. 0.2125 is the published value for the current
    instrument and it is NOT hardcoded here: a run from a different instrument
    or a re-binned export states its own, and a viewer that assumed one would put
    every transcript at the wrong distance from the origin while looking right.
    """
    doc = xenium_manifest(path)
    if not doc:
        return None
    for key in ("pixel_size", "pixel_size_um", "um_per_pixel"):
        value = doc.get(key)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def layers_for(elements, *, reference=None, pixel_size=None) -> list[LayerSpec]:
    """The elements as registered layers, minus the reference itself.

    The reference is excluded because `Project.reference_layer` synthesizes it
    from `ImageSpec` -- storing a second copy under its own id would be a layer
    written and never drawn, which is why `with_layer` refuses the reserved ids.

    Each transform is `inverse(reference -> system)` after `(element -> system)`,
    which is the whole of registration and is why the shared system never has to
    be the one the viewer draws in. An element that does not reach that system
    gets None, and the Layer Manager says "aligned by assumption".

    `pixel_size` covers the Xenium case, where nothing declares a transform and
    the registration IS the conversion from microns to reference pixels.
    """
    reference = reference or reference_of(elements)
    back = None
    if reference is not None and reference.to_system is not None:
        back = ngff_transform.invert(reference.to_system)

    scale = None
    if pixel_size:
        step = 1.0 / float(pixel_size)
        scale = (step, 0.0, 0.0, step, 0.0, 0.0)

    layers = []
    for element in elements:
        if reference is not None and element.id == reference.id:
            continue
        transform = None
        if back is not None and element.to_system is not None:
            transform = ngff_transform.compose(back, element.to_system)
        elif scale is not None:
            transform = scale
        layers.append(LayerSpec(
            id=element.id,
            kind=element.kind,
            label=element.label or element.id,
            src=str(element.path),
            modality=element.modality or None,
            transform=normalize_transform(transform),
            # How this transform was arrived at, so a corrected pixel size can
            # recompute exactly the ones derived from one and leave a
            # store-declared transform alone.
            transform_source=("store" if element.to_system is not None
                              else ("pixel_size" if scale is not None else "assumed")),
            coordinate_system="global" if element.to_system is not None else None,
        ))
    return layers


def register_scene(project, path, *, system="global", pixel_size=None):
    """Add every element of a store to a project as a layer.

    Returns the updated project; the caller saves. Re-registering the same store
    replaces each layer in place rather than appending, because `with_layer`
    keeps a layer's position when it already exists -- so a re-import after a
    corrected pixel size does not restack somebody's scene.
    """
    elements = read_scene(path, system)
    if not elements:
        return project
    if pixel_size is None and is_xenium_run(path):
        pixel_size = xenium_pixel_size(path) or (project.image.pixel_size or {}).get("value")
    for layer in layers_for(elements, pixel_size=pixel_size):
        project = project.with_layer(layer)
    return project
