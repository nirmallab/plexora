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
project names that the map does not have yet, and of every pyramid the
website's Python wrote into its store (`/data/<rel>`, which is
Documents/Plexora/projects/<rel> when the projects are kept there): that is
what lets the desktop app find the same file on this computer
(server/utils/relink.py) without asking -- and so never build again what the
website built. A mask's pyramid names the mask it was made from (`from`). The desktop app does the same for the files on its disk, but only in
a registry the website shares (`shared=True`: the website's projects on this
computer), so the website can match them in a folder the user grants.

A source that is a folder (an OME-Zarr or SpatialData store) has a folder
identity instead (`folder_identity`: `kind: "folder"`, its file count, bytes,
newest time and a hash of its listing), which `same_file` matches as a folder.
A project file (server/models/bundles.py) records one for every folder it
names, so the computer that imports it can find the store as it finds a file.
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
#: The website's project store, as its Python sees it (plexora/browser/boot.py).
STORE_ROOT = "/data/"
#: How a pyramid Plexora writes is named (image_pyramid COPY_SUFFIXES, the
#: mask pyramids' suffixes ending in the first): for `str.endswith`.
PYRAMID_ENDING = (".pyramid.ome.tiff", ".pyramid.approx")
#: A project entry's derived file and the field naming what it was made from.
_MADE_FROM = (("segmentation", "segmentationSource"),)


def website_file(value: str) -> bool:
    """A path the website records the identity of: one of the page's files,
    or a pyramid its Python wrote into the project store."""
    return value.startswith(PAGE_ROOTS) or (value.startswith(STORE_ROOT) and value.endswith(PYRAMID_ENDING))


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
    made_from = {out.get(made): out.get(source) for made, source in _MADE_FROM
                 if isinstance(out.get(made), str) and isinstance(out.get(source), str)}
    for path in sorted(_paths(out, wanted or (lambda value: value.startswith(PAGE_ROOTS)))):
        if path in known:
            continue
        facts = identify(path)
        if facts:
            facts = dict(facts)
            if not facts.get("from") and made_from.get(path) and made_from[path] != path:
                facts["from"] = made_from[path]
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
        return {name: record(entry, me, identify=identity, wanted=website_file) for name, entry in config.items()}
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


#: How many files a folder's identity walks before it settles for the folder's
#: own listing: the limit `segmentation_pyramid.source_fingerprint` stops at,
#: so a store too large to walk is never walked here either.
FOLDER_SCAN_LIMIT = 20000


