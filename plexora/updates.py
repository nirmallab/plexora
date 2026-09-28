"""Is there a newer Plexora, and can this process install it?

Help > Check for Updates asks three questions, and they are answered here so
that each can be tested without a server, a network or a pip:

- What is running (`current_version`) and what is published (`fetch_latest`,
  from PyPI's JSON API -- the same index `pip install plexora` reads).
- Whether *this* install can be upgraded in place (`install_kind`). A pip
  install into a writable environment can; a source checkout, a read-only
  site-packages and a container cannot, and for those the dialog shows the
  command to run instead of a button that would fail.
- What to run (`pip_command`), pinned to exactly the version the dialog
  showed, and re-requesting whichever extras are installed now, so an extra
  that gained a dependency in the new release gets it.

The desktop app is not updated from here at all: its Python lives inside a
signed, read-only bundle, and the Tauri shell replaces the whole bundle. This
module only reports `desktop` for it so the route can say so.

Stdlib only at import time. Nothing here is imported until a route asks.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

#: Where the published versions are read from. `PLEXORA_UPDATE_INDEX`
#: overrides it -- a URL or a local path to a file of the same shape -- so a
#: test, or a dry run in a scratch environment, never touches PyPI.
PYPI_URL = "https://pypi.org/pypi/plexora/json"
from plexora.links import GITHUB_REPO  # noqa: E402  (one copy, see plexora/links.py)
RELEASE_URL = f"https://github.com/{GITHUB_REPO}/releases/tag/v{{version}}"
RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/v{{version}}"
ISSUES_URL = f"https://github.com/{GITHUB_REPO}/issues/new"

#: Seconds before a slow index counts as offline. The check runs behind a
#: dialog that says "Checking…", and a spinner that turns for a minute is worse
#: than "could not reach PyPI".
FETCH_TIMEOUT = 5.0

#: How long one answer from the index is reused. The auto-check fires once a
#: day; this only stops several tabs, or a dialog opened twice, asking again.
CACHE_SECONDS = 6 * 3600

#: Extras that are never re-requested: `dev` is the test tooling, and asking
#: for it on a user's machine would install pytest into their environment.
SKIPPED_EXTRAS = frozenset({"dev"})

#: Settings keys, all under one `updates` object in the settings file.
SETTINGS_KEY = "updates"

_cache: dict = {}
_cache_lock = threading.Lock()


# -- versions ---------------------------------------------------------------


def _metadata_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return None
    try:
        return version("plexora")
    except PackageNotFoundError:
        return None


@functools.lru_cache(maxsize=1)
def current_version() -> str | None:
    """The version this process is running, or None for an uninstalled checkout.

    One place for what three modules used to look up separately. Each caller
    keeps its own wording for None, because "source" in an MCP payload and
    "unknown (source checkout)" from `--version` are different sentences.

    Cached, and that is load-bearing rather than an optimisation:
    `importlib.metadata` reads dist-info afresh on every call, so the moment
    pip finishes it names the NEW version while every imported module is
    still the old one. The update routes call this at import, which is server
    start, so the answer is the version that is actually running.
    """
    return _metadata_version()


_VERSION_RE = re.compile(
    r"^\s*v?(\d+(?:\.\d+)*)"
    r"(?:[-_.]?(a|b|c|rc|alpha|beta|pre|preview)[-_.]?(\d*))?"
    r"(?:[-_.]?(post|rev|r)[-_.]?(\d*))?"
    r"(?:[-_.]?(dev)[-_.]?(\d*))?"
    r"(?:\+.*)?\s*$",
    re.IGNORECASE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2,
             "pre": 2, "preview": 2}


def parse_version(text):
    """A sortable key for a PEP 440 version, or None when it is not one.

    `packaging` would do this, and is present in nearly every environment --
    but only transitively, and a version check that fails to import is a
    worse outcome than forty lines of regex. Covers what Plexora publishes:
    release numbers, a/b/rc, .postN and .devN.
    """
    match = _VERSION_RE.match(str(text or ""))
    if not match:
        return None
    release, pre, pre_n, post, post_n, dev, dev_n = match.groups()
    numbers = [int(part) for part in release.split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    # A dev release sorts before its pre-releases, which sort before the
    # final; post-releases sort after it. Tuples compare left to right, so
    # each marker is a rank followed by its number.
    if pre:
        pre_key = (_PRE_RANK[pre.lower()], int(pre_n or 0))
    elif dev and not post:
        pre_key = (-1, 0)
    else:
        pre_key = (3, 0)
    post_key = int(post_n or 0) if post else -1
    dev_key = int(dev_n or 0) if dev else float("inf")
    return (tuple(numbers), pre_key, post_key, dev_key)


def is_prerelease(text) -> bool:
    key = parse_version(text)
    return key is not None and (key[1][0] < 3 or key[3] != float("inf"))


def is_newer(candidate, current) -> bool:
    """True when `candidate` is a later version than `current`.

    An unparseable current version (a checkout, "unknown") is never behind:
    offering to replace something that cannot be named is how an editable
    install gets overwritten by a wheel.
    """
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    return a > b


# -- the index --------------------------------------------------------------


def _index_location():
    return os.environ.get("PLEXORA_UPDATE_INDEX") or PYPI_URL


def _read_json(location, timeout=FETCH_TIMEOUT):
    """JSON from a URL, a file:// URL or a plain path."""
    if location.startswith("file://"):
        from urllib.parse import unquote, urlparse

        location = unquote(urlparse(location).path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:", location):
            location = location[1:]
    if not re.match(r"^https?://", location):
        return json.loads(Path(location).read_text(encoding="utf-8"))

    from urllib.request import Request, urlopen

    request = Request(location, headers={
        "Accept": "application/json",
        "User-Agent": f"plexora/{current_version() or 'unknown'} update-check",
    })
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_from_index(payload, *, include_prereleases=False):
    """The newest installable version in a PyPI JSON document, or None.

    Read from `releases` rather than trusting `info.version`: a release whose
    every file is yanked is one pip will not pick for `==X` without a warning,
    and a release with no files at all (a name reserved, an upload that
    failed) is not installable. Pre-releases are left out unless asked for.
    """
    releases = (payload or {}).get("releases") or {}
    best = best_key = None
    for version, files in releases.items():
        if not files or all(bool(f.get("yanked")) for f in files):
            continue
        key = parse_version(version)
        if key is None:
            continue
        if is_prerelease(version) and not include_prereleases:
            continue
        if best_key is None or key > best_key:
            best, best_key = version, key
    if best is None:
        info_version = ((payload or {}).get("info") or {}).get("version")
        if info_version and parse_version(info_version) and (
                include_prereleases or not is_prerelease(info_version)):
            best = info_version
    return best


def release_notes(version, *, fetch=None):
    """(notes, url) for a GitHub release, or ("", url) when it cannot be read.

    Best effort by design. The notes are a courtesy on the way to the button;
    GitHub's API rate-limits anonymous callers at sixty requests an hour, and
    a check that failed because the changelog was unavailable would be
    refusing to answer the question it was actually asked.
    """
    fetch = fetch or _read_json
    url = RELEASE_URL.format(version=version)
    try:
        body = fetch(RELEASE_API.format(version=version))
    except Exception:
        return "", url
    notes = str((body or {}).get("body") or "").strip()
    return notes[:8000], str((body or {}).get("html_url") or url)


def fetch_latest(*, force=False, fetch=None, now=time.monotonic):
    """{latest, notes, notes_url} from the index, cached for CACHE_SECONDS.

    Raises whatever the fetch raised when the index cannot be read; the route
    turns that into an `error` for the dialog. The cache is keyed by where it
    was read from, so a test that points PLEXORA_UPDATE_INDEX somewhere else
    never sees an answer from before.
    """
    fetch = fetch or _read_json
    location = _index_location()
    current = current_version()
    with _cache_lock:
        hit = _cache.get(location)
        if hit and not force and now() - hit[0] < CACHE_SECONDS:
            return dict(hit[1])
    payload = fetch(location)
    latest = latest_from_index(
        payload, include_prereleases=bool(current and is_prerelease(current)))
    notes, notes_url = ("", "")
    if latest:
        notes, notes_url = release_notes(latest, fetch=fetch)
    result = {"latest": latest, "notes": notes, "notes_url": notes_url}
    with _cache_lock:
        _cache[location] = (now(), dict(result))
    return result


def _reset_cache_for_tests():
    with _cache_lock:
        _cache.clear()


# -- this install -----------------------------------------------------------


def _distribution():
    try:
        from importlib.metadata import PackageNotFoundError, distribution
    except ImportError:  # pragma: no cover
        return None
    try:
        return distribution("plexora")
    except PackageNotFoundError:
        return None


def _direct_url(dist):
    try:
        text = dist.read_text("direct_url.json")
    except Exception:
        return {}
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _installed(name):
    try:
        from importlib.metadata import PackageNotFoundError, distribution
    except ImportError:  # pragma: no cover
        return False
    try:
        distribution(name)
        return True
    except PackageNotFoundError:
        return False


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXTRA_MARKER = re.compile(r"""extra\s*==\s*["']([^"']+)["']""")


def installed_extras(requires=None, *, installed=_installed):
    """The extras of Plexora whose every requirement is installed now.

    Read off the package's own `Requires-Dist` lines, so a new extra needs no
    change here. An extra with only some of its packages present is left out:
    it was not asked for, a dependency of core simply overlaps it -- `spatial`
    is pyarrow, which pandas pulls in everywhere, and is the usual example.
    That case is harmless either way, since asking for an extra that is
    already satisfied installs nothing.
    """
    if requires is None:
        dist = _distribution()
        requires = list(dist.requires or []) if dist is not None else []
    by_extra: dict[str, list[str]] = {}
    for line in requires:
        requirement, _, marker = str(line).partition(";")
        found = _EXTRA_MARKER.search(marker)
        if not found:
            continue
        name = _REQ_NAME.match(requirement)
        if not name:
            continue
        by_extra.setdefault(found.group(1), []).append(name.group(1))
    return sorted(
        extra for extra, names in by_extra.items()
        if extra not in SKIPPED_EXTRAS and names
        and all(installed(n) for n in names))


def requirement_spec(version, extras=()):
    extras = sorted(set(extras))
    suffix = f"[{','.join(extras)}]" if extras else ""
    return f"plexora{suffix}=={version}"


def pip_command(version, *, extras=None, python=None, uv=None):
    """argv that installs exactly `version`, keeping today's extras.

    `only-if-needed` so that the upgrade moves Plexora and whatever its new
    pins demand, and nothing else: a lab environment's numpy is not ours to
    bump just because a newer one exists. With no pip in the environment (a
    uv-made venv), uv installs into this interpreter instead.
    """
    python = python or sys.executable
    spec = requirement_spec(version, installed_extras() if extras is None else extras)
    if uv:
        return [uv, "pip", "install", "--python", python, spec]
    return [python, "-m", "pip", "install", "--disable-pip-version-check",
            "--upgrade-strategy", "only-if-needed", spec]


def _quote(argv):
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    import shlex

    return " ".join(shlex.quote(part) for part in argv)


def _has_pip():
    import importlib.util

    try:
        return importlib.util.find_spec("pip") is not None
    except (ImportError, ValueError):
        return False


def _in_container():
    return Path("/.dockerenv").exists() or bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def install_kind(*, desktop=False, dist=..., writable=None, has_pip=None,
                 uv=..., container=None):
    """How this install can be upgraded, as {kind, can_install, reason}.

    `kind` is one of `desktop`, `editable`, `readonly`, `container`,
    `no_pip` or `pip`. Only `pip` (and `no_pip` with uv on PATH) can install
    in place; every other kind carries the sentence the dialog shows in place
    of the button. The keyword arguments exist for the tests.
    """
    if desktop:
        return {"kind": "desktop", "can_install": False, "uv": None,
                "reason": "The desktop app updates itself from its own window."}
    dist = _distribution() if dist is ... else dist
    if dist is None:
        return {"kind": "editable", "can_install": False, "uv": None,
                "reason": "This Plexora runs from a source checkout that was "
                          "never installed; update it with git."}
    direct = _direct_url(dist)
    if (direct.get("dir_info") or {}).get("editable"):
        return {"kind": "editable", "can_install": False, "uv": None,
                "reason": "This is an editable install of a source checkout. "
                          "Update it with git; pip would replace it with a "
                          "copy."}
    if container is None:
        container = _in_container()
    if container:
        return {"kind": "container", "can_install": False, "uv": None,
                "reason": "Plexora is running in a container. An upgrade made "
                          "inside it is lost when it restarts; update the "
                          "image instead."}
    if writable is None:
        try:
            site = Path(dist.locate_file(""))
            writable = os.access(site, os.W_OK)
        except Exception:
            writable = False
    if not writable:
        return {"kind": "readonly", "can_install": False, "uv": None,
                "reason": "The environment Plexora is installed in is not "
                          "writable by this account. Ask whoever manages it, "
                          "or run the command below where you can."}
    if has_pip is None:
        has_pip = _has_pip()
    if not has_pip:
        uv = shutil.which("uv") if uv is ... else uv
        if uv:
            return {"kind": "no_pip", "can_install": True, "uv": uv,
                    "reason": "This environment has no pip; uv installs "
                              "into it instead."}
        return {"kind": "no_pip", "can_install": False, "uv": None,
                "reason": "This environment has no pip, so Plexora cannot "
                          "upgrade itself here."}
    return {"kind": "pip", "can_install": True, "uv": None, "reason": ""}


def manual_command(version, kind):
    """The line somebody would type, for the dialog's Copy button."""
    if kind.get("kind") == "editable":
        return "git pull && pip install -e ."
    if kind.get("kind") == "container":
        return f"pip install --upgrade-strategy only-if-needed '{requirement_spec(version or '<version>', installed_extras())}'"
    return _quote(pip_command(version or "<version>", uv=kind.get("uv")))


# -- settings ---------------------------------------------------------------


def read_prefs():
    """The `updates` block of the settings file, with its defaults filled in."""
    from plexora import paths

    stored = paths.read_settings().get(SETTINGS_KEY)
    stored = stored if isinstance(stored, dict) else {}
    return {
        "auto_check": bool(stored.get("auto_check", True)),
        "skipped_version": stored.get("skipped_version") or None,
        "last_checked": stored.get("last_checked") or None,
        "last_latest": stored.get("last_latest") or None,
    }


def write_prefs(**changes):
    """Merge `changes` into the `updates` block and write the file back."""
    from plexora import paths

    data = paths.read_settings()
    block = data.get(SETTINGS_KEY)
    block = dict(block) if isinstance(block, dict) else {}
    for key, value in changes.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    data[SETTINGS_KEY] = block
    paths.write_settings(data)
    return read_prefs()


# -- the install job --------------------------------------------------------


class InstallJob:
    """One pip run in a background thread, with the tail of its output.

    There is only ever one: two upgrades racing into one site-packages is how
    an environment ends up with half of each. The log is kept to the last few
    hundred lines -- it is shown in a dialog, and pip's resolver can be chatty.
    """

    LOG_LINES = 400

    def __init__(self, version, command, *, popen=subprocess.Popen, env=None):
        self.version = version
        self.command = list(command)
        self.state = "running"
        self.returncode = None
        self.started = time.time()
        self._lines = deque(maxlen=self.LOG_LINES)
        self._lock = threading.Lock()
        self._popen = popen
        self._env = env
        self._lines.append("$ " + _quote(self.command))
        self.thread = threading.Thread(target=self._run, name="plexora-update",
                                       daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _log(self, line):
        with self._lock:
            self._lines.append(line.rstrip("\r\n"))

    def _run(self):
        from plexora._subprocess import popen_kwargs

        env = dict(os.environ if self._env is None else self._env)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PIP_NO_INPUT"] = "1"
        try:
            process = self._popen(
                self.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", bufsize=1, env=env, **popen_kwargs())
            for line in process.stdout:
                self._log(line)
            returncode = process.wait()
        except Exception as exc:  # the command itself could not start
            self._log(f"Could not run the installer: {exc}")
            returncode = -1
        with self._lock:
            self.returncode = returncode
            self.state = "done" if returncode == 0 else "failed"

    def snapshot(self, tail=60):
        with self._lock:
            lines = list(self._lines)[-tail:]
            return {"state": self.state, "version": self.version,
                    "returncode": self.returncode, "log_tail": lines}


_job: InstallJob | None = None
_job_lock = threading.Lock()


def current_job():
    return _job


def start_install(version, command, **kwargs):
    """Start the one install job, or return None if one is still running."""
    global _job
    with _job_lock:
        if _job is not None and _job.state == "running":
            return None
        _job = InstallJob(version, command, **kwargs).start()
        return _job


def _reset_job_for_tests():
    global _job
    with _job_lock:
        _job = None


def installed_on_disk():
    """The version pip has put on disk now, which a running process cannot see.

    `importlib.metadata` reads the dist-info directory afresh on each call,
    so after an upgrade it already names the new version while the modules
    this process imported are still the old ones. That gap is exactly what
    the dialog's "restart to finish" state is about.
    """
    return _metadata_version()
