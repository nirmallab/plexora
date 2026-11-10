"""Datasets: the folder a cohort of projects lives in.

One image is one project. That was true from the start and stays true -- a
project is still the thing a viewer opens, the thing a plugin is handed, the
thing that owns a mask and a feature table. What was missing is the level
above it: forty slides from one trial, a TMA series, an imaging run. Those
belong together, and until now the only place that fact could live was in the
project names.

A dataset is an **organizational container and nothing else**. It holds names,
not data. Deleting one releases its members and touches no file of theirs;
removing a project from one is not deleting the project. Nothing in the viewer,
the adapters or the plugin API reads this file, and a Plexora with an empty
`datasets.json` behaves exactly as it did before there was one.

**Why a sibling file rather than a key in config.json.** Every top-level key of
config.json is a project -- `Project.load_all` and `all_projects` enumerate it
that way and so does everything downstream. A `"datasets"` key would become a
phantom project on the Open Project page with no image and no way to delete it.
So: `<data_root>/datasets.json`, beside `remotes.json` and `figures/`.

**The user's own roots only.** A shared root is somebody else's install,
mounted read-only; the user's own grouping of what they found there is theirs.
`paths.figures_root()` and `captures_root()` are the same rule for the same
reason, and shared projects can still be members -- membership is recorded
here, not over there.

The user's own roots are two when the website keeps its projects on this
computer (`paths.owned_roots`: the data root, then Documents/Plexora/projects).
Each has a `datasets.json`, and both are read as one registry. The website
reads only the second, so a dataset is kept there once a project of that
folder is in it, and one made empty goes where new projects do
(`paths.new_projects_root`): what the user groups in the desktop app is
grouped on the website too. A dataset of the data root's projects alone stays
in the data root, and one kept in the library never moves back: a library
whose config.json cannot be read for a moment must not take a grouping off the
website.

**Members are recorded by name and by id.** `projects` lists names, as it
always has, and `projectIds` maps each name to its project's id
(`Project.id`) once the project has one. Names stay what everything reads;
the id is what recognises a member that is listed under another name where
it is seen now -- the same project through another location, or brought in
by a bundle. A reader that knows only names reads the file as before, and a
member recorded only by id (under `projectIds[""]`, or `projectIds` as a bare
list) is read too, and kept as it was on every write.

**Membership is pruned in the view and never on read.** A shared root that is
not mounted this morning makes its projects vanish from `Project.load_all()`.
If reading the file rewrote it to match, one unmounted drive would permanently
erase the grouping; instead the names stay on disk and are simply not returned
while the projects are not there. The prune lands the next time that record is
written for some other reason, by which point the name really is gone.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from plexora import paths
from plexora.server.models.project import _CONFIG_LOCK, read_config, write_config

FILENAME = "datasets.json"

#: Bumped only if the on-disk shape changes incompatibly. Readers ignore it;
#: it exists so a future one can tell what it is looking at.
VERSION = 1

#: Longer than any name anybody types and short enough to render in a folder
#: card without the card deciding the layout.
MAX_NAME = 120


class DatasetError(ValueError):
    """A name that cannot be used, or an id that names nothing.

    A `ValueError` because that is what the routes and the CLI already turn
    into a 400 and an exit code 2 -- this is user input being refused, not a
    bug.
    """


@dataclass(frozen=True)
class Dataset:
    """One folder: a name, and the projects the user put in it.

    `projects` is a tuple of **project names**, in the order they were added.
    Order is not meaningful yet and is preserved anyway, because the first
    dataset-level feature anybody will ask for is next/previous sample and
    that wants an order somebody chose.
    """

    id: str
    name: str
    created_at: str = ""
    description: str = ""
    #: Reserved for dataset-wide settings -- a shared marker mapping, a
    #: phenotype palette, batch configuration. Round-trips verbatim so that
    #: whatever writes it first does not have to teach this module about it.
    meta: Mapping[str, Any] = field(default_factory=dict)
    projects: tuple[str, ...] = ()
    #: Project name -> its id, for the members whose id is known; and the
    #: ids of members recorded by id alone, under "" (see the module
    #: docstring). Written beside `projects`, never instead of it.
    project_ids: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, dataset_id: str, raw: Mapping[str, Any] | None) -> "Dataset | None":
        """Read one record, or None for one too damaged to mean anything.

        A record with no name is not a dataset -- the name is the only thing
        the user ever sees of it -- and returning None rather than raising
        keeps one hand-edited entry from making the whole page 500.
        """
        raw = raw or {}
        name = str(raw.get("name") or "").strip()
        if not name:
            return None
        projects = raw.get("projects") or ()
        if isinstance(projects, str):  # a hand-edit, read charitably
            projects = [projects]
        seen, ordered = set(), []
        for entry in projects:
            entry = str(entry).strip()
            if entry and entry not in seen:
                seen.add(entry)
                ordered.append(entry)
        meta = raw.get("meta")
        return cls(
            id=str(dataset_id),
            name=name,
            created_at=str(raw.get("createdAt") or ""),
            description=str(raw.get("description") or ""),
            meta=dict(meta) if isinstance(meta, Mapping) else {},
            projects=tuple(ordered),
            project_ids=_read_ids(raw.get("projectIds"), ordered),
        )

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"name": self.name, "projects": list(self.projects)}
        ids: dict[str, Any] = {name: self.project_ids[name] for name in self.projects
                               if isinstance(self.project_ids.get(name), str)}
        if self.unnamed_ids:
            ids[""] = list(self.unnamed_ids)
        if ids:
            out["projectIds"] = ids
        if self.created_at:
            out["createdAt"] = self.created_at
        if self.description:
            out["description"] = self.description
        if self.meta:
            out["meta"] = dict(self.meta)
        return out

    def to_wire(self) -> dict:
        """The JSON one route, one CLI `--json` and one JS controller share."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "createdAt": self.created_at,
            "meta": dict(self.meta),
            "projects": list(self.projects),
            "projectCount": len(self.projects),
        }

    def with_projects(self, projects: Iterable[str]) -> "Dataset":
        return replace(self, projects=tuple(projects))

    @property
    def unnamed_ids(self) -> tuple:
        """Ids of members recorded by id alone, with no name beside them."""
        return tuple(self.project_ids.get("") or ())


