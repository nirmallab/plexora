"""Prepare data's manager: the projects a notebook analyzes, the dataset they
make, their columns, and their sample metadata -- read from and written to
Plexora's own records, never a copy.

Plexora is the one source of truth. Adding a project here adds it to the
dataset in Plexora; editing a project's columns edits the project, so the
viewer and every other notebook read the same answers. The page polls
`api.records_stamp()` and redraws when a record changed anywhere (another tab,
the viewer, the home page), and a notebook's projects are re-read from its
dataset each time it is shown.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from plexora.plugins.scimap.server import context as ctx
from plexora.plugins.scimap.server import samples, store
from plexora.plugins.scimap.server.adapter import RunError

_counts: dict = {}
_counts_lock = threading.Lock()


def cells_in(info) -> int | None:
    """How many cells a project's table holds, from the file's own metadata
    (or a line count for CSV), cached on the file's size and time."""
    src = info.get("src")
    if not src:
        return None
    try:
        stat = os.stat(src)
    except OSError:
        return None
    key = (src, stat.st_mtime_ns, stat.st_size, str(info.get("subset")))
    with _counts_lock:
        if key in _counts:
            return _counts[key]
    n = None
    try:
        kind = info.get("type")
        if kind == "csv" and not info.get("subset"):
            with open(src, "rb") as handle:
                n = sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b""))
            with open(src, "rb") as handle:
                handle.seek(max(0, stat.st_size - 1))
                if handle.read(1) != b"\n":
                    n += 1
            n -= 1  # the header
        elif kind == "parquet" and not info.get("subset"):
            import pyarrow.parquet as pq

            n = pq.ParquetFile(src).metadata.num_rows
        elif kind == "anndata" and str(src).endswith(".h5ad") and not info.get("subset"):
            import h5py

            with h5py.File(src, "r") as root:
                obs = root["obs"]
                index = obs.attrs.get("_index", "_index")
                index = index.decode() if isinstance(index, bytes) else index
                n = int(obs[index].shape[0])
    except Exception:  # noqa: BLE001 -- a count we cannot read is left blank
        n = None
    with _counts_lock:
        _counts[key] = n
    return n


def columns_of(info) -> dict:
    """The table's columns, for the role choosers: `{columns, positions}`.
    Header only for a flat table; obs and obsm names for an AnnData."""
    src, kind = info.get("src"), info.get("type")
    out = {"columns": [], "positions": []}
    try:
        if kind == "csv":
            import csv

            with open(src, newline="", encoding="utf-8-sig") as handle:
                out["columns"] = next(csv.reader(handle), [])
        elif kind == "parquet":
            import pyarrow.parquet as pq

            out["columns"] = list(pq.ParquetFile(src).schema_arrow.names)
        elif kind == "anndata" and str(src).endswith(".h5ad"):
            import h5py

            with h5py.File(src, "r") as root:
                order = root["obs"].attrs.get("column-order", [])
                out["columns"] = [c.decode() if isinstance(c, bytes) else str(c) for c in order]
                for key in (root["obsm"].keys() if "obsm" in root else []):
                    shape = getattr(root["obsm"][key], "shape", None)
                    if shape and len(shape) == 2 and shape[1] >= 2:
                        out["positions"] += [f"{key}[:, 0]", f"{key}[:, 1]"]
                x_count = root["X"].attrs.get("shape") if "X" in root else None
                out["n_vars"] = int(x_count[1]) if x_count is not None else None
                out["raw"] = "raw" in root
    except Exception:  # noqa: BLE001 -- no columns rather than a failure
        pass
    return out


