"""A gallery of single cells: one crop per cell, sorted by expression.

A gate is judged cell by cell, and a field shows a few cells at a size where
the one in question is hard to find. A gallery shows the cells themselves --
each centred in its own crop, its outline in magenta, its neighbours' outlines
dim, its id and marker value under it -- so "are the cells just above the gate
really positive?" is a question the picture answers. The borderline gallery
(the cells nearest the gate) is the single most useful gating view.

Each tile is an ordinary `render_region` of the tile's box (not stored), so a
tile draws exactly what a render of that box draws; the gallery is stored once,
content-addressed, with a manifest saying which cell is where.
"""

from __future__ import annotations

import io

import numpy as np

from plexora.agent import gate_rule
from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_IDS
from plexora.agent.presets import NUCLEAR_COLOR, TARGET_COLOR, nuclear_channel

MANIFEST_KIND = "plexora.cell_gallery"

#: Cells in one gallery, at most.
MAX_GALLERY_CELLS = 48

#: Pixel-space crop when the image states no pixel size.
DEFAULT_CROP_PX = 160.0
DEFAULT_CROP_UM = 40.0

SELECTIONS = ("positive", "negative", "borderline", "brightest", "dimmest")

#: The target cell's outline, and its neighbours'.
TARGET_OUTLINE = "#ff3df2"
NEIGHBOUR_OUTLINE = "#8a8f99"


def crop_for(record, crop_um=None, crop_px=None):
    """({"size_um"} | {"size_px"}, how) for a crop, with the defaults."""
    from plexora.server.utils import pixel_scale

    pixel = pixel_scale.pixel_size(record)
    if crop_px is not None:
        return {"size_px": float(crop_px)}, "explicit_px"
    if crop_um is not None:
        if not pixel:
            raise AgentError(
                "precondition_missing",
                "crop_um needs a calibrated image, and this one states no pixel size",
                detail={"missing": [{"key": "pixel_size", "label": "Pixel size"}],
                        "hint": "use crop_px, or set the pixel size in Plexora"})
        return {"size_um": float(crop_um)}, "explicit_um"
    if pixel:
        return {"size_um": DEFAULT_CROP_UM}, "default_um"
    return {"size_px": DEFAULT_CROP_PX}, "default_px"


def table_cells(data):
    """(ids, xs, ys) for every cell with a usable id, in table order."""
    from plexora.server.utils.label_overlay import cell_ids

    schema = data.schema
    frame = data.table.geometry()
    if frame is None or not schema or schema.x not in frame.columns \
            or schema.y not in frame.columns:
        raise AgentError("precondition_missing", "this project's table has no centroids",
                         detail={"missing": [{"key": "roles", "label": "x/y columns"}]})
    ids, keep = cell_ids(frame, schema.cell_id)
    xs = frame[schema.x].to_numpy().astype(np.float64)[keep]
    ys = frame[schema.y].to_numpy().astype(np.float64)[keep]
    return ids, xs, ys, keep


def marker_values(data, marker, keep):
    if marker not in data.table.markers:
        raise AgentError("invalid_input", f"{marker!r} is not a marker of {data.name!r}",
                         detail={"markers": data.table.markers[:200]})
    return np.asarray(data.table.columns([marker])[marker], dtype=np.float64)[keep]


def percentiles_of(values, finite_sorted):
    return np.searchsorted(finite_sorted, values, side="right") / max(1, finite_sorted.size) \
        * 100


def select(ids, values, *, how, n, low, high):
    """Indices into `ids` for a marker-based selection, in display order."""
    finite = np.isfinite(values)
    passes = gate_rule.passes(values, low, high)
    order_by_id = np.argsort(ids, kind="stable")
    if how == "positive":
        pool = order_by_id[passes[order_by_id]]
        return pool[np.argsort(-values[pool], kind="stable")][:n]
    if how == "negative":
        pool = order_by_id[(finite & ~passes)[order_by_id]]
        return pool[np.argsort(-values[pool], kind="stable")][:n]
    if how == "borderline":
        pool = order_by_id[finite[order_by_id]]
        return pool[np.argsort(np.abs(values[pool] - low), kind="stable")][:n]
    if how == "brightest":
        pool = order_by_id[finite[order_by_id]]
        return pool[np.argsort(-values[pool], kind="stable")][:n]
    pool = order_by_id[finite[order_by_id]]
    return pool[np.argsort(values[pool], kind="stable")][:n]


