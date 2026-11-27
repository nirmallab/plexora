"""What a notebook does: its edits, its runs, and how its steps relate.

The routes and the agent capabilities both call these functions, so a button
and an agent's call change a notebook in exactly the same way.

A step's state is derived, never typed in: queued or running while it has a
job in the runner; otherwise its last run's status, unless something it read
has changed since ("Input changed"), which is said with the reason and never
acted on without the user. A step whose result another step reads keeps
working as it was when that step ran; nothing reruns by itself.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import secrets
import time

from plexora.plugins.scimap.server import (
    context as ctx, figures, install, licence, modes, overlay, results, runner, store, tools, versions, working)
from plexora.plugins.scimap.server.adapter import RunError


def _version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


#: The shape of a cached result summary; a change here recomputes old ones.
SUMMARY_VERSION = 4


def software() -> dict:
    return {"scimappro": _version("scimappro"), "plexora": _version("plexora"),
            "anndata": _version("anndata"), "numba": _version("numba"),
            "python": platform.python_version()}


# -- creating and reading -------------------------------------------------


def create(kind, ref, title=None) -> dict:
    context = ctx.resolve(kind, ref)
    folder = ctx.default_results_dir(context)
    document = {"title": title or _default_title(context), "context": context,
                "results_dir": folder, "working": None, "cells": []}
    path, _mode = working.target(context, folder)
    if path:
        from pathlib import Path

        if Path(path).is_file():
            document["working"] = {"path": path, "file": Path(path).name,
                                   "results_dir": folder, "mode": _mode, "reused": True}
    document["cells"].append(_new_cell("prepare"))
    return store.create(document)


def _default_title(context):
    existing = {n["title"] for n in store.list_all()
                if (n.get("context") or {}).get("id") == context["id"]}
    base = "Analysis" if context["kind"] == "project" else "Dataset analysis"
    title, i = base, 2
    while title in existing:
        title, i = f"{base} {i}", i + 1
    return title


def _new_cell(tool_id, params=None):
    return {"id": store.new_cell_id(), "tool": tool_id, "params": dict(params or {}),
            "collapsed": False, "run": None, "stale": None, "job_id": None,
            "created_at": time.time()}


def for_context(kind, ref) -> list:
    return [n for n in store.list_all() if (n.get("context") or {}).get("kind") == kind
            and (n.get("context") or {}).get("id") == ref]


def work_of(document) -> dict:
    """The working file as the page and the tools see it: the record Prepare
    left, plus what the file holds now. `exists` False before Prepare."""
    record = dict(document.get("working") or {})
    path = record.get("path")
    if not path:
        return {"exists": False}
    info = working.describe(path)
    if not info.get("exists"):
        return {**record, "exists": False, "missing": True}
    if not record.get("image_column"):
        record.setdefault("image_column", working.IMAGE_COLUMN)
        record.setdefault("x_column", working.X_COLUMN)
        record.setdefault("y_column", working.Y_COLUMN)
    work = {**record, **info}
    # The kind of data (imaging, single-cell RNA, spots, bins) and the unit of
    # its coordinates: which matrix a step reads, what a radius is in.
    return {**work, **modes.work_facts(document, work)}


def view(notebook_id) -> dict:
    """Everything the page draws: the document with each step's derived state,
    the working file, the tools' availability and the licence."""
    from plexora.plugins.scimap.server import data as _data

    document = _data.sync(store.load(notebook_id))
    document = _reconcile(document)
    work = work_of(document)
    status = install.probe()
    roles = tools.column_roles(document, work)
    # SCIMAP Pro's reading of the data: what it is, which analyses fit it, its
    # standard workflow. A tool that does not fit is neither listed nor
    # suggested: it is off, and marked so.
    said = _advice(document, work)
    data_type = {key: (said.get("facts") or {}).get(key) for key in DATA_TYPE_KEYS}
    leave_out = modes.hidden(said)
    availability = {tid: ({"enabled": False, "reason": "", "link": None, "hidden": True} if tid in leave_out
                          else tools.availability(tid, document, work, status)) for tid in tools.TOOLS}
    counts = {}
    for run in store.runs(notebook_id):
        counts[run["cell_id"]] = counts.get(run["cell_id"], 0) + 1
    cells = [_cell_view(c, document, work, roles) for c in document["cells"]]
    for cell in cells:
        # The chip in the head (v3) and the Run button's "Run as v4".
        n = counts.get(cell["id"], 0)
        current = (cell.get("run") or {}).get("version")
        if current is None and cell.get("run") and n:
            known = _versions(notebook_id, cell["id"])
            current = _version_of(known, cell["run"].get("run_id"))
            n = max([r["version"] for r in known] or [n])
        cell["version"] = current
        cell["versions"] = n
        cell["next_version"] = max(n, current or 0) + 1
    return {"notebook": {**document, "cells": cells}, "working": _public_work(work),
            "availability": availability, "roles": roles,
            "catalog": modes.catalog(said), "data_type": data_type, "next": modes.next_steps(said),
            "running": runner.active(notebook_id), "warmed": runner.warmed()}


#: What the page is told about the data: enough to word and list the tools, never the provenance detail.
DATA_TYPE_KEYS = ("mode", "chip", "modality", "resolution", "platform", "coordinate_unit", "microns_per_unit",
                  "bin_size_um", "n_features", "raw_counts", "normalised", "panel", "source")


def _advice(document, work) -> dict:
    """`modes.advice`; when it cannot be read, nothing, which lists every tool as the notebook did before."""
    try:
        return modes.advice(document, work)
    except Exception:  # noqa: BLE001 -- the notebook still opens, with every tool listed
        return {"facts": {}, "states": {}, "recipe": {}}


def _public_work(work):
    hidden = {"calls"}
    return {k: v for k, v in work.items() if k not in hidden}


def _cell_view(cell, document, work, roles):
    out = dict(cell)
    job = runner.get(cell["job_id"]) if cell.get("job_id") else None
    if job and job["status"] in ("queued", "running"):
        out["state"] = job["status"]
        out["job"] = job
    else:
        run = cell.get("run") or {}
        status = run.get("status")
        if status == "done":
            out["state"] = "outdated" if cell.get("stale") else "done"
            if out["state"] == "done" and cell["tool"] == "celltypes":
                changed = _gates_changed(cell, document)
                if changed:
                    out["state"] = "outdated"
                    out["stale"] = {"reason": changed}
        elif status in ("failed", "cancelled"):
            out["state"] = status
        else:
            out["state"] = "idle"
    tool = tools.TOOLS.get(cell["tool"], {})
    if (cell.get("run") or {}).get("effective"):
        out["chips"] = tools.chips(cell["tool"], cell["run"]["effective"], work)
    # The choices of each label field, so the form can offer them.
    out["choices"] = {f["name"]: tools.columns_for(f.get("role"), roles, work)
                      for f in tool.get("fields", []) if f["kind"] == "column"}
    # The column a step reads when none is chosen: the latest one made.
    roles_of = {f["name"]: f.get("role") for f in tool.get("fields", [])}
    out["choice_defaults"] = {name: tools.default_choice(roles_of.get(name), choices, roles, document)
                              for name, choices in out["choices"].items()}
    if cell["tool"] == "celltypes":
        from plexora.plugins.scimap.server import gates

        out["gated"] = sorted(gates.state(document))
    return out


def _gates_changed(cell, document) -> str:
    """Why a Cell types result no longer matches the gates, or ""."""
    from plexora.plugins.scimap.server import gates

    used = ((cell.get("run") or {}).get("effective") or {}).get("gates")
    if not used:
        return ""
    try:
        now = gates.revision(document)
    except Exception:  # noqa: BLE001
        return ""
    if now in (used, cell.get("gates_kept")):
        return ""
    return "The gates changed in Gating since this ran."


def _reconcile(document):
    """A job that no longer exists (the app restarted mid-run) leaves its step
    failed as "interrupted", with its previous result intact."""
    changed = False
    for cell in document["cells"]:
        job_id = cell.get("job_id")
        if job_id and runner.get(job_id) is None:
            cell["job_id"] = None
            run = dict(cell.get("run") or {})
            if run.get("status") in (None, "queued", "running"):
                run.update(status="failed", error={"code": "interrupted",
                                                   "message": "Plexora was closed while this step ran."})
                cell["run"] = run
            changed = True
    if changed:
        document = store.save(document)
    return document


# -- edits ----------------------------------------------------------------


def apply_ops(notebook_id, ops, expected_revision=None) -> dict:
    def change(document):
        for op in ops or []:
            _apply(document, op)
    store.mutate(notebook_id, change, expected_revision)
    for op in ops or []:
        if op.get("_move"):
            # The notebook's own folder follows its results.
            try:
                store.move(notebook_id, op["_move"])
            except OSError:
                pass
    return view(notebook_id)


