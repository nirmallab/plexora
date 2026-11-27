"""SCIMAP's HTTP surface: two pages and a small JSON API.

Thin on purpose: parse, call `notebooks` (or `licence`, `export`), translate
the outcome. Status codes mean what they mean in Figure Builder:

    400  the request is wrong; retrying it unchanged fails the same way
    402  Scimap Pro's licence refuses the run (Starter's cell cap, a Pro feature)
    404  no such notebook or step
    409  someone else wrote first (stale revision), or the step is busy
    503  Scimap Pro is not installed here

On the website (`PLEXORA_BACKEND=browser`) the page renders the desktop-app
notice and every API route answers 409 with it: nothing imports Scimap Pro or
starts a worker in a browser tab.
"""

from __future__ import annotations

import json
import os

from flask import Blueprint, Response, jsonify, redirect, render_template, request

from plexora import api
from plexora.plugins.scimap import PLUGIN, VERSION
from plexora.plugins.scimap.server import install, licence, notebooks, overlay, palette, runner, store, tools
from plexora.plugins.scimap.server.adapter import RunError
from plexora.plugins.scimap.server.context import ContextError

scimap_bp = Blueprint("scimap", __name__, template_folder="../templates",
                      static_folder="../static", static_url_path="/static")

MAX_BODY_BYTES = 8 * 1024 * 1024
_STATUS = {"invalid_input": 400, "precondition_missing": 400, "not_available": 400,
           "license_required": 402, "conflict": 409, "not_installed": 503,
           "interrupted": 409, "busy": 409, "internal_error": 500}


def _on_the_website() -> bool:
    return os.environ.get("PLEXORA_BACKEND") == "browser"


def _website_notice():
    from plexora.browser.routes import feature

    return feature("scimap.workspace")


@scimap_bp.before_request
def _desktop_only():
    if _on_the_website() and request.path.startswith(f"{PLUGIN.url_prefix}/api/"):
        return jsonify(success=False, error="desktop_only", handoff=_website_notice()), 409
    return None


def _body():
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        raise RunError("invalid_input", "The request is too large.")
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _refusal(exc: RunError):
    return jsonify(success=False, error=exc.code, message=exc.message, detail=exc.detail), \
        _STATUS.get(exc.code, 400)


