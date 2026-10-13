"""One closed surface per object of a 3-D label volume.

The mask stays the truth: each surface is extracted from its object's voxels
and nothing done to a surface ever writes back into the mask. The object's id
IS its surface's id, which is the table's `cell_id` (`objects.py` attaches
the object table with `cell_id = label`), so picking, colour-by and the
table's rows line up with no mapping.

Every object is extracted in ONE pass over the volume (`nets.py`, multi-label
surface nets): the level is read once, in chunk-aligned blocks on a pool of
threads, and each cube between voxel centres puts one vertex for each label at
its corners. Neighbours never merge, a cell cut by the edge of the volume is
capped on its outer voxel faces, and the cost is the volume's, not the number
of cells times anything. A data node runs the same function on its own files,
and the mesh is the same bytes whatever the blocks or threads.

The voxel staircase is taken out by smoothing the mesh (`Smoothing`, Taubin,
each vertex kept within half a grid voxel of where the net put it), and the
mesh is pulled in along its normals by `inset` voxels (0.1 by default),
because two cells that touch share voxel faces and would otherwise draw
through each other.

At a level whose pixel is wider than the z step, planes are pooled in runs
(`levels.label_z_stride`): each pixel of a run takes the label on most of its
planes, the earliest on a tie, so an object one plane thick is kept.
"""

from __future__ import annotations

import time

import numpy as np

from plexora.server.surface import isosurface as iso_mod
from plexora.server.surface import marching
from plexora.server.surface import mesh as mesh_mod
from plexora.server.surface import nets

#: Inset applied to label surfaces unless asked otherwise, in level-0 voxels.
DEFAULT_INSET = 0.1
#: Planes of the coarsest level read at once when counting objects.
COUNT_PLANES = 16
#: Below this largest id the counts are a `bincount`; above, a sort.
BINCOUNT_LIMIT = 1 << 22


