"""The three-panel picture a gate is judged from, one per field.

A: the marker alone, as it is -- nothing drawn over it, so the eye judges the
   stain and not the overlay.
B: the marker in yellow over nuclear grey, with the cell outlines and the cells
   the gate calls positive in magenta (optionally a context marker in cyan).
C: where this field's cells fall on the marker's whole distribution, against
   the fitted populations, the gate and its borderline band.

Plus the numbers behind them: how many cells, how many called positive here
and overall, how many sit just either side of the threshold -- and a bounded
list of those borderline cells by id, so a judgement can name them.
"""

from __future__ import annotations

import numpy as np

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_BORDERLINE_ROWS, MAX_PANEL_WIDTH
from plexora.agent.presets import CONTEXT_COLOR, NUCLEAR_COLOR, TARGET_COLOR, nuclear_channel


def _panel(session, project, bounds, channels, *, segmentation, highlight, width):
    from plexora.agent import render
    from plexora.agent.render_spec import (Bounds, CellsSpec, ChannelSpec, OutputSpec,
                                           RenderInput)

    spec = RenderInput(
        project=project,
        bounds=Bounds(**bounds),
        channels=[ChannelSpec(name=name, color=color) for name, color in channels],
        segmentation=segmentation,
        cells=CellsSpec(highlight=highlight) if highlight is not None else None,
        output=OutputSpec(width=width, height=width),
        scale_bar=True,
    )
    return render.render_region(session, spec, store=False)


def render_gate_validation(session, data, marker, low, high, fields, *, band,
                           curves=None, context_marker=None, nuclear=None, panel_px=480,
                           max_borderline=MAX_BORDERLINE_ROWS):
    """[(png, field_record)] for each field, and the shared context."""
    from PIL import Image

    from plexora.agent import artifacts, plots
    from plexora.agent.gate_sampling import field_stats
    from plexora.agent.render_spec import MarkerHighlight
    from plexora.server.utils import fast_png
    from plexora.server.utils.label_overlay import cell_ids

    record = data.project
    channel_names = [c.get("fullname") or c.get("name") for c in record.image.real_channels]
    if marker not in channel_names:
        raise AgentError("precondition_missing",
                         f"{marker!r} is a table column with no image channel of that name, "
                         "so there is nothing to look at",
                         detail={"channels": channel_names})
    if context_marker and context_marker not in channel_names:
        raise AgentError("invalid_input", f"{context_marker!r} is not an image channel",
                         detail={"channels": channel_names})
    nuclear = nuclear or nuclear_channel(channel_names)
    panel_px = int(max(160, min(panel_px, (MAX_PANEL_WIDTH - 12) // 3)))

    schema = data.schema
    frame = data.table.geometry()
    ids, keep = cell_ids(frame, schema.cell_id)
    xs = frame[schema.x].to_numpy().astype(np.float64)[keep]
    ys = frame[schema.y].to_numpy().astype(np.float64)[keep]
    values = np.asarray(data.table.columns([marker])[marker], dtype=np.float64)[keep]
    finite = np.isfinite(values)
    positive_all = finite & (values > low) & (values < high)
    dataset_fraction = float(positive_all.sum() / max(1, finite.sum()))
    sorted_values = np.sort(values[finite])

    highlight = MarkerHighlight(marker=marker, low=low, high=high)
    b_channels = []
    if nuclear and nuclear != marker:
        b_channels.append((nuclear, NUCLEAR_COLOR))
    b_channels.append((marker, TARGET_COLOR))
    if context_marker and context_marker != marker:
        b_channels.append((context_marker, CONTEXT_COLOR))

    out = []
    for field in fields:
        bounds = field["bounds"]
        box = (bounds["x"], bounds["y"], bounds["x"] + bounds["width"],
               bounds["y"] + bounds["height"])
        a = _panel(session, record.name, bounds, [(marker, "#ffffff")],
                   segmentation="none", highlight=None, width=panel_px)
        b = _panel(session, record.name, bounds, b_channels, segmentation="outlines",
                   highlight=highlight, width=panel_px)
        inside, stats = field_stats(xs, ys, values, ids, box, low, high, band[0], band[1])
        field_values = values[inside]
        field_positive = positive_all[inside]
        c = plots.draw_histogram(values, gate=low, band=band, curves=curves,
                                 rug=field_values, rug_positive=field_positive,
                                 width=panel_px, height=panel_px,
                                 title=f"{marker}: all cells, this field's cells below")
        import io

        panels = [Image.open(io.BytesIO(p["png"])).convert("RGB") for p in (a, b)] + [c]
        composite = plots.montage(panels)
        text = (f"{field.get('field_id', '')} {field.get('class', '')}: "
                f"{stats['cells']} cells, {stats['positives']} called positive "
                f"({(stats['positive_fraction'] or 0):.0%}); gate {low:.4g}   "
                f"A raw {marker} | B gate overlay | C distribution")
        composite = plots.header(composite, text)
        png = fast_png.encode_rgb8_png(np.asarray(composite))

        near = inside & finite & (values >= band[0]) & (values <= band[1])
        rows = []
        for index in np.flatnonzero(near):
            value = float(values[index])
            rows.append({
                "cell_id": int(ids[index]),
                "expression": value,
                "percentile": float(np.searchsorted(sorted_values, value, side="right")
                                    / max(1, sorted_values.size) * 100),
                "distance_from_gate": value - low,
                "current_call": "positive" if positive_all[index] else "negative",
            })
        rows.sort(key=lambda r: (abs(r["distance_from_gate"]), r["cell_id"]))
        manifest = {
            "kind": "plexora.gate_validation_panel",
            "project": record.name, "marker": marker,
            "gate": {"low": low, "high": high}, "band": {"low": band[0], "high": band[1]},
            "field": field, "panels": {
                "A": {"channels": a["manifest"]["channels"], "overlays": []},
                "B": {"channels": b["manifest"]["channels"],
                      "overlays": b["manifest"]["overlays"],
                      "segmentation": b["manifest"]["segmentation"],
                      "cells": b["manifest"]["cells"]},
                "C": {"kind": "histogram", "log_axis": bool(values.size
                                                            and np.nanmin(values) >= 0)}},
            "bounds_fullres": a["manifest"]["bounds_fullres"],
            "channels": b["manifest"]["channels"],
            "egress": "rendered_pixels",
        }
        artifact = artifacts.put(record.name, png, manifest, kind="gate_validation")
        out.append((png, {
            "field_id": field.get("field_id"),
            "class": field.get("class"),
            "why": field.get("why"),
            "bounds": bounds,
            "cells_in_field": stats["cells"],
            "called_positive": stats["positives"],
            "field_positive_fraction": stats["positive_fraction"],
            "dataset_positive_fraction": dataset_fraction,
            "borderline": {"below": stats["borderline_below"],
                           "above": stats["borderline_above"],
                           "band": [band[0], band[1]]},
            "borderline_cells": rows[:max_borderline],
            "borderline_cells_truncated": len(rows) > max_borderline,
            "segmentation": b["manifest"]["segmentation"]["status"],
            "artifact": artifact,
        }))
    return out, {"nuclear_channel": nuclear, "context_marker": context_marker,
                 "panel_px": panel_px, "dataset_positive_fraction": dataset_fraction}
