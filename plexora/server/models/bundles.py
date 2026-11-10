"""Projects as one file: what moves them between the website, the desktop app,
other computers and other people.

A `.plexora` file (FORMAT 2) is a ZIP64 archive any unzip tool opens. It
carries one project or many -- a whole dataset, with its figures and
bookmarks -- and by default not their images: those are referenced by name,
size and identity (server/models/sources.py) and found on the computer that
imports the file (server/utils/relink.py). Deflate rather than zstd, because
Python's `zipfile`, Finder/Explorer and the browser's
`DecompressionStream("deflate-raw")` all read it.

    plexora-bundle.json                 the manifest: first member, stored
    projects/<id>/entry.json            the registry entry, its paths tokenised
    projects/<id>/state/<rel>           the project's own folder; <name>.db as
                                        an sqlite backup snapshot
    projects/<id>/derived/<rel>         opt-in "ready to open": files the entry
                                        names that Plexora made (tenx h5ad,
                                        *_tiled pictures, pyramids, projections)
    media/<key>/<name>                  opt-in: the user's images, masks and
                                        tables, keyed by their identity
    captures/<id>.json, captures/<id>/<n>.<fmt>
                                        bookmarks and captures of project <id>
    figures/<figure id>/...             a figure whose images all come from
                                        projects in the file

The manifest:

    {format: 2, bundle: {id, created, app: {kind, version, at}},
     projects: [{id, name, title, revision, parent, entry: {path, size, sha256},
                 files: [{path, size, sha256, kind: "state"|"table"|"derived"}],
                 refs: [{token, role, kind: "file"|"folder"|"node", identity,
                         included: <media path>|null, derived,
                         hints: {absolute, relative_to_bundle, at, recorded}}]}],
     media: [{path, size, sha256}], captures: [{project, path, size, sha256,
     count, previews: [...]}], datasets: [{id, name, projects: [id]}],
     figures: [{id, title, projects: [id], files: [...]}], left_out: [...]}

**Tokens.** The entry is written with `{{state}}/<rel>` and `{{derived}}/<rel>`
for the files inside the project, `{{ref:N}}` for every file outside it, and
`/generated/data/{{name}}/` for its tile addresses. Import replaces each with
the target's paths, so a project imported under another name, or on another
computer, points at its own files. A ref the importing computer finds is
recorded where it is found (`sources[...].at`); one it does not keeps its
identity, so `relink.ensure` and the website's "Find them..." go on looking.

**What goes.** Everything in the project's folder except what is rebuilt when
it opens (CACHES, one tuple shared with the size estimate), and the cell tables
Plexora made inside the project (the edited copy of a table, a corrected
parquet), which exist nowhere else. Never the computer's own records: nodes,
remotes, servers, tokens, telemetry, licences, the remote cache.

**Sync-ready.** Every carried file's sha256 is taken while it streams (never a
second read). A project's `revision` is the sha256 of its canonical entry and
its (path, sha256) pairs; import records `origin: {bundle, revision,
importedAt}` and `bundleRevision` in the entry (both round-trip through
`Project.extra`), and the next export names that revision as its `parent`.
That is what tells New from Already here, Update and Changed on both.

**Import** (`preview` then `apply`) checks every member first (only listed
paths; no absolute paths, `..`, drive letters, backslashes or links; no member
that inflates more than MAX_RATIO times; room on the disk), unpacks into
`<root>/.importing-<bundle id>/`, renames the rows of every database table
keyed by `datasource` when the project's name changes, replaces the tokens,
moves the files into place, merges datasets, captures and figures, and writes
the registry last. A failure or a cancel undoes all of it.

FORMAT 1 files (`plexora-project.json` + `project/<path>`) still import.

**Where the bytes move.** On the desktop and a server, here: `ZipSink`
streams each member into `<dest>.part` and renames it when the central
directory reads back right; `ZipSource` reads a member at a time by random
access. Nothing holds a bundle in memory. The website's Python cannot (its
heap is 4 GiB and its large files are not in it), so there Python only
decides -- `plan_for_page` lists every member with its source path and
`finish_manifest` turns the page's sums into the manifest; an import reads the
picked file where it is (the page mounts it read-only, so `ZipSource` reads a
slice at a time), `placements` says where each large member goes, and
`apply(..., placed=...)` takes those the page wrote there -- and the page's
own worker (client workers/bundleWorker.js) moves the bytes. Export, import
and "Make a copy" run as background tasks (`start_export`, `start_import`,
`start_copy`: stages Snapshot, Pack, Verify; Verify, Unpack, Place), mirrored
to the website's page like every long task (`layer_jobs`). The format and the
later cloud sync are written up in docs/internal/PROJECT_BUNDLES.md.

    plan(ids, include) -> Plan             estimate(ids, include) -> sizes
    export(ids, dest, include) -> manifest preview(path) -> per-project status
    apply(path, choices, into) -> result   import_bundle(data, name) -> name
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat as stat_module
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field, replace as dataclass_replace
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Optional

from plexora import paths
from plexora.server.models import sources

FORMAT = 2
MANIFEST = "plexora-bundle.json"
SUFFIX = ".plexora"
#: FORMAT 1, read only: its manifest and the folder its files are under.
FORMAT_1 = 1
MANIFEST_1 = "plexora-project.json"
FOLDER_1 = "project/"

#: Bytes copied at a time, hashed and counted as they go.
CHUNK = 1 << 20
#: Bytes of a member deflated at level 1 to decide how it is stored.
SAMPLE = 4 << 20
#: Stored whole: these are compressed already.
STORED_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".parquet", ".gz", ".zst", ".zip", ".mp4",
                   ".h5ad", ".npz", ".jp2")
#: A sample that deflates to more than this share of its size is stored.
INCOMPRESSIBLE = 0.9
#: A member larger than RATIO_FLOOR that inflates more than this is refused.
MAX_RATIO = 1000
RATIO_FLOOR = 1 << 20
#: What an import may take of the disk's free space.
FREE_SHARE = 0.95
#: A member at least this large is written with ZIP64 sizes from the start.
ZIP64_FROM = 1 << 30

#: Rebuilt when the project opens, so never carried and never counted. One
#: tuple for the export, the size estimate and the dialog: fnmatch patterns
#: over a file's path inside the project folder (`*` crosses folders).
CACHES = (
    "spatial_index_v*/*", "ball_tree.pickle",             # the cell index (data_model)
    "centroids_v*/*", "transcripts_v*/*", "bins_v*/*",     # tile caches
    "volume_v*/*", "objects_planes_v*/*",                  # volume bricks, plane index
    "*_pyramid.zarr/*",                                    # brightfield/dicom/tiff/image pyramids
    "plugins/gating/classifier/features/*",                # the classifier's feature cache
    "plugins/qc/*.npz",                                    # QC score fields, by fingerprint
    ".thumbnail*", ".open-*",                              # the card's picture, open marks
    "*.bak*", "*.corrupt-*", "*.lock", "*.tmp", "*.tmp.*", "*.part", "*-journal",
)
#: A project folder never holds these, but they are the computer's own and are
#: never carried even if one did: nodes and servers, their tokens, telemetry,
#: licences, the remote cache, an agent's notes.
NEVER = ("nodes.json", "remotes.json", "servers.json", "tokens.json", "server_token.json",
         ".telemetry", ".remote_cache", ".agent", ".videos", "licence.json", "license.json",
         "license.key", "licence.key")

#: What a bundle carries besides the project's own work, and the default of
#: each: tables made in Plexora, bookmarks/captures and figures on; the user's
#: images and the ready-to-open files off.
INCLUDE = ("tables", "captures", "figures", "images", "derived")
INCLUDE_DEFAULT = {"tables": True, "captures": True, "figures": True, "images": False, "derived": False}

#: Files the entry names that Plexora made and can make again, carried only
#: when "ready to open" is asked for.
DERIVED_NAMES = ("tenx/*", "*_tiled.ome.tif", "*.pyramid.ome.tif", "*.pyramid.ome.tiff",
                 "projections_v*/*", "surfaces_v*/*")
TABLE_SUFFIXES = (".csv", ".tsv", ".txt", ".parquet", ".h5ad", ".feather", ".arrow", ".xlsx")
#: Where an included image lands in the project's folder, and is known again.
MEDIA = "media"

STATE_TOKEN = "{{state}}"
DERIVED_TOKEN = "{{derived}}"
NAME_TOKEN = "{{name}}"
TILES = "/generated/data/"
_REF = re.compile(r"^\{\{ref:(\d+)\}\}$")

#: Keys of an entry that are this computer's bookkeeping, not the project.
_LOCAL_KEYS = ("sources", "origin", "bundleRevision", "lastOpenedAt")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$")
_FIGURE_ID = re.compile(r"^fig_[a-z0-9]{6,32}$")
_SQLITE_HEADER = b"SQLite format 3\x00"


class BundleError(ValueError):
    """A file that is not a Plexora project bundle, or one that cannot be read."""


class Cancelled(BundleError):
    """The user stopped the export or the import."""


_OUTSIDE = "This Plexora project file names a place outside its project."


# -- small helpers -----------------------------------------------------------


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _cached(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in CACHES)


def _never(rel: str) -> bool:
    first = rel.split("/", 1)[0]
    return first in NEVER or PurePosixPath(rel).name in NEVER


def _derived_name(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in DERIVED_NAMES)


def _include(include) -> dict:
    """`include` as a full mapping: a mapping, an iterable of names (those on,
    the rest off), or None for the defaults."""
    if include is None:
        return dict(INCLUDE_DEFAULT)
    if isinstance(include, str):
        include = [part.strip() for part in include.split(",") if part.strip()]
    if isinstance(include, dict):
        out = dict(INCLUDE_DEFAULT)
        out.update({key: bool(value) for key, value in include.items() if key in INCLUDE})
        return out
    names = {str(item).strip().lower() for item in include}
    unknown = names - set(INCLUDE)
    if unknown:
        raise BundleError(f"Unknown part to include: {sorted(unknown)[0]}. "
                          f"Choose from {', '.join(INCLUDE)}.")
    return {key: key in names for key in INCLUDE}


def _inside(path: str, folder: Optional[Path]) -> Optional[str]:
    """`path` relative to `folder` with forward slashes, or None when it is
    not inside it (spelled either way on Windows)."""
    if folder is None or not path:
        return None
    try:
        here = os.path.normcase(os.path.abspath(path))
        base = os.path.normcase(os.path.abspath(str(folder)))
    except (TypeError, ValueError):
        return None
    if here == base or not here.startswith(base.rstrip(os.sep) + os.sep):
        return None
    return os.path.relpath(os.path.abspath(path), os.path.abspath(str(folder))).replace(os.sep, "/")


def _canonical(entry: dict) -> str:
    clean = {key: value for key, value in (entry or {}).items() if key not in _LOCAL_KEYS and key != "id"}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def revision_of(entry: dict, files: Iterable[tuple]) -> str:
    """A project's revision: the sha256 of its canonical tokenised entry
    (without what one computer adds: `lastOpenedAt`, `sources`, `origin`,
    `bundleRevision`) and its sorted (path, sha256) pairs."""
    digest = hashlib.sha256(_canonical(entry).encode("utf-8"))
    for path, sha in sorted(files):
        digest.update(f"\n{path}\t{sha}".encode("utf-8"))
    return digest.hexdigest()


def _stamp(entry: dict, stats: Iterable[tuple]) -> str:
    """A stat-only fingerprint of a project as it is now: whether it moved
    since a revision was recorded, without reading a file."""
    digest = hashlib.sha256(_canonical(entry).encode("utf-8"))
    for rel, size, mtime_ns in sorted(stats):
        digest.update(f"\n{rel}\t{size}\t{mtime_ns}".encode("utf-8"))
    return digest.hexdigest()


def _app() -> dict:
    try:
        from plexora import __version__ as version
    except Exception:  # noqa: BLE001 -- a version is a courtesy
        version = "0"
    me = sources.app_key()
    if me == "website":
        kind = "website"
    elif os.environ.get("PLEXORA_DESKTOP", "").lower() in ("1", "true", "yes"):
        kind = "desktop"
    else:
        kind = "server"
    return {"kind": kind, "version": str(version), "at": me}


def _safe_member(name: str) -> bool:
    """A member path that stays inside wherever it is unpacked."""
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        return False
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return False
    parts = name.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _safe_name(name: str) -> str:
    """A project name usable as a folder name here."""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "").strip()).strip(". ")
    return text or "project"


# -- projects as this computer has them --------------------------------------


@dataclass
class _Local:
    """One registered project, and where its files are."""

    name: str
    id: str
    root: Path
    entry: dict
    state_dir: Path
    read_dir: Path
    derived_dir: Path
    shared: bool
    writable: bool


def _project_module():
    from plexora.server.models import project as project_module

    return project_module


def _project_id(name: str, root: Path, entry: dict, *, assign: bool = True) -> str:
    """The project's id (`id` in its entry): `project.ensure_id` when this
    build has it, else one written into the entry here, or for a project in a
    root nobody here can write, one made from where it is."""
    found = entry.get("id")
    if isinstance(found, str) and _ID.match(found):
        return found
    if assign:
        ensure = getattr(_project_module(), "ensure_id", None)
        if callable(ensure):
            try:
                got = ensure(name)
                if isinstance(got, str) and _ID.match(got):
                    return got
            except Exception:  # noqa: BLE001 -- fall back to writing it here
                pass
        if root in paths.owned_roots() and paths.is_writable(root):
            module = _project_module()
            with module.config_transaction():
                config = module.read_config(paths.config_path(root))
                current = config.get(name)
                if isinstance(current, dict):
                    pid = current.get("id")
                    if not (isinstance(pid, str) and _ID.match(pid)):
                        pid = uuid.uuid4().hex
                        current["id"] = pid
                        module.write_config(paths.config_path(root), config)
                    return pid
    return uuid.uuid5(uuid.NAMESPACE_URL, f"plexora:{root}/{name}").hex


def _local(name: str, *, assign: bool = True) -> _Local:
    module = _project_module()
    root = module.Project.root_for(name)
    if root is None:
        raise KeyError(f"Unknown project: {name!r}")
    entry = module.read_config(paths.config_path(root)).get(name)
    if not isinstance(entry, dict):
        raise KeyError(f"Unknown project: {name!r}")
    owned = root in paths.owned_roots()
    pid = _project_id(name, Path(root), entry, assign=assign)
    entry = dict(entry, id=pid)
    return _Local(name=name, id=pid, root=Path(root), entry=entry,
                  state_dir=paths.project_state_dir(name), read_dir=paths.project_dir(name, root),
                  derived_dir=paths.derived_root(name, home_root=root),
                  shared=not owned, writable=owned and paths.is_writable(root))


def resolve_projects(ids_or_names: Iterable[str] = (), dataset: Optional[str] = None) -> list:
    """Project names for ids or names (either), plus every project of
    `dataset` (by id or name), in order and once each."""
    module = _project_module()
    entries = module.Project.load_all()
    by_id = {entry.get("id"): name for name, entry in entries.items()
             if isinstance(entry, dict) and entry.get("id")}
    out = []
    for raw in ids_or_names or ():
        text = str(raw or "").strip()
        if not text:
            continue
        name = text if text in entries else by_id.get(text)
        if name is None:
            raise KeyError(f"Unknown project: {text!r}")
        if name not in out:
            out.append(name)
    if dataset:
        from plexora.server.models import datasets as registry

        found = registry.resolve(dataset, known=set(entries))
        out += [name for name in found.projects if name not in out]
    if not out:
        raise BundleError("Name at least one project.")
    return out


# -- the plan ----------------------------------------------------------------


@dataclass
class Member:
    """One file of a bundle: where it goes, what it is, and where it comes
    from (a path in this Python's filesystem, or `load()` for what is made on
    the way: an entry, a database snapshot, a capture)."""

    bundle_path: str
    kind: str
    size: int
    source: Optional[str] = None
    project: Optional[str] = None
    record: Optional[dict] = None
    load: Optional[Callable[[], bytes]] = None
    snapshot: bool = False
    mtime_ns: int = 0

    def to_json(self) -> dict:
        return {"bundle_path": self.bundle_path, "source": self.source, "kind": self.kind,
                "size": self.size, "project": self.project}


@dataclass
class Plan:
    """What an export writes: members in order, the manifest they fill in,
    each project's tokenised entry, and what was left out."""

    manifest: dict
    members: list
    entries: dict
    projects: list
    include: dict
    stamps: dict = field(default_factory=dict)
    caches: dict = field(default_factory=lambda: {"count": 0, "size": 0})
    #: {project id: [ref, ...]} with what the manifest leaves out of them:
    #: each one's size and where it is (`where`), for the dialog.
    refs: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(member.size for member in self.members)

    def materialise(self, work_dir) -> None:
        """Give every member a file: database snapshots, entries and captures
        written into `work_dir` (the website's page reads them from there)."""
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        for index, member in enumerate(self.members):
            if member.snapshot and member.source:
                target = work / f"{index:05d}.db"
                _snapshot(member.source, target)
                member.source, member.snapshot = str(target), False
                member.size = target.stat().st_size
            elif member.load is not None:
                target = work / f"{index:05d}.bin"
                target.write_bytes(member.load())
                member.source, member.load = str(target), None
                member.size = target.stat().st_size

    def to_json(self, work_dir=None) -> dict:
        """JSON for the website's byte mover: members with absolute sources
        (after `materialise(work_dir)` when given), the tokenised entries and
        the manifest skeleton (its sha256 and revisions filled in afterwards
        by `finish_manifest`)."""
        if work_dir is not None:
            self.materialise(work_dir)
        return {"format": FORMAT, "manifest": self.manifest, "entries": self.entries,
                "members": [member.to_json() for member in self.members],
                "total": self.total, "include": self.include}


