"""The places the user's projects are kept: Settings > Data's locations.

Plexora already reads projects from several roots (`paths.roots`): the data
root, the website's folder on this computer (Documents/Plexora/projects), and
any shared root a site provisioned. What this module adds is a name and an
identity for each, so the two apps' Settings pages can list them, a project
card can say where it lives, and the user can choose where new projects go:

- `data_root`: the data directory (`paths.data_root`).
- `library`: Documents/Plexora/projects, the workspace both apps share, or a
  folder `PLEXORA_LIBRARY_PATH` names.
- `legacy`: Documents/Plexora/Plexora/projects, where the website used to nest
  its projects. Listed so nothing made there disappears, never where a new
  project goes, and moved up beside the rest only on request (`tidy`).
- `workspace`: a workspace the user added here (settings.json "locations"),
  or, on the website, an extra folder the page mounted at /library/<id>.
- `shared`: a site-managed root, read-only.

A location's `path` is its root: the folder holding config.json and the
projects' folders. `workspace` is the folder above it when that is a
workspace's `projects/` -- the folder a user picks and recognises.

**Ids are stable.** A workspace's own id (workspace.json) when it has one, so
the website and the desktop app call one folder by one name; on the website,
the name of the folder's mount; otherwise `<kind>-` and the first twelve hex
characters of the SHA-1 of the root's resolved path.

**Nothing here deletes a project.** Removing a location forgets it: its files
stay where they are. A location that is not mounted today is listed as
unavailable and kept. `tidy` is the one move, and it copies or renames before
it removes anything.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from plexora import paths

KINDS = ("data_root", "library", "legacy", "workspace", "shared")

#: The settings.json "locations" block's version. Readers ignore it; it says
#: what a later one is looking at.
LOCATIONS_VERSION = 1

#: What a folder may hold and still count as empty to `tidy`: what an OS or a
#: write probe leaves behind.
_JUNK = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", ".localized"})


class LocationError(ValueError):
    """A request about a location that cannot be carried out, as a sentence
    the page shows. `status` is the HTTP answer the route gives."""

    status = 400


class UnknownLocation(LocationError):
    status = 404


class LocationsPinned(LocationError):
    """The environment decides what this asked to change."""

    status = 409


@dataclass(frozen=True)
class Location:
    id: str
    kind: str
    label: str
    path: Path
    workspace: Path | None
    rule: str
    available: bool
    writable: bool = False
    project_count: int | None = None
    is_default: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "path": str(self.path),
            "workspace": str(self.workspace) if self.workspace is not None else None,
            "available": self.available,
            "writable": self.writable,
            "project_count": self.project_count,
            "is_default": self.is_default,
            "rule": self.rule,
        }

    def summary(self) -> dict:
        """What a project card says about where it lives."""
        return {"id": self.id, "label": self.label, "kind": self.kind}


def on_the_website() -> bool:
    return os.environ.get("PLEXORA_BACKEND") == "browser"


def _resolved(path) -> Path | None:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _digest_id(kind: str, root: Path) -> str:
    return f"{kind}-{hashlib.sha1(str(root).encode('utf-8')).hexdigest()[:12]}"


def _record_root(record: dict) -> Path | None:
    folder = _resolved(record.get("path"))
    return folder / paths.PROJECTS_DIRNAME if folder is not None else None


def _record_id(record: dict) -> str:
    ident = record.get("id")
    if isinstance(ident, str) and ident.strip():
        return ident.strip()
    root = _record_root(record)
    return _digest_id("workspace", root if root is not None else Path(str(record.get("path"))))


def _workspace_of(root: Path) -> Path | None:
    """The workspace a root is the `projects/` of, or None."""
    return root.parent if root.name == paths.PROJECTS_DIRNAME else None


def _tail(path: Path, parts: int) -> str:
    return "/".join(path.parts[-parts:]) or str(path)


def _kind_of(rule: str) -> str:
    if rule == paths.RULE_LEGACY_NESTED:
        return "legacy"
    if rule == paths.RULE_REGISTERED_WORKSPACE:
        return "workspace"
    if rule == paths.RULE_WEBSITE_LIBRARY:
        return "library"
    # Named by PLEXORA_LIBRARY_PATH: on the website, the folders the user
    # added beside the one their projects are kept in.
    return "workspace" if on_the_website() else "library"


def _label(kind: str, root: Path, workspace: Path | None, marker: dict | None,
           record: dict | None) -> str:
    if kind == "workspace" and record is not None and str(record.get("label") or "").strip():
        return str(record["label"]).strip()
    if kind == "data_root":
        return "Data folder"
    if kind == "legacy":
        return _tail(workspace or root, 3)
    if kind == "library" and workspace is not None:
        return _tail(workspace, 2)
    if marker is not None and str(marker.get("name") or "").strip():
        return str(marker["name"]).strip()
    return (workspace or root).name or str(root)


class _Builder:
    """Collects locations in order, each root once, each id once."""

    def __init__(self, detail: bool, default_root: Path | None):
        self.detail = detail
        self.default_root = default_root
        self.out: list[Location] = []
        self.roots: set[Path] = set()
        self.ids: set[str] = set()

    def add(self, root: Path, kind: str, rule: str, *, available: bool,
            record: dict | None = None) -> None:
        if root in self.roots:
            return
        workspace = _workspace_of(root) if kind != "shared" else None
        marker = paths.read_workspace(workspace) if workspace is not None and available else None
        if record is not None:
            ident = _record_id(record)
        elif marker is not None:
            ident = str(marker["id"])
        elif on_the_website() and kind == "workspace":
            # The page mounts each folder it added at /library/<its id>.
            ident = root.name
        else:
            ident = _digest_id(kind, root)
        if ident in self.ids:
            # A copied workspace carries its original's id: the second copy
            # is told apart by where it is.
            ident = _digest_id(kind, root)
        writable = False
        count = None
        if self.detail and available:
            # Never `is_writable` on a shared root: it makes the folder it
            # asks about, and a shared root is somebody else's to provision.
            writable = _shared_writable(root) if kind == "shared" else paths.is_writable(root)
            count = paths._registry_size(root) or 0
        self.roots.add(root)
        self.ids.add(ident)
        self.out.append(Location(
            id=ident, kind=kind, label=_label(kind, root, workspace, marker, record),
            path=root, workspace=workspace, rule=rule, available=available,
            writable=writable, project_count=count,
            is_default=self.default_root is not None and root == self.default_root))


def _shared_writable(root: Path) -> bool:
    """Whether a shared root takes writes, without creating it."""
    from plexora.server.models import data_migration

    return root.is_dir() and data_migration.can_write(root)


def default_root() -> Path:
    """Where a project made now, with no location named, is written."""
    return paths.new_projects_root()


def locations(detail: bool = True) -> list[Location]:
    """Every location, in the order projects are looked for: the data root,
    the user's libraries and workspaces, then shared roots.

    `detail` adds what costs a probe or a read per location -- whether it can
    be written, how many projects it lists, which one new projects go to --
    and is what Settings shows; a project card needs only id, label and kind.
    """
    own = paths.data_root_resolution()
    chosen = default_root() if detail else None
    build = _Builder(detail, chosen)
    build.add(own.path, "data_root", own.rule, available=True)

    found = {resolution.path for resolution in paths.library_root_resolutions()}
    pinned = os.environ.get(paths.ENV_LIBRARY_PATH) is not None
    records = {} if pinned else {
        _record_root(record): record for record in paths.registered_locations()
        if _record_root(record) is not None}
    for candidate in paths.library_candidates():
        if candidate.path == own.path:
            continue
        kind = _kind_of(candidate.rule)
        # Looked up whatever the kind: Documents/Plexora added by hand is
        # listed under its own kind, and comes off the list the same way.
        record = records.get(candidate.path)
        if record is not None:
            # Added by the user: listed whether or not it is mounted today, and
            # usable once its workspace is there, projects or not.
            build.add(candidate.path, kind, candidate.rule, record=record,
                      available=Path(record["path"]).expanduser().is_dir())
        elif candidate.path in found:
            build.add(candidate.path, kind, candidate.rule, available=True)
        elif on_the_website() and candidate.path.is_dir():
            # A folder the page has just added and mounted, with no projects
            # in it yet: a place a new project can be saved to already.
            build.add(candidate.path, kind, candidate.rule, available=True)
        elif chosen is not None and candidate.path == chosen:
            # Documents/Plexora/projects before anything is in it: new projects
            # go there, so it is listed from the first run.
            build.add(candidate.path, kind, candidate.rule, available=candidate.path.is_dir())

    for resolution in paths.shared_root_resolutions():
        build.add(resolution.path, "shared", resolution.rule,
                  available=resolution.path.is_dir())
    return build.out


def find(location_id: str, detail: bool = True) -> Location:
    wanted = str(location_id or "").strip()
    if not wanted:
        raise LocationError("Name a location.")
    for location in locations(detail=detail):
        if location.id == wanted:
            return location
    raise UnknownLocation("That location is not in the list any more.")


def default_save() -> dict:
    """`{id, label, path}` of where new projects go."""
    listed = locations(detail=True)
    for location in listed:
        if location.is_default:
            return {"id": location.id, "label": location.label, "path": str(location.path)}
    root = default_root()
    return {"id": _digest_id("data_root", root), "label": "Data folder", "path": str(root)}


def summaries() -> dict:
    """Root -> `{id, label, kind}`, for the project cards: one pass for a
    whole listing, with no write probe and no registry read."""
    return {location.path: location.summary() for location in locations(detail=False)}


def root_for_new_projects(location_id) -> Path | None:
    """The root a request's new projects go to, for a location id it named:
    one of the user's own, mounted and writable. None for no id."""
    if location_id in (None, ""):
        return None
    location = find(location_id)
    if location.kind not in ("data_root", "library", "workspace"):
        raise LocationError("New projects can be saved only in one of your own folders.")
    if not location.available:
        raise LocationError(f"{location.label} is not available right now.")
    if not location.writable:
        raise LocationError(f"Plexora cannot write to {location.label}.")
    return location.path