def _find(document, cell_id):
    for cell in document["cells"]:
        if cell["id"] == cell_id:
            return cell
    raise RunError("invalid_input", "That step no longer exists.")


def _apply(document, op):
    kind = op.get("op")
    if kind == "add_cell":
        tool_id = op.get("tool")
        if tool_id not in tools.TOOLS or tools.TOOLS[tool_id].get("viewer"):
            raise RunError("invalid_input", f"unknown tool {tool_id!r}")
        cell = _new_cell(tool_id, op.get("params"))
        after = op.get("after")
        index = len(document["cells"])
        if after:
            for i, c in enumerate(document["cells"]):
                if c["id"] == after:
                    index = i + 1
        document["cells"].insert(index, cell)
        op["_created"] = cell["id"]
    elif kind == "remove_cell":
        cell = _find(document, op["id"])
        if runner.find(document["id"], cell["id"]):
            raise RunError("invalid_input", "Stop the step before deleting it.")
        label = ((cell.get("run") or {}).get("effective") or {}).get("label")
        document["cells"] = [c for c in document["cells"] if c["id"] != cell["id"]]
        if label:
            _mark_dependents(document, label, f"{tools.TOOLS[cell['tool']]['name']} was deleted.")
            if op.get("drop_column"):
                _drop_column(document, cell, label)
    elif kind == "restore_cell":
        # Undo of a delete: the step comes back as it was, run and all. Its
        # result is still in the working file (a quick delete never drops it).
        cell = op.get("cell") or {}
        if not isinstance(cell, dict) or cell.get("tool") not in tools.TOOLS or not cell.get("id"):
            raise RunError("invalid_input", "That step cannot be restored.")
        if any(c["id"] == cell["id"] for c in document["cells"]):
            raise RunError("invalid_input", "That step is already in the notebook.")
        cell = {k: v for k, v in cell.items() if k not in ("state", "job", "chips", "choices", "stale_view")}
        cell["job_id"] = None
        at = max(0, min(int(op.get("index", len(document["cells"]))), len(document["cells"])))
        document["cells"].insert(at, cell)
        name = tools.TOOLS[cell["tool"]]["name"]
        for other in document["cells"]:
            stale = other.get("stale") or {}
            if stale.get("reason") == f"{name} was deleted.":
                other["stale"] = None
    elif kind == "move_cell":
        cell = _find(document, op["id"])
        cells = [c for c in document["cells"] if c["id"] != cell["id"]]
        to = max(0, min(int(op.get("to", 0)), len(cells)))
        cells.insert(to, cell)
        document["cells"] = cells
    elif kind == "set_params":
        cell = _find(document, op["id"])
        cell["params"] = {**cell.get("params", {}), **(op.get("params") or {})}
        for key in op.get("clear") or []:
            cell["params"].pop(key, None)
    elif kind == "reset_params":
        _find(document, op["id"])["params"] = {}
    elif kind == "set_collapsed":
        cell = _find(document, op["id"]) if op.get("id") else None
        targets = [cell] if cell else document["cells"]
        for c in targets:
            c["collapsed"] = bool(op.get("collapsed"))
    elif kind == "keep":
        kept = _find(document, op["id"])
        kept["stale"] = None
        if kept.get("tool") == "celltypes":
            from plexora.plugins.scimap.server import gates

            kept["gates_kept"] = gates.revision(document)
    elif kind == "set_title":
        title = str(op.get("title") or "").strip()[:120]
        if title:
            document["title"] = title
    elif kind == "set_results_dir":
        document["results_dir"] = working.check_folder(op.get("path"))
        op["_move"] = document["results_dir"]
    elif kind == "set_memory":
        choice = op.get("memory")
        if choice not in ("auto", "memory", "stream"):
            raise RunError("invalid_input", "Memory is Automatic, In memory or Streamed.")
        document["memory"] = choice
    elif kind == "branch":
        # The version's settings become the step's, to change and run as a
        # new version whose parent is that one.
        cell = _find(document, op["id"])
        run = {r["version"]: r for r in _versions(document["id"], cell["id"])}.get(int(op.get("version", 0)))
        if run is None:
            raise RunError("invalid_input", "That version no longer exists.")
        cell["params"] = dict(run.get("params") or {})
        cell["base"] = run["version"]
    elif kind == "copy_version":
        cell = _find(document, op["id"])
        run = {r["version"]: r for r in _versions(document["id"], cell["id"])}.get(int(op.get("version", 0)))
        if run is None:
            raise RunError("invalid_input", "That version no longer exists.")
        params = {k: v for k, v in (run.get("params") or {}).items() if k != "label"}
        new = _new_cell(cell["tool"], params)
        index = next(i for i, c in enumerate(document["cells"]) if c["id"] == cell["id"])
        document["cells"].insert(index + 1, new)
        op["_created"] = new["id"]
    elif kind == "set_views":
        # How a step's result is drawn: display only. It never marks a result
        # outdated and drops no cached figure (each is cached by its view).
        from plexora.plugins.scimap.server import views as _views

        cell = _find(document, op["id"])
        cell["views"] = _views.clean_panels(cell["tool"], op.get("views"))
    elif kind == "set_display":
        cell = _find(document, op["id"])
        run = cell.get("run") or {}
        effective = run.get("effective") or {}
        effective["display"] = {**(effective.get("display") or {}), **(op.get("display") or {})}
        cell["params"] = {**cell.get("params", {}), **(op.get("display") or {})}
        if run:
            store.drop_result_cache(document["id"], run.get("run_id"))
    else:
        raise RunError("invalid_input", f"unknown operation {kind!r}")


def _mark_dependents(document, label, reason):
    for cell in document["cells"]:
        effective = (cell.get("run") or {}).get("effective") or {}
        reads = set((effective.get("inputs") or {}).values())
        reads |= {(effective.get("display") or {}).get("color")}
        if label in reads and (cell.get("run") or {}).get("status") == "done":
            cell["stale"] = {"reason": reason, "at": time.time(), "input": label}


def _drop_column(document, cell, label):
    work = work_of(document)
    if not work.get("exists"):
        return
    still = any(label in set(((c.get("run") or {}).get("effective") or {}).get("inputs", {}).values())
                for c in document["cells"])
    if still:
        return
    from plexora.plugins.scimap.server.adapter import drop_result

    def go(job):
        return {"dropped": drop_result(work["path"], label)}

    runner.submit(runner.Job("drop", go, notebook_id=document["id"], label=f"Removing {label}"))


# -- runs -----------------------------------------------------------------


def _busy(notebook_id, cell):
    """The step's own run while it is queued or running. A finished run's job
    that is still drawing its figures does not count: the step is done."""
    job = runner.find(notebook_id, cell["id"])
    return job if job is not None and job.id == cell.get("job_id") else None


def run_cell(notebook_id, cell_id, *, sample=None, initiated_by="viewer") -> dict:
    document = store.load(notebook_id)
    cell = _find(document, cell_id)
    if _busy(notebook_id, cell):
        raise RunError("invalid_input", "This step is already queued.")
    status = install.probe()
    if not status["compatible"]:
        raise RunError("not_installed", "Scimap Pro is not installed here." if not status["installed"]
                       else f"Scimap Pro {status['version']} is older than this notebook needs.")
    tool_id = cell["tool"]
    # Checked now so a refusal reaches the button at once -- unless another
    # step of this notebook is queued ahead, whose result this one may read
    # (Run all): then the arguments are settled when the step starts.
    ahead = any(j["cell_id"] != cell_id for j in runner.active(notebook_id))
    if not ahead:
        _effective_now(document, cell_id, tool_id, sample)
    run_id = "run_" + secrets.token_hex(6)
    known = _versions(notebook_id, cell_id)
    # A new version comes from the one on show (the last good result), not
    # from a failed attempt after it -- unless the user branched from one.
    shown = _shown_run(cell) or {}
    current = shown.get("version") or _version_of(known, shown.get("run_id"))
    stamp = {"version": max([r["version"] for r in known] or [0]) + 1,
             "parent": cell.get("base") or current,
             "params": dict(cell.get("params") or {})}

    def work_fn(job):
        try:
            effective = _effective_now(store.load(notebook_id), cell_id, tool_id, sample)
        except RunError as exc:
            now = time.time()
            _finish(notebook_id, cell_id, {"run_id": run_id, "cell_id": cell_id, "tool": tool_id,
                                           "started_at": now, "finished_at": now, "status": "failed",
                                           "error": exc.to_dict(), **stamp})
            raise
        return _execute(notebook_id, cell_id, run_id, tool_id, effective, job, stamp)

    job = runner.Job("run", work_fn, notebook_id=notebook_id, cell_id=cell_id,
                     label=tools.TOOLS[tool_id]["name"])

    def mark(document):
        c = _find(document, cell_id)
        c["job_id"] = job.id
        c["collapsed"] = False
        c.pop("base", None)
        if not c.get("folder"):
            index = next(i for i, x in enumerate(document["cells"]) if x["id"] == cell_id)
            c["folder"] = versions.step_folder(store.notebook_dir(notebook_id), c, index)
    store.mutate(notebook_id, mark)
    runner.submit(job)
    return {"job_id": job.id, "run_id": run_id}


