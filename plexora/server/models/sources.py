"""Where a project's files are, for each app that opens it.

The website and the desktop app can keep their projects in one folder
(Documents/Plexora/projects, client services/browserStore.js), so both read
the same config.json. But they do not see the same paths: a web page never
learns where a file is -- the website knows a picked image as
`/picked/<id>/<name>` and a pyramid it built as `/derived/<key>/<name>` --
while the desktop app knows `D:/data/<name>`. So each project entry may carry
a `sources` map, keyed by the path recorded when the file was added:

    "sources": {"/picked/ab12/slide.tif": {
        "name": "slide.tif", "size": 252151714, "mtime_ms": 1730000000123,
        "head": "<sha256 of the first MiB>", "tail": "<sha256 of the last MiB>",
        "from": "<a pyramid's source, as recorded>", "key": "<a pyramid's key>",
        "at": {"website": "/picked/ab12/slide.tif", "host:LAB-PC": "D:/data/slide.tif"}}}

`localize_config` (applied by project.read_config) gives every reader this
app's path wherever the map knows one, and `record_config` (applied by
project.write_config) puts the recorded paths back before the file is
written, so the other app's paths survive every save. Both are plain string
replacements over the entry's values, `sources` itself excepted, and are each
other's inverse.

On the website, `record_config` also adds the identity of every page file a
project names that the map does not have yet: that is what lets the desktop
app find the same file on this computer (server/utils/relink.py) without
asking. The desktop app does the same for the files on its disk, but only in
a registry the website shares (`shared=True`: the website's projects on this
computer), so the website can match them in a folder the user grants.
"""

from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path
from typing import Any, Callable, Optional

SOURCES = "sources"
#: The page's own files on the website (providers/browser.py PAGE_ROOTS).
PAGE_ROOTS = ("/picked/", "/derived/")
#: Bytes hashed at each end of a file for its identity.
IDENTITY_BYTES = 1 << 20


def app_key() -> str:
    """This app's name in `at`: the website, or this computer."""
    if os.environ.get("PLEXORA_BACKEND") == "browser":
        return "website"
    return f"host:{socket.gethostname()}"


def _replace(value, mapping: dict):
    """`value` with every string found in `mapping` replaced, recursively."""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_replace(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: (item if key == SOURCES else _replace(item, mapping)) for key, item in value.items()}
    return value


def _mapping(entry, me: str, reverse: bool) -> dict:
    out = {}
    for recorded, info in ((entry or {}).get(SOURCES) or {}).items():
        here = ((info or {}).get("at") or {}).get(me)
        if here and here != recorded:
            if reverse:
                out[here] = recorded
            else:
                out[recorded] = here
    return out


def localize(entry: dict, me: Optional[str] = None) -> dict:
    """One project entry as this app sees it."""
    if not isinstance(entry, dict) or not entry.get(SOURCES):
        return entry
    mapping = _mapping(entry, me or app_key(), reverse=False)
    return _replace(entry, mapping) if mapping else entry


def record(entry: dict, me: Optional[str] = None, *, identify: Optional[Callable] = None,
           wanted: Optional[Callable] = None) -> dict:
    """One project entry as it is written: the recorded paths, and -- with
    `identify` -- the identity of every file it names (`wanted`, by default a
    page file) that the map lacks."""
    if not isinstance(entry, dict):
        return entry
    me = me or app_key()
    mapping = _mapping(entry, me, reverse=True)
    out = _replace(entry, mapping) if mapping else entry
    if identify is None:
        return out
    known = dict(out.get(SOURCES) or {})
    added = False
    for path in sorted(_paths(out, wanted or (lambda value: value.startswith(PAGE_ROOTS)))):
        if path in known:
            continue
        facts = identify(path)
        if facts:
            facts = dict(facts)
            facts["at"] = {me: path}
            known[path] = facts
            added = True
    if added:
        out = dict(out)
        out[SOURCES] = known
    return out


def _paths(value, wanted) -> set:
    found = set()
    if isinstance(value, str):
        if wanted(value):
            found.add(value)
    elif isinstance(value, list):
        for item in value:
            found |= _paths(item, wanted)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key != SOURCES:
                found |= _paths(item, wanted)
    return found


def _local_file(value: str) -> bool:
    """A path to a file on this computer (not a page file, not an address)."""
    if len(value) < 3 or value.startswith(PAGE_ROOTS) or "://" in value or not os.path.isabs(value):
        return False
    try:
        return Path(value).is_file()
    except (OSError, ValueError):
        return False


def localize_config(config: dict, me: Optional[str] = None) -> dict:
    """Every entry of a config.json as this app sees it."""
    if not isinstance(config, dict):
        return config
    me = me or app_key()
    return {name: localize(entry, me) for name, entry in config.items()}


def record_config(config: dict, me: Optional[str] = None, *, shared: bool = False) -> dict:
    """Every entry of a config.json as it is written (`record`): on the
    website with its page files identified; in a registry the website shares
    (`shared`), with this computer's files identified."""
    if not isinstance(config, dict):
        return config
    me = me or app_key()
    if me == "website":
        return {name: record(entry, me, identify=identity) for name, entry in config.items()}
    if shared:
        return {name: record(entry, me, identify=file_identity, wanted=_local_file)
                for name, entry in config.items()}
    return {name: record(entry, me) for name, entry in config.items()}


def file_identity(path) -> Optional[dict]:
    """{name, size, mtime_ms, head, tail} of a file, or None when it cannot be read."""
    path = Path(path)
    try:
        stat = path.stat()
        size = stat.st_size
        with path.open("rb") as handle:
            head = hashlib.sha256(handle.read(IDENTITY_BYTES)).hexdigest()
            handle.seek(max(0, size - IDENTITY_BYTES))
            tail = hashlib.sha256(handle.read(IDENTITY_BYTES)).hexdigest()
    except OSError:
        return None
    return {"name": path.name, "size": int(size), "mtime_ms": int(stat.st_mtime_ns // 1_000_000),
            "head": head, "tail": tail}


def identity(path: str) -> Optional[dict]:
    """A page file's identity, and for a pyramid the page built, what it was
    built from (its OME description) and its key (its folder's name)."""
    facts = file_identity(path)
    if facts is None:
        return None
    if path.startswith("/derived/"):
        parts = path.split("/")
        if len(parts) >= 4:
            facts["key"] = parts[2]
        try:
            from plexora.server.utils import image_pyramid

            source = image_pyramid.copy_source(path)
        except Exception:  # noqa: BLE001 -- not a copy Plexora wrote: no source to name
            source = None
        if source:
            facts["from"] = str(source)
    return facts


def same_file(facts: dict, path) -> bool:
    """Whether `path` is the file `facts` describes: the same size, and the
    same modification time to the millisecond or the same bytes at both ends
    (a copy keeps the bytes but not always the time)."""
    found = file_identity(path)
    if found is None or not facts or found["size"] != facts.get("size"):
        return False
    if facts.get("mtime_ms") is not None and abs(found["mtime_ms"] - int(facts["mtime_ms"])) <= 1:
        return True
    return bool(facts.get("head")) and found["head"] == facts.get("head") and found["tail"] == facts.get("tail")


def describe(info: Any) -> str:
    """A source's name, for a message."""
    return str((info or {}).get("name") or "a file")
