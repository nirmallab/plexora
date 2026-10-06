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
            "expression": {"source": record.feature_source,
                           "log_transformed": bool(record.log_transformed),
                           "confirmed": "features" in set(record.confirmed),
                           "options": [o["value"] for o in _feature_options(record)]},
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


# -- which expression matrix is read ---------------------------------------------


class ExpressionInput(ProjectInput):
    sample_cells: int = Field(2000, ge=100, le=20000, description="Cells sampled per matrix.")
    seed: int = 0


def _expression_revision(record):
    from plexora.server.providers.local import _spec_hash

    return f"{_spec_hash(record.dataset)}|{int(record.log_transformed)}"


def inspect_expression_sources(call, inp):
    """Every matrix the project can read, what a sample of each looks like (as
    stored, never transformed) and what `expression.recommend` makes of them."""
    from plexora.agent import expression
    from plexora.api import features

    record = call.session.project(inp.project)
    if not record.has_table:
        raise AgentError("precondition_missing", f"{inp.project!r} has no cell table")
    now = features.current(record)
    options = []
    for option in features.options(record):
        values = features.sample(record, option["value"], inp.sample_cells, inp.seed)
        stats = expression.classify_markers(values)
        options.append({"value": option["value"], "label": option["label"],
                        "kind": stats.pop("kind"), "stats": stats})
    recommendation = expression.recommend(options, now, now["confirmed"])
    return {"project": record.name, "source_kind": record.source_kind or "csv",
            "revision": _expression_revision(record), "current": now, "options": options,
            "recommendation": recommendation, "rule": expression.RULE}


class SetExpressionInput(ProjectInput):
    features_layer: str = Field(description="`X` or `layer:<name>`, as "
                                            "inspect_expression_sources lists them.")
    features_log: bool = Field(description="Apply log1p as the values are read. Never for a "
                                           "matrix that is already log-transformed.")
    confirm: bool = Field(True, description="Record the choice as answered, so no tool asks "
                                            "it again.")


def set_expression_source(call, inp):
    from plexora.agent.receipts import make_receipt
    from plexora.api import features

    record = call.session.project(inp.project)
    if not record.has_table:
        raise AgentError("precondition_missing", f"{inp.project!r} has no cell table")
    allowed = [o["value"] for o in features.options(record)]
    if inp.features_layer not in allowed:
        raise AgentError("invalid_input", f"{inp.features_layer!r} is not a matrix of "
                         f"{inp.project!r}", detail={"allowed": allowed})
    revision_before = _expression_revision(record)
    try:
        before, after, changed = features.apply(inp.project, inp.features_layer,
                                                inp.features_log, confirm=inp.confirm)
    except ValueError as exc:
        raise AgentError("invalid_input", str(exc)) from None
    call.session.invalidate(inp.project)
    updated = call.session.project(inp.project)
    receipt = make_receipt(
        call, changed=changed, before=before, after=after,
        revision_before=revision_before, revision_after=_expression_revision(updated),
        persistent_state="project_config",
        undo_hint={"tool": "set_expression_source", "arguments": {
            "project": inp.project, "features_layer": before["features_layer"],
            "features_log": before["features_log"], "confirm": True}},
        extra={"note": "undo restores the matrix and the transform, not the confirmation"})
    if changed and call.notify is not None:
        try:
            call.notify(inp.project, "core", "reload", {"datasource_changed": True})
        except Exception:
            pass
    return {"receipt": receipt.model_dump(mode="json"), "before": before, "after": after,
            "changed": changed}


class SetPixelSizeInput(ProjectInput):
    microns_per_pixel: float | None = Field(
        description="What one full-resolution pixel is worth, in microns, as the user "
                    "stated it; null clears a stored value.", ge=0.01, le=50.0)


