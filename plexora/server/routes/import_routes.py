"""Creating a project, and describing what was imported.

One entry point. The upload page asks for an image, optionally a segmentation
mask, and optionally a data file, and nothing else -- the format is detected
rather than picked from a tab, and everything the old two-step config forms
demanded up front is either worked out from the file or deferred until some
feature actually needs it (see plexora/api/plugin.py's Requires).

The one thing that cannot be deferred is anything ambiguous about the *file*:
a .zarr store with several tables, or a table spanning several images, cannot
be read at all until the user picks one. Those two inputs appear on the form
only when they apply, which is what /inspect_data is for.

After import, a CSV goes to the column-classification screen -- marker versus
metadata is a property of the dataset, so it is established once and stored
centrally. AnnData and SpatialData skip it: `var` and `obs` already draw that
line, so asking would be asking the user to confirm what the file says.
"""

import shutil
from dataclasses import replace
from pathlib import Path

from flask import jsonify, redirect, render_template, request

from plexora import app, get_config, get_config_names, data_path
from plexora.datasource import (
    _dedupe_dataset_name,
    _derive_dataset_name_from_path,
    _segmentation_channel_name,
    _segmentation_config_fields,
    _segmentation_spec,
    register_anndata_datasource,
    register_datasource,
    register_image_datasource,
)
from plexora.server.models import data_model
from plexora.server.models.adapters import (
    SUPPORTED_DATA_DESCRIPTION,
    detect_data_type,
)
from plexora.server.models.adapters import inspection as data_inspection
from plexora.server.models.adapters.spatialdata_adapter import list_spatialdata_tables
from plexora.server.models.project import (
    ROLE_LABELS,
    ColumnGroups,
    ColumnRoles,
    DataSpec,
    Project,
)
from plexora.server.routes.page_routes import template_data


def _base_url():
    return app.config.get('PLEXORA_BASE_URL', '')


def trim_filepath_quotes(path):
    """Drag-and-drop from a file manager often brings the quotes with it, and
    on Windows a copied path is quoted as a matter of course."""
    if not path:
        return path
    path = str(path).strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
        path = path[1:-1]
    return path.strip()


def _resolved(path):
    return Path(trim_filepath_quotes(path)).expanduser() if path else None


# --------------------------------------------------------------------------
# Inspecting a data file before it is imported
# --------------------------------------------------------------------------

@app.route('/inspect_data', methods=['POST'])
def inspect_data():
    """What the upload form needs to know about the path the user just typed.

    Called as the Data field changes, and again once a table inside a
    multi-table store has been picked. Answers three questions at once -- is
    this readable, what format is it, and is there anything about it that
    cannot be worked out -- so the form reveals a table, matrix or subset
    picker only when the file genuinely forces the choice. One request rather
    than one per format is what lets the page have a single Data input.

    `table` names which table inside a .zarr store to look at. It is what makes
    the second call possible: everything after the table choice -- which matrix
    holds the intensities, whether the table spans several images -- is a
    question ABOUT a table, and a store with several has no answer to any of it
    until one is named.
    """
    payload = request.get_json(silent=True) or {}
    path = _resolved(payload.get('path'))
    chosen_table = (payload.get('table') or '').strip() or None
    if not path:
        return jsonify(ok=False, error="No path given"), 400

    try:
        data_type = detect_data_type(path)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 200

    result = {"ok": True, "data_type": data_type, "tables": [], "ambiguous": [],
              "layers": []}

    if data_type == "csv":
        # Nothing about a CSV is ambiguous at this stage: it has one table and
        # its columns get confirmed on the classification screen anyway.
        return jsonify(result)

    if data_type == "spatialdata":
        try:
            tables = list_spatialdata_tables(path)
        except Exception as exc:  # a malformed store, not a bug
            return jsonify(ok=False, error=f"Could not read the store: {exc}"), 200
        if not tables:
            return jsonify(ok=False, error="This .zarr store holds no tables."), 200
        result["tables"] = tables
        names = [t["name"] for t in tables]
        if chosen_table in names:
            table = chosen_table
        elif len(tables) > 1:
            # Nothing else is answerable yet. The form asks again with the
            # table once the user picks one -- which it must, because
            # otherwise the matrix and subset questions are never put, and an
            # import that should have asked "raw counts or log values?"
            # silently reads X.
            return jsonify(result)
        else:
            table = names[0]
    else:
        table = None

    try:
        inspection = _inspect(path, data_type, table)
    except Exception as exc:
        return jsonify(ok=False, error=f"Could not read the file: {exc}"), 200

    proposal = data_inspection.propose_read_spec(inspection)
    result["table"] = table
    # A file with extra expression matrices does not say which one holds the
    # values to threshold on -- raw counts and a log-transformed copy live side
    # by side, and picking X for the user silently decides what every marker
    # histogram in the app is a histogram of.
    result["layers"] = list(inspection.get("layers") or [])
    result["ambiguous"] = [
        {"column": column,
         "values": next((c.get("values") or [] for c in inspection["obs_columns"]
                         if c.get("name") == column), [])}
        for column in proposal["ambiguous"]
    ]
    return jsonify(result)