def _read_ids(raw, names) -> dict:
    """`projectIds` as read: `{name: id}`, plus `{"": [id, ...]}` for ids
    with no name -- a bare list, or a name the record does not list."""
    ids: dict[str, Any] = {}
    loose: list = []
    listed = set(names)
    if isinstance(raw, Mapping):
        for name, ident in raw.items():
            if name == "" and isinstance(ident, (list, tuple)):
                loose.extend(str(each).strip() for each in ident
                             if isinstance(each, str) and each.strip())
                continue
            if not isinstance(ident, str) or not ident.strip():
                continue
            if str(name) in listed:
                ids[str(name)] = ident.strip()
            else:
                loose.append(ident.strip())
    elif isinstance(raw, (list, tuple)):
        loose.extend(str(ident).strip() for ident in raw
                     if isinstance(ident, str) and ident.strip())
    if loose:
        ids[""] = tuple(dict.fromkeys(loose))
    return ids


def datasets_path(root=None) -> Path:
    """Where a registry lives: `root`'s, the user's data root's by default."""
    return (Path(root) if root is not None else paths.data_root()) / FILENAME


def _roots(root=None) -> list:
    """The registries read and written: `root`'s alone when it is named, else
    every root the user owns, the data root first (`paths.owned_roots`)."""
    if root is not None:
        return [Path(root)]
    return list(paths.owned_roots())


# -- reading -------------------------------------------------------------


def _read(root=None) -> dict:
    """The raw `{"version": …, "datasets": {…}}` document, or an empty one.

    Tolerates a file that is just the inner mapping, which is what a
    hand-written one tends to be.
    """
    raw = read_config(datasets_path(root))
    if not isinstance(raw, Mapping):
        return {}
    inner = raw.get("datasets")
    if isinstance(inner, Mapping):
        return dict(inner)
    # No "datasets" key at all: either an empty file or somebody's hand-written
    # flat mapping. Reading it as the latter costs nothing and loses nothing.
    return {k: v for k, v in raw.items() if k != "version" and isinstance(v, Mapping)}


@dataclass
class _Files:
    """The registries one change read, so it can write back only what moved.

    `homes` is the root whose file holds each dataset; `held` is what each
    root's file held, as records, when it was read.
    """

    roots: list
    homes: dict
    held: dict