def set_pixel_size(call, inp):
    """Record the pixel size of an image whose file states none, as the
    viewer's calibration control does (`source: manual`). Refused when the
    file states one: the file is read on every load, and a typed value would
    silently outrank it."""
    from plexora import datasource
    from plexora.agent.receipts import make_receipt
    from plexora.api import features
    from plexora.server.utils import pixel_scale

    record = call.session.project(inp.project)
    before = pixel_scale.pixel_size(record)
    if before and before.get("source") == "metadata":
        raise AgentError("conflict", f"{inp.project!r}'s image file states its pixel size "
                         f"({before['value']:.4g} µm/px); it is not overridden here",
                         detail={"pixel_size": before})
    before_value = before["value"] if before else None
    try:
        datasource.set_pixel_size(inp.project, inp.microns_per_pixel)
    except ValueError as exc:
        raise AgentError("invalid_input", str(exc)) from None
    call.session.invalidate(inp.project)
    after = pixel_scale.pixel_size(call.session.project(inp.project))
    after_value = after["value"] if after else None
    changed = before_value != after_value
    if changed:
        features.reload_if_loaded(inp.project)
    receipt = make_receipt(
        call, changed=changed, before={"microns_per_pixel": before_value},
        after={"microns_per_pixel": after_value}, persistent_state="project_config",
        undo_hint={"tool": "set_pixel_size", "arguments": {
            "project": inp.project, "microns_per_pixel": before_value}})
    if changed and call.notify is not None:
        try:
            call.notify(inp.project, "core", "reload", {"datasource_changed": True})
        except Exception:
            pass
    return {"receipt": receipt.model_dump(mode="json"), "before": before, "after": after,
            "changed": changed}


def _feature_options(record):
    from plexora.api import features

    try:
        return features.options(record)
    except Exception:
        return []


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


# -- which project shows this table (the bridge's binding) -----------------------


class ForTableInput(AgentModel):
    table: str = Field(description="Path to the cell table (.h5ad, .zarr, a SpatialData "
                                   "store), as this machine sees it.")
    image_id: str | None = Field(None, description="Which image of a multi-image table: the "
                                 "value its image-id column holds for that image's cells.")
    table_name: str | None = Field(None, description="Which table inside a SpatialData store.")


def _same_file(a, b) -> bool:
    from pathlib import Path

    try:
        return Path(str(a)).expanduser().resolve() == Path(str(b)).expanduser().resolve()
    except (OSError, RuntimeError):
        return str(a) == str(b)


def _reads_table(record, table, table_name):
    spec = record.dataset
    if spec is None or record.resources.get("table") is not None:
        return False
    if not _same_file(spec.src, table):
        return False
    return (spec.table or "") == (table_name or "")


def find_project_for_table(call, inp):
    """The project that shows this table (and this image of it), or None.

    Matched on what the project READS -- `DataSpec.src`, its table within a
    store, and its `subset` -- never on names: SCIMAP Pro holding
    `cohort.zarr` and asking for image `slide_A` is answered with the project
    that reads `cohort.zarr` restricted to `imageid == slide_A`. A project that
    reads the whole table answers for any image when nothing narrower does.
    Several equally good matches answer None, with all of them as
    `candidates`: choosing among them is the caller's call, not a guess here.
    """
    from plexora.server.models.project import all_projects

    candidates = []
    for record in sorted(all_projects(), key=lambda r: r.name.casefold()):
        if not _reads_table(record, inp.table, inp.table_name):
            continue
        subset = dict(record.dataset.subset or {})
        value = subset.get("value")
        candidates.append({"project": record.name,
                           "image_id": None if value is None else str(value),
                           "subset": subset or None,
                           "has_image": not record.image.is_blank})
    if inp.image_id is not None:
        exact = [c for c in candidates if c["image_id"] == str(inp.image_id)]
        whole = [c for c in candidates if c["subset"] is None]
        chosen = exact or whole
    else:
        chosen = candidates
    project = chosen[0]["project"] if len(chosen) == 1 else None
    return {"project": project, "candidates": candidates[:MAX_LIST],
            "ambiguous": len(chosen) > 1}