# -- changing the list -----------------------------------------------------


def _refuse_on_the_website() -> None:
    if on_the_website():
        raise LocationError("The website keeps its folders in this browser and "
                            "adds or removes them itself.")


def _refuse_when_pinned() -> None:
    if os.environ.get(paths.ENV_LIBRARY_PATH) is not None:
        raise LocationsPinned(
            f"{paths.ENV_LIBRARY_PATH} is set for this server, and it names the "
            f"folders Plexora lists. Unset it and restart Plexora to change them here.")


def _locations_block(settings: dict) -> dict:
    raw = settings.get(paths.LOCATIONS_KEY)
    items = raw.get("locations") if isinstance(raw, dict) else raw
    return {"version": LOCATIONS_VERSION,
            "locations": [dict(item) for item in (items if isinstance(items, list) else [])
                          if isinstance(item, dict)]}


def _workspace_folder(raw_path) -> Path:
    """The workspace a typed path means: the folder itself, or the folder
    above when the `projects/` folder inside one was picked."""
    folder = _resolved(str(raw_path or "").strip()) if str(raw_path or "").strip() else None
    if folder is None:
        raise LocationError("Enter the path of a folder.")
    if not folder.is_dir():
        raise LocationError(f"There is no folder at {folder}.")
    if (folder.name == paths.PROJECTS_DIRNAME
            and (folder / paths.CONFIG_FILENAME).exists()
            and not (folder / paths.PROJECTS_DIRNAME / paths.CONFIG_FILENAME).exists()):
        folder = folder.parent
    if (paths.read_workspace(folder) is None
            and not (folder / paths.PROJECTS_DIRNAME / paths.CONFIG_FILENAME).exists()):
        raise LocationError(
            f"{folder} is not a Plexora workspace: it has no workspace.json and "
            f"no projects folder with a config.json in it.")
    return folder


