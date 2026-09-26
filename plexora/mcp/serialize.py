"""Turning a capability's result into what goes over the wire -- bounded.

An agent's context is the scarce resource. A result is serialized as compact
JSON; when it would exceed `MAX_TOOL_CHARS`, its longest lists are halved until
it fits, and the result says `truncated: true` with the paths that were cut --
never a silently shorter answer.
"""

from __future__ import annotations

import json
import math
from typing import Any

from plexora.agent.limits import MAX_TOOL_CHARS


def _clean(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if hasattr(value, "item") and callable(value.item):
        try:
            return _clean(value.item())
        except Exception:
            return str(value)
    return value


def dumps(value) -> str:
    return json.dumps(_clean(value), separators=(",", ":"), ensure_ascii=False, default=str)


def _lists(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _lists(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        yield path, value
        for index, item in enumerate(value[:20]):
            yield from _lists(item, f"{path}[{index}]")


def bound(value: Any, limit: int = MAX_TOOL_CHARS) -> str:
    """Compact JSON of `value`, at most about `limit` characters."""
    value = _clean(value)
    text = dumps(value)
    if len(text) <= limit:
        return text
    cut = []
    for _ in range(64):
        candidates = [(len(dumps(items)), path, items) for path, items in _lists(value)
                      if len(items) > 1]
        if not candidates:
            break
        _size, path, items = max(candidates, key=lambda c: c[0])
        keep = max(1, len(items) // 2)
        dropped = len(items) - keep
        del items[keep:]
        cut.append({"path": path, "dropped": dropped})
        if isinstance(value, dict):
            value["truncated"] = True
            value["_truncated"] = cut
        text = dumps(value)
        if len(text) <= limit:
            return text
    return text[:limit]
