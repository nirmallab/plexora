"""Finding a project's files on this computer when another app recorded them.

A project made on the website names its files the way a web page knows them
(`/picked/<id>/<name>`, `/derived/<key>/<name>`), with each file's identity
(server/models/sources.py). When the desktop app opens such a project from
the library it shares with the website (Documents/Plexora/projects), `ensure`
finds each file here without asking and records where (`at` in the project's
`sources`), so every later open is immediate and the website's own paths are
left as they were.

Where it looks, cheapest first, and why:
1. where this computer already found it (`at`) -- every open after the first;
2. a pyramid the website built in Plexora's own folder
   (Documents/Plexora/pyramids/<key>/<name>), or beside its source; a file
   the website's Python wrote into its project store (`/data/<rel>`), which
   is Documents/Plexora/projects/<rel> when the projects are kept there;
3. the folder of a file of the same project already found -- one pick is one
   folder, so one find usually finds the rest;
4. the operating system's file index (Spotlight, Windows Search, plocate);
5. a walk of the user's own folders (Documents, Desktop, Downloads, Pictures,
   cloud folders, and the folders this computer's other projects read from),
   for at most WALK_SECONDS, once for every file still missing.
A candidate is the file only when `sources.same_file` says so: same size, and
the same time to the millisecond or the same bytes at both ends. A folder
source (a .zarr store) is looked for the same way, by its folder identity.

A project file brought from elsewhere (server/models/bundles.py) adds two
places before these, both from what the bundle recorded: the image's path
relative to where the bundle was saved (`relative_to_bundle`: a bundle saved
to /Volumes/lab/exports/ beside /Volumes/lab/images/ finds ../images/a.tif
again from //server/lab/exports/ on Windows), and the trailing parts of
its path under this computer's shared roots and Locations (`tail_candidates`).
"Find missing images..." searches one folder the user picked and nothing else
(`resolve_in`, `find_in_folder`).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

from plexora._subprocess import popen_kwargs
from plexora.server.models import sources

_log = logging.getLogger(__name__)

#: How long the walk of the user's folders may take, for all missing files.
WALK_SECONDS = 10.0
#: How long one query of the system's file index may take.
INDEX_SECONDS = 5.0
#: Folders a walk never enters: the system's, caches, and build trees.
_SKIP = {"appdata", "library", "node_modules", ".git", "$recycle.bin", "windows", "program files",
         "program files (x86)", "programdata", ".cache", ".conda", "site-packages", "__pycache__"}


def documents_dir() -> Path:
    try:
        from platformdirs import user_documents_dir

        return Path(user_documents_dir())
    except Exception:  # noqa: BLE001 -- platformdirs is a dependency; a bare home is the fallback
        return Path.home() / "Documents"


def plexora_folder() -> Path:
    """Plexora's own folder, shared with the website: Documents/Plexora."""
    return documents_dir() / "Plexora"


def website_copies(source, name: str, kind: str = "image") -> list:
    """Where the website keeps its copy of the file `source` in Plexora's
    folder, when it made one: Documents/Plexora/pyramids/<key>/<name>, the
    key worked out from the file as the page reads it (providers/browser.py
    `derived_key`: a web page sees times in whole milliseconds, so the
    millisecond each side of this computer's time is tried too). The paths
    that exist; [] on the website itself, for a folder, or a file not here.
    A desktop resolver adopts one only after its own check, and never writes
    there."""
    if sources.app_key() == "website":
        return []
    try:
        path = Path(source)
        stat = path.stat()
        if path.is_dir():
            return []
    except (OSError, TypeError, ValueError):
        return []
    folder = plexora_folder() / "pyramids"
    if not folder.is_dir():
        return []
    from plexora.server.providers.browser import derived_key

    millis = stat.st_mtime_ns // 1_000_000
    found = []
    for when in (millis, millis - 1, millis + 1):
        candidate = folder / derived_key(path.name, f"{stat.st_size}-{when * 1_000_000}", kind) / name
        if candidate.is_file() and candidate not in found:
            found.append(candidate)
    return found


def _library_path(recorded: str) -> Optional[Path]:
    """A file the website's Python wrote into its store (`/data/<rel>`), where
    it is on this computer when the projects are kept in Plexora's folder."""
    if not recorded.startswith("/data/"):
        return None
    return plexora_folder() / "projects" / recorded[len("/data/"):]


def _user_folders() -> list:
    home = Path.home()
    found = [documents_dir(), home / "Desktop", home / "Downloads", home / "Pictures"]
    try:
        found += sorted(path for path in home.iterdir()
                        if path.is_dir() and path.name.lower().startswith(("onedrive", "dropbox", "google drive", "box")))
    except OSError:
        pass
    return [path for path in found if path.is_dir()]


