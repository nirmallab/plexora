"""Moving the contents of one data root into another.

`plexora config set data-dir` has always been able to change WHERE Plexora
looks. It has never been able to bring anything with it, so the honest
description of changing the data directory used to be "your projects are still
in the old place and the app can no longer see them". This module is the other
half: it takes what is in the old root and puts it in the new one.

Three rules shape everything here, and all three are about not losing imaging
data that took a day to import:

**Nothing is ever merged.** If the target already holds an entry with the same
name -- a project directory, or a `config.json` from a previous Plexora root --
the migration is refused outright and names the collisions. Merging two project
registries is a decision with a right answer that only the user knows, and
guessing it silently would either shadow their old work or overwrite their new.

**The setting is written only after the copy succeeds.** The other order is
what produces the worst outcome available: a config pointing at an empty
directory while the data sits somewhere the app no longer looks. A failed
migration here leaves both the data and the pointer exactly as they were, so
the recovery is "try again", not "find my projects".

**A failure stops the run.** Remaining entries are left untouched and the
report names the one that failed and the ones that got through. Pressing on
would spread the damage across a tree nobody can now describe.

Progress is counted in TOP-LEVEL ENTRIES, not bytes. A byte total means walking
the whole tree before copying a single file, and this data lives on Dropbox and
on cluster filesystems where that walk is minutes of nothing visibly happening.
One project is the unit a user thinks in anyway.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import NamedTuple

from plexora import paths

#: Copy and leave the originals where they are. Costs twice the disk while both
#: copies exist, and is the only mode that cannot lose anything.
MODE_COPY = "copy"

#: Move: a rename within one filesystem, a copy-then-remove across two. This is
#: `shutil.move`'s own rule and the reason nothing here implements it by hand.
MODE_MOVE = "move"

#: Change the setting and touch no files. The default, and what someone
#: pointing Plexora at a root that already holds their projects wants.
MODE_NONE = "none"

MODES = (MODE_NONE, MODE_COPY, MODE_MOVE)


def migratable(root: Path) -> list[str]:
    """Top-level names in `root` worth moving, sorted.

    Write probes are skipped: `paths._probe_name` writes and unlinks one per
    process per thread, so a crashed process can leave one behind, and carrying
    a dead lock file into a new root would be the one thing in it that means
    something and is wrong.
    """
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(
        name for name in names
        if not name.startswith(paths._PROBE_PREFIX)
    )


class Plan(NamedTuple):
    """What a migration from `source` to `target` would do, and why it cannot.

    Computed fresh on every check and again immediately before the run starts.
    The gap between a user reading this and pressing the button is however long
    they think about it, and a project imported in another tab during that gap
    is a collision that was not there when the page was drawn.
    """

    source: Path
    target: Path
    #: Top-level names that would move.
    entries: tuple
    #: Names already present in the target. Never overwritten -- see the module
    #: docstring -- so any collision at all blocks the whole migration.
    collisions: tuple
    #: Reasons this cannot run at all, in a form fit to show a user.
    problems: tuple
    #: True when a move would be a rename rather than a copy, which is the
    #: difference between instant and an hour. Worth saying before they choose.
    same_filesystem: bool

    @property
    def can_migrate(self) -> bool:
        return bool(self.entries) and not self.problems and not self.collisions

    def describe(self) -> dict:
        return {
            "source": str(self.source),
            "target": str(self.target),
            "entries": list(self.entries),
            "entry_count": len(self.entries),
            "collisions": list(self.collisions),
            "problems": list(self.problems),
            "same_filesystem": self.same_filesystem,
            "can_migrate": self.can_migrate,
        }


def _nearest_existing(path: Path) -> Path:
    """The closest ancestor of `path` that exists, or `path` itself.

    Both questions the preview asks -- can this be written to, and is it on the
    same filesystem -- are unanswerable about a directory that does not exist
    yet, and asking them of the parent that WILL hold it is the same answer for
    every purpose here.
    """
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def can_write(path: Path) -> bool:
    """Whether Plexora could write into `path`, WITHOUT creating it.

    `paths.is_writable` cannot answer this for a preview, and not by a detail:
    it `mkdir(parents=True)`s the path it is asked about and caches the result
    for the life of the process. Both behaviours are right for a root the app
    is committing to and wrong for one a user is merely considering -- a typo
    in the box would leave a real empty directory on disk (and make the
    "does not exist yet" line this preview had just drawn a lie), and the
    cached answer would outlive them fixing the permissions and trying again.
    """
    probe_at = _nearest_existing(Path(path))
    if not probe_at.is_dir():
        return False
    probe = probe_at / f"{paths._PROBE_PREFIX}.check.{os.getpid()}.{threading.get_ident()}"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _same_filesystem(source: Path, target: Path) -> bool:
    """Whether a move between these two is a rename.

    False on any error: claiming "this will be instant" and then copying for an
    hour is the wrong way to be wrong.
    """
    try:
        return os.stat(source).st_dev == os.stat(_nearest_existing(target)).st_dev
    except OSError:
        return False


def plan(source, target) -> Plan:
    """What moving `source`'s contents into `target` would involve."""
    source = Path(source)
    target = Path(target)
    problems: list[str] = []

    if not source.is_dir():
        problems.append(f"There is nothing at {source} to migrate.")
    if source == target:
        problems.append("The new directory is the same as the current one.")
    elif source.is_dir():
        # Either nesting is refused. Target inside source means copying a tree
        # into itself; source inside target means the old root survives the
        # move as an empty directory sitting in the new root, where the project
        # listing would offer it as a project.
        if target.is_relative_to(source):
            problems.append(
                f"{target} is inside the current data directory. "
                "Choose a directory outside it."
            )
        elif source.is_relative_to(target):
            problems.append(
                f"The current data directory is inside {target}. "
                "Choose a directory that does not contain it."
            )

    if not can_write(target):
        problems.append(f"{target} cannot be written to.")

    entries = tuple(migratable(source)) if source.is_dir() else ()
    existing = set(migratable(target))
    collisions = tuple(name for name in entries if name in existing)

    return Plan(
        source=source,
        target=target,
        entries=entries,
        collisions=collisions,
        problems=tuple(problems),
        same_filesystem=_same_filesystem(source, target),
    )


