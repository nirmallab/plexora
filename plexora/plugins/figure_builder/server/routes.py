"""Figure Builder's HTTP surface.

Thin on purpose, like ROI's: parse, hand to the repository, translate the
outcome. Every rule about what a figure is lives in `schema`/`operations`, and
every rule about who may overwrite whom lives in `repository`, so there is one
place to read each and no route can quietly disagree with another.

Status codes carry meaning, because the client acts on them differently:

    400  the request is wrong -- an unknown operation, a panel naming a page
         that does not exist. Retrying it unchanged will fail the same way.
    404  no such figure. Distinct from 400 because the library shows it
         differently: a figure that has been deleted is not a bad request.
    409  the request was fine but somebody else wrote first. The client has
         work worth keeping, so this opens the conflict prompt rather than
         discarding either side.
    422  the stored figure cannot be read by this build -- damaged, or written
         by a newer Plexora. Nothing is written and nothing is drawn.

One route here is a PAGE rather than an API: `/figure/<id>` renders the
workspace for a figure opened without a project. A figure spans datasources, so
requiring one before it can be opened would make "open my figure" mean "guess
which of its four images you meant".
"""

from __future__ import annotations

from flask import Blueprint, Response, jsonify, render_template, request, send_file

from plexora import api, app
from plexora.plugins.figure_builder import PLUGIN, VERSION
from plexora.plugins.figure_builder.server import repository, schema, sources
from plexora.plugins.figure_builder.server.repository import ConflictError, UnknownFigure

figure_builder_bp = Blueprint(
    'figure_builder', __name__,
    template_folder='../templates',
    static_folder='../static',
    static_url_path='/static',
)

#: Biggest JSON body accepted. A batch of layout edits is kilobytes; a document
#: replace for a 200-panel figure is a few megabytes. Past this the request is a
#: mistake, and the client is told so rather than the server parsing it.
MAX_BODY_BYTES = 64 * 1024 * 1024


def _payload():
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        raise ValueError(f"request body is larger than {MAX_BODY_BYTES // (1024 * 1024)} MB")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    return body


def _not_found():
    return jsonify(success=False, error="unknown_figure"), 404


def _unreadable(exc):
    return jsonify(success=False, error="unreadable_figure", detail=str(exc)), 422


def _conflict(exc):
    return jsonify(success=False, error="stale_revision", revision=exc.current_revision), 409


# -- the library ---------------------------------------------------------


@figure_builder_bp.route('/api/figures', methods=['GET'])
def list_figures():
    """Every figure on this machine.

    Deliberately needs no datasource: the Figures tab is reachable with nothing
    open, and this route existing is also how the Open Project page
    feature-detects the plugin (a 404 means core-only, and the tab stays
    hidden).
    """
    return api.json_response({"success": True, "figures": repository.list_figures()})


@figure_builder_bp.route('/api/figures', methods=['POST'])
def create_figure():
    try:
        body = _payload()
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    figure_id = repository.create(title=body.get('title'))
    return api.json_response({"success": True, "figure_id": figure_id,
                              "document": repository.load(figure_id)})


@figure_builder_bp.route('/api/figures/<figure_id>', methods=['GET'])
def get_figure(figure_id):
    """The whole document, plus each source's status right now.

    Status is computed on read rather than stored, because it is a statement
    about the world outside this figure and the world changes without telling
    the figure. Stored `status` in the document is only ever what the user last
    acknowledged.
    """
    try:
        schema.validate_figure_id(figure_id)
        document = repository.load(figure_id)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except UnknownFigure:
        return _not_found()
    except schema.UnreadableFigure as exc:
        return _unreadable(exc)

    return api.json_response({
        "success": True,
        "document": document,
        "source_status": {source_id: sources.status_of(source)
                          for source_id, source in document["sources"].items()},
    })