def register(raw_path) -> Location:
    """Add an existing workspace to the list. Writes its workspace.json when
    it has none and the folder can be written. Already listed: that location,
    unchanged."""
    _refuse_on_the_website()
    _refuse_when_pinned()
    folder = _workspace_folder(raw_path)
    root = folder / paths.PROJECTS_DIRNAME
    for location in locations(detail=False):
        if location.path == root:
            return find(location.id)
    marker = paths.read_workspace(folder)
    if marker is None:
        try:
            marker = paths.write_workspace(folder, "desktop")
        except OSError:
            marker = None    # read-only: listed all the same, by its path
    taken = {location.id for location in locations(detail=False)}
    ident = str(marker["id"]) if marker is not None else _digest_id("workspace", root)
    if ident in taken:
        ident = _digest_id("workspace", root)
    record = {
        "id": ident,
        "kind": "workspace",
        "path": str(folder),
        "label": str((marker or {}).get("name") or folder.name),
        "addedAt": paths._utc_now(),
    }
    with paths.settings_lock():
        settings = paths.read_settings()
        block = _locations_block(settings)
        block["locations"].append(record)
        settings[paths.LOCATIONS_KEY] = block
        paths.write_settings(settings)
    paths.reset_libraries()
    return find(ident)


def unregister(location_id) -> dict:
    """Take a workspace the user added off the list. Its files are not
    touched. A default save location that pointed into it reverts to the
    usual one."""
    _refuse_on_the_website()
    _refuse_when_pinned()
    wanted = str(location_id or "").strip()
    with paths.settings_lock():
        settings = paths.read_settings()
        block = _locations_block(settings)
        kept, removed = [], None
        for record in block["locations"]:
            if removed is None and _record_id(record) == wanted:
                removed = record
            else:
                kept.append(record)
        if removed is None:
            if any(location.id == wanted for location in locations(detail=False)):
                raise LocationError("Only a workspace added here can be taken off the list.")
            raise UnknownLocation("That location is not in the list any more.")
        block["locations"] = kept
        settings[paths.LOCATIONS_KEY] = block
        root = _record_root(removed)
        chosen = _resolved(settings.get(paths.NEW_PROJECTS_KEY)) \
            if isinstance(settings.get(paths.NEW_PROJECTS_KEY), str) else None
        if chosen is not None and root is not None and chosen == root:
            settings.pop(paths.NEW_PROJECTS_KEY, None)
        paths.write_settings(settings)
    paths.reset_libraries()
    return {**removed, "id": _record_id(removed)}


