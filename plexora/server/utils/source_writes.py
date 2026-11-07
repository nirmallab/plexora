"""Saving into the user's own files from the website (plan Phase 4, source
write-back).

On the desktop a write into a source file opens the file where it is and
changes it: gates into an .h5ad's `uns` (gating's Save Gates to Source File,
the Single Cell Classifier's calls with them), ROI and QC columns onto the
cells (`roi.map_to_cells`, QC's `cells/write` and `segmentation/write`). On
the website the file the user picked is mounted read-only under
`/picked/<id>/` (client services/pickedFiles.js), and only the page can write
to it. So every writer asks this module where to write, and there:

- `writable(path)` yields the path itself anywhere but the website; for a
  picked file, a staging copy in the worker's memory (`copy=False` when the
  writer replaces the whole file, as a flat table's write does), made the first
  time it is asked for in a batch and used by every write after it;
- `current(path)` is where to read it meanwhile: the staged copy once there
  is one, so a writer that reads after it wrote sees what it wrote;
- when the batch ends -- the request (plexora/browser/bridge.py) or the
  background step (plexora/browser/cooperative.py) -- each staged file goes
  to the page once (`deliver(source, staged)`): the worker swaps it in for the
  picked file, so Python reads from then on what it wrote, as on the desktop,
  and the page writes it over the original through the browser's handle or
  offers it as a download (services/sourceWrites.js);
- a write that raises drops its staged copy: the original stays as it was.

Nothing is staged unless the user allowed it -- the website's Settings > Data
> "Save into your files", off by default, which the page answers when asked
(`ask("sourceWrites")`). A file the user did not pick is never a target: only
paths under the picked root are staged, and the page writes only through the
handle it holds for that path. A picked folder (a .zarr store) is refused
with a sentence: the page cannot yet write a store's files back.

Under CPython this runs as it is (tests/test_source_writes.py): `configure`
points `root` at a folder of the test's and hands in `ask` and `deliver`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

#: Where the page mounts what the user picked (services/pickedFiles.js ROOT).
PAGE_ROOT = "/picked/"

#: The largest file staged for an in-place write: the copy lives in the
#: worker's memory beside what Python already holds (the website's tables stay
#: well under it: handoffNotice's envelope).
LIMIT_BYTES = 1 << 30

#: Why a write was refused (`SourceWriteRefused.reason`).
OFF, FOLDER, TOO_LARGE = "off", "folder", "too_large"


class SourceWriteRefused(PermissionError, ValueError):
    """A write into a picked file that is not made, and why in one sentence.

    A ValueError too, so every writer's existing handler (the table operations
    turn one into `{"ok": False, "message"}`) says it to the user as it is."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


_root = PAGE_ROOT
_ask = None
_deliver = None
#: Sources staged and not yet delivered: normalised source path -> staged path.
_staged: dict[str, str] = {}
#: The open batches, innermost last: the sources each staged first.
_batches: list[set] = []
#: What went to the page, newest last: {source, name, bytes, sha256, outcome}.
delivered: list[dict] = []
_KEEP = 20


def configure(*, root: str | None = None, ask=None, deliver=None) -> None:
    """Who answers and where: `root` the picked files' folder (the website's
    `/picked/` by default); `ask(kind, args)` the page's question (the setting);
    `deliver(source, staged)` hands a staged file to the page."""
    global _root, _ask, _deliver
    if root is not None:
        _root = _key(root).rstrip("/") + "/"
    if ask is not None:
        _ask = ask
    if deliver is not None:
        _deliver = deliver


def reset() -> None:
    """Forget the wiring and anything staged (tests)."""
    global _root, _ask, _deliver
    for source in list(_staged):
        _drop(source)
    _batches.clear()
    delivered.clear()
    _root, _ask, _deliver = PAGE_ROOT, None, None


def _key(path) -> str:
    return str(path).replace("\\", "/")


def on_website() -> bool:
    return os.environ.get("PLEXORA_BACKEND") == "browser"


