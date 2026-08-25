# Native OS file/folder picker for the "Browse..." buttons in the quick-view
# and import UIs. Returns a path only -- never file bytes.
#
# macOS goes through `osascript`'s `choose file`/`choose folder` (a real
# Cocoa panel driven by AppleScript, no Python GUI toolkit involved) rather
# than tkinter. tkinter's askopenfilename on macOS routes through a hidden Tk
# root window that Cocoa uses to size/position/parent the panel, and that
# path is a long-standing source of broken behavior on recent macOS -- the
# panel can appear tiny and pinned to a screen corner, barely resizable, or
# even dismiss itself immediately (the click that opened it double-counts as
# a click "outside" the misplaced panel). osascript's panel is a normal,
# independently-managed system dialog and doesn't have any of that.
#
# Other platforms keep the tkinter subprocess approach, run out-of-process
# for two reasons: Tk needs to run on the process's *main* thread on macOS
# (moot here since macOS no longer uses this path, but still true in
# principle), but more importantly a request handled by waitress lands on an
# arbitrary worker thread; and a subprocess crash/hang (e.g. no display on a
# headless box) can't take the server down with it.
#
# Only meant for the terminal/`python run.py` and Jupyter (`server_cli.py`)
# launch paths, where sys.executable is a real Python interpreter. It is not
# safe to use as-is from a frozen/packaged build, where sys.executable is the
# packaged exe itself.

import json
import subprocess
import sys

# Keyed by the caller's `file_filter` argument (kept in sync with
# browsePicker.js's data-browse-filter values).
_TK_FILTERS = {
    "image": [("Image files", "*.tif *.tiff *.ome.tif *.ome.tiff *.svs *.qptiff *.png *.jpg *.jpeg"), ("All files", "*.*")],
    "csv": [("CSV files", "*.csv"), ("All files", "*.*")],
    "h5ad": [("AnnData files", "*.h5ad"), ("All files", "*.*")],
    # The single Data input accepts any feature-table format, so its picker
    # offers all of them at once rather than making the user pick a format
    # before picking a file. A .zarr store is a directory and is reached by
    # the same field's "Store..." button in directory mode.
    "data": [("Single-cell data", "*.csv *.tsv *.txt *.h5ad"), ("All files", "*.*")],
    # A list of channel names, for the viewer's rename upload. Spreadsheets
    # belong in it because a panel design is written in one far more often than
    # in a CSV (see server/utils/channel_file.py).
    "channels": [("Channel names", "*.csv *.tsv *.txt *.xlsx *.xlsm"), ("All files", "*.*")],
    "any": [("All files", "*.*")],
}

#: The filter names callers may ask for. Exported so the route validates
#: against this table rather than its own hand-typed copy -- the two drifted
#: once already: "data" was added here for the single Data input on the import
#: page but not to the route's allowlist, so that field's "File..." button
#: posted a filter the server rejected with a 400 and did nothing at all.
FILTER_NAMES = frozenset(_TK_FILTERS)

# AppleScript's `choose file of type` filters by Uniform Type Identifier
# conformance, not literally by filename extension -- an extension string
# only works here when macOS actually has a UTI registered for it (true for
# common ones like these image formats, via system/Preview/etc.
# declarations). ".h5ad" has no such registration on a stock Mac, so filtering
# by it doesn't just fail to narrow the list -- it greys out every file,
# including real .h5ad ones, since none of them "conform" to an unregistered
# type. Left unfiltered (None -> no `of type` clause) rather than chasing a
# UTI that may not exist on the user's machine.
_APPLESCRIPT_EXTENSIONS = {
    "image": ["tif", "tiff", "svs", "qptiff", "png", "jpg", "jpeg"],
    "csv": ["csv"],
    "h5ad": None,
    # Unfiltered for the same reason as "h5ad" above: the set includes .h5ad,
    # and one unregistered extension greys out every file in the dialog rather
    # than just failing to match its own.
    "data": None,
    # Unfiltered on the same grounds: ".tsv" has no stock UTI registration, and
    # one such extension in the set greys out the .csv and .xlsx files too.
    "channels": None,
    "any": None,
}

# The dialog script only ever receives a fixed, known filter key on argv --
# never an interpolated filetypes string -- since the script itself is
# passed to `python -c`.
_TK_DIALOG_SCRIPT = r"""
import json, sys
import tkinter as tk
from tkinter import filedialog

mode = sys.argv[1]
filter_key = sys.argv[2] if len(sys.argv) > 2 else "any"

FILTERS = %(filters)s

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
if mode == "directory":
    path = filedialog.askdirectory(parent=root)
else:
    path = filedialog.askopenfilename(parent=root, filetypes=FILTERS.get(filter_key, FILTERS["any"]))
root.destroy()
print(json.dumps({"path": path or None}))
""" % {"filters": repr(_TK_FILTERS)}


def _browse_for_path_macos(mode, file_filter, timeout):
    if mode == "directory":
        script = 'POSIX path of (choose folder with prompt "Select a folder")'
    else:
        extensions = _APPLESCRIPT_EXTENSIONS.get(file_filter)
        of_type = ""
        if extensions:
            quoted = ", ".join(f'"{ext}"' for ext in extensions)
            of_type = f" of type {{{quoted}}}"
        script = f'POSIX path of (choose file{of_type} with prompt "Select a file")'

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timed out waiting for the file browser.")
    except OSError as exc:
        raise RuntimeError(str(exc))

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # AppleScript error -128 is exactly "user cancelled" -- not a failure.
        if "-128" in stderr or "User canceled" in stderr:
            return None
        message = stderr.splitlines()[-1] if stderr else "Could not open the file browser."
        raise RuntimeError(message)

    path = result.stdout.strip()
    return path or None


def _browse_for_path_tk(mode, file_filter, timeout):
    try:
        result = subprocess.run(
            [sys.executable, "-c", _TK_DIALOG_SCRIPT, mode, file_filter],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timed out waiting for the file browser.")
    except OSError as exc:
        raise RuntimeError(str(exc))

    if result.returncode != 0:
        message = (result.stderr or "").strip().splitlines()
        raise RuntimeError(message[-1] if message else "Could not open the file browser.")

    try:
        return json.loads(result.stdout.strip().splitlines()[-1])["path"]
    except (ValueError, IndexError, KeyError):
        raise RuntimeError("Unexpected response from the file browser.")


def browse_for_path(mode="file", file_filter="any", timeout=300):
    """Opens a native file/folder picker and returns the chosen absolute
    path, or None if the user cancelled.

    `file_filter` narrows the file-type dropdown for mode="file" (ignored
    for mode="directory") -- one of the keys in FILTER_NAMES, defaulting to
    "any" (no narrowing beyond "All files").

    Raises RuntimeError with a user-facing message if no picker could be
    shown at all (no display, tkinter not installed, timed out, ...) so
    callers can fall back to a manual path input.
    """
    if sys.platform == "darwin":
        return _browse_for_path_macos(mode, file_filter, timeout)
    return _browse_for_path_tk(mode, file_filter, timeout)