@figure_builder_bp.route('/api/figures/<figure_id>', methods=['PATCH'])
def patch_figure(figure_id):
    """Apply a batch of edits, if the client is writing against the revision it
    last read. The batch is all-or-nothing, and is one undo step."""
    try:
        schema.validate_figure_id(figure_id)
        body = _payload()
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    try:
        revision = repository.apply(figure_id, body.get('base_revision'), body.get('operations'))
    except UnknownFigure:
        return _not_found()
    except ConflictError as exc:
        return _conflict(exc)
    except schema.UnreadableFigure as exc:
        return _unreadable(exc)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    return jsonify(success=True, revision=revision)


@figure_builder_bp.route('/api/figures/<figure_id>', methods=['PUT'])
def replace_figure(figure_id):
    """Store a whole document. For import and recovery only -- ordinary editing
    goes through PATCH, which is undoable and journalled."""
    try:
        schema.validate_figure_id(figure_id)
        body = _payload()
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    try:
        revision = repository.replace(figure_id, body.get('base_revision'), body.get('document'))
    except UnknownFigure:
        return _not_found()
    except ConflictError as exc:
        return _conflict(exc)
    except schema.UnreadableFigure as exc:
        return _unreadable(exc)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    return jsonify(success=True, revision=revision)


@figure_builder_bp.route('/api/figures/<figure_id>', methods=['DELETE'])
def delete_figure(figure_id):
    try:
        schema.validate_figure_id(figure_id)
        repository.delete(figure_id)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except UnknownFigure:
        return _not_found()
    return jsonify(success=True)


@figure_builder_bp.route('/api/figures/<figure_id>/duplicate', methods=['POST'])
def duplicate_figure(figure_id):
    """A copy, previews and imported assets included.

    A file copy rather than a document round trip: re-rendering every preview
    for a duplicate the user made in order to try a different layout would be
    minutes of work to produce pixels that already exist.
    """
    try:
        schema.validate_figure_id(figure_id)
        body = request.get_json(silent=True) or {}
        new_id = repository.duplicate(figure_id, title=body.get('title'))
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except UnknownFigure:
        return _not_found()
    except schema.UnreadableFigure as exc:
        return _unreadable(exc)
    return jsonify(success=True, figure_id=new_id)


# -- rasters -------------------------------------------------------------


def _image_response(data, media_type):
    """A raster the browser may cache only after revalidating.

    `no-cache` rather than a max-age: a preview is replaced in place whenever
    its panel changes, at the same URL, and a cached copy would show the user
    the view they just edited away from.
    """
    return Response(data, mimetype=media_type,
                    headers={"Cache-Control": "no-cache, must-revalidate"})


@figure_builder_bp.route('/api/figures/<figure_id>/previews/<panel_id>', methods=['GET'])
def get_preview(figure_id, panel_id):
    try:
        schema.validate_figure_id(figure_id)
        found = repository.get_preview(figure_id, panel_id)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except UnknownFigure:
        return _not_found()
    if found is None:
        return jsonify(success=False, error="no_preview"), 404
    data, fmt, _ = found
    return _image_response(data, f"image/{fmt}")


@figure_builder_bp.route('/api/figures/<figure_id>/previews/<panel_id>', methods=['POST'])
def put_preview(figure_id, panel_id):
    """Store a panel's preview raster.

    The body is the image itself rather than JSON with a base64 field: a WebP
    goes over the wire as bytes, and base64 would add a third to every panel
    the user captures. The render revision rides on the query string, and a
    render older than the one already stored is refused -- see
    repository.put_preview.
    """
    try:
        schema.validate_figure_id(figure_id)
        render_revision = int(request.args.get('render_revision', '0'))
        stored = repository.put_preview(
            figure_id, panel_id, render_revision, request.get_data(),
            width=int(request.args.get('width', '0') or 0),
            height=int(request.args.get('height', '0') or 0),
        )
    except (TypeError, ValueError) as exc:
        return jsonify(success=False, error=str(exc)), 400
    except UnknownFigure:
        return _not_found()
    return jsonify(success=True, stored=stored)


@figure_builder_bp.route('/api/figures/<figure_id>/thumbnail', methods=['GET'])
def get_thumbnail(figure_id):
    try:
        schema.validate_figure_id(figure_id)
        found = repository.get_thumbnail(figure_id)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except UnknownFigure:
        return _not_found()
    if found is None:
        return jsonify(success=False, error="no_thumbnail"), 404
    data, fmt = found
    return _image_response(data, f"image/{fmt}")