@app.route('/list_spatialdata_tables', methods=['POST'])
def list_spatialdata_tables_route():
    """The tables inside a .zarr store, for the picker that appears when there
    is more than one."""
    payload = request.get_json(silent=True) or {}
    path = _resolved(payload.get('path'))
    if not path or not path.is_dir():
        return jsonify(ok=False, error="Not a .zarr store"), 200
    try:
        return jsonify(ok=True, tables=list_spatialdata_tables(path))
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 200


def _inspect(path, data_type, table=None):
    if data_type == "csv":
        return data_inspection.inspect_csv(path)
    if data_type == "spatialdata":
        return data_inspection.inspect_spatialdata_table(path, table)
    return data_inspection.inspect_anndata(path)


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def _features_layer(value):
    """Which matrix the marker intensities come from, as the requirements modal
    and the edit page send it -- never the import form, which does not ask.

    `"X"` is the main matrix and `"layer:<name>"` names one of `adata.layers`.
    Prefixed rather than sent bare so that a layer called "X" -- which anndata
    permits -- cannot be confused with the main matrix. Returns the layer name,
    or None for X and for a caller that did not name one.
    """
    value = (value or "").strip()
    return value.split(":", 1)[1] or None if value.startswith("layer:") else None


def _fail(message, form=None):
    """Re-render the upload page with an error and whatever the user typed, so
    a rejected import does not throw the paths away."""
    return render_template(
        "upload.html",
        data=template_data(error=message, **(form or {})),
    ), 400


@app.route('/import', methods=['POST'])
def import_project():
    """Create a project from an image, an optional mask and optional data."""
    form = request.form
    name = (form.get('name') or '').strip()
    image_path = _resolved(form.get('image_file'))
    mask_path = _resolved(form.get('label_file'))
    data_file = _resolved(form.get('data_file'))
    table = (form.get('data_table') or '').strip() or None
    subset_column = (form.get('subset_column') or '').strip() or None
    subset_value = (form.get('subset_value') or '').strip() or None
    keep = {"form_name": name, "form_image": form.get('image_file'),
            "form_mask": form.get('label_file'), "form_data": form.get('data_file')}

    if not image_path or not image_path.exists():
        return _fail("Provide a valid path to the image file.", keep)
    if not name:
        name = _dedupe_dataset_name(_derive_dataset_name_from_path(image_path),
                                    get_config_names())
    if name in get_config():
        return _fail(f"A project named {name!r} already exists.", keep)
    if mask_path and not mask_path.exists():
        return _fail("Provide a valid path to the segmentation mask.", keep)

    if not data_file:
        # Image only, plus a mask if one was given. This is a complete project:
        # everything else is something a feature will ask for later.
        try:
            _register_image_only(name, image_path, mask_path)
        except Exception as exc:
            return _fail(f"Could not register the image: {exc}", keep)
        return redirect(f"{_base_url()}/{name}")

    if not data_file.exists():
        return _fail(f"No such data file: {data_file}", keep)
    try:
        data_type = detect_data_type(data_file)
    except ValueError as exc:
        return _fail(str(exc), keep)

    try:
        if data_type == "csv":
            _register_csv(name, image_path, mask_path, data_file)
            # The one screen that survives from the old two-step import, and
            # the only one: which columns are markers is a fact about the data
            # that every plugin then reads, so it is worth one confirmation.
            return redirect(f"{_base_url()}/project/{name}/columns")
        # No features_layer: an import lands on X, and which matrix to read is
        # asked by the first plugin that reads intensities (Requires(features=
        # True)), where it belongs. The layer names are recorded here either
        # way, so the modal can offer them without reopening the file.
        _register_anndata(name, image_path, mask_path, data_file, data_type,
                          table, subset_column, subset_value)
    except ValueError as exc:
        return _fail(str(exc), keep)
    except Exception as exc:
        return _fail(f"Could not import {data_file.name}: {exc}", keep)

    return redirect(f"{_base_url()}/{name}")


