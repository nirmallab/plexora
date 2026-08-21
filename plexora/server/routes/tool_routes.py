"""Opening a plugin's tool, and collecting whatever it still needs.

A plugin declares what it needs (plexora/api/plugin.py's Requires). Core works
out what the project already has, asks only for the difference, and stores the
answers on the project -- so the next plugin to want the same thing never asks
again. That is the whole mechanism: core never names a concrete plugin, and a
plugin never grows its own "please type a column name" box.

Opening a tool has three outcomes:

OPEN     -- everything it needs is there.
COLLECT  -- installed and compatible, but something is missing. Recoverable, so
            the client is told what to ask for and re-enters here once it has
            the answers. This is why the Tools menu lists compatible-but-not-
            ready plugins at all: hiding them hides the only route to making
            them work.
OFFER    -- nothing is blocking, but there is something to offer once. A plugin
            whose inputs are ALL optional would otherwise never reach the modal
            at all: `optional_missing_from` exists and is documented as "offered
            once", and without this outcome nothing ever consults it.
FALLBACK -- unknown datasource, uninstalled tool, or permanently incompatible.
            Stale and bookmarked links land here, so it must not error.
"""

from flask import jsonify, redirect, render_template, request

from plexora import app
from plexora.server import plugins as plugin_registry
from plexora.server.models import data_model
from plexora.server.models.project import ROLE_LABELS, ROLE_NAMES, Project
from plexora.server.routes.page_routes import template_data

OPEN, COLLECT, OFFER, FALLBACK = "open", "collect", "offer", "fallback"


def _resolve(datasource, tool_name):
    """(outcome, plugin, project) for opening `tool_name` on `datasource`.

    Whether a tool applies is the plugin's own declaration, not a rule core
    hardcodes -- core used to test `image_kind == 'rgb'` directly, which only
    ever encoded what gating in particular could not handle.
    """
    project = Project.find(datasource)
    plugin = plugin_registry.find(app, tool_name)
    if project is None or plugin is None or not plugin.requires.applies_to(project):
        return FALLBACK, None, project
    # Two reasons to collect, not one. Something absent is the obvious case; a
    # value the column predictor guessed and nobody ever looked at is the other,
    # and it is the more common one -- a conventionally-named table leaves
    # nothing missing at all, so without this the first launch would silently
    # run on five guesses.
    if plugin.requires.missing_from(project) or plugin.requires.unconfirmed_from(project):
        return COLLECT, plugin, project
    # Nothing blocks, but something has never been put in front of the user.
    # Kept as its own outcome rather than folded into COLLECT because the two
    # callers below want opposite things from it: the panel fetch shows the
    # form, the plain <a href> opens the tool. A plugin that requires nothing
    # at all -- ROI -- reaches the modal only through here.
    if plugin.requires.optional_missing_from(project):
        return OFFER, plugin, project
    return OPEN, plugin, project


