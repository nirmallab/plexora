"""Response helpers for plugin routes."""

from __future__ import annotations

import orjson
from flask import Response


def json_response(data) -> Response:
    """JSON response that can serialize numpy scalars and arrays.

    Plugin payloads are typically numpy-derived -- cell ids, per-cell values,
    mask indices -- and Flask's `jsonify` refuses those outright. orjson also
    avoids a Python-level encode of what can be millions of rows.
    """
    return Response(
        orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY),
        mimetype="application/json",
    )
