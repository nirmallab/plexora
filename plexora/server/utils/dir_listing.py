"""One directory's contents, for the explorer that stands in for a file dialog.

Lives here rather than in the route that first needed it because both machines
in a session now need to answer the same question. The viewer answers it about
its own filesystem (`/list_dir`); a data node answers it about the far side's
(`/node/v1/list_dir`), which is the only way to browse a cluster from a laptop
-- a compute node has no desktop for a native dialog to open on, and until
there was a listing the sole way to name a file over there was to already know
its path.

**Names, sizes, dates, and which entries are directories. Never bytes.**
Nothing here opens a file, and nothing it returns could not have been read by
running `ls -l` as the same account on the same machine -- which is the trust
boundary both callers already sit behind (a single user's server, guarded by a
token). The one exception is `dataset_marks`, which may read the open folder's
own metadata (a scale-factor file, a zarr group's attributes) to say what
dataset it is -- the open folder's, and never a child's.

Every path the explorer will need is built HERE, on the machine that owns the
filesystem: each entry's full path, and the breadcrumb trail up to the root.
The browser then never does path arithmetic, which is the only honest way to
browse a Windows node from a Mac -- the client has no idea whether the far side
joins with `/` or `\\`, and every attempt to guess got `C:\\data` wrong.

**Bounded, and honest about where it stopped.** A scratch folder with a
hundred thousand files is an ordinary thing on a cluster. Names come back up to
`LIST_DIR_LIMIT`; sizes and dates come back with them only for a folder small
enough to stat inside `LISTING_BUDGET_S`, and otherwise `partial` says so and
the explorer asks for them in chunks (`stat_entries`) for the rows on screen.
"""

from __future__ import annotations

import os
import stat as stat_module
import time
from pathlib import Path

from plexora.server.utils import dataset_marks

#: How many entries one listing hands back. Large, because the explorer draws
#: only the rows on screen and sorts in the browser -- a cut here is a folder
#: the user cannot see the end of, which is worse than a big answer.
LIST_DIR_LIMIT = 50_000

#: Where reading stops altogether. What was read is sorted and cut as usual,
#: `truncated` is set, and `total` becomes a lower bound: a directory of ten
#: million files is somebody's job output, not a place to choose a file from.
SCAN_HARD_CAP = 250_000

#: Folders up to this size get every row's size and date in the listing
#: itself. Past it the stats are left to `stat_entries`, which the explorer
#: asks for the rows actually on screen.
INLINE_STAT_LIMIT = 5_000

#: Seconds, from the end of the `scandir`, for everything optional: the child
#: marker checks and the inline stats. A listing on a slow mount still arrives;
#: it arrives `partial`.
LISTING_BUDGET_S = 2.0

#: Child folders are looked inside (one `scandir` each, for dataset markers and
#: locks) only when there are at most this many of them.
CHILD_MARKER_LIMIT = 200

#: How many names one child look reads before giving up on it.
CHILD_SCAN_CAP = 512

#: How many names one `stat_entries` call may ask about.
STAT_ENTRIES_LIMIT = 500


class ListingError(ValueError):
    """A directory that cannot be listed, with the sentence to show for it."""


def _now():
    return time.monotonic()


def _platform():
    from plexora import _resources

    return _resources.platform_word()


def directory_of(raw):
    """The folder `raw` names, resolved: home for nothing, a file's parent.

    An empty path means the user's home directory, which is where somebody
    starting to look for their data almost always is. A path naming a *file*
    means the folder that holds it, so a field already holding a path can be
    handed straight back as the place to open at.
    """
    try:
        directory = Path(raw).expanduser() if str(raw or "").strip() else Path.home()
        directory = directory.resolve()
    except (OSError, RuntimeError) as exc:
        raise ListingError(str(exc)) from exc

    try:
        # Tolerated here rather than in the route so the node gets it free:
        # both callers hand this whatever was in the text box, and that is as
        # often a file as a folder.
        if directory.is_file():
            directory = directory.parent
    except OSError:
        pass

    if not directory.is_dir():
        raise ListingError(f"Not a folder: {directory}")
    return directory


