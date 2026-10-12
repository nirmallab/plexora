"""One closed surface per object of a 3-D label volume.

The mask stays the truth: each surface is extracted from its object's voxels
and nothing done to a surface ever writes back into the mask. The object's id
IS its surface's id, which is the table's `cell_id` (`objects.py` attaches
the object table with `cell_id = label`), so picking, colour-by and the
table's rows line up with no mapping.

Each object is marched on its own, from a crop of its bounding box with one
voxel of margin, at level 0.5 of its binary mask: neighbours never merge, a
cell cut by the edge of the volume is capped on its outer voxel faces, and
the per-object loop is about a millisecond an object. Crops are read in
groups -- the objects whose boxes start in one tile of the plane are read as
one block -- so a mask stored in large chunks is not decoded once per cell.

Voxel staircases are taken out in two steps. Each object's binary crop is
first blurred with a Gaussian `blur` voxels wide along each axis -- each
axis's OWN voxel, so a plane step and a pixel step are rounded alike -- and
marched at 0.5: on an anisotropic stack this is what removes the terraces
between planes, which a smoothing constrained to half a voxel cannot. The mesh is then smoothed
(`Smoothing`, Taubin, each vertex kept within half a voxel of where marching
put it), and pulled in along its normals by `inset` voxels (0.1 by default),
because two cells that touch share voxel faces and would otherwise draw
through each other.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

import numpy as np

from plexora.server.surface import isosurface as iso_mod
from plexora.server.surface import marching
from plexora.server.surface import mesh as mesh_mod

#: Objects whose boxes start in one TILE x TILE square of the plane are read
#: as one block.
TILE = 256
#: Inset applied to label surfaces unless asked otherwise, in level-0 voxels.
DEFAULT_INSET = 0.1
#: The mask's anti-aliasing blur: a Gaussian this many voxels wide along each
#: axis (in that axis's own voxels).
DEFAULT_BLUR = 0.6


def object_boxes(label_volume, level=0, *, ids=None, progress=None, cancelled=None,
                 rows_per_read=1024):
    """`(ids, boxes, counts)` of every object present at `level`: boxes are
    `(z0, z1, y0, y1, x0, x1)` inclusive in that level's voxels, counts its
    voxels there. `ids` keeps only those objects. One pass, plane by plane."""
    from plexora.server.volume.measure import _bbox_kernel

    depth = int(label_volume.depth)
    height, width = (int(v) for v in label_volume.level_shapes()[level])
    size = 1
    big = np.iinfo(np.int64).max
    boxes = [np.full(size, big, np.int64), np.full(size, -1, np.int64),
             np.full(size, big, np.int64), np.full(size, -1, np.int64),
             np.full(size, big, np.int64), np.full(size, -1, np.int64)]
    counts = np.zeros(size, np.int64)
    total = depth * max(1, -(-height // rows_per_read))
    done = 0
    for z in range(depth):
        for y0 in range(0, height, rows_per_read):
            if cancelled is not None and cancelled():
                raise InterruptedError("cancelled")
            plane = label_volume.read(level, z, z, slice(y0, min(height, y0 + rows_per_read)),
                                      slice(None))[0]
            top = int(plane.max()) if plane.size else 0
            if top >= size:
                grow = top + 1 - size
                boxes = [np.concatenate([b, np.full(grow, big if i % 2 == 0 else -1, np.int64)])
                         for i, b in enumerate(boxes)]
                counts = np.concatenate([counts, np.zeros(grow, np.int64)])
                size = top + 1
            if top:
                counts += np.bincount(plane.ravel(), minlength=size)[:size].astype(np.int64)
                _bbox_kernel(plane.astype(np.uint32), y0, z, boxes[4], boxes[5], boxes[2],
                             boxes[3], boxes[0], boxes[1])
            done += 1
            if progress is not None:
                progress(done, total)
    present = np.nonzero(counts)[0]
    present = present[present > 0]
    if ids is not None:
        present = present[np.isin(present, np.asarray(list(ids), dtype=np.int64))]
    stacked = np.stack([b[present] for b in boxes], axis=1) if len(present) \
        else np.zeros((0, 6), np.int64)
    return present.astype(np.int64), stacked, counts[present]


def boxes_from_table(frame, *, level_factor=(1.0, 1.0), dims=None, ids=None):
    """`object_boxes`' answer from an object table (`objects_v1.parquet`):
    level-0 boxes scaled to a level with XY factor `level_factor`, one voxel
    of slack for the rounding."""
    labels = np.asarray(frame["label"], dtype=np.int64)
    keep = np.ones(len(labels), bool) if ids is None \
        else np.isin(labels, np.asarray(list(ids), dtype=np.int64))
    fx, fy = level_factor
    out = np.stack([
        np.asarray(frame["bbox_z0"])[keep], np.asarray(frame["bbox_z1"])[keep],
        np.floor(np.asarray(frame["bbox_y0"])[keep] / fy) - 1,
        np.ceil(np.asarray(frame["bbox_y1"])[keep] / fy) + 1,
        np.floor(np.asarray(frame["bbox_x0"])[keep] / fx) - 1,
        np.ceil(np.asarray(frame["bbox_x1"])[keep] / fx) + 1], axis=1).astype(np.int64)
    if dims is not None:
        depth, height, width = dims
        out[:, 0:2] = np.clip(out[:, 0:2], 0, depth - 1)
        out[:, 2:4] = np.clip(out[:, 2:4], 0, height - 1)
        out[:, 4:6] = np.clip(out[:, 4:6], 0, width - 1)
    counts = np.asarray(frame["volume_vox"])[keep] if "volume_vox" in frame.columns else None
    return labels[keep], out, counts


def _groups(boxes):
    groups = defaultdict(list)
    for index, box in enumerate(boxes):
        groups[(int(box[2]) // TILE, int(box[4]) // TILE)].append(index)
    return list(groups.values())


def blur_sigmas(scale, grid_scale, blur=DEFAULT_BLUR):
    """Per-axis (z, y, x) sigma in grid voxels: `blur` of each axis's own voxel."""
    return (float(blur),) * 3 if blur and float(blur) > 0 else None


