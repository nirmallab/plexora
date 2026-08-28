"""Where Plexora's data lives, and who decides.

Every path decision in the app comes from here. Before this module the answer
was computed once, at import time, in `plexora/__init__.py`, and its last
fallback was `Path("plexora/data").resolve()` -- relative to the *current
working directory*. That is right only when the process was launched from the
repository root, which is what `python run.py` does and what the README's
editable install assumed. For a real `pip install plexora` it meant that
importing the package from `~/analysis` silently created and used
`~/analysis/plexora/data`, so running a notebook from a different folder the
next day made every project look as though it had been deleted.

Two properties matter more than the specific locations:

**Resolved on demand, never snapshotted.** Everything here is a function.
Modules that did `from plexora import data_path` captured the value at import,
which meant any decision made after the first `import plexora` was silently
ignored -- that is why the Jupyter sidecar has to set real OS environment
variables before spawning a child rather than just passing `--data-dir`. A
function can also grow a parameter later; a module constant cannot, and the
per-user and shared-root work depends on exactly that.

**The write root and the read root are different questions.** A project the
user imported lives in their own root and is theirs to change. A project on a
site-managed shared root is readable by everyone and writable by nobody, yet a
user exploring it still needs somewhere to put their gates, ROIs and figures.
So reads resolve across `roots()` while writes always land in `data_root()`.
When a project's home root *is* the user root -- the entire single-user case --
the two collapse to one directory and nothing is different from before.

Deliberately a leaf module: it imports nothing from `plexora`, so it cannot
participate in the import cycle that `plexora/__init__.py` sits at the centre
of, and it is safe to call from anywhere including the CLI.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import NamedTuple

from platformdirs import user_config_dir, user_data_dir

#: Passed to platformdirs as both the app name and (via appauthor=False) the
#: whole of the path tail. Without appauthor=False, Windows gets
#: `AppData\Local\plexora\plexora` -- the vendor directory doubled up, which is
#: what the old appdirs call produced.
APP_NAME = "plexora"

ENV_DATA_PATH = "PLEXORA_DATA_PATH"
ENV_SHARED_PATH = "PLEXORA_SHARED_PATH"
ENV_MASK_OUTPUT = "PLEXORA_MASK_OUTPUT"

CONFIG_FILENAME = "config.json"
SETTINGS_FILENAME = "settings.json"

#: Directory under a root holding every figure. Dot-prefixed so a project
#: literally named "figures" cannot collide with it -- projects are directories
#: under the same root. See figure_builder's repository module.
FIGURES_DIRNAME = ".figures"

#: Written and removed to prove a root is actually writable. A probe beats
#: `os.access`, which on Windows reports the DACL rather than the effective
#: permission and cheerfully says yes for a directory that then refuses the
#: write.
_PROBE_PREFIX = ".plexora-write-probe"


def _probe_name() -> str:
    """A probe filename no other prober can be holding.

    Per process AND per thread, because both really do coincide: Waitress runs
    eight threads and the segmentation job adds more, and a cached miss lets
    two of them probe one root at the same moment. With a shared name, one
    thread's unlink lands between the other's write and its own unlink -- and
    on Windows that surfaces as a PermissionError, which this function would
    have read as "not writable". A root wrongly judged read-only silently
    stops recording config changes, which is a far worse failure than the
    write it was trying to avoid.
    """
    return f"{_PROBE_PREFIX}.{os.getpid()}.{threading.get_ident()}"


class DataRootError(RuntimeError):
    """The resolved data root cannot be used.

    Raised at resolution rather than at first write, so the message names the
    path and the flag that changes it instead of surfacing as an OSError three
    frames down inside a request.
    """


class Resolution(NamedTuple):
    """A resolved root and the rule that chose it, so `plexora where` can
    explain itself. Users who cannot find their projects need to know *why*
    Plexora picked a directory, not just which one."""

    path: Path
    rule: str


#: Resolution is pure with respect to the environment but does real filesystem
#: work (mkdir, the write probe), so it is done once per process. `reset()`
#: clears it; the test suite calls that after repointing the environment.
_cache: dict[str, object] = {}
_cache_lock = threading.RLock()


def reset() -> None:
    """Forget the resolved roots.

    For tests, which repoint `PLEXORA_DATA_PATH` at a tmp_path per test, and
    for `plexora config set`, which changes the answer underneath a live
    process.
    """
    with _cache_lock:
        _cache.clear()


# -- the settings file ---------------------------------------------------


def settings_path() -> Path:
    """Where the persistent choice of data directory is recorded.

    In the config directory, which is emphatically *not* derived from
    `data_root()` -- the file's whole job is to say where the data root is, so
    resolving it through one would be circular.

    That said, it does not always land somewhere else: Windows and macOS use a
    single per-app directory for both, so by default this file sits inside the
    default data root even though it is not reached through it. The case that
    matters is a user who moves their data elsewhere and later deletes the old
    default directory. They lose the pointer and Plexora returns to the
    default -- but it says so, because an absent config.json is exactly what
    `first_run_notice` reports on.
    """
    return Path(user_config_dir(APP_NAME, appauthor=False, roaming=False)) / SETTINGS_FILENAME


def read_settings() -> dict:
    """The settings file, or {} when there is not one yet.

    A damaged file reads as {} rather than raising: the fallbacks below are all
    still available, and refusing to start because a preferences file is
    corrupt would be a worse failure than quietly using the default.
    """
    path = settings_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(data: dict) -> None:
    """Replace the settings file in one step.

    Same temp-file-and-rename as the project config, and for the same reason:
    a reader in another process sees the whole previous file or the whole new
    one, never the empty window that `open(path, "w")` leaves open.
    """
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=4)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# -- the user's writable root --------------------------------------------


def _candidate_data_root() -> Resolution:
    """Pick the data root, without touching the filesystem.

    First hit wins, and the order is deliberate: an explicit instruction for
    this process beats a stored preference, which beats what the build shape
    implies, which beats the platform default.
    """
    from_env = os.environ.get(ENV_DATA_PATH)
    if from_env and from_env.strip():
        return Resolution(Path(from_env).expanduser().resolve(),
                          f"{ENV_DATA_PATH} environment variable")

    stored = read_settings().get("data_dir")
    if isinstance(stored, str) and stored.strip():
        return Resolution(Path(stored).expanduser().resolve(),
                          f"data_dir in {settings_path()}")

    if getattr(sys, "frozen", False):
        # A portable build keeps its data beside the executable so the whole
        # thing can be moved or handed over on a stick as one unit.
        return Resolution(Path(sys.executable).parent.resolve() / "data",
                          "frozen build, beside the executable")

    return Resolution(Path(user_data_dir(APP_NAME, appauthor=False)).resolve(),
                      "platform default")


def _prepare_data_root(resolution: Resolution) -> Resolution:
    """Create the root and prove it is writable.

    The probe runs once per process, on the resolution that gets cached. It is
    worth the two syscalls: a read-only or quota-exhausted root otherwise
    presents as a stack trace from whichever request happened to write first,
    which tells the user nothing about what to do.
    """
    path = resolution.path
    existed = path.exists()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataRootError(
            f"Plexora's data directory cannot be created: {path}\n"
            f"Chosen by: {resolution.rule}\n"
            f"Point it somewhere writable with 'plexora --data-dir <path>' or "
            f"'plexora config set data-dir <path>'."
        ) from exc

    probe = path / _probe_name()
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise DataRootError(
            f"Plexora's data directory is not writable: {path}\n"
            f"Chosen by: {resolution.rule}\n"
            f"Point it somewhere writable with 'plexora --data-dir <path>' or "
            f"'plexora config set data-dir <path>'."
        ) from exc

    # "First run" is judged by the absence of a project registry rather than by
    # whether we just created the directory: on a shared machine an admin often
    # makes the directory ahead of time, and the notice is still worth printing
    # for the user who has never seen it.
    with _cache_lock:
        _cache["first_run"] = not (path / CONFIG_FILENAME).exists()
        _cache["created"] = not existed
    return resolution


def data_root_resolution() -> Resolution:
    """The user's writable root, with the rule that chose it."""
    with _cache_lock:
        cached = _cache.get("data_root")
        if cached is not None:
            return cached  # type: ignore[return-value]
    resolved = _prepare_data_root(_candidate_data_root())
    with _cache_lock:
        _cache["data_root"] = resolved
    return resolved


