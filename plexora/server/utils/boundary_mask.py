"""A table of cell boundary polygons, drawn as the label mask the viewer uses.

A Xenium run states its segmentation as `cell_boundaries.parquet`: one row per
polygon VERTEX, in microns, with the cell's numeric `label_id` beside it. That
is the same segmentation an imaging pipeline would hand over as a raster mask
-- the instrument simply ships the outline rather than the fill -- and until
this module existed it was registered as a `shapes` layer that nothing drew.
The sample therefore had no Outlines, no Filled, no colour-by-gating and no
cell picking, on a run whose whole point is that its cells are segmented.

Rasterizing it is what closes that gap, and doing it here rather than in a new
renderer is deliberate: the mask the viewer draws is a tiled pyramidal label
OME-TIFF, every consumer downstream of `SegmentationSpec.derived` already
reads one, and a boundary table converted into one arrives as an ordinary
mask. Nothing after this line knows the pixels came from polygons.

Three properties this has to keep, because each is load-bearing somewhere:

**The label values are the table's `label_id`.** That is the same number
`xenium_cells.normalise_cells_table` writes into the cell table as
`cell_index`, which is what joins a mask pixel to a row -- so gating, phenotype
colouring and clicking a cell all land on the cell that was clicked. A run
whose boundaries carry no `label_id` is refused rather than numbered here:
inventing an order that the cell table does not share would colour every cell
as its neighbour, silently.

**The geometry is the reference image's.** `Project.all_layers` gives the mask
layer the image's own width, height and level count, so a pyramid of any other
size is not a misaligned mask, it is an unreadable one.

**The file is written by `segmentation_pyramid.write_label_pyramid`.** Same
tiles, same SubIFDs, same marker in the OME Name -- so `generated_mask_kind`
recognises it, `is_servable_label_pyramid` serves it untouched, and the
staleness machinery that watches a mask source watches this one too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import numpy as np

from plexora.server.utils import segmentation_pyramid

#: The numeric cell id, and the two coordinate columns. Xenium's names, and
#: not a heuristic: `is_boundary_table` is asked about arbitrary files the
#: user may have pointed at, and guessing which pair of float columns is a
#: polygon would turn somebody's measurement table into a mask.
LABEL_COLUMN = "label_id"
ID_COLUMN = "cell_id"
X_COLUMN = "vertex_x"
Y_COLUMN = "vertex_y"

#: Square tile edge of the pyramid written here. Matches what the Xenium
#: morphology pyramid uses, so a mask tile and an image tile cover the same
#: ground at the same level.
TILE_SIZE = 1024

#: OpenCV fills into a signed 32-bit raster, which is the ceiling on a label
#: this can draw. Ten times more cells than the largest run anybody has
#: shipped, but a table that exceeded it would otherwise wrap into negative
#: ids and paint two cells the same colour.
MAX_LABEL = 2 ** 31 - 1


class Polygons(NamedTuple):
    """Boundary polygons in the reference image's pixel grid.

    Flat arrays rather than a list of arrays: six million vertices is an
    ordinary size for one run, and a list of 250,000 small arrays costs more
    in object headers than the coordinates themselves.
    """

    #: One label per polygon, in the order they appear in the table.
    labels: np.ndarray
    #: `x[offsets[i]:offsets[i + 1]]` is polygon `i`. Length `count + 1`.
    offsets: np.ndarray
    x: np.ndarray
    y: np.ndarray
    #: `(count, 4)` of x0, y0, x1, y1 -- the bounding box used to decide which
    #: polygons a tile has to consider at all.
    bounds: np.ndarray

    @property
    def count(self) -> int:
        return int(self.labels.size)


def is_boundary_table(path) -> bool:
    """Whether `path` is a table of boundary polygons this can rasterize.

    Schema only -- no row is read -- because every mask-attaching path calls
    this to decide which kind of source it is holding, including on loads.
    """
    try:
        candidate = Path(path)
    except TypeError:
        return False
    if candidate.suffix.lower() != ".parquet" or not candidate.is_file():
        return False
    try:
        import polars as pl

        columns = set(pl.scan_parquet(str(candidate)).collect_schema().names())
    except Exception:
        return False
    return {X_COLUMN, Y_COLUMN, LABEL_COLUMN} <= columns


def is_boundary_geojson(path) -> bool:
    """Whether `path` is a GeoJSON of labelled cell polygons.

    Space Ranger 4 states a Visium HD segmentation this way:
    `cell_segmentations.geojson`, a FeatureCollection with one `Polygon` per
    cell and an integer `properties.cell_id`. Schema only -- the first 64 KB
    -- because this is asked on loads, and the file is hundreds of megabytes.
    """
    try:
        candidate = Path(path)
    except TypeError:
        return False
    if candidate.suffix.lower() not in (".geojson", ".json") or not candidate.is_file():
        return False
    try:
        with candidate.open("rb") as handle:
            head = handle.read(65536)
    except OSError:
        return False
    return b"FeatureCollection" in head and b'"cell_id"' in head


def is_boundary_source(path) -> bool:
    """Whether `path` is polygons this can draw: a boundary table or GeoJSON."""
    return is_boundary_table(path) or is_boundary_geojson(path)


def describe(path) -> str:
    """One line about what rasterizing `path` is going to do, for the progress
    panel. Cheap enough to call on the request thread."""
    return f"Drawing {Path(path).stem.replace('_', ' ')} into a label mask"


def read_polygons(path, *, transform=None, pixel_size=None) -> Polygons:
    """Every polygon in `path`, in reference pixels.

    `transform` is the layer's own affine, in the order the client applies it
    (`a, b, c, d, e, f`), and it wins over `pixel_size`: it is the
    registration the importer composed for this run, and the one every other
    layer of the sample is drawn through. Using the pixel size instead would
    put the mask where the image is only for as long as nobody registers the
    run against anything.
    """
    if is_boundary_geojson(path):
        return read_geojson_polygons(path, transform=transform,
                                     pixel_size=pixel_size)

    import polars as pl

    frame = pl.read_parquet(str(path), columns=[X_COLUMN, Y_COLUMN, LABEL_COLUMN])
    if not frame.height:
        raise ValueError(f"{Path(path).name}: the boundary table is empty.")

    raw_labels = frame[LABEL_COLUMN].to_numpy()
    x = frame[X_COLUMN].to_numpy().astype(np.float64, copy=False)
    y = frame[Y_COLUMN].to_numpy().astype(np.float64, copy=False)

    px, py = _to_reference_pixels(x, y, transform=transform,
                                  pixel_size=pixel_size)

    # A polygon is a run of consecutive rows sharing a label -- the layout
    # every vendor writes, and the only one in which vertex ORDER means
    # anything. Found with one diff rather than a group-by, which would
    # reorder the vertices and turn each cell into a bow tie.
    starts = np.concatenate(([0], np.flatnonzero(np.diff(raw_labels)) + 1))
    offsets = np.concatenate((starts, [raw_labels.size])).astype(np.int64)
    labels = raw_labels[starts]

    if labels.min() < 1:
        raise ValueError(
            f"{Path(path).name}: {LABEL_COLUMN} has a value below 1, and zero "
            "is background in every mask Plexora reads.")
    if labels.max() > MAX_LABEL:
        raise ValueError(
            f"{Path(path).name}: {LABEL_COLUMN} reaches {int(labels.max())}, "
            f"past the {MAX_LABEL} a label raster can carry.")

    bounds = np.empty((labels.size, 4), dtype=np.float32)
    bounds[:, 0] = np.minimum.reduceat(px, starts)
    bounds[:, 1] = np.minimum.reduceat(py, starts)
    bounds[:, 2] = np.maximum.reduceat(px, starts)
    bounds[:, 3] = np.maximum.reduceat(py, starts)

    return Polygons(
        labels=labels.astype(np.int64, copy=False),
        offsets=offsets,
        x=px.astype(np.float32, copy=False),
        y=py.astype(np.float32, copy=False),
        bounds=bounds,
    )


_GEOJSON_POLYGON = re.compile(
    rb'"type"\s*:\s*"Polygon"\s*,\s*"coordinates"\s*:\s*\[\s*(\[.*?\])\s*\]'
    rb'\s*\}\s*,\s*"properties"\s*:\s*\{\s*"cell_id"\s*:\s*(\d+)', re.S)


def read_geojson_polygons(path, *, transform=None, pixel_size=None) -> Polygons:
    """Every cell polygon of a GeoJSON, in reference pixels.

    The exterior ring of each `Polygon` (a `MultiPolygon` contributes each of
    its parts under the one label), labelled by `properties.cell_id`.

    By pattern over the raw bytes when the file is laid out the way Space
    Ranger writes it -- geometry first, `cell_id` first among the properties
    -- because `json.load` of 800,000 polygons is gigabytes of Python lists.
    Anything else is parsed in full, correctly and slowly.
    """
    data = Path(path).read_bytes()
    features = data.count(b'"Feature"')
    matches = list(_GEOJSON_POLYGON.finditer(data))
    if matches and len(matches) == features:
        rings = [m.group(1) for m in matches]
        labels = np.fromiter((int(m.group(2)) for m in matches),
                             dtype=np.int64, count=len(matches))
        del matches
        # Only the outer ring: a ring text is `[x, y], [x, y], ...`, and the
        # first `]]` closes it (holes follow as further rings, and a cell
        # mask has none worth drawing).
        # The group opens on the ring's own `[`; dropped, what is left
        # starts at the first vertex.
        rings = [ring.lstrip()[1:].split(b"]]", 1)[0] for ring in rings]
        counts = np.fromiter((ring.count(b"[") for ring in rings),
                             dtype=np.int64, count=len(rings))
        text = b" ".join(rings).translate(bytes.maketrans(b"[],", b"   "))
        del rings
        values = np.array(text.split(), dtype=np.float64)
        del text
        xy = values.reshape(-1, 2) if values.size % 2 == 0 else None
        if xy is None or len(xy) != counts.sum():
            raise ValueError(f"{Path(path).name}: could not read the polygons.")
        x, y = xy[:, 0], xy[:, 1]
    else:
        import json

        doc = json.loads(data)
        label_list, xs, ys, count_list = [], [], [], []
        for feature in doc.get("features") or ():
            props = feature.get("properties") or {}
            geometry = feature.get("geometry") or {}
            if "cell_id" not in props:
                continue
            parts = ([geometry.get("coordinates") or []]
                     if geometry.get("type") == "Polygon"
                     else list(geometry.get("coordinates") or [])
                     if geometry.get("type") == "MultiPolygon" else [])
            for part in parts:
                if not part:
                    continue
                ring = np.asarray(part[0], dtype=np.float64)[:, :2]
                label_list.append(int(props["cell_id"]))
                xs.append(ring[:, 0])
                ys.append(ring[:, 1])
                count_list.append(len(ring))
        if not label_list:
            raise ValueError(f"{Path(path).name}: no labelled polygons.")
        labels = np.asarray(label_list, dtype=np.int64)
        counts = np.asarray(count_list, dtype=np.int64)
        x, y = np.concatenate(xs), np.concatenate(ys)

    if labels.min() < 1:
        raise ValueError(
            f"{Path(path).name}: cell_id has a value below 1, and zero is "
            "background in every mask Plexora reads.")
    if labels.max() > MAX_LABEL:
        raise ValueError(
            f"{Path(path).name}: cell_id reaches {int(labels.max())}, past the "
            f"{MAX_LABEL} a label raster can carry.")
    px, py = _to_reference_pixels(x, y, transform=transform,
                                  pixel_size=pixel_size)
    offsets = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)
    starts = offsets[:-1]
    bounds = np.empty((labels.size, 4), dtype=np.float32)
    bounds[:, 0] = np.minimum.reduceat(px, starts)
    bounds[:, 1] = np.minimum.reduceat(py, starts)
    bounds[:, 2] = np.maximum.reduceat(px, starts)
    bounds[:, 3] = np.maximum.reduceat(py, starts)
    return Polygons(labels=labels, offsets=offsets,
                    x=px.astype(np.float32, copy=False),
                    y=py.astype(np.float32, copy=False), bounds=bounds)


def _to_reference_pixels(x, y, *, transform=None, pixel_size=None):
    """Mirror of the transcripts reader's own conversion, and deliberately the
    same arithmetic: the two layers describe the same cells, and a mask that
    disagreed with the molecules drawn on top of it would be worse than no
    mask at all."""
    if transform is not None and len(tuple(transform)) == 6:
        a, b, c, d, e, f = (float(v) for v in transform)
        return (a * x + c * y + e, b * x + d * y + f)
    if pixel_size:
        step = 1.0 / float(pixel_size)
        return (x * step, y * step)
    raise ValueError(
        "Drawing boundaries needs the registration that puts them in the "
        "image's pixel grid -- a transform or the run's pixel size. Without "
        "one the mask would be drawn at a plausible-looking fraction of its "
        "true size.")


def rasterize(polygons: Polygons, destination, *, width: int, height: int,
              tile_size: int = TILE_SIZE, levels: Optional[int] = None,
              compression: Optional[str] = "zlib",
              max_workers: Optional[int] = None,
              progress_callback: Optional[Callable[[int, int], None]] = None,
              stage_callback: Optional[Callable[..., None]] = None) -> str:
    """Draw `polygons` as a tiled pyramidal label mask `width` x `height`.

    Every level is drawn from the polygons themselves rather than downsampled
    from the one above it. That costs nothing extra -- filling a polygon at
    level 5 is 1024 times less work than at level 0 -- and it is what keeps
    the cheap levels honest: a nearest-neighbour downsample of a filled mask
    picks one pixel in each block and calls it the block's cell, which at the
    zoom levels where cells are a pixel across is a different answer every
    time the grid shifts.
    """
    import cv2

    if width <= 0 or height <= 0:
        raise ValueError("A mask needs a non-zero width and height.")

    order = np.argsort(polygons.bounds[:, 1], kind="stable")
    top_sorted = polygons.bounds[order, 1]
    cache: dict = {}

    def row_candidates(factor, y_start, y_stop):
        """Polygons whose bounding box reaches into this band of tiles.

        Cached per band because `write_label_pyramid` walks a level in
        row-major order: every tile in a row asks the same question of six
        million vertices, and answering it once per row rather than once per
        tile is the difference between seconds and minutes.
        """
        key = (factor, y_start)
        hit = cache.get(key)
        if hit is None:
            cache.clear()
            top = y_start * factor
            bottom = y_stop * factor
            limit = int(np.searchsorted(top_sorted, bottom, side="left"))
            hit = order[:limit]
            hit = hit[polygons.bounds[hit, 3] >= top]
            cache[key] = hit
        return hit

    def block(factor, y_start, y_stop, x_start, x_stop,
              level_height, level_width):
        raster = np.zeros((y_stop - y_start, x_stop - x_start), np.int32)
        candidates = row_candidates(factor, y_start, y_stop)
        if not candidates.size:
            return _as_labels(raster)
        left = x_start * factor
        right = x_stop * factor
        bounds = polygons.bounds
        hit = candidates[(bounds[candidates, 2] >= left)
                         & (bounds[candidates, 0] < right)]
        scale = 1.0 / factor
        for index in hit:
            start, stop = polygons.offsets[index], polygons.offsets[index + 1]
            label = int(polygons.labels[index])
            xs = polygons.x[start:stop] * scale - x_start
            ys = polygons.y[start:stop] * scale - y_start
            box = bounds[index]
            if (box[2] - box[0]) * scale < 1.0 or (box[3] - box[1]) * scale < 1.0:
                # Sub-pixel at this zoom. `fillPoly` rounds every vertex to
                # the same pixel and draws nothing at all, which is how a
                # whole-slide view of a segmented run comes back empty -- so
                # the cell is stamped as the one pixel it has shrunk to.
                row = int(round(float(ys.mean())))
                column = int(round(float(xs.mean())))
                if 0 <= row < raster.shape[0] and 0 <= column < raster.shape[1]:
                    raster[row, column] = label
                continue
            if stop - start < 3:
                continue
            points = np.empty((stop - start, 2), np.int32)
            np.rint(xs, out=xs)
            np.rint(ys, out=ys)
            points[:, 0] = xs
            points[:, 1] = ys
            # One call per polygon rather than one per level-0 pixel: the
            # scanline fill is the whole reason this is OpenCV. `fillPoly`
            # clips to the raster itself, so a cell straddling a tile edge
            # needs no handling here -- and because the vertices are rounded
            # in LEVEL coordinates before the tile origin is subtracted, the
            # two tiles round it identically and the seam is exact.
            cv2.fillPoly(raster, [points], label)
        return _as_labels(raster)

    return segmentation_pyramid.write_label_pyramid(
        destination,
        height=int(height),
        width=int(width),
        dtype=np.uint32,
        block=block,
        tile_size=tile_size,
        compression=compression,
        max_workers=max_workers,
        marker=segmentation_pyramid.FILLED_MARKER,
        min_levels=levels,
        progress_callback=progress_callback,
        stage_callback=stage_callback,
    )


def _as_labels(raster: np.ndarray) -> np.ndarray:
    """A signed fill raster read back as unsigned labels.

    The detour through int32 is OpenCV's: it has no unsigned 32-bit raster.
    Ids are positive and bounded by `MAX_LABEL`, so the two readings of those
    bytes are the same number.
    """
    return raster.view(np.uint32)


def build(table_path, destination, *, width: int, height: int,
          transform=None, pixel_size=None, tile_size: int = TILE_SIZE,
          levels: Optional[int] = None,
          progress_callback=None, stage_callback=None) -> str:
    """Read `table_path` and write its mask to `destination`."""
    if stage_callback is not None:
        stage_callback("loading")
    polygons = read_polygons(table_path, transform=transform,
                             pixel_size=pixel_size)
    return rasterize(polygons, destination, width=width, height=height,
                     tile_size=tile_size, levels=levels,
                     progress_callback=progress_callback,
                     stage_callback=stage_callback)


def resolve_mask(table_path, data_directory=None, *, geometry,
                 progress_callback=None, stage_callback=None) -> str:
    """The mask to serve for `table_path`, drawing one if there is not one yet.

    The counterpart of `data_model.resolve_outline_segmentation`, and it
    adopts an already-derived pyramid on the same terms: beside the source
    first, so a second project importing the same run pays for the drawing
    once.
    """
    location = segmentation_pyramid.resolve_derived_mask(
        table_path, data_directory, mode=segmentation_pyramid.MODE_FILLED)
    if location.existing is not None:
        return str(location.existing)
    return build(
        table_path, location.target,
        width=geometry["width"], height=geometry["height"],
        transform=geometry.get("transform"),
        pixel_size=geometry.get("pixel_size"),
        tile_size=int(geometry.get("tile_size") or TILE_SIZE),
        levels=geometry.get("levels"),
        progress_callback=progress_callback, stage_callback=stage_callback,
    )


def geometry_for(project, table_path) -> dict:
    """Where `table_path`'s polygons land in `project`'s reference frame.

    Resolved from the project every time rather than recorded once, because
    the thing that would have to be recorded is a registration that already
    exists in three places -- and a fourth copy of it is a fourth thing to be
    wrong after somebody corrects a pixel size.

    Order matters. A registered layer reading the same file states the
    transform the importer composed for it. Failing that, a sibling layer of
    the same bundle does: everything a Xenium run ships is in one frame, so
    the transcripts' registration is the boundaries' registration. The image's
    pixel size is the last resort, and no answer at all is an error rather
    than an identity transform -- a mask drawn at 1 px per micron is not
    approximately right, it is a fifth of the slide in the corner.
    """
    image = project.image
    if not image.width or not image.height:
        raise ValueError(
            f"{project.name}: the reference image has no geometry yet, so "
            "there is nothing to draw a mask against.")
    geometry = {"width": int(image.width), "height": int(image.height),
                "tile_size": int(image.tile_width or TILE_SIZE),
                # The mask layer is served at the IMAGE's level count, so the
                # mask has to HAVE that many levels -- see `pyramid_factors`.
                "levels": int(image.max_level or 0) or None}

    source = Path(table_path)
    # The mask's own registration, recorded with it, first. A Visium HD run's
    # cell polygons are in the microscope's full-res pixels while the bins
    # beside them are in grid squares -- the sibling rule below would draw
    # every cell seven times too small.
    segmentation = project.segmentation
    if (segmentation.transform and segmentation.source
            and Path(segmentation.source) == source):
        geometry["transform"] = tuple(segmentation.transform)
        return geometry
    layers = [l for l in project.spatial_layers if l.transform]
    same_file = next((l for l in layers if l.src and Path(l.src) == source), None)
    if same_file is not None:
        geometry["transform"] = tuple(same_file.transform)
        return geometry

    root = str(source.parent)
    sibling = next((l for l in layers
                    if str((l.source or {}).get("root") or "") == root), None)
    if sibling is not None:
        geometry["transform"] = tuple(sibling.transform)
        return geometry

    size = (image.pixel_size or {}).get("value")
    if size:
        geometry["pixel_size"] = float(size)
        return geometry

    raise ValueError(
        f"{source.name}: nothing in this sample says how its coordinates map "
        "onto the image, so the boundaries cannot be drawn. They are in "
        "microns and the image is in pixels; importing the run directory "
        "supplies the conversion, and so does setting the image's pixel size "
        "on the edit page and reattaching the mask.")


__all__ = [
    "ID_COLUMN",
    "LABEL_COLUMN",
    "MAX_LABEL",
    "TILE_SIZE",
    "X_COLUMN",
    "Y_COLUMN",
    "Polygons",
    "build",
    "describe",
    "geometry_for",
    "is_boundary_geojson",
    "is_boundary_source",
    "is_boundary_table",
    "read_geojson_polygons",
    "rasterize",
    "read_polygons",
    "resolve_mask",
]
