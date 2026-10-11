"""One cell, explained: every marker with its percentile, where it is, who is near.

What a person asks when a gate calls a cell they doubt: how bright is it in
everything, not only this marker; is it in the tumour region; what are its
neighbours; and what does it look like. The answer is numbers from the table,
region membership from the stored polygons, neighbours by centroid distance,
and a crop with the cell outlined and its neighbours labelled.
"""

from __future__ import annotations

import numpy as np

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_LIST

#: Neighbours reported, at most.
MAX_NEIGHBOURS = 32

#: Markers reported as the cell's strongest, by percentile.
TOP_MARKERS = 5

#: The widest default crop, in full-resolution pixels.
MAX_CROP_PX = 1000.0


def explain_cell(session, data, *, cell_id, marker=None, k=8, within_um=None, crop_um=None,
                 crop_px=None, include_crop=True, include_metadata=False, store=True):
    from plexora.agent import render
    from plexora.agent.cell_gallery import (NEIGHBOUR_OUTLINE, TARGET_OUTLINE, crop_for,
                                            table_cells)
    from plexora.server.utils import pixel_scale

    record = data.project
    ids, xs, ys, keep = table_cells(data)
    id_column = data.schema.cell_id if data.schema else None
    # Compared as the uint32 ids `cell_ids` made, after a range check: an id
    # column may be float or string in the table itself.
    if not 0 <= int(cell_id) <= np.iinfo(np.uint32).max:
        raise AgentError("invalid_input", f"{cell_id} is not a cell id",
                         detail={"id_column": id_column})
    where = np.flatnonzero(ids == np.uint32(cell_id))
    if not where.size:
        raise AgentError("invalid_input", f"no cell {cell_id} in {record.name!r}",
                         detail={"id_column": id_column, "n_cells": int(ids.size)})
    index = int(where[0])
    x, y = float(xs[index]), float(ys[index])

    markers = list(data.table.markers)
    columns = data.table.columns(markers) if markers else {}
    values = {}
    for name in markers:
        column = np.asarray(columns[name], dtype=np.float64)[keep]
        value = float(column[index])
        if not np.isfinite(value):
            values[name] = {"value": None, "percentile": None}
            continue
        finite = column[np.isfinite(column)]
        # O(n): a count, not a sort.
        values[name] = {"value": value,
                        "percentile": float((finite <= value).sum() / max(1, finite.size)
                                            * 100)}
    ranked = sorted((name for name in markers if values[name]["percentile"] is not None),
                    key=lambda name: (-values[name]["percentile"], name))

    metadata = {}
    schema = data.schema
    for role in ("celltype", "image_id"):
        column = getattr(schema, role, None) if schema else None
        if column:
            try:
                got = data.table.columns([column])[column]
                metadata[column] = np.asarray(got, dtype=object)[keep][index]
            except Exception:
                pass
    if include_metadata:
        from plexora.api.dataset import TableHandle

        for column in TableHandle(record).metadata_columns[:MAX_LIST]:
            if column in metadata:
                continue
            try:
                got = data.table.columns([column])[column]
                metadata[column] = np.asarray(got, dtype=object)[keep][index]
            except Exception:
                continue
    metadata = {key: (value.item() if hasattr(value, "item") else value)
                for key, value in metadata.items()}

    pixel = pixel_scale.pixel_size(record)
    scale = pixel["value"] if pixel else None
    distances = np.hypot(xs - x, ys - y)
    order = np.argsort(distances, kind="stable")
    order = order[order != index]
    if within_um is not None:
        if scale is None:
            raise AgentError("precondition_missing",
                             "within_um needs a calibrated image, and this one states no "
                             "pixel size",
                             detail={"missing": [{"key": "pixel_size", "label": "Pixel size"}]})
        order = order[distances[order] * scale <= within_um]
    k = int(max(0, min(k, MAX_NEIGHBOURS)))
    neighbours = []
    for j in order[:k].tolist():
        entry = {"cell_id": int(ids[j]),
                 "distance": float(distances[j] * scale) if scale else float(distances[j]),
                 "unit": "um" if scale else "px"}
        if marker and marker in values:
            column = np.asarray(columns[marker], dtype=np.float64)[keep]
            entry["value"] = float(column[j]) if np.isfinite(column[j]) else None
        neighbours.append(entry)

    out = {
        "project": record.name,
        "cell_id": int(cell_id),
        "id_column": id_column,
        "centroid": {"x": x, "y": y, "unit": "px"},
        "centroid_um": {"x": x * scale, "y": y * scale} if scale else None,
        "markers": values,
        "top_markers": [{"marker": name, **values[name]} for name in ranked[:TOP_MARKERS]],
        "focus_marker": ({"marker": marker, **values[marker]}
                         if marker and marker in values else None),
        "metadata": metadata,
        "regions": render.rois_containing(record.name, x, y),
        "neighbours": neighbours,
        "neighbour_rule": {"k": k, "within_um": within_um,
                           "distance": "centroid to centroid"},
        "log_transformed": data.table.log_transformed,
    }
    png = None
    if include_crop:
        from plexora.agent.render_spec import (CellsSpec, CenterBounds, ChannelSpec,
                                               IdHighlight, OutputSpec, RenderInput)
        from plexora.agent.presets import NUCLEAR_COLOR, TARGET_COLOR, nuclear_channel

        crop, crop_how = crop_for(record, crop_um, crop_px)
        if crop_how.startswith("default") and neighbours:
            # A crop that shows the cell and not the neighbours it names is half
            # an answer: by default, widen it to take in the farthest one.
            far_px = float(distances[order[:k]].max()) if k else 0.0
            side_px = crop["size_px"] if "size_px" in crop else crop["size_um"] / scale
            wanted = min(MAX_CROP_PX, max(side_px, 2.4 * far_px))
            if wanted > side_px:
                crop = {"size_px": wanted}
                crop_how = "default_fits_neighbours"
        channel_names = [c.get("fullname") or c.get("name")
                         for c in record.image.real_channels]
        channels = []
        nuclear = nuclear_channel(channel_names)
        if nuclear and nuclear != marker:
            channels.append(ChannelSpec(name=nuclear, color=NUCLEAR_COLOR))
        if marker and marker in channel_names:
            channels.append(ChannelSpec(name=marker, color=TARGET_COLOR))
        spec = RenderInput(
            project=record.name,
            center=CenterBounds(center_x=x, center_y=y, **crop),
            channels=channels or None,
            segmentation="outlines",
            cells=CellsSpec(highlight=IdHighlight(cell_ids=[int(cell_id)]), label_ids=True,
                            max_labels=k + 1, show_negative=True,
                            positive_color=TARGET_OUTLINE, negative_color=NEIGHBOUR_OUTLINE),
            output=OutputSpec(width=480, height=480),
        )
        rendered = render.render_region(session, spec, store=store)
        png = rendered["png"]
        out["crop"] = {"artifact": rendered["artifact"], "how": crop_how,
                       "bounds_fullres": rendered["manifest"]["bounds_fullres"],
                       "segmentation_status": rendered["manifest"]["segmentation"]["status"],
                       "outline_drawn": rendered["manifest"]["cells"]["highlight_rendering"]}
    return out, png