def label_counts(label_volume, *, ids=None, progress=None, cancelled=None):
    """`(ids, counts)` of the objects in the label volume, counted on its
    COARSEST level (every plane, none pooled) and scaled to level-0 voxels by
    the level's XY factor: an estimate for planning, read in a fraction of
    the time a full level takes. An id asked for and not seen there (an
    object smaller than a coarse pixel) is counted as one coarse pixel, and
    the build reports whether it was found."""
    shapes = label_volume.level_shapes()
    level = len(shapes) - 1
    height, width = (int(v) for v in shapes[level])
    factor = (label_volume.width / max(1, width)) * (label_volume.height / max(1, height))
    depth = int(label_volume.depth)
    found_ids, found_counts = [], []
    steps = max(1, -(-depth // COUNT_PLANES))
    for step, z0 in enumerate(range(0, depth, COUNT_PLANES)):
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")
        block = np.asarray(label_volume.read(level, z0, min(depth, z0 + COUNT_PLANES) - 1,
                                             slice(None), slice(None))).ravel()
        top = int(block.max()) if block.size else 0
        if top:
            if top < BINCOUNT_LIMIT:
                counted = np.bincount(block, minlength=top + 1)
                present = np.nonzero(counted)[0]
                found_ids.append(present.astype(np.int64))
                found_counts.append(counted[present].astype(np.int64))
            else:
                present, counted = np.unique(block, return_counts=True)
                found_ids.append(present.astype(np.int64))
                found_counts.append(counted.astype(np.int64))
        if progress is not None:
            progress(step + 1, steps)
    if found_ids:
        every = np.concatenate(found_ids)
        unique, inverse = np.unique(every, return_inverse=True)
        counts = np.bincount(inverse.ravel(), weights=np.concatenate(found_counts))
    else:
        unique, counts = np.zeros(0, np.int64), np.zeros(0)
    keep = unique > 0
    unique, counts = unique[keep], counts[keep] * factor
    if ids is not None:
        wanted = np.unique(np.asarray(list(ids), dtype=np.int64))
        seen = np.isin(wanted, unique)
        pick = np.isin(unique, wanted)
        unique = np.concatenate([unique[pick], wanted[~seen]])
        counts = np.concatenate([counts[pick], np.full(int((~seen).sum()), factor)])
        order = np.argsort(unique, kind="stable")
        unique, counts = unique[order], counts[order]
    return unique.astype(np.int64), np.round(counts).astype(np.int64)


def regions_from_table(frame, *, factor_xy=(1.0, 1.0), z_stride=1, shape=None):
    """Grid boxes `(z0, z1, y0, y1, x0, x1)`, inclusive, of the objects of an
    object table (level-0 boxes) at a level with XY factor `factor_xy`
    pooling `z_stride` planes, with a voxel of slack for the rounding: the
    blocks an extraction needs to read."""
    fx, fy = (float(v) for v in factor_xy)
    stride = max(1, int(z_stride))
    out = np.stack([
        np.asarray(frame["bbox_z0"]) // stride - 1, np.asarray(frame["bbox_z1"]) // stride + 1,
        np.floor(np.asarray(frame["bbox_y0"]) / fy) - 1,
        np.ceil(np.asarray(frame["bbox_y1"]) / fy) + 1,
        np.floor(np.asarray(frame["bbox_x0"]) / fx) - 1,
        np.ceil(np.asarray(frame["bbox_x1"]) / fx) + 1], axis=1).astype(np.int64)
    if shape is not None:
        depth, height, width = shape
        out[:, 0:2] = np.clip(out[:, 0:2], 0, depth - 1)
        out[:, 2:4] = np.clip(out[:, 2:4], 0, height - 1)
        out[:, 4:6] = np.clip(out[:, 4:6], 0, width - 1)
    return out


def extract_labels(label_volume, level, grid: marching.Grid, *, ids=None, regions=None,
                   scale=(1.0, 1.0, 1.0), smoothing: mesh_mod.Smoothing | None = None,
                   inset=DEFAULT_INSET, keep_raw=True, progress=None, stage=None,
                   cancelled=None, z_stride=1, threads=None, tile=None,
                   block_voxels=nets.BLOCK_VOXELS):
    """Surfaces of every object of `label_volume` at `level` (only `ids`
    when given), in one pass.

    `grid` maps the level's voxels onto level-0 voxels (`Grid.of` with the
    level's XY factor and the same `z_stride`); `scale` is one level-0
    voxel's physical size. `regions` (grid boxes, `regions_from_table`)
    limits the blocks read to those that hold an object. `stage("smoothing")`
    is entered after the extraction when `stage` is given. Returns `(Mesh,
    report)`."""
    started = time.perf_counter()
    volume = nets.Volume(label_volume, level, grid.shape, z_stride=z_stride, ids=ids)
    pieces, engine = nets.surface_pieces(
        volume, regions=regions, tile=tile or _tile_of(label_volume, level),
        block_voxels=block_voxels, threads=threads, progress=progress, cancelled=cancelled)
    depth, height, width = volume.shape
    positions, triangles, object_id = nets.assemble(pieces, (depth + 1, height + 1, width + 1))
    surface = mesh_mod.Mesh(positions=grid.to_level0(positions),
                            triangles=triangles.astype(np.uint32), object_id=object_id)
    surface = mesh_mod.sort_by_object(surface)
    found = surface.ids()
    missing = [] if ids is None else \
        sorted(int(v) for v in np.setdiff1d(np.asarray(list(ids), dtype=np.int64),
                                            found.astype(np.int64)))
    engine.update(algorithm="surface_nets_multilabel",
                  seconds_extract=round(time.perf_counter() - started, 3))
    report = {"objects": surface.n_objects, "missing": missing[:50],
              "missing_count": len(missing), "engine": engine}
    if cancelled is not None and cancelled():
        raise InterruptedError("cancelled")
    if stage is not None and smoothing is not None and smoothing.iterations > 0:
        stage("smoothing")
    smoothed = time.perf_counter()
    surface = iso_mod.finish(surface, smoothing, scale=scale, voxel=grid.scale,
                             keep_raw=keep_raw, report=report, inset=inset, progress=progress)
    engine["seconds_finish"] = round(time.perf_counter() - smoothed, 3)
    return surface, report


def _tile_of(label_volume, level):
    """The tile a level is read in, `(rows, columns)`: its chunk when the
    store says, else the label store's chunk (`volume/labels.CHUNK`)."""
    arrays = getattr(label_volume, "_arrays", None)
    try:
        chunks = arrays[level].chunks
        return max(64, int(chunks[1])), max(64, int(chunks[2]))
    except Exception:
        return nets.TILE, nets.TILE