def _effective_now(document, cell_id, tool_id, sample=None) -> dict:
    """The step's exact call, from the notebook as it is now, or a refusal."""
    cell = _find(document, cell_id)
    work = work_of(document)
    if tool_id == "prepare":
        folder = document.get("results_dir")
        planned = working.plan(document["context"], folder)
        if planned["problems"]:
            raise RunError("invalid_input", " ".join(planned["problems"]))
        params = cell.get("params") or {}
        return {"key": "io.readIntensityTable", "kwargs": {}, "label": None, "inputs": {},
                "results_dir": folder, "sample": sample,
                "options": {"markers": params.get("markers") or None, "log": params.get("log"),
                            "metadata": _sample_metadata(document)}}
    if not work.get("exists"):
        raise RunError("precondition_missing", "Prepare the data first.")
    try:
        effective = tools.effective(tool_id, cell.get("params"), work, document, cell_id)
    except ValueError as exc:
        raise RunError("invalid_input", str(exc)) from exc
    cap = licence.max_cells()
    if cap and work.get("n_cells", 0) > cap and effective.get("key"):
        raise RunError("license_required",
                       f"Scimap Pro Starter analyzes up to {cap:,} cells; this table has "
                       f"{work['n_cells']:,}. Add a license, or prepare a sample of the cells.",
                       {"reason": "limit_exceeded", "max_cells": cap, "n_cells": work["n_cells"]})
    return effective


def run_all(notebook_id) -> list:
    document = store.load(notebook_id)
    queued = []
    for cell in document["cells"]:
        state = _cell_view(cell, document, {}, {})["state"]
        if state in ("idle", "failed", "outdated", "cancelled"):
            try:
                queued.append(run_cell(notebook_id, cell["id"]))
            except RunError as exc:
                queued.append({"cell_id": cell["id"], "error": exc.to_dict()})
                break
    return queued


def cancel_cell(notebook_id, cell_id) -> bool:
    job = runner.find(notebook_id, cell_id)
    return runner.cancel(job.id) if job else False


def _execute(notebook_id, cell_id, run_id, tool_id, effective, job, stamp=None):
    """On the runner thread: run one step and record what happened."""
    from plexora.plugins.scimap.server import adapter

    started = time.time()
    document = store.load(notebook_id)
    cell = _find(document, cell_id)
    record = {"run_id": run_id, "cell_id": cell_id, "tool": tool_id, "started_at": started,
              "status": "running", "effective": effective, "software": software(),
              "licence": licence.state()["tier"], **(stamp or {}),
              "identity": _identity(document, effective), "inputs_at": _inputs_at(document, effective)}
    store.put_run(notebook_id, record)
    if stamp:
        versions.write_params(_version_dir(notebook_id, cell, stamp["version"]), record)
    reused = None
    try:
        if tool_id != "prepare" and effective.get("key") and effective.get("label"):
            reused = _reuse(notebook_id, cell, record, job)
        if reused is not None:
            outcome = reused
        elif tool_id == "prepare":
            document = store.load(notebook_id)
            target = (document.get("working") or {}).get("path")
            with working.writing(target):
                made = working.prepare(document["context"], effective["results_dir"],
                                       sample=effective.get("sample"), say=job.say,
                                       options=effective.get("options"))
                # Prepare always ends by asking Scimap Pro how it will read the
                # file; every later step opens it with the same setting.
                job.say("Asking Scimap Pro how to read it")
                from plexora.plugins.scimap.server import memory as _memory

                try:
                    made["reading"] = _memory.report(made["path"], document.get("memory") or "auto")
                except Exception as exc:  # noqa: BLE001 -- the card says it could not ask
                    made["reading"] = {"error": str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__}
            outcome = {"outputs": [], "experimental_unit": None, "warnings": [], "working": made}
        elif effective.get("key"):
            document = store.load(notebook_id)
            work = work_of(document)
            with working.writing(work["path"]):
                _keep_current(notebook_id, _find(document, cell_id), work["path"], job)
                # Calls the step makes first (Check quality measures before it
                # flags; Cell types from gates rescales a copy of the raw values;
                # Normalize scales the counts before it logs them).
                for step in effective.get("before") or []:
                    if job.cancel_requested:
                        raise runner.Cancelled()
                    job.say(f"Running {step['key']}")
                    adapter.execute(work["path"], step["key"], step["kwargs"], label=step.get("label"),
                                    say=job.say, run_id=run_id, memory=document.get("memory") or "auto",
                                    writes=step.get("writes") or ())
                outcome = adapter.execute(work["path"], effective["key"], effective["kwargs"],
                                          label=effective.get("label"), say=job.say, run_id=run_id,
                                          memory=document.get("memory") or "auto")
            if job.cancel_requested and outcome.get("mode") == "out-of-core" and effective.get("label"):
                adapter.drop_result(work["path"], effective["label"])
                raise runner.Cancelled()
        else:
            job.say("Summarizing")
            outcome = {"outputs": [], "experimental_unit": None, "warnings": []}
    except runner.Cancelled:
        _finish(notebook_id, cell_id, {**record, "status": "cancelled", "finished_at": time.time()})
        raise
    except RunError as exc:
        _finish(notebook_id, cell_id, {**record, "status": "failed", "error": exc.to_dict(),
                                       "finished_at": time.time()})
        raise
    except Exception as exc:  # noqa: BLE001
        _finish(notebook_id, cell_id, {**record, "status": "failed", "finished_at": time.time(),
                                       "error": {"code": "internal_error",
                                                 "message": str(exc).splitlines()[0][:500] if str(exc) else type(exc).__name__}})
        raise
    finished = time.time()
    done = {**record, "status": "done", "finished_at": finished,
            "seconds": round(finished - started, 2),
            "outputs": outcome.get("outputs"), "warnings": outcome.get("warnings"),
            "mode": outcome.get("mode"), "experimental_unit": outcome.get("experimental_unit"),
            "cache": outcome.get("cache") or {"kind": "computed"},
            "result_of": outcome.get("result_of") or run_id}
    effective["experimental_unit"] = outcome.get("experimental_unit")
    if tool_id == "prepare":
        done["working"] = {k: v for k, v in outcome["working"].items() if k != "columns"}
    _finish(notebook_id, cell_id, done)
    # The summary and figures now, on this thread and while the file is warm,
    # so the page draws at once.
    try:
        summary(notebook_id, cell_id)
        _build_figures(notebook_id, cell_id)
    except Exception:  # noqa: BLE001 -- drawn on demand instead
        pass
    _notify(notebook_id, effective.get("label"))
    return {"run_id": run_id}


def _finish(notebook_id, cell_id, run):
    store.put_run(notebook_id, run)

    def change(document):
        cell = _find(document, cell_id)
        cell["job_id"] = None
        previous = cell.get("run") or {}
        if run["status"] == "done":
            cell["run"] = run
            cell["stale"] = None
            if cell["tool"] == "prepare":
                # Done once, read rarely: the design folds it to one line.
                cell["collapsed"] = True
                document["working"] = run.get("working")
                document["results_dir"] = (run.get("working") or {}).get("results_dir") or document.get("results_dir")
                for other in document["cells"]:
                    if other["id"] != cell_id and (other.get("run") or {}).get("status") == "done":
                        other["stale"] = {"reason": "The data was prepared again.", "at": time.time()}
            else:
                label = (run.get("effective") or {}).get("label")
                same = run.get("result_of") and run.get("result_of") == previous.get("result_of", previous.get("run_id"))
                if label and previous.get("status") == "done" and not same:
                    name = tools.TOOLS[cell["tool"]]["name"]
                    stamp = time.strftime("%H:%M", time.localtime(run["finished_at"]))
                    for other in document["cells"]:
                        if other["id"] == cell_id:
                            continue
                        eff = (other.get("run") or {}).get("effective") or {}
                        reads = set((eff.get("inputs") or {}).values()) | {(eff.get("display") or {}).get("color")}
                        if label in reads and (other.get("run") or {}).get("status") == "done":
                            other["stale"] = {"reason": f"{name} was rerun at {stamp}.", "at": time.time(),
                                              "input": label}
        else:
            # A failed or cancelled run keeps the previous result on show.
            kept = dict(previous) if previous.get("status") == "done" else {}
            cell["run"] = {**run, "previous": kept} if kept else run
    store.mutate(notebook_id, change)