def listing(raw, limit=LIST_DIR_LIMIT, show_hidden=False):
    """One directory, as the explorer draws it.

    `{path, parent, crumbs, entries, truncated, total, partial, dataset,
    platform}`. `total` counts the entries shown with `show_hidden` (a lower
    bound when the scan hit `SCAN_HARD_CAP`); `partial` means some rows have
    no `mtime` because they were not stat'ed; `dataset` says the open folder
    is itself one dataset (see `dataset_marks.dataset_of`).

    `show_hidden` includes dotfiles. Off by default because they are noise in a
    picker for scientific data, and on when the user asks -- a `.snakemake` or
    a `.config` is occasionally exactly what somebody is looking for.
    """
    directory = directory_of(raw)

    # Collected whole, and cheaply: name plus the d_type the readdir already
    # returned, no stat. Sorting has to see every entry -- cutting at the limit
    # first made the rows shown an arbitrary slice of the directory order,
    # which on a scratch mount is no order at all. Every name is kept for the
    # markers whether or not it is shown: zarr's are dotfiles.
    found, names, capped = [], [], False
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                names.append(entry.name)
                if len(names) >= SCAN_HARD_CAP:
                    capped = True
                if show_hidden or not entry.name.startswith("."):
                    found.append((entry.name, _is_dir(entry), entry))
                if capped:
                    break
    except PermissionError as exc:
        # Said plainly, because on a cluster this is not a bug to report but a
        # fact about the account: /n/groups holds a hundred directories the
        # user cannot enter and they need to read the sentence and move on.
        raise ListingError(f"Permission denied: {directory}") from exc
    except OSError as exc:
        raise ListingError(f"Cannot read {directory}: {exc}") from exc

    # Directories first, then by name: a .zarr store is a directory and the
    # single Data input takes one, so the two kinds have to be equally easy to
    # reach rather than one buried under the other. The explorer re-sorts as
    # the user asks; this order only decides which rows survive the cut.
    found.sort(key=lambda item: (not item[1], item[0].lower()))
    kept = found[:limit]
    deadline = _now() + LISTING_BUDGET_S

    # Marks first, then stats: a store row can only be marked here, while a
    # date the budget did not reach is fetched later by `stat_entries`.
    looked = _child_looks(kept, found, deadline)
    rows = [_row(entry, is_dir, directory, looked.get(entry.name, False))
            for _, is_dir, entry in kept]
    # Every row of a small folder; of a big one only the folders just looked
    # inside, which already cost a `scandir` each and must keep the `denied`
    # their look found rather than have `stat_entries` call them plain dirs.
    stat_all = len(kept) <= INLINE_STAT_LIMIT
    for row, (_, _, entry) in zip(rows, kept):
        if row["kind"] == "broken" or not (stat_all or row["name"] in looked):
            continue
        if _now() >= deadline:
            break
        _stat_into(row, entry)
    partial = any(row["mtime"] is None and row["kind"] != "broken" for row in rows)

    parent = str(directory.parent) if directory.parent != directory else None
    return {"path": str(directory), "parent": parent,
            "crumbs": crumbs(directory), "entries": rows,
            "truncated": capped or len(found) > limit, "total": len(found),
            "partial": partial,
            "dataset": dataset_marks.dataset_of(directory, names,
                                                children=looked or None),
            "platform": _platform()}


def _child_looks(kept, found, deadline):
    """`{name: child names | None}` for the kept folders, when there are few.

    Skipped entirely past `CHILD_MARKER_LIMIT` subdirectories -- a folder of
    two thousand sample folders is browsed, not imported -- and cut short by
    the listing's deadline. A link to a folder is looked inside too: on a
    cluster the group share is very often reached through one.
    """
    if sum(1 for _, is_dir, _ in found if is_dir) > CHILD_MARKER_LIMIT:
        return {}
    looked = {}
    for name, is_dir, entry in kept:
        if not is_dir:
            continue
        if _now() >= deadline:
            break
        looked[name] = dataset_marks.child_names(entry.path, CHILD_SCAN_CAP)
    return looked


