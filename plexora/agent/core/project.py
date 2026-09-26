"""Projects, datasets and where their resources are."""

from __future__ import annotations

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_LIST, bounded
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel, NoInput, ProjectInput


class ListProjectsInput(AgentModel):
    dataset: str | None = Field(None, description="Only projects in this dataset (name or id).")
    query: str | None = Field(None, description="Only projects whose name contains this text.")
    limit: int = Field(50, ge=1, le=MAX_LIST, description="At most this many.")


def _card(record, dataset_of):
    from plexora.server.models import manifest

    summary = manifest.summary(record)
    return {
        "name": record.name,
        "dataset": dataset_of.get(record.name),
        "image_kind": record.image.kind,
        "modality": record.image.modality,
        "channels": len(record.image.real_channels),
        "size": [record.image.width, record.image.height],
        "segmentation": summary["segmentation"],
        "table": summary["table"],
        "table_type": summary["tableType"],
        "modalities": summary["layers"]["modalities"],
        "needs_setup": summary["needsSetup"],
        "distributed": record.is_distributed,
    }


def _datasets_by_project():
    try:
        from plexora import datasets

        return {name: d.name for d in datasets.list_datasets() for name in d.projects}
    except Exception:
        return {}


def list_projects(call, inp):
    from plexora.server.models.project import Project

    dataset_of = _datasets_by_project()
    entries = Project.load_all()
    names = sorted(entries, key=str.casefold)
    if inp.dataset:
        try:
            from plexora import datasets

            members = set(datasets.dataset(inp.dataset).projects)
        except KeyError as exc:
            raise AgentError("invalid_input", str(exc.args[0] if exc.args else exc)) from None
        names = [n for n in names if n in members]
    if inp.query:
        folded = inp.query.casefold()
        names = [n for n in names if folded in n.casefold()]
    cards = []
    shown, truncated = bounded(names, inp.limit)
    for name in shown:
        try:
            cards.append(_card(Project.load(name), dataset_of))
        except Exception as exc:  # a malformed entry must not hide the rest
            cards.append({"name": name, "error": str(exc)})
    return {"projects": cards, "total": len(names), "truncated": truncated}


def _layers(record):
    from plexora.server.models import manifest

    return [{k: v for k, v in layer.items() if k not in ("source",)}
            for layer in manifest.layers(record)]


def applicability(record, *, capabilities=None):
    """{owner: {applies, missing, capabilities}} for every registered owner."""
    from plexora.agent import registry

    out = {}
    for cap in capabilities or registry.all_capabilities():
        entry = out.setdefault(cap.owner, {"applies": True, "missing": [],
                                           "capabilities": []})
        entry["capabilities"].append(cap.tool_name)
        if cap.requires is None or entry.get("_checked"):
            continue
        entry["_checked"] = True
        entry["applies"] = bool(cap.requires.applies_to(record))
        if entry["applies"]:
            entry["missing"] = [r.describe() for r in cap.requires.missing_from(record)]
    for entry in out.values():
        entry.pop("_checked", None)
    return out


class InspectInput(ProjectInput):
    include_table: bool = Field(True, description="Read the cell table for row counts "
                                "and column facts (loads it into this session).")


def inspect_project(call, inp):
    from plexora.server.models import manifest
    from plexora.server.utils import pixel_scale

    from plexora.api.dataset import ImageHandle

    record = call.session.project(inp.project)
    image = record.image
    out = {
        "name": record.name,
        "summary": manifest.summary(record),
        "image": {
            "kind": image.kind,
            "modality": image.modality,
            "blank": image.is_blank,
            "width": image.width, "height": image.height,
            "levels": (image.max_level or 0) + 1 if image.max_level is not None else None,
            "channels": [c.get("fullname") or c.get("name") for c in image.real_channels],
            "pixel_size": pixel_scale.pixel_size(record),
            "location": ImageHandle(record).locator.to_dict(),
        },
        "segmentation": {
            "available": record.segmentation.available,
            "pending": record.segmentation.pending,
            "scale": record.segmentation.scale,
            "location": _seg_locator(record),
        },
        "layers": _layers(record),
        "capabilities": applicability(record),
        "open_questions": manifest.open_questions(record),
    }
    if record.has_table:
        from plexora.api.dataset import DatasetSchema, TableHandle

        schema = DatasetSchema.from_project(record)
        # A handle with no provider reads nothing but the record; anything
        # that would touch the rows goes through `call.data`, the session's
        # own copy, and only when asked to.
        recorded = TableHandle(record)
        if record.columns.classified:
            markers = list(record.columns.markers)
        elif inp.include_table:
            markers = call.data.table.markers
        else:
            markers = []
        table = {
            "source_kind": recorded.source_kind,
            "location": recorded.locator.to_dict(),
            "log_transformed": recorded.log_transformed,
            "roles": {"cell_id": schema.cell_id, "x": schema.x, "y": schema.y,
                      "celltype": schema.celltype, "image_id": schema.image_id},
            "markers": markers[:MAX_LIST],
            "markers_truncated": len(markers) > MAX_LIST,
            "markers_classified": record.columns.classified,
            "metadata_columns": recorded.metadata_columns[:MAX_LIST],
        }
        if inp.include_table:
            frame = call.data.table.geometry()
            table["n_cells"] = int(frame.height) if frame is not None else None
        out["table"] = table
    else:
        out["table"] = None
    return out


