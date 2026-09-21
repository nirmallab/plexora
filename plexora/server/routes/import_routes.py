"""The HTTP surface of importing, and the writers every surface shares.

Two halves, and the split is the point.

**The routes** are the import dialog's: `/import/inspect` says what a set of
paths holds, `/import/sample` registers it, `/import/layers` adds to a sample
that exists, and `/import/status` says what is still being prepared. Each is a
thin translation into `import_proposal` and `import_sample`, which do the work
and are testable without a request.

**The writers** are everybody's. `attach_segmentation` and
`replace_project_data` are what the edit page, the requirements modal, the Cells
control and the importer all call, and they are here because that is where they
have always been. A mask attached from any of those leaves an identical record
and starts an identical job -- which `tests/test_import_entry_points.py` asserts
rather than assumes.

What used to be here and is not: the three-field form (`POST /import`), the page
that rendered it, and the column-classification screen. The form could take an
image, a mask and a table and nothing else, which made a Xenium run six imports;
the columns screen asked the marker/metadata split up front, which is a
`confirm`-tier requirement the first tool that reads markers already asks better
(see plexora/api/plugin.py's Requires, and manifest.never_confirmed).
"""

import shutil
import time
import uuid
from dataclasses import replace
from pathlib import Path

from flask import jsonify, request

from plexora import app, get_config, get_config_names, paths
from plexora.datasource import (
    _segmentation_config_fields,
    _segmentation_spec,
    _with_area_channel,
    deferred_spec,
)
from plexora.server.models import data_model, datasets
from plexora.server.models.adapters import (
    SUPPORTED_DATA_DESCRIPTION,
    detect_data_type,
    is_flat_table,
)
from plexora.server.models.adapters import inspection as data_inspection
from plexora.server.models.adapters.spatialdata_adapter import list_spatialdata_tables
from plexora.server.models.project import (ColumnGroups, ColumnRoles, DataSpec,
                                           Project)


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
# Import Sample: inspect, then register
# --------------------------------------------------------------------------

def _picked(payload):
    """The paths a request named, tidied but not interpreted.

    A `node://<node>/<resource>` is left exactly as it is: the detector asks
    the node what it is serving, and `attach_segmentation` and
    `replace_project_data` both dispatch on that prefix themselves -- so
    splitting it apart here would mean putting it back together twice. Every
    other entry is trimmed of the quotes a file manager wraps a dragged path
    in and expanded, which is what every path field in the app does.
    """
    raw = payload.get('paths') or []
    if isinstance(raw, str):
        raw = [raw]
    picked = []
    for entry in raw:
        text = str(entry).strip()
        if not text:
            continue
        picked.append(text if text.startswith('node://')
                      else str(_resolved(text) or text))
    return picked


@app.route('/import/inspect', methods=['POST'])
def import_inspect():
    """What these files are, and what sample they would make.

    Called as picks accumulate and again whenever a question is answered, so it
    has to be cheap: nothing here opens pixels except the one bounded window
    that tells a small mask from a small grayscale photograph.

    `sample` scopes it to an existing project -- "+ Add Layer" -- which drops
    anything already registered and proposes everything else as a layer of it.
    """
    from plexora.server.models import import_proposal

    payload = request.get_json(silent=True) or {}
    proposal = import_proposal.inspect_paths(
        _picked(payload), node=(payload.get('node') or '').strip() or None,
        answers=payload.get('answers') or {},
        sample=(payload.get('sample') or '').strip() or None)
    return jsonify(proposal.to_dict())


@app.route('/import/sample', methods=['POST'])
def import_sample_route():
    """Register the sample these paths make, and say where to open it."""
    from plexora.server.models import import_sample as importer

    payload = request.get_json(silent=True) or {}
    try:
        result = importer.import_sample(
            _picked(payload), answers=payload.get('answers') or {},
            name=(payload.get('name') or '').strip() or None,
            dataset=payload.get('dataset'),
            node=(payload.get('node') or '').strip() or None,
            replace=(payload.get('replace') or '').strip() or None,
            index=int(payload.get('index') or 0))
    except importer.NameTaken as exc:
        # 409 with a free name rather than renaming silently: somebody who
        # typed a name meant it, and quietly filing their import under
        # `melanoma_2` is how two copies of one slide happen.
        return jsonify(error=str(exc), suggestion=exc.suggestion), 409
    except (importer.ImportError_, ValueError) as exc:
        return jsonify(error=str(exc)), 400

    result['redirect'] = f"{_base_url()}/{result['name']}"
    return jsonify(result)


