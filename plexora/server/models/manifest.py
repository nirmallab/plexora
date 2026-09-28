"""Whether one project has answered each question a plugin can ask about it.

Every surface in Plexora -- a plugin's `Requires`, the edit page, the Open
Project card's "Needs setup" badge, the CLI, the Python API -- asks some form
of "does this project have X yet". This module is the one place that answers
it, so every reader agrees on what "has a table" means.

This module tracks three states, not two:

- `PRESENT` -- the user answered this, and the answer is recorded.
- `GUESSED` -- there is a value, but Plexora worked it out. A guess that
  happens to be right is still a guess, so the first tool that depends on
  one shows it back once, for confirmation.
- `MISSING` -- nothing, and nothing to show back.

Plus `NOT_APPLICABLE`, for a question this project's format cannot be asked
at all: an AnnData's x/y do not exist as columns, because the adapter builds
them from a read spec, so offering two column selects for them is a form
with no possible answer in it. That is a different thing from missing.
"""

# Four readers used to ask this themselves, independently, and "has a table"
# meant something slightly different in the edit page, the Open Project card
# and the CLI. This module is the merge: `api/plugin.py`'s own `_answered`
# just calls `answered()` now, and the requirements tests are the net that
# says the move changed nothing.

from __future__ import annotations

from plexora.server.models.project import ROLE_LABELS, ROLE_NAMES, Project

#: The user answered this question, and the answer is recorded.
PRESENT = "present"
#: There is a value, but Plexora worked it out rather than being told. A
#: guess that happens to be right is still a guess.
GUESSED = "guessed"
#: Nothing is recorded, and there is nothing to show back.
MISSING = "missing"
#: This project's format cannot be asked this question at all -- a different
#: thing from missing, and not a gap for the user to fill.
NOT_APPLICABLE = "not_applicable"

#: The coordinate question, which stands in for the x/y role pair wherever the
#: table is built from a read spec. Same constant `api/plugin.py` exports.
COORDINATES_KEY = "coordinates"

#: Every question core can record an answer to, in the order a form should ask
#: them: the files first, then what is inside them, then what the columns mean.
KEYS = (
    "image",
    "segmentation",
    "table",
    "features",
    "markers",
    COORDINATES_KEY,
) + tuple(f"role:{role}" for role in ROLE_NAMES)

#: Human labels, so every surface words a question identically. Roles come from
#: the project record's own vocabulary rather than being restated.
LABELS = {
    "image": "Image",
    "segmentation": "Segmentation mask",
    "table": "Single-cell data",
    "features": "Expression values",
    "markers": "Marker and metadata columns",
    COORDINATES_KEY: "Cell coordinates",
    **{f"role:{role}": label for role, label in ROLE_LABELS.items()},
}

#: Inputs that are a path the user typed or browsed to, never something the app
#: worked out. There is nothing to confirm about them -- showing a file path
#: back and asking "is this the file you chose?" is noise -- so they are
#: PRESENT the moment they exist, never GUESSED.
GIVEN_KEYS = frozenset({"image", "table", "segmentation"})


def never_confirmed(project: Project, key: str) -> bool:
    """Whether this input is one the user is never shown for confirmation.

    Either because they supplied it themselves (`GIVEN_KEYS`), or because the
    answer is not a guess in the first place: an AnnData or SpatialData file
    states its own marker/metadata split, and putting `var` and `obs` in a
    drag-and-drop box asks the user to confirm what the file already says.
    """
    if key in GIVEN_KEYS:
        return True
    if key == "markers":
        return project.columns_are_structural
    return False


def status(project: Project, key: str) -> dict:
    """The state of one question about a project.

    `value` is what a surface shows back -- a path, a column name, a list --
    or None. For `"table"` on a source that cannot yet be read it is the
    dict `{src, table, unresolved}`, which is what the requirements modal
    prefills its data field from: the user already gave the path, so asking
    for it again is asking a question they already answered.

    Args:
        project (Project): The project to check, e.g. `dataset.project` from
            a `ProjectData` handle.
        key (str): Which question to ask. One of `KEYS`.

    Returns:
        dict: `{"status": ..., "value": ..., "confirmed": ...}`. `status` is
        one of `PRESENT`, `GUESSED`, `MISSING` or `NOT_APPLICABLE`.

    Raises:
        KeyError: If `key` is not one of `KEYS`.
    """
    project = _as_project(project)
    if key not in KEYS:
        raise KeyError(f"Unknown manifest key: {key!r} (expected one of {list(KEYS)})")
    state, value = _state(project, key)
    if state in (MISSING, NOT_APPLICABLE):
        return {"status": state, "value": value, "confirmed": False}
    confirmed = key in project.confirmed or never_confirmed(project, key)
    return {"status": PRESENT if confirmed else GUESSED,
            "value": value, "confirmed": bool(confirmed)}


