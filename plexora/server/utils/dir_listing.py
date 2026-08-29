"""One directory's contents, for the picker that stands in for a file dialog.

Lives here rather than in the route that first needed it because both machines
in a session now need to answer the same question. The viewer answers it about
its own filesystem (`/list_dir`); a data node answers it about the far side's
(`/node/v1/list_dir`), which is the only way to browse a cluster from a laptop
-- a compute node has no desktop for a native dialog to open on, and until
there was a listing the sole way to name a file over there was to already know
its path.

**Names, sizes, and which entries are directories. Never bytes.** Nothing here
opens a file, and nothing it returns could not have been read by running `ls`
as the same account on the same machine -- which is the trust boundary both
callers already sit behind (a single user's server, guarded by a token).

Every path the picker will need is built HERE, on the machine that owns the
filesystem: each entry's full path, and the breadcrumb trail up to the root.
The browser then never does path arithmetic, which is the only honest way to
browse a Windows node from a Mac -- the client has no idea whether the far side
joins with `/` or `\\`, and every attempt to guess got `C:\\data` wrong.
"""

from __future__ import annotations

import os
from pathlib import Path

#: How many entries one listing hands back. A scratch directory with a hundred
#: thousand files is an ordinary thing on a cluster, and this picker is for
#: finding one file rather than for reading a directory whole.
LIST_DIR_LIMIT = 2000


class ListingError(ValueError):
    """A directory that cannot be listed, with the sentence to show for it."""


def listing(raw, limit=LIST_DIR_LIMIT, show_hidden=False):
    """`{path, parent, crumbs, entries, truncated}` for one directory.

    An empty path means the user's home directory, which is where somebody
    starting to look for their data almost always is. A path naming a *file*
    means the folder that holds it, so a field already holding a path can be
    handed straight back as the place to open at.

    `show_hidden` includes dotfiles. Off by default because they are noise in a
    picker for scientific data, and on when the user asks -- a `.snakemake` or
    a `.config` is occasionally exactly what somebody is looking for.
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

    # Collected whole, and cheaply: name plus the d_type the readdir already
    # returned, no stat. Sorting has to see every entry -- cutting at the limit
    # first made the 2000 shown an arbitrary slice of the directory order,
    # which on a scratch mount is no order at all.
    found = []
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                if not show_hidden and entry.name.startswith("."):
                    continue
                found.append((entry.name, _is_dir(entry), entry))
    except PermissionError as exc:
        # Said plainly, because on a cluster this is not a bug to report but a
        # fact about the account: /n/groups holds a hundred directories the
        # user cannot enter and they need to read the sentence and move on.
        raise ListingError(f"Permission denied: {directory}") from exc
    except OSError as exc:
        raise ListingError(f"Cannot read {directory}: {exc}") from exc

    # Directories first, then by name: a .zarr store is a directory and the
    # single Data input takes one, so the two kinds have to be equally easy to
    # reach rather than one buried under the other.
    found.sort(key=lambda item: (not item[1], item[0].lower()))
    truncated = len(found) > limit

    # Only now, and only on what is actually being sent: `described` stats each
    # file for its size, and a stat per entry across a hundred thousand of them
    # is a directory listing that takes a minute on NFS.
    entries = [described(entry, is_dir, directory)
               for _, is_dir, entry in found[:limit]]

    parent = str(directory.parent) if directory.parent != directory else None
    return {"path": str(directory), "parent": parent,
            "crumbs": crumbs(directory), "entries": entries,
            "truncated": truncated}


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


def described(entry, is_dir, directory):
    """One directory entry, as the picker draws it.

    `path` is the whole point of building this server-side: the browser opens
    what it is given rather than joining a name onto a directory with a
    separator it had to guess.
    """
    size = None
    if not is_dir:
        try:
            size = entry.stat().st_size
        except OSError:
            # Guarded for the same reason `_is_dir` is: a broken symlink is an
            # entry with no size, not a listing that fails.
            size = None
    return {"name": entry.name, "is_dir": is_dir, "size": size,
            "path": str(directory / entry.name)}
