"""Choosing a file or a folder on the website.

The behaviour is in tests/js/website_pick_probe.mjs, run in node below:
services/pickedFiles.js (picks, drops, a handle mounted for Python),
services/localFiles.js (the explorer over the folders the user gives the
site) and the website branch of services/browsePicker.js.

What is left here is what only Python can say: the page marks datasets by
Python's own rules (server/utils/dataset_marks.py), and lists a folder as
server/utils/dir_listing.py does. Both are compared against Python itself --
the names a rule reads come from the constants Python reads them from, so a
rule that changes there and not in the page fails here.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.server.utils import dataset_marks, dicom_wsi, dir_listing, ome_zarr, spatial_scene

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "website_pick_probe.mjs"
JS = REPO_ROOT / "plexora" / "client" / "src" / "js"

#: Every line the probe prints, so a check that is deleted or renamed fails
#: here rather than passing by not running.
CHECKS = (
    "a browser that cannot give the site a folder answers none of the explorer's routes",
    "a node's listing, and a path in Python's own files, are not the page's to answer",
    "with no folder given, the explorer opens on an empty list of places",
    "Add folder keeps the folder, and the list of places shows it by its name",
    "the same folder given twice is one, and one inside it opens there",
    "inside a folder: the desktop's rows, crumbs and parent, folders first",
    "...and each folder that is one dataset says which, by the names inside it",
    "hidden files only when asked for",
    "a file's path opens the folder holding it",
    "the open folder that is a dataset says so, with this licence's word on it",
    "a folder the browser stopped granting answers Reconnect, and opens once it is granted",
    "stat_entries: sizes and dates for names in the folder, and never a path",
    "the sidebar's places: the folders given, a lapsed one said so, no home, no search",
    "Recent: newest first, once each, eight at most; pins add, go and keep their order",
    "Remove from Folders takes its Recent, its pins and its last place with it",
    "searching below a folder is not offered on the website yet",
    "what the explorer answers is mounted: a folder with its tree, a file as itself",
    "a DICOM file brings its slide's instances, not the rest of its folder",
    "an MRXS brings the folder beside it, with no question",
    "the gate is asked once, before anything is mounted, and a no mounts nothing",
    "the next Browse of the field opens where its pick was chosen",
    "the title bar's buttons follow what the field takes",
    "Choose folder: a dataset is the answer at once, any other folder is kept and opened",
    "...and a field that takes a folder takes whichever folder was chosen",
    "Choose file is the browser's own dialog, narrowed to the field and opened beside its file",
    "a drop on the explorer: a file is the answer, a plain folder is kept and opened",
    "a drop is read in its own turn: every handle and entry asked for before anything waits",
    "Firefox and Safari: a dropped folder is read through its entry, every batch of it",
    "an MRXS dropped with its folder is one slide",
    "a drop's gate is asked once the drop is read, and a no takes nothing",
    "a DICOM file picked alone asks for its folder, which opens beside it",
    "...and a no to that question is a cancelled pick",
    "the file dialog is narrowed to the field's formats, and opens beside a handle",
    "startHandle: beside a picked file, never inside a picked folder's tree",
    "a DICOM folder remembered is mounted again as its instances, not the rest",
    "Firefox and Safari: a field that takes either asks File or Folder, under the button pressed",
    "...and a caller that names no button gets the one just pressed",
    "the browser's file dialog gets the field's formats, one extension each, no stores",
    "Chrome and Edge open the explorer over the folders given, and mount what it answers",
    "a box says what it will not take from a drop, by its names, before reading it",
    "a file let go anywhere but a box that takes it is refused, not opened in place of the app",
    "...in the local app opened in a browser tab too, but not in the desktop app",
    "...and a file input, which the browser fills from a drop itself, is left alone",
)


def _cases():
    """(name, child names, DICOM one level down) for every rule's branch."""
    xenium = sorted({*spatial_scene.XENIUM_FILES, *spatial_scene.XENIUM_TABLES})
    groups = sorted({*spatial_scene.SPATIALDATA_KINDS, spatial_scene.SPATIALDATA_TABLES})
    markers = sorted(ome_zarr._ZARR_MARKERS)
    cases = [("Documents", ["a.txt", "b.csv"], False), ("empty", [], False),
             ("run", ["experiment.xenium"], False)]
    cases += [("run", [name], False) for name in xenium]
    cases += [("run", [a, b], False) for a, b in zip(xenium, xenium[1:])]
    for group in groups:
        cases += [("project", [group], False), ("store.zarr", [group], False),
                  ("store", [group, markers[0]], False)]
    cases += [("image.ome.zarr", ["labels", ".zattrs"], False),
              ("store", ["labels", "images", ".zgroup"], False)]
    cases += [("store", [marker], False) for marker in markers]
    cases += [(name, [], False) for name in ("a.zarr", "b.ome.zarr", "C.ZARR", "d.n5", "e.zarr.bak")]
    for matrix in ("filtered_feature_bc_matrix.h5", "raw_feature_bc_matrix", "feature_bc_matrix.h5ad"):
        cases.append(("outs", [spatial_scene.VISIUM_SPATIAL, matrix], False))
    cases += [("outs", [spatial_scene.VISIUM_SPATIAL], False),
              ("outs", [spatial_scene.VISIUM_HD_BINNED], False),
              (spatial_scene.VISIUM_HD_BINNED, ["square_002um", "square_008um"], False),
              (spatial_scene.VISIUM_HD_BINNED, ["square_2um"], False),
              ("square_016um", [spatial_scene.VISIUM_SPATIAL], False),
              ("square_016um", ["other"], False)]
    for suffix in dicom_wsi.DICOM_SUFFIXES:
        cases += [("slide", [f"a{suffix}"], False), ("slide", [f"A{suffix.upper()}"], False)]
    cases += [("slide", ["DICOMDIR"], False), ("slide", ["dicomdir"], False),
              ("export", ["series1", "series2"], True), ("export", ["notes.txt"], True)]
    return cases


