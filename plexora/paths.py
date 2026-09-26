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

#: A data directory *suggested* by whoever launched this process -- in practice
#: a saved connection profile, whose `data_dir` reaches the remote host this
#: way. It sits below the settings file rather than above it because the
#: account being connected to is the one that knows where its own work is. A
#: laptop that outranks it produces the split this channel exists to prevent:
#: `plexora dataset create` over ssh wrote to the account's recorded directory
#: while the viewer, launched by the profile, read a different one, and the
#: dataset was invisible with no error on either side.
ENV_DATA_PATH_DEFAULT = "PLEXORA_DATA_PATH_DEFAULT"

ENV_SHARED_PATH = "PLEXORA_SHARED_PATH"
ENV_MASK_OUTPUT = "PLEXORA_MASK_OUTPUT"

CONFIG_FILENAME = "config.json"
SETTINGS_FILENAME = "settings.json"

#: The rule that comes from the platform rather than from anything the user
#: said. Named so the notice code can ask "did somebody choose this?" without
#: matching a sentence that may later be reworded. There used to be a second
#: one, for frozen builds that kept their data beside the executable; the
#: desktop app ships a real interpreter instead, so it shares this default
#: with the CLI and notebooks.
RULE_PLATFORM_DEFAULT = "platform default"

#: The first line of the two-used-directories refusal, fixed so that
#: `plexora connect` can recognise it in a remote's dying output and relay the
#: explanation instead of reporting that the remote exited. Changing the
#: wording here silently downgrades that diagnosis to "exited unexpectedly".
CONFLICT_MARKER = "Two data directories hold Plexora work for this account."

#: Directory under a root holding every figure. Dot-prefixed so a project
#: literally named "figures" cannot collide with it -- projects are directories
#: under the same root. See figure_builder's repository module.
FIGURES_DIRNAME = ".figures"

#: Directory under a root holding captures that are not in a figure yet -- the
#: captures bin. Dot-prefixed for the same reason as FIGURES_DIRNAME, and a
#: sibling of it rather than a table inside a figure: a capture in the bin
#: belongs to no figure by definition, which is the whole point of it. See
#: figure_builder's captures module.
CAPTURES_DIRNAME = ".captures"

#: Where what an external agent did is kept: the audit log of every mutation it
#: attempted and the rendered evidence it was shown. Dot-prefixed like the two
#: above, so nothing mistakes it for a project folder, and never on a shared
#: root -- the actions are this user's.
AGENT_DIRNAME = ".agent"

#: Directory under a root holding the bytes of images and tables read from a
#: web address -- the chunk cache `server/utils/remote_store.py` keeps. A
#: cache and nothing else: every file in it can be fetched again, which is why
#: a data-directory move leaves it behind (`data_migration.migratable`).
REMOTE_CACHE_DIRNAME = ".remote_cache"

#: How much disk the remote chunk cache may use, in bytes, when neither the
#: settings file nor the environment says. Ten gigabytes holds several whole
#: IDR images and every coarse level of hundreds more.
REMOTE_CACHE_DEFAULT_BYTES = 10 * 1024 ** 3

#: The smallest budget anyone may set. Below one gigabyte a single large
#: shard evicts everything else, and the cache stops being one.
REMOTE_CACHE_MIN_BYTES = 1024 ** 3

ENV_REMOTE_CACHE_BYTES = "PLEXORA_REMOTE_CACHE_BYTES"

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

    stored = _stored_data_dir()
    if stored is not None:
        return Resolution(stored, f"data_dir in {settings_path()}")

    # Below the settings file, deliberately. A suggestion is what somebody on
    # another machine thinks this account's data directory should be; the
    # account's own recorded answer knows better, and if it has one the
    # suggestion is reconciled against it rather than applied.
    suggested = _suggested_data_dir()
    if suggested is not None:
        return Resolution(suggested,
                          f"{ENV_DATA_PATH_DEFAULT}, suggested by the connection")

    return Resolution(Path(user_data_dir(APP_NAME, appauthor=False)).resolve(),
                      RULE_PLATFORM_DEFAULT)


def _stored_data_dir() -> Path | None:
    """The account's own recorded data directory, resolved, or None."""
    stored = read_settings().get("data_dir")
    if isinstance(stored, str) and stored.strip():
        return Path(stored).expanduser().resolve()
    return None


def _suggested_data_dir() -> Path | None:
    """The data directory this process was launched with a suggestion of."""
    raw = os.environ.get(ENV_DATA_PATH_DEFAULT)
    if raw and raw.strip():
        return Path(raw).expanduser().resolve()
    return None


def _registry_size(root) -> int | None:
    """How many projects a root's registry lists, or None if it has no registry.

    The question being asked is "does this directory hold Plexora work?", and
    the count is what makes the answer checkable by a user looking at two paths
    that differ by one path segment. Tolerant of a damaged file for the same
    reason `read_settings` is: a registry that will not parse is still a
    registry, and reporting "no work here" about it would be the wrong half of
    the answer to base a refusal on.

    Re-implements the count rather than calling `project.read_config`, because
    this module imports nothing from `plexora` and has to keep it that way --
    the CLI resolves the root before the package is importable.
    """
    path = Path(root) / CONFIG_FILENAME
    try:
        # ValueError as well as OSError: a UnicodeDecodeError on a registry
        # that is not text at all is one of these, and must read as "cannot
        # say" rather than crash the message that was explaining the problem.
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if not text.strip():
        return 0
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return len(data) if isinstance(data, dict) else None