@app.route('/import/layers', methods=['POST'])
def import_layers_route():
    """Add layers to a sample that already exists. "+ Add Layer"."""
    from plexora.server.models import import_sample as importer

    payload = request.get_json(silent=True) or {}
    sample = (payload.get('sample') or '').strip()
    if not sample:
        return jsonify(error="sample is required"), 400
    try:
        return jsonify(importer.add_layers(
            sample, _picked(payload), answers=payload.get('answers') or {},
            node=(payload.get('node') or '').strip() or None))
    except (importer.ImportError_, ValueError) as exc:
        return jsonify(error=str(exc)), 400


# --------------------------------------------------------------------------
# What a sample is still preparing
# --------------------------------------------------------------------------

@app.route('/import/status')
def import_status():
    """Everything one sample is still building, as one document.

    Replaces two polls with one. The mask had `/get_segmentation_status` and a
    percentage-band vocabulary; the transcripts plugin had
    `/plugins/transcripts/status` and a different one, private to it and lost
    on a restart. A third modality would have been a third endpoint, and the
    viewer would have had to know which to ask before it knew what it had.

    `/get_segmentation_status` stays exactly as it is -- it is what an import
    page polls and what a node's own progress surface uses -- and this is not a
    rename of it: it is the composition, and `layer_jobs.status` is where the
    composing happens.
    """
    from plexora.server.models import layer_jobs

    sample = (request.args.get('sample') or '').strip()
    if not sample:
        return jsonify(layers={}, pending=False), 400
    return jsonify(layer_jobs.status(sample))


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
    chosen_table = (payload.get('table') or '').strip() or None

    # A node address in the field: the same questions, answered by the node,
    # which is the only process that can open the file. Handled before
    # `_resolved`, which would otherwise turn the locator into a path that
    # exists nowhere and report an unreadable file -- a red border that then
    # BLOCKS the form from submitting a perfectly serveable table.
    try:
        located = _node_locator(payload.get('path'))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 200
    if located:
        return _inspect_on_node(located, chosen_table)

    path = _resolved(payload.get('path'))
    if not path:
        return jsonify(ok=False, error="No path given"), 400

    try:
        data_type = detect_data_type(path)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 200

    result = {"ok": True, "data_type": data_type, "tables": [], "ambiguous": [],
              "layers": []}

    if is_flat_table(data_type):
        # Nothing about a flat table is ambiguous at this stage: it has one
        # table and its columns get confirmed on the classification screen
        # anyway.
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
    result["ambiguous"] = _ambiguous_view(inspection, proposal)
    return jsonify(result)


def _ambiguous_view(inspection, proposal):
    columns = inspection.get("obs_columns") or []
    return [
        {"column": column,
         "values": next((c.get("values") or [] for c in columns
                         if c.get("name") == column), [])}
        for column in proposal.get("ambiguous") or []
    ]


def _inspect_on_node(located, chosen_table):
    """`/inspect_data`, when the file lives on a data node.

    The node's /inspect endpoint produces the same document the local
    inspection does, so this is a reshaping rather than a second
    implementation: the form's table, matrix and subset questions appear for a
    remote file exactly when they would for a local one.
    """
    from plexora import nodes as node_api

    node, resource_id = located
    try:
        document = node_api.inspect_table(node, resource_id, table=chosen_table)
    except KeyError:
        return jsonify(ok=False, error=f"No data node named {node!r} is "
                       f"registered here."), 200
    except Exception as exc:
        return jsonify(ok=False, error=f"The node {node!r} could not inspect "
                       f"{resource_id!r}: {exc}"), 200

    result = {"ok": True, "data_type": document.get("data_type"),
              "tables": list(document.get("tables") or []), "ambiguous": [],
              "layers": []}
    if "proposed" not in document:
        # A multi-table store with no table chosen yet: the form shows the
        # picker and asks again, exactly as for a local store.
        return jsonify(result)
    result["table"] = document.get("table")
    result["layers"] = list(document.get("layers") or [])
    result["ambiguous"] = _ambiguous_view(document, document["proposed"])
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
    if is_flat_table(data_type):
        return data_inspection.inspect_flat_table(path, data_type)
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