def _guarded(fn):
    """Translate the plugin's refusals into answers, in one place."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RunError as exc:
            return _refusal(exc)
        except ContextError as exc:
            return jsonify(success=False, error=exc.code, message=str(exc)), 404
        except store.UnknownNotebook:
            return jsonify(success=False, error="unknown_notebook",
                           message="That notebook no longer exists."), 404
        except store.ConflictError as exc:
            return jsonify(success=False, error="stale_revision", revision=exc.current_revision,
                           message="The notebook changed in another window."), 409
        except licence.LicenceError as exc:
            return jsonify(success=False, error=exc.code, message=str(exc),
                           licence=licence.state()), 400
    wrapper.__name__ = fn.__name__
    return wrapper


def _ok(**values):
    return api.json_response({"success": True, **values})


# -- status ---------------------------------------------------------------


def status_payload() -> dict:
    return {"install": install.probe(), "licence": licence.state(),
            "desktop": install.desktop(), "website": _on_the_website(),
            "palette": list(palette.OKABE_ITO), "warmed": runner.warmed(),
            "version": VERSION,
            # What a feature waits for from Scimap Pro: "" when it can run.
            "needs": {feature: install.why_not(feature) for feature in install.NEEDS}}


@scimap_bp.route("/api/status", methods=["GET"])
def status():
    return _ok(status=status_payload())


@scimap_bp.route("/api/tools", methods=["GET"])
def list_tools():
    return _ok(categories=tools.describe())


@scimap_bp.route("/api/warm", methods=["POST"])
def warm():
    runner.warm()
    return _ok(warmed=runner.warmed())


@scimap_bp.route("/api/places", methods=["GET"])
@_guarded
def places():
    from plexora.plugins.scimap.server import context

    return _ok(projects=context.projects(), datasets=context.datasets(),
               notebooks=store.list_all())


# -- notebooks ------------------------------------------------------------


@scimap_bp.route("/api/notebooks", methods=["GET"])
@_guarded
def list_notebooks():
    kind, ref = request.args.get("kind"), request.args.get("ref")
    if kind and ref:
        return _ok(notebooks=notebooks.for_context(kind, ref))
    return _ok(notebooks=store.list_all())


@scimap_bp.route("/api/notebooks", methods=["POST"])
@_guarded
def create_notebook():
    body = _body()
    document = notebooks.create(body.get("kind"), body.get("ref"), body.get("title"))
    return _ok(notebook_id=document["id"], view=notebooks.view(document["id"]))


@scimap_bp.route("/api/notebooks/<notebook_id>", methods=["GET"])
@_guarded
def get_notebook(notebook_id):
    return _ok(view=notebooks.view(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>", methods=["PATCH"])
@_guarded
def patch_notebook(notebook_id):
    body = _body()
    ops = body.get("ops")
    if not isinstance(ops, list):
        raise RunError("invalid_input", "expected a list of operations")
    view = notebooks.apply_ops(notebook_id, ops, body.get("revision"))
    created = [op.get("_created") for op in ops if op.get("_created")]
    return _ok(view=view, created=created)


@scimap_bp.route("/api/notebooks/<notebook_id>", methods=["DELETE"])
@_guarded
def delete_notebook(notebook_id):
    if not store.exists(notebook_id):
        raise store.UnknownNotebook(notebook_id)
    store.delete(notebook_id)
    record = overlay.get()
    if record and record.get("notebook_id") == notebook_id:
        overlay.clear()
    return _ok()


@scimap_bp.route("/api/notebooks/<notebook_id>/duplicate", methods=["POST"])
@_guarded
def duplicate_notebook(notebook_id):
    document = store.load(notebook_id)
    copy = {k: v for k, v in document.items() if k not in ("id", "revision", "created_at")}
    copy["title"] = f"{document.get('title') or 'Notebook'} copy"
    cells = []
    for cell in document["cells"]:
        clone = {**cell, "id": store.new_cell_id(), "job_id": None, "folder": None}
        cells.append(clone)
    copy["cells"] = cells
    made = store.create(copy)
    return _ok(notebook_id=made["id"])


@scimap_bp.route("/api/notebooks/<notebook_id>/plan", methods=["GET"])
@_guarded
def prepare_plan(notebook_id):
    from plexora.plugins.scimap.server import working

    document = store.load(notebook_id)
    folder = request.args.get("folder") or document.get("results_dir") or ""
    return _ok(plan=working.plan(document["context"], folder))


@scimap_bp.route("/api/notebooks/<notebook_id>/column", methods=["GET"])
@_guarded
def column_summary(notebook_id):
    return _ok(summary=notebooks.column_summary(notebook_id, request.args.get("name") or ""))


@scimap_bp.route("/api/notebooks/<notebook_id>/run_all", methods=["POST"])
@_guarded
def run_all(notebook_id):
    return _ok(queued=notebooks.run_all(notebook_id), view=notebooks.view(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/run", methods=["POST"])
@_guarded
def run_cell(notebook_id, cell_id):
    body = _body()
    sample = body.get("sample")
    queued = notebooks.run_cell(notebook_id, cell_id, sample=int(sample) if sample else None)
    return _ok(**queued, view=notebooks.view(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/cancel", methods=["POST"])
@_guarded
def cancel_cell(notebook_id, cell_id):
    notebooks.cancel_cell(notebook_id, cell_id)
    return _ok(view=notebooks.view(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/result", methods=["GET"])
@_guarded
def cell_result(notebook_id, cell_id):
    return _ok(result=notebooks.summary(notebook_id, cell_id,
                                        refresh=request.args.get("refresh") == "1"))


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/history", methods=["GET"])
@_guarded
def cell_history(notebook_id, cell_id):
    return _ok(history=notebooks.history(notebook_id, cell_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/compare", methods=["GET"])
@_guarded
def cell_compare(notebook_id, cell_id):
    try:
        a, b = int(request.args.get("a", "")), int(request.args.get("b", ""))
    except ValueError as exc:
        raise RunError("invalid_input", "Choose two versions to compare.") from exc
    return _ok(compare=notebooks.compare(notebook_id, cell_id, a, b))


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/current", methods=["POST"])
@_guarded
def cell_make_current(notebook_id, cell_id):
    version = _body().get("version")
    if version is None:
        raise RunError("invalid_input", "Which version?")
    return _ok(view=notebooks.make_current(notebook_id, cell_id, int(version)))


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/advanced", methods=["POST"])
@_guarded
def cell_advanced(notebook_id, cell_id):
    """Check one function's Advanced box as it is typed."""
    from plexora.plugins.scimap.server import advanced, tools

    body = _body()
    document = store.load(notebook_id)
    cell = notebooks._find(document, cell_id)
    key = body.get("key") or tools.TOOLS[cell["tool"]].get("call")
    allowed = {f["key"] for f in advanced.functions(cell["tool"])}
    if key not in allowed:
        raise RunError("invalid_input", f"This step does not call {key}.")
    form = {}
    if key == tools.TOOLS[cell["tool"]].get("call"):
        params = {**(cell.get("params") or {}), **(body.get("params") or {})}
        params.pop("advanced", None)
        try:
            form = tools.effective(cell["tool"], params, notebooks.work_of(document), document, cell_id)["kwargs"]
        except ValueError:
            form = {}
    return _ok(check=advanced.check(cell["tool"], key, body.get("text") or "", form))