def _open(root=None) -> tuple:
    """Every dataset across `_roots(root)`, keyed by id, and where each is.

    An id in two files (a hand copy) is read once, from the earlier root.
    """
    files = _Files(roots=_roots(root), homes={}, held={})
    records: dict[str, Dataset] = {}
    for home in files.roots:
        files.held[home] = {}
        for dataset_id, raw in _read(home).items():
            dataset = Dataset.from_dict(dataset_id, raw)
            if dataset is None:
                continue
            files.held[home][dataset.id] = dataset.to_dict()
            if dataset.id not in records:
                records[dataset.id] = dataset
                files.homes[dataset.id] = home
    return records, files


def _home(dataset: Dataset, files: _Files, members: Mapping) -> Path:
    """The root whose file keeps `dataset` (see the module docstring)."""
    own = files.roots[0]
    current = files.homes.get(dataset.id)
    if current is not None and current != own:
        return current
    for library in files.roots[1:]:
        if any(name in members[library] for name in dataset.projects):
            return library
    # Only when a library is read: asking makes the library's folder.
    if current is None and not dataset.projects and len(files.roots) > 1:
        target = paths.new_projects_root()
        if target in files.roots:
            return target
    return own


def _store(records: Mapping[str, Dataset], files: _Files) -> None:
    """Write each registry whose datasets changed.

    Libraries first: a dataset moving out of the data root is written into the
    library before it is taken out of the data root, so a write that fails
    between the two leaves it in both files, where reading finds it once.
    """
    records = _with_ids(records)
    members = {library: set(read_config(paths.config_path(library)))
               for library in files.roots[1:]}
    placed: dict = {home: {} for home in files.roots}
    for dataset in records.values():
        placed[_home(dataset, files, members)][dataset.id] = dataset
    for home in reversed(files.roots):
        kept = placed[home]
        if {ds.id: ds.to_dict() for ds in kept.values()} != files.held[home]:
            _write(kept, home)


def _write(records: Mapping[str, Dataset], root=None) -> None:
    write_config(datasets_path(root), {
        "version": VERSION,
        "datasets": {ds.id: ds.to_dict() for ds in records.values()},
    })


def _project_ids() -> dict:
    """Project name -> id, for every visible project that has one; the
    user's own root first, as `Project.load_all` merges them. Read through
    the registry cache, so a listing that needs it twice parses nothing
    again."""
    from plexora.server.models.project import _parsed, clean_project_id

    out: dict = {}
    seen: set = set()
    for root in paths.roots():
        for name, entry in _parsed(paths.config_path(root)).items():
            if name in seen:
                continue
            seen.add(name)
            ident = clean_project_id(entry.get("id")) if isinstance(entry, Mapping) else None
            if ident:
                out[name] = ident
    return out


def _with_ids(records: Mapping[str, Dataset]) -> dict:
    """Every record with its members' ids filled in from the projects as
    they are now. An id already recorded for a member whose project cannot be
    read today (a root not mounted) is kept, for the reason membership is
    never pruned on read."""
    if not any(ds.projects for ds in records.values()):
        return dict(records)
    known = _project_ids()
    out = {}
    for dataset_id, dataset in records.items():
        ids = {name: known.get(name) or dataset.project_ids.get(name)
               for name in dataset.projects}
        ids = {name: ident for name, ident in ids.items() if ident}
        if dataset.unnamed_ids:
            ids[""] = dataset.unnamed_ids
        out[dataset_id] = replace(dataset, project_ids=ids)
    return out


def load_all(root=None, known: Iterable[str] | None = None) -> dict:
    """Every dataset, keyed by id, newest membership pruned to `known`.

    `known` is the set of project names that currently resolve -- pass
    `Project.load_all()` to drop members whose projects have been deleted or
    whose shared root is not mounted. **This never writes.** See the module
    docstring: a pruning read would turn an unmounted drive into permanent
    data loss.
    """
    names = set(known) if known is not None else None
    records, _ = _open(root)
    if names is None:
        return records
    by_id = None
    out = {}
    for dataset_id, dataset in records.items():
        members = [name for name in dataset.projects if name in names]
        # A member not seen under its recorded name, or recorded by id
        # alone: found by its id when a visible project has it. Only looked
        # up when there is one, so the ordinary listing reads no registry.
        missing = [dataset.project_ids[name] for name in dataset.projects
                   if name not in names and isinstance(dataset.project_ids.get(name), str)]
        missing.extend(dataset.unnamed_ids)
        if missing:
            if by_id is None:
                by_id = {ident: name for name, ident in _project_ids().items()}
            for ident in missing:
                name = by_id.get(ident)
                if name is not None and name in names and name not in members:
                    members.append(name)
        out[dataset_id] = dataset.with_projects(members)
    return out


