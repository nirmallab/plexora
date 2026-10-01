"""Offloading large tool results: the model sees a stub, and asks for slices.

A tool result above `threshold_tokens` (4k by default) is stored, content-
addressed, under `<data_root>/.agent/ai/offload/<id>.txt`, and the model is
given `{artifact_id, kind, head, tail, size}` instead -- the first and last
few hundred characters, and how big the whole is. `read_artifact(id, range |
query)` (a local tool in chat mode) returns a character range or the lines
that match a query. Context stays small; nothing is lost; and the same result
offloaded twice is stored once and has the same id.

The stored text is JSON indented one space per level, so a query matches a
line that means something (`"marker": "CD45"`) rather than the whole answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

CHARS_PER_TOKEN = 3.5
ID_PATTERN = re.compile(r"^off_[0-9a-f]{20}$")
HEAD_CHARS = 1200
TAIL_CHARS = 600
#: Characters one `read_artifact` slice returns at most.
MAX_SLICE = 12_000
MAX_MATCHES = 80
#: The largest text stored at all (a bigger answer is cut, and says so).
MAX_STORED = 8 * 1024 * 1024


def default_root() -> Path:
    from plexora import paths

    return paths.agent_root() / "ai" / "offload"


def _text(result) -> str:
    if isinstance(result, str):
        return result
    from plexora.mcp import serialize

    return json.dumps(serialize._clean(result), indent=1, ensure_ascii=False, default=str)


def tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def offload(result, threshold_tokens: int = 4000, *, kind: str = "tool_result",
            root: Path | str | None = None) -> dict | None:
    """The stub for `result` when it is larger than `threshold_tokens`, after
    storing it; None when it is small enough to send as it is."""
    text = _text(result)
    if tokens(text) <= threshold_tokens:
        return None
    cut = len(text) > MAX_STORED
    if cut:
        text = text[:MAX_STORED]
    artifact_id = "off_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
    folder = Path(root) if root else default_root()
    path = folder / f"{artifact_id}.txt"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    stub = {"offloaded": True, "artifact_id": artifact_id, "kind": kind, "size": len(text),
            "tokens": tokens(text), "lines": text.count("\n") + 1,
            "head": text[:HEAD_CHARS], "tail": text[-TAIL_CHARS:],
            "hint": "read_artifact with this artifact_id and a character range, or a query, "
                    "returns the parts you need"}
    if cut:
        stub["truncated_at"] = MAX_STORED
    return stub


def read_artifact(artifact_id: str, *, start: int | None = None, end: int | None = None,
                  query: str | None = None, root: Path | str | None = None) -> dict:
    """A slice of an offloaded result: characters [start, end), or the lines
    that contain `query` (case-insensitive) with their line numbers."""
    if not ID_PATTERN.match(str(artifact_id)):
        raise KeyError(f"not an offloaded artifact id: {artifact_id!r}")
    path = (Path(root) if root else default_root()) / f"{artifact_id}.txt"
    if not path.is_file():
        raise KeyError(f"no offloaded artifact {artifact_id!r}")
    text = path.read_text(encoding="utf-8")
    if query:
        needle = str(query).lower()
        matches = [{"line": n + 1, "text": line[:400]}
                   for n, line in enumerate(text.splitlines()) if needle in line.lower()]
        return {"artifact_id": artifact_id, "query": query, "matches": matches[:MAX_MATCHES],
                "n_matches": len(matches), "truncated": len(matches) > MAX_MATCHES}
    lo = max(0, int(start or 0))
    hi = min(len(text), int(end) if end is not None else lo + MAX_SLICE, lo + MAX_SLICE)
    return {"artifact_id": artifact_id, "start": lo, "end": hi, "size": len(text),
            "text": text[lo:hi], "more": hi < len(text)}
