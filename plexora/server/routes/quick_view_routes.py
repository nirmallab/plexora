# "Quick view" landing page: register a datasource from just a local image
# path (no CSV/segmentation/h5ad) and hand the client a redirect straight
# into the viewer. Paths only, never bytes -- see datasource.py's
# register_image_datasource/register_rgb_datasource docstrings.

from pathlib import Path

from flask import jsonify, request, send_file

from plexora import app, get_config, get_config_names
from plexora.datasource import (
    _dedupe_dataset_name,
    _derive_dataset_name_from_path,
    _find_existing_datasource_for_image,
    _sniff_quick_view_kind,
    register_image_datasource,
    register_rgb_datasource,
)
from plexora.server.routes.import_routes import _node_locator, trim_filepath_quotes


@app.route('/quick_view', methods=['POST'])
def quick_view():
    payload = request.get_json(silent=True) or {}
    path = trim_filepath_quotes((payload.get('path') or '').strip())
    base_url = app.config.get('PLEXORA_BASE_URL', '')

    # Asked before the existence check, because `Path("node://hpc/slide")` is a
    # valid relative path that exists nowhere -- so "File does not exist" would
    # be the answer to a question nobody asked.
    try:
        located = _node_locator(path)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    if located:
        return _quick_view_on_node(located, base_url)

    if not path or not Path(path).is_file():
        return jsonify(success=False, error="File does not exist."), 400

    # Same image already registered (quick-viewed before, or imported through
    # the full wizard) -- reopen that project instead of creating a duplicate.
    existing_name = _find_existing_datasource_for_image(path, get_config())
    if existing_name:
        return jsonify(success=True, name=existing_name, redirect=f"{base_url}/{existing_name}")

    try:
        kind = _sniff_quick_view_kind(path)
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400

    name = _dedupe_dataset_name(_derive_dataset_name_from_path(path), get_config_names())

    try:
        if kind == 'ome_tiff':
            register_image_datasource(name=name, image=path)
        else:
            register_rgb_datasource(name=name, image=path)
    except Exception as exc:
        return jsonify(success=False, error=str(exc)), 400

    return jsonify(success=True, name=name, redirect=f"{base_url}/{name}")


def _quick_view_on_node(located, base_url):
    """Open an image that is on another machine, with the same one gesture.

    The landing page's promise is "no setup required", and that promise was
    only ever kept for somebody whose images are on the machine Plexora runs
    on. Picking a slide off a cluster is the same act and takes the same route:
    no CSV, no mask, straight into the viewer.

    Always the multichannel pipeline. The local branch also has an RGB one, for
    a PNG or a JPEG dropped on the page -- but a node serves images through the
    tile pipeline and nobody keeps a screenshot on a cluster, so there is
    nothing here for that branch to be.
    """
    from plexora import nodes as node_api
    from plexora.server.models.project import ImageSpec, Project

    node, resource_id = located
    existing_name = _find_node_datasource(node, resource_id)
    if existing_name:
        return jsonify(success=True, name=existing_name,
                       redirect=f"{base_url}/{existing_name}")

    name = _dedupe_dataset_name(resource_id, get_config_names())
    # The same two steps the import wizard takes (_register_node_image): an
    # empty project, then the attach that asks the node for its geometry. A
    # half-registered project is worse than none.
    Project(name=name, image=ImageSpec()).save()
    try:
        node_api.attach_image(name, node=node, resource_id=resource_id)
    except Exception as exc:
        Project.load(name).delete()
        return jsonify(success=False, error=str(exc)), 400

    return jsonify(success=True, name=name, redirect=f"{base_url}/{name}")


def _find_node_datasource(node, resource_id):
    """A project already pointed at this node's resource, or None.

    The node counterpart of `_find_existing_datasource_for_image`, and it
    cannot share that code: a node-backed project has no `channelFile` to
    compare, by design -- the primary never records a path on another machine.
    What it records is the binding, so that is what is matched.
    """
    from plexora.server.models.project import Project

    for name in get_config_names():
        try:
            binding = Project.load(name).resource("image")
        except Exception:
            continue
        if binding and binding.node == node and binding.resource_id == resource_id:
            return name
    return None


@app.route('/generated/rgb/<string:datasource>')
def generate_rgb_image(datasource):
    config = get_config()
    entry = config.get(datasource)
    if not entry or entry.get('image_kind') != 'rgb':
        return jsonify(error="Not an RGB quick-view datasource."), 404
    return send_file(entry['channelFile'])
