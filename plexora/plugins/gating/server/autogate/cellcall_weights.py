"""Where the cell model's weights come from, and proving they are intact.

One small ONNX file (~2.8 MB), not in the wheel: it is fetched once, the
first time somebody asks for model calls, from the same public release
repository magic select's weights come from, and checked against the sha256
pinned below before it is given its real name (`vision.model_files`).

What the file is: three seeds of a small cell-aware CNN (~200k weights
each) averaged inside the graph, trained on Plexora's own labelled cells
(four images: CyCIF x2, IMC, CODEX) after pretraining on their clearest
cells. The average, not one seed, because its confident calls are the more
precise ones: at a live session's gates on held-out lsp11385_2 cells, one
seed made more confident overrides with no more of them right (near the
gate 79 % against 80 %, far from it 81 % against 85 %), for a third of the
time. No public dataset went into these
weights. Input `crops` (N, 12, 48, 48)
float32 in the `cellcall.CHANNELS` order; output `p_own` (N,): the
probability that the cell's stain is its own.
"""

from __future__ import annotations

import os
from pathlib import Path

from plexora import paths
from plexora.vision import model_files

MODEL_NAME = "cellcall"
#: Bumped whenever the file changes: part of every cache key over its calls.
MODEL_VERSION = "1"
#: The input/output contract (`cellcall.CHANNELS`, `CELLCALL["side"]`).
MODEL_LAYOUT = "cellcall_v1"

RELEASE_BASE = "https://github.com/nirmallab/plexora-models/releases/download/cellcall-model-v1/"
ENV_MODEL_URL = "PLEXORA_CELLCALL_MODEL_URL"
#: An offline machine copies the file into a directory and points this at it.
ENV_MODEL_DIR = "PLEXORA_CELLCALL_MODEL_DIR"
#: For development and held-out benchmarks only: one ONNX file used as it is,
#: never checked against the pin. Every run it makes is keyed and recorded as
#: `unpinned:<its own sha256>`, so its calls never mix with the shipped model's.
ENV_MODEL_FILE = "PLEXORA_CELLCALL_MODEL_FILE"

MODEL_FILES = {
    "cellcall_v1.onnx": {
        "role": "model",
        "sha256": "d2e53f78ead5103a1235c4f5415184488486f30640d9baf43b33ea023abc15c1",
        "bytes": 2795846,
    },
}

TOTAL_BYTES = model_files.total_bytes(MODEL_FILES)


class CellModelMissing(RuntimeError):
    """The weights are not on disk."""

    def __init__(self, missing=()):
        names = ", ".join(missing) or "the model file"
        super().__init__(f"The cell model is not set up yet ({names} missing).")
        self.missing = list(missing)


def model_dir() -> tuple[Path, str]:
    """(directory, which rule said so): the environment, else
    `<data_root>/.models/cellcall`."""
    raw = os.environ.get(ENV_MODEL_DIR)
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser(), "environment"
    return paths.models_root() / "cellcall", "default"


def model_path(directory: Path | None = None) -> Path:
    directory = Path(directory) if directory is not None else model_dir()[0]
    return directory / next(iter(MODEL_FILES))


def missing(directory: Path | None = None, *, deep: bool = False) -> list[str]:
    directory = Path(directory) if directory is not None else model_dir()[0]
    return model_files.missing(directory, MODEL_FILES, deep=deep)


def status() -> dict:
    """{ready, missing, directory, source, model}: a stat, never a hash."""
    override = override_file()
    if override is not None:
        return {"ready": override.is_file(), "missing": [] if override.is_file() else
                [str(override)], "directory": str(override.parent), "source": "unpinned_file",
                "model": {"name": MODEL_NAME, "version": MODEL_VERSION, "layout": MODEL_LAYOUT},
                "size_mb": round(override.stat().st_size / 1e6, 1) if override.is_file() else None}
    directory, source = model_dir()
    gone = missing(directory)
    return {"ready": not gone, "missing": gone, "directory": str(directory), "source": source,
            "model": {"name": MODEL_NAME, "version": MODEL_VERSION, "layout": MODEL_LAYOUT},
            "size_mb": round(TOTAL_BYTES / 1e6, 1)}


def download(*, progress=None, cancelled=None, force: bool = False) -> Path:
    directory = model_dir()[0]
    return model_files.download(directory, MODEL_FILES,
                                model_files.release_base(RELEASE_BASE, ENV_MODEL_URL),
                                progress=progress, cancelled=cancelled, force=force)


def override_file() -> Path | None:
    """The unpinned file `ENV_MODEL_FILE` names, or None."""
    raw = os.environ.get(ENV_MODEL_FILE)
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    return None


_SHA_CACHE: dict = {}


def file_sha(path) -> str:
    """sha256 of a file, cached by path, size and modification time."""
    import hashlib

    path = Path(path)
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _SHA_CACHE:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        _SHA_CACHE[key] = digest.hexdigest()
    return _SHA_CACHE[key]


def identity(path=None) -> str:
    """What a run's key and record name the weights by: the pinned sha256
    (first 16 hex) for the shipped file, `unpinned:<sha16>` for any other."""
    pinned = next(iter(MODEL_FILES.values()))["sha256"]
    if path is None:
        path = override_file()
        if path is None:
            return pinned[:16]
    try:
        sha = file_sha(path)
    except OSError:
        return "unpinned:missing"
    return sha[:16] if sha == pinned else f"unpinned:{sha[:16]}"


def ensure(*, progress=None, cancelled=None) -> Path:
    """The verified model file, fetched first if it is missing (or the
    unpinned `ENV_MODEL_FILE`, as it is). Raises `CellModelMissing` when it
    cannot be had (offline, no release, a named file that is not there)."""
    override = override_file()
    if override is not None:
        if not override.is_file():
            raise CellModelMissing([str(override)])
        return override
    directory = model_dir()[0]
    if missing(directory, deep=True):
        try:
            download(progress=progress, cancelled=cancelled)
        except model_files.DownloadCancelled:
            raise
        except Exception as exc:
            raise CellModelMissing(missing(directory)) from exc
    gone = missing(directory, deep=True)
    if gone:
        raise CellModelMissing(gone)
    return model_path(directory)


def remove() -> list[str]:
    return model_files.remove(model_dir()[0], MODEL_FILES)