def _expected(name, children, dicom_below):
    mark = dataset_marks.store_by_names(name, children)
    if mark is None and dicom_below:
        mark = dataset_marks._mark("dicom")
    return {"by_name": dataset_marks.store_by_name(name),
            "by_names": dataset_marks.store_by_names(name, children),
            "dataset": None if mark is None else {**mark, "licensed": True}}


def _tree(root):
    """A folder with one of each thing a listing marks, and a hidden file."""
    root.mkdir(parents=True)
    (root / "a.ome.tif").write_bytes(b"x" * 100)
    (root / "b.csv").write_bytes(b"x" * 10)
    (root / ".hidden").write_bytes(b"x")
    (root / "store.zarr").mkdir()
    (root / "store.zarr" / ".zattrs").write_text("{}")
    (root / "run").mkdir()
    (root / "run" / "experiment.xenium").write_text("{}")
    (root / "run" / "transcripts.parquet").write_bytes(b"x")
    (root / "dicom").mkdir()
    (root / "dicom" / "x.dcm").write_bytes(b"x")
    (root / "plain").mkdir()
    (root / "plain" / "readme.txt").write_text("x")
    (root / "sd").mkdir()
    for part in (".zattrs", "images", "tables"):
        path = root / "sd" / part
        path.mkdir() if part != ".zattrs" else path.write_text("{}")
    return root


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    work = tmp_path_factory.mktemp("website_pick")
    cases = work / "cases.json"
    cases.write_text(json.dumps([{"name": n, "children": c, "dicom_below": d} for n, c, d in _cases()]))
    tree = _tree(work / "Lab data")
    proc = subprocess.run([node, str(PROBE), "--marks", str(cases), "--tree", str(tree)],
                          capture_output=True, text=True, cwd=REPO_ROOT, timeout=120, encoding="utf-8")
    lines = {line.split(" ", 1)[0]: line.split(" ", 1)[1] for line in proc.stdout.splitlines()
             if line.startswith(("MARKS ", "LISTING "))}
    return {"proc": proc, "tree": tree, "marks": json.loads(lines.get("MARKS", "null")),
            "listing": json.loads(lines.get("LISTING", "null"))}


