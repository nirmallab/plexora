"""What files dropped on the window would do, before they do it.

One route. The desktop shell delivers a drop as paths; the page asks here what
those paths are relative to the project it has open, and gets back a plan of
actions, each naming the existing route that carries it out
(`server/models/drop_plan.py`). Asked once while the files hover, to label the
place they would land, and once on the drop. Nothing here writes.
"""

from __future__ import annotations

from flask import jsonify, request

from plexora import app
from plexora.server.routes.import_routes import _json_errors, _picked


@app.route('/drop/inspect', methods=['POST'])
@_json_errors
def drop_inspect():
    """The plan for dropping these paths here.

    `{"paths": [...], "datasource"?, "target"?, "node"?}` -> the plan
    (`drop_plan.inspect_drop`). `datasource` is the project open on the page,
    and `target` the drop zone the paths are over ("viewer", "home", ...).
    `forget: true` drops the cached plans first, which the page sends with the
    drop itself so a file attached a moment ago is seen as attached.
    """
    from plexora.server.models import drop_plan

    payload = request.get_json(silent=True) or {}
    paths = _picked(payload)
    if not any(paths):
        return jsonify(error="paths is required"), 400
    if payload.get('forget'):
        drop_plan.forget()
    try:
        plan = drop_plan.inspect_drop(
            paths,
            datasource=(payload.get('datasource') or '').strip() or None,
            target=(payload.get('target') or '').strip() or None,
            node=(payload.get('node') or '').strip() or None)
    except drop_plan.UnknownDatasource as exc:
        return jsonify(error=f"Unknown sample: {exc.args[0]}"), 404
    return jsonify(plan)
