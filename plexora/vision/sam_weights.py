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

from pathlib import Path

from plexora import paths
from plexora.vision import model_files
from plexora.vision.model_files import (  # noqa: F401  (this module's public names)
    DownloadCancelled,
    WeightsCorrupt,
    sha256_of,
    verify,
)

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

TOTAL_BYTES = model_files.total_bytes(MODEL_FILES)


class SamWeightsMissing(RuntimeError):
    """The weights are not on disk. `INSTALL` is the line to show somebody."""

    INSTALL = "plexora ai segment install"

    def __init__(self, missing=()):
        names = ", ".join(missing) or "the model files"
        super().__init__(
            f"Magic select is not set up yet ({names} missing). It sets itself "
            f"up the first time it is used, or run: {self.INSTALL}")
        self.missing = list(missing)


def model_dir() -> Path:
    return paths.segment_model_dir()[0]


def model_dir_source() -> str:
    return paths.segment_model_dir()[1]


def release_base() -> str:
    return model_files.release_base(RELEASE_BASE, ENV_MODEL_URL)


def path_for(role: str, directory: Path | None = None) -> Path:
    directory = Path(directory) if directory is not None else model_dir()
    for name, entry in MODEL_FILES.items():
        if entry["role"] == role:
            return directory / name
    raise KeyError(role)


def file_status(directory: Path | None = None, *, deep: bool = False) -> dict:
    """Per file: present, size matches, and (when `deep`) checksum matches
    (`model_files.file_status`; `OnnxSamBackend` verifies once more before
    loading)."""
    directory = Path(directory) if directory is not None else model_dir()
    return model_files.file_status(directory, MODEL_FILES, deep=deep)


def missing(directory: Path | None = None, *, deep: bool = False) -> list[str]:
    directory = Path(directory) if directory is not None else model_dir()
    return model_files.missing(directory, MODEL_FILES, deep=deep)


def _writable(directory: Path) -> bool:
    return model_files.writable(directory)


def download(directory: Path | None = None, *, progress=None, cancelled=None,
             force: bool = False) -> Path:
    """Fetch every missing file, verified, atomically. Returns the directory.

    `progress(done_bytes, total_bytes, file_name)` spans both files, so one
    bar can cover the whole setup (`model_files.download`).
    """
    directory = Path(directory) if directory is not None else model_dir()
    return model_files.download(directory, MODEL_FILES, release_base(), progress=progress,
                                cancelled=cancelled, force=force)


def remove(directory: Path | None = None) -> list[str]:
    """Delete the model files (and any partial download). Returns names removed."""
    directory = Path(directory) if directory is not None else model_dir()
    return model_files.remove(directory, MODEL_FILES)