def test_the_probe_passes(probe):
    proc = probe["proc"]
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"  ok  {line}" in probe["proc"].stdout


def test_no_check_was_quietly_dropped(probe):
    assert f"{len(CHECKS)} checks passed" in probe["proc"].stdout


def test_the_page_marks_every_case_as_python_does(probe):
    """dataset_marks.store_by_name, store_by_names and the names half of
    dataset_of, for a case per branch of each rule."""
    cases = _cases()
    assert probe["marks"] is not None, probe["proc"].stdout
    assert len(probe["marks"]) == len(cases)
    for (name, children, below), got in zip(cases, probe["marks"]):
        assert got == _expected(name, children, below), (name, children, below)


def test_the_page_lists_a_folder_as_python_does(probe):
    """The rows dir_listing.listing gives for the same folder: names in the
    same order, the same kinds, sizes and dates for files, the same marks.
    A folder's own date is not something a browser's handle gives."""
    assert probe["listing"] is not None, probe["proc"].stdout
    for hidden, page in ((False, probe["listing"]["shown"]), (True, probe["listing"]["hidden"])):
        python = dir_listing.listing(str(probe["tree"]), show_hidden=hidden)
        assert [e["name"] for e in page["entries"]] == [e["name"] for e in python["entries"]]
        for got, want in zip(page["entries"], python["entries"]):
            assert (got["is_dir"], got["kind"], got["store"]) == (want["is_dir"], want["kind"], want["store"]), want
            if not want["is_dir"]:
                assert (got["size"], got["mtime"]) == (want["size"], want["mtime"]), want
        assert (page["total"], page["truncated"], page["partial"], page["dataset"]) == (
            python["total"], python["truncated"], python["partial"], python["dataset"])


def test_the_page_uses_python_s_limits():
    """A folder given to the site lists as the same folder does on the desktop."""
    local = (JS / "services" / "localFiles.js").read_text(encoding="utf-8")
    for name in ("LIST_DIR_LIMIT", "SCAN_HARD_CAP", "INLINE_STAT_LIMIT", "CHILD_MARKER_LIMIT",
                 "CHILD_SCAN_CAP", "STAT_ENTRIES_LIMIT"):
        assert f"const {name} = {getattr(dir_listing, name)};" in local, name
    assert f"const LISTING_BUDGET_MS = {int(dir_listing.LISTING_BUDGET_S * 1000)};" in local
    from plexora.server.routes import browse_routes

    assert f"const RECENT_LIMIT = {browse_routes.RECENT_LIMIT};" in local
    assert f"const PINNED_LIMIT = {browse_routes.PINNED_LIMIT};" in local


def test_the_page_names_datasets_as_python_does():
    """The labels in a sentence ("This folder is a Xenium run") and the
    licence each type needs."""
    local = (JS / "services" / "localFiles.js").read_text(encoding="utf-8")
    for kind, label in dataset_marks.TYPE_LABELS.items():
        assert f'{kind}: "{label}"' in local, kind
    for kind, entitlement in dataset_marks.ENTITLEMENTS.items():
        assert f'{kind}: "{entitlement}"' in local, kind


def test_the_explorer_s_routes_are_answered_in_the_page_before_python():
    """The five routes are the page's ("js"), and the folders' answer comes
    before the wait for Python's start: a Browse on the home page, pressed
    while Python starts, lists at once."""
    from plexora.browser import routes

    for endpoint in ("list_dir", "stat_entries", "fs_places_route", "picker_prefs",
                     "save_picker_prefs", "search_dir"):
        assert routes.classify(endpoint) == "js", endpoint
    backend = (JS / "services" / "backend.js").read_text(encoding="utf-8")
    assert backend.index("global.PlexoraLocalFiles.answer(request, global)") \
        < backend.index("const gate = global.PlexoraShellGate;")
    shell = (JS / "services" / "shell.js").read_text(encoding="utf-8")
    assert 'at("/client/src/js/services/localFiles.js")' in shell
    assert "global.PlexoraLocalFiles = host.PlexoraLocalFiles || null;" in shell