def _row(entry, is_dir, directory, inside=False):
    """One entry before its stat: name, kind, path and any dataset mark.

    `inside` is the child look's answer -- a list of names, None for a folder
    that refused it, or False when nobody looked.
    """
    kind = "dir" if is_dir else "file"
    if _is_link(entry):
        kind = "link"
        if not is_dir and _dangles(entry):
            kind = "broken"
    store = None
    if is_dir:
        if inside is None:
            kind = "denied"
        elif inside:
            store = dataset_marks.store_by_names(entry.name, inside)
        store = store or dataset_marks.store_by_name(entry.name)
    return {"name": entry.name, "is_dir": is_dir, "size": None,
            "path": str(directory / entry.name), "mtime": None,
            "kind": kind, "store": store}


def _stat_into(row, entry):
    """Fill a row's size (files) and mtime (everything) from one stat."""
    try:
        found = entry.stat()
    except OSError:
        # Guarded for the same reason `_is_dir` is: one unreadable entry is an
        # entry with no size, not a listing that fails.
        return
    if not row["is_dir"]:
        row["size"] = found.st_size
    row["mtime"] = int(found.st_mtime)


def crumbs(directory):
    """The trail from the filesystem root down to `directory`, clickable.

    `p.name or str(p)` is what makes the top of the trail readable on either
    kind of machine: the name of `/` is the empty string and the name of
    `C:\\` is too, so both fall through to the path itself.
    """
    return [{"label": part.name or str(part), "path": str(part)}
            for part in reversed([directory, *directory.parents])]


def _is_dir(entry):
    """Whether a scanned entry is a directory, never raising.

    A scratch mount routinely holds broken symlinks and directories the user
    cannot enter, and one of them must not blank the listing that contains it.
    """
    try:
        return entry.is_dir()
    except OSError:
        return False


def _is_link(entry):
    """A symlink or a Windows junction. Free: the readdir already said."""
    try:
        if entry.is_symlink():
            return True
        junction = getattr(entry, "is_junction", None)
        return bool(junction and junction())
    except OSError:
        return False


def _dangles(entry):
    """Whether a link that is not a folder points at nothing.

    Nearly free: `_is_dir` already stat'ed through the link, and a link to a
    file keeps that stat cached, so only a dangling one pays a second call.
    """
    try:
        entry.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def stat_entries(raw, names):
    """Sizes, dates and kinds for some names in one folder.

    `{path, entries: {name: {size, mtime, kind, is_dir}}}` -- what a `partial`
    listing left out, asked for a screenful at a time. Each name must be a bare
    name in that folder: no separator, not `.` or `..`, because this answers
    about one directory and is not a way to stat the rest of the disk. A name
    that is not there (deleted since the listing) is simply left out.
    """
    directory = directory_of(raw)
    if not isinstance(names, (list, tuple)):
        raise ListingError("names must be a list")
    if len(names) > STAT_ENTRIES_LIMIT:
        raise ListingError(
            f"At most {STAT_ENTRIES_LIMIT} names can be asked about at once.")
    for name in names:
        if (not isinstance(name, str) or not name or name in (".", "..")
                or any(mark in name for mark in _NOT_IN_A_NAME)):
            raise ListingError(f"Not a name in this folder: {name!r}")

    entries = {}
    for name in names:
        path = directory / name
        try:
            found = os.lstat(path)
        except OSError:
            continue
        kind = "dir" if stat_module.S_ISDIR(found.st_mode) else "file"
        if stat_module.S_ISLNK(found.st_mode) or (
                kind == "dir" and _is_junction(path)):
            kind = "link"
            try:
                found = os.stat(path)
            except FileNotFoundError:
                entries[name] = {"size": None, "mtime": None,
                                 "kind": "broken", "is_dir": False}
                continue
            except OSError:
                continue
        is_dir = stat_module.S_ISDIR(found.st_mode)
        entries[name] = {"size": None if is_dir else found.st_size,
                         "mtime": int(found.st_mtime), "kind": kind,
                         "is_dir": is_dir}
    return {"path": str(directory), "entries": entries}


#: What a bare name cannot hold: this machine's separators, a NUL, and on
#: Windows a drive colon -- `D:\\data / "C:x"` is a path on another drive.
_NOT_IN_A_NAME = tuple(
    mark for mark in (os.sep, os.altsep, "\0", ":" if os.name == "nt" else None)
    if mark)


def _is_junction(path):
    junction = getattr(os.path, "isjunction", None)
    try:
        return bool(junction and junction(path))
    except OSError:
        return False