@scimap_bp.route("/api/notebooks/<notebook_id>/data", methods=["GET"])
@_guarded
def notebook_data(notebook_id):
    from plexora.plugins.scimap.server import data

    return _ok(data=data.view(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/data", methods=["POST"])
@_guarded
def notebook_data_edit(notebook_id):
    """One edit to the notebook's data, written to Plexora's records:
    `{add: [projects]}`, `{remove: project}` or `{rename: name}`."""
    from plexora.plugins.scimap.server import data

    body = _body()
    if body.get("add") is not None:
        out = data.add_projects(notebook_id, list(body["add"]))
    elif body.get("remove"):
        out = data.remove_project(notebook_id, body["remove"])
    elif body.get("rename") is not None:
        out = data.rename_dataset(notebook_id, body["rename"])
    else:
        raise RunError("invalid_input", "expected add, remove or rename")
    return _ok(data=out, view=notebooks.view(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/data/projects/<name>", methods=["GET"])
@_guarded
def notebook_project_columns(notebook_id, name):
    from plexora.plugins.scimap.server import data

    document = store.load(notebook_id)
    if name not in document["context"]["projects"]:
        raise RunError("invalid_input", f"{name} is not part of this notebook's data.")
    return _ok(project=data.project_columns(name))


@scimap_bp.route("/api/notebooks/<notebook_id>/data/projects/<name>", methods=["POST"])
@_guarded
def notebook_project_edit(notebook_id, name):
    from plexora.plugins.scimap.server import data

    out = data.edit_project(notebook_id, name, _body().get("edits") or {})
    return _ok(data=out, project=data.project_columns(name), view=notebooks.view(notebook_id))


@scimap_bp.route("/api/stamp", methods=["GET"])
@_guarded
def records_stamp():
    """Changes whenever a project or dataset record changes anywhere, so the
    page redraws its data from Plexora."""
    from plexora import api

    return _ok(stamp=api.records_stamp())


@scimap_bp.route("/api/tools/<tool_id>/functions", methods=["GET"])
@_guarded
def tool_functions(tool_id):
    from plexora.plugins.scimap.server import advanced, tools

    if tool_id not in tools.TOOLS:
        raise RunError("invalid_input", f"unknown tool {tool_id!r}")
    return _ok(functions=advanced.functions(tool_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/reading", methods=["POST"])
@_guarded
def notebook_reading(notebook_id):
    return _ok(**notebooks.reading(notebook_id, _body().get("memory")), view=notebooks.view(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/samples", methods=["GET"])
@_guarded
def notebook_samples(notebook_id):
    from plexora.plugins.scimap.server import samples

    document = store.load(notebook_id)
    table = samples.get(document["context"])
    work = notebooks.work_of(document)
    images = work.get("images") if work.get("exists") else notebooks._images_before(document)
    return _ok(samples=table, images=images, match=samples.matched(table, images))


@scimap_bp.route("/api/notebooks/<notebook_id>/samples", methods=["PUT"])
@_guarded
def notebook_samples_put(notebook_id):
    return _ok(**notebooks.save_samples(notebook_id, _body().get("samples") or {}),
               view=notebooks.view(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/samples/import", methods=["POST"])
@_guarded
def notebook_samples_import(notebook_id):
    """A CSV or Excel sheet, read into a table for the page to show; saved
    only when the user saves it."""
    from plexora.plugins.scimap.server import samples

    upload = request.files.get("file")
    if upload is None:
        raise RunError("invalid_input", "Choose a CSV or Excel file.")
    payload = upload.read(20 * 1024 * 1024 + 1)
    if len(payload) > 20 * 1024 * 1024:
        raise RunError("invalid_input", "That sheet is larger than 20 MB.")
    try:
        table = samples.from_file(upload.filename or "sheet.csv", payload)
    except Exception as exc:  # noqa: BLE001 -- a sheet we cannot read
        raise RunError("invalid_input", f"That file could not be read as a table: {exc}") from exc
    return _ok(samples=table)


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/figures", methods=["GET"])
@_guarded
def cell_figures(notebook_id, cell_id):
    return _ok(figures=notebooks.figures_for(notebook_id, cell_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/draw", methods=["POST"])
@_guarded
def cell_draw(notebook_id, cell_id):
    """One view of the step's result, drawn from what its run stored."""
    body = request.get_json(silent=True) or {}
    opts = body.get("opts") if isinstance(body.get("opts"), dict) else {}
    return _ok(figure=notebooks.view_for(notebook_id, cell_id, str(body.get("view") or ""), opts,
                                         compute=bool(body.get("compute"))))


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/figures/<name>.<fmt>", methods=["GET"])
@_guarded
def cell_figure_file(notebook_id, cell_id, name, fmt):
    from plexora.plugins.scimap.server import figures

    if fmt not in figures.FORMATS:
        raise RunError("invalid_input", f"Unknown format {fmt!r}: PNG, PDF or SVG.")
    view = request.args.get("view") or None
    try:
        opts = json.loads(request.args.get("opts") or "{}")
    except ValueError as exc:
        raise RunError("invalid_input", "The view settings are not valid JSON.") from exc
    data, filename = notebooks.figure_file(notebook_id, cell_id, name, fmt, view=view,
                                           opts=opts if isinstance(opts, dict) else {})
    return Response(data, mimetype=figures.FORMATS[fmt],
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-store"})


@scimap_bp.route("/vendor/plotly.min.js", methods=["GET"])
def plotly_js():
    """plotly.js from the installed plotly package, the same build Scimap Pro's
    figures were made for, so nothing is fetched from a CDN."""
    import importlib.util
    from pathlib import Path

    from flask import send_file

    spec = importlib.util.find_spec("plotly")
    if spec is None or not spec.origin:
        return Response("plotly is not installed", status=404, mimetype="text/plain")
    path = Path(spec.origin).parent / "package_data" / "plotly.min.js"
    if not path.is_file():
        return Response("plotly.js is missing", status=404, mimetype="text/plain")
    response = send_file(path, mimetype="text/javascript", max_age=86400)
    return response


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/colors", methods=["GET"])
@_guarded
def cell_colors(notebook_id, cell_id):
    column = request.args.get("column") or ""
    return _ok(colors=notebooks.color_codes(notebook_id, cell_id, column))


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/lasso", methods=["POST"])
@_guarded
def cell_lasso(notebook_id, cell_id):
    body = _body()
    polygon = body.get("polygon")
    if not isinstance(polygon, list) or len(polygon) < 3:
        raise RunError("invalid_input", "A selection needs at least three points.")
    return _ok(selection=notebooks.lasso(notebook_id, cell_id, polygon, body.get("color")))


# -- the embedding (every cell, binary) and labels --------------------------


def _binary(payload: bytes):
    response = Response(payload, mimetype="application/octet-stream")
    response.headers["Cache-Control"] = "no-store"
    return response


@scimap_bp.route("/api/notebooks/<notebook_id>/embedding", methods=["GET"])
@_guarded
def embedding_info(notebook_id):
    return _ok(embedding=notebooks.embedding_info(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/embedding/coords", methods=["GET"])
@_guarded
def embedding_coords(notebook_id):
    key = request.args.get("key") or "X_umap"
    try:
        dims = tuple(int(v) for v in (request.args.get("dims") or "0,1").split(","))[:2]
    except ValueError:
        raise RunError("invalid_input", "dims is two numbers, as 0,1")
    return _binary(notebooks.embedding_coords(notebook_id, key, dims if len(dims) == 2 else (0, 1)))


@scimap_bp.route("/api/notebooks/<notebook_id>/embedding/identity", methods=["GET"])
@_guarded
def embedding_identity(notebook_id):
    return _binary(notebooks.embedding_identity(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/embedding/color", methods=["GET"])
@_guarded
def embedding_color(notebook_id):
    return _binary(notebooks.embedding_color(notebook_id, request.args.get("column") or ""))


@scimap_bp.route("/api/notebooks/<notebook_id>/labels", methods=["POST"])
@_guarded
def label_cells(notebook_id):
    import base64
    import binascii

    body = _body()
    try:
        bitmask = base64.b64decode(str(body.get("cells") or ""), validate=True)
    except (binascii.Error, ValueError):
        raise RunError("invalid_input", "The selection could not be read.")
    if len(bitmask) > 64 * 1024 * 1024:
        raise RunError("invalid_input", "The selection is too large.")
    return _ok(labels=notebooks.label_cells(notebook_id, body.get("column"), body.get("label"), bitmask,
                                            overwrite=bool(body.get("overwrite")), new=bool(body.get("new"))))


@scimap_bp.route("/api/notebooks/<notebook_id>/labels/undo", methods=["POST"])
@_guarded
def undo_label(notebook_id):
    return _ok(labels=notebooks.undo_label(notebook_id))


@scimap_bp.route("/api/notebooks/<notebook_id>/selections", methods=["POST"])
@_guarded
def save_selection(notebook_id):
    import base64
    import binascii

    body = _body()
    try:
        bitmask = base64.b64decode(str(body.get("cells") or ""), validate=True)
    except (binascii.Error, ValueError):
        raise RunError("invalid_input", "The selection could not be read.")
    if len(bitmask) > 64 * 1024 * 1024:
        raise RunError("invalid_input", "The selection is too large.")
    return _ok(saved=notebooks.save_selection(notebook_id, body.get("name"), bitmask))


@scimap_bp.route("/api/notebooks/<notebook_id>/overlay", methods=["POST"])
@_guarded
def overlay_column(notebook_id):
    return _ok(**notebooks.overlay_column(notebook_id, str(_body().get("column") or "")))


@scimap_bp.route("/api/embedding", methods=["GET"])
@_guarded
def embedding_for_project():
    project = request.args.get("project") or ""
    return _ok(notebooks=notebooks.embedding_for_project(project) if project else [])


@scimap_bp.route("/api/notebooks/<notebook_id>/cells/<cell_id>/view", methods=["POST"])
@_guarded
def cell_view(notebook_id, cell_id):
    body = _body()
    shown = notebooks.show_in_viewer(notebook_id, cell_id, focus=body.get("focus"),
                                     selection=bool(body.get("selection")),
                                     project=body.get("project"))
    return _ok(**shown)


@scimap_bp.route("/api/notebooks/<notebook_id>/runs", methods=["GET"])
@_guarded
def notebook_runs(notebook_id):
    return _ok(runs=store.runs(notebook_id, request.args.get("cell")))


# -- exports --------------------------------------------------------------


@scimap_bp.route("/api/notebooks/<notebook_id>/export", methods=["GET"])
@_guarded
def export_notebook(notebook_id):
    from plexora.plugins.scimap.server import export

    fmt = request.args.get("format", "ipynb")
    include = request.args.get("include", "workflow")
    name, mimetype, payload = export.build(notebook_id, fmt, include)
    return Response(payload, mimetype=mimetype,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@scimap_bp.route("/api/notebooks/<notebook_id>/export/estimate", methods=["GET"])
@_guarded
def export_estimate(notebook_id):
    from plexora.plugins.scimap.server import export

    return _ok(sizes=export.estimate(notebook_id))


@scimap_bp.route("/api/import", methods=["POST"])
@_guarded
def import_notebook():
    from plexora.plugins.scimap.server import export

    upload = request.files.get("file")
    if upload is None:
        raise RunError("invalid_input", "Choose a .scimap file.")
    made = export.import_native(upload.read(), kind=request.form.get("kind"),
                                ref=request.form.get("ref"))
    return _ok(notebook_id=made["id"])


# -- the viewer overlay ---------------------------------------------------


@scimap_bp.route("/api/overlay", methods=["GET"])
def get_overlay():
    return _ok(overlay=overlay.public(overlay.get()))


@scimap_bp.route("/api/overlay", methods=["DELETE"])
def clear_overlay():
    record = overlay.get()
    overlay.clear()
    if record:
        for project in record.get("projects") or []:
            try:
                api.notify_viewers(project, "core", "dataset.changed",
                                   {"sections": [f"obs:{overlay.ANNOTATION}"]})
            except Exception:  # noqa: BLE001
                pass
    return _ok()


# -- Scimap Pro's licence -------------------------------------------------


def _local_or_token():
    import ipaddress

    from flask import current_app

    if current_app.config.get("PLEXORA_AUTH_TOKEN"):
        return None
    try:
        if ipaddress.ip_address(request.remote_addr or "").is_loopback:
            return None
    except ValueError:
        pass
    return jsonify(success=False, error="forbidden",
                   message="License changes are accepted from this computer only."), 403


@scimap_bp.route("/api/licence", methods=["GET"])
def licence_state():
    return _ok(licence=licence.state())


@scimap_bp.route("/api/licence/activate", methods=["POST"])
@_guarded
def licence_activate():
    refused = _local_or_token()
    if refused:
        return refused
    return _ok(licence=licence.activate(_body().get("credential")))


@scimap_bp.route("/api/licence/install", methods=["POST"])
@_guarded
def licence_install():
    refused = _local_or_token()
    if refused:
        return refused
    upload = request.files.get("file")
    text = upload.read(licence.MAX_CERTIFICATE_TEXT + 1).decode("utf-8", "replace") if upload \
        else _body().get("certificate")
    return _ok(licence=licence.install_file(text))


@scimap_bp.route("/api/licence/connect", methods=["POST"])
@_guarded
def licence_connect():
    refused = _local_or_token()
    if refused:
        return refused
    return _ok(**licence.connect_start())


@scimap_bp.route("/api/licence/connect/poll", methods=["POST"])
@_guarded
def licence_connect_poll():
    refused = _local_or_token()
    if refused:
        return refused
    return _ok(**licence.connect_poll(_body().get("code")))


# -- pages ----------------------------------------------------------------


def _page_data(**values):
    """The context base.html reads, built here as Figure Builder's pages build
    theirs (see figure_builder/server/routes.py `_page_data` for why)."""
    from plexora import app, get_config_names
    from plexora.server import plugins as plugin_registry

    base_url = app.config.get("PLEXORA_BASE_URL", "")
    data = {
        "datasource": "",
        "datasources": get_config_names(),
        "is_docker": app.config.get("IS_DOCKER", False),
        "base_url": base_url,
        "image_kind": "",
        "active_tool": "",
        "available_tools": [],
        "active_tool_scripts": PLUGIN.asset_urls("scripts", base_url),
        "active_tool_styles": PLUGIN.asset_urls("styles", base_url),
        "active_tool_panels": {},
        "plugin_nav_items": plugin_registry.nav_items(app, base_url),
        "scimap_url": f"{base_url}{PLUGIN.url_prefix}",
        "scimap_version": VERSION,
    }
    if os.environ.get("PLEXORA_BACKEND"):
        data["backend"] = os.environ["PLEXORA_BACKEND"]
    data.update(values)
    return data


def _loaded_datasource():
    try:
        from plexora.server.models import data_model

        name = data_model.get_current_datasource_name()
        return name or ""
    except Exception:  # noqa: BLE001
        return ""


@scimap_bp.route("/notebook", methods=["GET"])
def notebook_page():
    """Open the right notebook for what was asked: `?notebook=`, a context
    (`?project=` / `?dataset=`) -- its latest notebook, made if it has none --
    or the project the viewer has loaded. With none of those, the page offers
    a project or a dataset."""
    if _on_the_website():
        return render_template("scimap/workspace.html",
                               data=_page_data(notebook_id="", handoff=_website_notice()))
    wanted = request.args.get("notebook")
    if wanted and store.exists(wanted):
        return redirect(f"{PLUGIN.url_prefix}/notebook/{wanted}")
    kind, ref = None, None
    if request.args.get("dataset"):
        kind, ref = "dataset", request.args["dataset"]
    elif request.args.get("project"):
        kind, ref = "project", request.args["project"]
    else:
        loaded = _loaded_datasource()
        if loaded:
            kind, ref = "project", loaded
    if kind:
        try:
            existing = notebooks.for_context(kind, ref)
            if existing and request.args.get("new") != "1":
                return redirect(f"{PLUGIN.url_prefix}/notebook/{existing[0]['id']}")
            document = notebooks.create(kind, ref)
            return redirect(f"{PLUGIN.url_prefix}/notebook/{document['id']}")
        except ContextError:
            pass
    return render_template("scimap/workspace.html", data=_page_data(notebook_id=""))


@scimap_bp.route("/notebook/<notebook_id>", methods=["GET"])
def notebook_by_id(notebook_id):
    if _on_the_website():
        return render_template("scimap/workspace.html",
                               data=_page_data(notebook_id="", handoff=_website_notice()))
    if not store.exists(notebook_id):
        return redirect(f"{PLUGIN.url_prefix}/notebook")
    runner.warm()
    return render_template("scimap/workspace.html", data=_page_data(notebook_id=notebook_id))