def _holds_work(root) -> bool:
    """Whether a root has a project registry at all.

    Existence of `config.json`, not a non-zero project count: a root that has
    been opened and emptied is still an answer somebody gave, and silently
    adopting a different directory over it is the surprise being removed.
    """
    return (Path(root) / CONFIG_FILENAME).exists()


def _reconcile_suggestion(resolution: Resolution) -> None:
    """Settle a suggested data directory against the account's own answer.

    Runs after the winning root is proven writable and before it is cached, so
    that a directory is only ever recorded once Plexora has shown it can
    actually write there. Records what it decided in the cache; the wording
    lives in `data_root_notices`.

    The refusal case is the one worth arguing for. When the account has
    recorded one directory and the connection suggests another, and *both* hold
    a registry, there is no answer this code can pick that is not somebody's
    work disappearing -- so it picks neither and says so. Every other case
    resolves silently-but-audibly: an unclaimed account adopts the suggestion,
    a claimed one keeps its own.
    """
    stored = _stored_data_dir()
    suggested = _suggested_data_dir()

    if os.environ.get(ENV_DATA_PATH, "").strip():
        # An explicit per-process instruction is the one thing that is allowed
        # to win outright, so nothing is reconciled -- but a setting it is
        # stepping over is worth a line, because that setting is what every
        # other process on this account is using.
        if stored is not None and stored != resolution.path:
            with _cache_lock:
                _cache["shadowed_setting"] = stored
        return

    if suggested is None:
        return

    if stored is None:
        # A vacuum. Adopting into it is the only write this module makes to the
        # settings file, and it is what stops the next `plexora dataset create`
        # on this account from landing somewhere the viewer will not look.
        try:
            write_settings({**read_settings(), "data_dir": str(resolution.path)})
        except OSError as exc:
            # A quota'd home directory on a cluster is the realistic cause.
            # The suggestion still governs this process; it just will not
            # govern the next one, and the notice has to say so.
            with _cache_lock:
                _cache["adopt_failed"] = str(exc)
            return
        with _cache_lock:
            _cache["adopted"] = resolution.path
        return

    if stored == suggested:
        return

    if _holds_work(suggested):
        counts = []
        for label, path in (("this account's setting", stored),
                            ("the connection's suggestion", suggested)):
            size = _registry_size(path)
            if size is None:
                tail = "registry unreadable"
            else:
                tail = f"{size} project{'' if size == 1 else 's'}"
            counts.append(f"  {path}  ({label}, {tail})")
        raise DataRootError(
            CONFLICT_MARKER + "\n"
            + "\n".join(counts) + "\n"
            "Plexora will not choose between them. Either keep the account's "
            "own directory and clear the data directory on the connection "
            "profile, or run 'plexora config set data-dir "
            f"{suggested}' on this host to make the suggestion the account's "
            "answer."
        )

    with _cache_lock:
        _cache["ignored_suggestion"] = suggested


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
        # Creating the platform default is what a first run looks like and
        # needs no comment. Creating a directory somebody *named* means the
        # name was wrong, or the filesystem lost it -- a purged scratch volume
        # on a cluster -- and either way the user is about to see an install
        # that looks brand new when it is not.
        _cache["created_explicit"] = (
            not existed and resolution.rule != RULE_PLATFORM_DEFAULT
        )

    _reconcile_suggestion(resolution)
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


def data_root_notices() -> list[str]:
    """Everything worth saying out loud about how the root was chosen.

    Separate from `first_run_notice` rather than folded into it: that one
    answers "you have never run this before", which is true once per install,
    while these answer "the sources disagreed and here is what happened", which
    can be true on any session and must not be suppressed by having seen the
    first-run line already.

    Empty on the ordinary path. Every line here exists because the alternative
    is a user looking at an empty project list with nothing on screen to
    explain it.
    """
    resolution = data_root_resolution()
    with _cache_lock:
        adopted = _cache.get("adopted")
        adopt_failed = _cache.get("adopt_failed")
        ignored = _cache.get("ignored_suggestion")
        shadowed = _cache.get("shadowed_setting")
        created_explicit = _cache.get("created_explicit")

    lines: list[str] = []
    if adopted is not None:
        lines.append(
            f"Recorded {adopted} as this account's data directory, from the "
            f"connection profile. Every Plexora command on this host will use "
            f"it. Change it with 'plexora config set data-dir <path>'."
        )
    if adopt_failed is not None:
        lines.append(
            f"Using {resolution.path}, suggested by the connection, but it "
            f"could not be recorded in {settings_path()} ({adopt_failed}). "
            f"Commands run on this host outside this session may use a "
            f"different directory."
        )
    if ignored is not None:
        lines.append(
            f"The connection suggested {ignored}; this account keeps its data "
            f"in {resolution.path}, so the suggestion was not used. Update the "
            f"connection profile to match."
        )
    if shadowed is not None:
        lines.append(
            f"{ENV_DATA_PATH} is set, so this process is using "
            f"{resolution.path}. The recorded data_dir {shadowed} is what "
            f"every other Plexora command on this account uses."
        )
    # Not when the directory was just adopted: an account choosing its data
    # directory for the first time is *expected* to have nothing in it, and
    # warning about that turns a successful setup into an alarm.
    if created_explicit and adopted is None:
        lines.append(
            f"{resolution.path} did not exist, so it was created empty "
            f"({resolution.rule}). If you expected projects here, they are "
            f"not in it."
        )
    return lines


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