def data_root() -> Path:
    """Where this user's projects, figures and per-project state are written."""
    return data_root_resolution().path


def first_run_notice() -> str | None:
    """A one-off line naming the data directory, or None if it is not new.

    Printed by the CLI. The location is a platform convention directory, which
    is the right default and also the one a user is least likely to guess, so
    saying it once beats making them run `plexora where` to find out where
    their work went.
    """
    resolution = data_root_resolution()
    with _cache_lock:
        if not _cache.get("first_run"):
            return None
    return (
        f"Plexora will keep your projects in:\n"
        f"  {resolution.path}\n"
        f"Move it any time with 'plexora config set data-dir <path>'."
    )


# -- shared, read-mostly roots -------------------------------------------


def shared_root_resolutions() -> list[Resolution]:
    """Site-managed roots holding projects several users can open.

    From `PLEXORA_SHARED_PATH` (os.pathsep-separated, like PATH) and then
    `shared_dirs` in the settings file. Neither is created if it is missing:
    a shared root is somebody else's to provision, and silently making an empty
    one would turn a typo into a root that exists and holds nothing.

    Cached like `data_root`, and for a sharper reason: `shared_roots()` being
    empty is what lets the tile path skip resolving which root a project came
    from, so this is consulted often enough that a settings-file read per call
    would show up.
    """
    with _cache_lock:
        cached = _cache.get("shared_roots")
        if cached is not None:
            return list(cached)  # type: ignore[arg-type]

    seen: set[Path] = set()
    out: list[Resolution] = []

    def _add(raw, rule):
        if not isinstance(raw, str) or not raw.strip():
            return
        path = Path(raw).expanduser().resolve()
        if path in seen:
            return
        seen.add(path)
        out.append(Resolution(path, rule))

    from_env = os.environ.get(ENV_SHARED_PATH) or ""
    for entry in from_env.split(os.pathsep):
        _add(entry, f"{ENV_SHARED_PATH} environment variable")

    stored = read_settings().get("shared_dirs")
    if isinstance(stored, (list, tuple)):
        for entry in stored:
            _add(entry, f"shared_dirs in {settings_path()}")

    # The user's own root is never also a shared root, whatever the
    # configuration says: it is already first in roots(), and letting it appear
    # twice would make a project look shared to its own owner.
    own = data_root()
    resolved = [r for r in out if r.path != own]
    with _cache_lock:
        _cache["shared_roots"] = resolved
    return list(resolved)