def _dataset_request(form):
    """`(dataset_id, new_name)` from the import form, checked before anything
    is registered.

    Checked early on purpose. The project is filed *after* it exists -- a
    dataset can only hold projects that do -- and by then the import has
    already written config.json and copied nothing back. A dataset id that has
    since been deleted, or a name that collides, would have to be reported
    against a project that is already registered, which leaves the user with
    the thing they asked for filed somewhere they did not ask for and an error
    page saying so. So the id is resolved here, where refusing still costs
    nothing but the form.

    Raises ValueError with something worth showing the user.
    """
    dataset_id = (form.get("dataset") or "").strip()
    new_name = (form.get("dataset_new") or "").strip()
    if new_name:
        # A name that is already taken is not an error: the picker offers
        # "New dataset..." to somebody who could not find the folder they
        # wanted, and two people typing the same cohort name mean the same
        # folder. Resolving it here is what keeps `create` below from
        # refusing on a duplicate.
        existing = datasets.find_by_name(new_name, known=get_config_names())
        return (existing.id, "") if existing else ("", new_name)
    if dataset_id and datasets.find(dataset_id, known=get_config_names()) is None:
        raise ValueError(
            "That dataset no longer exists. Choose another, or none.")
    return dataset_id, ""


def _file_under(name, dataset_id, new_name):
    """Put a freshly registered project in its dataset, if one was asked for.

    Never raises. Every caller is past the point of no return -- the project
    is registered and the response is a redirect into it -- so a dataset that
    vanished between the check above and here costs the filing and not the
    import. The project lands at the top level, where it is one drag from
    where it should be.
    """
    if not dataset_id and not new_name:
        return
    try:
        if new_name:
            datasets.create(new_name, projects=[name],
                            known=get_config_names())
        else:
            datasets.assign([name], dataset_id, known=get_config_names())
    except (datasets.DatasetError, OSError):
        pass


def _node_locator(value):
    """`(node, resource)` when this field names a data node, else None.

    Tested before anything treats the value as a path, because
    `Path("node://hpc/slide")` is a perfectly valid relative path that exists
    nowhere -- so the "provide a valid path" refusal would be the answer to a
    question the user did not ask.
    """
    from plexora.server.providers.base import NODE_SCHEME

    text = trim_filepath_quotes(value or "").strip()
    if not text.startswith(NODE_SCHEME):
        return None
    rest = text[len(NODE_SCHEME):].strip("/")
    node, _, resource = rest.partition("/")
    if not node or not resource:
        raise ValueError(
            f"{text!r} is not a data node address. Write it as "
            f"node://<node>/<resource>.")
    return node, resource


def _attach_node_resources(name, mask_node=None, data_node=None, table=None,
                           subset_column=None, subset_value=None):
    """Point a freshly registered project's mask and/or table at data nodes.

    Called from every import branch rather than only the one where the image is
    on a node too. Where each resource lives is an independent fact, and
    treating the mask's as a consequence of the image's made the commonest
    split of all impossible to state on the form: the slide on this machine and
    the mask left beside the segmentation job that wrote it.

    Any failure deletes the project. A half-registered one is worse than none:
    it appears in the picker, opens onto an error, and the user cannot import
    over it because the name is taken.
    """
    from plexora import nodes as node_api
    from plexora.server.models.project import Project

    def _attach(what, located, call):
        if not located:
            return
        try:
            call(located)
        except Exception as exc:
            Project.load(name).delete()
            # KeyError's str() carries its own quotes; a message read by a user
            # should not.
            because = str(exc).strip("'\"")
            raise ValueError(f"Could not attach the {what} from node "
                             f"{located[0]!r}: {because}") from exc

    _attach("segmentation mask", mask_node,
            lambda at: node_api.attach_segmentation(name, node=at[0],
                                                    resource_id=at[1]))
    _attach("table", data_node,
            lambda at: node_api.attach_table(
                name, node=at[0], resource_id=at[1], table=table,
                subset_column=subset_column, subset_value=subset_value))


