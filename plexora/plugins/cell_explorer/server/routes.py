"""Cell Explorer's HTTP surface.

Thin on purpose: parse, hand to the right module, translate the outcome. What a
variable is lives in `variables`, how values are packed lives in `values`, and
who may overwrite whom lives in `state`, so there is one place to read each and
no route can quietly disagree with another.

Status codes carry meaning, because the client acts on them differently:

    400  the request is wrong -- an unknown datasource, a column this project
         does not have. Retrying it unchanged will fail the same way.
    409  the request was fine but somebody else saved first. The client has
         preferences worth keeping, so this opens a prompt rather than
         discarding either side.
    422  the stored settings are from a newer Plexora. Nothing is written and
         the panel says so, rather than overwriting what it cannot read.
"""

from flask import Blueprint, Response, jsonify, request

from plexora import api
from plexora.plugins.cell_explorer.server import values as value_encoder
from plexora.plugins.cell_explorer.server import variables
from plexora.plugins.cell_explorer.server.state import (
    CellExplorerRepository,
    ConflictError,
    UnreadableState,
)

# template_folder/static_folder make this plugin self-contained: Flask's
# DispatchingJinjaLoader already searches every blueprint's template folder, and
# the blueprint serves its own assets.
cell_explorer_bp = Blueprint(
    'cell_explorer', __name__,
    template_folder='../templates',
    static_folder='../static',
    static_url_path='/static',
)


def _dataset(datasource):
    """A dataset for a datasource that exists, or a KeyError."""
    if not datasource:
        raise KeyError(datasource)
    return api.dataset(datasource)


@cell_explorer_bp.route('/api/variables', methods=['GET'])
def get_variables():
    """Every column that can be coloured by, described well enough to render
    the panel without asking for any values."""
    try:
        dataset = _dataset(request.args.get('datasource'))
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    if not dataset.table.available:
        # Not an error: a project can legitimately be an image and nothing else,
        # and the panel has an empty state that says what to do about it.
        return api.json_response({"success": True, "variables": [],
                                  "reason": "no_table"})

    return api.json_response({
        "success": True,
        "variables": variables.describe_all(dataset),
        # What core can draw cells with, so the panel can say "there is nothing
        # to draw these on" before it fetches a single value.
        "can_draw": {
            "segmentation": dataset.segmentation.available,
            "segmentation_pending": dataset.segmentation.pending,
            "centroids": bool(dataset.schema and dataset.schema.x and dataset.schema.y),
        },
    })


@cell_explorer_bp.route('/api/values', methods=['GET'])
def get_values():
    """One column's values as a packed binary buffer.

    Not JSON: a million cells is ordinary here, and the difference is ~30 MB of
    objects to parse against ~6 MB of typed array to read. See values.py for the
    layouts, which the client mirrors in cellExplorerApi.js.
    """
    column = request.args.get('column') or ''
    try:
        dataset = _dataset(request.args.get('datasource'))
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    descriptor = variables.find(dataset, column) if column else None
    if descriptor is None:
        return jsonify(success=False,
                       error=f"No such metadata column: {column!r}"), 400

    kind = request.args.get('kind')
    if kind in ("categorical", "continuous") and kind != descriptor["kind"]:
        # The user overrode the inference. Honoured only where the descriptor
        # carries both halves (see variables.describe), so an override cannot
        # ask for an encoding whose dictionary was never computed.
        if not descriptor.get("ambiguous"):
            return jsonify(success=False,
                           error=f"{column!r} cannot be read as {kind}"), 400
        descriptor = {**descriptor, "kind": kind}

    try:
        payload, resolved_kind, count = value_encoder.encode(dataset, column, descriptor)
    except (KeyError, ValueError) as exc:
        return jsonify(success=False, error=str(exc)), 400

    response = Response(payload, mimetype="application/octet-stream")
    response.headers["Content-Encoding"] = "gzip"
    response.headers["X-Cell-Count"] = str(count)
    response.headers["X-Value-Kind"] = resolved_kind
    # So a proxy or a browser cache cannot serve one column's buffer for
    # another's -- the URL differs, but the headers above are what the client
    # reads to decide how to decode it.
    response.headers["Cache-Control"] = "no-store"
    return response


@cell_explorer_bp.route('/api/state', methods=['GET'])
def get_state():
    """The stored display preferences, plus the revision to write against."""
    datasource = request.args.get('datasource')
    try:
        _dataset(datasource)
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    try:
        state = CellExplorerRepository(datasource).load()
    except UnreadableState as exc:
        return jsonify(success=False, error=str(exc),
                       schema_version=exc.schema_version), 422
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    return api.json_response({"success": True, **state})


@cell_explorer_bp.route('/api/state', methods=['POST'])
def post_state():
    """Store display preferences, if nobody else has written since the caller
    last read."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(success=False, error="expected a JSON object"), 400

    datasource = body.get('datasource') or request.args.get('datasource')
    try:
        _dataset(datasource)
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    try:
        state = CellExplorerRepository(datasource).save(
            body.get('revision'), body.get('settings'))
    except ConflictError as exc:
        return jsonify(success=False, error="conflict",
                       revision=exc.current_revision), 409
    except UnreadableState as exc:
        return jsonify(success=False, error=str(exc),
                       schema_version=exc.schema_version), 422
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    return api.json_response({"success": True, **state})