@figure_builder_bp.route('/api/figures/<figure_id>/thumbnail', methods=['PUT'])
def put_thumbnail(figure_id):
    try:
        schema.validate_figure_id(figure_id)
        repository.put_thumbnail(figure_id, request.get_data())
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except UnknownFigure:
        return _not_found()
    return jsonify(success=True)


# -- figure-only assets --------------------------------------------------


@figure_builder_bp.route('/api/figures/<figure_id>/assets', methods=['POST'])
def add_asset(figure_id):
    """Import an image into this figure and nowhere else.

    The filename comes from the query string and the bytes from the body, so
    nothing here parses a multipart form -- the client already has the file as
    a Blob and this is the cheapest thing to send it as.
    """
    try:
        schema.validate_figure_id(figure_id)
        asset = repository.import_asset(
            figure_id, request.args.get('filename', ''), request.get_data())
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except UnknownFigure:
        return _not_found()
    return jsonify(success=True, **asset)


@figure_builder_bp.route('/api/figures/<figure_id>/assets/<asset_id>', methods=['GET'])
def get_asset(figure_id, asset_id):
    try:
        schema.validate_figure_id(figure_id)
        path = repository.asset_path(figure_id, asset_id)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    if path is None:
        return jsonify(success=False, error="no_such_asset"), 404
    return send_file(str(path), conditional=True)


# -- sources -------------------------------------------------------------


@figure_builder_bp.route('/api/sources/<datasource>', methods=['GET'])
def describe_source(datasource):
    """What this project looks like right now, for capture and for relinking.

    Reads the project record only. Nothing here loads a datasource, so asking
    about eight sources in a row cannot evict the one the user is looking at.
    """
    try:
        return api.json_response({"success": True, "source": sources.describe(datasource)})
    except KeyError:
        return jsonify(success=False, error="unknown_datasource"), 404


# -- export --------------------------------------------------------------


@figure_builder_bp.route('/api/figures/<figure_id>/export/preflight', methods=['POST'])
def export_preflight(figure_id):
    """What the user should know before committing to an export.

    Answered before a single pixel is rendered, so "this panel is 96 DPI at that
    size" and "this channel is gone" arrive while the dialog is open rather than
    as a warning attached to a file that is already written.
    """
    from plexora.plugins.figure_builder.server import export

    try:
        schema.validate_figure_id(figure_id)
        body = _payload()
        document = repository.load(figure_id)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except UnknownFigure:
        return _not_found()
    except schema.UnreadableFigure as exc:
        return _unreadable(exc)

    return api.json_response({"success": True, **export.preflight(document, body)})


@figure_builder_bp.route('/api/figures/<figure_id>/export', methods=['POST'])
def start_export(figure_id):
    """Begin an export and answer with a job id.

    The document is read HERE and handed to the job, so the export renders one
    revision throughout. A figure edited while an export runs must not produce a
    file that is half of one layout and half of another.
    """
    from plexora.plugins.figure_builder.server import export, export_jobs

    try:
        schema.validate_figure_id(figure_id)
        body = _payload()
        document = repository.load(figure_id)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except UnknownFigure:
        return _not_found()
    except schema.UnreadableFigure as exc:
        return _unreadable(exc)

    if body.get("format", "pdf") not in export.FORMATS:
        return jsonify(success=False, error="unknown_format",
                       formats=list(export.FORMATS)), 400
    if not any(panel["placement"] for panel in document["panels"].values()):
        return jsonify(success=False,
                       error="This figure has no panels on a page yet."), 400

    job_id = export_jobs.start(figure_id, document, body)
    return jsonify(success=True, job_id=job_id)