def _index(name: str) -> list:
    """Paths the operating system's file index knows by this name."""
    try:
        if sys.platform == "darwin" and shutil.which("mdfind"):
            done = subprocess.run(["mdfind", "-name", name], capture_output=True, text=True, timeout=INDEX_SECONDS,
                                  **popen_kwargs())
            return [Path(line) for line in done.stdout.splitlines() if Path(line).name == name]
        if os.name == "nt":
            quoted = name.replace("'", "''")
            script = (
                "$c = New-Object -ComObject ADODB.Connection; "
                "$c.Open(\"Provider=Search.CollatorDSO;Extended Properties='Application=Windows';\"); "
                f"$r = $c.Execute(\"SELECT System.ItemPathDisplay FROM SYSTEMINDEX WHERE System.FileName = '{quoted}'\"); "
                "while (-not $r.EOF) { $r.Fields.Item('System.ItemPathDisplay').Value; $r.MoveNext() }")
            # By its full path: PATH may not reach System32's PowerShell (the
            # desktop app's bundled server, a conda shell).
            shell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            done = subprocess.run([str(shell) if shell.is_file() else "powershell", "-NoProfile", "-NonInteractive",
                                   "-Command", script],
                                  capture_output=True, text=True, timeout=INDEX_SECONDS, **popen_kwargs())
            return [Path(line.strip()) for line in done.stdout.splitlines() if line.strip()]
        for tool in ("plocate", "locate"):
            if shutil.which(tool):
                done = subprocess.run([tool, "-b", "-l", "50", f"\\{name}"], capture_output=True, text=True,
                                      timeout=INDEX_SECONDS, **popen_kwargs())
                return [Path(line) for line in done.stdout.splitlines()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return []


def _walk(roots: Iterable[Path], wanted: set, deadline: float) -> dict:
    """{name: [paths]} for the wanted names under `roots`, breadth first,
    until the deadline."""
    found: dict = {}
    queue = [Path(root) for root in roots]
    seen = set()
    while queue and time.monotonic() < deadline:
        folder = queue.pop(0)
        try:
            key = folder.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    name = entry.name
                    if entry.is_dir(follow_symlinks=False):
                        # A folder source (a .zarr store) is wanted by name
                        # too, and not walked into once found.
                        if name in wanted:
                            found.setdefault(name, []).append(Path(entry.path))
                        elif not name.startswith(".") and name.lower() not in _SKIP:
                            queue.append(Path(entry.path))
                    elif name in wanted:
                        found.setdefault(name, []).append(Path(entry.path))
        except OSError:
            continue
    return found


def _present(path, facts=None) -> bool:
    """Whether `path` is there as what `facts` describes: a folder for a
    folder identity (`sources.folder_identity`), else a file."""
    try:
        candidate = Path(path)
        return candidate.is_dir() if sources.is_folder(facts) else candidate.is_file()
    except (OSError, TypeError, ValueError):
        return False


def _first(facts: dict, candidates: Iterable[Path]) -> Optional[Path]:
    for candidate in candidates:
        if candidate and _present(candidate, facts) and sources.same_file(facts, candidate):
            return Path(candidate)
    return None


def resolve(entry: dict, me: str, extra_roots: Iterable[Path] = (), *, walk_seconds: float = WALK_SECONDS,
            index=_index) -> dict:
    """{recorded path: path here} for every file of `entry` that this
    computer can find and has not recorded yet."""
    known = (entry or {}).get(sources.SOURCES) or {}
    here = {recorded: (info.get("at") or {}).get(me) for recorded, info in known.items()}
    missing = [recorded for recorded, path in here.items()
               if not (path and _present(path, known[recorded]))
               and not (_present(recorded, known[recorded]) and not recorded.startswith(sources.PAGE_ROOTS))]
    out: dict = {}

    def folders_found():
        return {Path(path).parent for path in list(out.values()) + [p for p in here.values() if p]
                if path and Path(path).exists()}

    deadline = time.monotonic() + walk_seconds

    def look(group):
        for recorded in group:
            facts = known[recorded] or {}
            name = facts.get("name") or Path(recorded).name
            candidates = []
            library = _library_path(recorded)
            if library is not None:
                candidates.append(library)
            if facts.get("key"):
                candidates.append(plexora_folder() / "pyramids" / facts["key"] / name)
            source = facts.get("from")
            source_here = out.get(source) or (here.get(source) if source else None)
            if source_here:
                candidates.append(Path(source_here).parent / name)
            candidates += [folder / name for folder in folders_found()]
            hit = _first(facts, candidates)
            if hit is None:
                hit = _first(facts, index(name))
            if hit is not None:
                out[recorded] = str(hit)
        still = [recorded for recorded in group if recorded not in out]
        if still and time.monotonic() < deadline:
            wanted = {(known[r] or {}).get("name") or Path(r).name: r for r in still}
            roots = list(dict.fromkeys([*folders_found(), *extra_roots, *_user_folders()]))
            walked = _walk(roots, set(wanted), deadline)
            for name, recorded in wanted.items():
                hit = _first(known[recorded] or {}, walked.get(name, ()))
                if hit is not None:
                    out[recorded] = str(hit)

    # The user's files first, then what was made from them (the page's
    # pyramids, what the website's Python wrote into its store): a pyramid
    # beside its original is found from where the original was.
    made = ("/derived/", "/data/")
    look([recorded for recorded in missing if not recorded.startswith(made)])
    look([recorded for recorded in missing if recorded.startswith(made)])
    return out


def ensure(name: str) -> int:
    """Find the files of project `name` that this computer has not found yet
    and record where they are. Returns how many were found. Never on the
    website (it records page paths; it finds nothing on a disk)."""
    me = sources.app_key()
    if me == "website":
        return 0
    from plexora.server.models.project import Project, config_transaction, read_config, write_config
    from plexora import paths

    root = Project.root_for(name)
    if root is None:
        return 0
    path = paths.config_path(root)
    entry = read_config(path).get(name) or {}
    if not entry.get(sources.SOURCES):
        return 0
    found = resolve(entry, me, extra_roots=_project_folders(me))
    if not found:
        return 0
    with config_transaction():
        config = read_config(path)
        current = config.get(name)
        if current is None:
            return 0
        known = dict(current.get(sources.SOURCES) or {})
        for recorded, here in found.items():
            info = dict(known.get(recorded) or {})
            info["at"] = {**(info.get("at") or {}), me: here}
            known[recorded] = info
        current = dict(current)
        current[sources.SOURCES] = known
        config[name] = current
        write_config(path, config)
    _log.info("Found %d of %s's files on this computer", len(found), name)
    return len(found)


def _project_folders(me: str) -> list:
    """The folders this computer's other projects read from."""
    from plexora.server.models.project import Project

    folders = []
    try:
        for entry in Project.load_all().values():
            for value in sources._replace(entry, {}).values() if isinstance(entry, dict) else ():
                if isinstance(value, str) and len(value) > 3 and Path(value).is_absolute():
                    parent = Path(value).parent
                    if parent.is_dir():
                        folders.append(parent)
    except Exception:  # noqa: BLE001 -- a hint, never a reason to fail an open
        return []
    return list(dict.fromkeys(folders))[:50]


def missing_here(name: str) -> list:
    """The files of project `name` this app has no path for: [{recorded, name,
    size, ...}], from the identities the project recorded. On the website this
    is what the page asks the user to find, by choosing the folder that holds
    them (client services/pickedFiles.js `find`); a pyramid the page made that
    waits on a folder the browser opens again after one click (client
    services/derivedIndex.js) says so (`waiting`, and where: `place`), so the
    page can ask for that click instead of a search."""
    from plexora.server.models.project import Project, read_config
    from plexora import paths

    me = sources.app_key()
    root = Project.root_for(name)
    if root is None:
        return []
    entry = read_config(paths.config_path(root)).get(name) or {}
    out = []
    waiting_here = None
    for recorded, info in (entry.get(sources.SOURCES) or {}).items():
        info = info or {}
        here = (info.get("at") or {}).get(me) or (recorded if me == "website" and
                                                  recorded.startswith(sources.PAGE_ROOTS) else None)
        if here and _present(here, info):
            continue
        # Where it was recorded, when that is a path here and the file is
        # there (`resolve` takes it as found for the same reason).
        if me != "website" and not recorded.startswith(sources.PAGE_ROOTS) and _present(recorded, info):
            continue
        item = {"recorded": recorded, "name": info.get("name") or Path(recorded).name,
                "size": info.get("size"), "mtime_ms": info.get("mtime_ms"),
                "head": info.get("head"), "tail": info.get("tail")}
        if me == "website" and recorded.startswith("/derived/"):
            if waiting_here is None:
                waiting_here = _waiting_copies()  # one question to the page, asked once
            waiting = waiting_here.get(recorded)
            if waiting is not None:
                item.update(waiting=True, place=waiting.get("place"))
        out.append(item)
    return out


def _waiting_copies() -> dict:
    """{path: the page's record} of the website's copies waiting on a
    folder's permission (providers/browser.py `derived_index`)."""
    from plexora.server.providers import browser as browser_provider

    return {path: found for path, found in browser_provider.derived_index().items()
            if isinstance(found, dict) and found.get("state") == "waiting"}


def record_found(name: str, found: dict) -> int:
    """Record where this app found files of project `name` ({recorded: path
    here}), each only if it is the file the project recorded. Returns how many
    were recorded."""
    from plexora.server.models.project import Project, config_transaction, read_config, write_config
    from plexora import paths

    me = sources.app_key()
    root = Project.root_for(name)
    if root is None or not found:
        return 0
    path = paths.config_path(root)
    with config_transaction():
        config = read_config(path)
        current = config.get(name)
        if current is None:
            return 0
        known = dict(current.get(sources.SOURCES) or {})
        recorded = 0
        for original, here in found.items():
            info = known.get(original)
            if not info or not isinstance(here, str) or not sources.same_file(info, here):
                continue
            info = dict(info)
            info["at"] = {**(info.get("at") or {}), me: here}
            known[original] = info
            recorded += 1
        if recorded:
            current = dict(current)
            current[sources.SOURCES] = known
            config[name] = current
            write_config(path, config)
    return recorded


# -- a project brought from elsewhere (server/models/bundles.py) -----------


def relative_to_bundle(relative, bundle_path) -> Optional[Path]:
    """Where a file recorded `relative` to an exported bundle's folder is,
    relative to where the bundle is now; None when nothing was recorded.

    Written with forward slashes whatever the exporting computer was, so a
    bundle from a Mac on a share finds its images from Windows."""
    if not relative or not bundle_path:
        return None
    text = str(relative).replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return None
    try:
        base = Path(bundle_path)
        base = base if base.is_dir() else base.parent
        return Path(os.path.normpath(base.joinpath(*text.split("/"))))
    except (OSError, TypeError, ValueError):
        return None


#: How many trailing parts of a recorded path are tried under each root.
TAIL_DEPTH = 6


def tail_candidates(recorded, roots: Iterable[Path], depth: int = TAIL_DEPTH) -> list:
    """`root/<the last k parts of recorded>` for k from `depth` down to 1, for
    each root: a share mounted at /Volumes/lab on one computer and at
    //server/lab or L: on another keeps everything below its mount the same.
    Longest tails first, so the deepest agreement is tried before a bare name."""
    if not recorded:
        return []
    parts = [part for part in str(recorded).replace("\\", "/").split("/") if part and not part.endswith(":")]
    out = []
    for root in roots:
        try:
            base = Path(root)
        except TypeError:
            continue
        for k in range(min(depth, len(parts)), 0, -1):
            candidate = base.joinpath(*parts[-k:])
            if candidate not in out:
                out.append(candidate)
    return out


def resolve_in(entry: dict, me: str, folder, *, walk_seconds: float = WALK_SECONDS) -> dict:
    """{recorded path: path here} for the files of `entry` this app has no
    path for that are under `folder` -- the one folder the user picked, and
    nowhere else (no index, no other folders)."""
    known = (entry or {}).get(sources.SOURCES) or {}
    wanted = {}
    for recorded, info in known.items():
        info = info or {}
        here = (info.get("at") or {}).get(me)
        if here and _present(here, info):
            continue
        if not recorded.startswith(sources.PAGE_ROOTS) and _present(recorded, info):
            continue
        wanted.setdefault(info.get("name") or Path(recorded).name, []).append(recorded)
    if not wanted:
        return {}
    walked = _walk([Path(folder)], set(wanted), time.monotonic() + walk_seconds)
    out = {}
    for name, recorded_paths in wanted.items():
        for recorded in recorded_paths:
            hit = _first(known[recorded] or {}, walked.get(name, ()))
            if hit is not None:
                out[recorded] = str(hit)
    return out


def find_in_folder(name: str, folder) -> dict:
    """Look for project `name`'s missing files in `folder` only, and record
    where they are. `{found: n, missing: [...]}` (`missing_here`)."""
    me = sources.app_key()
    from plexora.server.models.project import Project, read_config
    from plexora import paths

    root = Project.root_for(name)
    if root is None:
        raise KeyError(name)
    entry = read_config(paths.config_path(root)).get(name) or {}
    found = resolve_in(entry, me, folder)
    recorded = record_found(name, found) if found else 0
    return {"found": recorded, "missing": missing_here(name)}
