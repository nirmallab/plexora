"""One file's bytes, read from or written to the machine that owns them.

The counterpart of `dir_listing.py`, which deliberately stops at names: it says
what is in a directory, and this says what is in a file. Both live here rather
than in the route that first needed them because both machines in a session
answer the same two questions -- the viewer about its own filesystem, a data
node about the far side's -- and a rule enforced in only one of them is a rule
that holds until somebody picks Remote.

**This moves bytes, and that is the point.** A plugin's Upload button opening a
file on a cluster, and its Download button putting a result back there, cannot
be done with paths alone; the browser is on a third machine and has no route to
either filesystem. What bounds it is the same boundary `/upload_data_file` and
`/list_dir` already sit behind: one user's server, one token, one account whose
files that user could have read with `cat` over ssh.

Three rules the callers depend on:

**The server joins the path.** A write takes a directory and a bare `name`, and
anything with a separator or a `..` in it is refused rather than normalized.
Same reasoning as the listing module's: the browser cannot know whether the far
side joins with `/` or `\\`, so it never tries, and a name that arrived from a
form cannot walk out of the folder the user picked.

**An existing file is never silently replaced.** `write_file` refuses, with
`.exists` set, and the caller turns that into a Replace? question. Suffixing a
`(2)` onto the name instead would be the wrong kindness: the user picked that
filename to overwrite last week's export as often as not, and either way they
should be the one to say so.

**A partial write never appears under the real name.** Bytes land in a temp
file beside the target and are moved onto it with `os.replace`, so a transfer
that dies halfway leaves the previous file intact rather than a truncated one
with the right name -- which on a shared filesystem is the version somebody
else's script picks up.
"""

from __future__ import annotations

import mimetypes
import os
import tempfile
from pathlib import Path

#: The largest single file this will write. Matches the primary's buffered-read
#: ceiling (`providers/http.MAX_BUFFERED_BYTES`) and the upload cap, so a file
#: that can be sent is a file that can be stored, rather than one refused after
#: five minutes of copying.
WRITE_MAX_BYTES = 512 * 1024 * 1024

#: How much is moved per copy step. Large enough that a gigabyte is not a
#: million syscalls, small enough that the buffer is not what fails.
CHUNK = 1 << 16


class TransferError(ValueError):
    """A transfer that cannot happen, with the sentence to show for it.

    `.exists` is set when the refusal is specifically "there is already a file
    there" -- the one refusal the caller can offer a way past, so it travels as
    a flag rather than as a substring somebody has to match on.
    """

    def __init__(self, message, exists=False):
        super().__init__(message)
        self.exists = bool(exists)


def open_read(raw):
    """`(path, size, mimetype, name)` for a file that can be sent.

    Everything the response needs to be built from, resolved once here so the
    route does not stat the file a second time and disagree with itself about
    its size. The file itself is opened by the caller -- it is streamed, and
    holding an open handle across a `jsonify` would be the one way to leak one.
    """
    try:
        path = Path(str(raw or "")).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise TransferError(str(exc)) from exc

    if not path.exists():
        raise TransferError(f"No such file: {path}")
    if path.is_dir():
        # Worth its own sentence: this is what a user gets for picking a `.zarr`
        # store, which IS a directory and IS the thing they meant to send.
        # "Not a file" without the name reads as a bug rather than as an answer.
        raise TransferError(f"That is a folder, not a file: {path}")
    if not path.is_file():
        raise TransferError(f"Not a regular file: {path}")

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise TransferError(f"Cannot read {path}: {exc}") from exc

    mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path, size, mimetype, path.name


def safe_name(name):
    """A bare filename, or a refusal.

    The whole of the client's trust in this module: the directory came from a
    picker that walked the real filesystem, and the name came from a text box.
    A name is a name -- no separator of either flavour, no `.` or `..`, nothing
    absolute -- so joining it onto the directory cannot leave the directory.
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        raise TransferError("A file needs a name.")
    if "/" in cleaned or "\\" in cleaned:
        raise TransferError(
            "A file name cannot contain a folder separator -- pick the folder "
            "above and give the file a plain name.")
    if cleaned in (".", "..") or cleaned.startswith("."):
        # Leading dots are refused rather than hidden-file support: this is a
        # save dialog, and a `.` typed first is far more often a slip than a
        # deliberate dotfile.
        raise TransferError(f"Not a usable file name: {cleaned}")
    return cleaned


def write_file(directory_raw, name, stream, overwrite=False,
               max_bytes=WRITE_MAX_BYTES):
    """Copy `stream` into `directory_raw/name`, atomically. Returns the path.

    `stream` is anything with `.read(n)` -- a Werkzeug upload, a urllib3
    response, an open file. It is read in chunks and never held whole: the
    files this carries are exports of a whole cell table, and the machine
    relaying them has other work.

    The size is counted while copying rather than trusted from a header,
    because the header is the client's claim and the bytes are the fact.
    """
    try:
        directory = Path(str(directory_raw or "")).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise TransferError(str(exc)) from exc

    if not directory.is_dir():
        raise TransferError(f"Not a folder: {directory}")

    target = directory / safe_name(name)
    if target.exists() and not overwrite:
        raise TransferError(f"There is already a file called {target.name} "
                            f"in {directory}.", exists=True)
    if target.is_dir():
        # Reachable even with `overwrite`, and `os.replace` onto a directory
        # fails with an errno nobody can act on.
        raise TransferError(f"{target} is a folder.")

    try:
        handle = tempfile.NamedTemporaryFile(
            dir=str(directory), prefix=".plexora-", suffix=".part", delete=False)
    except OSError as exc:
        raise TransferError(f"Cannot write to {directory}: {exc}") from exc

    temp = Path(handle.name)
    written = 0
    try:
        with handle:
            while True:
                piece = stream.read(CHUNK)
                if not piece:
                    break
                written += len(piece)
                if written > max_bytes:
                    raise TransferError(
                        f"That file is larger than this server will write "
                        f"({max_bytes // (1024 * 1024)} MB).")
                handle.write(piece)
        os.replace(temp, target)
    except TransferError:
        _discard(temp)
        raise
    except OSError as exc:
        _discard(temp)
        raise TransferError(f"Cannot write {target}: {exc}") from exc
    except Exception:
        _discard(temp)
        raise

    return target, written


def _discard(temp):
    """Remove a half-written temp file, never raising in a failure path."""
    try:
        temp.unlink()
    except OSError:
        pass
