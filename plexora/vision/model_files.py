"""Model weights fetched once from a release and proven intact before use.

Every model Plexora downloads (magic select's, the gating plugin's cell
model) lives outside the wheel as a few files pinned by sha256 and size in
its own weights module (`sam_weights.MODEL_FILES`). This module is what they
share: a file only gets its real name after its checksum matched, so a
download that dies halfway, or arrives different, never becomes a file a
runtime would load.

`files` everywhere is `{name: {"sha256", "bytes", ...}}`; `base` is the
release URL the names are appended to.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from plexora import paths

_CHUNK = 1 << 20
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 60.0


class WeightsCorrupt(RuntimeError):
    """A downloaded file did not match its pinned checksum."""


class DownloadCancelled(RuntimeError):
    """`cancelled()` said stop."""


def release_base(default: str, env: str) -> str:
    """`default`, or the mirror the environment variable `env` names (for a
    firewalled site; the files are verified either way)."""
    raw = (os.environ.get(env) or "").strip()
    base = raw or default
    return base if base.endswith("/") else base + "/"


def total_bytes(files: dict) -> int:
    return sum(entry["bytes"] for entry in files.values())


# Verification is a full read of the file; the result is remembered per
# (path, size, mtime) so a status polled once a second by a downloading page
# stays a stat, not a hash.
_VERIFIED: dict[tuple[str, int, int], bool] = {}
_VERIFIED_LOCK = threading.Lock()


def _stamp(path: Path):
    try:
        info = path.stat()
    except OSError:
        return None
    return (str(path), info.st_size, info.st_mtime_ns)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(path: Path, sha256: str) -> bool:
    stamp = _stamp(path)
    if stamp is None:
        return False
    with _VERIFIED_LOCK:
        known = _VERIFIED.get(stamp)
    if known is not None:
        return known
    ok = sha256_of(path) == sha256
    with _VERIFIED_LOCK:
        _VERIFIED[stamp] = ok
    return ok


def file_status(directory: Path, files: dict, *, deep: bool = False) -> dict:
    """Per file: present, size matches, and (when `deep`) checksum matches.

    The cheap form trusts a file of the right size; that is safe because a
    file only gets its real name after its checksum was verified (download)
    or because somebody placed it there by hand (offline directory), and a
    runtime verifies once more before loading.
    """
    directory = Path(directory)
    out = {}
    for name, entry in files.items():
        path = directory / name
        try:
            size = path.stat().st_size
        except OSError:
            out[name] = {"present": False, "ok": False, "bytes": entry["bytes"]}
            continue
        ok = size == entry["bytes"]
        if ok and deep:
            ok = verify(path, entry["sha256"])
        out[name] = {"present": True, "ok": ok, "bytes": entry["bytes"]}
    return out


def missing(directory: Path, files: dict, *, deep: bool = False) -> list[str]:
    return [name for name, row in file_status(directory, files, deep=deep).items()
            if not row["ok"]]


def writable(directory: Path) -> bool:
    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return paths.is_writable(Path(directory))


def download(directory: Path, files: dict, base: str, *, progress=None, cancelled=None,
             force: bool = False) -> Path:
    """Fetch every missing file of `files` from `base`, verified, atomically.

    `progress(done_bytes, total_bytes, file_name)` is called as bytes arrive,
    across all files, so one bar can cover the whole setup. `cancelled()` is
    polled per chunk. Bytes stream into `<name>.part` and are hashed as they
    arrive; only a matching file is renamed into place.
    """
    import urllib3

    directory = Path(directory)
    if not writable(directory):
        raise PermissionError(f"Cannot write to {directory}")
    total = total_bytes(files)
    todo = [name for name in files
            if force or not (directory / name).exists()
            or not verify(directory / name, files[name]["sha256"])]
    done = sum(files[name]["bytes"] for name in files if name not in todo)
    if progress:
        progress(done, total, todo[0] if todo else None)
    if not todo:
        return directory

    pool = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
        retries=urllib3.Retry(total=3, redirect=5, backoff_factor=0.5),
    )
    for name in todo:
        entry = files[name]
        target = directory / name
        part = directory / (name + ".part")
        digest = hashlib.sha256()
        response = pool.request("GET", base + name, preload_content=False,
                                redirect=True)
        try:
            if response.status != 200:
                raise OSError(f"{base + name} answered HTTP {response.status}")
            with open(part, "wb") as handle:
                for block in response.stream(_CHUNK):
                    if cancelled and cancelled():
                        raise DownloadCancelled("cancelled")
                    handle.write(block)
                    digest.update(block)
                    done += len(block)
                    if progress:
                        progress(min(done, total), total, name)
        except BaseException:
            unlink(part)
            raise
        finally:
            response.release_conn()
        if digest.hexdigest() != entry["sha256"]:
            unlink(part)
            raise WeightsCorrupt(
                f"{name} did not match its checksum; nothing was installed.")
        os.replace(part, target)
        with _VERIFIED_LOCK:
            stamp = _stamp(target)
            if stamp:
                _VERIFIED[stamp] = True
    return directory


def remove(directory: Path, files: dict) -> list[str]:
    """Delete the files (and any partial download) and the directory's
    `cache/`. Returns the names removed."""
    directory = Path(directory)
    gone = []
    for name in files:
        for candidate in (directory / name, directory / (name + ".part")):
            if candidate.exists():
                unlink(candidate)
                gone.append(candidate.name)
    cache = directory / "cache"
    if cache.is_dir():
        import shutil

        shutil.rmtree(cache, ignore_errors=True)
    return gone


def unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
