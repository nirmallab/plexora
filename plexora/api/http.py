"""Response helpers for plugin routes."""

from __future__ import annotations

import orjson
from flask import Response


def json_response(data) -> Response:
    """Build a JSON HTTP response that can serialize numpy scalars and arrays.

    Plugin route payloads are typically numpy-derived -- cell ids, per-cell
    values, mask indices -- and Flask's own `jsonify` refuses those outright.
    Uses orjson, which also avoids a Python-level encode of what can be
    millions of rows.

    Args:
        data: The value to serialize as JSON -- typically a dict or list,
            which may contain numpy scalars or arrays anywhere inside it.

    Returns:
        flask.Response: A response with `application/json` content, ready to
        return from a plugin route.

    Example:
        ```python
        from flask import Blueprint
        from plexora import api

        bp = Blueprint("my_plugin", __name__)

        @bp.route("/my_plugin/values")
        def values():
            return api.json_response({"ids": some_numpy_array})
        ```
    """
    return Response(
        orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY),
        mimetype="application/json",
    )
