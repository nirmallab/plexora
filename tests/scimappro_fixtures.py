"""SCIMAP Pro, as Plexora's bridge tests need it -- without SCIMAP Pro installed.

Two things, both fakes, both small:

- `make_anndata_project` -- a project whose cell table is an AnnData (zarr or
  h5ad) in the shape SCIMAP Pro reads and writes: `CellID`, `X_centroid`,
  `Y_centroid`, `imageid`, a `phenotype`, over the same blob scene the agent
  tests draw (tests/agent_fixtures.py), so cell ids line up with the mask.
- `install_fake_scimappro(monkeypatch)` -- a stand-in `scimappro` module (three
  registry entries: `sp.spatialNeighbors`, `tl.summarizeSamples`,
  `pl.spatialScatterPlot`, and `LicenseError`), and a plugin registered through
  a fake `plexora.plugins` entry point exactly as SCIMAP Pro's real
  `scimappro_plexora` is. The plugin's capabilities follow the contract
  (spatialbridge `docs/contract.md`, "Plexora plugin"): names
  `scimappro.<ns>.<fn>`, tools `scimappro_<ns>_<snake_fn>`, never importing
  `scimappro` until a handler runs. tests/test_scimappro_plugin.py pins that
  contract against this fake; the real plugin must satisfy the same tests.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import types
from pathlib import Path

import numpy as np

from tests.agent_fixtures import CHANNELS, blob_scene, write_image_pyramid, write_mask_pyramid
from tests.helpers import ALL_CONFIRMED, anndata_spec, image_spec, project

# -- an AnnData project ---------------------------------------------------------


def write_anndata(path, cells, *, image_id="slide_A", extra_images=(), phenotype=True):
    """An AnnData of `cells` (blob_scene's) at `path` (.h5ad or .zarr), with
    the obs columns SCIMAP Pro's readers make. `extra_images` adds the same
    cells again under other image ids, for a multi-image table."""
    import anndata as ad
    import pandas as pd

    rows = []
    for image in (image_id, *extra_images):
        for cell in cells:
            rows.append({"CellID": int(cell["id"]), "X_centroid": float(cell["x"]),
                         "Y_centroid": float(cell["y"]), "imageid": image,
                         "group": cell["group"], "DNA": cell["dna"], "CD8": cell["cd8"]})
    frame = pd.DataFrame(rows)
    obs = frame[["CellID", "X_centroid", "Y_centroid", "imageid"]].copy()
    if phenotype:
        obs["phenotype"] = pd.Categorical(frame["group"].map(
            {"positive": "CD8T", "negative": "Other", "borderline": "Other"}))
    obs.index = [f"{i}_{c}" for i, c in zip(obs["imageid"], obs["CellID"])]
    adata = ad.AnnData(frame[list(CHANNELS)].to_numpy(dtype="float32"), obs=obs,
                       var=pd.DataFrame(index=list(CHANNELS)))
    path = Path(path)
    if path.suffix == ".h5ad":
        adata.write_h5ad(path)
    else:
        adata.write_zarr(path)
    return path


def make_anndata_project(data_root, name="tonsil", *, fmt="zarr", image_id="slide_A",
                         extra_images=(), subset=True, size=256, register=True):
    """Register one project reading an AnnData table where it lies.

    Returns `{name, table, image_path, mask_path, cells, image_id}`. With
    `register=False` the files are written and nothing is registered -- what
    `bind_project` starts from.
    """
    data_root = Path(data_root)
    folder = data_root / f"_{name}_files"
    folder.mkdir(parents=True, exist_ok=True)
    image, labels, cells = blob_scene(size=size, grid=4, radius=12)
    image_path = write_image_pyramid(folder / "image.ome.tif", image)
    mask_path = write_mask_pyramid(folder / "mask.tif", labels)
    table = write_anndata(folder / f"cells.{fmt}", cells, image_id=image_id,
                          extra_images=extra_images)
    out = {"name": name, "table": str(table), "image_path": str(image_path),
           "mask_path": str(mask_path), "cells": cells, "image_id": image_id}
    if not register:
        return out
    obs_columns = ["CellID", "X_centroid", "Y_centroid", "imageid", "phenotype"]
    spec = anndata_spec(
        table, coordinates={"source": "obs", "x_column": "X_centroid",
                            "y_column": "Y_centroid"},
        obs_id_field="CellID", cell_id="CellID", celltype="phenotype",
        markers=CHANNELS, obs_columns=obs_columns,
        subset={"column": "imageid", "value": image_id} if subset else None)
    record = project(name, dataset=spec, segmentation=str(mask_path),
                     image=image_spec(channels=CHANNELS, width=size, height=size,
                                      src=str(image_path)),
                     confirmed=ALL_CONFIRMED)
    record = dataclasses.replace(record, image=dataclasses.replace(
        record.image, max_level=2, tile_width=128, tile_height=128))
    config_path = data_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config[name] = record.to_entry()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return out


def add_obs_column(table, column, values):
    """Write one obs column into the table the way another process would --
    plain anndata, nothing of Plexora's -- and leave the rest as it was."""
    import anndata as ad
    import pandas as pd

    table = str(table)
    adata = ad.read_zarr(table) if table.endswith(".zarr") else ad.read_h5ad(table)
    adata.obs[column] = pd.Categorical(values) if isinstance(values[0], str) \
        else np.asarray(values)
    if table.endswith(".zarr"):
        adata.write_zarr(table)
    else:
        adata.write_h5ad(table)


# -- a fake scimappro and its Plexora plugin ----------------------------------------

#: What the real `scimappro/ai/registry.json` carries per function, cut down to
#: the fields the plugin derives capabilities from.
REGISTRY = {
    "sp.spatialNeighbors": {
        "ns": "sp", "fn": "spatialNeighbors", "stage": "spatial",
        "summary": "Build each cell's spatial neighbourhood within a radius.",
        "use_when": "Any neighbourhood or interaction analysis starts here.",
        "not_when": "The table has no coordinates.",
        "synonyms": ["neighbourhood graph", "spatial neighbors"],
        "roles": ["spatial.neighborhood"],
        "requires": {"obs": ["X_centroid", "Y_centroid"]},
        "outputs": ["uns:{label}"],
        "params": {"radius": {"type": "number", "default": 30.0,
                              "description": "Neighbourhood radius in pixels."},
                   "phenotype": {"type": "string", "default": "phenotype",
                                 "description": "The obs column of cell types."}},
        "experimental_unit": "image",
    },
    "tl.summarizeSamples": {
        "ns": "tl", "fn": "summarizeSamples", "stage": "tools",
        "summary": "Summarise a per-cell column per sample.",
        "use_when": "Before any cross-sample comparison.",
        "not_when": "One sample only.",
        "synonyms": ["per sample summary"], "roles": ["stats.compare"],
        "requires": {"obs": ["phenotype"]}, "outputs": ["uns:{label}"],
        "params": {"column": {"type": "string", "default": "phenotype",
                              "description": "Which obs column to summarise."}},
        "experimental_unit": "sample",
    },
    "pl.spatialScatterPlot": {
        "ns": "pl", "fn": "spatialScatterPlot", "stage": "plotting",
        "summary": "Plot cells at their coordinates, coloured by a column.",
        "use_when": "To see where a phenotype sits.", "not_when": "",
        "synonyms": ["spatial plot"], "roles": ["report.figure"],
        "requires": {"obs": ["X_centroid", "Y_centroid"]}, "outputs": [],
        "params": {"colorBy": {"type": "string", "default": "phenotype",
                               "description": "Which obs column colours the cells."}},
        "experimental_unit": "image",
    },
}

_TYPES = {"number": float, "string": str, "integer": int, "boolean": bool}
_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00"
        b"\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
        b"\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def tool_name(key: str) -> str:
    ns, fn = key.split(".", 1)
    return f"scimappro_{ns}_{snake(fn)}"


def _fake_scimappro_module():
    """The `scimappro` the handlers import: functions that write a column or
    a uns key into the table at a path, LicenseError, UnsupportedModality."""
    module = types.ModuleType("scimappro")
    module.__version__ = "0.0-fake"
    module.calls = []
    module.licensed = True

    class LicenseError(Exception):
        pass

    class UnsupportedModality(Exception):
        pass

    module.LicenseError = LicenseError
    module.UnsupportedModality = UnsupportedModality

    def run(key, path, *, label, subset=None, **params):
        import anndata as ad

        module.calls.append({"key": key, "path": str(path), "label": label,
                             "subset": subset, "params": params})
        if not module.licensed:
            raise LicenseError(f"{key} needs a SCIMAP Pro licence (feature 'spatial')")
        if params.get("phenotype") == "__modality__":
            raise UnsupportedModality(f"{key} does not apply to this table")
        adata = ad.read_zarr(path) if str(path).endswith(".zarr") else ad.read_h5ad(path)
        n_images = int(adata.obs["imageid"].nunique()) if "imageid" in adata.obs else 1
        if key.startswith("pl."):
            return {"png": _PNG, "n_images": n_images}
        adata.obs[f"{label}_label"] = np.arange(adata.n_obs) % 3
        adata.uns[label] = {"function": key, "radius": float(params.get("radius", 0) or 0)}
        if str(path).endswith(".zarr"):
            adata.write_zarr(path)
        else:
            adata.write_h5ad(path)
        return {"label": label, "outputs": [f"obs:{label}_label", f"uns:{label}"],
                "experimental_unit": {"unit": REGISTRY[key]["experimental_unit"],
                                      "nReplicates": n_images},
                "warnings": ["one image: descriptive, no cohort claim"] if n_images == 1
                else []}

    module.run = run
    module.REGISTRY = REGISTRY
    return module


def _input_model(key, entry):
    from pydantic import Field, create_model

    from plexora.agent.schemas import AgentModel

    fields = {"project": (str, Field(description="The Plexora project whose table to "
                                                 "analyse."))}
    for param, spec in entry["params"].items():
        fields[param] = (_TYPES[spec["type"]] | None,
                         Field(spec.get("default"), description=spec.get("description")))
    fields["label"] = (str, Field(key.split(".", 1)[1], description="The result's name "
                                  "in the table."))
    fields["expected_revision"] = (int | None, Field(None, description="The workspace "
                                                     "revision last read."))
    if not key.startswith("pl."):
        fields["confirm"] = (bool, Field(False, description="The user asked for this "
                                         "write into the table."))
    return create_model(f"Scimappro_{snake(entry['fn'])}_Input", __base__=AgentModel,
                        **fields)


def _handler(key, entry):
    def handler(call, inp):
        from plexora.agent.errors import AgentError

        try:
            import scimappro
        except ImportError:
            raise AgentError("capability_unavailable", "SCIMAP Pro is not installed",
                             detail={"hint": 'pip install "scimappro[bridge]"'}) from None
        record = call.session.project(inp.project)
        spec = record.dataset
        if spec is None or spec.type not in ("anndata", "spatialdata"):
            raise AgentError("precondition_missing", "SCIMAP Pro needs an AnnData table",
                             detail={"missing": [{"key": "table"}]})
        path = spec.src
        obs = _obs_columns(path)
        missing = [c for c in entry["requires"].get("obs", []) if c not in obs]
        params = {k: v for k, v in inp.model_dump().items()
                  if k in entry["params"] and v is not None}
        for param in ("phenotype", "column"):
            value = params.get(param)
            if value and value != "__modality__" and value not in obs:
                missing.append(value)
        if missing:
            raise AgentError("precondition_missing",
                             f"the table has no {missing} column(s)",
                             detail={"missing": [{"key": f"obs:{c}"} for c in missing]})
        sections = [s.format(label=inp.label) for s in entry["outputs"]] + \
            ([f"obs:{inp.label}_label"] if not key.startswith("pl.") else [])
        workspace = None
        try:
            from spatialbridge import workspace as sb_workspace
            from spatialbridge.errors import BridgeError

            workspace = sb_workspace.ensure(path, producer="scimappro")
            workspace.check(inp.expected_revision, sections, writer="scimappro")
        except ImportError:
            workspace = None
        except BridgeError as exc:
            raise AgentError("conflict", exc.message, detail={"bridge": exc.to_problem()},
                             retryable=False) from None
        subset = (spec.subset or {}).get("value")
        call.progress(0, 1, f"running {key}")
        try:
            outcome = scimappro.run(key, path, label=inp.label, subset=subset, **params)
        except scimappro.LicenseError as exc:
            raise AgentError("license_required", str(exc), detail={
                "provider": "scimappro", "hint": "a SCIMAP Pro licence unlocks it"}) from None
        except scimappro.UnsupportedModality as exc:
            raise AgentError("unsupported_modality", str(exc)) from None
        call.progress(1, 1, "done")
        if key.startswith("pl."):
            return {"function": key, "_images": [outcome["png"]],
                    "manifest": {"n_images": outcome["n_images"]}}
        revision = None
        if workspace is not None:
            revision = workspace.commit(sections, "scimappro",
                                        execution_id=f"ex_plexora_{call.operation_id}")
        from plexora.agent.receipts import make_receipt

        receipt = make_receipt(
            call, changed=True, after={"label": inp.label, "sections": sections,
                                       "execution_id": f"ex_plexora_{call.operation_id}",
                                       "revision": revision},
            persistent_state="source_file", source_file_modified=True, source_path=path,
            reversible=False,
            undo_hint={"tool": "scimappro_drop_result",
                       "arguments": {"project": inp.project, "label": inp.label}},
            bridge={"peer": "scimappro", "execution_id": f"ex_plexora_{call.operation_id}",
                    "workspace_id": workspace.id if workspace else None,
                    "table_fingerprint": None, "token": "never-recorded"})
        if call.notify is not None:
            try:
                call.notify(inp.project, "core", "dataset.changed",
                            {"sections": sections, "producer": "scimappro"})
            except Exception:
                pass
        return {**outcome, "receipt": receipt.model_dump(mode="json")}

    handler.__name__ = tool_name(key)
    return handler


def _obs_columns(path):
    import zarr

    if str(path).endswith(".zarr"):
        group = zarr.open_group(str(path), mode="r")
        return list(group["obs"].attrs.get("column-order", []))
    import h5py

    with h5py.File(path, "r") as handle:
        return [str(c) for c in handle["obs"].attrs.get("column-order", [])]


def capabilities():
    """The plugin's capabilities, from the registry alone -- no `scimappro`
    import, as the real plugin's must be."""
    from plexora.agent.registry import Capability

    out = []
    for key, entry in REGISTRY.items():
        plot = key.startswith("pl.")
        purpose = " ".join(filter(None, (entry["summary"], "Use when: " + entry["use_when"],
                                         ("Not when: " + entry["not_when"])
                                         if entry["not_when"] else "")))
        common = dict(name=f"scimappro.{key}", tool_name=tool_name(key), owner="scimappro",
                      purpose=purpose, input_model=_input_model(key, entry),
                      handler=_handler(key, entry),
                      tags=(entry["ns"], entry["stage"], *entry["synonyms"],
                            *entry["roles"], "analysis"),
                      requires=_requires())
        if plot:
            out.append(Capability(permission="read", visual_output=True,
                                  egress="rendered_pixels", **common))
        else:
            out.append(Capability(permission="source_file_write", source_file_write=True,
                                  persistent=True, reversible=False, remote_safe=False,
                                  execution="job", streams_progress=True,
                                  egress="aggregates", **common))
    search, describe, run, drop = _meta_models()
    out.append(Capability(
        name="scimappro.search", tool_name="scimappro_search_functions", owner="scimappro",
        purpose="Find SCIMAP Pro functions by the user's words.", permission="read",
        input_model=search, handler=_search, tags=("analysis", "search")))
    out.append(Capability(
        name="scimappro.describe", tool_name="scimappro_describe_function", owner="scimappro",
        purpose="One SCIMAP Pro function: what it needs, writes and is for.",
        permission="read", input_model=describe, handler=_describe, tags=("analysis",)))
    out.append(Capability(
        name="scimappro.run", tool_name="scimappro_run", owner="scimappro",
        purpose="Run any SCIMAP Pro function by its key, with its parameters.",
        permission="source_file_write", source_file_write=True, persistent=True,
        reversible=False, remote_safe=False, execution="job", input_model=run,
        handler=_run_any, tags=("analysis",), requires=_requires()))
    out.append(Capability(
        name="scimappro.drop_result", tool_name="scimappro_drop_result", owner="scimappro",
        purpose="Remove a result SCIMAP Pro wrote into the table (the undo of a run).",
        permission="destructive", input_model=drop, handler=_drop, reversible=False,
        tags=("analysis",)))
    return out


def _requires():
    from plexora.api.plugin import Requires

    return Requires(table=True, roles=("cell_id", "x", "y"))


def _meta_models():
    from typing import Any

    from pydantic import Field

    from plexora.agent.schemas import AgentModel

    class SearchInput(AgentModel):
        query: str = Field(description="The user's words.")

    class DescribeInput(AgentModel):
        key: str = Field(description="A function key, e.g. sp.spatialNeighbors.")

    class RunInput(AgentModel):
        project: str
        key: str
        params: dict[str, Any] | None = None
        label: str | None = None
        expected_revision: int | None = None
        confirm: bool = False

    class DropInput(AgentModel):
        project: str
        label: str
        confirm: bool = False

    return SearchInput, DescribeInput, RunInput, DropInput


def _describe(call, inp):
    from plexora.agent.errors import AgentError

    if inp.key not in REGISTRY:
        raise AgentError("invalid_input", f"no SCIMAP Pro function {inp.key!r}")
    return {"key": inp.key, "tool": tool_name(inp.key), **REGISTRY[inp.key]}


def _run_any(call, inp):
    from plexora.agent.errors import AgentError

    if inp.key not in REGISTRY:
        raise AgentError("invalid_input", f"no SCIMAP Pro function {inp.key!r}")
    model = _input_model(inp.key, REGISTRY[inp.key])
    arguments = {"project": inp.project, **(inp.params or {}),
                 "expected_revision": inp.expected_revision}
    if inp.label:
        arguments["label"] = inp.label
    if "confirm" in model.model_fields:
        arguments["confirm"] = inp.confirm
    return _handler(inp.key, REGISTRY[inp.key])(call, model.model_validate(arguments))


def _drop(call, inp):
    from plexora.agent.receipts import make_receipt

    receipt = make_receipt(call, changed=True, before={"label": inp.label}, after=None,
                           persistent_state="source_file", reversible=False)
    return {"dropped": inp.label, "receipt": receipt.model_dump(mode="json")}


def _search(call, inp):
    words = set(inp.query.lower().split())
    hits = [key for key, entry in REGISTRY.items()
            if words & set(" ".join([entry["summary"], *entry["synonyms"]]).lower().split())]
    return {"functions": [{"key": key, "tool": tool_name(key)} for key in hits]}


def plugin():
    from plexora.api.plugin import Plugin

    return Plugin(name="scimappro", label="Analysis", version="0.0-fake",
                  requires=_requires(), owns_cell_layer=False,
                  capabilities_factory=capabilities, entitlement=None)


class _EntryPoint:
    name = "scimappro"
    group = "plexora.plugins"

    def __init__(self, descriptor):
        self._descriptor = descriptor

    def load(self):
        return self._descriptor


def install_fake_scimappro(monkeypatch, *, with_module=True):
    """Register the fake plugin as an entry point and, unless told not to,
    the fake `scimappro` module. Returns `(plugin, module or None)`."""
    from plexora.server import plugins as plugin_registry

    descriptor = plugin()
    found = dict(plugin_registry._entry_points_by_name())
    found["scimappro"] = _EntryPoint(descriptor)
    monkeypatch.setattr(plugin_registry, "_entry_points_by_name", lambda: dict(found))
    module = None
    if with_module:
        module = _fake_scimappro_module()
        monkeypatch.setitem(sys.modules, "scimappro", module)
    else:
        monkeypatch.setitem(sys.modules, "scimappro", None)   # import raises ImportError
    return descriptor, module
