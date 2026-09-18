"""Waiting out Windows' brief refusals to replace a file.

POSIX lets a rename land on a file somebody else has open. Windows does not:
`MoveFileEx` fails with ERROR_ACCESS_DENIED for as long as any handle is open
on the destination, and there is no flag that asks to be let through. The
handle usually belongs to nothing of ours -- Defender's real-time scan reads a
file within milliseconds of its creation, and so does a search indexer or a
sync client -- so the failure is a few milliseconds long and has nothing to do
with the program's own correctness.

Two things in Plexora replace files often enough to meet it: config.json
(`server/models/project.py`) and every zarr store anything writes, which is
OME-Zarr pyramids, SpatialData tables, AnnData gates and ROI shapes. Measured
here, replacing one small file two hundred times in a row was refused 138
times -- and each refusal cleared on the next attempt.

This module holds the retry both use. It is a leaf: stdlib only, plus zarr at
the moment the shim is installed.
"""

from __future__ import annotations

import functools
import os
import time


def past_transient_locks(action, attempts: int = 100, delay: float = 0.02):
    """Run a file operation, retrying past Windows' brief sharing violations.

    Windows refuses to replace a file another process has open, and refuses to
    open one that is being replaced -- and unlike POSIX there is no way to ask
    to be let through. Both windows are one rename long, so a short retry turns
    a hard failure into a wait. This only matters between processes (a notebook
    sidecar and a CLI server sharing a data directory); readers and writers in
    one process are already serialized by the lock. Giving up re-raises rather
    than leaving the caller with a half-truth.

    Two seconds of budget, not the 400 ms this started with: the thing holding
    the handle is often not another Plexora process at all but a scanner --
    Defender, or a sync client walking a data directory that lives in Dropbox --
    and those hold on for longer than one rename. The cost is paid only when a
    write is genuinely blocked, and losing a project record is far worse than
    waiting.
    """
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def install_zarr_retry() -> None:
    """Give zarr's local store the same retry, on Windows only.

    `zarr.storage._local._put` writes a key to a temporary file beside the
    target and renames it over the top -- the right way to write a file, and
    the one that Windows refuses mid-scan. Every writer that reaches a local
    zarr store goes through this one function, ours and our dependencies'
    alike, so wrapping it covers SpatialData's tables and AnnData's groups
    without either of them knowing.

    What made this show up as a hard failure rather than a rare one is that a
    zarr group's metadata is rewritten once per attribute assigned:
    `attrs.update({...})` with four keys is four renames of `zarr.json`
    milliseconds apart, and the scan of the file written by one is still open
    when the next arrives. (Plexora's own writes now go through
    `Group.update_attributes`, which is one rename -- see
    `server/utils/ome_zarr.build_extension`. Third-party writers do not.)

    Idempotent, and quiet when zarr is absent or has been rearranged
    underneath us: a missing `_put` means no retry, not an import error at
    startup. Not installed off Windows, where the rename cannot fail this way.
    """
    if os.name != "nt":
        return
    try:
        from zarr.storage import _local
    except Exception:
        return

    original = getattr(_local, "_put", None)
    if original is None or getattr(original, "_plexora_retry", False):
        return

    @functools.wraps(original)
    def _put(path, value, exclusive=False):
        # Retried whole rather than at the rename: a refused attempt deletes
        # its own temporary file on the way out, so the next one starts from
        # the same place the first did. Only PermissionError is caught --
        # `exclusive=True` signals "already there" with FileExistsError, which
        # zarr relies on and must not be waited out.
        return past_transient_locks(
            lambda: original(path, value, exclusive=exclusive))

    _put._plexora_retry = True
    _local._put = _put