def set_default(location_id) -> Location:
    """Make a location where new projects go (`settings["new_projects_dir"]`,
    which `paths.new_projects_root` honours first)."""
    _refuse_on_the_website()
    if paths._served_from_elsewhere():
        raise LocationsPinned("This Plexora is reached from another computer, so "
                              "new projects stay in its data folder.")
    location = find(location_id)
    if location.kind == "legacy":
        raise LocationError("New projects do not go into the older nested folder. "
                            "Tidy it up, then choose Documents/Plexora.")
    if location.kind == "shared":
        raise LocationError("A shared folder is read-only. Choose one of your own.")
    if not location.available:
        raise LocationError(f"{location.label} is not available right now.")
    if not location.writable:
        raise LocationError(f"Plexora cannot write to {location.label}.")
    with paths.settings_lock():
        settings = paths.read_settings()
        settings[paths.NEW_PROJECTS_KEY] = str(location.path)
        paths.write_settings(settings)
    return find(location.id)


# -- tidying up the older nested folder ------------------------------------


def _holds_nothing(folder: Path) -> bool:
    """Whether `folder` is absent or holds nothing worth keeping: what an OS
    leaves behind, write probes, and a registry or dataset list that names
    nothing."""
    try:
        entries = list(folder.iterdir())
    except FileNotFoundError:
        return True
    except OSError:
        return False
    for entry in entries:
        name = entry.name
        if name in _JUNK or name.startswith(paths._PROBE_PREFIX):
            continue
        if name == paths.CONFIG_FILENAME and entry.is_file() and paths._registry_size(folder) == 0:
            continue
        if name == "datasets.json" and entry.is_file() and _no_datasets(entry):
            continue
        return False
    return True


def _no_datasets(path: Path) -> bool:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    inner = data.get("datasets")
    return not inner if isinstance(inner, dict) else not any(
        key != "version" for key in data)


def _clear(folder: Path) -> None:
    """Remove a folder `_holds_nothing` passed: its files one by one, then
    the folder. Never a tree: anything unexpected stops it."""
    if not folder.exists():
        return
    for entry in folder.iterdir():
        entry.unlink()
    folder.rmdir()


def _same_filesystem(source: Path, target_parent: Path) -> bool:
    try:
        return os.stat(source).st_dev == os.stat(target_parent).st_dev
    except OSError:
        return False


def _census(folder: Path) -> tuple[int, int]:
    """(files, bytes) under `folder`."""
    files = size = 0
    for base, _dirs, names in os.walk(folder):
        for name in names:
            files += 1
            size += os.lstat(os.path.join(base, name)).st_size
    return files, size


