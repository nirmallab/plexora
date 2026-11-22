"""Where a project's cells are, in units another application can measure in.

A hand-off FROM Plexora (agent/bridge_provider.py `table_for`) names the
table the other side opens; this says which of its columns hold a cell's
position and how to read them in micrometres, so distances there are the
tissue's -- without Plexora writing a column into the user's table.

`xy_fingerprint` is the other question about where cells are: whether they
are still where a stored result read them, for the keys of results built on
positions (the Single Cell Classifier's neighbour features).
"""

from __future__ import annotations


def coordinates_for(record) -> dict:
    """What a hand-off FROM Plexora says about where cells are, so the other
    side measures distance in µm without Plexora writing into the table:
    `roles` (which columns hold the id, x, y and -- for 3-D objects or a time
    series -- z and t), and `coordinates`: the table's own µm columns when it
    has them (an object table measured in µm), else the scale from its pixel
    columns to µm (`um_per_unit`, per axis, with where each step came from).
    An axis whose step is unknown is left out and named in `missing`, never
    given a default."""
    from plexora.server.utils import pixel_scale

    roles = {key: record.roles.get(key) for key in
             ("cell_id", "x", "y", "z", "t", "image_id", "celltype") if record.roles.get(key)}
    columns = set(getattr(record.dataset.columns, "metadata", ()) or ())
    z_role = roles.get("z")
    if {"x_um", "y_um"} <= columns and (not z_role or "z_um" in columns):
        coordinates = {"units": "µm", "columns": {"x": "x_um", "y": "y_um",
                                                  **({"z": "z_um"} if z_role else {})}}
        return {"roles": roles, "coordinates": coordinates}
    try:
        steps = pixel_scale.spacing(record)
    except Exception:
        steps = {"x": None, "y": None, "z": None}
    scale, sources, missing = {}, {}, []
    for axis in ("x", "y") + (("z",) if z_role else ()):
        step = steps.get(axis)
        if step is None:
            missing.append("pixel_size" if axis != "z" else "spacing:z")
            continue
        scale[axis] = float(step["value"])
        sources[axis] = step.get("source")
    coordinates = {"units": "pixels", "columns": {axis: roles[axis] for axis in scale},
                   "um_per_unit": scale, "sources": sources}
    if missing:
        coordinates["missing"] = sorted(set(missing))
    return {"roles": roles, "coordinates": coordinates}


def xy_fingerprint(frame, x, y) -> str | None:
    """A short digest of every cell's x and y as `frame` gives them in columns
    `x` and `y`: the row count and the two sums, rounded to a pixel. None
    without a frame or either column.

    For the keys of results that read where cells are -- a neighbour feature,
    a centroid mapped onto a mask, a calibration anchored on cells -- because
    those keys otherwise cover the expression matrix and the ids only, and a
    project whose coordinates were read in the wrong space and then put right
    keeps the same values and the same ids. One cell moved by a pixel or more
    changes it; a missing value counts as nothing rather than making the
    digest unavailable. The same shape as `agent/evidence/calibration.py`'s
    own digest, so that one can be this."""
    import hashlib

    import polars as pl

    try:
        if frame is None or not x or not y or x not in frame.columns or y not in frame.columns:
            return None
        sums = [frame.height]
        for name in (x, y):
            column = frame[name].cast(pl.Float64)
            sums.append(round(float(column.fill_nan(None).sum() or 0.0)))
    except Exception:
        return None
    return hashlib.sha1(repr(tuple(sums)).encode("utf-8")).hexdigest()[:10]