@figure_builder_bp.route('/api/figures/<figure_id>/export/<job_id>', methods=['GET'])
def export_status(figure_id, job_id):
    from plexora.plugins.figure_builder.server import export_jobs

    job = export_jobs.get(job_id)
    if job is None or job["figure_id"] != figure_id:
        return jsonify(success=False, error="unknown_job"), 404
    if job["status"] == "unavailable":
        # A format this build cannot write. 501 rather than 500: the request was
        # fine and the answer is an install line, not a bug report.
        return jsonify(success=False, error="format_unavailable",
                       detail=job["error"], job=job), 501
    return api.json_response({"success": True, "job": job})


@figure_builder_bp.route('/api/figures/<figure_id>/export/<job_id>', methods=['DELETE'])
def cancel_export(figure_id, job_id):
    """Ask a running export to stop.

    Cancelling changes nothing about the figure -- it is a render, not an edit
    -- and anything half-written is removed, because a partial PDF in the
    downloads folder is worse than no PDF: it looks finished.
    """
    from plexora.plugins.figure_builder.server import export_jobs

    job = export_jobs.get(job_id)
    if job is None or job["figure_id"] != figure_id:
        return jsonify(success=False, error="unknown_job"), 404
    export_jobs.cancel(job_id)
    return jsonify(success=True)


@figure_builder_bp.route('/api/figures/<figure_id>/export/<job_id>/download', methods=['GET'])
def download_export(figure_id, job_id):
    from plexora.plugins.figure_builder.server import export_jobs

    job = export_jobs.get(job_id)
    if job is None or job["figure_id"] != figure_id:
        return jsonify(success=False, error="unknown_job"), 404
    path = export_jobs.download_path(job_id)
    if not path:
        return jsonify(success=False, error="not_ready"), 409
    return send_file(path, as_attachment=True)


# -- pages of its own ----------------------------------------------------
#
# Two pages, neither of which is about a datasource. A figure spans several, so
# requiring one before it can be opened would make "open my figure" mean "guess
# which of its four images you meant" -- and the library is reached most often
# from the state where nothing at all is open.


def _page_data(**values):
    """The context base.html reads, built here rather than through core's
    `template_data`.

    That looks like duplication and is the boundary working: these are the
    plugin's own pages, base.html is the only thing they inherit, and a plugin
    importing `plexora.server.routes.page_routes` for a helper is a plugin that
    breaks when core reorganises its routes. The keys below are exactly the ones
    base.html reads, and the asset lists come from the descriptor so they cannot
    drift from what the tool path serves.
    """
    from plexora import get_config_names
    from plexora.server import plugins as plugin_registry

    base_url = app.config.get('PLEXORA_BASE_URL', '')
    data = {
        'datasource': '',
        'datasources': get_config_names(),
        'is_docker': app.config.get('IS_DOCKER', False),
        'base_url': base_url,
        'image_kind': '',
        'active_tool': '',
        'available_tools': [],
        'active_tool_scripts': PLUGIN.asset_urls('scripts', base_url),
        'active_tool_styles': PLUGIN.asset_urls('styles', base_url),
        'active_tool_panels': {},
        # base.html renders the File menu on every page, this one included --
        # so the entry that leads here has to be present here too, or the menu
        # loses items the moment the user follows one of them.
        'plugin_nav_items': plugin_registry.nav_items(app, base_url),
        'figure_builder_url': f"{base_url}{PLUGIN.url_prefix}",
        'figure_builder_version': VERSION,
    }
    data.update(values)
    return data


@figure_builder_bp.route('/figures', methods=['GET'])
def library_page():
    """The figure library: every figure on this machine."""
    return render_template('figure_builder/library.html', data=_page_data())


@figure_builder_bp.route('/figure/<figure_id>', methods=['GET'])
def figure_page(figure_id):
    """One figure, opened with no project."""
    try:
        schema.validate_figure_id(figure_id)
    except ValueError:
        return jsonify(success=False, error="unknown_figure"), 404
    if not repository.exists(figure_id):
        return jsonify(success=False, error="unknown_figure"), 404

    # `figure_id` rides on the page rather than being fetched, so the workspace
    # knows which figure it is without an extra round trip before it can start
    # the one that matters.
    return render_template('figure_builder/workspace.html',
                           data=_page_data(figure_id=figure_id))