def _move(source: Path, target: Path) -> None:
    """One folder to where it belongs, `target` being absent.

    A rename when both are on one filesystem -- the ordinary case, the two
    being one folder apart -- which is a single step that cannot leave half
    a folder anywhere. Otherwise copy, check the copy holds as many files and
    bytes as the original, and only then remove the original; a copy that
    does not match is removed instead and the move refused.
    """
    if _same_filesystem(source, target.parent):
        os.rename(source, target)
        return
    folder = source.is_dir()
    try:
        if folder:
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)
        matches = (_census(source) == _census(target) if folder
                   else source.stat().st_size == target.stat().st_size)
    except BaseException:
        # Half a copy is worse than none: it would be read as the projects.
        _discard(target)
        raise
    if not matches:
        _discard(target)
        raise LocationError(f"The copy of {source.name} did not match the original, "
                            f"so the older folder was left as it was.")
    if folder:
        shutil.rmtree(source)
    else:
        source.unlink()


def _discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def tidy(location_id) -> dict:
    """Move the older nested folder's projects and pyramids up beside the
    rest: Documents/Plexora/Plexora/{projects,pyramids} ->
    Documents/Plexora/{projects,pyramids}.

    Only when Documents/Plexora/projects holds nothing yet: two registries
    are never merged here, for data_migration's reason. A pyramid already in
    Documents/Plexora/pyramids under the same key is kept and the older copy
    left where it is. The older folder is removed once nothing is left in it.
    Returns `{moved, left, location}`, `location` being the projects' new
    place.
    """
    _refuse_on_the_website()
    location = find(location_id)
    if location.kind != "legacy" or location.workspace is None:
        raise LocationError("Only the older nested folder (Documents/Plexora/Plexora) "
                            "can be tidied up.")
    source = location.workspace
    target = source.parent
    projects = target / paths.PROJECTS_DIRNAME
    if not _holds_nothing(projects):
        raise LocationError(
            f"{projects} already holds projects, so the older folder was left as it "
            f"is. Move what you need from {source} by hand.")

    moved: list[str] = []
    left: list[str] = []
    try:
        _clear(projects)
        _move(source / paths.PROJECTS_DIRNAME, projects)
        moved.append(paths.PROJECTS_DIRNAME)
        pyramids = source / "pyramids"
        if pyramids.is_dir():
            destination = target / "pyramids"
            destination.mkdir(exist_ok=True)
            for entry in sorted(pyramids.iterdir()):
                if entry.name in _JUNK:
                    continue
                if (destination / entry.name).exists():
                    left.append(f"pyramids/{entry.name}")
                    continue
                _move(entry, destination / entry.name)
                moved.append(f"pyramids/{entry.name}")
            if _holds_nothing(pyramids):
                _clear(pyramids)
        # The older folder's own marker goes with its projects when the
        # workspace has none, so the id the website knew it by is kept.
        marker = source / paths.WORKSPACE_FILENAME
        if marker.is_file():
            if not (target / paths.WORKSPACE_FILENAME).exists():
                os.replace(marker, target / paths.WORKSPACE_FILENAME)
                moved.append(paths.WORKSPACE_FILENAME)
            elif _holds_nothing_but(source, {paths.WORKSPACE_FILENAME}):
                marker.unlink()
        if _holds_nothing(source):
            _clear(source)
        else:
            # What the user put there themselves, and pyramids kept above.
            left.extend(sorted(entry.name for entry in source.iterdir()
                               if entry.name not in _JUNK and entry.name != "pyramids"))
    except (OSError, LocationError) as exc:
        try:
            projects.mkdir(exist_ok=True)       # as it was: there, and empty
        except OSError:
            pass
        if isinstance(exc, LocationError):
            raise
        raise LocationError(f"The older folder could not be moved: {exc}") from exc
    finally:
        paths.reset_libraries()

    moved_to = next((place for place in locations(detail=True) if place.path == projects.resolve()),
                    None)
    return {"moved": moved, "left": left,
            "location": moved_to.to_dict() if moved_to is not None else None}


def _holds_nothing_but(folder: Path, allowed: set) -> bool:
    try:
        return all(entry.name in allowed or entry.name in _JUNK for entry in folder.iterdir())
    except OSError:
        return False