def _needs(plugin, project):
    """What the client renders a form from.

    Everything here is generic: a list of typed requirements plus the material
    needed to answer them (the project's columns, its current roles). The
    modal builds inputs from `kind` without knowing which plugin asked.

    Three lists, because the user is in three different positions:

    `missing`  -- nothing is stored; the field starts empty.
    `confirm`  -- something is stored but was guessed; the field is prefilled
                  and the user is being shown it once.
    `optional` -- absent and not blocking; offered while the form is open.

    A requirement the user has already answered appears in none of them. That
    is the property the whole design turns on, and it is why an answer is
    recorded on the project rather than by the plugin that asked for it.
    """
    from plexora.server.routes.project_routes import known_layers, known_obsm

    # Filled in from the file for a project imported before layers or obsm were
    # recorded, which is precisely the project whose matrix and coordinate
    # choices are wrong and cannot be corrected. Deferred import:
    # project_routes imports this module.
    project = known_obsm(known_layers(project))
    return {
        "tool": plugin.name,
        "label": plugin.label,
        # Why anyone would fill in a form that blocks nothing. Core's own
        # wording covers the blocking case for every plugin; this one cannot be
        # written generically, so the plugin supplies it (Plugin.intro).
        "intro": plugin.intro,
        "missing": [r.describe() for r in plugin.requires.missing_from(project)],
        "confirm": [r.describe() for r in plugin.requires.unconfirmed_from(project)],
        "optional": [r.describe() for r in plugin.requires.optional_missing_from(project)],
        "columns": {
            "markers": list(project.columns.markers),
            "metadata": list(project.columns.metadata),
        },
        # What a role select is made of: the columns to offer, the answer to
        # preselect, and what a blank one falls back to. All three come from the
        # project rather than being assembled client-side, because for AnnData
        # and SpatialData the question is asked about the source file's obs
        # columns and answered into the read spec -- a translation the modal has
        # no business knowing about (see Project.role_columns).
        "roleColumns": project.role_columns,
        "roleAnswers": project.role_answers,
        "roleDefaults": project.role_defaults,
        # What the coordinate field is made of: every obsm array the file
        # carries (with its shape), every obs column, and the source recorded
        # now. Assembled here rather than client-side for the same reason the
        # role keys are -- the answer is a read spec, not a column name.
        "coordinateOptions": project.coordinate_options,
        # Whether the user has said this table covers a single image, which is
        # an answer to the image-id question and not an absence of one.
        "singleImage": bool(project.dataset and project.dataset.single_image),
        # The same thing for the cell id: whether the user has said to number
        # the rows, which is an answer and not a blank left alone.
        "rowNumberIds": bool(project.dataset and project.dataset.row_number_ids),
        # What the "Expression values" field is made of: every matrix the file
        # carries, which one is read now, and whether it is log1p'd on the way
        # in. Same three keys the edit page renders from.
        "featureOptions": project.feature_options,
        "featureSource": project.feature_source,
        "featureLog": project.log_transformed,
        # The whole role vocabulary, so every surface words a role the same way
        # and core owns the wording rather than each of them. The modal reads a
        # label off the requirement itself and does not need this today; it is
        # kept because a client asking what this project's roles are called
        # should not have to know which of them happened to be asked for.
        "roleLabels": dict(ROLE_LABELS),
        "hasTable": project.has_table,
        "segmentationPending": project.segmentation.pending,
    }


@app.route('/<string:datasource>/tools/<string:tool_name>')
def open_tool(datasource, tool_name):
    """The plain <a href> path, for a client that did not intercept the click.

    Hands off to the edit page rather than the upload page: the edit page is
    generated from the same requirements, so there is one surface to maintain
    and the user lands on the specific fields this tool is missing.

    OFFER is deliberately not COLLECT here. Nothing is blocking, so sending the
    user to the edit page would be a detour to answer a question they are
    entitled to ignore -- and this is the path taken when the client could not
    intercept the click, which is exactly when a detour is hardest to undo.
    """
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    outcome, _, _ = _resolve(datasource, tool_name)

    if outcome == COLLECT:
        return redirect(f"{base_url}/edit_config/{datasource}?needs={tool_name}")
    if outcome == FALLBACK:
        return redirect(f"{base_url}/{datasource}")
    return redirect(f"{base_url}/{datasource}?tool={tool_name}")


@app.route('/<string:datasource>/tools/<string:tool_name>/panel')
def tool_panel(datasource, tool_name):
    """Fetched by toolLoader.js the first time a tool is opened mid-session
    (plain viewer already loaded, no navigation) -- mirrors open_tool()'s
    checks above, but returns JSON for client-side injection instead of a
    redirect, so the viewer/OpenSeadragon instance already on the page is never
    torn down.

    When something is missing this returns `needs` rather than a redirect. That
    is the point of the modal: navigating away to collect a column name would
    rebuild the whole viewer to answer one question.

    OFFER is served the same way. The form it produces has no empty blocking
    field and its Cancel is a real answer, so a user who wants none of it is one
    click from the panel -- which is what makes offering it acceptable at all.
    """
    base_url = app.config.get('PLEXORA_BASE_URL', '')
    outcome, plugin, project = _resolve(datasource, tool_name)

    if outcome in (COLLECT, OFFER):
        return jsonify({"needs": _needs(plugin, project)})
    if outcome == FALLBACK:
        return jsonify({"redirect": f"{base_url}/{datasource}"}), 400

    data = template_data(datasource=datasource, active_tool=tool_name)
    fragments = {
        slot: render_template(template_path, data=data)
        for slot, template_path in plugin.panels.items()
    }
    return jsonify({
        "fragments": fragments,
        "scripts": plugin.asset_urls("scripts", base_url),
        "styles": plugin.asset_urls("styles", base_url),
    })