def _seg_locator(record):
    binding = record.resources.get("segmentation")
    if binding is not None:
        return {"kind": "segmentation", "provider": "node", "node": binding.node,
                "resource_id": binding.resource_id}
    return {"kind": "segmentation", "provider": "local",
            "path": record.segmentation.derived}


class ListDatasetsInput(AgentModel):
    pass


def list_datasets(call, inp):
    from plexora import datasets

    out = []
    for d in datasets.list_datasets():
        members, truncated = bounded(d.projects)
        out.append({"id": d.id, "name": d.name, "description": d.description,
                    "projects": members, "projects_truncated": truncated,
                    "n_projects": len(d.projects)})
    return {"datasets": out}


class DatasetInput(AgentModel):
    dataset: str = Field(description="Dataset name or id.")


def get_dataset(call, inp):
    from plexora import datasets

    try:
        d = datasets.dataset(inp.dataset)
    except KeyError as exc:
        raise AgentError("invalid_input", str(exc.args[0] if exc.args else exc)) from None
    members, truncated = bounded(d.projects)
    manifest = {}
    for name in members:
        try:
            manifest[name] = datasets.project_manifest(name)["summary"]
        except KeyError:
            continue
    return {"id": d.id, "name": d.name, "description": d.description,
            "meta": dict(d.meta), "projects": members, "projects_truncated": truncated,
            "summaries": manifest}


def resource_status(call, inp):
    """Where each resource of a project is, and whether it can be read now."""
    from plexora.server.models import nodes as node_registry

    record = call.session.project(inp.project)
    out = {}
    for kind in ("image", "segmentation", "table"):
        binding = record.resources.get(kind)
        if binding is None:
            path = {"image": record.image.src,
                    "segmentation": record.segmentation.derived,
                    "table": record.dataset.src if record.dataset else None}[kind]
            present = bool(path)
            exists = None
            if path:
                from pathlib import Path

                exists = Path(str(path)).exists()
            out[kind] = {"provider": "local", "present": present, "path": path,
                         "readable": bool(present and exists), "status":
                         ("ready" if present and exists else
                          "missing_file" if present else "absent")}
            continue
        entry = {"provider": "node", "node": binding.node,
                 "resource_id": binding.resource_id, "present": True}
        node = node_registry.find(binding.node)
        if node is None:
            entry.update(status="unknown_node", readable=False)
        elif node_registry.is_disconnected(node):
            entry.update(status="disconnected", readable=False,
                         hint="reconnect the node from Settings > Remotes")
        else:
            try:
                from plexora import nodes

                described = nodes.resource_status(binding.node, binding.resource_id,
                                                  timeout=5.0)
                entry.update(status=described.get("status") or "ready",
                             readable=described.get("status") in (None, "ready"),
                             detail=described)
            except Exception as exc:
                entry.update(status="unreachable", readable=False, error=str(exc),
                             retryable=True)
        out[kind] = entry
    return {"project": record.name, "resources": out}


def capabilities():
    return [
        Capability(
            name="project.list", tool_name="list_projects", owner="core",
            purpose="List the projects (samples) Plexora knows, with what each has: "
                    "image kind, channel count, segmentation and cell-table status.",
            permission="read", input_model=ListProjectsInput, handler=list_projects,
            tags=("project", "projects", "sample", "samples", "dataset", "list",
                  "what", "have", "data"),
        ),
        Capability(
            name="project.inspect", tool_name="inspect_project", owner="core",
            purpose="Everything about one project an analysis needs first: image size, "
                    "channels, pixel size, segmentation, cell table (roles, markers, "
                    "cell count), layers, and which tools apply or what they still need.",
            permission="read", input_model=InspectInput, handler=inspect_project,
            tags=("project", "inspect", "describe", "triage", "channels", "markers",
                  "summary", "what"),
        ),
        Capability(
            name="dataset.list", tool_name="list_datasets", owner="core",
            purpose="List datasets -- the folders cohorts of projects are grouped in.",
            permission="read", input_model=ListDatasetsInput, handler=list_datasets,
            tags=("dataset", "datasets", "cohort", "list"),
        ),
        Capability(
            name="dataset.get", tool_name="get_dataset", owner="core",
            purpose="One dataset's projects and each project's setup summary.",
            permission="read", input_model=DatasetInput, handler=get_dataset,
            tags=("dataset", "cohort", "projects"),
        ),
        Capability(
            name="resource.status", tool_name="get_resource_status", owner="core",
            purpose="Where a project's image, mask and table are (this machine or a data "
                    "node) and whether each can be read right now.",
            permission="read", input_model=ProjectInput, handler=resource_status,
            tags=("resource", "status", "node", "remote", "available", "location"),
        ),
    ]
