"""A project as one file: what moves it between the website, the desktop app
and another computer.

A `.plexora` file is a zip of a project's own work -- its registry entry and
the files Plexora keeps for it (gates, regions, plugin state, its database)
-- and never its images or tables: those are the user's, already on the
computer that opens the bundle, and are found there by their identity
(server/models/sources.py; the desktop app finds them itself,
server/utils/relink.py, the website asks for their folder once). Caches that
are rebuilt when the project opens are left out too.

    plexora-project.json  {format, name, title, entry, app, exported, left_out}
    project/<path>        the project folder's files

`entry` is the project's registry entry as recorded (each app's paths kept in
`sources`), with the identity of every file it names, so the computer that
imports it can match them.

    export(name) -> bytes                 import_bundle(data, name=None) -> name
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from plexora import paths
from plexora.server.models import sources

FORMAT = 1
MANIFEST = "plexora-project.json"
FOLDER = "project/"
SUFFIX = ".plexora"
#: A file larger than this is left out (and named in `left_out`).
LARGEST = 64 << 20
#: Rebuilt when the project opens, so never carried: the spatial index
#: (data_model.SPATIAL_INDEX_DIR), the classifier's feature cache, the
#: thumbnail, the open marks (open_marks.py), and pyramidized copies.
_CACHES = ("spatial_index_v1/", "plugins/gating/classifier/features/")
_CACHE_NAMES = (".thumbnail", ".open-")
_CACHE_SUFFIXES = (".pyramid.ome.tiff", ".pyramid.ome.tif")


class BundleError(ValueError):
    """A file that is not a Plexora project bundle, or one that cannot be read."""


def _left_out(relative: str, size: int, named: set) -> Optional[str]:
    name = PurePosixPath(relative).name
    if relative.startswith(_CACHES) or name.startswith(_CACHE_NAMES) or name.endswith(_CACHE_SUFFIXES):
        return "rebuilt when the project opens"
    if relative in named:
        return "the user's own file, found by its identity"
    if size > LARGEST:
        return f"larger than {LARGEST >> 20} MB"
    return None


def _recorded_entry(name: str) -> tuple[dict, Path]:
    from plexora.server.models.project import Project, read_config

    root = Project.root_for(name)
    if root is None:
        raise KeyError(f"Unknown project: {name!r}")
    entry = read_config(paths.config_path(root)).get(name) or {}
    me = sources.app_key()
    identify = sources.identity if me == "website" else sources.file_identity
    wanted = None if me == "website" else (lambda value: sources._local_file(value)
                                           or value.startswith(sources.PAGE_ROOTS))
    return sources.record(entry, me, identify=identify, wanted=wanted), root


def export(name: str) -> bytes:
    """Project `name` as a `.plexora` file's bytes."""
    entry, root = _recorded_entry(name)
    folder = paths.project_state_dir(name)
    named = set()
    for value in sources._paths(entry, lambda v: True):
        try:
            named.add(Path(value).resolve().relative_to(folder.resolve()).as_posix())
        except (ValueError, OSError):
            continue
    buffer = io.BytesIO()
    left_out = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        if folder.is_dir():
            for path in sorted(folder.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(folder).as_posix()
                why = _left_out(relative, path.stat().st_size, named)
                if why:
                    if not relative.startswith(_CACHES) and not PurePosixPath(relative).name.startswith(_CACHE_NAMES):
                        left_out.append({"path": relative, "why": why})
                    continue
                bundle.write(path, FOLDER + relative)
        bundle.writestr(MANIFEST, json.dumps({
            "format": FORMAT, "name": name, "title": entry.get("title"), "entry": entry,
            "app": sources.app_key(), "exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "left_out": left_out}, indent=1))
    return buffer.getvalue()


def _free_name(wanted: str) -> str:
    from plexora.server.models.project import Project

    taken = set(Project.load_all())
    if wanted not in taken:
        return wanted
    for number in range(2, 1000):
        candidate = f"{wanted}_{number}"
        if candidate not in taken:
            return candidate
    raise BundleError(f"Too many projects called {wanted!r} already.")


def read_manifest(data) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data) as bundle:
            manifest = json.loads(bundle.read(MANIFEST).decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as error:
        raise BundleError("This is not a Plexora project file.") from error
    if manifest.get("format") != FORMAT or not isinstance(manifest.get("entry"), dict) or not manifest.get("name"):
        raise BundleError("This Plexora project file is from a version this one cannot read.")
    return manifest


def import_bundle(data, name: Optional[str] = None) -> str:
    """Register the project in a `.plexora` file (bytes or a path) in this
    app's own projects, under its name or the next free one, and put its
    files in place. Its images are found when it opens. Returns its name."""
    from plexora.server.models.project import config_transaction, read_config, write_config

    raw = Path(data).read_bytes() if isinstance(data, (str, Path)) else bytes(data)
    manifest = read_manifest(raw)
    original = str(manifest["name"])
    target = _free_name(name or original)
    root = paths.data_root()
    folder = paths.project_dir(target, root)
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        for member in bundle.infolist():
            if member.is_dir() or not member.filename.startswith(FOLDER):
                continue
            relative = PurePosixPath(member.filename[len(FOLDER):])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise BundleError("This Plexora project file names a place outside its project.")
            parts = list(relative.parts)
            if len(parts) == 1 and parts[0] == f"{original}.db":
                parts[0] = f"{target}.db"  # the database is named for its project
            destination = folder.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(bundle.read(member))
    with config_transaction():
        config = read_config(paths.config_path(root))
        config[target] = dict(manifest["entry"])
        write_config(paths.config_path(root), config)
    return target
