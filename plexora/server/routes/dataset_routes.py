"""Datasets over HTTP: make a folder, rename it, put projects in it.

Five routes and one verb that matters. `POST /projects/assign` is what
drag-and-drop, the Move picker and "Remove from dataset" all post -- one
operation, because they are one operation: a project's membership is being set
to a dataset or to nothing. Three routes for three gestures would be three
places for the single-parent rule to be got wrong.

Nothing here touches a project. Deleting a dataset releases its members and
leaves every file where it was; a shared project (which Plexora cannot write to
at all) can be a member, because membership lives in the user's own root rather
than beside the project.
"""

from flask import jsonify, request

from plexora import app
from plexora.server.models import datasets
from plexora.server.models.project import Project


def _known():
    """The project names that currently resolve, for pruning and validation."""
    return set(Project.load_all())


def _listing():
    known = _known()
    return [dataset.to_wire()
            for dataset in sorted(datasets.load_all(known=known).values(),
                                  key=lambda d: d.name.casefold())]


@app.route('/datasets')
def list_datasets():
    return jsonify(success=True, datasets=_listing())


@app.route('/datasets', methods=['POST'])
def create_dataset():
    payload = request.get_json(silent=True) or {}
    try:
        dataset = datasets.create(
            payload.get("name"),
            description=payload.get("description") or "",
            projects=payload.get("projects") or (),
            known=_known(),
        )
    except datasets.DatasetError as exc:
        # 409 for a name already taken, 400 for everything else. The client
        # shows both as the same sentence; the code is what lets it tell "try
        # another name" from "that project is gone".
        taken = "already a dataset" in str(exc)
        return jsonify(success=False, error=str(exc)), 409 if taken else 400
    return jsonify(success=True, dataset=dataset.to_wire()), 201


@app.route('/datasets/<string:dataset_id>', methods=['POST'])
def update_dataset(dataset_id):
    """Rename, or set the description. Both, in one post, if you like."""
    payload = request.get_json(silent=True) or {}
    try:
        dataset = datasets.find(dataset_id)
        if dataset is None:
            return jsonify(success=False, error="Unknown dataset"), 404
        if payload.get("name") is not None:
            dataset = datasets.rename(dataset_id, payload["name"])
        if payload.get("description") is not None:
            dataset = datasets.describe(dataset_id,
                                        description=payload["description"])
    except datasets.DatasetError as exc:
        taken = "already a dataset" in str(exc)
        return jsonify(success=False, error=str(exc)), 409 if taken else 400
    return jsonify(success=True, dataset=dataset.to_wire())


@app.route('/datasets/<string:dataset_id>/delete', methods=['POST'])
def delete_dataset(dataset_id):
    """Remove the folder. **The projects in it are untouched** and go back to
    the top level, which is why this needs no confirmation about data loss --
    there is none, and the dialog says so."""
    dataset = datasets.remove(dataset_id)
    if dataset is None:
        return jsonify(success=False, error="Unknown dataset"), 404
    return jsonify(success=True, released=list(dataset.projects))


@app.route('/projects/assign', methods=['POST'])
def assign_projects():
    """Set which dataset holds these projects. `dataset: null` takes them out.

    The one verb behind every gesture that moves a project: dropping cards on a
    folder, dropping them on the breadcrumb root, the Move to… picker, and
    Remove from dataset. Single-parent, so a project is released from wherever
    it was before it lands and there is no moment at which one name is in two
    folders.
    """
    payload = request.get_json(silent=True) or {}
    names = payload.get("projects")
    if not isinstance(names, list) or not names:
        return jsonify(success=False, error="Name at least one project"), 400
    # Explicit: `null` is the meaningful "no dataset", and an absent key is a
    # caller that forgot to say. Treating the two the same would make a
    # malformed request silently unassign.
    if "dataset" not in payload:
        return jsonify(success=False,
                       error="Name a dataset, or null to remove from one"), 400

    try:
        dataset = datasets.assign(names, payload["dataset"], known=_known())
    except datasets.DatasetError as exc:
        missing = "no dataset" in str(exc)
        return jsonify(success=False, error=str(exc)), 404 if missing else 400
    return jsonify(success=True,
                   dataset=dataset.to_wire() if dataset else None,
                   projects=list(names))