def _register_node_image(name, image_node, data_file, mask_node=None,
                         data_node=None, table=None, subset_column=None,
                         subset_value=None, image_type=None):
    """Create a project whose image is on a node.

    `data_file` is a local path or nothing, and `data_node` is the same
    question answered the other way: the table can sit beside the user (an
    `.h5ad` that came back from a cluster sits on the laptop, and the slide it
    describes does not), on a node of its own, or be added later.

    `image_type` is the form's Image type override, and it reaches the node
    image the same way it reaches a local one. It used to be dropped here,
    which made the control on the form a control that did nothing for exactly
    the images whose type the form could say least about.
    """
    from plexora import nodes as node_api
    from plexora.server.models.project import ImageSpec, Project

    Project(name=name, image=ImageSpec()).save()
    try:
        node_api.attach_image(name, node=image_node[0], resource_id=image_node[1],
                              image_type=image_type)
    except Exception:
        # A half-registered project is worse than none -- see
        # _attach_node_resources, which takes the same care for the rest.
        Project.load(name).delete()
        raise

    _attach_node_resources(name, mask_node=mask_node, data_node=data_node,
                           table=table, subset_column=subset_column,
                           subset_value=subset_value)
    if data_file:
        try:
            # The same call the Edit page makes. A project that started as an
            # image only becomes a full one by exactly one route, so a table
            # attached here is inspected, classified and role-guessed
            # identically to one attached anywhere else.
            replace_project_data(name, str(data_file),
                                 {"table": table,
                                  "subset_column": subset_column,
                                  "subset_value": subset_value})
        except Exception:
            Project.load(name).delete()
            raise


#: Flat pictures. Not a pyramid, no channels, no tiles -- the same thing quick
#: view calls `rgb`, and the only image kind anything routes by EXTENSION.
#: Everything else is decided by reading the file.
_FLAT_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")


def attach_segmentation(name, mask_path, mode=None):
    """Point a project at a segmentation mask and start its conversion.

    Shared by import, the edit page and the requirements modal, because a mask
    arrives by all three routes and the work is identical: fingerprint the
    source, record it, then let the background job derive the pyramid and patch
    the project when it lands.

    `mask_path` may name a data node instead (`node://<node>/<resource>`), and
    the dispatch is here rather than in each caller for the same reason the
    rest of this function is shared: all three surfaces offer the same field,
    and a mask that could only be moved onto a node from one of them would be a
    mask the other two silently deleted.
    """
    from plexora import nodes as node_api
    from plexora.server.utils import segmentation_pyramid

    located = _node_locator(mask_path)
    if located:
        return node_api.attach_segmentation(name, node=located[0],
                                            resource_id=located[1])

    project = Project.find(name)
    if project is not None and project.resource("segmentation") is not None:
        # Coming home from a node -- or being cleared while on one. The binding
        # goes first, or the project would keep reading the mask from a machine
        # its own record no longer names.
        node_api.detach(name, "segmentation", path=mask_path)

    dataset_dir = paths.derived_root(name)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    fields, pending = _segmentation_config_fields(
        Path(mask_path) if mask_path else None, dataset_dir,
        segmentation_async=bool(mask_path), segmentation_mode=mode,
    )

    def _apply(project):
        # The viewer expects the mask layer first in imageData -- shared with
        # the node path, which has to keep the same promise.
        return project.patch(
            image=replace(project.image, channels=tuple(_with_area_channel(
                name, project.image.channels, mask_path))),
            segmentation=_segmentation_spec(fields),
        )

    updated = Project.mutate(name, _apply)
    if updated is not None and pending:
        data_model.start_segmentation_job(
            name, pending, dataset_dir, fields["segmentationMode"]
        )
    return updated