def _notify(notebook_id, label):
    """Tell open viewers a column changed, so an overlay of it redraws."""
    record = overlay.get()
    if not record or record.get("notebook_id") != notebook_id:
        return
    try:
        from plexora import api

        for project in record.get("projects") or []:
            api.notify_viewers(project, "core", "dataset.changed",
                               {"sections": [f"obs:{overlay.ANNOTATION}"]})
    except Exception:  # noqa: BLE001
        pass


# -- results --------------------------------------------------------------


class _reading:
    """`working.reading`, refusing with a plain reason when a run holds the file."""

    def __init__(self, path):
        self._inner = working.reading(path)

    def __enter__(self):
        try:
            self._inner.__enter__()
        except TimeoutError as exc:
            raise RunError("busy", "A step is writing the working file; try again when it finishes.") from exc
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def _shown_run(cell):
    run = cell.get("run") or {}
    if run.get("status") == "done":
        return run
    previous = run.get("previous") or {}
    return previous if previous.get("status") == "done" else None


def summary(notebook_id, cell_id, refresh=False) -> dict:
    document = store.load(notebook_id)
    cell = _find(document, cell_id)
    run = _shown_run(cell)
    if run is None:
        raise RunError("invalid_input", "This step has no result yet.")
    if not refresh:
        cached = store.get_result(notebook_id, run["run_id"])
        if cached is not None and cached.get("v") == SUMMARY_VERSION:
            return cached
    work = work_of(document)
    if cell["tool"] == "prepare":
        body = {"working": _public_work({k: v for k, v in work.items() if k != "columns"}),
                "unit": None}
    else:
        if not work.get("exists"):
            raise RunError("precondition_missing", "The working file is missing.")
        effective = dict(run.get("effective") or {})
        effective["experimental_unit"] = run.get("experimental_unit")
        if cell["tool"] == "umap":
            effective["display"] = {"color": _umap_color(cell, document, work)}
        try:
            body = results.summarize(cell["tool"], effective, work["path"], work)
        except KeyError as exc:
            raise RunError("precondition_missing",
                           f"The result {exc.args[0] if exc.args else ''} is no longer in the working file. "
                           "Run the step again.") from exc
    body["run_id"] = run["run_id"]
    body["v"] = SUMMARY_VERSION
    store.put_result(notebook_id, run["run_id"], body)
    return body


# -- figures (Scimap Pro's own plots) ----------------------------------------


def _figure_inputs(notebook_id, cell_id):
    document = store.load(notebook_id)
    cell = _find(document, cell_id)
    run = _shown_run(cell)
    if run is None or cell["tool"] == "prepare":
        raise RunError("invalid_input", "This step has no figures.")
    work = work_of(document)
    if not work.get("exists"):
        raise RunError("precondition_missing", "The working file is missing.")
    effective = dict(run.get("effective") or {})
    display = {"color": _umap_color(cell, document, work)} if cell["tool"] == "umap" else {}
    return cell, run, work, effective, display


def _set_key(run, cell, display):
    from plexora.plugins.scimap.server import views as _views

    return figures.key(run["run_id"], {"display": display, "panels": _views.clean_panels(cell["tool"], cell.get("views"))})


def _view_key(run, view, opts, display):
    return figures.key(run["run_id"], {"display": display, "view": view, "opts": opts or {}})


def _catalogue(cell, effective, work, display):
    from plexora.plugins.scimap.server import views as _views

    c = _views.context(cell["tool"], effective, work, display)
    return _views.catalogue(cell["tool"], c), _views.panels_of(cell, c)


def _build_figures(notebook_id, cell_id) -> dict:
    """On the runner thread: build (or reuse) one step's panels. Each panel is
    cached by its own view and settings, so a view drawn once (a switch back
    and forth, a panel added) is not drawn again."""
    cell, run, work, effective, display = _figure_inputs(notebook_id, cell_id)
    key = _set_key(run, cell, display)
    cached = store.get_result(notebook_id, key)
    if cached is not None:
        return cached
    catalogue, panels = _catalogue(cell, effective, work, display)
    out = []
    for panel in panels:
        entry = _drawn(notebook_id, cell, run, work, effective, display, panel["view"], panel["opts"])
        out.append({"name": panel["id"], "opts": panel["opts"], **entry})
    body = {"run_id": run["run_id"], "display": display, "catalogue": catalogue, "figures": out}
    store.put_result(notebook_id, key, body)
    return body


def _drawn(notebook_id, cell, run, work, effective, display, view, opts, compute=False):
    """One view, from the cache or drawn here (on the runner thread)."""
    key = _view_key(run, view, opts, display)
    cached = store.get_result(notebook_id, key)
    if cached is not None:
        return cached
    data = None
    if compute:
        data = figures.computed(run["run_id"], cell["tool"], effective, work["path"], work)
    elif run["run_id"] in figures._computed:
        data = figures._computed[run["run_id"]]
    entry = figures.draw(cell["tool"], effective, work["path"], work, view, opts, display, data=data)
    if not entry.get("needs_compute") and not entry.get("error"):
        store.put_result(notebook_id, key, entry)
    return entry


def figures_for(notebook_id, cell_id, timeout=180) -> dict:
    """One step's figures, from the cache or drawn next on the runner."""
    cell, run, _work, _eff, display = _figure_inputs(notebook_id, cell_id)
    cached = store.get_result(notebook_id, _set_key(run, cell, display))
    if cached is not None:
        return cached
    return _on_runner("figures", lambda job: _build_figures(notebook_id, cell_id),
                      notebook_id, cell_id, timeout)


def view_for(notebook_id, cell_id, view, opts=None, compute=False, timeout=180) -> dict:
    """One view of a step's result with its settings, drawn from what the run
    stored: switching a view never runs the analysis again. `compute` makes
    what a compute view needs (Across radii) in memory first."""
    from plexora.plugins.scimap.server import views as _views

    cell, run, work, effective, display = _figure_inputs(notebook_id, cell_id)
    opts = _views.clean_opts(opts)
    if _views.find(cell["tool"], view) is None:
        raise RunError("invalid_input", f"This step has no view {view!r}.")
    cached = store.get_result(notebook_id, _view_key(run, view, opts, display))
    if cached is not None:
        return cached
    return _on_runner("view", lambda job: _drawn(notebook_id, cell, run, work, effective, display,
                                                 view, opts, compute=compute),
                      notebook_id, cell_id, timeout)


def figure_file(notebook_id, cell_id, name, fmt, view=None, opts=None, timeout=180) -> tuple[bytes, str]:
    """One figure for a paper: matplotlib through Scimap Pro, as PNG/PDF/SVG.
    `view` and `opts` draw the view as the page shows it now."""
    from plexora.plugins.scimap.server import views as _views

    cell, run, work, effective, display = _figure_inputs(notebook_id, cell_id)
    opts = _views.clean_opts(opts)

    def draw(job):
        try:
            data = figures._computed.get(run["run_id"]) if view == "radii" else None
            if view == "radii" and data is None:
                data = figures.computed(run["run_id"], cell["tool"], effective, work["path"], work)
            return figures.export(cell["tool"], effective, work["path"], work, name, fmt, display,
                                  cell=cell, view=view, opts=opts, data=data)
        except KeyError as exc:
            raise RunError("invalid_input", f"This step has no figure {name!r}.") from exc
        except ValueError as exc:
            raise RunError("invalid_input", str(exc)) from exc

    return _on_runner("figure_file", draw, notebook_id, cell_id, timeout)


def _on_runner(kind, work_fn, notebook_id, cell_id, timeout):
    job = runner.submit(runner.Job(kind, work_fn, notebook_id=notebook_id, cell_id=cell_id,
                                   label="Drawing"), front=True)
    state = runner.wait(job.id, timeout) or {}
    if state.get("status") != "done":
        error = state.get("error") or {}
        if state.get("status") in ("queued", "running"):
            raise RunError("busy", "Another step is running; the figure is drawn when it finishes.")
        raise RunError(error.get("code") or "internal_error",
                       error.get("message") or "The figure could not be drawn.")
    return job.result


def column_summary(notebook_id, name) -> dict:
    """Sizes and highest markers of each group of one label column, for the
    annotation and merge tables."""
    document = store.load(notebook_id)
    work = work_of(document)
    if not work.get("exists"):
        raise RunError("precondition_missing", "Prepare the data first.")
    if name not in {c["name"] for c in work.get("columns") or []}:
        raise RunError("invalid_input", f"The working file has no column {name!r}.")
    with _reading(work["path"]):
        return results.clean(results.labels_summary(work["path"], name, work))


def _umap_color(cell, document, work):
    roles = tools.column_roles(document, work)
    shown = _shown_run(cell) or {}
    chosen = (cell.get("params") or {}).get("color") or \
        ((shown.get("effective") or {}).get("display") or {}).get("color")
    if chosen and chosen in roles:
        return chosen
    choices = tools.columns_for("labels", roles)
    return tools._latest(choices, document) if choices else None