def captures_root() -> Path:
    """Where this user's not-yet-assigned captures live.

    Never a shared root, for the same reason as `figures_root`: a capture is
    taken by one person while looking at an image, it belongs to no project and
    to no figure, and there is nothing for a site-managed root to hold. Its
    whole reason to exist is that the answer to "which figure?" has not been
    given yet, and the bin is where the capture waits safely until it is.
    """
    return data_root() / CAPTURES_DIRNAME


def agent_root() -> Path:
    """Where an external agent's audit log and rendered evidence live.

    Beside the figures and the captures bin, for the same reason: it belongs to
    the person whose agent it was, not to a project or a site.
    """
    return data_root() / AGENT_DIRNAME


def remote_cache_root() -> Path:
    """Where bytes read from web addresses are cached.

    The user's own root, never a shared one: what a person has looked at is
    theirs, and a site root is often read-only to the people using it.
    """
    return data_root() / REMOTE_CACHE_DIRNAME


def remote_cache_budget() -> int:
    """The remote chunk cache's byte budget.

    Not cached, for the reason `mask_output_preference` gives: a Settings save
    or `plexora config set remote-cache-gb` has to reach a running server.
    The environment wins over the settings file, as it does for every other
    setting here. Anything unreadable is the default rather than an error.
    """
    raw = os.environ.get(ENV_REMOTE_CACHE_BYTES)
    if raw in (None, ""):
        raw = read_settings().get("remote_cache_bytes")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return REMOTE_CACHE_DEFAULT_BYTES
    return value if value > 0 else REMOTE_CACHE_DEFAULT_BYTES


def _also_configured(winner: Path) -> list[str]:
    """The data directories that were named and did not win.

    The whole diagnosis for the split-account case is two paths side by side
    with their project counts, so `plexora where` has to show the loser as well
    as the winner -- printing only the winner is what let a profile override go
    unnoticed for a whole cohort.

    Only sources this account actually configured, plus the platform default
    when it holds work. Nothing is scanned for: a guessed directory that turns
    out to be somebody else's is a worse answer than a short list.
    """
    candidates: list[tuple[Path, str]] = []
    from_env = os.environ.get(ENV_DATA_PATH)
    if from_env and from_env.strip():
        candidates.append((Path(from_env).expanduser().resolve(),
                           f"{ENV_DATA_PATH} environment variable"))
    stored = _stored_data_dir()
    if stored is not None:
        candidates.append((stored, f"data_dir in {settings_path()}"))
    suggested = _suggested_data_dir()
    if suggested is not None:
        candidates.append((suggested, f"{ENV_DATA_PATH_DEFAULT}, suggested by the connection"))
    default = Path(user_data_dir(APP_NAME, appauthor=False)).resolve()
    if _holds_work(default):
        candidates.append((default, RULE_PLATFORM_DEFAULT))

    lines: list[str] = []
    seen: set[Path] = {winner}
    for path, rule in candidates:
        if path in seen:
            continue
        seen.add(path)
        size = _registry_size(path)
        if size is None:
            state = "exists" if path.is_dir() else "missing"
        else:
            state = f"exists, {size} project{'' if size == 1 else 's'}"
        lines.append(f"  {path}  ({state})")
        lines.append(f"    named by: {rule}")
    if not lines:
        return []
    return ["also configured, not in force:", *lines]


def describe() -> list[str]:
    """Human-readable lines for `plexora where`.

    Catches `DataRootError` rather than letting it out: `where` is the command
    somebody runs *because* Plexora refused to start, and a traceback from the
    diagnostic tool is the one response that leaves them with nothing.
    """
    try:
        resolution = data_root_resolution()
    except DataRootError as exc:
        return str(exc).splitlines()
    lines = [f"data root:    {resolution.path}", f"  chosen by:  {resolution.rule}"]
    lines.extend(_also_configured(resolution.path))
    shared = shared_root_resolutions()
    if not shared:
        lines.append("shared roots: (none)")
        return lines
    for entry in shared:
        state = "" if entry.path.is_dir() else "  [missing]"
        lines.append(f"shared root:  {entry.path}{state}")
        lines.append(f"  chosen by:  {entry.rule}")
    return lines