def find(dataset_id: str, root=None, known: Iterable[str] | None = None) -> "Dataset | None":
    return load_all(root, known).get(str(dataset_id))


def get(dataset_id: str, root=None, known: Iterable[str] | None = None) -> Dataset:
    """One dataset, or a DatasetError naming what does exist."""
    datasets = load_all(root, known)
    dataset = datasets.get(str(dataset_id))
    if dataset is not None:
        return dataset
    listed = ", ".join(sorted(ds.name for ds in datasets.values())) or "none"
    raise DatasetError(f"no dataset with id {dataset_id!r} (datasets: {listed})")


def find_by_name(name: str, root=None, known: Iterable[str] | None = None) -> "Dataset | None":
    """Look one up the way a person names it. Case-insensitive, because
    `create` already refuses two names that differ only in case."""
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return None
    for dataset in load_all(root, known).values():
        if dataset.name.casefold() == wanted:
            return dataset
    return None


def resolve(name_or_id: str, root=None, known: Iterable[str] | None = None) -> Dataset:
    """A dataset by id or by name -- what a CLI argument and a `dataset=`
    keyword both are. Id first: it is the unambiguous one."""
    text = str(name_or_id or "").strip()
    if not text:
        raise DatasetError("name a dataset")
    datasets = load_all(root, known)
    if text in datasets:
        return datasets[text]
    for dataset in datasets.values():
        if dataset.name.casefold() == text.casefold():
            return dataset
    listed = ", ".join(sorted(ds.name for ds in datasets.values())) or "none"
    raise DatasetError(f"no dataset named {text!r} (datasets: {listed})")


def membership(root=None, known: Iterable[str] | None = None) -> dict:
    """Project name -> the dataset holding it. The reverse index.

    One pass over the file, so a listing of two hundred projects does not do
    two hundred lookups. Single-parent membership makes this a plain dict; if
    a hand-edited file lists one project twice, the first dataset wins and the
    duplicate is left alone until something writes.
    """
    out: dict[str, Dataset] = {}
    for dataset in load_all(root, known).values():
        for name in dataset.projects:
            out.setdefault(name, dataset)
    return out


# -- validation ----------------------------------------------------------