# The one truth: every `has.*` boolean the edit page renders calls this too.
def answered(project: Project, key: str) -> bool:
    """Whether the project holds a value for this question, guessed or given.

    Says nothing about who supplied the value -- a predictor's guess counts
    as answered here the same as something the user typed in. `status()` is
    what tells the two apart.

    Args:
        project (Project): The project to check.
        key (str): Which question to ask. One of `KEYS`.

    Returns:
        bool: True if `status(project, key)` is `PRESENT` or `GUESSED`.

    Raises:
        KeyError: If `key` is not one of `KEYS`.
    """
    return status(project, key)["status"] in (PRESENT, GUESSED)


def manifest(project: Project) -> dict:
    """Every question, keyed, in ask order."""
    project = _as_project(project)
    return {key: status(project, key) for key in KEYS}


def summary(project: Project) -> dict:
    """The short form a project card or a CLI listing is drawn from.

    Deliberately not the whole manifest: a page listing two hundred projects
    wants a handful of facts about each, not the full set of questions.

    Args:
        project (Project): The project to summarize.

    Returns:
        dict: The image kind, the segmentation and table status, the
        dataset type, what is still unresolved, whether the project needs
        setup, and its layer count and modalities.
    """
    project = _as_project(project)
    data = project.dataset
    return {
        "imageKind": project.image.kind,
        "segmentation": ("present" if project.segmentation.available
                         else "pending" if project.segmentation.requested
                         else "missing"),
        "table": ("present" if project.has_table
                  else "unresolved" if project.has_data_source
                  else "missing"),
        "tableType": data.type if data else None,
        "unresolved": list(project.unresolved),
        "needsSetup": needs_setup(project),
        # What is in this sample beyond the image, for the card's badges:
        # "Xenium - 4 layers". Two numbers and a short list rather than the
        # layer records, because this is drawn two hundred times on one page.
        "layers": {
            "count": len(project.spatial_layers),
            "modalities": sorted({layer.modality for layer in project.all_layers
                                  if layer.modality}),
        },
        # Which data nodes hold any of this sample -- the image of an IDC slide
        # on a Google Cloud VM, say. Named, because it is what the card has to
        # say: this one opens only while that machine is connected.
        "remote": _remote_nodes(project),
    }


def _remote_nodes(project: Project) -> list[str]:
    """The data nodes any of this project's resources or layers are served by."""
    project = _as_project(project)
    names = {binding.node for binding in (project.resources or {}).values()
             if getattr(binding, "node", None)}
    for layer in project.all_layers:
        src = str(getattr(layer, "src", "") or "")
        if src.startswith("node://"):
            node = src[len("node://"):].split("/", 1)[0]
            if node:
                names.add(node)
    return sorted(names)


def needs_setup(project: Project) -> bool:
    """Whether this project is holding a question it cannot answer itself.

    Only the one state deserves a badge on a card: a data file was named and
    something about it is still undecided, so the project silently opens as
    an image and the user has no other way to find out why. An image-only
    project is never flagged -- that is a complete, valid project, and the
    whole premise of "an image alone is enough".

    Args:
        project (Project): The project to check.

    Returns:
        bool: True if the project has anything left unresolved.
    """
    return bool(_as_project(project).unresolved)


def open_questions(project: Project, keys=None) -> list[str]:
    """Which questions this project still cannot answer, in ask order.

    A role question (`"role:cell_id"`, and the rest) is withheld while the
    table itself is unanswered: asking which column holds the cell id before
    any columns exist is a question with no possible answer, and `"table"`
    already covers it.

    Args:
        project (Project): The project to check.
        keys (list[str], optional): Which questions to consider. Defaults to
            every key in `KEYS`.

    Returns:
        list[str]: The keys from `keys` this project has not answered, in
        `KEYS` order.
    """
    project = _as_project(project)
    wanted = list(keys) if keys is not None else list(KEYS)
    out = []
    for key in KEYS:
        if key not in wanted:
            continue
        if _is_table_scoped(key) and not project.has_table:
            # `table` itself still reports, so the user is asked for the file
            # rather than for what is in it.
            continue
        if answered(project, key):
            continue
        out.append(key)
    return out


def _is_table_scoped(key: str) -> bool:
    return key != "table" and (key.startswith("role:") or key in
                               ("markers", "features", COORDINATES_KEY))


# -- the rules ------------------------------------------------------------


def layers(project: Project) -> list:
    """Every layer of one sample, flattened for a UI to render.

    Not part of `KEYS`, and deliberately: `KEYS` is the requirement vocabulary
    -- things a tool can ask the user to supply -- and it is table-shaped
    because that is what those questions are about. A layer is not asked for by
    naming a column; it is imported. So this sits beside the requirement
    machinery rather than inside it, and nothing that reads `KEYS` changes.
    """
    return [
        {
            "id": layer.id,
            "kind": layer.kind,
            "modality": layer.modality,
            "label": layer.label or layer.id,
            "status": layer.status,
            "unresolved": list(layer.unresolved),
            "source": dict(layer.source) if layer.source else None,
            "src": layer.src,
            "transformSource": layer.transform_source,
        }
        for layer in project.all_layers
    ]