def _read_only(path) -> sqlite3.Connection:
    """An sqlite connection that cannot write (a URI, so spaces and `?` in
    the path are escaped: Application Support has one)."""
    return sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=10)


def _snapshot(source, target) -> None:
    """A consistent copy of an sqlite database (its backup API: what a write
    in progress has not committed is not in it)."""
    target = Path(target)
    target.unlink(missing_ok=True)
    origin = _read_only(source)
    try:
        copy = sqlite3.connect(str(target))
        try:
            origin.backup(copy)
        finally:
            copy.close()
    finally:
        origin.close()


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == _SQLITE_HEADER
    except OSError:
        return False


def _walk(folder: Path, skip: Callable[[str], bool]):
    """(rel, path, size, mtime_ns) for every regular file under `folder`,
    links never followed, folders `skip` names never entered."""
    if folder is None or not folder.is_dir():
        return
    for root, folders, files in os.walk(folder, followlinks=False):
        rel_root = os.path.relpath(root, folder).replace(os.sep, "/")
        rel_root = "" if rel_root == "." else rel_root + "/"
        folders[:] = sorted(name for name in folders
                            if not skip(rel_root + name + "/_")
                            and not os.path.islink(os.path.join(root, name)))
        for name in sorted(files):
            full = os.path.join(root, name)
            try:
                info = os.lstat(full)
            except OSError:
                continue
            if not stat_module.S_ISREG(info.st_mode):
                continue
            yield rel_root + name, full, info.st_size, info.st_mtime_ns


def _strings(value, role=""):
    """(role, string) for every string of an entry, `sources` and this
    computer's bookkeeping excepted."""
    if isinstance(value, str):
        yield role, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{role}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not role and key in _LOCAL_KEYS:
                continue
            yield from _strings(item, f"{role}.{key}" if role else key)


def _replace_strings(value, change: Callable[[str], str], top=True):
    if isinstance(value, str):
        return change(value)
    if isinstance(value, list):
        return [_replace_strings(item, change, False) for item in value]
    if isinstance(value, dict):
        return {key: (item if top and key in _LOCAL_KEYS else _replace_strings(item, change, False))
                for key, item in value.items()}
    return value


def _is_url(value: str) -> bool:
    return "://" in value[:16]


def _pathlike(value: str) -> bool:
    if len(value) < 3 or "\n" in value or _is_url(value) or value.startswith(TILES):
        return False
    return os.path.isabs(value) or value.startswith(sources.PAGE_ROOTS) or bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _exists(value: str) -> bool:
    try:
        return Path(value).exists()
    except (OSError, ValueError):
        return False


def _identity(path: str, known: Optional[dict], compute: bool) -> Optional[dict]:
    """A ref's identity: the one the project recorded, else made here."""
    if known:
        return {key: value for key, value in known.items() if key != "at"}
    if not compute:
        return None
    return sources.path_identity(path)


def _size_of(path: str) -> Optional[int]:
    try:
        target = Path(path)
        if target.is_dir():
            return sum(size for _rel, _full, size, _m in _walk(target, lambda rel: False))
        return target.stat().st_size
    except OSError:
        return None


def _shared_places() -> list:
    """The shared roots and the Locations of kind "shared"."""
    return list(paths.shared_roots()) + [Path(loc["path"]) for loc in _locations()
                                         if loc.get("kind") == "shared" and loc.get("path")]


def where(path: str, shared: Optional[list] = None) -> str:
    """Where a file the project names is: "node" (stays on its data node),
    "shared" (a shared root, a /Volumes mount, a network path), "here" (this
    computer), or "missing". `shared`: `_shared_places()`, read once by a
    caller asking about many files."""
    text = str(path or "")
    if text.startswith("node://"):
        return "node"
    if _is_url(text):
        return "remote"
    if text.startswith(("\\\\", "//")):
        return "shared"
    roots = _shared_places() if shared is None else shared
    if any(_inside(text, root) for root in roots):
        return "shared"
    if sys.platform == "darwin" and text.startswith("/Volumes/"):
        return "shared"
    return "here" if _exists(text) else "missing"


def _locations() -> list:
    """Part 1's Locations (server/models/library.py), when this build has them."""
    try:
        from plexora.server.models import library  # noqa: PLC0415 -- optional, newer builds
    except Exception:  # noqa: BLE001
        return []
    try:
        found = library.locations(detail=False) or []
    except Exception:  # noqa: BLE001 -- a location list that fails is no list
        return []
    return [loc.to_dict() if hasattr(loc, "to_dict") else loc for loc in found
            if hasattr(loc, "to_dict") or isinstance(loc, dict)]


def _relative_hint(path: str, dest) -> Optional[str]:
    """`path` relative to the folder the bundle is saved in, when the two
    share more than the top of the disk: the network-share case."""
    if not dest or not _pathlike(path) or path.startswith(sources.PAGE_ROOTS):
        return None
    try:
        folder = os.path.dirname(os.path.abspath(str(dest)))
        target = os.path.abspath(path)
        common = os.path.commonpath([folder, target])
    except (ValueError, TypeError):
        return None
    anchor = Path(common).anchor
    if not common or os.path.normcase(common.rstrip("\\/")) == os.path.normcase(anchor.rstrip("\\/")):
        return None
    return os.path.relpath(target, folder).replace(os.sep, "/")


def _project_plan(local: _Local, include: dict, *, dest=None, identities=True, media_keys=None):
    """One project's part of a plan: its members, manifest record, tokenised
    entry, its stat stamp and what was left out."""
    name, pid = local.name, local.id
    entry = local.entry
    known = entry.get("sources") or {}
    shared = _shared_places()
    recorded_for = {}
    for recorded, info in known.items():
        here = ((info or {}).get("at") or {}).get(sources.app_key())
        recorded_for[here or recorded] = recorded
    base = f"projects/{pid}/"
    members, files, refs, left_out = [], [], [], []
    stats = []
    caches = {"count": 0, "size": 0}
    tables = {}       # rel -> path, named tables inside the project
    derived = {}      # rel -> path, named derived files
    ref_by_path = {}

    places = [("state", local.state_dir)]
    if local.read_dir != local.state_dir:
        places.append(("state", local.read_dir))
    if local.derived_dir not in (local.state_dir, local.read_dir):
        places.append(("derived", local.derived_dir))

    def internal(value):
        for token, folder in places:
            rel = _inside(value, folder)
            if rel is not None:
                return token, rel
        return None

    def add_ref(value, role, kind=None, derived_flag=False):
        if value in ref_by_path:
            ref = refs[ref_by_path[value]]
            ref.setdefault("roles", [ref["role"]]).append(role)
            return ref["token"]
        recorded = recorded_for.get(value, value)
        info = known.get(recorded) or {}
        identity = _identity(value, info, identities and _exists(value))
        folder = (sources.is_folder(identity) if identity else (_exists(value) and Path(value).is_dir()))
        token = "{{ref:%d}}" % len(refs)
        hints = {"absolute": value, "relative_to_bundle": _relative_hint(value, dest),
                 "at": dict((info or {}).get("at") or {}), "recorded": recorded}
        refs.append({"token": token, "role": role, "kind": kind or ("folder" if folder else "file"),
                     "identity": identity, "included": None, "derived": bool(derived_flag),
                     "size": (identity or {}).get("size") if identity else _size_of(value),
                     "hints": hints, "where": where(value, shared)})
        ref_by_path[value] = len(refs) - 1
        return token

    entry_tokens = {}
    for role, value in _strings(entry):
        if value in entry_tokens:
            continue
        if value.startswith(f"{TILES}{name}/"):
            entry_tokens[value] = f"{TILES}{NAME_TOKEN}/" + value[len(f"{TILES}{name}/"):]
            continue
        if value.startswith("node://") or not _pathlike(value):
            continue
        inside = internal(value)
        if inside is not None:
            token, rel = inside
            if rel.startswith(MEDIA + "/"):
                entry_tokens[value] = add_ref(value, role)        # an image a bundle brought
                continue
            if _cached(rel):
                entry_tokens[value] = (STATE_TOKEN if token == "state" else DERIVED_TOKEN) + "/" + rel
                continue
            is_table = not _derived_name(rel) and (rel.lower().endswith(TABLE_SUFFIXES)
                                                  or role.startswith("dataset"))
            if is_table:
                if include["tables"]:
                    tables[rel] = value
                    entry_tokens[value] = f"{STATE_TOKEN}/{rel}"
                else:
                    entry_tokens[value] = add_ref(value, role)
            else:
                if include["derived"]:
                    derived[rel] = value
                    entry_tokens[value] = f"{DERIVED_TOKEN}/{rel}"
                else:
                    entry_tokens[value] = add_ref(value, role, derived_flag=True)
            continue
        if _exists(value) or value in recorded_for or value in known:
            derived_flag = value.endswith(sources.PYRAMID_ENDING) or _derived_name(Path(value).name)
            entry_tokens[value] = add_ref(value, role, derived_flag=derived_flag)

    for kind, binding in (entry.get("resources") or {}).items():
        if isinstance(binding, dict) and binding.get("provider") == "node":
            refs.append({"token": None, "role": kind, "kind": "node",
                         "identity": {"node": binding.get("node"), "resource_id": binding.get("resource_id"),
                                      "fingerprint": binding.get("fingerprint")},
                         "included": None, "derived": False, "size": (binding.get("fingerprint") or {}).get("size"),
                         "hints": {}, "where": "node"})

    # The project's folder: its own work, the tables it names, its caches out.
    # A file the entry names that is not carried here (a derived file, or a
    # table left out) is not "state" either: it is carried as itself or as a
    # ref, never twice.
    elsewhere = [os.path.normcase(os.path.abspath(value))
                 for value in list(derived.values()) + list(ref_by_path)]

    def handled(full):
        here = os.path.normcase(os.path.abspath(full))
        return any(here == other or here.startswith(other.rstrip(os.sep) + os.sep) for other in elsewhere)

    def under_table(rel):
        return any(rel == table or rel.startswith(table + "/") for table in tables)

    def add(place, rel, full, size, mtime_ns, kind):
        record = {"path": f"{base}{place}/{rel}", "size": size, "sha256": None, "kind": kind}
        files.append(record)
        if kind in ("state", "table"):
            stats.append((f"{place}/{rel}", size, mtime_ns))
        snapshot = kind == "state" and (rel == f"{name}.db" or (rel.endswith((".db", ".sqlite"))
                                                                and _is_sqlite(Path(full))))
        members.append(Member(record["path"], kind, size, source=full, project=pid, record=record,
                              snapshot=snapshot, mtime_ns=mtime_ns))

    carried = set()
    for place, folder in places:
        if place != "state":
            continue
        for rel, full, size, mtime_ns in _walk(folder, lambda rel: _cached(rel) or _never(rel)):
            if rel in carried:
                continue
            if _cached(rel):
                caches["count"] += 1
                caches["size"] += size
                continue
            if _never(rel) or rel.startswith(MEDIA + "/") or handled(full):
                continue
            carried.add(rel)
            add("state", rel, full, size, mtime_ns, "table" if under_table(rel) else "state")

    def files_of(value):
        target = Path(value)
        if target.is_dir():
            return list(_walk(target, lambda rel: False))
        size, mtime_ns = _stat(value)
        return [("", value, size, mtime_ns)]

    # Named tables and derived files the walk did not reach (in the read or
    # derived folder rather than the state folder).
    for group, place, kind in ((tables, "state", "table"), (derived, "derived", "derived")):
        for rel, value in sorted(group.items()):
            if kind == "table" and (rel in carried or any(item.startswith(rel + "/") for item in carried)):
                continue
            for sub, full, size, mtime_ns in files_of(value):
                if size is None:
                    left_out.append({"project": pid, "path": rel, "why": "not found on this computer"})
                    continue
                path_rel = f"{rel}/{sub}" if sub else rel
                carried.add(path_rel)
                add(place, path_rel, full, size, mtime_ns, kind)

    # The user's images, when asked for: keyed by identity, once per bundle.
    media = []
    if include["images"] or include["derived"]:
        for ref in refs:
            if ref["kind"] not in ("file", "folder") or not ref["identity"]:
                continue
            if ref["derived"] and not include["derived"]:
                continue
            if not ref["derived"] and not include["images"]:
                continue
            source = ref["hints"]["absolute"]
            if not _exists(source):
                continue
            key = hashlib.sha256(json.dumps({k: ref["identity"].get(k) for k in
                                             ("kind", "name", "size", "head", "tail", "listing")},
                                            sort_keys=True).encode("utf-8")).hexdigest()[:16]
            leaf = _safe_name(Path(source).name)
            prefix = f"{MEDIA}/{key}/{leaf}"
            ref["included"] = prefix
            if media_keys is not None and prefix in media_keys:
                continue
            if media_keys is not None:
                media_keys.add(prefix)
            if ref["kind"] == "folder":
                for sub, full, size, mtime_ns in _walk(Path(source), lambda r: False):
                    record = {"path": f"{prefix}/{sub}", "size": size, "sha256": None}
                    media.append(record)
                    members.append(Member(record["path"], "media", size, source=full, record=record))
            else:
                size = _stat(source)[0] or 0
                record = {"path": prefix, "size": size, "sha256": None}
                media.append(record)
                members.append(Member(prefix, "media", size, source=source, record=record))

    tokenised = _replace_strings({key: value for key, value in entry.items()
                                  if key not in ("sources", "origin", "bundleRevision")},
                                 lambda text: entry_tokens.get(text, text))
    entry_text = json.dumps(tokenised, indent=1, sort_keys=True, ensure_ascii=False).encode("utf-8")
    entry_record = {"path": f"{base}entry.json", "size": len(entry_text), "sha256": None}
    members.insert(0, Member(entry_record["path"], "entry", len(entry_text), project=pid,
                             record=entry_record, load=lambda data=entry_text: data))
    record = (entry.get("bundleRevision") or {}) if isinstance(entry.get("bundleRevision"), dict) else {}
    origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
    project = {"id": pid, "name": name, "title": entry.get("title"), "revision": None,
               "parent": record.get("id") or origin.get("revision"),
               "entry": entry_record, "files": files,
               "refs": [{key: value for key, value in ref.items() if key not in ("where", "size")}
                        for ref in refs]}
    stamp = _stamp(tokenised, stats)
    return {"project": project, "members": members, "entry": tokenised, "refs": refs, "media": media,
            "left_out": left_out, "stamp": stamp, "previous": record, "caches": caches}