def _row(name) -> dict:
    info = ctx.table_of(name)
    if not info.get("has_table"):
        return {"project": name, "has_table": False, "ok": False,
                "status": "No cell table", "missing": ["table"]}
    roles = info["roles"]
    needed = ("cell_id", "x", "y") if info["type"] in ctx.TABULAR else ()
    missing = [r for r in needed if not roles.get(r)]
    if info["type"] in ctx.ANNDATA and not (roles.get("x") and roles.get("y")):
        coords = info.get("coordinates") or {}
        if not coords:
            missing += [r for r in ("x", "y") if not roles.get(r)]
    words = {"cell_id": "Cell ID", "x": "X", "y": "Y", "image_id": "image"}
    if missing:
        names = [words.get(m, m) for m in missing]
        status = (" and ".join(names) if len(names) <= 2 else ", ".join(names)) + " not set"
    else:
        have = [words[r] for r in ("cell_id", "x", "y") if roles.get(r)]
        status = ", ".join(have) + (", image set" if info.get("image") else "")
    return {"project": name, "has_table": True, "ok": not missing and info["supported"],
            "type": info["type"], "kind": {"csv": "CSV", "parquet": "Parquet", "anndata": "AnnData",
                                           "spatialdata": "SpatialData"}.get(info["type"], info["type"]),
            "file": info["file"], "src": info["src"], "cells": cells_in(info),
            "markers": len(info["markers"]), "marker_names": info["markers"],
            "metadata": info.get("metadata") or [], "roles": roles, "missing": missing,
            "status": status if info["supported"] else
            ("Table is on a data node" if info.get("on_node") else f"{info['type']} tables are not supported yet"),
            "pixel_size": info.get("pixel_size"), "image": info.get("image"), "mask": info.get("mask"),
            "log_transformed": info.get("log_transformed")}


def sync(document) -> dict:
    """The notebook's context as Plexora has it now: a dataset's members are
    re-read from the dataset. Returns the (possibly updated) document."""
    context = document.get("context") or {}
    if context.get("kind") != "dataset":
        return document
    try:
        found = ctx.dataset(context["id"])
    except ctx.ContextError:
        return document
    if found["projects"] != context.get("projects") or found["name"] != context.get("name"):
        def change(doc):
            before = list(doc["context"].get("projects") or [])
            doc["context"] = {**doc["context"], "projects": list(found["projects"]), "name": found["name"]}
            if set(before) != set(found["projects"]):
                for cell in doc["cells"]:
                    if cell["tool"] == "prepare" and (cell.get("run") or {}).get("status") == "done":
                        import time

                        cell["stale"] = {"reason": "The dataset's projects changed.", "at": time.time()}
        document = store.mutate(document["id"], change)
    return document


def view(notebook_id) -> dict:
    """Everything the Prepare manager draws."""
    from plexora import api

    document = sync(store.load(notebook_id))
    context = document["context"]
    rows = [_row(name) for name in context["projects"]]
    member = set(context["projects"])
    in_dataset = {}
    for d in ctx.datasets():
        for name in d["projects"]:
            in_dataset[name] = d["name"]
    available = []
    for name in api.list_projects():
        if name in member:
            continue
        info = ctx.table_of(name)
        available.append({"project": name, "has_table": bool(info.get("has_table")),
                          "cells": cells_in(info) if info.get("has_table") else None,
                          "type": info.get("type"), "dataset": in_dataset.get(name)})
    markers = [r.get("marker_names") or [] for r in rows if r.get("has_table")]
    common = [m for m in markers[0] if all(m in other for other in markers[1:])] if markers else []
    extra = sorted({m for group in markers for m in group} - set(common))
    table = samples.get(context)
    from plexora.plugins.scimap.server import notebooks

    work = notebooks.work_of(document)
    images = work.get("images") if work.get("exists") else list(context["projects"])
    return {
        "context": context,
        "rows": rows,
        "available": available,
        "dataset": {"id": context["id"], "name": context["name"]} if context["kind"] == "dataset" else None,
        "markers": {"common": common, "left_out": extra},
        "samples": table,
        "images": images,
        "match": samples.matched(table, images),
        "results_dir": document.get("results_dir"),
        "default_results_dir": ctx.default_results_dir(context),
        "memory": document.get("memory") or "auto",
        "problems": [f"Fix {r['project']}'s columns first" for r in rows if r.get("has_table") and r["missing"]]
                    + [f"{r['project']} has no cell table" for r in rows if not r.get("has_table")],
        "cells": sum(r.get("cells") or 0 for r in rows),
        "stamp": api.records_stamp(),
    }


def project_columns(name) -> dict:
    """The edit form under a project's row."""
    info = ctx.table_of(name)
    if not info.get("has_table"):
        return {"project": name, "has_table": False}
    return {**_row(name), **columns_of(info)}


# -- edits, written to Plexora --------------------------------------------


def _unique_name(base, taken):
    taken = {t.casefold() for t in taken}
    name, i = base, 2
    while name.casefold() in taken:
        name, i = f"{base} {i}", i + 1
    return name