def shared_roots() -> list[Path]:
    return [resolution.path for resolution in shared_root_resolutions()]


def roots() -> list[Path]:
    """Every root a project may be found in, the user's own first.

    Order is the precedence rule: a name present in more than one root resolves
    to the user's copy. Somebody who has made their own version of a shared
    project means to open theirs.
    """
    return [data_root(), *shared_roots()]


# -- paths within a root -------------------------------------------------


def config_path(root=None) -> Path:
    """The project registry for one root. Defaults to the user's own."""
    return Path(root) / CONFIG_FILENAME if root is not None else data_root() / CONFIG_FILENAME


def project_dir(name, root=None) -> Path:
    """A project's directory in `root`, or in the user's own root.

    A pure join -- it does not search and does not create. Callers that need to
    know which root actually owns a project ask the project registry, which is
    the thing that reads config.json.
    """
    base = Path(root) if root is not None else data_root()
    return base / name


def project_state_dir(name) -> Path:
    """Where this user's own state for `name` is written, whoever owns it.

    Always under the user's root, including for a project whose home is a
    shared root. That is what makes a shared project explorable rather than
    merely visible: the gates, ROIs and plugin tables a user produces while
    looking at somebody else's data are theirs, and they have to land
    somewhere writable.
    """
    return data_root() / name


