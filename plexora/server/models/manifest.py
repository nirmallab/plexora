"""What one project has, what it has been told, and what is still open.

Every surface in Plexora asks the same question in a different way. A plugin's
`Requires` asks it through `_answered`. The edit page asks it through
`_describe()["has"]`. The Open Project card wants to know whether to draw a
"Needs setup" badge. The CLI wants to print a table. The Python API wants to
return a dict. Four readers, three of which were written independently, and
"has a table" meant something slightly different in each.

This module is the one reader. Everything else delegates to it, so adding a
question -- or changing what counts as an answer to one -- is a change here
rather than a search across the tree.

**Three states, not two.** The distinction the old boolean could not carry:

  PRESENT   the user answered this, and the answer is recorded.
  GUESSED   there is a value, but Plexora worked it out. A guess that happens
            to be right is still a guess, so the first tool that depends on one
            shows it back once, prefilled.
  MISSING   nothing, and nothing to show back.

plus NOT_APPLICABLE for a question this project's format cannot be asked: an
AnnData's x/y do not exist as columns, because the adapter builds them from a
read spec, and offering two column selects for them is a form with no possible
answer in it. That is a different thing from missing, and treating it as
missing is what made the modal ask unanswerable questions.

**The rules here are the ones `api/plugin.py::_answered` already applied**,
moved rather than rewritten: a structural format answers `role:cell_id`
through the read spec and not the role, `role:image_id` counts "one image" as
an answer, `features` is never absent because a table is always read from some
matrix. `plugin.py` now calls `answered()` and the requirements tests are the
net that says the move changed nothing.
"""

from __future__ import annotations

from plexora.server.models.project import ROLE_LABELS, ROLE_NAMES, Project

PRESENT = "present"
GUESSED = "guessed"
MISSING = "missing"
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
    """The state of one question: `{status, value, confirmed}`.

    `value` is what the surface shows back -- a path, a column name, a list --
    or None. For `table` on an unresolved source it is the dict
    `{src, table, unresolved}`, which is what the requirements modal prefills
    its data field from: the user already gave the path, so asking for it again
    is asking a question they answered.
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


def answered(project: Project, key: str) -> bool:
    """Whether the project holds a value for this input, guessed or given.

    **The one truth.** `api/plugin.py::_answered` is this function, and so is
    every `has.*` boolean the edit page renders. Says nothing about who
    supplied the value -- that is what `status()` is for, and why
    `unconfirmed_from` needs both this and the `confirmed` list.
    """
    return status(project, key)["status"] in (PRESENT, GUESSED)


def manifest(project: Project) -> dict:
    """Every question, keyed, in ask order."""
    project = _as_project(project)
    return {key: status(project, key) for key in KEYS}


def summary(project: Project) -> dict:
    """The short form a project card and a CLI listing are drawn from.

    Deliberately not the whole manifest: a page listing two hundred projects
    wants five facts about each, not sixty, and "what kind of image is this"
    is not a question anybody answers -- it is a fact about the file.
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
    }


def needs_setup(project: Project) -> bool:
    """Whether this project is holding a question it cannot answer itself.

    Only the one state deserves a badge on a card: a data file was named and
    something about it is still undecided, so the project silently opens as an
    image and the user has no other way to find out why. An image-only project
    is NOT flagged -- that is a complete, valid project and the whole premise
    of "an image alone is enough".
    """
    return bool(_as_project(project).unresolved)


def open_questions(project: Project, keys=None) -> list[str]:
    """Which of `keys` this project still cannot answer, in ask order.

    Role questions are withheld while the table is unanswered, the same rule
    `Requires.missing_from` applies: asking which column holds the cell id
    before any columns exist is a question with no answers in it, and the table
    question already covers it.
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


def _state(project: Project, key: str) -> tuple:
    """(status, value) before `confirmed` is consulted."""
    if key == "image":
        # The floor of the contract: a project without one cannot be
        # registered, so this is PRESENT for every project that exists. It is
        # in the manifest anyway, because a plugin declaring what it needs
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