def color_codes(notebook_id, cell_id, column) -> dict:
    document = store.load(notebook_id)
    cell = _find(document, cell_id)
    run = _shown_run(cell)
    work = work_of(document)
    if run is None or not work.get("exists"):
        raise RunError("invalid_input", "This step has no result yet.")
    label = (run.get("effective") or {}).get("label")
    scatter = results.scatter(work["path"], label)  # same sample, same order
    import numpy as np

    n = scatter["n_total"]
    idx = np.arange(n)
    if scatter["sampled"]:
        rng = np.random.default_rng(0)
        idx = np.sort(rng.choice(n, results.SCATTER_MAX, replace=False))
    with _reading(work["path"]):
        return results.clean(results.color_codes(work["path"], column, idx))


def lasso(notebook_id, cell_id, polygon, color=None) -> dict:
    import numpy as np

    document = store.load(notebook_id)
    cell = _find(document, cell_id)
    run = _shown_run(cell)
    work = work_of(document)
    if run is None or not work.get("exists"):
        raise RunError("invalid_input", "This step has no result yet.")
    label = (run.get("effective") or {}).get("label")
    with _reading(work["path"]):
        out, inside = results.lasso(work["path"], label, polygon, color)
        images = results.obs_column(work["path"], work.get("image_column") or "imageid").astype(str)
    path = store.notebook_dir(notebook_id) / f"selection_{cell_id}.npy"
    np.save(path, np.flatnonzero(inside))
    per = {}
    for image in np.unique(images[inside]).tolist():
        per[image] = int((images[inside] == image).sum())
    out["per_project"] = per
    return results.clean(out)


# -- the viewer -----------------------------------------------------------


def show_in_viewer(notebook_id, cell_id, *, focus=None, selection=False, project=None) -> dict:
    """Record what to colour, and where to open the viewer."""
    document = store.load(notebook_id)
    cell = _find(document, cell_id)
    run = _shown_run(cell)
    work = work_of(document)
    if not work.get("exists"):
        raise RunError("invalid_input", "Prepare the data first.")
    tool_id = cell["tool"]
    effective = (run or {}).get("effective") or {}
    column = None
    if tool_id in tools.LABEL_TOOLS:
        column = effective.get("label")
    elif tool_id in ("composition", "expression", "distance", "interaction", "markers", "compare"):
        column = (effective.get("inputs") or {}).get("source")
    elif tool_id == "umap":
        column = _umap_color(cell, document, work)
    if run is None and tool_id != "prepare":
        raise RunError("invalid_input", "Run this step first.")
    context = document["context"]
    projects = list(context["projects"])
    record = {"notebook_id": notebook_id, "cell_id": cell_id, "path": work["path"],
              "image_column": work.get("image_column") or "imageid",
              "cell_column": work.get("cell_column"), "projects": projects,
              "context": {"kind": context["kind"], "id": context["id"], "name": context["name"]},
              "title": f"{tools.TOOLS[tool_id]['name']}" + (f" ({column})" if column else ""),
              "column": column, "focus": [str(f) for f in focus] if focus else None, "kind": "labels"}
    if selection:
        path = store.notebook_dir(notebook_id) / f"selection_{cell_id}.npy"
        if not path.is_file():
            raise RunError("invalid_input", "Draw a selection first.")
        record.update(kind="selection", selection=str(path), title="Selected cells")
    if column is None and not selection:
        overlay.clear()
        record = None
    else:
        overlay.put(record)
    if project and project in projects:
        target = project
    elif selection:
        target = _project_with_most_selected(record, projects)
    else:
        target = ctx.last_viewed(projects) if context["kind"] == "dataset" else projects[0]
    return {"project": target, "overlay": overlay.public(record) if record else None,
            "annotation": overlay.ANNOTATION if record else None}


def _project_with_most_selected(record, projects):
    import numpy as np

    try:
        idx = np.load(record["selection"])
        with _reading(record["path"]):
            images = results.obs_column(record["path"], record["image_column"]).astype(str)[idx]
        values, counts = np.unique(images, return_counts=True)
        best = str(values[int(np.argmax(counts))])
        return best if best in projects else ctx.last_viewed(projects)
    except Exception:  # noqa: BLE001
        return ctx.last_viewed(projects)


# -- versions -------------------------------------------------------------
#
# Every run of a step is a version. The working file holds the current
# version's result; a version a newer run replaced is kept in its folder
# (server/versions.py) and can be made current again, branched from, or
# copied to a new step. A run whose call and inputs equal a kept version's is
# not computed again: that version's result is put back ("Reused v2's
# result"). Nothing in the history is deleted when an upstream step changes:
# the steps that read it say Outdated, with their results kept.


def _versions(notebook_id, cell_id) -> list:
    """The step's runs, oldest first, each with its version number (runs made
    before versions existed are numbered in order)."""
    out = []
    for i, run in enumerate(store.runs(notebook_id, cell_id), start=1):
        run = dict(run)
        run.setdefault("version", i)
        out.append(run)
    seen, numbered = set(), []
    for run in out:  # a legacy number may collide with a stamped one
        while run["version"] in seen:
            run["version"] += 1
        seen.add(run["version"])
        numbered.append(run)
    return numbered


def _version_of(known, run_id):
    for run in known:
        if run["run_id"] == run_id:
            return run["version"]
    return None


def _version_dir(notebook_id, cell, version):
    folder = cell.get("folder") or versions.step_folder(store.notebook_dir(notebook_id), cell, 0)
    return versions.version_dir(store.notebook_dir(notebook_id), folder, version)


def _producer(document, label):
    """The step whose current result is the column `label`, and that run."""
    for cell in document["cells"]:
        run = cell.get("run") or {}
        if run.get("status") == "done" and (run.get("effective") or {}).get("label") == label:
            return cell, run
    return None, None


def _prepare_run(document):
    for cell in document["cells"]:
        if cell["tool"] == "prepare" and (cell.get("run") or {}).get("status") == "done":
            return cell["run"]
    return {}


def _identity(document, effective) -> str:
    """What makes two runs the same: the call, and exactly which results it
    read (the prepared data, and the version behind each input column)."""
    inputs = {}
    for name in sorted(set((effective.get("inputs") or {}).values())):
        _cell, run = _producer(document, name)
        inputs[name] = (run or {}).get("result_of") or (run or {}).get("run_id")
    body = {"key": effective.get("key"), "kwargs": effective.get("kwargs"), "inputs": inputs,
            "advanced": effective.get("advanced"), "before": effective.get("before") or None,
            "gates": effective.get("gates"),
            "data": _prepare_run(document).get("run_id")}
    return hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _inputs_at(document, effective) -> dict:
    prepared = _prepare_run(document)
    out = {"data": {"version": prepared.get("version") or 1, "at": prepared.get("finished_at"),
                    "file": (document.get("working") or {}).get("file")} if prepared else None,
           "columns": {}}
    for name in sorted(set((effective.get("inputs") or {}).values())):
        cell, run = _producer(document, name)
        if cell is not None:
            out["columns"][name] = {"step": tools.TOOLS[cell["tool"]]["name"],
                                    "version": run.get("version"), "at": run.get("finished_at")}
    return out


def _keep_current(notebook_id, cell, path, job=None):
    """Before a new version replaces the current one in the working file, copy
    the current one's result into its folder (once)."""
    current = cell.get("run") or {}
    if current.get("status") != "done":
        current = current.get("previous") or {}
    label = (current.get("effective") or {}).get("label")
    if current.get("status") != "done" or not label or not (current.get("effective") or {}).get("key"):
        return
    version = current.get("version") or _version_of(_versions(notebook_id, cell["id"]), current["run_id"])
    dest = _version_dir(notebook_id, cell, version)
    if versions.is_saved(dest):
        return
    if job is not None:
        job.say(f"Keeping v{version}")
    if not (dest / versions.PARAMS).is_file():
        versions.write_params(dest, {**current, "version": version})
    versions.save(path, versions.specs_of(current), label, dest)


def _reuse(notebook_id, cell, record, job):
    """The outcome of a kept version with this run's identity, put back into
    the working file -- or None when there is none and the step must run."""
    known = [r for r in _versions(notebook_id, cell["id"])
             if r.get("status") == "done" and r.get("identity") == record["identity"]
             and r["run_id"] != record["run_id"]]
    if not known:
        return None
    current = cell.get("run") or {}
    document = store.load(notebook_id)
    work = work_of(document)
    match = next((r for r in known if r["run_id"] == current.get("run_id")), None)
    if match is not None and not cell.get("stale") and current.get("status") == "done":
        source = match  # the working file already holds it
    else:
        source = None
        for run in reversed(known):
            dest = _version_dir(notebook_id, cell, run["version"])
            if versions.is_saved(dest):
                source = run
                break
        if source is None:
            return None
        job.say(f"Reusing v{source['version']}")
        with working.writing(work["path"]):
            _keep_current(notebook_id, cell, work["path"], job)
            try:
                versions.restore(work["path"], _version_dir(notebook_id, cell, source["version"]))
            except versions.Mismatch:
                return None
    return {"outputs": source.get("outputs"), "warnings": source.get("warnings") or [],
            "mode": source.get("mode"), "experimental_unit": source.get("experimental_unit"),
            "cache": {"kind": "reused", "from": source["version"]},
            "result_of": source.get("result_of") or source["run_id"]}


