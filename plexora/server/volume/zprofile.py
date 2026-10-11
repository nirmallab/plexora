"""A cell's marker plane by plane: where in depth its signal sits.

A per-cell mean folds every plane of the cell into one number, which is what a
gate is set on and what it should be. What the number cannot say is where the
signal came from: a membrane marker brightest on the cell's own middle planes,
or on its top plane only -- where a neighbour above it touches -- or carrying on
past the cell's last plane into the tissue under it. Those are the questions
behind a borderline call in 3-D, and the profile answers them with numbers:

- per plane, the mean inside the cell's own voxels on that plane, and the mean
  over its footprint (the union of its outline over all its planes) -- the
  latter also on the plane just before its first and just after its last,
  where it has no voxels;
- the peak plane, and whether it is one of the cell's end planes;
- the width at half height in planes (and microns when the z step is known);
- `beyond`: the footprint mean on the plane just outside each end (`before`
  its first plane, `after` its last) over the cell's own mean -- near or
  above one says the signal does not stop where the cell does.

Full resolution, one cell's box at a time: a few hundred thousand voxels.
"""

from __future__ import annotations

import numpy as np

#: The most voxels one cell's profile reads (box x planes, the planes outside
#: it included). Far above a cell; it stops a mislabelled background object.
MAX_VOXELS = 16_000_000


class ProfileTooLarge(ValueError):
    pass


def _mean(values):
    return float(values.mean()) if values.size else None


def object_profile(labels, read_block, row, channels, *, depth, z_um=None, halo=1):
    """One cell's profile for each of `channels`.

    `labels` is a `LabelVolume` frame; `read_block(channel_index, z0, z1, rows,
    cols)` reads `(Z, H, W)` image voxels at level 0; `row` is the cell's
    object-table row (`label`, `bbox_*`); `channels` is `[(index, name)]`.
    """
    label = int(row["label"])
    z0, z1 = int(row["bbox_z0"]), int(row["bbox_z1"])
    rows = slice(int(row["bbox_y0"]), int(row["bbox_y1"]) + 1)
    cols = slice(int(row["bbox_x0"]), int(row["bbox_x1"]) + 1)
    lo, hi = max(0, z0 - halo), min(depth - 1, z1 + halo)
    voxels = (hi - lo + 1) * (rows.stop - rows.start) * (cols.stop - cols.start)
    if voxels > MAX_VOXELS:
        raise ProfileTooLarge(f"cell {label}'s box through its planes is {voxels:,} voxels, "
                              f"more than one profile reads ({MAX_VOXELS:,})")
    mask = labels.read(0, lo, hi, rows, cols) == label
    footprint = mask.any(axis=0)
    out = {"object_id": label, "planes": [z0, z1], "read_planes": [lo, hi], "channels": {}}
    for index, name in channels:
        block = np.asarray(read_block(index, lo, hi, rows, cols), dtype=np.float64)
        planes = []
        for k, z in enumerate(range(lo, hi + 1)):
            inside = mask[k]
            planes.append({"z": z, "voxels": int(inside.sum()),
                           "mean": _mean(block[k][inside]),
                           "footprint_mean": _mean(block[k][footprint])})
        out["channels"][name] = _summarise(planes, mask, block, z0, z1, z_um)
    return out


def _summarise(planes, mask, block, z0, z1, z_um):
    own = [p for p in planes if z0 <= p["z"] <= z1 and p["mean"] is not None]
    whole = _mean(block[mask])
    summary = {"profile": planes, "mean": whole}
    if not own:
        return summary
    means = np.asarray([p["mean"] for p in own])
    peak = int(np.argmax(means))
    top, floor = float(means[peak]), float(means.min())
    half = floor + (top - floor) / 2
    # Contiguous planes around the peak at or above half height.
    a = b = peak
    while a > 0 and means[a - 1] >= half:
        a -= 1
    while b < len(means) - 1 and means[b + 1] >= half:
        b += 1
    width = b - a + 1 if top > floor else len(means)
    summary.update({
        "peak_z": own[peak]["z"],
        "peak_at_end_plane": len(own) > 1 and own[peak]["z"] in (z0, z1),
        "peak_over_floor": round(top / floor, 3) if floor > 0 else None,
        "fwhm_planes": int(width),
        "fwhm_um": round(width * z_um, 3) if z_um else None,
    })
    beyond = {}
    for side, z in (("before", z0 - 1), ("after", z1 + 1)):
        plane = next((p for p in planes if p["z"] == z), None)
        if plane is not None and plane["footprint_mean"] is not None and whole:
            beyond[side] = round(plane["footprint_mean"] / whole, 3)
    summary["beyond"] = beyond
    return summary
