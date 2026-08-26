"""The ROI plugin's HTTP surface.

Thin on purpose: parse, hand to the repository, translate the outcome. Every
rule about what an annotation is lives in `operations`/`schema`/`geometry`, and
every rule about who may overwrite whom lives in `repository`, so there is one
place to read each and no route can quietly disagree with another.

Status codes carry meaning here, because the client acts on them differently:

    400  the request is wrong -- a malformed geometry, an unknown category.
         Fix the request; retrying it unchanged will fail the same way.
    409  the request was fine but somebody else wrote first. The client has
         work worth keeping, so this opens the conflict prompt rather than
         discarding either side.
    422  the image underneath these annotations has changed. Nothing is
         written and nothing is drawn until the user decides.
"""

import json

from flask import Blueprint, Response, jsonify, request

from plexora import api
from plexora.plugins.roi import VERSION
# Imported for its side effect as much as for its names: importing it is what
# registers this plugin's table operations, and the routes below name them. It
# is import-light on purpose (see the module docstring) so this costs nothing.
from plexora.plugins.roi.server import geojson, schema, tableops
from plexora.plugins.roi.server.repository import (
    ConflictError,
    ImageMismatch,
    ROIRepository,
)

# template_folder/static_folder make this plugin self-contained: Flask's
# DispatchingJinjaLoader already searches every blueprint's template folder, and
# the blueprint serves its own assets.
roi_bp = Blueprint(
    'roi', __name__,
    template_folder='../templates',
    static_folder='../static',
    static_url_path='/static',
)

#: Biggest request body accepted. A batch of edits is kilobytes; a GeoJSON
#: import of a few thousand traced contours is megabytes. Past this the request
#: is a mistake, and the client is told so rather than the server parsing it.
MAX_BODY_BYTES = 64 * 1024 * 1024


def _payload():
    """The request's JSON body, or a ValueError worth showing the user."""
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        raise ValueError(f"request body is larger than {MAX_BODY_BYTES // (1024 * 1024)} MB")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    return body


def _repository(datasource):
    """A repository for a datasource that exists, or a KeyError."""
    if not datasource:
        raise KeyError(datasource)
    api.dataset(datasource)  # raises KeyError for an unknown project
    return ROIRepository(datasource)


def _mismatch_response(exc):
    return jsonify(
        success=False,
        error="image_dimensions_changed",
        stored_image_size=exc.stored,
        image_size=exc.current,
    ), 422


def _image_id_or_none(datasource):
    """This project's image id, swallowing the ambiguous case.

    Used by the two read-only routes -- the export and the panel's opening
    question -- where an unresolvable column must not cost the user their
    download or their panel. `map_to_cells` calls `mapping.current_image_id`
    directly and lets the ValueError through, because there the ambiguity is
    the difference between annotating the right cells and the wrong ones.
    """
    from plexora.plugins.roi.server import mapping

    try:
        return mapping.current_image_id(api.dataset(datasource))
    except (KeyError, ValueError):
        return None


@roi_bp.route('/api/state', methods=['GET'])
def get_state():
    """Everything the panel draws from: categories, this image's regions, the
    revision to write against, and whether the image still matches."""
    try:
        repository = _repository(request.args.get('datasource'))
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    try:
        state = repository.load()
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    entry = state["images"].get(schema.DEFAULT_IMAGE) or schema.empty_image()
    return api.json_response({
        "success": True,
        "schema_version": state["schema_version"],
        "revision": state["revision"],
        "categories": state["categories"],
        "image": schema.DEFAULT_IMAGE,
        "coordinate_space": entry["coordinate_space"],
        "features": entry["features"],
        **repository.status(state),
    })


@roi_bp.route('/api/operations', methods=['POST'])
def post_operations():
    """Apply a batch of edits, if the client is writing against the revision it
    last read. The batch is all-or-nothing."""
    try:
        body = _payload()
        repository = _repository(body.get('datasource'))
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    try:
        revision = repository.apply(body.get('base_revision'), body.get('operations'))
    except ConflictError as exc:
        return jsonify(success=False, error="stale_revision",
                       revision=exc.current_revision), 409
    except ImageMismatch as exc:
        return _mismatch_response(exc)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    return jsonify(success=True, revision=revision)