def _register_image_only(name, image_path, mask_path):
    register_image_datasource(name=name, image=image_path)
    if mask_path:
        attach_segmentation(name, mask_path)


def _register_csv(name, image_path, mask_path, csv_path):
    """Register a CSV project, copying the table into the project directory."""
    local_csv = _copy_into_project(name, csv_path)
    inspection = data_inspection.inspect_csv(local_csv)
    roles = inspection["roles"]
    register_datasource(
        name=name,
        image=image_path,
        features=local_csv,
        # The predictor's guesses, which the classification screen confirms
        # next. register_datasource validates that they exist.
        x=roles.get("x"),
        y=roles.get("y"),
        id_column=roles.get("cell_id"),
        celltype_column=roles.get("celltype"),
        segmentation=mask_path,
        segmentation_async=bool(mask_path),
    )


def _register_anndata(name, image_path, mask_path, features_path, data_type,
                      table, subset_column, subset_value):
    """Register an .h5ad or one table of a .zarr store, reading `X`.

    Which matrix to read is deliberately not a parameter. It only matters to a
    plugin that reads marker intensities, so it is that plugin's requirement
    (`Requires(features=True)`) and the modal collects it -- along with whether
    to log-transform -- the first time such a tool opens. Importing is not the
    moment to answer a question about thresholding, and this path asks only what
    registering the project cannot proceed without. The layer names are recorded
    from the inspection below either way, so the modal can offer them without
    reopening the file.
    """
    if data_type == "spatialdata" and not table:
        tables = list_spatialdata_tables(features_path)
        if len(tables) != 1:
            raise ValueError(
                "This .zarr store holds several tables -- choose which one to load."
            )
        table = tables[0]["name"]

    inspection = _inspect(features_path, data_type, table)
    proposal = data_inspection.propose_read_spec(inspection)
    if proposal["ambiguous"] and not subset_column:
        raise ValueError(
            f"{features_path.name} spans several images "
            f"(column {proposal['ambiguous'][0]!r}) -- choose which one to load."
        )

    coordinates = proposal["coordinates"]
    register_anndata_datasource(
        name=name,
        image=image_path,
        features=features_path,
        table=table,
        segmentation=mask_path,
        segmentation_async=bool(mask_path),
        coordinate_source=coordinates.get("source"),
        obsm_key=coordinates.get("obsm_key"),
        x=coordinates.get("x_column"),
        y=coordinates.get("y_column"),
        feature_source="X",
        subset_by=subset_column,
        subset_value=subset_value,
    )


def attach_segmentation(name, mask_path, mode=None):
    """Point a project at a segmentation mask and start its conversion.

    Shared by import, the edit page and the requirements modal, because a mask
    arrives by all three routes and the work is identical: fingerprint the
    source, record it, then let the background job derive the pyramid and patch
    the project when it lands.
    """
    from plexora.server.utils import segmentation_pyramid

    dataset_dir = data_path / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    fields, pending = _segmentation_config_fields(
        Path(mask_path) if mask_path else None, dataset_dir,
        segmentation_async=bool(mask_path), segmentation_mode=mode,
    )

    def _apply(project):
        image = project.image
        channels = list(image.channels)
        has_area = any(c.get("fullname") == "Area" for c in channels)
        if mask_path and not has_area:
            # The viewer expects the mask layer first in imageData.
            label_name = _segmentation_channel_name(mask_path)
            channels.insert(0, {"name": "Area", "fullname": "Area",
                                "src": f"/generated/data/{name}/{label_name}/"})
        elif not mask_path and has_area:
            channels = [c for c in channels if c.get("fullname") != "Area"]
        return project.patch(
            image=replace(image, channels=tuple(channels)),
            segmentation=_segmentation_spec(fields),
        )

    updated = Project.mutate(name, _apply)
    if updated is not None and pending:
        data_model.start_segmentation_job(
            name, pending, dataset_dir, fields["segmentationMode"]
        )
    return updated