def replace_project_data(name, data_path_str, payload=None, spatial=None):
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

    @param spatial - `{"pixel_size", "root"}` when this table came out of a
        spatial run, which is the only case where the file as shipped is not
        readable as it stands. See `server/utils/xenium_cells.py`.
    """
    payload = payload or {}
    project = Project.find(name)
    if project is None:
        raise ValueError(f"Unknown project: {name!r}")
    table = (payload.get("table") or "").strip() or None

    if not data_path_str:
        # Clearing the data file. The image and mask stay; every table-derived
        # fact goes, since none of it describes anything any more -- the
        # binding included, or the project would keep reading through a node
        # for a table it no longer has.
        return Project.mutate(
            name, lambda p: p.patch(dataset=None)
                             .with_resource("table", None)
                             .forget_table_answers())

    located = _node_locator(data_path_str)
    if located:
        # The table is on a data node. Delegated rather than reimplemented:
        # `nodes.attach_table` has the node run the same inspection this
        # function runs locally, and records the binding alongside the spec, so
        # a table swapped in here comes out shaped like one attached anywhere
        # else. `reinspect` because this field means "read that other file",
        # not "the same table moved" -- reusing a CSV's spec to read an .h5ad
        # is exactly the silent corruption this function exists to prevent.
        from plexora import nodes as node_api

        Project.mutate(name, lambda p: p.forget_table_answers())
        return node_api.attach_table(
            name, node=located[0], resource_id=located[1], table=table,
            subset_column=(payload.get("subset_column") or "").strip() or None,
            subset_value=(payload.get("subset_value") or "").strip() or None,
            reinspect=True)

    source = Path(data_path_str).expanduser()
    if not source.exists():
        raise ValueError(f"No such data file: {source}")
    data_type = detect_data_type(source)

    subset_column = (payload.get("subset_column") or "").strip() or None
    # The same relaxation `_register_anndata` makes, and for the same reason:
    # this is the route the edit page and the requirements modal post through,
    # so answering the table question later is re-posting this path WITH a
    # table. Recorded unresolved, the project keeps the path and stays
    # openable as an image.
    deferred = deferred_spec(source, data_type, table=table,
                             subset_by=subset_column)
    if deferred is not None:
        return Project.mutate(
            name, lambda p: p.forget_table_answers()
                             .with_resource("table", None)
                             .patch(dataset=deferred))

    if data_type == "spatialdata" and not table:
        table = list_spatialdata_tables(source)[0]["name"]

    flat = is_flat_table(data_type)
    derived = {}
    if flat:
        # Copied and corrected BEFORE inspection, not after. The header the
        # roles are predicted from has to be the header the adapter will
        # actually read -- a `cell_index` added afterwards is a column no role
        # can name, which is how the Xenium cell id stayed a string.
        source = _copy_into_project(name, source)
        if spatial:
            from plexora.server.utils import xenium_cells

            record = xenium_cells.normalise_cells_table(
                source, pixel_size=spatial.get("pixel_size"),
                root=spatial.get("root"))
            derived = dict(record) if record else {}

    inspection = _inspect(source, data_type, table)
    # Both branches produce markers/metadata/roles: inspect_flat_table
    # classifies the header directly, propose_read_spec folds
    # classify_from_inspection into the read spec. Read the classification off
    # `proposal` in both -- the raw AnnData inspection has var_names/obs_columns
    # and no such keys.
    proposal = (inspection if flat
                else data_inspection.propose_read_spec(inspection))

    if flat:
        spec_kwargs = {"coordinates": {}, "features": {}, "obs_id_field": None,
                       "obs_columns": (), "layers": ()}
    else:
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
            # The file's obsm arrays, so the coordinate question has something
            # to offer after the fact. Recorded from the one pass that already
            # knows -- `described_spec` does the same for the programmatic API,
            # and this path used to be the one that did not, which left the
            # edit page's coordinate picker with nothing but the detected key.
            "obsm": tuple(entry for entry in (inspection.get("obsm") or ())
                          if isinstance(entry, dict)),
        }

    known = set(proposal["markers"]) | set(proposal["metadata"])
    if not flat:
        # The adapter synthesizes these regardless of what the file calls them.
        known |= {"id", "X", "Y"}

    def _apply(current):
        kept = {role: column for role, column in current.roles.to_dict().items()
                if column in known}
        roles = ColumnRoles(**{**proposal["roles"], **kept})
        if not flat:
            roles = replace(roles, x="X", y="Y", cell_id="id")
        if derived.get("cell_index_from"):
            # The numeric id the normaliser added. Set rather than predicted:
            # the header still carries the vendor's string `cell_id`, and
            # every name-based heuristic picks that one -- which is the value
            # that cannot be packed into a centroid record.
            from plexora.server.utils.xenium_cells import INDEX_COLUMN

            roles = replace(roles, cell_id=INDEX_COLUMN)
        # Whatever the user confirmed about the old table described columns
        # that may not exist in this one, so those answers are dropped and the
        # fresh predictions go back in front of them. The mask and the cell
        # layer are unaffected -- neither is a fact about the table.
        current = current.forget_table_answers()
        # The file is on this machine now, so any node binding it had is a
        # stale instruction to read it somewhere else.
        current = current.with_resource("table", None)
        return current.patch(dataset=DataSpec(
            type=data_type,
            src=str(source),
            table=table,
            roles=roles,
            columns=ColumnGroups(markers=tuple(proposal["markers"]),
                                 metadata=tuple(proposal["metadata"])),
            derived=derived,
            **spec_kwargs,
        ))

    return Project.mutate(name, _apply)


def _copy_into_project(name, table_path):
    """A quantification table is small next to the image, and a project that
    keeps working after the user tidies their downloads folder is worth the
    disk. AnnData and SpatialData are referenced in place -- those are not
    small.

    A Xenium run's `cells.parquet` is a few megabytes beside five gigabytes of
    morphology, so the same rule holds for it: the copy is what the ROI
    plugin's region columns get written into, and writing those back into the
    vendor's own output directory is not something to do behind the user."""
    dataset_dir = paths.project_state_dir(name)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    local = dataset_dir / table_path.name
    if table_path.resolve() != local.resolve():
        shutil.copy2(table_path, local)
    _forget_upload(table_path)
    return local


