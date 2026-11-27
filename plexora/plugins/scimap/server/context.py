"""What a notebook is about: one project, or a dataset of several.

Read from Plexora's own records and never by loading a datasource: the page
must open in milliseconds even when the viewer has nothing loaded, and asking
a datasource a question loads it.
"""

from __future__ import annotations

from pathlib import Path

#: Table formats the Prepare step can turn into a working AnnData.
TABULAR = ("csv", "parquet")
ANNDATA = ("anndata",)


class ContextError(ValueError):
    def __init__(self, message, code="invalid_context"):
        super().__init__(message)
        self.code = code


def _info(name) -> dict:
    from plexora import api

    try:
        return api.project_info(name)
    except KeyError as exc:
        raise ContextError(f"There is no project called {name!r}.", "unknown_project") from exc


def table_of(name) -> dict:
    """The project's cell table, as the Prepare step needs it, or a refusal
    that says what is missing. Read from Plexora's record (`api.project_info`),
    never by loading the project."""
    info = _info(name)
    if not info.get("has_table") or not info.get("src"):
        return {"project": name, "has_table": False}
    roles = info.get("roles") or {}
    src = str(info["src"])
    kind = info.get("type")
    return {
        "project": name,
        "has_table": True,
        "type": kind,
        "src": src,
        "file": Path(src).name,
        "folder": str(Path(src).parent),
        "table": info.get("table") or "",
        "on_node": bool(info.get("on_node")),
        "subset": dict(info.get("subset") or {}),
        "roles": {"cell_id": roles.get("cell_id"), "x": roles.get("x"), "y": roles.get("y"),
                  "image_id": roles.get("image_id"), "celltype": roles.get("celltype")},
        "markers": list(info.get("markers") or ()),
        "metadata": list(info.get("metadata_columns") or ()),
        "log_transformed": bool(info.get("log_transformed")),
        "obs_id_field": roles.get("cell_id") if kind in ANNDATA else None,
        "pixel_size": info.get("pixel_size_effective"),
        "image": info.get("image"),
        "mask": info.get("mask"),
        "coordinates": dict(info.get("coordinates") or {}),
        "supported": kind in TABULAR + ANNDATA and not info.get("on_node"),
    }


def projects() -> list:
    """Every project, for the project picker."""
    from plexora import api

    out = []
    for name in api.list_projects():
        try:
            info = api.project_info(name)
        except KeyError:
            continue
        out.append({"name": name, "has_table": bool(info.get("has_table")), "type": info.get("type")})
    return out


def datasets() -> list:
    from plexora import api

    return [{"id": d["id"], "name": d["name"], "projects": list(d.get("projects") or []),
             "meta": d.get("meta") or {}} for d in api.list_datasets()]


def dataset(dataset_id) -> dict:
    for found in datasets():
        if found["id"] == dataset_id:
            return found
    raise ContextError("That dataset no longer exists.", "unknown_dataset")


def resolve(kind, ref) -> dict:
    """`{kind, id, name, projects}` for a project name or a dataset id."""
    if kind == "project":
        _info(ref)
        return {"kind": "project", "id": ref, "name": ref, "projects": [ref]}
    if kind == "dataset":
        found = dataset(ref)
        if not found["projects"]:
            raise ContextError("That dataset has no projects yet.", "empty_dataset")
        return {"kind": "dataset", "id": found["id"], "name": found["name"],
                "projects": found["projects"]}
    raise ContextError(f"unknown context kind {kind!r}")


#: The folder results go in, beside the data.
RESULTS_DIRNAME = "scimap_analysis"


def default_results_dir(context) -> str:
    """`scimap_analysis/` beside the data: for a project, in its table's
    folder; for a dataset, in the folder its tables share, else the first
    project's. Plexora's own folder when that cannot be written to."""
    import os

    folders = []
    for name in context.get("projects") or []:
        try:
            info = table_of(name)
        except ContextError:
            continue
        if info.get("has_table"):
            folders.append(info["folder"])
    if folders:
        shared = folders[0] if len(set(folders)) == 1 else None
        base = Path(shared or folders[0])
        if os.access(base, os.W_OK):
            return str(base / RESULTS_DIRNAME)
    from plexora.plugins.scimap.server import store

    return str(store.root() / "results" / store.slug(context.get("name"), "analysis"))


def last_viewed(projects) -> str | None:
    """The member most recently opened in the viewer (`lastOpenedAt`), for a
    dataset step shown in the viewer: it lands on the image the user was
    looking at."""
    from plexora import api

    best, when = None, ""
    for name in projects:
        try:
            stamp = str(api.project_info(name).get("last_opened_at") or "")
        except KeyError:
            continue
        if stamp > when:
            best, when = name, stamp
    return best or (projects[0] if projects else None)
