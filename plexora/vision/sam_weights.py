"""Where magic select's model weights come from, and proving they are intact.

The weights are not in the wheel: ~44 MB that most installs never use would
ship to every one of them. They are fetched once, the first time somebody arms
magic select, from one release this project controls, and each file is
checked against the sha256 pinned below before it is given its real name. A
download that dies halfway, or arrives different, never becomes a file the
runtime would load.

Users never see the model's name; this module is the one place it is spelled
out, together with its licence (Apache-2.0, recorded in the status payload,
the CLI output and exported provenance).

`tools/sam/export_mobile_sam.py` produces both files and prints the
`MODEL_FILES` entries below.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from plexora import paths

MODEL_NAME = "mobile_sam"
#: Bumped whenever the files change. It is part of every cache key that holds
#: a result computed with the model (QC's trace cache, the embedding cache),
#: so new weights never serve an outline the old ones drew.
MODEL_VERSION = "1"
MODEL_LICENSE = "Apache-2.0"
#: The input/output contract of the two files (see the export script).
MODEL_LAYOUT = "sam_lowres_v1"

#: The one release the files are downloaded from: a public repository of its
#: own, because the code repository is private and its releases need a login.
#: The environment variable is for a mirror inside a firewalled site; the
#: files are verified either way.
RELEASE_BASE = "https://github.com/nirmallab/plexora-models/releases/download/segment-model-v1/"
ENV_MODEL_URL = "PLEXORA_SEGMENT_MODEL_URL"

MODEL_FILES = {
    "mobile_sam_encoder.onnx": {
        "role": "encoder",
        "sha256": "4e0921311fdb401b57029287d5a49c9169e102cb4e6d16d28f39ce930820fa04",
        "bytes": 28016149,
    },
    "mobile_sam_decoder.onnx": {
        "role": "decoder",
        "sha256": "d296f48503b96ac3d4ba02ce2326c0a565fce9b9568c0faa9268d5559d608664",
        "bytes": 16492452,
    },
}

TOTAL_BYTES = sum(entry["bytes"] for entry in MODEL_FILES.values())

_CHUNK = 1 << 20
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 60.0


class SamWeightsMissing(RuntimeError):
    """The weights are not on disk. `INSTALL` is the line to show somebody."""

    INSTALL = "plexora ai segment install"

    def __init__(self, missing=()):
        names = ", ".join(missing) or "the model files"
        super().__init__(
            f"Magic select is not set up yet ({names} missing). It sets itself "
            f"up the first time it is used, or run: {self.INSTALL}")
        self.missing = list(missing)


class WeightsCorrupt(RuntimeError):
    """A downloaded file did not match its pinned checksum."""


class DownloadCancelled(RuntimeError):
    """`cancelled()` said stop."""


def model_dir() -> Path:
    return paths.segment_model_dir()[0]


def model_dir_source() -> str:
    return paths.segment_model_dir()[1]


def release_base() -> str:
    raw = (os.environ.get(ENV_MODEL_URL) or "").strip()
    base = raw or RELEASE_BASE
    return base if base.endswith("/") else base + "/"


def path_for(role: str, directory: Path | None = None) -> Path:
    directory = Path(directory) if directory is not None else model_dir()
    for name, entry in MODEL_FILES.items():
        if entry["role"] == role:
            return directory / name
    raise KeyError(role)


# Verification is a full read of ~44 MB; the result is remembered per
# (path, size, mtime) so `status()` -- polled once a second by a downloading
# page -- stays a stat, not a hash.
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


def file_status(directory: Path | None = None, *, deep: bool = False) -> dict:
    """Per file: present, size matches, and (when `deep`) checksum matches.

    The cheap form trusts a file of the right size; that is safe because a
    file only gets its real name after its checksum was verified (download)
    or because somebody placed it there by hand (offline directory), and
    `OnnxSamBackend` verifies once more before loading.
    """
    directory = Path(directory) if directory is not None else model_dir()
    out = {}
    for name, entry in MODEL_FILES.items():
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


def missing(directory: Path | None = None, *, deep: bool = False) -> list[str]:
    return [name for name, row in file_status(directory, deep=deep).items()
            if not row["ok"]]


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return paths.is_writable(directory)


def download(directory: Path | None = None, *, progress=None, cancelled=None,
             force: bool = False) -> Path:
    """Fetch every missing file, verified, atomically. Returns the directory.

    `progress(done_bytes, total_bytes, file_name)` is called as bytes arrive,
    across both files, so one bar can cover the whole setup. `cancelled()` is
    polled per chunk. Bytes stream into `<name>.part` and are hashed as they
    arrive; only a matching file is renamed into place.
    """
    import urllib3

    directory = Path(directory) if directory is not None else model_dir()
    if not _writable(directory):
        raise PermissionError(f"Cannot write to {directory}")
    todo = [name for name in MODEL_FILES
            if force or not (directory / name).exists()
            or not verify(directory / name, MODEL_FILES[name]["sha256"])]
    done = sum(MODEL_FILES[name]["bytes"] for name in MODEL_FILES if name not in todo)
    if progress:
        progress(done, TOTAL_BYTES, todo[0] if todo else None)
    if not todo:
        return directory

    pool = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
        retries=urllib3.Retry(total=3, redirect=5, backoff_factor=0.5),
    )
    base = release_base()
    for name in todo:
        entry = MODEL_FILES[name]
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
                        progress(min(done, TOTAL_BYTES), TOTAL_BYTES, name)
        except BaseException:
            _unlink(part)
            raise
        finally:
            response.release_conn()
        if digest.hexdigest() != entry["sha256"]:
            _unlink(part)
            raise WeightsCorrupt(
                f"{name} did not match its checksum; nothing was installed.")
        os.replace(part, target)
        with _VERIFIED_LOCK:
            stamp = _stamp(target)
            if stamp:
                _VERIFIED[stamp] = True
    return directory


def remove(directory: Path | None = None) -> list[str]:
    """Delete the model files (and any partial download). Returns names removed."""
    directory = Path(directory) if directory is not None else model_dir()
    gone = []
    for name in MODEL_FILES:
        for candidate in (directory / name, directory / (name + ".part")):
            if candidate.exists():
                _unlink(candidate)
                gone.append(candidate.name)
    cache = directory / "cache"
    if cache.is_dir():
        import shutil

        shutil.rmtree(cache, ignore_errors=True)
    return gone


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