def _stat(path) -> tuple:
    try:
        info = os.stat(path)
        return info.st_size, info.st_mtime_ns
    except OSError:
        return None, 0


def _captures_db() -> Path:
    return paths.captures_root() / "captures.db"


def _read_captures(names: Iterable[str]) -> list:
    """The captures (and bookmarks) of these projects, without their pictures:
    rows of the figure builder's bin (`.captures/captures.db`)."""
    path = _captures_db()
    names = list(names)
    if not names or not path.is_file():
        return []
    try:
        connection = _read_only(path)
    except sqlite3.Error:
        return []
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(captures)")}
        if "datasource" not in columns:
            return []
        marks = ",".join("?" for _ in names)
        rows = connection.execute(
            f"SELECT *, length(preview) AS preview_size FROM captures WHERE datasource IN ({marks}) "
            "ORDER BY created_at", names).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    out = []
    for row in rows:
        data = {key: row[key] for key in row.keys() if key not in ("preview",)}
        out.append(data)
    return out


def _capture_preview(capture_id: str) -> bytes:
    connection = _read_only(_captures_db())
    try:
        row = connection.execute("SELECT preview FROM captures WHERE capture_id = ?", (capture_id,)).fetchone()
    finally:
        connection.close()
    return bytes(row[0]) if row and row[0] is not None else b""


def _read_figures(names: set) -> list:
    """Figures (`.figures/<id>/figure.db`) whose images all come from projects
    in `names`: [{id, title, folder, datasources}]."""
    root = paths.figures_root()
    if not names or not root.is_dir():
        return []
    out = []
    for folder in sorted(root.iterdir()):
        if not _FIGURE_ID.match(folder.name) or not (folder / "figure.db").is_file():
            continue
        try:
            connection = _read_only(folder / "figure.db")
            try:
                row = connection.execute("SELECT json FROM document WHERE id = 1").fetchone()
            finally:
                connection.close()
            document = json.loads(row[0]) if row else None
        except (sqlite3.Error, ValueError, TypeError):
            continue
        if not isinstance(document, dict):
            continue
        used = {source.get("datasource") for source in (document.get("sources") or {}).values()
                if isinstance(source, dict) and source.get("kind", "plexora_project") == "plexora_project"
                and source.get("datasource")}
        if used and used <= names:
            out.append({"id": folder.name, "title": document.get("title") or folder.name,
                        "folder": folder, "datasources": sorted(used)})
    return out


def plan(ids: Iterable[str] = (), include=None, *, dest=None, dataset: Optional[str] = None,
         identities: bool = True) -> Plan:
    """What exporting these projects (ids or names, or a dataset's) writes.

    Stats files and never reads them, except the 2 MiB a ref's identity takes
    when the project has not recorded it (`identities`; the size estimate
    passes False). `dest` is where the file will be saved, for each image's
    path relative to it (`relative_to_bundle`)."""
    include = _include(include)
    names = resolve_projects(ids, dataset=dataset)
    locals_ = [_local(name) for name in names]
    by_name = {local.name: local for local in locals_}
    manifest = {"format": FORMAT,
                "bundle": {"id": uuid.uuid4().hex, "created": _now(), "app": _app()},
                "projects": [], "media": [], "captures": [], "datasets": [], "figures": [],
                "left_out": []}
    members, entries, stamps = [], {}, {}
    caches = {"count": 0, "size": 0}
    media_keys: set = set()
    refs_of = {}
    for local in locals_:
        part = _project_plan(local, include, dest=dest, identities=identities, media_keys=media_keys)
        manifest["projects"].append(part["project"])
        manifest["media"] += part["media"]
        manifest["left_out"] += part["left_out"]
        members += part["members"]
        entries[local.id] = part["entry"]
        stamps[local.id] = {"stamp": part["stamp"], "previous": part["previous"]}
        refs_of[local.id] = part["refs"]
        caches["count"] += part["caches"]["count"]
        caches["size"] += part["caches"]["size"]

    # Datasets: the bundled projects' own, with the bundled members only.
    from plexora.server.models import datasets as registry

    try:
        held = registry.membership()
    except Exception:  # noqa: BLE001 -- a grouping that cannot be read is no grouping
        held = {}
    grouped: dict = {}
    for local in locals_:
        dataset_record = held.get(local.name)
        if dataset_record is None:
            continue
        item = grouped.setdefault(dataset_record.id, {
            "id": dataset_record.id, "name": dataset_record.name, "projects": [],
            "description": dataset_record.description, "meta": dict(dataset_record.meta)})
        item["projects"].append(local.id)
    manifest["datasets"] = list(grouped.values())

    if include["captures"]:
        for local in locals_:
            rows = _read_captures([local.name])
            if not rows:
                continue
            listed, previews = [], []
            for index, row in enumerate(rows):
                row = dict(row)
                size = int(row.pop("preview_size") or 0)
                if size:
                    fmt = re.sub(r"[^a-z0-9]", "", str(row.get("format") or "webp").lower())[:5] or "webp"
                    path = f"captures/{local.id}/{index}.{fmt}"
                    record = {"path": path, "size": size, "sha256": None}
                    previews.append(record)
                    members.append(Member(path, "capture", size, project=local.id, record=record,
                                          load=lambda cid=row["capture_id"]: _capture_preview(cid)))
                    row["preview"] = path
                listed.append(row)
            text = json.dumps(listed, indent=1, ensure_ascii=False).encode("utf-8")
            record = {"project": local.id, "path": f"captures/{local.id}.json", "size": len(text),
                      "sha256": None, "count": len(listed), "previews": previews}
            manifest["captures"].append(record)
            members.append(Member(record["path"], "capture", len(text), project=local.id, record=record,
                                  load=lambda data=text: data))

    if include["figures"]:
        ids_of = {local.name: local.id for local in locals_}
        for figure in _read_figures(set(by_name)):
            files = []
            for rel, full, size, mtime_ns in _walk(figure["folder"],
                                                    lambda rel: rel.startswith("exports/") or _cached(rel)):
                if _cached(rel):
                    continue
                path = f"figures/{figure['id']}/{rel}"
                record = {"path": path, "size": size, "sha256": None}
                files.append(record)
                members.append(Member(path, "figure", size, source=full, record=record,
                                      snapshot=rel == "figure.db", mtime_ns=mtime_ns))
            manifest["figures"].append({"id": figure["id"], "title": figure["title"],
                                        "projects": [ids_of[name] for name in figure["datasources"]],
                                        "files": files})
    return Plan(manifest=manifest, members=members, entries=entries, projects=locals_, include=include,
                stamps=stamps, caches=caches, refs=refs_of)


def estimate(ids: Iterable[str] = (), include=None, *, dataset: Optional[str] = None) -> dict:
    """Sizes for the export dialog, from stat calls alone: each part's size
    and count whether or not it is on (so the dialog can show them all), each
    image with where it is, and the total of what `include` carries."""
    chosen = _include(include)
    built = plan(ids, {key: True for key in INCLUDE}, dataset=dataset, identities=False)
    rows = {key: {"size": 0, "count": 0} for key in ("state", "tables", "captures", "figures",
                                                      "images", "derived")}
    rows["images"]["items"], rows["derived"]["items"] = [], []
    for member in built.members:
        key = {"entry": "state", "state": "state", "table": "tables", "capture": "captures",
               "figure": "figures", "derived": "derived"}.get(member.kind)
        if key is None:
            continue
        rows[key]["size"] += member.size
        rows[key]["count"] += 1
    for pid, refs in built.refs.items():
        name = next(local.name for local in built.projects if local.id == pid)
        for ref in refs:
            key = "derived" if ref["derived"] else "images"
            absolute = (ref.get("hints") or {}).get("absolute")
            item = {"project": name, "role": ref["role"], "kind": ref["kind"], "where": ref.get("where"),
                    "size": ref.get("size"),
                    "name": Path(absolute).name if absolute else (ref.get("identity") or {}).get("resource_id"),
                    "path": absolute, "can_include": ref.get("where") in ("here", "shared")}
            if ref["kind"] == "node":
                item["node"] = (ref.get("identity") or {}).get("node")
            rows[key]["items"].append(item)
            if item["can_include"]:
                rows[key]["size"] += int(ref.get("size") or 0)
                rows[key]["count"] += 1
    total = rows["state"]["size"]
    for key, row in (("tables", "tables"), ("captures", "captures"), ("figures", "figures"),
                     ("images", "images"), ("derived", "derived")):
        if chosen[key]:
            total += rows[row]["size"]
    return {"projects": [{"id": local.id, "name": local.name, "title": local.entry.get("title"),
                          "shared": local.shared} for local in built.projects],
            "rows": rows, "include": chosen, "total": total, "caches": built.caches,
            "datasets": [{"id": item["id"], "name": item["name"]} for item in built.manifest["datasets"]],
            "figures": [{"id": item["id"], "title": item["title"]} for item in built.manifest["figures"]],
            "left_out": built.manifest["left_out"]}


def plan_for_page(ids: Iterable[str] = (), include=None, *, dataset: Optional[str] = None,
                  work_dir=None) -> dict:
    """The website's seam: the plan as JSON, its made members (entries, the
    database snapshots, captures) written into `work_dir`, so the page's
    worker streams every member's bytes from a path in this Python's
    filesystem and asks `finish_manifest` for the manifest at the end."""
    built = plan(ids, include, dataset=dataset)
    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="plexora-bundle-"))
    out = built.to_json(work)
    out["work_dir"] = str(work)
    out["stamps"] = {pid: value["stamp"] for pid, value in built.stamps.items()}
    # The manifest's room, kept first and filled in at the end (ZipSink.start).
    out["reserve"] = _reserve(built.manifest)
    return out


