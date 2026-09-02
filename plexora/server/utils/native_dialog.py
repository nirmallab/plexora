# Native OS file/folder picker for the "Browse..." buttons in the quick-view
# and import UIs. Returns a path only -- never file bytes.
#
# Three modes. "file" and "directory" are the single-kind dialogs; "any" is
# the one every field actually uses, and it takes either -- because the thing
# being asked for is "your image" or "your table", and whether that happens to
# be a file (.ome.tif, .csv) or a folder (an OME-Zarr store) is a fact about
# the format, not a question the user should have to answer by choosing which
# of two buttons to press first.
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
# Mode "any" needs one step further down: AppleScript's `choose file` and
# `choose folder` are two different commands and neither takes the other's
# kind. The panel underneath both -- NSOpenPanel -- does, via
# `canChooseFiles` AND `canChooseDirectories` together, and JXA
# (`osascript -l JavaScript`) can reach it through the ObjC bridge. No other
# platform has an equivalent: Tk's dialogs and the Windows common dialogs are
# single-kind by construction.
#
# Which does NOT make those platforms the same as a machine with no desktop.
# Windows and Linux still have real system file browsers -- one per kind -- so
# mode "any" is refused there with `fallback: "kinds"` and the Browse button
# asks which kind before opening the genuine dialog for it (browse_routes.py,
# services/browsePicker.js). The in-app listing picker is `fallback: "list"`,
# and it is for a machine that can show no dialog at all: a compute node, a
# container, notebook mode. Answering both cases with "list" is what once
# replaced every Windows and Linux file dialog with a substitute for one.
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
import os
import shutil
import subprocess
import sys

# Keyed by the caller's `file_filter` argument (kept in sync with
# browsePicker.js's data-browse-filter values).
_TK_FILTERS = {
    # No "*.zarr" here, and none in _APPLESCRIPT_EXTENSIONS below. An OME-Zarr
    # image is a *directory*, which no file-only dialog can return however it
    # is filtered -- it is reached through mode "any", where filters do not
    # apply at all. Listing it would also repeat the ".h5ad" mistake documented
    # under _APPLESCRIPT_EXTENSIONS: macOS has no UTI for it, and one
    # unregistered extension greys out every other file in the panel.
    #
    # ".mrxs" is absent for the first of those reasons: a MIRAX slide is a
    # small index file BESIDE a directory of the same name, and picking it in a
    # file dialog works -- but it is the one whole-slide format that needs
    # OpenSlide, and offering it in a filter would promise something an install
    # without the [wsi] extra cannot do. The path box still accepts it, and
    # `_sniff_quick_view_kind` says exactly what is missing.
    # ".dcm" IS offered: a DICOM slide is a folder of instances, but picking any
    # one of them selects that slide (dicom_wsi.assemble_slide gathers its
    # siblings), so unlike ".zarr" the file dialog can genuinely return one.
    "image": [("Image files", "*.tif *.tiff *.ome.tif *.ome.tiff *.svs *.ndpi *.scn *.bif *.qptiff *.dcm *.png *.jpg *.jpeg"), ("All files", "*.*")],
    "csv": [("CSV files", "*.csv"), ("All files", "*.*")],
    "h5ad": [("AnnData files", "*.h5ad"), ("All files", "*.*")],
    # The single Data input accepts any feature-table format, so its picker
    # offers all of them at once rather than making the user pick a format
    # before picking a file. A .zarr store is a directory, and the hybrid
    # panel mode "any" opens for it is unfiltered -- see below.
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
    # "dcm" is listed here where ".h5ad" was not, because macOS ships a stock
    # UTI for DICOM (`org.nema.dicom`, which is why Preview opens one) and so it
    # narrows the panel rather than greying every file in it. Not verifiable
    # from the machine this was written on: if a Mac ever shows an image picker
    # with everything greyed out, this entry is the one to drop back to None.
    "image": ["tif", "tiff", "svs", "ndpi", "scn", "bif", "qptiff", "dcm",
              "png", "jpg", "jpeg"],
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

# The one dialog that takes a file OR a folder. Same output contract as the Tk
# script above -- last line of stdout is `{"path": <string|null>}`, and cancel
# is a null rather than a non-zero exit -- so both runners parse the same way.
#
# No file-type filter is set, deliberately. `allowedContentTypes` filters by
# UTI conformance exactly as AppleScript's `of type` does, and carries the same
# trap documented under _APPLESCRIPT_EXTENSIONS: an extension macOS has no
# registration for (.h5ad, .tsv) does not merely fail to match, it greys out
# every other file in the panel too. The field the path lands in sniffs it the
# moment it arrives -- `/inspect_data`, `check_path_existence` -- so a wrong
# pick is answered in the form, where the reason can actually be written out,
# rather than by a panel that silently refuses to let anything be clicked.
_JXA_HYBRID_SCRIPT = r"""
ObjC.import('AppKit');
function run() {
    var app = $.NSApplication.sharedApplication;
    // osascript is a faceless background agent. Without a policy that can own
    // the active state, the panel opens BEHIND the browser window the user
    // just clicked in -- there, but invisibly so. Accessory rather than
    // Regular: it can come frontmost without bouncing an icon into the Dock
    // for the seconds this lives.
    app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
    var panel = $.NSOpenPanel.openPanel;
    panel.canChooseFiles = true;
    panel.canChooseDirectories = true;
    // A .zarr store is a directory the user means to CHOOSE, not one to open
    // and look inside. Without this the panel treats every folder as
    // navigation and there is no way to answer with one.
    panel.allowsMultipleSelection = false;
    panel.resolvesAliases = true;
    panel.message = 'Select a file, or a folder such as an OME-Zarr (.zarr) store';
    panel.prompt = 'Choose';
    app.activateIgnoringOtherApps(true);
    var pressed = panel.runModal;
    var ok = (typeof $.NSModalResponseOK !== 'undefined') ? $.NSModalResponseOK : 1;
    var path = null;
    if (pressed === ok && panel.URLs.count > 0) {
        path = ObjC.unwrap(panel.URLs.objectAtIndex(0).path);
    }
    return JSON.stringify({ path: path });
}
"""


def _browse_for_path_macos_hybrid(timeout):
    """The file-or-folder panel. macOS only -- see the module docstring."""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _JXA_HYBRID_SCRIPT],
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