def project_roots(name) -> list[Path]:
    """Every root with a directory for `name`, the user's own first.

    Directory existence only -- ownership is a question for the registry. Used
    to find derived artifacts (tile pyramids, centroid tiles) that may have
    been built into either the home root or the user's own.
    """
    return [root for root in roots() if (root / name).is_dir()]


def is_writable(root) -> bool:
    """Whether a root accepts writes, probed once per process per root.

    Shared roots are usually read-only to the people opening them, and that is
    the whole question for derived artifacts: a tile pyramid that already
    exists beside a shared image should be read where it is, but one that has
    to be built has to go somewhere this user can actually write.
    """
    path = Path(root)
    key = f"writable:{path}"
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            return bool(cached)
    probe = path / _probe_name()
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError:
        writable = False
    with _cache_lock:
        _cache[key] = writable
    return writable


def derived_root(name, home_root=None) -> Path:
    """Where derived artifacts for `name` are BUILT -- pyramids, tile caches.

    The project's own root when that can be written to, so two users opening
    the same shared image share one pyramid rather than each spending minutes
    building their own. Otherwise the user's own root, because a derived
    artifact that cannot be written is a feature that does not work.

    Reading is the other half and is not symmetric -- use `find_derived`, which
    also looks where a previous build may have put something.
    """
    if home_root is not None and is_writable(home_root):
        return Path(home_root) / name
    for root in project_roots(name):
        if is_writable(root):
            return root / name
    return project_state_dir(name)


#: Where a newly derived label pyramid is written. "beside" puts it next to the
#: mask it came from, so a second project built from that mask -- and a data
#: node pointed at it, which has no project to look under -- find the
#: conversion already done. "project" keeps it under the project's own
#: directory, which is what to choose when the mask lives somewhere that should
#: not accumulate large files: a synced folder, or a directory under backup.
#:
#: Either way BOTH places are searched before anything is built, so changing
#: this never orphans a pyramid or forces a rebuild.
MASK_OUTPUT_CHOICES = ("beside", "project")


def mask_output_preference() -> str:
    """Which of MASK_OUTPUT_CHOICES is in force.

    Deliberately not cached, unlike the roots above. This is read once per
    import and once per project load, where a settings-file read costs nothing
    -- and caching it would mean `plexora config set` did not reach a server
    that was already running.

    An unrecognised value reads as the default rather than raising. The CLI
    validates what it writes, so anything else arrived by a hand-edited
    settings file or an environment variable, and neither is worth refusing to
    start over.
    """
    raw = os.environ.get(ENV_MASK_OUTPUT) or read_settings().get("mask_output") or ""
    value = str(raw).strip().lower()
    return value if value in MASK_OUTPUT_CHOICES else MASK_OUTPUT_CHOICES[0]


def find_derived(name, *parts, home_root=None) -> Path | None:
    """An existing derived artifact, or None.

    Looks in the project's own root before the user's own, so a pyramid the
    site built beside a shared image wins over a stale private copy. Returns
    None rather than a non-existent path: callers use that to decide whether
    to build, and a path that merely might exist cannot answer that.
    """
    seen: list[Path] = []
    if home_root is not None:
        seen.append(Path(home_root) / name)
    seen.extend(root / name for root in project_roots(name))
    seen.append(project_state_dir(name))
    for base in seen:
        candidate = base.joinpath(*parts) if parts else base
        if candidate.exists():
            return candidate
    return None


def figures_root() -> Path:
    """Where this user's figures live.

    Never a shared root. A figure can span several datasources or none, so no
    project owns one and there is nothing for a site-managed root to hold.
    """
    return data_root() / FIGURES_DIRNAME


def describe() -> list[str]:
    """Human-readable lines for `plexora where`."""
    resolution = data_root_resolution()
    lines = [f"data root:    {resolution.path}", f"  chosen by:  {resolution.rule}"]
    shared = shared_root_resolutions()
    if not shared:
        lines.append("shared roots: (none)")
        return lines
    for entry in shared:
        state = "" if entry.path.is_dir() else "  [missing]"
        lines.append(f"shared root:  {entry.path}{state}")
        lines.append(f"  chosen by:  {entry.rule}")
    return lines
