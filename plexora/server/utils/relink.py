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
   (Documents/Plexora/pyramids/<key>/<name>), or beside its source;
3. the folder of a file of the same project already found -- one pick is one
   folder, so one find usually finds the rest;
4. the operating system's file index (Spotlight, Windows Search, plocate);
5. a walk of the user's own folders (Documents, Desktop, Downloads, Pictures,
   cloud folders, and the folders this computer's other projects read from),
   for at most WALK_SECONDS, once for every file still missing.
A candidate is the file only when `sources.same_file` says so: same size, and
the same time to the millisecond or the same bytes at both ends.
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
                        if not name.startswith(".") and name.lower() not in _SKIP:
                            queue.append(Path(entry.path))
                    elif name in wanted:
                        found.setdefault(name, []).append(Path(entry.path))
        except OSError:
            continue
    return found


def _first(facts: dict, candidates: Iterable[Path]) -> Optional[Path]:
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and sources.same_file(facts, candidate):
            return Path(candidate)
    return None


def resolve(entry: dict, me: str, extra_roots: Iterable[Path] = (), *, walk_seconds: float = WALK_SECONDS,
            index=_index) -> dict:
    """{recorded path: path here} for every file of `entry` that this
    computer can find and has not recorded yet."""
    known = (entry or {}).get(sources.SOURCES) or {}
    here = {recorded: (info.get("at") or {}).get(me) for recorded, info in known.items()}
    missing = [recorded for recorded, path in here.items()
               if not (path and Path(path).is_file()) and not (Path(recorded).is_file() and
                                                               not recorded.startswith(sources.PAGE_ROOTS))]
    out: dict = {}

    def folders_found():
        return {Path(path).parent for path in list(out.values()) + [p for p in here.values() if p]
                if path and Path(path).is_file()}

    deadline = time.monotonic() + walk_seconds

    def look(group):
        for recorded in group:
            facts = known[recorded] or {}
            name = facts.get("name") or Path(recorded).name
            candidates = []
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

    # The user's files first, then the pyramids made from them: a pyramid
    # beside its original is found from where the original was.
    look([recorded for recorded in missing if not recorded.startswith("/derived/")])
    look([recorded for recorded in missing if recorded.startswith("/derived/")])
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
    size}], from the identities the project recorded. On the website this is
    what the page asks the user to find, by choosing the folder that holds
    them (client services/pickedFiles.js `find`)."""
    from plexora.server.models.project import Project, read_config
    from plexora import paths

    me = sources.app_key()
    root = Project.root_for(name)
    if root is None:
        return []
    entry = read_config(paths.config_path(root)).get(name) or {}
    out = []
    for recorded, info in (entry.get(sources.SOURCES) or {}).items():
        info = info or {}
        here = (info.get("at") or {}).get(me) or (recorded if me == "website" and
                                                  recorded.startswith(sources.PAGE_ROOTS) else None)
        if here and Path(here).is_file():
            continue
        out.append({"recorded": recorded, "name": info.get("name") or Path(recorded).name,
                    "size": info.get("size"), "mtime_ms": info.get("mtime_ms"),
                    "head": info.get("head"), "tail": info.get("tail")})
    return out


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