class BindInput(AgentModel):
    table: str = Field(description="Path to the cell table on this machine.")
    image: str | None = Field(None, description="The image to register with it, when no "
                              "project shows this table yet. Needed only then.")
    mask: str | None = Field(None, description="Its segmentation mask (a label image).")
    image_id: str | None = Field(None, description="Which image of a multi-image table this "
                                 "project shows (the image-id column's value for it).")
    name: str | None = Field(None, description="The new project's name; default from the "
                             "image's filename.")
    roles: dict[str, str | None] | None = Field(
        None, description="Which column is which: cell_id, x, y, image_id, celltype.")
    table_name: str | None = Field(None, description="Which table inside a SpatialData store.")
    dataset: str | None = Field(None, description="Put the new project in this dataset.")
    exist_ok: bool = Field(True, description="Answer with the project that already shows "
                           "this table instead of making a second one.")


_ROLE_KEYS = ("cell_id", "x", "y", "image_id", "celltype")


def _register_bound(inp, roles, subset):
    """Register the project `bind_project` makes; returns its name.

    An AnnData/SpatialData table is registered with its coordinates, id column
    and subset given AT registration -- the adapter is planned there, and a
    store with no `obsm["spatial"]` cannot be planned without them -- then the
    same answers are recorded as confirmed (`configure_project`), so no tool
    asks about them again. A flat table takes `create_project`'s own path.
    """
    from pathlib import Path

    from plexora import datasets
    from plexora.server.models.adapters import detect_data_type, is_flat_table

    image = str(Path(inp.image).expanduser())
    mask = str(Path(inp.mask).expanduser()) if inp.mask else None
    table = str(Path(inp.table).expanduser())
    answers = {}
    if roles.get("cell_id"):
        answers["cell_id"] = roles["cell_id"]
    if roles.get("celltype"):
        answers["celltype"] = roles["celltype"]
    if subset is not None:
        answers["single_image"] = True
    elif roles.get("image_id"):
        answers["sample"] = roles["image_id"]
    if is_flat_table(detect_data_type(Path(table))):
        if roles.get("x") and roles.get("y"):
            answers.update(x=roles["x"], y=roles["y"])
        return datasets.create_project(image, name=inp.name, segmentation=mask, data=table,
                                       subset=subset, exist_ok=False, dataset=inp.dataset,
                                       **answers)
    from plexora import get_config
    from plexora.datasource import (_dedupe_dataset_name, _derive_dataset_name_from_path,
                                    register_anndata_datasource)

    config = get_config()
    name = str(inp.name) if inp.name else _dedupe_dataset_name(
        _derive_dataset_name_from_path(Path(image)), config.keys())
    if name in config:
        raise ValueError(f"there is already a project called {name!r}")
    coordinates = None
    if roles.get("x") and roles.get("y"):
        coordinates = {"source": "obs", "x_column": roles["x"], "y_column": roles["y"]}
    register_anndata_datasource(
        name, image, features=table, segmentation=mask,
        coordinate_source="obs" if coordinates else None,
        x=roles.get("x") if coordinates else None, y=roles.get("y") if coordinates else None,
        obs_id_field=roles.get("cell_id"), celltype_column=roles.get("celltype"),
        subset_by=(subset or {}).get("column"), subset_value=(subset or {}).get("value"),
        table=inp.table_name or None, segmentation_async=bool(mask))
    if coordinates:
        answers["coordinates"] = coordinates
    if answers:
        datasets.configure_project(name, **answers)
    if inp.dataset:
        datasets._assign_to(name, inp.dataset)
    return name