def _describe_change(run, parent, work) -> str:
    """What a version changed from the one it came from, in the form's words."""
    tool_id = run.get("tool")
    mine = tools.chips(tool_id, run.get("effective") or {}, work) if run.get("effective") else []
    if parent is None or not parent.get("effective"):
        return " · ".join(mine[:4])
    their_list = tools.chips(tool_id, parent.get("effective") or {}, work)
    theirs = set(their_list)
    changed = [c for c in mine if c not in theirs and not c.startswith("Seed")] or \
        [c for c in mine if c not in theirs]
    gone = [c.split(" = ")[0] + " back to default" for c in their_list if " = " in c and c not in mine]
    return " · ".join((changed + gone)[:3]) or "Same settings"


def _call_text(effective) -> str:
    key = effective.get("key")
    if not key:
        return ""
    def short(v):
        if isinstance(v, list) and len(v) > 4:
            return f"[{len(v)} markers]" if all(isinstance(x, str) for x in v) else f"[{len(v)} items]"
        return repr(v)
    args = ", ".join(f"{k}={short(v)}" for k, v in (effective.get("kwargs") or {}).items())
    return f"sm.{key}(adata, {args})"


def history(notebook_id, cell_id) -> dict:
    """The History panel: every version of one step, newest first in the tree
    the page draws (children under their parent)."""
    document = store.load(notebook_id)
    cell = _find(document, cell_id)
    work = work_of(document)
    known = _versions(notebook_id, cell_id)
    by_version = {r["version"]: r for r in known}
    current = (cell.get("run") or {}).get("run_id")
    base = store.notebook_dir(notebook_id)
    rows = []
    for run in known:
        parent = by_version.get(run.get("parent"))
        dest = _version_dir(notebook_id, cell, run["version"])
        status = run.get("status")
        cache = run.get("cache") or {}
        if status == "done" and cache.get("kind") == "reused":
            how = f"Reused v{cache.get('from')}'s result"
        elif status == "done":
            how = f"Computed in {_seconds(run.get('seconds'))}"
        elif status == "cancelled":
            how = f"Stopped after {_seconds((run.get('finished_at') or 0) - (run.get('started_at') or 0))}"
        elif status == "failed":
            how = (run.get("error") or {}).get("message") or "Failed"
        else:
            how = "Running"
        is_current = run["run_id"] == current
        held = is_current or versions.is_saved(dest)
        restorable = status == "done" and cell["tool"] != "prepare" and bool(
            (run.get("effective") or {}).get("key")) and (is_current or versions.is_saved(dest))
        rows.append({
            "version": run["version"], "parent": run.get("parent"), "run_id": run["run_id"],
            "status": status, "current": is_current, "how": how,
            "change": _describe_change(run, parent, work) if run.get("effective") else "",
            "extra": [f"{k} = {v!r}" for k, v in ((run.get("effective") or {}).get("extra") or {}).items()],
            "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
            "seconds": run.get("seconds"), "call": _call_text(run.get("effective") or {}),
            "inputs": run.get("inputs_at"), "params": run.get("params") or {},
            "kept": _relative(dest, document) if held and not is_current else None,
            "bytes": versions.size(dest) if versions.is_saved(dest) else None,
            "in_file": is_current, "restorable": restorable,
            "result": _result_line(notebook_id, run),
            "software": (run.get("software") or {}).get("scimappro"),
            "error": run.get("error"),
        })
    return {"cell_id": cell_id, "tool": cell["tool"], "versions": rows,
            "current": next((r["version"] for r in rows if r["current"]), None),
            "folder": _relative(base / (cell.get("folder") or ""), document) if cell.get("folder") else None}