def finish_manifest(manifest: dict, sums: dict, *, entries: Optional[dict] = None,
                    left_out: Optional[list] = None) -> dict:
    """The manifest with every file's sha256 (and size) from `sums`
    ({bundle_path: sha256 | {sha256, size}}), what the page could not read
    (`left_out`: [{path, why}]) taken out of it, and -- given the plan's
    tokenised `entries` -- each project's revision and parent, as `export`
    sets them. What the page's worker writes first, once it has streamed the
    rest."""
    out = json.loads(json.dumps(manifest))
    for gone in left_out or ():
        path = str((gone or {}).get("path") or "")
        record = next((record for record in _records(out) if record.get("path") == path), None)
        if record is None:
            continue
        _drop_record(out, record)
        parts = path.split("/")
        project = parts[1] if parts[0] == "projects" and len(parts) > 1 else None
        out.setdefault("left_out", []).append({"project": project, "path": path,
                                               "why": str(gone.get("why") or "not read")})
    for record in _records(out):
        found = sums.get(record["path"])
        if isinstance(found, dict):
            record["sha256"] = found.get("sha256")
            if found.get("size") is not None:
                record["size"] = int(found["size"])
        elif isinstance(found, str):
            record["sha256"] = found
    for project in out.get("projects") or ():
        entry = (entries or {}).get(project.get("id"))
        if not isinstance(entry, dict):
            continue
        own = [(record["path"].split("/", 2)[2], record.get("sha256")) for record in project.get("files") or ()
               if record.get("kind") in ("state", "table")]
        project["revision"] = revision_of(entry, own)
        # Exported again unchanged: the same revision, so the same parent.
        try:
            previous = _local(str(project.get("name") or ""), assign=False).entry.get("bundleRevision")
        except (KeyError, ValueError):
            previous = None
        if isinstance(previous, dict) and previous.get("id") == project["revision"]:
            project["parent"] = previous.get("parent")
    return out


def record_export(manifest: dict, stamps: dict) -> None:
    """Note in each exported project's entry the revision it went out at (as
    `export` does once its file is written), so a later version coming back
    is an Update. `stamps`: the plan's {project id: stat stamp}."""
    for project in manifest.get("projects") or ():
        if not project.get("revision") or project.get("id") not in (stamps or {}):
            continue
        try:
            local = _local(str(project.get("name") or ""), assign=False)
        except (KeyError, ValueError):
            continue
        if local.id == project.get("id"):
            _record_revision(local, project["revision"], project.get("parent"), stamps[project["id"]])


def _records(manifest: dict):
    """Every file record of a manifest: what validation accepts."""
    for project in manifest.get("projects") or ():
        if isinstance(project.get("entry"), dict):
            yield project["entry"]
        yield from project.get("files") or ()
    yield from manifest.get("media") or ()
    for capture in manifest.get("captures") or ():
        yield capture
        yield from capture.get("previews") or ()
    for figure in manifest.get("figures") or ():
        yield from figure.get("files") or ()


# -- sinks -------------------------------------------------------------------


def _choose_method(bundle_path: str, source: Optional[Path], data: Optional[bytes]) -> tuple:
    """(compress_type, level): stored for what is compressed already or does
    not compress (its first SAMPLE bytes deflated at level 1 keep more than
    INCOMPRESSIBLE of their size), else deflated at level 6."""
    if bundle_path.lower().endswith(STORED_SUFFIXES):
        return zipfile.ZIP_STORED, None
    if data is not None:
        sample = data[:SAMPLE]
    else:
        try:
            with open(source, "rb") as handle:
                sample = handle.read(SAMPLE)
        except OSError:
            return zipfile.ZIP_DEFLATED, 6
    if len(sample) < 512:
        return zipfile.ZIP_DEFLATED, 6
    packer = zlib.compressobj(1, zlib.DEFLATED, -15)
    packed = len(packer.compress(sample)) + len(packer.flush())
    if packed > INCOMPRESSIBLE * len(sample):
        return zipfile.ZIP_STORED, None
    return zipfile.ZIP_DEFLATED, 6


def _level(info: zipfile.ZipInfo, level: Optional[int]) -> None:
    """A member's deflate level: `compress_level` from Python 3.13,
    `_compresslevel` before (the desktop app's 3.12)."""
    if level is None:
        return
    try:
        info.compress_level = level
    except AttributeError:
        info._compresslevel = level


def _zip_time(mtime: Optional[float]) -> tuple:
    stamp = time.localtime(mtime if mtime else time.time())
    return max(stamp[:6], (1980, 1, 1, 0, 0, 0))


class _Progress:
    def __init__(self, total, progress, cancel):
        self.total = max(int(total or 0), 1)
        self.done = 0
        self.progress = progress
        self.cancel = cancel

    def check(self):
        if self.cancel is not None and (self.cancel.is_set() if hasattr(self.cancel, "is_set") else self.cancel()):
            raise Cancelled("Stopped.")

    def advance(self, count):
        self.done += count
        if self.progress is not None:
            self.progress(min(self.done, self.total), self.total)


class ZipSink:
    """Writes a bundle as a ZIP64 file: into `<dest>.part`, 1 MiB at a time,
    each member's sha256 and CRC taken as it streams, cancel checked between
    chunks; the manifest first (stored, its room kept and filled in at the
    end), then the central directory read back (`verify`), fsync, and the
    `.part` renamed over `dest`."""

    def __init__(self, dest, *, total=0, progress=None, cancel=None):
        self.dest = Path(dest)
        self.part = self.dest.with_name(self.dest.name + ".part")
        self.part.parent.mkdir(parents=True, exist_ok=True)
        self._progress = _Progress(total, progress, cancel)
        self._zip = zipfile.ZipFile(self.part, "w", allowZip64=True)
        self._written: dict = {}
        self._manifest_info = None
        self._reserved = 0

    def start(self, reserve: int) -> None:
        """Keep the first member's room for the manifest."""
        info = zipfile.ZipInfo(MANIFEST, date_time=_zip_time(None))
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o644 << 16
        self._reserved = int(reserve)
        self._zip.writestr(info, b" " * self._reserved)
        self._manifest_info = info

    def add_file(self, bundle_path: str, source, *, mtime=None) -> dict:
        source = Path(source)
        size = source.stat().st_size
        method, level = _choose_method(bundle_path, source, None)
        info = zipfile.ZipInfo(bundle_path, date_time=_zip_time(mtime or source.stat().st_mtime))
        info.compress_type = method
        _level(info, level)
        info.external_attr = 0o644 << 16
        info.file_size = size
        digest, crc, written = hashlib.sha256(), 0, 0
        with source.open("rb") as handle, \
                self._zip.open(info, "w", force_zip64=size >= ZIP64_FROM) as out:
            while True:
                self._progress.check()
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                crc = zlib.crc32(chunk, crc)
                out.write(chunk)
                written += len(chunk)
                self._progress.advance(len(chunk))
        record = {"size": written, "sha256": digest.hexdigest(), "crc": crc & 0xFFFFFFFF}
        self._written[bundle_path] = record
        return record

    def add_bytes(self, bundle_path: str, data: bytes) -> dict:
        self._progress.check()
        method, level = _choose_method(bundle_path, None, data)
        info = zipfile.ZipInfo(bundle_path, date_time=_zip_time(None))
        info.compress_type = method
        _level(info, level)
        info.external_attr = 0o644 << 16
        self._zip.writestr(info, data)
        self._progress.advance(len(data))
        record = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                  "crc": zlib.crc32(data) & 0xFFFFFFFF}
        self._written[bundle_path] = record
        return record

    def finish(self, manifest: dict) -> Path:
        """Fill in the manifest, close, verify, fsync and rename."""
        text = json.dumps(manifest, indent=1, ensure_ascii=False).encode("utf-8")
        if len(text) > self._reserved:
            raise BundleError("The project file's manifest outgrew its room.")
        text += b" " * (self._reserved - len(text))
        info = self._manifest_info
        handle = self._zip.fp
        handle.flush()
        end = handle.tell()
        handle.seek(info.header_offset + 26)
        name_length, extra_length = int.from_bytes(handle.read(2), "little"), int.from_bytes(handle.read(2), "little")
        handle.seek(info.header_offset + 30 + name_length + extra_length)
        handle.write(text)
        info.CRC = zlib.crc32(text) & 0xFFFFFFFF
        handle.seek(info.header_offset + 14)
        handle.write(info.CRC.to_bytes(4, "little"))
        handle.seek(end)
        self._written[MANIFEST] = {"size": len(text), "crc": info.CRC}
        self._zip.close()
        self.verify()
        with open(self.part, "rb") as stored:
            os.fsync(stored.fileno())
        os.replace(self.part, self.dest)
        return self.dest

    def verify(self) -> None:
        """The central directory read back: every member there, with the size
        and CRC it was written with."""
        with zipfile.ZipFile(self.part) as bundle:
            listed = {info.filename: info for info in bundle.infolist()}
        for name, record in self._written.items():
            info = listed.get(name)
            if info is None or info.file_size != record["size"] or (info.CRC & 0xFFFFFFFF) != record["crc"]:
                raise BundleError(f"The project file did not write {name} correctly.")

    def abort(self) -> None:
        try:
            self._zip.close()
        except Exception:  # noqa: BLE001 -- closing a broken file on the way out
            pass
        self.part.unlink(missing_ok=True)