def add_projects(notebook_id, names) -> dict:
    """Add projects to the notebook's data. A project notebook becomes a
    dataset notebook: a dataset is made in Plexora with both."""
    from plexora import api

    document = sync(store.load(notebook_id))
    context = document["context"]
    names = [n for n in names or [] if n not in context["projects"]]
    known = set(api.list_projects())
    unknown = [n for n in names if n not in known]
    if unknown:
        raise RunError("invalid_input", f"There is no project called {unknown[0]!r}.")
    if not names:
        return view(notebook_id)
    try:
        if context["kind"] == "dataset":
            api.update_dataset(context["id"], projects=list(context["projects"]) + names)
            made = ctx.resolve("dataset", context["id"])
        else:
            title = document.get("title") or context["name"]
            base = title if title not in ("Analysis",) and not title.startswith("Analysis ") else f"{context['name']} and more"
            taken = [d["name"] for d in api.list_datasets()]
            dataset = api.create_dataset(_unique_name(base, taken), list(context["projects"]) + names)
            made = ctx.resolve("dataset", dataset["id"])
    except ValueError as exc:
        raise RunError("invalid_input", str(exc)) from exc
    _set_context(notebook_id, made)
    return view(notebook_id)


def remove_project(notebook_id, name) -> dict:
    from plexora import api

    document = sync(store.load(notebook_id))
    context = document["context"]
    if context["kind"] != "dataset" or name not in context["projects"]:
        raise RunError("invalid_input", "Only a dataset's projects can be removed here.")
    rest = [p for p in context["projects"] if p != name]
    if not rest:
        raise RunError("invalid_input", "A dataset keeps at least one project.")
    try:
        api.update_dataset(context["id"], projects=rest)
    except ValueError as exc:
        raise RunError("invalid_input", str(exc)) from exc
    _set_context(notebook_id, ctx.resolve("dataset", context["id"]))
    return view(notebook_id)


def rename_dataset(notebook_id, name) -> dict:
    from plexora import api

    document = store.load(notebook_id)
    context = document["context"]
    name = str(name or "").strip()
    if context["kind"] != "dataset" or not name:
        raise RunError("invalid_input", "Give the dataset a name.")
    try:
        api.update_dataset(context["id"], name=name)
    except ValueError as exc:
        raise RunError("invalid_input", str(exc)) from exc
    _set_context(notebook_id, ctx.resolve("dataset", context["id"]))
    return view(notebook_id)


def _set_context(notebook_id, made):
    import time

    def change(doc):
        before = list(doc["context"].get("projects") or [])
        doc["context"] = made
        if set(before) != set(made["projects"]):
            for cell in doc["cells"]:
                if cell["tool"] == "prepare" and (cell.get("run") or {}).get("status") == "done":
                    cell["stale"] = {"reason": "The dataset's projects changed.", "at": time.time()}
    store.mutate(notebook_id, change)


def edit_project(notebook_id, name, edits) -> dict:
    """Save the edit form to the project itself."""
    from plexora import api

    document = store.load(notebook_id)
    if name not in document["context"]["projects"]:
        raise RunError("invalid_input", f"{name} is not part of this notebook's data.")
    edits = edits or {}
    roles = edits.get("roles")
    if roles is not None:
        roles = {k: (v or None) for k, v in roles.items()
                 if k in ("cell_id", "x", "y", "image_id", "celltype")}
    pixel = edits.get("pixel_size")
    if pixel not in (None, ""):
        try:
            pixel = float(pixel)
        except (TypeError, ValueError) as exc:
            raise RunError("invalid_input", "The pixel size is a number of µm per pixel.") from exc
        if pixel < 0:
            raise RunError("invalid_input", "The pixel size is a number of µm per pixel.")
    else:
        pixel = None
    table = edits.get("table_src")
    if table:
        if not Path(os.path.expanduser(str(table))).is_file():
            raise RunError("invalid_input", "That table file does not exist.")
    try:
        api.update_project(name, roles=roles, markers=edits.get("markers"),
                           metadata=edits.get("metadata"), pixel_size=pixel,
                           table_src=str(Path(os.path.expanduser(table))) if table else None)
    except (KeyError, ValueError) as exc:
        raise RunError("invalid_input", str(exc.args[0] if exc.args else exc)) from exc
    import time

    def change(doc):
        for cell in doc["cells"]:
            if cell["tool"] == "prepare" and (cell.get("run") or {}).get("status") == "done":
                cell["stale"] = {"reason": f"{name}'s data was edited.", "at": time.time()}
    store.mutate(notebook_id, change)
    return view(notebook_id)
