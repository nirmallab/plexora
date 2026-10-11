"""Where a project's cells are, in units another application can measure in.

A hand-off FROM Plexora (agent/bridge_provider.py `table_for`) names the
table the other side opens; this says which of its columns hold a cell's
position and how to read them in micrometres, so distances there are the
tissue's -- without Plexora writing a column into the user's table.
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