def _seconds(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "?"
    return f"{value:.0f} s" if value >= 1 else f"{value:.1f} s"


def _relative(path, document):
    """A version folder as the user knows it: under the results folder."""
    from pathlib import Path

    folder = document.get("results_dir") or ""
    try:
        rel = Path(path).resolve().relative_to(Path(folder).resolve().parent)
        return str(rel) + "/"
    except (ValueError, OSError):
        return str(path)


def _result_line(notebook_id, run) -> str | None:
    cached = store.get_result(notebook_id, run["run_id"]) or {}
    groups = cached.get("groups") or cached.get("clusters")
    if isinstance(groups, list) and groups:
        sizes = sorted((g.get("pct") or 0 for g in groups if isinstance(g, dict)), reverse=True)
        if sizes:
            return f"{len(groups)} groups · largest {sizes[0]:.1f}%, smallest {sizes[-1]:.1f}%"
        return f"{len(groups)} groups"
    return cached.get("headline")


def make_current(notebook_id, cell_id, version, timeout=600) -> dict:
    """Put a kept version's result back into the working file and make it the
    step's current version. Nothing is recomputed; the steps that read the
    step's column say Outdated, with their results kept."""
    document = store.load(notebook_id)
    cell = _find(document, cell_id)
    if _busy(notebook_id, cell):
        raise RunError("invalid_input", "Wait for the step to finish first.")
    known = {r["version"]: r for r in _versions(notebook_id, cell_id)}
    run = known.get(int(version))
    if run is None or run.get("status") != "done":
        raise RunError("invalid_input", "Only a finished version can be made current.")
    if run["run_id"] == (cell.get("run") or {}).get("run_id"):
        return view(notebook_id)
    if cell["tool"] == "prepare" or not (run.get("effective") or {}).get("key"):
        # Nothing in the file to put back: the version's settings become the
        # step's, to run again.
        raise RunError("invalid_input", "This version's result is not kept. Branch from it and run it.")
    dest = _version_dir(notebook_id, cell, run["version"])
    if not versions.is_saved(dest):
        raise RunError("invalid_input", "This version's result is not kept. Branch from it and run it.")

    def work_fn(job):
        doc = store.load(notebook_id)
        c = _find(doc, cell_id)
        work = work_of(doc)
        with working.writing(work["path"]):
            _keep_current(notebook_id, c, work["path"], job)
            job.say(f"Putting back v{run['version']}")
            try:
                versions.restore(work["path"], dest)
            except versions.Mismatch as exc:
                raise RunError("invalid_input", str(exc)) from exc
        return True

    job = runner.submit(runner.Job("restore", work_fn, notebook_id=notebook_id, cell_id=cell_id,
                                   label=f"Making v{run['version']} current"))
    state = runner.wait(job.id, timeout) or {}
    if state.get("status") != "done":
        error = state.get("error") or {}
        raise RunError(error.get("code") or "internal_error", error.get("message") or "That did not finish.")
    name = tools.TOOLS[cell["tool"]]["name"]

    def change(doc):
        c = _find(doc, cell_id)
        previous = c.get("run") or {}
        c["run"] = run
        c["stale"] = None
        c["params"] = dict(run.get("params") or c.get("params") or {})
        label = (run.get("effective") or {}).get("label")
        old_label = (previous.get("effective") or {}).get("label")
        for other in doc["cells"]:
            if other["id"] == cell_id:
                continue
            eff = (other.get("run") or {}).get("effective") or {}
            reads = set((eff.get("inputs") or {}).values()) | {(eff.get("display") or {}).get("color")}
            if (label in reads or old_label in reads) and (other.get("run") or {}).get("status") == "done":
                other["stale"] = {"reason": f"{name} v{run['version']} was made current.",
                                  "at": time.time(), "input": label}
    store.mutate(notebook_id, change)
    _notify(notebook_id, (run.get("effective") or {}).get("label"))
    return view(notebook_id)


def compare(notebook_id, cell_id, a, b) -> dict:
    """Two versions side by side: their settings, their results and, for two
    partitions of the same cells, how closely they agree (adjusted Rand
    index, 1 being the same partition)."""
    document = store.load(notebook_id)
    cell = _find(document, cell_id)
    work = work_of(document)
    known = {r["version"]: r for r in _versions(notebook_id, cell_id)}
    left, right = known.get(int(a)), known.get(int(b))
    if left is None or right is None:
        raise RunError("invalid_input", "Choose two versions of this step.")
    current = (cell.get("run") or {}).get("run_id")
    rows = []
    lk = (left.get("effective") or {}).get("kwargs") or {}
    rk = (right.get("effective") or {}).get("kwargs") or {}
    if "params" in left and "params" in right:
        rows = _setting_rows(cell["tool"], left, right, work)
    else:
        lchips = tools.chips(cell["tool"], left.get("effective") or {}, work) if left.get("effective") else []
        rchips = tools.chips(cell["tool"], right.get("effective") or {}, work) if right.get("effective") else []
        for i in range(max(len(lchips), len(rchips))):
            x = lchips[i] if i < len(lchips) else "—"
            y = rchips[i] if i < len(rchips) else "—"
            rows.append({"label": "Setting", "left": x, "right": y, "differs": x != y})
    names = sorted(set(lk) | set(rk), key=lambda n: (n not in lk, n))
    calls = [{"name": n, "left": repr(lk[n]) if n in lk else "default",
              "right": repr(rk[n]) if n in rk else "default"} for n in names if lk.get(n) != rk.get(n)]
    agreement = None
    label_l = (left.get("effective") or {}).get("label")
    label_r = (right.get("effective") or {}).get("label")
    if work.get("exists") and label_l and label_r and cell["tool"] in tools.LABEL_TOOLS:
        try:
            with _reading(work["path"]):
                x = versions.labels(work["path"], None if left["run_id"] == current
                                    else _version_dir(notebook_id, cell, left["version"]), label_l)
                y = versions.labels(work["path"], None if right["run_id"] == current
                                    else _version_dir(notebook_id, cell, right["version"]), label_r)
            if x is not None and y is not None and len(x) == len(y):
                from sklearn.metrics import adjusted_rand_score

                agreement = {"ari": round(float(adjusted_rand_score(x, y)), 3), "cells": int(len(x))}
        except RunError:
            raise
        except Exception:  # noqa: BLE001 -- no agreement line rather than a failure
            agreement = None
    # Which steps would say Outdated if the non-current one were made current.
    other = left if right["run_id"] == current else right
    label = (other.get("effective") or {}).get("label")
    readers = []
    for c in document["cells"]:
        eff = (c.get("run") or {}).get("effective") or {}
        reads = set((eff.get("inputs") or {}).values()) | {(eff.get("display") or {}).get("color")}
        if c["id"] != cell_id and label and label in reads:
            readers.append(tools.TOOLS[c["tool"]]["name"])
    return {"left": {"version": left["version"], "current": left["run_id"] == current,
                     "result": _result_line(notebook_id, left)},
            "right": {"version": right["version"], "current": right["run_id"] == current,
                      "result": _result_line(notebook_id, right)},
            "settings": rows, "calls": calls, "agreement": agreement,
            "label": label, "outdated": readers}


def _setting_rows(tool_id, left, right, work) -> list:
    """The compare table's settings: one row per field that differs, in the
    form's words, then one row for those that do not."""
    tool = tools.TOOLS.get(tool_id) or {}
    every = list(work.get("markers") or [])

    def shown(field, value):
        if field["kind"] == "markers":
            if not value:
                auto = [m for m in every if not tools.NUCLEAR.search(m)]
                left_out = [m for m in every if m not in auto]
                return f"{len(auto)}" + (f" ({', '.join(left_out[:2])} out)" if left_out else "")
            out = [m for m in every if m not in value]
            return f"{len(value)}" + (f" ({', '.join(out[:2])} out)" if out else "")
        if field["kind"] in ("mapping", "groups"):
            return f"{len(value or {})} set"
        if field["kind"] == "segmented":
            return dict(field.get("choices") or []).get(value, value)
        if value is None:
            return "Automatic"
        return f"{value:g}" if isinstance(value, float) else str(value)

    rows, same = [], []
    lp, rp = left.get("params") or {}, right.get("params") or {}
    def applies(field, params):
        return all(params.get(k, next((f.get("default") for f in tool.get("fields") or [] if f["name"] == k), None)) == v
                   for k, v in (field.get("show_if") or {}).items())

    for field in tool.get("fields") or []:
        if field.get("display") or field["kind"] == "label":
            continue
        if not applies(field, lp) and not applies(field, rp):
            continue
        lv = lp.get(field["name"], field.get("default"))
        rv = rp.get(field["name"], field.get("default"))
        a, b = shown(field, lv), shown(field, rv)
        if a != b:
            rows.append({"label": field["label"], "left": a, "right": b, "differs": True})
        elif field["kind"] != "markers" or a:
            same.append((field["label"], a))
    le = (left.get("effective") or {}).get("extra") or {}
    re_ = (right.get("effective") or {}).get("extra") or {}
    for name in sorted(set(le) | set(re_), key=lambda k: (k not in le, k)):
        a = repr(le[name]) if name in le else "default"
        b = repr(re_[name]) if name in re_ else "default"
        if a != b:
            rows.append({"label": name, "left": a, "right": b, "differs": True, "code": True})
    if same:
        labels = [l for l, _ in same[:4]]
        text = ", ".join(labels).lower()
        rows.append({"label": text[:1].upper() + text[1:], "left": " · ".join(v for _, v in same[:4]),
                     "right": " · ".join(v for _, v in same[:4]), "differs": False})
    return rows


def _sample_metadata(document) -> dict | None:
    from plexora.plugins.scimap.server import samples

    return samples.get(document["context"]) or None


# -- the data: how it is read, and its sample metadata --------------------


def reading(notebook_id, memory_choice=None) -> dict:
    """Ask Scimap Pro again how it reads the working file (after the Memory
    setting changed). Nothing reruns."""
    from plexora.plugins.scimap.server import memory as _memory

    if memory_choice is not None:
        apply_ops(notebook_id, [{"op": "set_memory", "memory": memory_choice}])
    document = store.load(notebook_id)
    work = work_of(document)
    if not work.get("exists"):
        return {"reading": None, "memory": document.get("memory") or "auto"}

    def ask(job):
        return _memory.report(work["path"], document.get("memory") or "auto")

    report = _on_runner("reading", ask, notebook_id, None, 120)

    def keep(doc):
        if doc.get("working"):
            doc["working"]["reading"] = report
        for cell in doc["cells"]:
            if cell["tool"] == "prepare" and (cell.get("run") or {}).get("working"):
                cell["run"]["working"]["reading"] = report
    store.mutate(notebook_id, keep)
    for cell in document["cells"]:
        if cell["tool"] == "prepare" and cell.get("run"):
            store.drop_result_cache(notebook_id, cell["run"]["run_id"])
    return {"reading": report, "memory": document.get("memory") or "auto"}


def save_samples(notebook_id, table) -> dict:
    """Save the sample metadata to Plexora's records and, when the data is
    prepared, write it into the working file at once."""
    from plexora.plugins.scimap.server import samples

    document = store.load(notebook_id)
    before = samples.get(document["context"])
    saved = samples.put(document["context"], table)
    work = work_of(document)
    written = None
    if work.get("exists"):
        def write(job):
            with working.writing(work["path"]):
                return samples.write_into(work["path"], saved, work.get("image_column") or "imageid",
                                          previous=before.get("columns") or [])
        written = _on_runner("samples", write, notebook_id, None, 300)
        changed = set(before.get("columns") or []) | set(saved["columns"])

        def mark(doc):
            for cell in doc["cells"]:
                eff = (cell.get("run") or {}).get("effective") or {}
                reads = set((eff.get("inputs") or {}).values()) | {(eff.get("display") or {}).get("color")}
                if reads & changed and (cell.get("run") or {}).get("status") == "done":
                    cell["stale"] = {"reason": "The sample metadata changed.", "at": time.time()}
        store.mutate(notebook_id, mark)
    images = work.get("images") if work.get("exists") else _images_before(document)
    return {"samples": saved, "written": written, "match": samples.matched(saved, images)}


def _images_before(document) -> list:
    """The images a not-yet-prepared notebook will have: one per project (its
    name), as Prepare names them."""
    return list(document["context"].get("projects") or [])


# -- the embedding: every cell, its identity in the viewer, labels ----------


def _embedding_work(notebook_id):
    document = store.load(notebook_id)
    work = work_of(document)
    if not work.get("exists"):
        raise RunError("invalid_input", "Prepare the data first.")
    return document, work


def embedding_info(notebook_id) -> dict:
    """What the embedding view offers: the embeddings, the colour-by columns,
    the projects (in the order identity codes index), and which columns were
    made by labelling (only those can be labelled into)."""
    from plexora.plugins.scimap.server import embedding

    document, work = _embedding_work(notebook_id)
    with _reading(work["path"]):
        out = embedding.catalogue(work)
    context = document["context"]
    labelled = [c for c in document.get("label_columns") or []
                if any(col["name"] == c for col in work.get("columns") or [])]
    return {**out, "notebook_id": notebook_id, "title": document.get("title"),
            "projects": list(context.get("projects") or []),
            "context": {"kind": context["kind"], "id": context["id"], "name": context["name"]},
            "label_columns": labelled, "file": os.path.basename(work["path"]),
            "stamp": work.get("mtime")}


def embedding_coords(notebook_id, key, dims=(0, 1)) -> bytes:
    from plexora.plugins.scimap.server import embedding

    _, work = _embedding_work(notebook_id)
    try:
        with _reading(work["path"]):
            xy, extent = embedding.coords(work["path"], key, dims)
    except KeyError as exc:
        raise RunError("invalid_input", f"The working file has no embedding {key}.") from exc
    return embedding.pack({"key": key, "n": int(xy.size // 2), "extent": extent, "stamp": work.get("mtime")},
                          [("xy", xy)])


def embedding_identity(notebook_id) -> bytes:
    from plexora.plugins.scimap.server import embedding

    document, work = _embedding_work(notebook_id)
    projects = list(document["context"].get("projects") or [])
    with _reading(work["path"]):
        names, codes, labels = embedding.identity(work, projects, store.notebook_dir(notebook_id) / "cache")
    matched = int((labels > 0).sum())
    return embedding.pack({"projects": names, "n": int(labels.size), "matched": matched,
                           "stamp": work.get("mtime")},
                          [("codes", codes), ("labels", labels)])


def embedding_color(notebook_id, column) -> bytes:
    from plexora.plugins.scimap.server import embedding

    _, work = _embedding_work(notebook_id)
    try:
        with _reading(work["path"]):
            meta, values = embedding.color(work, column)
    except KeyError as exc:
        raise RunError("invalid_input", f"The working file has no column {column}.") from exc
    except ValueError as exc:
        raise RunError("invalid_input", str(exc)) from exc
    return embedding.pack(meta, [("values", values)])


def label_cells(notebook_id, column, label, bitmask: bytes, *, overwrite=False, new=False) -> dict:
    """Label the chosen cells (a bitmask over the working file's rows) in
    `column`, in place, then tell everyone that reads it."""
    from plexora.plugins.scimap.server import embedding

    document, work = _embedding_work(notebook_id)
    labelled = list(document.get("label_columns") or [])
    try:
        column = embedding.check_column(column, work, labelled)
        if new and any(c["name"] == column for c in work.get("columns") or []):
            raise embedding.LabelError(f"There is already a column {column}.")
        chosen = embedding.rows_from_bitmask(bitmask, int(work.get("n_cells") or 0))
        if not chosen.any():
            raise embedding.LabelError("Select some cells first.")
        out = embedding.write_label(work["path"], column, label, chosen, overwrite=overwrite,
                                    undo_path=store.notebook_dir(notebook_id) / "label_undo.npz")
    except embedding.LabelError as exc:
        raise RunError("invalid_input", str(exc)) from exc

    def change(doc):
        cols = list(doc.get("label_columns") or [])
        if column not in cols:
            cols.append(column)
        doc["label_columns"] = cols
        doc["label_undo"] = {"column": column, "label": out["label"], "labelled": out["labelled"],
                             "at": time.time()}
        _mark_dependents(doc, column, f"Labels in {column} changed.")
    store.mutate(notebook_id, change)
    _labels_changed(document, work, column)
    return {**out, "file": os.path.basename(work["path"])}


def undo_label(notebook_id) -> dict:
    from plexora.plugins.scimap.server import embedding

    document, work = _embedding_work(notebook_id)
    try:
        out = embedding.undo_label(work["path"], store.notebook_dir(notebook_id) / "label_undo.npz")
    except embedding.LabelError as exc:
        raise RunError("invalid_input", str(exc)) from exc
    column = out["column"]

    def change(doc):
        if out["removed"]:
            doc["label_columns"] = [c for c in doc.get("label_columns") or [] if c != column]
        doc["label_undo"] = None
        _mark_dependents(doc, column, f"Labels in {column} changed.")
    store.mutate(notebook_id, change)
    _labels_changed(document, work, column)
    return out


def _labels_changed(document, work, column):
    """A label column changed: viewers re-read it (an AnnData project whose
    table is the working file shows it in Cell Explorer at once), and an
    overlay of it redraws."""
    try:
        from plexora import api

        for project in document["context"].get("projects") or []:
            api.notify_viewers(project, "core", "dataset.changed", {"sections": [f"obs:{column}"]})
    except Exception:  # noqa: BLE001
        pass
    record = overlay.get()
    if record and record.get("notebook_id") == document["id"] and record.get("column") == column:
        _notify(document["id"], column)


def embedding_for_project(project) -> list:
    """Notebooks whose working file holds `project` and an embedding, newest
    first: what Cell Explorer's "Embedding" opens."""
    out = []
    for item in store.list_all():
        projects = (item.get("context") or {}).get("projects") or []
        if project not in projects:
            continue
        try:
            document = store.load(item["id"])
            work = work_of(document)
        except Exception:  # noqa: BLE001
            continue
        if not work.get("exists"):
            continue
        from plexora.plugins.scimap.server import embedding

        keys = [k for k in work.get("obsm") or [] if embedding.is_embedding(k)]
        if not keys:
            continue
        out.append({"notebook_id": item["id"], "title": item.get("title"), "embeddings": keys,
                    "updated_at": item.get("updated_at")})
    return out


def save_selection(notebook_id, name, bitmask: bytes) -> list:
    """Keep the chosen cells (a bitmask over the working file's rows) as a
    saved selection named `name` on each image that has some -- Plexora's own
    selections (core `set_selection`), which later steps, the viewer and an
    agent read by name. Ids are the table's cell ids, as the identity gives
    them; a re-save under the same name replaces it."""
    from plexora.agent import AgentSession, registry
    from plexora.agent.core.selection import MAX_IDS
    from plexora.plugins.scimap.server import embedding

    import re

    # Saved selections are named in letters, digits, "_", "." and "-": a
    # name typed with spaces is kept readable with "_".
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "").strip()).strip("_")
    if not name:
        raise RunError("invalid_input", "Name the selection first.")
    if len(name) > 64:
        raise RunError("invalid_input", "Keep the name to 64 characters.")
    document, work = _embedding_work(notebook_id)
    projects = list(document["context"].get("projects") or [])
    with _reading(work["path"]):
        names, codes, labels = embedding.identity(work, projects, store.notebook_dir(notebook_id) / "cache")
    try:
        chosen = embedding.rows_from_bitmask(bitmask, int(labels.size))
    except embedding.LabelError as exc:
        raise RunError("invalid_input", str(exc)) from exc
    if not chosen.any():
        raise RunError("invalid_input", "Select some cells first.")
    plan = []
    for index, project in enumerate(names):
        ids = labels[chosen & (codes == index) & (labels > 0)]
        if not ids.size:
            continue
        if ids.size > MAX_IDS:
            raise RunError("invalid_input", f"{ids.size:,} cells on {project} is more than a saved selection holds "
                                            f"({MAX_IDS:,}). Select fewer cells.")
        plan.append((project, ids))
    if not plan:
        raise RunError("invalid_input", "None of the selected cells could be matched to an image.")
    try:
        registry.get("set_selection")
    except Exception:  # noqa: BLE001 -- not registered in this process yet
        registry.register_core()
    session = AgentSession()
    saved = []
    for project, ids in plan:
        answer = registry.invoke(session, "set_selection", {
            "project": project, "name": name, "cell_ids": [int(i) for i in ids]})
        if not answer.get("ok"):
            problem = answer.get("error")
            message = getattr(problem, "message", None) or (problem.get("message") if isinstance(problem, dict) else None)
            raise RunError("invalid_input", f"{project}: {message or 'the selection could not be saved'}")
        saved.append({"project": project, "name": name, "n": int(ids.size)})
    return saved


def overlay_column(notebook_id, column) -> dict:
    """Colour the viewer by `column` of this notebook's working file (the
    floating embedding's Color by, for a column Cell Explorer does not have)."""
    document, work = _embedding_work(notebook_id)
    if not any(c["name"] == column and c.get("kind") == "category" for c in work.get("columns") or []):
        raise RunError("invalid_input", f"{column} is not a label column.")
    context = document["context"]
    record = {"notebook_id": notebook_id, "cell_id": None, "path": work["path"],
              "image_column": work.get("image_column") or "imageid",
              "cell_column": work.get("cell_column"), "projects": list(context["projects"]),
              "context": {"kind": context["kind"], "id": context["id"], "name": context["name"]},
              "title": column, "column": column, "focus": None, "kind": "labels"}
    overlay.put(record)
    for project in context["projects"]:
        try:
            from plexora import api

            api.notify_viewers(project, "core", "dataset.changed", {"sections": [f"obs:{overlay.ANNOTATION}"]})
        except Exception:  # noqa: BLE001
            pass
    return {"overlay": overlay.public(record), "annotation": overlay.ANNOTATION}