def replace_project_data(name, data_path_str, payload=None):
    """Attach a feature table to a project, or swap the one it has.

    Used by the edit page and by the requirements modal, so a project that
    started as an image only becomes a full one by the same route an existing
    project changes its data file by. Re-detects the format rather than
    assuming the new file matches the old: swapping a CSV for an .h5ad is an
    ordinary thing to want, and it is what the old edit path silently
    corrupted.

    Roles that still name a real column survive the swap; the rest are cleared,
    because a role pointing at a column that no longer exists is worse than an
    unanswered one -- whatever needs it will ask again.
    """
    payload = payload or {}
    project = Project.find(name)
    if project is None:
        raise ValueError(f"Unknown project: {name!r}")

    if not data_path_str:
        # Clearing the data file. The image and mask stay; every table-derived
        # fact goes, since none of it describes anything any more.
        return Project.mutate(
            name, lambda p: p.patch(dataset=None).forget_table_answers())

    source = Path(data_path_str).expanduser()
    if not source.exists():
        raise ValueError(f"No such data file: {source}")
    data_type = detect_data_type(source)

    table = (payload.get("table") or "").strip() or None
    if data_type == "spatialdata" and not table:
        tables = list_spatialdata_tables(source)
        if len(tables) != 1:
            raise ValueError(
                "This .zarr store holds several tables -- choose which one to load."
            )
        table = tables[0]["name"]

    inspection = _inspect(source, data_type, table)
    # Both branches produce markers/metadata/roles: inspect_csv classifies the
    # header directly, propose_read_spec folds classify_from_inspection into the
    # read spec. Read the classification off `proposal` in both -- the raw
    # AnnData inspection has var_names/obs_columns and no such keys.
    proposal = (inspection if data_type == "csv"
                else data_inspection.propose_read_spec(inspection))

    if data_type == "csv":
        source = _copy_into_project(name, source)
        spec_kwargs = {"coordinates": {}, "features": {}, "obs_id_field": None,
                       "obs_columns": (), "layers": ()}
    else:
        subset_column = (payload.get("subset_column") or "").strip() or None
        if proposal["ambiguous"] and not subset_column:
            raise ValueError(
                f"{source.name} spans several images "
                f"(column {proposal['ambiguous'][0]!r}) -- choose which one to load."
            )
        layer = _features_layer(payload.get("features_layer"))
        if layer and layer not in (inspection.get("layers") or []):
            raise ValueError(f"{source.name} has no layer named {layer!r}.")
        spec_kwargs = {
            "coordinates": proposal["coordinates"],
            "features": ({"source": "layer", "layer": layer} if layer
                         else proposal["features"]),
            "obs_id_field": None,
            "subset": ({"column": subset_column,
                        "value": (payload.get("subset_value") or "").strip()}
                       if subset_column else {}),
            # The file's own annotation columns, which is what the role
            # questions are asked about for these formats -- unlike `metadata`
            # below, which is what the adapter's table ends up holding.
            "obs_columns": tuple(
                str(c["name"]) for c in (inspection.get("obs_columns") or ())
                if c.get("name")
            ),
            "layers": tuple(str(name) for name in (inspection.get("layers") or ())),
        }

    known = set(proposal["markers"]) | set(proposal["metadata"])
    if data_type != "csv":
        # The adapter synthesizes these regardless of what the file calls them.
        known |= {"id", "X", "Y"}

    def _apply(current):
        kept = {role: column for role, column in current.roles.to_dict().items()
                if column in known}
        roles = ColumnRoles(**{**proposal["roles"], **kept})
        if data_type != "csv":
            roles = replace(roles, x="X", y="Y", cell_id="id")
        # Whatever the user confirmed about the old table described columns
        # that may not exist in this one, so those answers are dropped and the
        # fresh predictions go back in front of them. The mask and the cell
        # layer are unaffected -- neither is a fact about the table.
        current = current.forget_table_answers()
        return current.patch(dataset=DataSpec(
            type=data_type,
            src=str(source),
            table=table,
            roles=roles,
            columns=ColumnGroups(markers=tuple(proposal["markers"]),
                                 metadata=tuple(proposal["metadata"])),
            **spec_kwargs,
        ))

    return Project.mutate(name, _apply)