@app.route('/<string:datasource>/tools/<string:tool_name>/requirements')
def tool_requirements(datasource, tool_name):
    """What a tool still needs, without opening it.

    Lets a plugin ask for something mid-session -- gating wants an image-id
    column only when the user chooses to write gates back to the source file,
    long after the panel opened.

    `?keys=a,b` adds a `requested` list holding exactly those inputs, if the
    project still cannot answer them. That is a different question from the
    lists above: those are what the user has not been asked yet, this is what a
    specific action needs right now, and an optional field they were offered
    and skipped shows up only in the second.
    """
    outcome, plugin, project = _resolve(datasource, tool_name)
    if outcome == FALLBACK:
        return jsonify(success=False, error="Unknown tool or datasource"), 404

    payload = _needs(plugin, project)
    keys = [key for key in (request.args.get("keys") or "").split(",") if key]
    if keys:
        payload["requested"] = [
            r.describe() for r in plugin.requires.requested_from(project, keys)
        ]
    return jsonify(success=True, **payload)


@app.route('/<string:datasource>/requirements', methods=['POST'])
def satisfy_requirements(datasource):
    """Record answers to whatever was missing.

    Everything reusable lands on the project, not in the asking plugin's own
    storage: which column holds the cell id is a fact about the dataset, and a
    second plugin needing it must find it already answered. Plugin-private
    state goes through plexora.api.store instead, which is namespaced per
    plugin precisely because it is nobody else's business.

    Accepts any subset -- the modal posts once with whatever the user filled
    in, and the reply says what is still outstanding.
    """
    from plexora.server.routes import import_routes

    project = Project.find(datasource)
    if project is None:
        return jsonify(success=False, error="Unknown datasource"), 404

    payload = request.get_json(silent=True) or {}
    reload_needed = False

    try:
        if payload.get("data"):
            import_routes.replace_project_data(datasource, payload["data"], payload)
            reload_needed = True
        if payload.get("segmentation"):
            import_routes.attach_segmentation(datasource, payload["segmentation"])
        if not reload_needed:
            # Skipped when the data file itself just changed:
            # replace_project_data already read this answer out of the same
            # payload, and re-applying it would check the choice against the
            # file that has just been replaced.
            from plexora.server.routes.project_routes import apply_feature_choice

            reload_needed = apply_feature_choice(
                Project.find(datasource) or project, payload)
        answers = ("roles", "columns", "confirm", "coordinates", "single_image",
                   "row_number_ids")
        if any(payload.get(key) for key in answers):
            Project.mutate(datasource, lambda p: _apply(p, payload))
            # `single_image` is absent here on purpose: it records what the
            # table already was, so nothing about how it is read has changed.
            # `coordinates` very much has -- it repoints the adapter at a
            # different array -- and so does `row_number_ids`, which clears the
            # obs column the identifier was being read from.
            reload_needed = reload_needed or bool(
                payload.get("roles") or payload.get("columns")
                or payload.get("coordinates") or payload.get("row_number_ids"))
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    if reload_needed:
        error = _reload_or_restore(datasource, project)
        if error:
            return jsonify(success=False, error=error), 400

    project = Project.find(datasource)
    tool_name = payload.get("tool")
    plugin = plugin_registry.find(app, tool_name) if tool_name else None
    # Everything the tool would still stop for, so the modal knows whether to
    # close or re-render. Unconfirmed items count: naming a data file makes a
    # whole set of column questions askable that were not askable before, and
    # those arrive unconfirmed.
    still_missing = (
        [r.describe() for r in plugin.requires.missing_from(project)]
        + [r.describe() for r in plugin.requires.unconfirmed_from(project)]
        if plugin else []
    )
    # What is newly offerable, which is not the same list and does not belong in
    # it. Naming a data file makes "which column holds the cell id" ASKABLE for
    # the first time, and for a plugin that only ever offers that question it
    # arrives here rather than in `stillMissing` -- so a modal that closed on
    # `stillMissing` alone would shut the moment the file was attached, one
    # question short. Nothing blocks on these: they keep the form open, they do
    # not keep the tool shut.
    still_optional = (
        [r.describe() for r in plugin.requires.optional_missing_from(project)]
        if plugin else []
    )
    return jsonify(
        success=True,
        stillMissing=still_missing,
        stillOptional=still_optional,
        # Whether the datasource was re-read. The client holds a snapshot of the
        # table's per-column statistics taken at page load, and a tool is drawn
        # from that snapshot -- so an answer that changes which numbers are read
        # (a different matrix, the log transform, a swapped file) leaves the
        # panel showing the old ones unless the client is told to re-fetch.
        reloaded=reload_needed,
        segmentationPending=project.segmentation.pending,
    )


