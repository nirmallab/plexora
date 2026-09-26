"""The cell table: which columns are markers, and what one marker looks like."""

from __future__ import annotations

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_BINS, MAX_LIST
from plexora.agent.registry import Capability
from plexora.agent.schemas import ProjectInput
from plexora.api.plugin import Requires

_NEEDS_TABLE = Requires(table=True)


def list_markers(call, inp):
    record = call.session.project(inp.project)
    if record.columns.classified:
        markers = list(record.columns.markers)
    else:
        markers = call.data.table.markers
    from plexora.api.dataset import DatasetSchema, TableHandle

    schema = DatasetSchema.from_project(record)
    image_channels = set(record.image.channel_names)
    return {
        "project": record.name,
        "markers": markers[:MAX_LIST],
        "truncated": len(markers) > MAX_LIST,
        "n_markers": len(markers),
        "classified": record.columns.classified,
        "metadata_columns": TableHandle(record).metadata_columns[:MAX_LIST],
        "roles": {"cell_id": schema.cell_id, "x": schema.x, "y": schema.y,
                  "celltype": schema.celltype, "image_id": schema.image_id},
        # Worth saying: a marker with no image channel of the same name cannot
        # be looked at, and a channel with no column cannot be gated.
        "markers_without_channel": [m for m in markers if m not in image_channels][:MAX_LIST],
        "channels_without_marker": sorted(image_channels - set(markers))[:MAX_LIST],
    }


class DistributionInput(ProjectInput):
    marker: str = Field(description="A marker column, as `list_markers` names it.")
    bins: int = Field(50, ge=5, le=MAX_BINS, description="Histogram bins (merged from 50).")


def _rebin(histogram, bins):
    if not histogram or bins >= len(histogram):
        return histogram
    step = len(histogram) / bins
    out = []
    for i in range(bins):
        chunk = histogram[int(round(i * step)):int(round((i + 1) * step))] or []
        if not chunk:
            continue
        out.append({"x": sum(p["x"] for p in chunk) / len(chunk),
                    "y": sum(p["y"] for p in chunk) / len(chunk)})
    return out


def marker_distribution(call, inp):
    data = call.data
    if inp.marker not in data.table.markers:
        raise AgentError("invalid_input", f"{inp.marker!r} is not a marker of {inp.project!r}",
                         detail={"markers": data.table.markers[:MAX_LIST]})
    desc = dict(data.table.describe().get(inp.marker) or {})
    histogram = [{"x": float(p["x"]), "y": float(p["y"])} for p in desc.pop("histogram", [])]
    return {
        "project": inp.project, "marker": inp.marker,
        "log_transformed": data.table.log_transformed,
        "stats": {k: float(v) if isinstance(v, (int, float)) else v for k, v in desc.items()},
        "histogram": _rebin(histogram, inp.bins),
        "histogram_density": True,
    }


def capabilities():
    return [
        Capability(
            name="table.markers", tool_name="list_markers", owner="core",
            purpose="Which cell-table columns are markers (gateable intensities), which "
                    "are metadata, which columns fill the cell-id/x/y roles, and which "
                    "markers have no image channel to look at.",
            permission="read", input_model=ProjectInput, handler=list_markers,
            requires=_NEEDS_TABLE, reads=("table",),
            tags=("marker", "markers", "columns", "table", "features", "phenotype"),
        ),
        Capability(
            name="feature.summarize", tool_name="get_marker_distribution", owner="core",
            purpose="One marker's distribution across all cells: count, mean, sd, "
                    "quartiles, min/max and a density histogram.",
            permission="read", input_model=DistributionInput, handler=marker_distribution,
            requires=_NEEDS_TABLE, reads=("table",), egress="aggregates",
            tags=("distribution", "histogram", "marker", "intensity", "summary", "qc"),
        ),
    ]