def folder_identity(path) -> Optional[dict]:
    """{kind: "folder", name, entries, size, mtime_ms, fingerprint, listing}
    of a folder source (an OME-Zarr or SpatialData store), from stat calls
    alone, or None when it is not a folder here.

    `fingerprint` is `segmentation_pyramid.source_fingerprint`'s folder form
    (`<count>-<bytes>-<newest_ns>`), so the two can be compared; `listing` is
    a hash of the sorted relative paths and sizes, which a copy keeps when it
    does not keep the times. A store past FOLDER_SCAN_LIMIT files is known by
    its top level instead (`entries` None), as the fingerprint falls back to
    the folder's own time."""
    path = Path(path)
    try:
        stat = path.stat()
        if not path.is_dir():
            return None
    except OSError:
        return None
    count = total = newest = 0
    listing = hashlib.sha256()
    rows = []
    for root, folders, files in os.walk(path):
        folders.sort()
        for name in files:
            full = os.path.join(root, name)
            try:
                entry = os.stat(full)
            except OSError:
                continue
            count += 1
            total += entry.st_size
            newest = max(newest, entry.st_mtime_ns)
            rows.append(f"{os.path.relpath(full, path).replace(os.sep, '/')}\t{entry.st_size}")
            if count >= FOLDER_SCAN_LIMIT:
                try:
                    top = sorted(os.listdir(path))
                except OSError:
                    top = []
                return {"kind": "folder", "name": path.name, "entries": None, "size": None,
                        "mtime_ms": int(stat.st_mtime_ns // 1_000_000),
                        "fingerprint": f"dir-{stat.st_mtime_ns}",
                        "listing": hashlib.sha256("\n".join(top).encode("utf-8")).hexdigest()}
    for row in sorted(rows):
        listing.update(row.encode("utf-8") + b"\n")
    return {"kind": "folder", "name": path.name, "entries": count, "size": int(total),
            "mtime_ms": int(newest // 1_000_000), "fingerprint": f"{count}-{total}-{newest}",
            "listing": listing.hexdigest()}


def path_identity(path) -> Optional[dict]:
    """A file's identity, or a folder's (`folder_identity`)."""
    try:
        if Path(path).is_dir():
            return folder_identity(path)
    except (OSError, ValueError):
        return None
    return file_identity(path)


def is_folder(facts) -> bool:
    """Whether an identity is a folder's."""
    return isinstance(facts, dict) and facts.get("kind") == "folder"


def _same_folder(facts: dict, path) -> bool:
    """Whether the folder at `path` is the one `facts` describes: the same
    number of files and bytes, and the same newest time to the millisecond or
    the same listing. A store too large to walk is matched by its top level."""
    found = folder_identity(path)
    if found is None:
        return False
    if facts.get("entries") is None or found.get("entries") is None:
        return (facts.get("entries") is None and found.get("entries") is None
                and found.get("listing") == facts.get("listing"))
    if found["entries"] != facts.get("entries") or found["size"] != facts.get("size"):
        return False
    if facts.get("mtime_ms") is not None and abs(found["mtime_ms"] - int(facts["mtime_ms"])) <= 1:
        return True
    return bool(facts.get("listing")) and found["listing"] == facts.get("listing")


#: What `refresh` makes again: the facts a file's contents change.
_CONTENT_FACTS = ("size", "mtime_ms", "head", "tail")


def _same_path(recorded: str, path: str) -> bool:
    """One file's path either way it is spelled here (`D:\\a` and `d:/a` on
    Windows); a page file's path as it is."""
    if recorded == path:
        return True
    if recorded.startswith(PAGE_ROOTS) or path.startswith(PAGE_ROOTS):
        return False
    return os.path.normcase(os.path.normpath(recorded)) == os.path.normcase(os.path.normpath(path))


def refresh(config: dict, path: str, me: Optional[str] = None, *,
            identify: Callable = file_identity) -> bool:
    """After this app saved into `path` (server/utils/source_writes.py): its
    identity made again in every entry of `config` whose `sources` names it --
    as recorded, or as this app's own path for it -- so the other app still
    takes the updated file for the same one. In place; whether anything changed."""
    me = me or app_key()
    path = str(path)
    facts = None
    changed = False
    for entry in (config or {}).values():
        for recorded, info in ((entry or {}).get(SOURCES) or {}).items() if isinstance(entry, dict) else ():
            if not isinstance(info, dict) or not _same_path((info.get("at") or {}).get(me) or recorded, path):
                continue
            facts = facts or identify(path)
            if not facts:
                return changed
            for key in _CONTENT_FACTS:
                if info.get(key) != facts.get(key):
                    info[key] = facts.get(key)
                    changed = True
    return changed


def identity(path: str) -> Optional[dict]:
    """A page file's identity, and for a pyramid the page built, what it was
    built from (its OME description) and its key (its folder's name)."""
    facts = file_identity(path)
    if facts is None:
        return None
    if path.startswith("/derived/") or path.startswith(STORE_ROOT):
        parts = path.split("/")
        if path.startswith("/derived/") and len(parts) >= 4:
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
    (a copy keeps the bytes but not always the time). A folder identity
    (`folder_identity`) is matched as a folder."""
    if is_folder(facts):
        return _same_folder(facts, path)
    found = file_identity(path)
    if found is None or not facts or found["size"] != facts.get("size"):
        return False
    if facts.get("mtime_ms") is not None and abs(found["mtime_ms"] - int(facts["mtime_ms"])) <= 1:
        return True
    return bool(facts.get("head")) and found["head"] == facts.get("head") and found["tail"] == facts.get("tail")


def describe(info: Any) -> str:
    """A source's name, for a message."""
    return str((info or {}).get("name") or "a file")