# --------------------------------------------------------------------------
# A CSV handed over by the browser
#
# The one thing a browser CAN do that a path cannot: send the bytes. It is
# offered for a flat quantification table and for nothing else, and the reason
# is the line above -- a flat table is copied into the project directory
# anyway, so uploading one costs a copy that was always going to happen, and
# the result outlives the session that produced it. An .h5ad or a .zarr store
# is referenced in place and is routinely tens of gigabytes; uploading one
# would be moving the very data this whole design exists to leave where it is.
#
# It is also the ONLY way to name a local file when there is no data node on
# the user's machine -- a session started by hand over ssh, or through an Open
# OnDemand portal. Those sessions can still bring their cell table.
# --------------------------------------------------------------------------

#: What the upload accepts: the FLAT table formats, for the reason in the block
#: above -- those are the ones copied into the project anyway. Extensions
#: rather than sniffing, because this is a staging step: `detect_data_type`
#: reads the file afterwards and is the thing that actually decides what it is.
#: Kept in step with `dataLocation.js`, which greys the Upload option out for
#: anything else before the request is made.
UPLOAD_SUFFIXES = (".csv", ".tsv", ".txt", ".parquet")

#: A ceiling, not a target. A quantification table for a whole slide is tens of
#: megabytes; something a hundred times that is not a CSV somebody meant to
#: send through a browser, and refusing it early beats filling a scratch disk.
UPLOAD_MAX_BYTES = 512 * 1024 * 1024

#: How long a staged file survives if nothing imports it. Long enough that a
#: user who uploads and then goes to find their image still has it; short
#: enough that an abandoned import does not sit on the disk for a week.
UPLOAD_KEEP_SECONDS = 24 * 60 * 60


def _uploads_root():
    return paths.data_root() / "uploads"


@app.route('/upload_data_file', methods=['POST'])
def upload_data_file():
    """Stage a flat table the browser sent, and answer with a path on this
    machine.

    A path, deliberately: from here the file is an ordinary local file and
    every import route treats it as one, so nothing downstream learns that a
    browser was involved.
    """
    upload = request.files.get('file')
    if upload is None or not (upload.filename or '').strip():
        return jsonify(ok=False, error="No file was sent."), 400

    filename = Path(upload.filename).name
    if not filename.lower().endswith(UPLOAD_SUFFIXES):
        return jsonify(
            ok=False,
            error=f"Only {', '.join(UPLOAD_SUFFIXES)} can be sent from your "
                  f"computer this way. AnnData and SpatialData are read where "
                  f"they lie -- name a path on the server, or connect this "
                  f"computer as a data node.",
        ), 400

    _sweep_uploads()
    staged = _uploads_root() / uuid.uuid4().hex
    staged.mkdir(parents=True, exist_ok=True)
    target = staged / filename
    try:
        upload.save(target)
    except OSError as exc:
        shutil.rmtree(staged, ignore_errors=True)
        return jsonify(ok=False, error=f"Could not save the file: {exc}"), 500

    if target.stat().st_size > UPLOAD_MAX_BYTES:
        # Checked after the write rather than from Content-Length: a length
        # header is the client's claim about the body, and this is the only
        # number that is a fact.
        shutil.rmtree(staged, ignore_errors=True)
        return jsonify(
            ok=False,
            error=f"{filename} is larger than this server accepts through a "
                  f"browser. Put it somewhere the server can read, or connect "
                  f"this computer as a data node.",
        ), 400

    return jsonify(ok=True, path=str(target), name=filename)