def extract(label_volume, level, grid: marching.Grid, ids, boxes, *, scale=(1.0, 1.0, 1.0),
            smoothing: mesh_mod.Smoothing | None = None, inset=DEFAULT_INSET,
            blur=DEFAULT_BLUR, keep_raw=True, progress=None, cancelled=None, z_stride=1):
    """Surfaces of the objects `ids` (boxes from `object_boxes`) at `level`.

    `grid` maps the level's voxels onto level-0 voxels (`Grid.of` with the
    level's XY factor, origin 0 and the same `z_stride`); `scale` is one
    level-0 voxel's physical size. With `z_stride` above 1 every run of that
    many planes (aligned to plane 0) is pooled into one grid plane -- a voxel
    is the cell's when any plane of its run is, so nothing thinner than the
    run vanishes. Returns `(Mesh, report)`."""
    from scipy import ndimage

    depth = int(label_volume.depth)
    height, width = (int(v) for v in label_volume.level_shapes()[level])
    stride = max(1, int(z_stride))
    sigmas = blur_sigmas(scale, grid.scale, blur)
    # Margin around each box: one voxel, and room for the blur to fall off --
    # in grid voxels, so `stride` planes per grid plane along z.
    mz, my, mx = ((1 + int(np.ceil(3 * s)) for s in sigmas) if sigmas else (1, 1, 1))
    mz *= stride
    pieces = []
    missing = []
    total = len(ids)
    done = 0
    for group in _groups(boxes):
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")
        sel = boxes[group]
        z0 = max(0, int(sel[:, 0].min()) - mz)
        z1 = min(depth - 1, int(sel[:, 1].max()) + mz)
        if stride > 1:
            # Whole runs, so every object pools the same planes together.
            z0 = (z0 // stride) * stride
            z1 = min(depth - 1, (z1 // stride + 1) * stride - 1)
        y0 = max(0, int(sel[:, 2].min()) - my)
        y1 = min(height - 1, int(sel[:, 3].max()) + my)
        x0 = max(0, int(sel[:, 4].min()) - mx)
        x1 = min(width - 1, int(sel[:, 5].max()) + mx)
        block = np.asarray(label_volume.read(level, z0, z1, slice(y0, y1 + 1), slice(x0, x1 + 1)))
        for index in group:
            label = int(ids[index])
            bz0, bz1, by0, by1, bx0, bx1 = (int(v) for v in boxes[index])
            lz0, lz1 = max(0, bz0 - mz - z0), min(block.shape[0] - 1, bz1 + mz - z0)
            ly0, ly1 = max(0, by0 - my - y0), min(block.shape[1] - 1, by1 + my - y0)
            lx0, lx1 = max(0, bx0 - mx - x0), min(block.shape[2] - 1, bx1 + mx - x0)
            if stride > 1:
                lz0 = (lz0 // stride) * stride
                lz1 = min(block.shape[0] - 1, (lz1 // stride + 1) * stride - 1)
            crop = block[lz0:lz1 + 1, ly0:ly1 + 1, lx0:lx1 + 1] == label
            if stride > 1:
                # The last run may be short (the volume's end): it pools the
                # planes there are.
                short = (-crop.shape[0]) % stride
                if short:
                    crop = np.concatenate(
                        [crop, np.zeros((short,) + crop.shape[1:], dtype=bool)])
                crop = crop.reshape((-1, stride) + crop.shape[1:]).any(axis=1)
            crop = crop.astype(np.float32)
            if sigmas:
                # `nearest`: inside the volume the crop's margin is background
                # already; at the volume's edge a cut cell is continued past it,
                # so the blur does not eat its cap (the padding below caps it).
                crop = ndimage.gaussian_filter(crop, sigmas, mode="nearest")
            done += 1
            if progress is not None:
                progress(done, total)
            if not crop.any():
                missing.append(label)
                continue
            try:
                positions, triangles = marching.march(
                    lambda a, b, crop=crop: crop[a:b + 1], crop.shape, 0.5,
                    strip=max(2, crop.shape[0]), close=True, pad_value=0.0)
            except marching.NoSurface:
                missing.append(label)
                continue
            positions = positions + np.asarray([x0 + lx0, y0 + ly0, (z0 + lz0) // stride],
                                               np.float32)
            pieces.append(mesh_mod.Mesh(positions=positions, triangles=triangles,
                                        object_id=np.full(len(positions), label, np.uint32)))
    surface = mesh_mod.concat(pieces)
    surface = replace(surface, positions=grid.to_level0(surface.positions))
    surface = mesh_mod.sort_by_object(surface)
    report = {"objects": surface.n_objects, "missing": missing[:50],
              "missing_count": len(missing),
              "mask_blur": {"fraction_of_voxel": blur, "sigma_vox_zyx": list(sigmas)}
              if sigmas else None}
    surface = iso_mod.finish(surface, smoothing, scale=scale, voxel=grid.scale,
                             keep_raw=keep_raw, report=report, inset=inset)
    return surface, report