class FolderSink:
    """Writes a bundle as a folder of the same layout ("Make a copy")."""

    def __init__(self, dest, *, total=0, progress=None, cancel=None):
        self.dest = Path(dest)
        self.part = self.dest.with_name(self.dest.name + ".part")
        shutil.rmtree(self.part, ignore_errors=True)
        self.part.mkdir(parents=True)
        self._progress = _Progress(total, progress, cancel)

    def start(self, reserve: int) -> None:
        pass

    def _target(self, bundle_path):
        target = self.part.joinpath(*bundle_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def add_file(self, bundle_path: str, source, *, mtime=None) -> dict:
        digest, written = hashlib.sha256(), 0
        with Path(source).open("rb") as handle, self._target(bundle_path).open("wb") as out:
            while True:
                self._progress.check()
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
                written += len(chunk)
                self._progress.advance(len(chunk))
        return {"size": written, "sha256": digest.hexdigest()}

    def add_bytes(self, bundle_path: str, data: bytes) -> dict:
        self._progress.check()
        self._target(bundle_path).write_bytes(data)
        self._progress.advance(len(data))
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def finish(self, manifest: dict) -> Path:
        (self.part / MANIFEST).write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
        shutil.rmtree(self.dest, ignore_errors=True)
        os.replace(self.part, self.dest)
        return self.dest

    def abort(self) -> None:
        shutil.rmtree(self.part, ignore_errors=True)


def _reserve(manifest: dict) -> int:
    """Room for the finished manifest: the skeleton with every sha256 and
    revision at full length, and slack for sizes that move while packing."""
    sized = json.loads(json.dumps(manifest))
    for record in _records(sized):
        record["sha256"] = "0" * 64
        record["size"] = 10 ** 15
    for project in sized.get("projects") or ():
        project["revision"] = "0" * 64
        project["parent"] = "0" * 64
    return len(json.dumps(sized, indent=1, ensure_ascii=False).encode("utf-8")) + 16384


def export(ids: Iterable[str] = (), dest=None, *, include=None, dataset: Optional[str] = None,
           sink=None, progress=None, cancel=None, stage=None, record: bool = True) -> dict:
    """Write these projects to `dest` (a `.plexora` file, or `sink`).
    Returns `{path, size, manifest}`.

    `progress(done, total)` in bytes, `cancel` an Event (or a callable), and
    `stage(key)` told "snapshot", "pack" and "verify" as they begin. A
    cancel removes the `.part` file and raises Cancelled. With `record`, each
    project's entry notes the revision it was exported at (`bundleRevision`),
    so the next import of a later version of it is an Update."""
    stage = stage or (lambda key: None)
    built = plan(ids, include, dest=dest, dataset=dataset)
    work = Path(tempfile.mkdtemp(prefix="plexora-bundle-"))
    target = None
    try:
        stage("snapshot")
        built.materialise(work)
        stage("pack")
        target = sink or ZipSink(dest, total=built.total, progress=progress, cancel=cancel)
        manifest = built.manifest
        target.start(_reserve(manifest))
        for member in built.members:
            if member.load is not None:
                written = target.add_bytes(member.bundle_path, member.load())
            else:
                try:
                    written = target.add_file(member.bundle_path, member.source)
                except FileNotFoundError:
                    manifest["left_out"].append({"project": member.project, "path": member.bundle_path,
                                                 "why": "gone before it was packed"})
                    _drop_record(manifest, member.record)
                    continue
            if member.record is not None:
                member.record["size"] = written["size"]
                member.record["sha256"] = written["sha256"]
        for project in manifest["projects"]:
            own = [(record["path"].split("/", 2)[2], record["sha256"]) for record in project["files"]
                   if record.get("kind") in ("state", "table")]
            project["revision"] = revision_of(built.entries[project["id"]], own)
            previous = built.stamps[project["id"]]["previous"] or {}
            if previous.get("id") == project["revision"]:
                project["parent"] = previous.get("parent")
        stage("verify")
        path = target.finish(manifest)
    except BaseException:
        if target is not None:
            target.abort()
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if record:
        for local, project in zip(built.projects, manifest["projects"]):
            _record_revision(local, project["revision"], project["parent"], built.stamps[local.id]["stamp"])
    size = path.stat().st_size if path.is_file() else None
    return {"path": str(path), "size": size, "manifest": manifest}


def _drop_record(manifest: dict, record) -> None:
    if record is None:
        return
    for project in manifest.get("projects") or ():
        if record in project.get("files", []):
            project["files"].remove(record)
    for key in ("media",):
        if record in manifest.get(key, []):
            manifest[key].remove(record)
    for figure in manifest.get("figures") or ():
        if record in figure.get("files", []):
            figure["files"].remove(record)


def _record_revision(local: _Local, revision: str, parent: Optional[str], stamp: str) -> None:
    """Note in the project's entry which revision it is at, and its stat stamp
    then; skipped for a project nobody here can write."""
    if not local.writable:
        return
    module = _project_module()
    try:
        with module.config_transaction():
            config = module.read_config(paths.config_path(local.root))
            current = config.get(local.name)
            if not isinstance(current, dict):
                return
            current["bundleRevision"] = {"id": revision, "parent": parent, "stamp": stamp, "at": _now()}
            module.write_config(paths.config_path(local.root), config)
    except OSError:
        return


# -- sources -----------------------------------------------------------------


def _synthesise_1(manifest_1: dict, names: list, sizes: dict) -> dict:
    """A FORMAT 1 manifest as FORMAT 2 describes it."""
    if manifest_1.get("format") != FORMAT_1 or not isinstance(manifest_1.get("entry"), dict) \
            or not manifest_1.get("name"):
        raise BundleError("This Plexora project file is from a version this one cannot read.")
    entry = manifest_1["entry"]
    name = str(manifest_1["name"])
    pid = entry.get("id") if isinstance(entry.get("id"), str) and _ID.match(entry["id"]) else \
        hashlib.sha256(f"{name}\n{manifest_1.get('exported')}".encode("utf-8")).hexdigest()[:32]
    files = [{"path": member, "size": sizes.get(member, 0), "sha256": None, "kind": "state",
              "rel": member[len(FOLDER_1):]}
             for member in names if member.startswith(FOLDER_1) and not member.endswith("/")]
    return {"format": FORMAT_1, "bundle": {"id": pid, "created": manifest_1.get("exported"),
                                           "app": {"kind": manifest_1.get("app")}},
            "projects": [{"id": pid, "name": name, "title": manifest_1.get("title"), "revision": None,
                          "parent": None, "entry": None, "files": files, "refs": []}],
            "media": [], "captures": [], "datasets": [], "figures": [],
            "left_out": manifest_1.get("left_out") or [], "_entry_1": entry}


class ZipSource:
    """A `.plexora` file read by random access through its central directory:
    a member at a time, never the whole file."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self._zip = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError, ValueError) as error:
            raise BundleError("This is not a Plexora project file.") from error
        self._infos = {info.filename: info for info in self._zip.infolist()}
        self._manifest = None

    def members(self) -> set:
        return set(self._infos)

    def info(self, name: str) -> Optional[dict]:
        info = self._infos.get(name)
        if info is None:
            return None
        mode = (info.external_attr >> 16) & 0o170000
        return {"size": info.file_size, "compressed": info.compress_size,
                "stored": info.compress_type == zipfile.ZIP_STORED, "link": mode == stat_module.S_IFLNK}

    def open(self, name: str):
        return self._zip.open(self._infos[name])

    def read(self, name: str, limit: int = 64 << 20) -> bytes:
        info = self._infos.get(name)
        if info is None:
            raise BundleError("This Plexora project file is incomplete.")
        if info.file_size > limit:
            raise BundleError("This Plexora project file's manifest is too large.")
        with self._zip.open(info) as handle:
            return handle.read(limit + 1)

    def manifest(self) -> dict:
        if self._manifest is None:
            try:
                if MANIFEST in self._infos:
                    manifest = json.loads(self.read(MANIFEST).decode("utf-8"))
                    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                        raise BundleError("This Plexora project file is from a version this one cannot read.")
                elif MANIFEST_1 in self._infos:
                    manifest = _synthesise_1(json.loads(self.read(MANIFEST_1).decode("utf-8")),
                                             list(self._infos),
                                             {name: info.file_size for name, info in self._infos.items()})
                else:
                    raise BundleError("This is not a Plexora project file.")
            except (ValueError, UnicodeDecodeError, zipfile.BadZipFile) as error:
                if isinstance(error, BundleError):
                    raise
                raise BundleError("This is not a Plexora project file.") from error
            self._manifest = manifest
        return self._manifest

    def entry(self, project: dict) -> dict:
        manifest = self.manifest()
        if manifest.get("format") == FORMAT_1:
            return dict(manifest["_entry_1"])
        record = project.get("entry") or {}
        if not _safe_member(record.get("path", "")):
            raise BundleError(_OUTSIDE)
        try:
            entry = json.loads(self.read(record["path"]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise BundleError("This Plexora project file is damaged.") from error
        if not isinstance(entry, dict):
            raise BundleError("This Plexora project file is damaged.")
        return entry

    def close(self) -> None:
        self._zip.close()


class FolderSource:
    """A bundle laid out as a folder (what FolderSink writes). `owned`: the
    folder is this import's own to empty, so its files are moved, not copied."""

    def __init__(self, path, *, owned: bool = False):
        self.path = Path(path)
        self.owned = owned
        if not (self.path / MANIFEST).is_file():
            raise BundleError("This is not a Plexora project folder.")
        self._manifest = None

    def members(self) -> set:
        return {rel for rel, _full, _size, _m in _walk(self.path, lambda rel: False)}

    def _file(self, name):
        return self.path.joinpath(*name.split("/"))

    def info(self, name: str) -> Optional[dict]:
        target = self._file(name)
        try:
            info = os.lstat(target)
        except OSError:
            return None
        return {"size": info.st_size, "compressed": info.st_size, "stored": True,
                "link": stat_module.S_ISLNK(info.st_mode)}

    def open(self, name: str):
        return self._file(name).open("rb")

    def read(self, name: str, limit: int = 64 << 20) -> bytes:
        with self.open(name) as handle:
            return handle.read(limit + 1)

    def manifest(self) -> dict:
        if self._manifest is None:
            try:
                manifest = json.loads((self.path / MANIFEST).read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise BundleError("This is not a Plexora project folder.") from error
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise BundleError("This Plexora project file is from a version this one cannot read.")
            self._manifest = manifest
        return self._manifest

    entry = ZipSource.entry

    def close(self) -> None:
        pass


class ManifestSource:
    """A manifest alone, for a preview of a bundle whose bytes are elsewhere
    (the website's page reads the central directory itself)."""

    def __init__(self, manifest: dict, path=None):
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise BundleError("This Plexora project file is from a version this one cannot read.")
        self._manifest = manifest
        self.path = Path(path) if path else None

    def manifest(self) -> dict:
        return self._manifest

    def close(self) -> None:
        pass


def open_source(source):
    """A Source for a path (a `.plexora` file or a bundle folder), a Source,
    or a manifest dict."""
    if isinstance(source, (ZipSource, FolderSource, ManifestSource)):
        return source
    if isinstance(source, dict):
        return ManifestSource(source)
    path = Path(source)
    if path.is_dir():
        return FolderSource(path)
    if not path.is_file():
        raise BundleError("There is no Plexora project file there.")
    return ZipSource(path)


# -- import: deciding --------------------------------------------------------


def _target_root(into=None, name=None) -> Path:
    """Where imported projects are registered: the Location `into` names
    (server/models/library.py: one of the user's own, mounted and writable),
    else where new projects go (`paths.new_projects_root`), never the data
    root by default. A build without Locations ignores `into`."""
    if into:
        try:
            from plexora.server.models import library  # noqa: PLC0415 -- optional, newer builds
        except Exception:  # noqa: BLE001
            library = None
        choose = getattr(library, "root_for_new_projects", None)
        if callable(choose):
            try:
                root = choose(into)
            except ValueError as error:  # LocationError, UnknownLocation
                raise BundleError(str(error) or "There is no such location.") from error
            if root is not None:
                return Path(root)
    return paths.new_projects_root(name)


def _occupied(folder: Path, ignore=frozenset()) -> bool:
    """Whether `folder` holds anything -- but the files in `ignore`, which
    this import's page put there itself (`placed`)."""
    try:
        if not ignore:
            return folder.is_dir() and any(folder.iterdir())
        if not folder.is_dir():
            return False
        return any(not child.is_dir() and str(child) not in ignore for child in folder.rglob("*"))
    except OSError:
        return True


def _free_name(wanted: str, root: Path, taken: set, ignore=frozenset()) -> str:
    """`wanted`, or the next `wanted_<n>` that no registry holds, no other
    import of this file takes, and whose folder is free in `root` (but for
    the files of `ignore`, this import's own)."""
    wanted = _safe_name(wanted)

    def free(candidate):
        return candidate not in taken and not _occupied(Path(root) / candidate, ignore)

    if free(wanted):
        return wanted
    for number in range(2, 10000):
        candidate = f"{wanted}_{number}"
        if free(candidate):
            return candidate
    raise BundleError(f"Too many projects called {wanted!r} already.")


def _local_projects() -> dict:
    """{id: {name, root, entry}} of the user's own projects with an id."""
    module = _project_module()
    out = {}
    for root in paths.owned_roots():
        for name, entry in module.read_config(paths.config_path(root)).items():
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                out.setdefault(entry["id"], {"name": name, "root": Path(root), "entry": entry})
    return out


def local_revision(name: str) -> Optional[str]:
    """The revision project `name` is at, when it has not moved since one was
    recorded (exported or imported); None when it has, or none was."""
    try:
        local = _local(name, assign=False)
    except KeyError:
        return None
    recorded = local.entry.get("bundleRevision")
    if not isinstance(recorded, dict) or not recorded.get("id"):
        return None
    part = _project_plan(local, INCLUDE_DEFAULT, identities=False)
    return recorded["id"] if part["stamp"] == recorded.get("stamp") else None


STATUSES = ("new", "renamed", "already_here", "update", "conflict")
#: What each status does unless the caller chooses otherwise.
DEFAULT_ACTION = {"new": "import", "renamed": "import", "already_here": "skip", "update": "update",
                  "conflict": "keep_both"}
ACTIONS = ("import", "skip", "update", "replace", "keep_both")


def _decide(manifest: dict, root: Path, choices=None, *, rename: Optional[dict] = None,
            force: Optional[str] = None, placed: Optional[dict] = None) -> list:
    """Per project: status, action, target name and the local project it
    meets (by id). Files the page `placed` for this import do not make a
    folder taken: they are in the folder `placements` named for it."""
    module = _project_module()
    choices = choices or {}
    rename = rename or {}
    ignore = frozenset(str(Path(path)) for path in (placed or {}).values())
    local = _local_projects()
    taken = set(module.Project.load_all())
    decided = []
    for project in manifest.get("projects") or ():
        pid = str(project.get("id") or "")
        if not _ID.match(pid):
            raise BundleError("This Plexora project file is damaged.")
        name = _safe_name(rename.get(pid) or project.get("name") or "project")
        mine = local.get(pid)
        status, local_name, local_rev = "new", None, None
        if mine is not None:
            local_name = mine["name"]
            local_rev = local_revision(local_name)
            if local_rev and local_rev == project.get("revision"):
                status = "already_here"
            elif local_rev and local_rev == project.get("parent"):
                status = "update"
            else:
                status = "conflict"
        choice = force or choices.get(pid)
        action = choice if choice in ACTIONS else DEFAULT_ACTION[status]
        if mine is None and action in ("update", "replace"):
            action = "import"
        if action in ("update", "replace"):
            target = local_name
        elif action == "skip":
            target = local_name or name
        else:
            target = _free_name(name, root, taken, ignore)
            if mine is None and target != name:
                status = "renamed" if status == "new" else status
            taken.add(target)
        decided.append({"id": pid, "project": project, "status": status, "action": action,
                        "target": target, "local": local_name, "local_revision": local_rev,
                        "root": (mine["root"] if action in ("update", "replace") else root)})
    return decided


def _find_cheap(ref: dict, bundle_path, me: str, roots: list) -> Optional[str]:
    """A ref found by what costs nothing: the same path (as recorded, or as
    this computer recorded it), relative to the bundle, and the trailing parts
    of its path under the shared roots and Locations."""
    from plexora.server.utils import relink

    identity = ref.get("identity") or {}
    hints = ref.get("hints") or {}
    candidates = []
    for value in ((hints.get("at") or {}).get(me), hints.get("absolute"), hints.get("recorded")):
        if value and _pathlike(value) and not value.startswith(sources.PAGE_ROOTS):
            candidates.append(Path(value))
    relative = relink.relative_to_bundle(hints.get("relative_to_bundle"), bundle_path)
    if relative is not None:
        candidates.append(relative)
    absolute = hints.get("absolute") or hints.get("recorded")
    if absolute and roots:
        candidates += relink.tail_candidates(absolute, roots)
    for candidate in candidates:
        if not identity:
            if _exists(str(candidate)):
                return str(candidate)
            continue
        if relink._present(candidate, identity) and sources.same_file(identity, candidate):
            return str(candidate)
    return None


def _search_roots() -> list:
    roots = list(paths.shared_roots()) + [Path(loc["path"]) for loc in _locations() if loc.get("path")]
    if sys.platform == "darwin":
        try:
            roots += [path for path in Path("/Volumes").iterdir() if path.is_dir()]
        except OSError:
            pass
    return list(dict.fromkeys(roots))


def preview(source, into=None) -> dict:
    """What importing this bundle would do, from its manifest alone: per
    project its status (new, renamed, already_here, update, conflict), the
    name it would get, and its images: found here, included, to find later,
    on a node."""
    opened = open_source(source)
    try:
        return _preview(opened.manifest(), into, getattr(opened, "path", None))
    finally:
        if opened is not source:
            opened.close()


def preview_manifest(manifest: dict, into=None, *, bundle_path=None) -> dict:
    """`preview` for a manifest the page read itself (no zip here)."""
    if isinstance(manifest, dict) and manifest.get("format") == FORMAT_1:
        raise BundleError("Open this older project file itself to see what is in it.")
    return _preview(ManifestSource(manifest).manifest(), into, bundle_path)


def placements(source, choices=None, into=None, *, at_least: int = 0) -> list:
    """The website's seam for an import: the members its page writes itself,
    straight into the project store, before `apply(..., placed=...)` does the
    rest -- so a bundle's large files never pass through its Python's heap.
    `[{path, target, size, sha256}]`: each member of `at_least` bytes or more,
    and the file `apply` would move it to.

    Only projects imported into a folder of their own (import, keep both): an
    update's folder is moved aside first, so its members are unpacked here.
    Never the entry, the project's database (its rows are renamed), captures
    or figures (merged into stores of their own). An image two projects
    include goes where `apply` puts it: with the first of them."""
    opened = open_source(source)
    try:
        manifest = opened.manifest()
        if manifest.get("format") != FORMAT:
            return []
        root = _target_root(into)
        decided = _decide(manifest, root, choices)
        chosen = [item for item in decided if item["action"] != "skip"]
        _validate(opened, manifest, chosen, root)
    finally:
        if opened is not source:
            opened.close()
    media = {record["path"]: record for record in manifest.get("media") or ()}
    out, claimed = [], set()

    def add(record, target):
        if int(record.get("size") or 0) >= at_least:
            out.append({"path": record["path"], "target": str(target), "size": int(record.get("size") or 0),
                        "sha256": record.get("sha256")})

    for item in chosen:
        project, pid, target = item["project"], item["id"], item["target"]
        mine = item["action"] in ("import", "keep_both")
        state_dir = paths.project_dir(target, item["root"])
        derived_dir = paths.derived_root(target, home_root=item["root"])
        head = f"projects/{pid}/"
        for record in (project.get("files") or []) if mine else []:
            path = record["path"]
            place, _, rel = path[len(head):].partition("/") if path.startswith(head) else ("", "", "")
            if place not in ("state", "derived") or not rel:
                continue
            if place == "state" and rel == f"{project.get('name') or ''}.db":
                continue
            add(record, (state_dir if place == "state" else derived_dir).joinpath(*rel.split("/")))
        for ref in project.get("refs") or ():
            prefix = ref.get("included")
            if not prefix or prefix in claimed:
                continue
            claimed.add(prefix)
            if not mine:
                continue
            if ref.get("kind") == "folder":
                for path, record in media.items():
                    if path.startswith(prefix + "/"):
                        add(record, state_dir.joinpath(*path.split("/")))
            elif prefix in media:
                add(media[prefix], state_dir.joinpath(*prefix.split("/")))
    return out


def _preview(manifest: dict, into, bundle_path) -> dict:
    root = _target_root(into)
    decided = _decide(manifest, root)
    me = sources.app_key()
    roots = _search_roots()
    out = []
    for item in decided:
        project = item["project"]
        found = included = missing = node = 0
        for ref in project.get("refs") or ():
            if ref.get("kind") == "node":
                node += 1
            elif ref.get("included"):
                included += 1
            elif _find_cheap(ref, bundle_path, me, roots):
                found += 1
            else:
                missing += 1
        size = sum(int(record.get("size") or 0) for record in project.get("files") or ())
        out.append({"id": item["id"], "name": project.get("name"), "title": project.get("title"),
                    "status": item["status"], "action": item["action"], "target": item["target"],
                    "local": item["local"], "revision": project.get("revision"),
                    "parent": project.get("parent"), "size": size,
                    "refs": {"found": found, "included": included, "missing": missing, "node": node,
                             "nodes": sorted({(ref.get("identity") or {}).get("node") or ""
                                              for ref in project.get("refs") or ()
                                              if ref.get("kind") == "node"} - {""})}})
    needed = sum(int(record.get("size") or 0) for record in _records(manifest))
    free = _free_bytes(root)
    bundle = manifest.get("bundle") or {}
    return {"format": manifest.get("format"), "bundle": {"id": bundle.get("id"), "created": bundle.get("created"),
                                                          "app": bundle.get("app")},
            "root": str(root), "location": into, "projects": out,
            "datasets": [{"id": item.get("id"), "name": item.get("name"),
                          "projects": len(item.get("projects") or ())} for item in manifest.get("datasets") or ()],
            "figures": [{"id": item.get("id"), "title": item.get("title")} for item in manifest.get("figures") or ()],
            "captures": sum(int(item.get("count") or 0) for item in manifest.get("captures") or ()),
            "needed": needed, "free": free, "fits": free is None or needed <= FREE_SHARE * free}


def _free_bytes(root: Path) -> Optional[int]:
    probe = Path(root)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


# -- import: doing -----------------------------------------------------------


def _validate(source, manifest: dict, chosen: list, root: Path) -> list:
    """Every member this import reads, checked before one byte is written:
    listed in the manifest, a safe relative path, not a link, not a member
    that inflates past MAX_RATIO, and all of them fitting in the disk's free
    space. Returns the records to extract."""
    for record in _records(manifest):
        if not _safe_member(record.get("path", "")):
            raise BundleError(_OUTSIDE)
    for project in manifest.get("projects") or ():
        for ref in project.get("refs") or ():
            if ref.get("included") and not _safe_member(ref["included"]):
                raise BundleError(_OUTSIDE)
    for figure in manifest.get("figures") or ():
        if not _FIGURE_ID.match(str(figure.get("id") or "")):
            raise BundleError(_OUTSIDE)
    wanted = _wanted(manifest, chosen)
    present = source.members()
    total = 0
    for record in wanted:
        name = record["path"]
        if name not in present:
            raise BundleError("This Plexora project file is incomplete.")
        info = source.info(name)
        if info["link"]:
            raise BundleError("This Plexora project file holds a link, which Plexora does not unpack.")
        if info["size"] > RATIO_FLOOR and not info["stored"] and \
                (info["compressed"] <= 0 or info["size"] / info["compressed"] > MAX_RATIO):
            raise BundleError("This Plexora project file inflates far beyond its size, so it is not unpacked.")
        total += info["size"]
    free = _free_bytes(root)
    if free is not None and total > FREE_SHARE * free:
        raise BundleError(f"There is not enough free space for these projects "
                          f"({total / 1e9:.1f} GB needed, {free / 1e9:.1f} GB free).")
    return wanted


def _wanted(manifest: dict, chosen: list) -> list:
    """The records of the chosen projects, the media they include, their
    captures and the figures whose projects are all chosen."""
    ids = {item["id"] for item in chosen}
    out = []
    media_paths = set()
    for project in manifest.get("projects") or ():
        if project.get("id") not in ids:
            continue
        if isinstance(project.get("entry"), dict):
            out.append(project["entry"])
        out += project.get("files") or []
        for ref in project.get("refs") or ():
            if ref.get("included"):
                media_paths.add(ref["included"])
    for record in manifest.get("media") or ():
        if any(record["path"] == prefix or record["path"].startswith(prefix + "/") for prefix in media_paths):
            out.append(record)
    for capture in manifest.get("captures") or ():
        if capture.get("project") in ids:
            out.append(capture)
            out += capture.get("previews") or []
    for figure in manifest.get("figures") or ():
        if set(figure.get("projects") or ()) <= ids and figure.get("projects"):
            out += figure.get("files") or []
    return out


def _extract(source, record: dict, target: Path, progress: _Progress) -> None:
    """One member to `target`, its sha256 checked when the manifest has one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(source, FolderSource) and source.owned:
        os.replace(source._file(record["path"]), target)
        progress.advance(int(record.get("size") or 0))
        return
    digest = hashlib.sha256()
    with source.open(record["path"]) as handle, target.open("wb") as out:
        while True:
            progress.check()
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
            progress.advance(len(chunk))
    if record.get("sha256") and digest.hexdigest() != record["sha256"]:
        raise BundleError(f"This Plexora project file is damaged ({record['path']} does not match).")


def rename_rows(db_path, old: str, new: str) -> int:
    """Rows of every table keyed by `datasource` renamed from `old` to `new`,
    after deleting rows of any other name: a project's database keys each
    table's one row by the project's name (database_model `_SCHEMA`), so a
    file renamed without its rows opens with no channel settings, gates,
    regions or QC. Returns how many tables changed."""
    connection = sqlite3.connect(str(db_path), timeout=10)
    changed = 0
    try:
        with connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")}
                if "datasource" not in columns:
                    continue
                connection.execute(f"DELETE FROM {quoted} WHERE datasource != ?", (old,))
                cursor = connection.execute(f"UPDATE {quoted} SET datasource = ? WHERE datasource = ?", (new, old))
                changed += 1 if cursor.rowcount else 0
    finally:
        connection.close()
    return changed


def _detokenise(entry: dict, *, original: str, target: str, state_dir: Path, derived_dir: Path,
                refs: list, found: dict, me: str) -> dict:
    """The entry with its tokens replaced by this computer's paths, and a
    `sources` map holding every ref's identity and where it was found here."""
    by_token = {ref.get("token"): ref for ref in refs if ref.get("token")}

    def value_of(ref):
        hints = ref.get("hints") or {}
        return hints.get("recorded") or hints.get("absolute")

    def change(text):
        if text.startswith(STATE_TOKEN + "/"):
            return str(state_dir.joinpath(*text[len(STATE_TOKEN) + 1:].split("/")))
        if text == STATE_TOKEN:
            return str(state_dir)
        if text.startswith(DERIVED_TOKEN + "/"):
            return str(derived_dir.joinpath(*text[len(DERIVED_TOKEN) + 1:].split("/")))
        if text.startswith(f"{TILES}{NAME_TOKEN}/"):
            return f"{TILES}{target}/" + text[len(f"{TILES}{NAME_TOKEN}/"):]
        if text.startswith(f"{TILES}{original}/") and original != target:
            return f"{TILES}{target}/" + text[len(f"{TILES}{original}/"):]
        match = _REF.match(text)
        if match and text in by_token:
            return value_of(by_token[text]) or text
        return text

    out = _replace_strings(entry, change)
    known = dict(out.get("sources") or {}) if isinstance(out.get("sources"), dict) else {}
    for ref in refs:
        if ref.get("kind") not in ("file", "folder") or not ref.get("token"):
            continue
        value = value_of(ref)
        if not value:
            continue
        at = {key: path for key, path in ((ref.get("hints") or {}).get("at") or {}).items() if key != me}
        here = found.get(ref["token"])
        if here and here != value:
            at[me] = here
        if ref.get("identity"):
            facts = dict(ref["identity"])
            facts["at"] = at
            known[value] = facts
    for key in ("sources", "origin", "bundleRevision"):
        out.pop(key, None)
    if known:
        out["sources"] = known
    return out


def _find_refs(refs_by_project: dict, bundle_path, placed_media: dict, me: str, *, search: bool) -> dict:
    """{project id: {token: path here}} for each file and folder ref:
    included ones where they were placed, the rest by `_find_cheap`, then
    (with `search`) one `relink.resolve` for all that are left: the
    operating system's index and a walk of the user's folders and the
    bundle's own."""
    from plexora.server.utils import relink

    found: dict = {pid: {} for pid in refs_by_project}
    roots = _search_roots()
    left = {}
    for pid, refs in refs_by_project.items():
        for ref in refs:
            if ref.get("kind") not in ("file", "folder") or not ref.get("token"):
                continue
            if ref.get("included") and ref["included"] in placed_media:
                found[pid][ref["token"]] = placed_media[ref["included"]]
                continue
            hit = _find_cheap(ref, bundle_path, me, roots)
            if hit:
                found[pid][ref["token"]] = hit
            elif ref.get("identity"):
                # Keyed by something that is no path, so `resolve` never
                # takes the key itself for a file that exists.
                left[f"{{{{{pid}|{ref['token']}}}}}"] = (pid, ref)
    if search and left:
        fake = {sources.SOURCES: {key: dict(ref["identity"], at={}) for key, (_pid, ref) in left.items()}}
        extra = [Path(bundle_path).parent] if bundle_path else []
        try:
            hits = relink.resolve(fake, me, extra_roots=extra, walk_seconds=FIND_SECONDS)
        except Exception:  # noqa: BLE001 -- a search that fails finds nothing; relink.ensure tries later
            hits = {}
        for key, path in hits.items():
            pid, ref = left[key]
            found[pid][ref["token"]] = path
    return found


#: How long an import searches the user's folders for images not found where
#: the bundle said; what it does not find, `relink.ensure` looks for at open.
FIND_SECONDS = 5.0


def _backup(folder: Path, root: Path, name: str, entry: Optional[dict]) -> Optional[Path]:
    """The project's state moved to `<root>/.replaced/<name>-<stamp>/`, its
    registry entry beside it, so Replace and Update can be undone by hand."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = Path(root) / ".replaced" / f"{name}-{stamp}"
    number = 2
    while target.exists():
        target = Path(root) / ".replaced" / f"{name}-{stamp}-{number}"
        number += 1
    target.mkdir(parents=True)
    if folder.exists():
        os.replace(folder, target / "state") if _same_device(folder, target) else shutil.move(str(folder), str(target / "state"))
    if entry is not None:
        (target / "entry.json").write_text(json.dumps(entry, indent=1, ensure_ascii=False), encoding="utf-8")
    return target


def _same_device(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b if b.exists() else b.parent).st_dev
    except OSError:
        return False


def _move(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if _same_device(source, target.parent):
        os.replace(source, target)
    else:
        shutil.move(str(source), str(target))


_IMPORTING: set = set()
_IMPORTING_LOCK = threading.Lock()


def apply(source, choices=None, into=None, *, progress=None, cancel=None, stage=None,
          rename: Optional[dict] = None, search: bool = True, new_ids: bool = False,
          placed: Optional[dict] = None) -> dict:
    """Import a bundle: the projects `choices` ({id: action}) names, each by
    its status's default otherwise (DEFAULT_ACTION), into the Location `into`
    (else where new projects go). Returns `{projects: [{id, name, status,
    action, target, backup?, refs: {found, included, missing, node}}],
    datasets, figures, captures}`.

    `placed` ({member path: file}): members the website's page has already
    written where `placements` said they go, checked by their size and never
    unpacked again; a failure removes them with the rest.

    Nothing is written to the registry until every file is in place, and a
    failure or a cancel puts everything back as it was."""
    stage = stage or (lambda key: None)
    placed = {str(path): str(where) for path, where in (placed or {}).items()}
    opened = open_source(source)
    try:
        stage("verify")
        manifest = opened.manifest()
        root = _target_root(into)
        decided = _decide(manifest, root, choices, rename=rename, force="keep_both" if new_ids else None,
                          placed=placed)
        chosen = [item for item in decided if item["action"] != "skip"]
        wanted = _validate(opened, manifest, chosen, root)
        bundle_id = re.sub(r"[^A-Za-z0-9_-]", "", str((manifest.get("bundle") or {}).get("id") or "")) or uuid.uuid4().hex
        with _IMPORTING_LOCK:
            if bundle_id in _IMPORTING:
                raise BundleError("This project file is being imported already.")
            _IMPORTING.add(bundle_id)
        try:
            taken = {record["path"] for record in wanted}
            result = _apply(opened, manifest, decided, chosen, wanted, root, bundle_id,
                            progress=progress, cancel=cancel, stage=stage, search=search,
                            placed={path: where for path, where in placed.items() if path in taken})
            for path, where in placed.items():
                if path not in taken:
                    Path(where).unlink(missing_ok=True)
            return result
        finally:
            with _IMPORTING_LOCK:
                _IMPORTING.discard(bundle_id)
    except BaseException:
        # What the page wrote for an import that did not happen goes too, and
        # the folders it made for it where they are left empty.
        try:
            top = _target_root(into) if placed else None
        except BundleError:
            top = None
        for where in placed.values():
            try:
                Path(where).unlink(missing_ok=True)
            except OSError:
                continue
            for folder in Path(where).parents:
                if top is None or folder == Path(top) or Path(top) not in folder.parents:
                    break
                try:
                    folder.rmdir()
                except OSError:
                    break
        raise
    finally:
        if opened is not source:
            opened.close()


def _take_placed(where: str, destination: Path, record: dict, undo: list) -> None:
    """A member the page wrote itself, where `placements` said (or, should
    this import have decided otherwise since, moved there)."""
    where = Path(where)
    try:
        size = where.stat().st_size
    except OSError:
        size = None
    # The website leaves a file too large to hold in its folder, unseen here
    # (storeSync.js HELD_BYTES): its page checked its CRC and sha256.
    if size is not None and size != int(record.get("size") or 0):
        raise BundleError(f"This Plexora project file did not unpack whole ({record['path']}).")
    undo.append(lambda d=destination: d.unlink(missing_ok=True))
    if size is not None and os.path.normcase(str(where)) != os.path.normcase(str(destination)):
        if destination.exists():
            raise BundleError(f"{destination} is in the way.")
        _move(where, destination)


def _apply(opened, manifest, decided, chosen, wanted, root, bundle_id, *, progress, cancel, stage, search,
           placed=None):
    module = _project_module()
    me = sources.app_key()
    placed = placed or {}
    stage_dir = Path(root) / f".importing-{bundle_id}"
    meter = _Progress(sum(int(record.get("size") or 0) for record in wanted), progress, cancel)
    undo: list = []
    later: list = []
    results = []
    bundle_path = getattr(opened, "path", None)
    try:
        shutil.rmtree(stage_dir, ignore_errors=True)
        stage_dir.mkdir(parents=True)
        stage("unpack")
        staged = {}
        for record in wanted:
            if record["path"] in placed:
                meter.advance(int(record.get("size") or 0))
                continue
            target = stage_dir.joinpath(*record["path"].split("/"))
            _extract(opened, record, target, meter)
            staged[record["path"]] = target
        meter.check()
        stage("place")
        renamed = {}
        placed_media: dict = {}
        entries = {}
        for item in chosen:
            project = item["project"]
            pid, original, target = item["id"], str(project.get("name") or ""), item["target"]
            target_root = item["root"]
            state_dir = paths.project_dir(target, target_root)
            derived_dir = paths.derived_root(target, home_root=target_root)
            local_entry = None
            if item["action"] in ("update", "replace"):
                local_root = item["root"]
                local_entry = module.read_config(paths.config_path(local_root)).get(target)
                backup = _backup(paths.project_state_dir(target), Path(local_root), target, local_entry)
                item["backup"] = str(backup)
                undo.append(lambda backup=backup, folder=paths.project_state_dir(target): _restore(backup, folder))
                state_dir = paths.project_state_dir(target)
            # Its own folder: state and tables. A new project's folder is
            # made here and removed whole on the way back (one holding only
            # what the page placed for it is new too); an update's was moved
            # aside above.
            if not _occupied(state_dir, frozenset(str(Path(where)) for where in placed.values())):
                undo.append(lambda d=state_dir: shutil.rmtree(d, ignore_errors=True))
            for record in project.get("files") or ():
                path = record["path"]
                if manifest.get("format") == FORMAT_1:
                    rel, place = path[len(FOLDER_1):], "state"
                else:
                    head = f"projects/{pid}/"
                    if not path.startswith(head):
                        raise BundleError(_OUTSIDE)
                    place, _, rel = path[len(head):].partition("/")
                    if place not in ("state", "derived") or not rel:
                        raise BundleError(_OUTSIDE)
                if rel == f"{original}.db" and place == "state":
                    rel = f"{target}.db"
                    if original != target:
                        rename_rows(staged[path], original, target)
                folder = state_dir if place == "state" else derived_dir
                destination = folder.joinpath(*rel.split("/"))
                if path in placed:
                    _take_placed(placed[path], destination, record, undo)
                    continue
                if destination.exists():
                    if place == "state":
                        raise BundleError(f"{destination} is in the way.")
                    # A ready-to-open file this computer has already: kept
                    # aside until the import has landed.
                    aside = destination.with_name(destination.name + f".before-{bundle_id}")
                    os.replace(destination, aside)
                    undo.append(lambda a=aside, b=destination: os.replace(a, b))
                    later.append(lambda a=aside: a.unlink(missing_ok=True))
                undo.append(lambda d=destination: d.unlink(missing_ok=True))
                _move(staged[path], destination)
            # The images it included, in its own folder, once per bundle.
            for ref in project.get("refs") or ():
                prefix = ref.get("included")
                if not prefix or prefix in placed_media:
                    continue
                destination = state_dir.joinpath(*prefix.split("/"))
                if ref.get("kind") == "folder":
                    for record in manifest.get("media") or ():
                        if not record["path"].startswith(prefix + "/"):
                            continue
                        final = state_dir.joinpath(*record["path"].split("/"))
                        if record["path"] in placed:
                            _take_placed(placed[record["path"]], final, record, undo)
                        elif record["path"] in staged:
                            _move(staged[record["path"]], final)
                elif prefix in placed:
                    record = next((item for item in manifest.get("media") or () if item["path"] == prefix),
                                  {"path": prefix})
                    _take_placed(placed[prefix], destination, record, undo)
                elif prefix in staged:
                    _move(staged[prefix], destination)
                placed_media[prefix] = str(destination)
            renamed[original] = target
            item["state_dir"], item["derived_dir"] = state_dir, derived_dir
        # Every project's images, found in one search (one walk of the user's
        # folders for a whole dataset, not one per project).
        found_all = _find_refs({item["id"]: item["project"].get("refs") or [] for item in chosen},
                               bundle_path, placed_media, me, search=search)
        for item in chosen:
            project = item["project"]
            pid, original, target = item["id"], str(project.get("name") or ""), item["target"]
            found = found_all.get(pid, {})
            entry = _staged_entry(staged, project) or opened.entry(project)
            if manifest.get("format") == FORMAT_1:
                entry = _replace_strings(entry, lambda text: (f"{TILES}{target}/" + text[len(f"{TILES}{original}/"):]
                                                              if text.startswith(f"{TILES}{original}/") else text))
            else:
                entry = _detokenise(entry, original=original, target=target, state_dir=item["state_dir"],
                                    derived_dir=item["derived_dir"], refs=project.get("refs") or [], found=found,
                                    me=me)
            new_id = uuid.uuid4().hex if item["action"] == "keep_both" and item["local"] else pid
            entry["id"] = new_id
            item["new_id"] = new_id
            entry["origin"] = {"bundle": (manifest.get("bundle") or {}).get("id"),
                               "revision": project.get("revision"), "importedAt": _now(),
                               "app": ((manifest.get("bundle") or {}).get("app") or {}).get("kind")}
            entries[pid] = entry
            refs = project.get("refs") or []
            results.append({"id": new_id, "bundle_id": pid, "name": project.get("name"), "status": item["status"],
                            "action": item["action"], "target": target, "backup": item.get("backup"),
                            "refs": {"found": sum(1 for ref in refs if ref.get("token") in found
                                                  and not ref.get("included")),
                                     "included": sum(1 for ref in refs if ref.get("included")),
                                     "missing": sum(1 for ref in refs if ref.get("kind") in ("file", "folder")
                                                    and ref.get("token") not in found),
                                     "node": sum(1 for ref in refs if ref.get("kind") == "node")}})
        meter.check()
        names_by_id = {item["id"]: item["target"] for item in chosen}
        datasets_done = _merge_datasets(manifest, names_by_id, chosen, undo,
                                        ids={item["target"]: item.get("new_id") for item in chosen})
        captures_done = _merge_captures(manifest, staged, renamed, names_by_id, undo)
        figures_done = _merge_figures(manifest, staged, renamed, names_by_id, undo)
        meter.check()
        # The registry, last.
        with module.config_transaction():
            by_root: dict = {}
            for item in chosen:
                by_root.setdefault(Path(item["root"]), []).append(item)
            for target_root, items in by_root.items():
                path = paths.config_path(target_root)
                config = module.read_config(path)
                for item in items:
                    config[item["target"]] = entries[item["id"]]
                module.write_config(path, config)
    except BaseException:
        for step in reversed(undo):
            try:
                step()
            except Exception:  # noqa: BLE001 -- put back as much as can be put back
                pass
        raise
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
    for step in later:
        try:
            step()
        except OSError:
            pass
    for item in chosen:
        if not item["project"].get("revision"):
            continue
        try:
            local = _local(item["target"], assign=False)
            part = _project_plan(local, INCLUDE_DEFAULT, identities=False)
            _record_revision(local, item["project"].get("revision"), item["project"].get("parent"), part["stamp"])
        except Exception:  # noqa: BLE001 -- a revision is a courtesy for the next import
            pass
    skipped = [{"id": item["id"], "name": item["project"].get("name"), "status": item["status"],
                "action": "skip", "target": item["target"]} for item in decided if item["action"] == "skip"]
    return {"projects": results + skipped, "datasets": datasets_done, "captures": captures_done,
            "figures": figures_done, "root": str(root)}


def _staged_entry(staged: dict, project: dict) -> Optional[dict]:
    """A project's entry as unpacked (its source may have given the file up:
    an owned folder moves its files)."""
    record = project.get("entry") if isinstance(project.get("entry"), dict) else None
    target = staged.get(record["path"]) if record else None
    if target is None:
        return None
    try:
        entry = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BundleError("This Plexora project file is damaged.") from error
    if not isinstance(entry, dict):
        raise BundleError("This Plexora project file is damaged.")
    return entry


def _restore(backup: Path, folder: Path) -> None:
    held = backup / "state"
    if held.exists():
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        _move(held, folder)
    shutil.rmtree(backup, ignore_errors=True)


def _remove_if_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass


def _merge_datasets(manifest: dict, names_by_id: dict, chosen: list, undo: list, ids=None) -> list:
    """The bundle's datasets merged by id, then by name, with the imported
    projects in them. A project that replaced one of the user's keeps the
    dataset it was in."""
    from plexora.server.models import datasets as registry

    joining = {item["id"] for item in chosen if item["action"] not in ("update", "replace")}
    done = []
    with registry._CONFIG_LOCK:
        records, files = registry._open()
        before = dict(records)
        for raw in manifest.get("datasets") or ():
            members = [names_by_id[pid] for pid in raw.get("projects") or () if pid in joining and pid in names_by_id]
            if not members or not str(raw.get("name") or "").strip():
                continue
            dataset_id = re.sub(r"[^A-Za-z0-9_-]", "", str(raw.get("id") or "")) or uuid.uuid4().hex[:12]
            current = records.get(dataset_id) or next(
                (item for item in records.values()
                 if item.name.casefold() == str(raw["name"]).strip().casefold()), None)
            records = registry._released(records, members)
            # Each member's id beside its name, where this build records them
            # (the projects are registered after this, so nothing else can).
            known = {name: (ids or {}).get(name) for name in members if (ids or {}).get(name)}
            if current is not None:
                current = records[current.id]
                current = current.with_projects(
                    tuple(current.projects) + tuple(name for name in members if name not in current.projects))
                if known and hasattr(current, "project_ids"):
                    current = dataclass_replace(current, project_ids={**current.project_ids, **known})
                records[current.id] = current
                done.append({"id": current.id, "name": current.name, "projects": members, "new": False})
            else:
                made = registry.Dataset(
                    id=dataset_id, name=str(raw["name"]).strip()[:registry.MAX_NAME],
                    created_at=registry._now(), description=str(raw.get("description") or ""),
                    meta=dict(raw.get("meta") or {}) if isinstance(raw.get("meta"), dict) else {},
                    projects=tuple(members))
                if known and hasattr(made, "project_ids"):
                    made = dataclass_replace(made, project_ids=known)
                records[dataset_id] = made
                done.append({"id": dataset_id, "name": str(raw["name"]).strip(), "projects": members, "new": True})
        if done:
            registry._store(records, files)

            def put_back(before=before):
                with registry._CONFIG_LOCK:
                    _now_records, now_files = registry._open()
                    registry._store(before, now_files)

            undo.append(put_back)
    return done


def _merge_captures(manifest: dict, staged: dict, renamed: dict, names_by_id: dict, undo: list) -> int:
    """Captures and bookmarks of the imported projects into this computer's
    bin, their project's name rewritten; a capture id already here gets a new
    one. Skipped (and said) when this build has no figure builder."""
    records = [capture for capture in manifest.get("captures") or ()
               if capture.get("project") in names_by_id and capture.get("path") in staged]
    if not records:
        return 0
    try:
        from plexora.plugins.figure_builder.server import captures as bin_
    except Exception:  # noqa: BLE001 -- no figure builder in this build: nothing to put them in
        return 0
    inserted = []
    with bin_._LOCK:
        connection = bin_._connect()
        try:
            existing = {row[0] for row in connection.execute("SELECT capture_id FROM captures")}
            with connection:
                for capture in records:
                    target = names_by_id[capture["project"]]
                    rows = json.loads(staged[capture["path"]].read_text(encoding="utf-8"))
                    for row in rows if isinstance(rows, list) else ():
                        capture_id = str(row.get("capture_id") or "")
                        if not bin_.CAPTURE_ID_PATTERN.match(capture_id) or capture_id in existing:
                            capture_id = bin_.new_capture_id()
                        source = row.get("source_json") or "{}"
                        try:
                            parsed = json.loads(source)
                            if isinstance(parsed, dict) and parsed.get("datasource") in renamed:
                                parsed["datasource"] = renamed[parsed["datasource"]]
                            source = json.dumps(parsed, separators=(",", ":"))
                        except ValueError:
                            pass
                        preview = None
                        if row.get("preview") and row["preview"] in staged:
                            preview = staged[row["preview"]].read_bytes()
                        connection.execute(
                            "INSERT INTO captures (capture_id, datasource, source_json, scene_json, caption, "
                            "width, height, format, preview, created_at, bookmarked, label) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (capture_id, target, source, row.get("scene_json") or "{}", row.get("caption") or "",
                             int(row.get("width") or 0), int(row.get("height") or 0), row.get("format") or "webp",
                             preview, row.get("created_at") or _now(), int(row.get("bookmarked") or 0),
                             row.get("label") or ""))
                        existing.add(capture_id)
                        inserted.append(capture_id)
        finally:
            connection.close()

    def take_out(ids=tuple(inserted)):
        with bin_._LOCK:
            connection = bin_._connect()
            try:
                with connection:
                    connection.executemany("DELETE FROM captures WHERE capture_id = ?", [(i,) for i in ids])
            finally:
                connection.close()

    undo.append(take_out)
    return len(inserted)


def _merge_figures(manifest: dict, staged: dict, renamed: dict, names_by_id: dict, undo: list) -> list:
    """Figures whose projects were all imported, copied into this user's
    figures with each image's project name rewritten, under a new id when
    theirs is taken."""
    done = []
    for figure in manifest.get("figures") or ():
        projects = figure.get("projects") or []
        files = figure.get("files") or []
        if not projects or not all(pid in names_by_id for pid in projects) \
                or not all(record["path"] in staged for record in files):
            continue
        figure_id = figure["id"]
        root = paths.figures_root()
        if (root / figure_id).exists():
            figure_id = "fig_" + uuid.uuid4().hex[:12]
        folder = root / figure_id
        prefix = f"figures/{figure['id']}/"
        for record in files:
            _move(staged[record["path"]], folder.joinpath(*record["path"][len(prefix):].split("/")))
        undo.append(lambda folder=folder: shutil.rmtree(folder, ignore_errors=True))
        database = folder / "figure.db"
        if database.is_file():
            connection = sqlite3.connect(str(database), timeout=10)
            try:
                with connection:
                    row = connection.execute("SELECT json FROM document WHERE id = 1").fetchone()
                    document = json.loads(row[0]) if row else {}
                    document["figure_id"] = figure_id
                    for source in (document.get("sources") or {}).values():
                        if isinstance(source, dict) and source.get("datasource") in renamed:
                            source["datasource"] = renamed[source["datasource"]]
                    connection.execute("UPDATE document SET json = ? WHERE id = 1",
                                       (json.dumps(document, separators=(",", ":")),))
                    summary = sorted({source.get("display_name") or source.get("datasource")
                                      for source in (document.get("sources") or {}).values()
                                      if isinstance(source, dict)
                                      and (source.get("display_name") or source.get("datasource"))})
                    connection.executemany(
                        "INSERT INTO meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        [("figure_id", figure_id), ("sources", json.dumps(summary))])
                    try:
                        connection.execute("DELETE FROM journal")
                    except sqlite3.Error:
                        pass  # an older figure without a journal
            except (sqlite3.Error, ValueError):
                pass
            finally:
                connection.close()
        done.append({"id": figure_id, "title": figure.get("title")})
    return done


# -- FORMAT 1 and one-call wrappers -----------------------------------------


def read_manifest(path) -> dict:
    """The manifest of a bundle (a path), as FORMAT 2 describes it."""
    opened = open_source(path)
    try:
        return opened.manifest()
    finally:
        opened.close()


def _spill(data) -> Path:
    """Bytes (or a stream) written to a file of their own in a temporary
    folder, 1 MiB at a time."""
    folder = Path(tempfile.mkdtemp(prefix="plexora-upload-"))
    target = folder / f"upload{SUFFIX}"
    with target.open("wb") as out:
        if isinstance(data, (bytes, bytearray, memoryview)):
            out.write(data)
        else:
            while True:
                chunk = data.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
    return target


def import_bundle(data, name: Optional[str] = None, into=None) -> str:
    """Add every project of a `.plexora` file (a path, bytes or a stream) to
    this app's projects as new ones, under their names or the next free ones,
    and return the first one's name. The one-call form the older routes and
    the command line's `--name` use; the dialog previews first."""
    spilled = None
    if isinstance(data, (str, Path)):
        path = Path(data)
    else:
        path = spilled = _spill(data)
    try:
        opened = open_source(path)
        try:
            manifest = opened.manifest()
            projects = manifest.get("projects") or []
            rename = {projects[0]["id"]: name} if name and len(projects) == 1 else None
            result = apply(opened, into=into, rename=rename, new_ids=True)
        finally:
            opened.close()
    finally:
        if spilled is not None:
            shutil.rmtree(spilled.parent, ignore_errors=True)
    names = [item["target"] for item in result["projects"] if item.get("action") != "skip"]
    if not names:
        raise BundleError("This Plexora project file holds no project.")
    return names[0]


def export_file(name: str, dest) -> Path:
    """Project `name` as a `.plexora` file at `dest` (the defaults)."""
    return Path(export([name], dest)["path"])


# -- copies ------------------------------------------------------------------


def copy_project(name: str, into=None, *, progress=None, cancel=None, stage=None) -> dict:
    """"Make a copy": the project (and its state, its tables, its bookmarks
    and figures) written as a bundle folder beside where it lands and
    imported from there as a new project -- also for a shared project, which
    becomes the user's own."""
    root = _target_root(into)
    folder = Path(root) / f".importing-copy-{uuid.uuid4().hex[:12]}"
    try:
        export([name], folder, include={"tables": True, "captures": True, "figures": False},
               sink=FolderSink(folder, progress=None, cancel=cancel), stage=None, record=False)
        result = apply(FolderSource(folder, owned=True), into=into, new_ids=True,
                       progress=progress, cancel=cancel, stage=stage)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        shutil.rmtree(folder.with_name(folder.name + ".part"), ignore_errors=True)
    return result


# -- jobs --------------------------------------------------------------------


TASK_OWNER = "project-bundle"
EXPORT_STAGES = {
    "snapshot": (0, 3, "Taking a snapshot"),
    "pack": (3, 97, "Packing"),
    "verify": (97, 100, "Checking the file"),
}
IMPORT_STAGES = {
    "verify": (0, 3, "Checking the file"),
    "unpack": (3, 95, "Unpacking"),
    "place": (95, 100, "Adding the projects"),
}
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def staging_root() -> Path:
    """Where a bundle is written when the server cannot write where the user
    is (downloaded from there), and where uploads wait for their import: the
    data root, which has the room; on the website its temporary folder, since
    its data root is the folder the page mirrors into the user's own."""
    if sources.app_key() == "website":
        return Path(tempfile.gettempdir()) / "plexora-bundles"
    return paths.data_root() / ".bundles"


def _sweep(days: float = 1.0) -> None:
    root = staging_root()
    if not root.is_dir():
        return
    cutoff = time.time() - days * 86400
    for child in root.iterdir():
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
        except OSError:
            continue


def file_name(names: list, dataset: Optional[str]) -> str:
    if dataset:
        base = dataset
    elif len(names) == 1:
        base = names[0]
    else:
        base = f"plexora-projects-{time.strftime('%Y-%m-%d')}"
    return _safe_name(base) + SUFFIX


def start_export(ids: Iterable[str] = (), include=None, dest=None, *, dataset: Optional[str] = None) -> dict:
    """Export as a background task; its first status. Without `dest`, the
    file is written under `staging_root()` and fetched from
    `/projects/export/<job>/file`."""
    names = resolve_projects(ids, dataset=dataset)
    include = _include(include)
    token = uuid.uuid4().hex[:16]
    staged = dest is None
    if staged:
        _sweep()
        dest = staging_root() / token / file_name(names, dataset)
    elif not os.path.isabs(str(dest)):
        raise BundleError("Choose where to save the file (a full path).")
    dest = Path(dest)
    if dest.suffix.lower() != SUFFIX and not staged:
        dest = dest.with_name(dest.name + SUFFIX)
    state = {"kind": "export", "names": names, "dest": str(dest), "staged": staged, "done": 0, "total": 0,
             "result": None, "error": None}
    with _JOBS_LOCK:
        _JOBS[token] = state

    def work(stage, report, cancel):
        def progress(done, total):
            state["done"], state["total"] = done, total
            report(done, total)

        try:
            result = export(names, dest, include=include, progress=progress, cancel=cancel, stage=stage)
        except Cancelled:
            return
        except Exception as error:
            state["error"] = str(error)
            raise
        state["result"] = {"path": result["path"], "size": result["size"],
                           "projects": [{"id": p["id"], "name": p["name"], "revision": p["revision"]}
                                        for p in result["manifest"]["projects"]],
                           "left_out": result["manifest"]["left_out"]}

    _start(token, work, EXPORT_STAGES, "export")
    return job_status(token)


def start_import(path, choices=None, into=None, *, cleanup: bool = False, placed: Optional[dict] = None) -> dict:
    """Import as a background task; its first status. `cleanup`: `path` is
    an upload of this server's, removed when the task ends. `placed`: what the
    website's page has written itself (`apply`)."""
    path = Path(path)
    opened = open_source(path)
    opened.close()
    token = uuid.uuid4().hex[:16]
    state = {"kind": "import", "path": str(path), "done": 0, "total": 0, "result": None, "error": None}
    with _JOBS_LOCK:
        _JOBS[token] = state

    def work(stage, report, cancel):
        def progress(done, total):
            state["done"], state["total"] = done, total
            report(done, total)

        try:
            state["result"] = apply(path, choices, into, progress=progress, cancel=cancel, stage=stage,
                                    placed=placed)
        except Cancelled:
            return
        except Exception as error:
            state["error"] = str(error)
            raise
        finally:
            if cleanup:
                shutil.rmtree(path.parent, ignore_errors=True) if path.parent.parent == staging_root() \
                    else path.unlink(missing_ok=True)

    _start(token, work, IMPORT_STAGES, "import")
    return job_status(token)


def start_copy(name: str, into=None) -> dict:
    """"Make a copy" as a background task (polled like an import)."""
    _local(name, assign=False)
    token = uuid.uuid4().hex[:16]
    state = {"kind": "copy", "name": name, "done": 0, "total": 0, "result": None, "error": None}
    with _JOBS_LOCK:
        _JOBS[token] = state

    def work(stage, report, cancel):
        def progress(done, total):
            state["done"], state["total"] = done, total
            report(done, total)

        try:
            state["result"] = copy_project(name, into, progress=progress, cancel=cancel, stage=stage)
        except Cancelled:
            return
        except Exception as error:
            state["error"] = str(error)
            raise

    _start(token, work, IMPORT_STAGES, "import")
    return job_status(token)


def _start(token: str, work, stages: dict, route: str) -> None:
    from plexora.server.models import layer_jobs

    layer_jobs.start_task(TASK_OWNER, token, work, stages=stages, mirror={
        "key": f"bundle-{route}:{token}", "get": f"/projects/{route}/{token}",
        "answer": lambda: job_status(token),
        "cancel": f"/projects/{route}/{token}/cancel", "cancelled": {"cancelled": True, "job": token}})


def job_status(token: str) -> dict:
    """`{job, kind, status, progress, stage, stage_label, error, done, total,
    result, file?}` of an export, import or copy."""
    from plexora.server.models import layer_jobs

    with _JOBS_LOCK:
        state = _JOBS.get(token)
    if state is None:
        raise KeyError(token)
    record = layer_jobs.tasks(TASK_OWNER, prefix=token).get(token) or {}
    status = record.get("status", "pending")
    out = {"job": token, "kind": state["kind"], "status": status, "progress": record.get("progress", 0),
           "stage": record.get("stage"), "stage_label": record.get("stage_label"),
           "error": state.get("error") or record.get("error"), "done": state["done"], "total": state["total"],
           "result": state.get("result")}
    if state["kind"] == "export":
        dest = Path(state["dest"])
        out["file"] = {"name": dest.name, "size": (state.get("result") or {}).get("size"),
                       "path": None if state["staged"] else str(dest),
                       "url": f"/projects/export/{token}/file" if state["staged"] else None}
    return out


def job_file(token: str) -> Optional[Path]:
    """The finished file of a staged export, or None."""
    with _JOBS_LOCK:
        state = _JOBS.get(token)
    if state is None or state["kind"] != "export" or not state.get("result"):
        return None
    path = Path(state["dest"])
    return path if path.is_file() else None


def cancel_job(token: str) -> bool:
    """Stop a job; a staged export's file is removed too."""
    from plexora.server.models import layer_jobs

    with _JOBS_LOCK:
        state = _JOBS.get(token)
    if state is None:
        raise KeyError(token)
    layer_jobs.stop_task(TASK_OWNER, token, timeout=5.0)
    if state["kind"] == "export":
        dest = Path(state["dest"])
        Path(str(dest) + ".part").unlink(missing_ok=True)
        if state["staged"]:
            shutil.rmtree(dest.parent, ignore_errors=True)
    return True


def forget() -> None:
    with _JOBS_LOCK:
        _JOBS.clear()