def is_picked(path) -> bool:
    """Whether `path` is a file the user picked on the website: the only
    files this module stages."""
    return on_website() and _key(path).startswith(_root)


def allowed() -> bool:
    """The user's setting, from the page: True only when it says so."""
    if _ask is None:
        return False
    try:
        answer = _ask("sourceWrites", {})
    except Exception:  # noqa: BLE001 -- a page that cannot answer has not said yes
        return False
    return answer is True


def current(path):
    """Where to read `path` now: its staged copy while one is waiting to go
    to the page, else `path` itself."""
    return _staged.get(_key(path), path)


def _refuse_unless_writable(source: str, copy: bool) -> None:
    name = source.rstrip("/").rsplit("/", 1)[-1]
    if os.path.isdir(source):
        raise SourceWriteRefused(
            f"{name} is a folder: the website cannot save into a folder yet. "
            "Open the project in the desktop app to save into it.", FOLDER)
    if not allowed():
        raise SourceWriteRefused(
            "Saving into your files is off. Turn it on in Settings › Data, then save again.", OFF)
    if copy:
        size = os.path.getsize(source)
        if size > LIMIT_BYTES:
            raise SourceWriteRefused(
                f"{name} is {size / 2**30:.1f} GB, more than the website can save "
                f"({LIMIT_BYTES / 2**30:.0f} GB). The desktop app saves it in place.", TOO_LARGE)


def check(path, *, copy: bool = True) -> None:
    """Raise `SourceWriteRefused` now if `writable(path)` would: for a writer
    that does something first (a backup) it should not do for a refused write."""
    source = _key(path)
    if is_picked(source) and source not in _staged:
        _refuse_unless_writable(source, copy)


def _stage(source: str, copy: bool) -> str:
    folder = tempfile.mkdtemp(prefix="plexora-write-")
    staged = os.path.join(folder, source.rstrip("/").rsplit("/", 1)[-1])
    if copy:
        shutil.copyfile(source, staged)
    return staged


@contextlib.contextmanager
def writable(path, *, copy: bool = True):
    """The path to write `path` at. Anywhere but a picked file on the website,
    `path` itself. A picked file: its staged copy (made from the original with
    `copy`, or a path to create the whole file at without it), refused with
    `SourceWriteRefused` when the user has not allowed it. A write that raises
    drops the staged copy; outside any batch the file goes to the page as the
    write ends."""
    source = _key(path)
    if not is_picked(source):
        yield path
        return
    staged = _staged.get(source)
    if staged is None:
        _refuse_unless_writable(source, copy)
        staged = _stage(source, copy)
        _staged[source] = staged
        if _batches:
            _batches[-1].add(source)
    try:
        yield staged
    except BaseException:
        _drop(source)
        raise
    if not _batches:
        _send({source})


@contextlib.contextmanager
def batch():
    """Writes made inside go to the page once, when it ends: a request, or a
    background step. Batches nest; a file goes when the batch that staged it
    ends."""
    mine: set = set()
    _batches.append(mine)
    try:
        yield
    finally:
        _batches.remove(mine)
        _send(mine)


def _drop(source: str) -> None:
    staged = _staged.pop(source, None)
    for held in _batches:
        held.discard(source)
    if staged:
        shutil.rmtree(os.path.dirname(staged), ignore_errors=True)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _send(sources) -> None:
    for source in sorted(sources):
        staged = _staged.pop(source, None)
        if staged is None or not os.path.exists(staged):
            # Staged for a whole-file write that never wrote it: nothing to send.
            if staged:
                shutil.rmtree(os.path.dirname(staged), ignore_errors=True)
            continue
        record = {"source": source, "name": Path(staged).name,
                  "bytes": os.path.getsize(staged), "sha256": _sha256(staged), "outcome": None}
        try:
            record["outcome"] = _deliver(source, staged) if _deliver is not None else None
        finally:
            shutil.rmtree(os.path.dirname(staged), ignore_errors=True)
        delivered.append(record)
        del delivered[:-_KEEP]