# -- the job -------------------------------------------------------------

#: One migration at a time, process-wide. Not keyed by anything: there is one
#: data root, so two concurrent migrations of it is not a case to support but a
#: case to refuse.
_job: dict = {"status": "idle"}
_job_lock = threading.Lock()


def status() -> dict:
    with _job_lock:
        return dict(_job)


def _set(**values) -> None:
    with _job_lock:
        _job.update(values)


def is_running() -> bool:
    with _job_lock:
        return _job.get("status") == "running"


def _transfer(source: Path, target: Path, name: str, mode: str) -> None:
    """One top-level entry, by whichever stdlib call is right for it.

    `shutil.move` rather than a hand-rolled rename-else-copy: it already does
    exactly that, including the cross-device fallback, and a reimplementation
    would differ from it on the cases nobody tests.
    """
    src = source / name
    dst = target / name
    if mode == MODE_MOVE:
        shutil.move(str(src), str(dst))
        return
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def _run(source: Path, target: Path, mode: str, on_success) -> None:
    done: list[str] = []
    try:
        target.mkdir(parents=True, exist_ok=True)
        current = plan(source, target)
        # Re-planned inside the thread, so a project imported between the
        # user's last look at the page and this moment is still a collision.
        if not current.can_migrate:
            reason = (current.problems or
                      (f"Already in the new directory: "
                       f"{', '.join(current.collisions)}",))
            raise RuntimeError(" ".join(reason))

        total = len(current.entries)
        for index, name in enumerate(current.entries):
            _set(status="running", done=index, total=total, current=name,
                 migrated=list(done))
            _transfer(source, target, name, mode)
            done.append(name)

        on_success()
        _set(status="done", done=len(done), total=total, current="",
             migrated=list(done), error="")
    except Exception as exc:  # noqa: BLE001 - reported, never raised into a thread
        _set(status="error", current="", migrated=list(done),
             error=f"{type(exc).__name__}: {exc}")


def start(source, target, mode, on_success) -> None:
    """Begin migrating in the background. Raises if one is already running.

    `on_success` is what records the new directory, and it is a callback rather
    than something this module does itself for the ordering reason in the
    module docstring: it runs inside the worker, after the last entry has
    landed, and not at all if anything failed.
    """
    if mode not in (MODE_COPY, MODE_MOVE):
        raise ValueError(f"unknown migration mode {mode!r}")
    with _job_lock:
        if _job.get("status") == "running":
            raise RuntimeError("A migration is already running.")
        _job.clear()
        _job.update(status="running", done=0, total=0, current="",
                    migrated=[], error="", mode=mode,
                    source=str(source), target=str(target))
    threading.Thread(
        target=_run,
        args=(Path(source), Path(target), mode, on_success),
        daemon=True,
        name="plexora-data-migration",
    ).start()


def reset() -> None:
    """Forget a finished job. For tests, and for a page reloaded after one."""
    with _job_lock:
        _job.clear()
        _job["status"] = "idle"
