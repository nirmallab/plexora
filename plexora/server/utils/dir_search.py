"""Names matching a query anywhere under one folder, for the explorer's search.

The listing answers "what is in this folder"; this answers "where under here
is the file called ...", which on a cluster is the question people actually
have -- they remember `LSP11385` and not the four folders above it.

**Budgeted, not cancellable.** Breadth-first so the nearest matches arrive
first, and bounded every way a walk can run away: a number of matches, a
number of milliseconds, a depth, and two searches at once per process. The
explorer cannot stop a walk once it has started; it drops a stale answer and
the walk ends on its own budget. `stopped` says which bound ended it, so a
search that found nothing in three seconds is not mistaken for a search that
found nothing at all.

Names only, like the listing: a match carries no size or date (the explorer
asks for those as it draws them), and nothing is opened. A store directory
(`*.zarr`) is a match like any folder but is never entered -- its insides are
chunk names, thousands of them, and none is what anybody is looking for.
"""

from __future__ import annotations

import fnmatch
import os
import threading
import time
from collections import deque
from pathlib import Path

from plexora.server.utils import dataset_marks, dir_listing

#: The most matches one search returns.
SEARCH_LIMIT = 500
#: The longest one search walks, in milliseconds.
SEARCH_BUDGET_MS = 3000
#: How many folders deep below the starting one a search looks.
SEARCH_MAX_DEPTH = 8
#: The longest query accepted, in characters.
QUERY_LIMIT = 256
#: How many entries are read between two looks at the clock, inside one folder.
CLOCK_EVERY = 256

#: Two walks at once per process: one for the search being typed and one for
#: the search it replaced, which is still running out its budget.
_slots = threading.BoundedSemaphore(2)


class SearchBusy(RuntimeError):
    """Both search slots are taken; the caller should try again shortly."""


def _now():
    return time.monotonic()


def clean_query(query):
    """The query stripped, or ValueError when it is empty or too long."""
    if query is not None and not isinstance(query, str):
        raise ValueError("query must be text")
    text = (query or "").strip()
    if not text:
        raise ValueError("Type something to search for.")
    if len(text) > QUERY_LIMIT:
        raise ValueError(f"A search is at most {QUERY_LIMIT} characters.")
    return text


def _matcher(query):
    """A name test: a case-blind substring, or a glob when it has `* ? [`."""
    folded = query.casefold()
    if any(mark in query for mark in "*?["):
        return lambda name: fnmatch.fnmatchcase(name.casefold(), folded)
    return lambda name: folded in name.casefold()


def search(raw, query, show_hidden=False, limit=SEARCH_LIMIT,
           budget_ms=SEARCH_BUDGET_MS, max_depth=SEARCH_MAX_DEPTH):
    """`{path, query, matches, visited, stopped}` for one search under `raw`.

    Each match is a listing entry (`size` and `mtime` null) plus `rel`, its
    path below the starting folder spelled the way this machine spells paths,
    and `depth` (1 for a name directly inside). `visited` counts the folders
    read. `stopped` is `"limit"`, `"budget"` or `"depth"` when that bound cut
    the walk short, and None when the walk ran out of folders.

    The bounds are clamped to the module's caps whatever is asked. Raises
    `ListingError` for a starting folder that cannot be read, ValueError for a
    bad query and `SearchBusy` when two searches are already running.
    """
    query = clean_query(query)
    limit = max(1, min(SEARCH_LIMIT, int(limit)))
    budget_ms = max(1, min(SEARCH_BUDGET_MS, int(budget_ms)))
    max_depth = max(1, min(SEARCH_MAX_DEPTH, int(max_depth)))
    directory = dir_listing.directory_of(raw)

    if not _slots.acquire(blocking=False):
        raise SearchBusy("Two searches are already running here; "
                         "try again in a moment.")
    try:
        return _walk(directory, query, show_hidden, limit,
                     _now() + budget_ms / 1000.0, max_depth)
    finally:
        _slots.release()


def _walk(directory, query, show_hidden, limit, deadline, max_depth):
    matches_name = _matcher(query)
    matches, visited, stopped = [], 0, None
    pending = deque([(directory, "", 0)])
    while pending:
        if _now() >= deadline:
            stopped = "budget"
            break
        folder, rel, depth = pending.popleft()
        try:
            with os.scandir(folder) as scan:
                entries = []
                for count, entry in enumerate(scan, 1):
                    entries.append(entry)
                    if count % CLOCK_EVERY == 0 and _now() >= deadline:
                        break
        except PermissionError:
            if depth == 0:
                raise dir_listing.ListingError(
                    f"Permission denied: {directory}") from None
            continue
        except OSError as exc:
            if depth == 0:
                raise dir_listing.ListingError(
                    f"Cannot read {directory}: {exc}") from exc
            continue
        visited += 1
        # Sorted so a search answers the same way twice; readdir order on a
        # scratch mount is no order at all.
        entries.sort(key=lambda entry: entry.name.lower())
        for index, entry in enumerate(entries, 1):
            name = entry.name
            if not show_hidden and name.startswith("."):
                continue
            if matches_name(name):
                matches.append(_match(entry, folder, rel, depth + 1))
                if len(matches) >= limit:
                    return _answer(directory, query, matches, visited, "limit")
            if _descends(entry):
                if depth + 1 < max_depth:
                    pending.append((Path(entry.path), os.path.join(rel, name),
                                    depth + 1))
                else:
                    stopped = stopped or "depth"
            if index % CLOCK_EVERY == 0 and _now() >= deadline:
                return _answer(directory, query, matches, visited, "budget")
    return _answer(directory, query, matches, visited, stopped)


def _descends(entry):
    """Whether a walk goes into this entry: a real folder, not a store.

    `follow_symlinks=False`, and no junctions either: a link to a folder is a
    match but never a way in, or a link back up the tree would be a walk that
    never ends.
    """
    try:
        if not entry.is_dir(follow_symlinks=False) or dir_listing._is_link(entry):
            return False
    except OSError:
        return False
    return not entry.name.lower().endswith(dataset_marks.STORE_DIR_SUFFIXES)


def _match(entry, folder, rel, depth):
    """One match, in the listing's entry shape with no stat."""
    is_dir = dir_listing._is_dir(entry)
    kind = "dir" if is_dir else "file"
    if dir_listing._is_link(entry):
        kind = "link"
    return {"name": entry.name, "is_dir": is_dir, "size": None,
            "path": str(Path(folder) / entry.name), "mtime": None,
            "kind": kind,
            "store": dataset_marks.store_by_name(entry.name) if is_dir else None,
            "rel": os.path.join(rel, entry.name), "depth": depth}


def _answer(directory, query, matches, visited, stopped):
    return {"path": str(directory), "query": query, "matches": matches,
            "visited": visited, "stopped": stopped}