def _copy_into_project(name, csv_path):
    """A quantification CSV is small next to the image, and a project that
    keeps working after the user tidies their downloads folder is worth the
    disk. AnnData and SpatialData are referenced in place -- those are not
    small."""
    dataset_dir = data_path / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    local = dataset_dir / csv_path.name
    if csv_path.resolve() != local.resolve():
        shutil.copy2(csv_path, local)
    return local


# --------------------------------------------------------------------------
# Column classification -- marker versus metadata
# --------------------------------------------------------------------------

@app.route('/project/<string:name>/columns', methods=['GET'])
def project_columns_page(name):
    """Confirm which columns are markers and which are measurements.

    Only reachable for CSV: the other formats already carry the distinction in
    their own structure. The prediction is made server-side by the same
    classifier every other path uses, so what the user is correcting is the
    same guess the rest of the app would have made.
    """
    project = Project.find(name)
    if project is None or not project.has_table:
        return redirect(f"{_base_url()}/open_project")

    columns = project.columns
    if not columns.all:
        inspection = _inspect(Path(project.dataset.src), project.dataset.type,
                              project.dataset.table)
        markers, metadata = inspection["markers"], inspection["metadata"]
    else:
        markers, metadata = list(columns.markers), list(columns.metadata)

    return render_template("project_columns.html", data=template_data(
        datasetName=name,
        markers=markers,
        metadata=metadata,
        roles=project.roles.to_dict(),
        # The labels come from the server so the wording is identical here, in
        # the requirements modal, and on the edit page.
        roleLabels=dict(ROLE_LABELS),
        segmentation_pending=project.segmentation.pending,
    ))


@app.route('/project/<string:name>/columns', methods=['POST'])
def save_project_columns(name):
    """Store the confirmed split centrally, where every plugin reads it."""
    payload = request.get_json(silent=True) or {}
    updated = Project.mutate(name, lambda p: _apply_columns(p, payload))
    if updated is None:
        return jsonify(success=False, error="Unknown project"), 404

    data_model.load_datasource(name, reload=True)
    return jsonify(success=True, segmentation_pending=updated.segmentation.pending)


def _apply_columns(project, payload):
    project = project.with_columns(payload.get("markers") or [],
                                   payload.get("metadata") or [])
    roles = payload.get("roles") or {}
    project = project.with_roles(roles)
    # This screen IS the confirmation for everything it shows, so a plugin
    # opened afterwards must not put the same split and the same role selects
    # in front of the user a second time. Only the roles that came back with a
    # column are confirmed: a select left on "Choose a column..." was not
    # answered, and something may still need it.
    return project.with_confirmed(
        ["markers"] + [f"role:{role}" for role, column in roles.items() if column])


# --------------------------------------------------------------------------
# Small checks the upload form makes as the user types
# --------------------------------------------------------------------------

@app.route('/check_file_existence', methods=['POST'])
def check_file_existence():
    payload = request.get_json(silent=True) or {}
    path = _resolved(payload.get('path'))
    return jsonify(exists=bool(path and path.is_file()))


@app.route('/check_path_existence', methods=['POST'])
def check_path_existence():
    """Exists as either a file or a directory -- a .zarr store is a directory,
    and the single Data input accepts both."""
    payload = request.get_json(silent=True) or {}
    path = _resolved(payload.get('path'))
    return jsonify(exists=bool(path and path.exists()))


@app.route('/dataset_existence', methods=['POST'])
def check_dataset_exists():
    payload = request.get_json(silent=True) or {}
    return jsonify(exists=(payload.get('datasetName') in get_config()))


@app.route('/supported_data_formats')
def supported_data_formats():
    """So the form's help text and the server's rejection message cannot
    disagree about what the Data input takes."""
    return jsonify(description=SUPPORTED_DATA_DESCRIPTION)