@roi_bp.route('/api/export.geojson', methods=['GET'])
def export_geojson():
    """The whole annotation project as a file.

    Deliberately has no preconditions beyond the project existing: export is the
    escape hatch, and the moments a user most needs it -- a failed save, a
    conflict, an image that no longer matches -- are exactly the moments a
    stricter route would refuse.
    """
    datasource = request.args.get('datasource')
    try:
        repository = _repository(datasource)
        state = repository.load()
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    document = geojson.export_document(state, datasource, VERSION,
                                       image_id=_image_id_or_none(datasource))
    return Response(
        json.dumps(document, indent=2),
        mimetype=geojson.MIME_TYPE,
        headers={"Content-disposition": f"attachment; filename={datasource}_rois.geojson"},
    )


@roi_bp.route('/api/import', methods=['POST'])
def import_geojson():
    """Add a GeoJSON document's regions to this project.

    A dimension mismatch comes back as `success: false` with a warning rather
    than an error: it is a question for the user, and the answer is normally no.
    Re-post with `accept_dimension_mismatch` to go ahead anyway.
    """
    try:
        body = _payload()
        repository = _repository(body.get('datasource'))
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    document = body.get('document')
    width, height = repository.image_size()
    errors, warnings = geojson.validate_document(document, image_size=(width, height))
    if errors:
        return jsonify(success=False, error="; ".join(errors)), 400
    if warnings.get("dimension_mismatch") and not body.get("accept_dimension_mismatch"):
        return jsonify(success=False, warning="dimension_mismatch",
                       **warnings["dimension_mismatch"])

    try:
        state = repository.load()
        operation, report = geojson.import_features(state, document)
        revision = repository.apply(body.get('base_revision'), [operation])
    except ConflictError as exc:
        return jsonify(success=False, error="stale_revision",
                       revision=exc.current_revision), 409
    except ImageMismatch as exc:
        return _mismatch_response(exc)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    return jsonify(success=True, revision=revision, operation=operation, **report)


# -- native export adapters ---------------------------------------------
#
# Everything below writes into the file the project was imported from, which is
# the user's own data. All of it is explicit, none of it happens on a save, and
# none of it replaces an existing element without being told to.


@roi_bp.route('/api/adapters/destination', methods=['GET'])
def adapter_destination():
    """Where this project's annotations can go, and what to call them there.

    One route because it is one question, asked once when the panel opens:
    which native destination this project has (a CSV has none -- a CSV cannot
    hold a polygon, and the honest companion for one is the GeoJSON sidecar),
    what the entry is called by default, which names are already taken in the
    user's file, and which one this project last wrote to.

    Answering it in pieces meant two round trips and a `window.prompt` that
    re-asked from scratch every time.
    """
    from plexora.plugins.roi.server import adapters

    datasource = request.args.get('datasource')
    try:
        dataset = api.dataset(datasource)
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    kind = dataset.source_kind
    payload = {"success": True, "source_kind": kind, "kind": None,
               "label": "", "default_name": "", "existing": [],
               # Whether there are cells to map regions onto at all. Answered
               # here rather than in a route of its own because the panel needs
               # it at the same moment it needs everything else on this payload,
               # and one question the panel asks once should stay one request.
               "has_table": bool(dataset.table.available),
               "image_id": _image_id_or_none(datasource),
               "remembered": ROIRepository(datasource).destination()}

    # Listing is best-effort by design: it is a convenience for naming, and a
    # file that cannot be listed right now must not cost the user their panel.
    # The write path checks again, for real, and refuses there. That is also
    # why an unreachable node is caught here rather than allowed to fail the
    # panel -- the names are a nicety, and the refusal that matters happens on
    # the write.
    if kind in ('anndata', 'spatialdata'):
        try:
            existing = dataset.table.run("roi.destinations", {}).get("existing", [])
        except Exception:
            existing = []
        if kind == 'anndata':
            payload.update(kind="anndata", label="AnnData (.uns)",
                           default_name=adapters.DEFAULT_UNS_KEY,
                           existing=existing)
        else:
            payload.update(kind="spatialdata", label="SpatialData shapes",
                           default_name=adapters.DEFAULT_ELEMENT,
                           existing=existing)

    return jsonify(**payload)