def _clean_name(name: str, *, datasets: Mapping[str, Dataset], allow: str = "") -> str:
    """A usable dataset name, or a DatasetError saying why not.

    Unique after `casefold()`: "Melanoma" and "melanoma" are one folder as far
    as anybody reading the page is concerned, and two of them is a bug report.
    `allow` is the id being renamed, which may of course keep its own name.
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        raise DatasetError("a dataset needs a name")
    if len(cleaned) > MAX_NAME:
        raise DatasetError(f"that name is too long (limit {MAX_NAME} characters)")
    wanted = cleaned.casefold()
    for dataset in datasets.values():
        if dataset.id != allow and dataset.name.casefold() == wanted:
            raise DatasetError(f"there is already a dataset called {dataset.name!r}")
    return cleaned


def _known_projects() -> set:
    """Every project name that currently resolves, across all roots."""
    from plexora.server.models.project import Project

    return set(Project.load_all())


def _check_projects(names: Iterable[str], known: Iterable[str] | None) -> tuple:
    """Clean a list of project names, refusing any that is not registered."""
    resolved = set(known) if known is not None else _known_projects()
    out, seen = [], set()
    for raw in names or ():
        name = str(raw).strip()
        if not name or name in seen:
            continue
        if name not in resolved:
            raise DatasetError(f"no project named {name!r}")
        seen.add(name)
        out.append(name)
    return tuple(out)


# -- writing -------------------------------------------------------------


def create(name: str, *, description: str = "", projects: Iterable[str] = (),
           root=None, known: Iterable[str] | None = None) -> Dataset:
    """Make a folder. Every named project must already exist."""
    with _CONFIG_LOCK:
        records, files = _open(root)
        clean = _clean_name(name, datasets=records)
        members = _check_projects(projects, known)
        dataset = Dataset(
            id=uuid.uuid4().hex[:12],
            name=clean,
            created_at=_now(),
            description=str(description or "").strip(),
            projects=members,
        )
        # Single parent: joining this one is leaving whatever held them.
        records = _released(records, members)
        records[dataset.id] = dataset
        _store(records, files)
        return dataset


def rename(dataset_id: str, name: str, root=None) -> Dataset:
    with _CONFIG_LOCK:
        records, files = _open(root)
        dataset = records.get(str(dataset_id))
        if dataset is None:
            raise DatasetError(f"no dataset with id {dataset_id!r}")
        clean = _clean_name(name, datasets=records, allow=dataset.id)
        updated = replace(dataset, name=clean)
        records[updated.id] = updated
        _store(records, files)
        return updated


def describe(dataset_id: str, *, description: str | None = None,
             meta: Mapping[str, Any] | None = None, root=None) -> Dataset:
    """Set the free-text description, the `meta` bag, or both.

    `meta` MERGES, for the same reason `Project.patch` does: the palette and
    the marker mapping will be written by different screens, and a whole-bag
    replacement means whichever saves second erases the other. A key set to
    None is removed, which is the only way to take one back out.
    """
    with _CONFIG_LOCK:
        records, files = _open(root)
        dataset = records.get(str(dataset_id))
        if dataset is None:
            raise DatasetError(f"no dataset with id {dataset_id!r}")
        updated = dataset
        if description is not None:
            updated = replace(updated, description=str(description).strip())
        if meta is not None:
            merged = {**updated.meta, **dict(meta)}
            updated = replace(updated, meta={k: v for k, v in merged.items() if v is not None})
        records[updated.id] = updated
        _store(records, files)
        return updated


def remove(dataset_id: str, root=None) -> "Dataset | None":
    """Delete the folder. **The projects in it are not touched** -- they go
    back to being unassociated, which is where they started."""
    with _CONFIG_LOCK:
        records, files = _open(root)
        dataset = records.pop(str(dataset_id), None)
        if dataset is None:
            return None
        _store(records, files)
        return dataset


def assign(projects: Iterable[str], dataset_id: str | None, root=None,
           known: Iterable[str] | None = None) -> "Dataset | None":
    """Put projects in a dataset, or take them out of every one.

    The single verb behind drag-and-drop, the Move picker and "Remove from
    dataset" (`dataset_id=None`). Single-parent: a project is released from
    wherever it was before it lands, so there is no state in which one name is
    in two folders.
    """
    with _CONFIG_LOCK:
        records, files = _open(root)
        members = _check_projects(projects, known)
        target = None
        if dataset_id is not None:
            target = records.get(str(dataset_id))
            if target is None:
                raise DatasetError(f"no dataset with id {dataset_id!r}")
        records = _released(records, members)
        if target is not None:
            target = records[target.id]
            target = target.with_projects(tuple(target.projects) + members)
            records[target.id] = target
        _store(records, files)
        return target


def forget_project(name: str, root=None) -> "Dataset | None":
    """Drop one project name from whatever holds it. Called when a project is
    deleted. A no-op -- and NO write -- when it is not a member, so deleting a
    project on a machine that has never made a dataset does not create a file.
    """
    name = str(name or "").strip()
    if not name:
        return None
    with _CONFIG_LOCK:
        records, files = _open(root)
        holder = next((ds for ds in records.values() if name in ds.projects), None)
        if holder is None:
            return None
        records = _released(records, (name,))
        _store(records, files)
        return records[holder.id]


def _released(records: Mapping[str, Dataset], names: Iterable[str]) -> dict:
    """Every record with `names` taken out of it."""
    dropped = set(names)
    if not dropped:
        return dict(records)
    return {
        dataset_id: replace(
            dataset,
            projects=tuple(name for name in dataset.projects if name not in dropped),
            project_ids={name: ident for name, ident in dataset.project_ids.items()
                         if name not in dropped})
        for dataset_id, dataset in records.items()
    }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
