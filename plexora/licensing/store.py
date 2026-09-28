"""Where the licence lives on disk, and which environment variables move it.

In the per-user CONFIG directory, never the data root. A data root can be a
shared project volume or an NFS mount other accounts can read; a licence is one
person's, and the config directory is theirs. On a cluster the config directory
is under `$HOME`, which every login node, compute node, Open OnDemand session,
JupyterHub server and bind-mounted container of that account shares -- so one
registration covers all of them without a copy anywhere.

    license/license.json      the certificate and what is known about it
    license/environment.json  this environment's secret (and delegation key)

Both are written 0600, chmod'd BEFORE the rename (the `secret_store` rule: a
rename-then-chmod leaves a window where the file is world-readable, and on a
shared `$HOME` that window is the threat). Neither is ever put in settings.json,
the data root or browser storage.

Resolution order, highest first -- an explicit instruction for this process
outranks what was installed earlier:

1. `PLEXORA_LICENSE_JOB_CERT`  a short-lived job certificate minted by a primary
2. `PLEXORA_LICENSE_TOKEN`     a licence token, exchanged for a certificate once
3. `PLEXORA_LICENSE_FILE`      a `.plexora` offline licence file
4. `license.json`              written by `plexora license activate|install`

`PLEXORA_LICENSE_OFFLINE=1` forbids every licensing network call.
`PLEXORA_LICENSE_DIR` relocates the directory; the test suite uses it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ENV_DIR = "PLEXORA_LICENSE_DIR"
ENV_TOKEN = "PLEXORA_LICENSE_TOKEN"
ENV_FILE = "PLEXORA_LICENSE_FILE"
ENV_JOB_CERT = "PLEXORA_LICENSE_JOB_CERT"
ENV_OFFLINE = "PLEXORA_LICENSE_OFFLINE"
ENV_SERVER = "PLEXORA_LICENSE_SERVER"
ENV_NO_HEARTBEAT = "PLEXORA_LICENSE_NO_HEARTBEAT"

LICENSE_FILENAME = "license.json"
ENVIRONMENT_FILENAME = "environment.json"
SCHEMA = 1

#: Where the licence service lives when `PLEXORA_LICENSE_SERVER` does not say.
#: Must match `PUBLIC_BASE_URL` in licensing/wrangler.toml. An empty value
#: means "no licensing service is configured": every online action says so,
#: and nothing is ever sent anywhere. Free never contacts it either way.
DEFAULT_SERVER = "https://license.plexoraapp.com"

_lock = threading.RLock()


def license_dir() -> Path:
    override = os.environ.get(ENV_DIR)
    if override and override.strip():
        return Path(override).expanduser()
    from platformdirs import user_config_dir

    return Path(user_config_dir("plexora", appauthor=False, roaming=False)) / "license"


def license_path() -> Path:
    return license_dir() / LICENSE_FILENAME


def environment_path() -> Path:
    return license_dir() / ENVIRONMENT_FILENAME


def _read_json(path: Path) -> dict:
    """A JSON object from `path`, or {} for missing, unreadable or damaged.

    Damage is not an error: it reads as "no licence", which is Free and is
    fixed by activating again -- never a Plexora that will not start.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_private_json(path: Path, data: dict) -> Path:
    """Replace `path` with `data`, atomically, readable by this user only."""
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                # Windows has no equivalent, and some network filesystems
                # refuse. Losing the write is worse than a wider mode on a file
                # already inside the user's own config directory.
                pass
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def create_private_json(path: Path, data: dict) -> bool:
    """Write `path` only if nothing is there yet. False when somebody else won.

    For the environment secret, which two jobs starting on two nodes of one
    cluster can both try to create in the same `$HOME` at once. A hard link is
    the exclusive create that also holds on NFS; a replace would let the second
    job silently mint a second identity -- a second registration -- for what
    is one cluster.
    """
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.new")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, path)
                return True
            except FileExistsError:
                return False
            except OSError:
                # A filesystem without hard links. Best effort: the window is
                # the exists() check, which is still far narrower than none.
                if path.exists():
                    return False
                os.replace(tmp, path)
                return True
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass


def read_license() -> dict:
    return _read_json(license_path())


def write_license(record: dict) -> Path:
    return write_private_json(license_path(), {"schema": SCHEMA, **record})


def update_license(**fields) -> dict | None:
    """Merge `fields` into license.json if there is one. Returns the record,
    or None when there was nothing to update or the write failed."""
    with _lock:
        record = read_license()
        if not record.get("certificate"):
            return None
        record.update(fields)
        try:
            write_license(record)
        except OSError:
            return None
        return record


def clear_license() -> bool:
    """Remove license.json. The environment secret is kept: it IS this
    environment, and the server still knows it by it."""
    with _lock:
        try:
            license_path().unlink()
            return True
        except OSError:
            return False


def read_environment() -> dict:
    return _read_json(environment_path())


def write_environment(record: dict) -> Path:
    return write_private_json(environment_path(), {"schema": SCHEMA, **record})


def clear_environment() -> bool:
    with _lock:
        try:
            environment_path().unlink()
            return True
        except OSError:
            return False


def _clean(value) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def env_token() -> str | None:
    return _clean(os.environ.get(ENV_TOKEN))


def env_file() -> str | None:
    return _clean(os.environ.get(ENV_FILE))


def env_job_cert() -> str | None:
    return _clean(os.environ.get(ENV_JOB_CERT))


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def offline_only() -> bool:
    return _truthy(ENV_OFFLINE)


def heartbeat_disabled() -> bool:
    return _truthy(ENV_NO_HEARTBEAT)


def server_url() -> str:
    """The licence service's base URL, or "" when none is configured."""
    return (_clean(os.environ.get(ENV_SERVER)) or DEFAULT_SERVER or "").rstrip("/")


def portal_url(page: str = "") -> str:
    """A page of the licence portal, or "" when no service is configured."""
    base = server_url()
    if not base:
        return ""
    return f"{base}/portal{('/' + page.lstrip('/')) if page else ''}"


def read_license_file(path) -> str:
    """The certificate inside a `.plexora` offline licence file.

    The certificate is on a line of its own, so the file can carry `#` comment
    lines naming who it was issued to and when it runs out -- a copy found on a
    shared filesystem is visibly somebody's. Raises ValueError when there is no
    certificate line, OSError when the file cannot be read.
    """
    from plexora.licensing.certificate import looks_like

    text = Path(path).expanduser().read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and looks_like(stripped):
            return stripped
    raise ValueError(f"no Plexora certificate in {path}")