@roi_bp.route('/api/adapters/anndata', methods=['POST'])
def save_to_anndata():
    """Write the annotations into the source .h5ad at uns/plexora/<key>.

    Only that subtree is rewritten -- never a read-and-write-back of the whole
    file, which would rebuild X and obs and turn an annotation export into a
    rewrite of the user's measurements.

    An existing key is refused unless `replace` says otherwise, so one file can
    carry several annotation passes and no pass can quietly land on another.
    """
    try:
        body = _payload()
        dataset = api.dataset(body.get('datasource'))
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    repository = ROIRepository(dataset.name)
    result = dataset.table.run("roi.save_anndata", {
        "state": repository.load(),
        "plugin_version": VERSION,
        "key": body.get('key'),
        "replace": bool(body.get('replace')),
    })
    if not result.get("ok"):
        if result.get("reason") == tableops.KEY_EXISTS:
            return jsonify(success=False, error="key_exists",
                           keys=result["existing"], suggestion=result["suggestion"])
        return jsonify(success=False, error=result.get("message", "")), 400

    repository.remember_destination(result["name"])
    return jsonify(success=True, **{k: v for k, v in result.items() if k != "ok"})


@roi_bp.route('/api/adapters/spatialdata', methods=['POST'])
def save_to_spatialdata():
    """Write the annotations into the store as a shapes element."""
    try:
        body = _payload()
        dataset = api.dataset(body.get('datasource'))
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    # Passed through as given: naming, defaulting and validation are the
    # adapter's, so there is one rule rather than two that can disagree.
    repository = ROIRepository(dataset.name)
    result = dataset.table.run("roi.save_spatialdata", {
        "state": repository.load(),
        "element_name": body.get('element_name'),
    })
    if not result.get("ok"):
        if result.get("reason") == tableops.ELEMENT_EXISTS:
            # Not an error: naming an element that is already there is an
            # ordinary thing to do, and the answer is a different name -- never
            # a silent overwrite of a layer that may be somebody's
            # segmentation. Unlike the AnnData branch there is no `replace`:
            # spatialdata's own writer refuses it, and delete-then-rewrite has
            # a window where the user has neither.
            return jsonify(success=False, error="element_exists",
                           elements=result["existing"],
                           suggestion=result["suggestion"])
        return jsonify(success=False, error=result.get("message", "")), 400

    repository.remember_destination(result["name"])
    return jsonify(success=True, **{k: v for k, v in result.items() if k != "ok"})


@roi_bp.route('/api/map_to_cells', methods=['POST'])
def map_to_cells():
    """Annotate this project's cells with the regions they fall inside.

    The other direction from everything above: those routes write the polygons
    into the user's file, this one writes onto the rows. Two columns --
    `<name>_category` and `<name>_name` -- so the result is something a user can
    group by without parsing anything.

    Two `needs` replies rather than error text. A cell id is what lines a label
    up with a row, and an image id is what stops one image's regions being
    written onto another's cells; when either is unanswered the client asks for
    it through the requirements modal and retries. The client branches on this
    field, never on the message -- wording is not an API.
    """
    from plexora.plugins.roi.server import mapping, tableops

    try:
        body = _payload()
        dataset = api.dataset(body.get('datasource'))
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except KeyError:
        return jsonify(success=False, error="Unknown datasource"), 400

    if not dataset.table.available:
        return jsonify(success=False,
                       error="This project has no cell-level data",
                       needs="table"), 400

    schema_ = dataset.schema
    if schema_ is None or not schema_.cell_id:
        return jsonify(success=False,
                       error="No cell ID column recorded for this project",
                       needs="role:cell_id"), 400

    try:
        # Deliberately not swallowed the way the read-only routes swallow it: a
        # column that resolves to several images means Plexora cannot tell whose
        # cells these are, and guessing writes labels onto somebody else's.
        mapping.current_image_id(dataset)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc), needs="role:image_id"), 400

    repository = ROIRepository(dataset.name)
    state = repository.load()
    entry = state["images"].get(schema.DEFAULT_IMAGE) or schema.empty_image()
    if not entry["features"]:
        return jsonify(success=False, error="There are no ROIs to map"), 400

    # The join and the write go out as one operation, because both need the
    # table's file and the loaded frame to be the same machine's -- see
    # tableops.map_to_cells. For an ordinary project this is a direct call.
    result = dataset.table.run("roi.map_to_cells", {
        "features": entry["features"],
        "categories": state["categories"],
        "x_column": schema_.x,
        "y_column": schema_.y,
        "prefix": body.get('name'),
        "replace": bool(body.get('replace')),
    })
    if not result.get("ok"):
        if result.get("reason") == tableops.COLUMN_EXISTS:
            return jsonify(success=False, error="column_exists",
                           columns=result["existing"],
                           suggestion=result["suggestion"])
        return jsonify(success=False, error=result.get("message", "")), 400

    return jsonify(success=True, n_rois=len(entry["features"]),
                   **{k: v for k, v in result.items() if k != "ok"})