def _forget_upload(path):
    """Drop a staged upload once it has been copied into a project.

    Only ever inside the uploads directory, and only the one staging folder --
    this runs on every flat-table import, including the overwhelming majority
    that came from a path the user typed and that Plexora has no business
    deleting.
    """
    try:
        staged = Path(path).resolve().parent
        if staged.parent == _uploads_root().resolve():
            shutil.rmtree(staged, ignore_errors=True)
    except OSError:
        pass


def _sweep_uploads():
    """Remove staged files nothing ever imported.

    An upload that is never imported -- the user changed their mind, or the
    import failed on the image path -- would otherwise sit there forever. Run
    on the way in rather than at startup: it is a directory listing, it is
    bounded by how many uploads are outstanding, and a server that is never
    restarted still tidies up.
    """
    root = _uploads_root()
    if not root.is_dir():
        return
    cutoff = time.time() - UPLOAD_KEEP_SECONDS
    try:
        for staged in root.iterdir():
            if staged.is_dir() and staged.stat().st_mtime < cutoff:
                shutil.rmtree(staged, ignore_errors=True)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Column classification -- marker versus metadata
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


@app.route('/detect_image_type', methods=['POST'])
def detect_image_type():
    """What the image at this path is, before anything has been imported.

    The form used to ask, with "Automatic" preselected. It does not any more:
    the answer is in the file for all but a handful of images, and a question
    whose answer is already known is a question that should not be on a form.
    So the same detector conversion runs is run here, and what comes back is
    shown beside the project name as a fact -- with a pencil, for the file that
    genuinely says nothing about itself.

    Metadata only. A whole-slide format states its layout structurally and
    DICOM states its acquisition mode in every instance header; the TIFF ladder
    reads headers and, at worst, one small plane. It never converts anything.

    Never an error the form has to handle: an unreadable or half-typed path is
    a null verdict, the select stays on "Automatic", and conversion detects
    again from scratch. Being unable to say so early costs nothing.
    """
    from plexora.server.providers import local as local_providers

    payload = request.get_json(silent=True) or {}

    # A node address in the field, handled before `_resolved` for the same
    # reason /inspect_data handles it there: `Path("node://hpc/slide")` is a
    # perfectly valid relative path that exists nowhere, so the check below
    # would report "nothing here" about a file the node can read perfectly
    # well -- and the line beside the project name would stay blank for every
    # image on another machine.
    try:
        located = _node_locator(payload.get('path'))
    except ValueError:
        return jsonify(verdict=None)
    if located:
        return _detect_on_node(located)

    path = _resolved(payload.get('path'))
    if not path or not path.exists():
        return jsonify(verdict=None)
    try:
        found = local_providers.detect_image_type(path)
    except Exception:
        return jsonify(verdict=None)
    return jsonify(verdict=found.verdict, confidence=found.confidence,
                   reason=found.reason)


def _detect_on_node(located):
    """`/detect_image_type`, when the image lives on a data node.

    The node ran the same detector when the resource was added and reports the
    answer in its handshake, so this is a lookup rather than a second
    implementation -- and the form says the same thing about a slide whichever
    machine it is on. A node that is unreachable, or too old to say, is a null
    verdict like any other: the same silence a half-typed path produces, and
    the import records whatever the node reports at attach time regardless.
    """
    from plexora import nodes as node_api

    node, resource_id = located
    try:
        verdict, reason = node_api.image_type_on_node(node, resource_id)
    except Exception:
        return jsonify(verdict=None)
    return jsonify(verdict=verdict, confidence=None, reason=reason)


@app.route('/dataset_existence', methods=['POST'])
def check_dataset_exists():
    payload = request.get_json(silent=True) or {}
    return jsonify(exists=(payload.get('datasetName') in get_config()))


@app.route('/supported_data_formats')
def supported_data_formats():
    """So the form's help text and the server's rejection message cannot
    disagree about what the Data input takes."""
    return jsonify(description=SUPPORTED_DATA_DESCRIPTION)