def render_cell_gallery(session, data, *, cell_ids=None, marker=None, select_how=None,
                        n=24, low=None, high=None, crop_um=None, crop_px=None, tile_px=160,
                        channels=None, sort=None, store=True):
    """{png, manifest, artifact, tiles} for a gallery of cells."""
    from PIL import Image

    from plexora.agent import artifacts, plots, render
    from plexora.agent.render_spec import (CellsSpec, CenterBounds, ChannelSpec, IdHighlight,
                                           OutputSpec, RenderInput)
    from plexora.server.utils import fast_png

    record = data.project
    if (cell_ids is None) == (select_how is None):
        raise AgentError("invalid_input", "give either cell_ids or a marker with select")
    if select_how is not None and not marker:
        raise AgentError("invalid_input", "select needs a marker")
    n = int(n)
    if n < 1 or n > MAX_GALLERY_CELLS:
        raise AgentError("too_large" if n > MAX_GALLERY_CELLS else "invalid_input",
                         f"a gallery holds 1 to {MAX_GALLERY_CELLS} cells (asked for {n})",
                         detail={"max": MAX_GALLERY_CELLS})
    channel_records = list(record.image.real_channels)
    channel_names = [c.get("fullname") or c.get("name") for c in channel_records]
    if marker and not channels and marker not in channel_names:
        raise AgentError("precondition_missing",
                         f"{marker!r} is a table column with no image channel of that name, "
                         "so there is nothing to look at; pass channels to draw others",
                         detail={"channels": channel_names})
    crop, crop_how = crop_for(record, crop_um, crop_px)

    ids, xs, ys, keep = table_cells(data)
    gate = None
    values = None
    if marker:
        values = marker_values(data, marker, keep)
        gate_source = "explicit"
        if low is None or high is None:
            stored = render._stored_gate(data, marker)
            gate_source = stored["source"] if low is None else "explicit"
            low = stored["low"] if low is None else float(low)
            high = stored["high"] if high is None else float(high)
        gate = {"low": float(low), "high": float(high), "source": gate_source}

    truncated = False
    if cell_ids is not None:
        if len(cell_ids) > MAX_IDS:
            raise AgentError("too_large", f"at most {MAX_IDS} cell ids", detail={"max": MAX_IDS})
        position = {int(cid): i for i, cid in enumerate(ids.tolist())}
        unknown = [int(c) for c in cell_ids if int(c) not in position]
        if unknown:
            raise AgentError("invalid_input", f"{len(unknown)} cell id(s) are not in the table",
                             detail={"unknown": unknown[:50],
                                     "id_column": data.schema.cell_id if data.schema else None})
        wanted = list(dict.fromkeys(int(c) for c in cell_ids))
        truncated = len(wanted) > n
        chosen = np.array([position[c] for c in wanted[:n]], dtype=np.int64)
        sort = sort or ("value_desc" if values is not None else "given")
        if sort == "value_desc" and values is not None:
            chosen = chosen[np.argsort(-np.nan_to_num(values[chosen], nan=-np.inf),
                                       kind="stable")]
        elif sort == "value_asc" and values is not None:
            chosen = chosen[np.argsort(np.nan_to_num(values[chosen], nan=np.inf),
                                       kind="stable")]
        elif sort == "id":
            chosen = chosen[np.argsort(ids[chosen], kind="stable")]
        how = "ids"
    else:
        if select_how not in SELECTIONS:
            raise AgentError("invalid_input", f"select is one of {SELECTIONS}")
        chosen = select(ids, values, how=select_how, n=n, low=gate["low"], high=gate["high"])
        how = select_how
        sort = {"borderline": "distance_from_gate", "dimmest": "value_asc"}.get(
            select_how, "value_desc")
        if sort == "distance_from_gate" and len(chosen):
            # Nearest the gate first is how they were picked; shown by value so
            # the row reads from just below to just above.
            chosen = chosen[np.argsort(values[chosen], kind="stable")]
    if not len(chosen):
        raise AgentError("invalid_input", f"no cells match {how!r}",
                         detail={"gate": gate, "selection": how})

    draw_channels = []
    if channels:
        palette = (TARGET_COLOR, NUCLEAR_COLOR, "#00e5ff")
        for index, name in enumerate(channels[:3]):
            draw_channels.append(ChannelSpec(name=name, color=palette[index]))
    else:
        nuclear = nuclear_channel(channel_names)
        if nuclear and nuclear != marker:
            draw_channels.append(ChannelSpec(name=nuclear, color=NUCLEAR_COLOR))
        if marker:
            draw_channels.append(ChannelSpec(name=marker, color=TARGET_COLOR))
        if not draw_channels:
            draw_channels.append(ChannelSpec(name=channel_names[0], color="#ffffff"))

    finite_sorted = np.sort(values[np.isfinite(values)]) if values is not None else None
    tiles, captions, rows = [], [], []
    for index in chosen.tolist():
        cid = int(ids[index])
        spec = RenderInput(
            project=record.name,
            center=CenterBounds(center_x=float(xs[index]), center_y=float(ys[index]), **crop),
            channels=draw_channels,
            segmentation="outlines",
            cells=CellsSpec(highlight=IdHighlight(cell_ids=[cid]), show_negative=True,
                            positive_color=TARGET_OUTLINE, negative_color=NEIGHBOUR_OUTLINE),
            output=OutputSpec(width=int(tile_px), height=int(tile_px)),
            scale_bar=False,
        )
        rendered = render.render_region(session, spec, store=False)
        tiles.append(Image.open(io.BytesIO(rendered["png"])).convert("RGB"))
        row = {"cell_id": cid, "x": float(xs[index]), "y": float(ys[index]),
               "bounds_fullres": rendered["manifest"]["bounds_fullres"],
               "level": rendered["manifest"]["level"],
               "segmentation_status": rendered["manifest"]["segmentation"]["status"],
               "outline_drawn": rendered["manifest"]["cells"]["highlight_rendering"]}
        caption = f"#{cid}"
        if values is not None:
            value = float(values[index])
            call = ("pos" if gate_rule.passes(np.array([value]), gate["low"],
                                              gate["high"])[0] else "neg")
            row.update(value=value if np.isfinite(value) else None,
                       percentile=(float(percentiles_of(value, finite_sorted))
                                   if np.isfinite(value) else None),
                       call="positive" if call == "pos" else "negative",
                       distance_from_gate=(value - gate["low"]) if np.isfinite(value) else None)
            caption += f"  {value:.4g}  {call}" if np.isfinite(value) else "  n/a"
        rows.append(row)
        captions.append(caption)

    columns = int(min(8, max(1, round(np.sqrt(len(tiles) * 1.5)))))
    image, placed = plots.grid(tiles, captions, columns=columns)
    for row, (r, c) in zip(rows, placed):
        row["row"], row["col"] = r, c
    title = f"{record.name}: {len(tiles)} cells"
    if marker:
        title += (f", {marker} {how}; gate {gate['low']:.4g}"
                  + ("" if how == "ids" else f" ({gate['source']})"))
    image = plots.header(image, title)
    png = fast_png.encode_rgb8_png(np.asarray(image))
    manifest = {
        "kind": MANIFEST_KIND,
        "project": record.name,
        "marker": marker,
        "gate": gate,
        "selection": {"how": how, "n_requested": n, "n_drawn": len(tiles), "sort": sort,
                      "truncated": truncated},
        "crop": {**crop, "how": crop_how},
        "tile_px": int(tile_px),
        "columns": columns,
        "channels": [c.model_dump(mode="json") for c in draw_channels],
        "tiles": rows,
        "egress": "rendered_pixels",
        "provenance": {"table": data.table.locator.to_dict(),
                       "renderer": "plexora.agent.cell_gallery/1"},
    }
    artifact = artifacts.put(record.name, png, manifest, kind="cell_gallery") if store else None
    return {"png": png, "manifest": manifest, "artifact": artifact}