def available():
    """Whether a native dialog could plausibly be shown on this machine.

    Syntactic and cheap on purpose -- no subprocess. The honest test would be
    to open a dialog and see what happens, and on a headless box that is
    precisely the thing that hangs: the picker waits for input from a desktop
    nobody can see, holding a worker thread until it is killed.

    Asked on the way to deciding what to OFFER, so a wrong "no" costs a
    directory-listing picker instead of a native one, and a wrong "yes" costs
    the error `browse_for_path` already raises. Both are recoverable; a hang is
    not, which is why this leans conservative on Unix.
    """
    if sys.platform == "darwin":
        return shutil.which("osascript") is not None
    if sys.platform.startswith("win"):
        return True
    # X11 or Wayland. Without one of them Tk cannot connect to a display, and
    # a compute node or a container is exactly where that is true.
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def hybrid_available():
    """Whether mode="any" -- one dialog taking a file OR a folder -- can be
    shown here at all.

    macOS only, and syntactic for the same reasons `available()` is. Asked on
    the way to deciding what to offer: a "no" is not a failure but a routing
    decision, and on a machine where `available()` said yes it is a small one
    -- the caller asks which kind and comes back here for a dialog this can
    open. Only when there is no desktop at all does the in-app listing stand in
    for an OS dialog.
    """
    return sys.platform == "darwin" and shutil.which("osascript") is not None


#: The three answers to "what can this machine put on a screen?", which is a
#: different question from "did this call work". HYBRID takes a file or a
#: folder in one panel (macOS alone); KINDS has real system dialogs but one
#: kind at a time (Windows, Linux); NONE has no desktop to draw on at all.
#:
#: Written down as a vocabulary because the answer has to travel. A node states
#: it in `/hello` so the machine relaying a Browse click can tell KINDS from
#: NONE -- two refusals that arrive as the same "no" once they have crossed a
#: network as prose, which is what left a laptop with a perfectly good file
#: dialog being offered the listing picker instead of it.
HYBRID, KINDS, NONE = "hybrid", "kinds", "none"


def dialog_kind():
    """Which of HYBRID/KINDS/NONE describes this machine.

    Derived from the two predicates rather than probing anything itself, so
    there is one place the three-way answer is decided and it cannot drift from
    what `browse_for_path` will actually agree to do.
    """
    if hybrid_available():
        return HYBRID
    if available():
        return KINDS
    return NONE


def browse_for_path(mode="file", file_filter="any", timeout=300):
    """Opens a native file/folder picker and returns the chosen absolute
    path, or None if the user cancelled.

    `mode` is "file", "directory", or "any" -- the last taking either kind in
    one dialog, which is what every path field asks for, and which only macOS
    can do (see `hybrid_available`).

    `file_filter` narrows the file-type dropdown for mode="file" (ignored for
    "directory", and for "any", whose panel is deliberately unfiltered) -- one
    of the keys in FILTER_NAMES, defaulting to "any" (no narrowing beyond "All
    files").

    Raises RuntimeError with a user-facing message if no picker could be
    shown at all (no display, tkinter not installed, timed out, ...) so
    callers can fall back to a manual path input.
    """
    if mode == "any":
        # Guarded here as well as at the routes, which is where the refusal is
        # turned into the listing picker. This one is for a caller that never
        # asked -- reaching a Tk dialog with mode "any" would silently open a
        # file-only panel, and a user would be left clicking at a .zarr store
        # that would not highlight.
        if not hybrid_available():
            raise RuntimeError(
                "This machine has no dialog that can take a file or a folder.")
        return _browse_for_path_macos_hybrid(timeout)
    if sys.platform == "darwin":
        return _browse_for_path_macos(mode, file_filter, timeout)
    return _browse_for_path_tk(mode, file_filter, timeout)