def _state(project: Project, key: str) -> tuple:
    """(status, value) before `confirmed` is consulted."""
    if key == "image":
        # PRESENT for every project that exists, including one whose reference
        # frame is blank. That is not a fudge: a blank frame IS the sample's
        # coordinate system, every layer is registered against it, and the
        # things that read this -- the requirements modal, a plugin's
        # `missing_from` -- are asking "is there somewhere to draw", which
        # there is. The `value` is None there, because there is no file, and
        # nothing that shows it treats a path as required.
        #
        # It is in the manifest at all because a plugin declaring what it needs
        # should be able to name the image without core treating that as an
        # error, and because a summary that omits the one universal resource
        # reads as though it were optional.
        return (PRESENT, project.image.src or None)

    if key == "segmentation":
        if not project.segmentation.requested:
            return (MISSING, None)
        return (PRESENT, project.segmentation.source or project.segmentation.derived)

    if key == "table":
        if project.has_table:
            return (PRESENT, {"src": project.dataset.src,
                              "table": project.dataset.table,
                              "type": project.dataset.type,
                              "unresolved": []})
        if project.has_data_source:
            # A path the user gave that cannot be read yet. MISSING, because
            # nothing can use it -- but with the path in `value`, so whoever
            # asks shows it back rather than an empty box.
            return (MISSING, {"src": project.dataset.src,
                              "table": project.dataset.table,
                              "type": project.dataset.type,
                              "unresolved": list(project.dataset.unresolved)})
        return (MISSING, None)

    if key == "features":
        # Never absent: a table is always being read from some matrix, so this
        # is only ever a value nobody has looked at rather than a gap. It
        # reaches the user through `unconfirmed_from`, never `missing_from`.
        if not project.has_table:
            return (MISSING, None)
        return (PRESENT, project.feature_source)

    if key == "markers":
        if not project.has_table:
            return (MISSING, None)
        if not project.columns.classified:
            return (MISSING, None)
        return (PRESENT, {"markers": list(project.columns.markers),
                          "metadata": list(project.columns.metadata)})

    if key == COORDINATES_KEY:
        if not project.has_table:
            return (MISSING, None)
        if not project.columns_are_structural:
            # A CSV answers x and y as two ordinary column roles; there is no
            # separate coordinate question to ask.
            return (NOT_APPLICABLE, None)
        # The recorded read spec, not the roles: `roles.x`/`roles.y` are the
        # literal "X"/"Y" the adapter emits and are set the moment a table
        # exists, so they say nothing about whether anyone chose a source.
        coordinates = dict(project.dataset.coordinates or {})
        return (PRESENT, coordinates) if coordinates else (MISSING, None)

    if key.startswith("role:"):
        return _role_state(project, key.split(":", 1)[1])

    raise KeyError(f"Unknown manifest key: {key!r}")


def _role_state(project: Project, role: str) -> tuple:
    if not project.has_table:
        return (MISSING, None)

    if role in ("x", "y") and project.columns_are_structural:
        # For these formats the two roles collapse into the one coordinate
        # question -- the adapter builds X and Y from a read spec, and an obsm
        # array supplies both axes at once, which no pair of column selects can
        # express. Not missing: unanswerable as put.
        return (NOT_APPLICABLE, None)

    if role == "cell_id" and project.columns_are_structural:
        # The role is not the answer for these formats. It names a column of
        # the table the adapter EMITS, and the importer sets it to the
        # adapter's own positional "id" the moment a table loads -- so reading
        # it here would report every AnnData and SpatialData project as having
        # answered a question nobody was asked, which is exactly what left a
        # project drawing gates against row numbers while its mask carried the
        # label values from obs.
        #
        # The read spec is the answer: a named obs column, or the explicit
        # "number the rows" that names none. See DataSpec.row_number_ids.
        if project.dataset.obs_id_field:
            return (PRESENT, project.dataset.obs_id_field)
        if project.dataset.row_number_ids:
            return (PRESENT, "Row number")
        return (MISSING, None)

    if role == "image_id":
        # "This table covers one image" is an answer, and the only one some
        # files have -- so it counts here, while a bare absent role does not.
        # See DataSpec.single_image for why it is not stored as a blank role.
        if project.roles.image_id:
            return (PRESENT, project.roles.image_id)
        if project.dataset.single_image:
            return (PRESENT, "Single image")
        return (MISSING, None)

    column = project.roles.get(role)
    return (PRESENT, column) if column else (MISSING, None)


def _as_project(project) -> Project:
    """Accept a Project or a raw config entry, as `api/plugin.py` does."""
    if isinstance(project, Project):
        return project
    return Project.from_entry("", project or {})