def bind_project(call, inp):
    """The project that shows this table, made when there is none.

    The bridge's half of "show SCIMAP Pro's table in Plexora": the table is
    read where it lies (never copied), restricted to one image when `image_id`
    names one, with the columns the caller named recorded as answered. An
    existing project is reused when `exist_ok`; a new one needs `image`.
    Writes Plexora's project registry and nothing else -- the table is opened
    read-only.
    """
    from plexora.agent.receipts import make_receipt

    roles = {k: v for k, v in (inp.roles or {}).items() if v and k in _ROLE_KEYS}
    unknown = sorted(set(inp.roles or {}) - set(_ROLE_KEYS))
    if unknown:
        raise AgentError("invalid_input", f"unknown roles {unknown}; one of {list(_ROLE_KEYS)}")
    found = find_project_for_table(call, ForTableInput(
        table=inp.table, image_id=inp.image_id, table_name=inp.table_name))
    if found["project"] and inp.exist_ok:
        call.project_name = found["project"]
        return {"project": found["project"], "created": False,
                "candidates": found["candidates"]}
    if not inp.image:
        raise AgentError(
            "precondition_missing",
            "no project shows this table yet, and making one needs the image it was "
            "measured on",
            detail={"missing": ["image"], "candidates": found["candidates"],
                    "hint": "pass image= (and mask= for cell outlines) once"})
    from pathlib import Path

    from plexora import datasets

    if not Path(inp.table).expanduser().exists():
        raise AgentError("invalid_input", f"no table at {inp.table}")
    subset = None
    if inp.image_id is not None:
        column = roles.get("image_id")
        if not column:
            raise AgentError("invalid_input", "image_id needs roles.image_id: which column "
                             "holds it", detail={"hint": "e.g. roles={'image_id': 'imageid'}"})
        subset = {"column": column, "value": str(inp.image_id)}
    try:
        name = _register_bound(inp, roles, subset)
    except ValueError as exc:
        raise AgentError("invalid_input", str(exc)) from None
    call.project_name = name
    receipt = make_receipt(
        call, changed=True, before=None,
        after={"project": name, "table": inp.table, "image_id": inp.image_id,
               "roles": roles},
        persistent_state="config", reversible=False)
    return {"project": name, "created": True, "receipt": receipt.model_dump(mode="json")}


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
            name="project.inspect_expression", tool_name="inspect_expression_sources",
            owner="core",
            purpose="Which expression matrices a project can read (X, each AnnData layer), "
                    "what a sample of each looks like (raw counts, raw intensity, "
                    "log-like, scaled) and which one to gate on, or that the user must "
                    "be asked.",
            permission="read", input_model=ExpressionInput,
            handler=inspect_expression_sources, egress="aggregates", reads=("table",),
            tags=("expression", "matrix", "layer", "log", "log1p", "transform", "features",
                  "normalised", "counts", "setup"),
        ),
        Capability(
            name="project.set_expression", tool_name="set_expression_source", owner="core",
            purpose="Choose the matrix a project's marker values are read from and whether "
                    "log1p is applied, and record the choice as answered. Undoable.",
            permission="reversible_write", input_model=SetExpressionInput,
            handler=set_expression_source, writes=("project",), persistent=True,
            egress="metadata",
            tags=("expression", "matrix", "layer", "log", "log1p", "transform", "features",
                  "setup"),
        ),
        Capability(
            name="project.set_pixel_size", tool_name="set_pixel_size", owner="core",
            purpose="Record what one pixel is worth (microns) for an image whose file does "
                    "not say, as the user stated it -- scale bars, fields in microns and "
                    "distances then use it. Undoable.",
            permission="reversible_write", input_model=SetPixelSizeInput,
            handler=set_pixel_size, writes=("project",), persistent=True,
            egress="metadata",
            tags=("pixel", "size", "scale", "calibration", "microns", "resolution", "mpp",
                  "setup"),
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
            name="project.for_table", tool_name="find_project_for_table", owner="core",
            purpose="Which project shows a given cell table (and one image of a multi-image "
                    "table), matched on the file it reads -- the first step of showing another "
                    "application's dataset in the viewer. None, with candidates, when there "
                    "is no single answer.",
            permission="read", input_model=ForTableInput, handler=find_project_for_table,
            tags=("project", "bind", "bridge", "handoff")),
        Capability(
            name="project.bind", tool_name="bind_project", owner="core",
            purpose="The project that shows a cell table, registered from its image (and "
                    "mask) when there is none yet: the table is read where it lies, one image "
                    "of it when image_id says which, with the named columns recorded.",
            permission="reversible_write", input_model=BindInput, handler=bind_project,
            writes=("project",), persistent=True, reversible=False,
            tags=("project", "bind", "bridge", "handoff")),
        Capability(
            name="resource.status", tool_name="get_resource_status", owner="core",
            purpose="Where a project's image, mask and table are (this machine or a data "
                    "node) and whether each can be read right now.",
            permission="read", input_model=ProjectInput, handler=resource_status,
            tags=("resource", "status", "node", "remote", "available", "location"),
        ),
    ]