def _reload_or_restore(datasource, previous):
    """Reload the datasource, putting `previous` back if it will not load.

    An answer is only really accepted once the file can be read with it. A role
    can rewrite the read spec, and some answers are ones the adapter refuses --
    an obs column whose name collides with one it synthesizes, most obviously --
    and it only says so here. Storing such an answer and leaving it stored gives
    the user a project that no longer opens at all, which is a far worse outcome
    than the question they were trying to answer.

    Scoped to ValueError, which is how an adapter says "I cannot read the file
    this way" and is already what these routes treat as the user's to fix.
    Anything else is a bug rather than a rejected answer, and swallowing it here
    would turn it into a silent no-op save.

    Returns an error message, or None when the reload was fine.
    """
    try:
        data_model.load_datasource(datasource, reload=True)
        return None
    except ValueError as exc:
        previous.save()
        try:
            data_model.load_datasource(datasource, reload=True)
        except ValueError:
            # The project was already unloadable before this request. Nothing
            # to restore it to, and the message below is still the useful half.
            pass
        return str(exc)


def _apply(project, payload):
    columns = payload.get("columns") or {}
    if columns:
        project = project.with_columns(columns.get("markers") or [],
                                       columns.get("metadata") or [])
    project = apply_column_answers(project, payload)
    # Confirmed last, from two sources. `confirm` is the list of keys the form
    # actually showed: an optional field left blank still counts, because the
    # user was asked and declined, and re-asking on every open is what this
    # list exists to prevent. `_supplied_keys` covers whatever arrived in the
    # payload regardless, so a caller that answers without saying which
    # requirement it was answering does not leave the question open.
    return project.with_confirmed(
        list(payload.get("confirm") or ()) + _supplied_keys(payload))


def apply_column_answers(project, payload):
    """Record the answers a form gives about a project's columns.

    Shared by the requirements modal and the project edit page so the two
    cannot drift: both post `roles`, `coordinates`, `single_image` and
    `row_number_ids`, and both have to mean the same thing by them.

    Order matters. The two flags are applied after the roles because each is an
    alternative answer to a question a role also answers -- naming an image-id
    column retracts a previous "only one image", naming an id column retracts
    "number the rows" (see Project.with_role_answers) -- so a payload carrying
    both means the user has just switched to the flag.
    """
    project = project.with_role_answers(payload.get("roles") or {})
    coordinates = payload.get("coordinates")
    if coordinates:
        project = project.with_coordinates(coordinates)
    if payload.get("single_image"):
        project = project.with_single_image(True)
    if payload.get("row_number_ids"):
        project = project.with_row_number_ids(True)
    return project


def _supplied_keys(payload):
    """The requirement keys this payload carries an answer for."""
    keys = []
    if payload.get("data"):
        keys.append("table")
    if payload.get("segmentation"):
        keys.append("segmentation")
    if payload.get("columns"):
        keys.append("markers")
    if payload.get("features_layer") or "features_log" in payload:
        keys.append("features")
    # Filtered against ROLE_NAMES, the same gate with_role_answers applies to
    # the answers themselves. Without it a payload naming a role core cannot
    # store still marked a key confirmed -- so the answer was dropped and the
    # question was never asked again, which is exactly how a project ended up
    # keeping a cell id nobody chose.
    keys.extend(f"role:{role}" for role, column in (payload.get("roles") or {}).items()
                if column and role in ROLE_NAMES)
    if payload.get("coordinates"):
        keys.append("coordinates")
    if payload.get("single_image"):
        # The other answer to the image-id question, and the one that names no
        # column -- so without this the question would be asked again forever.
        keys.append("role:image_id")
    if payload.get("row_number_ids"):
        # Likewise for the cell id.
        keys.append("role:cell_id")
    return keys
