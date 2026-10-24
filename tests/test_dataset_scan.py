"""Finding a cohort's files: what the walk keeps, skips, and never enters.

Two promises are pinned here besides the obvious. The walk is names and stat
only -- a header is read once per file, in the background, and never again for
the same file version. And everything left out is left out ON THE RECORD:
a folder that could not be read, a link that was not followed, a budget that
ran out, each says so instead of the scan coming back quietly smaller.
"""

import os
import threading
import time

import pytest

from plexora.server.models import dataset_scan, import_proposal, layer_jobs
from tests import dataset_fixtures as fx
from tests.ngff_fixtures import write_ngff, write_spatialdata_like


@pytest.fixture(autouse=True)
def _forget_scans():
    dataset_scan.forget()
    yield
    for owner, task_id in layer_jobs.running_tasks(prefix="scan-"):
        layer_jobs.stop_task(owner, task_id)
    dataset_scan.forget()


def _rels(inventory):
    return sorted(entry.rel for entry in inventory.entries.values())


def _walk(root, **limits):
    return dataset_scan.walk([root], dataset_scan.Inventory(), limits=limits or None)


def test_hidden_temporary_and_system_files_are_skipped(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "A.ome.tiff")
    fx.image(root / ".hidden" / "B.ome.tiff")
    fx.image(root / "__MACOSX" / "C.ome.tiff")
    fx.image(root / "._A.ome.tiff")
    (root / "~$sheet.csv").write_text("a,b\n", encoding="utf-8")
    (root / "download.csv.crdownload").write_text("a,b\n", encoding="utf-8")
    (root / "README.txt").write_text("hello, world\n", encoding="utf-8")
    assert _rels(_walk(root)) == ["A.ome.tiff"]


def test_plexoras_own_pyramids_are_not_samples(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "A.tif")
    fx.image(root / "A.pyramid.ome.tiff")
    fx.mask(root / "A_mask.tif")
    fx.mask(root / "A_mask.labels.pyramid.ome.tiff")
    fx.image(root / "Lonely.pyramid.ome.tiff")
    inventory = _walk(root)
    assert _rels(inventory) == ["A.tif", "A_mask.tif", "Lonely.pyramid.ome.tiff"]
    assert len(inventory.skipped) == 2


def test_a_zarr_store_is_one_entry_and_never_entered(tmp_path):
    root = tmp_path / "cohort"
    write_ngff(root / "slide.ome.zarr")
    inventory = _walk(root)
    [entry] = inventory.entries.values()
    assert entry.rel == "slide.ome.zarr" and entry.format == "zarr"
    assert inventory.counts["dirs"] == 2  # the root and the store, no chunks


def test_a_spatialdata_store_is_one_bundle(tmp_path):
    root = tmp_path / "cohort"
    write_spatialdata_like(root / "sample.zarr")
    inventory = _walk(root)
    [entry] = inventory.entries.values()
    assert entry.kind == "bundle" and entry.format == "spatialdata"


@pytest.mark.pro
def test_a_xenium_run_is_one_bundle_project(tmp_path):
    from plexora.server.models import dataset_plan
    from tests.test_import_proposal import _xenium_run

    root = tmp_path / "cohort"
    _xenium_run(root / "output-XETG_1")
    fx.mask(root / "output-XETG_1_extra_mask.tif")
    inventory = dataset_scan.scan([root])
    run = next(e for e in inventory.entries.values() if e.kind == "bundle")
    assert run.format == "xenium" and run.role == "image"
    assert run.layers and run.label
    plan = dataset_plan.plan(inventory, config={})
    [project] = plan["projects"]
    assert project["kind"] == "bundle"


def test_a_xenium_run_without_the_licence_says_why(tmp_path):
    from plexora.server.models import dataset_plan
    from tests.test_import_proposal import _xenium_run

    root = tmp_path / "cohort"
    _xenium_run(root / "run1")
    plan = dataset_plan.plan(dataset_scan.scan([root]), config={})
    assert plan["projects"] == []
    [item] = plan["unrecognised"]
    assert "licence" in item["reason"]


def test_a_linked_folder_is_not_followed_and_a_linked_file_counts_once(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "images" / "A.ome.tiff")
    os.symlink(root, root / "loop")
    os.symlink(root / "images" / "A.ome.tiff", root / "A_again.ome.tiff")
    inventory = _walk(root)
    assert _rels(inventory) == ["A_again.ome.tiff", "images/A.ome.tiff"]
    assert any(s["path"].endswith("loop") for s in inventory.skipped)
    assert len({e.real for e in inventory.entries.values()}) == 1


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="permissions do not bind here")
def test_an_unreadable_folder_is_reported_and_the_walk_goes_on(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "A.ome.tiff")
    locked = root / "locked"
    fx.image(locked / "B.ome.tiff")
    locked.chmod(0)
    try:
        inventory = _walk(root)
    finally:
        locked.chmod(0o755)
    assert _rels(inventory) == ["A.ome.tiff"]
    assert inventory.errors == [{"path": str(locked), "error": "Permission denied"}]


def test_depth_and_entry_budgets_stop_the_walk_and_say_so(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "a" / "b" / "c" / "deep.ome.tiff", size=64)
    fx.image(root / "top.ome.tiff", size=64)
    shallow = _walk(root, max_depth=2)
    assert _rels(shallow) == ["top.ome.tiff"]
    assert shallow.budget_hit["reason"] == "depth"

    for index in range(30):
        (root / f"note{index}.log").write_text("x", encoding="utf-8")
    capped = _walk(root, max_entries=10)
    assert capped.budget_hit["reason"] == "entries"


def test_pipeline_intermediates_and_raw_cycles_beside_registered_are_skipped(tmp_path):
    root = fx.mcmicro(tmp_path / "run")
    fx.image(root / "raw" / "LSP1_cycle1.ome.tif", size=64)
    rels = _rels(_walk(root))
    assert not any(rel.startswith(("raw/", "qc/", "probability-maps/")) for rel in rels)


def test_cancel_keeps_what_was_found(tmp_path):
    root = tmp_path / "cohort"
    for index in range(5):
        fx.image(root / f"d{index}" / "x.ome.tiff", size=64)
    cancel = threading.Event()
    seen = []

    def progress(done, total, message):
        seen.append(done)
        if done == 2:
            cancel.set()

    inventory = dataset_scan.walk([root], dataset_scan.Inventory(), cancel=cancel,
                                  progress=progress)
    assert 0 < len(inventory.entries) < 5


def test_inspection_is_cached_by_file_version(tmp_path, monkeypatch):
    root = fx.layout_a(tmp_path / "cohort")
    calls = []
    real = import_proposal._detect_file
    monkeypatch.setattr(import_proposal, "_detect_file",
                        lambda path, *a, **k: calls.append(path) or real(path, *a, **k))
    dataset_scan.scan([root])
    first = len(calls)
    assert first == 5  # three images and two masks; tables are read apart
    dataset_scan.scan([root])
    assert len(calls) == first
    time.sleep(0.01)
    fx.mask(root / "masks" / "A_mask.ome.tiff", size=256)
    os.utime(root / "masks" / "A_mask.ome.tiff", ns=(1, 1))
    dataset_scan.scan([root])
    assert len(calls) == first + 1


def test_tables_are_read_by_their_header_only(tmp_path, monkeypatch):
    root = fx.layout_a(tmp_path / "cohort")
    monkeypatch.setattr(import_proposal, "_table_size",
                        lambda path: pytest.fail("a table was counted"))
    inventory = dataset_scan.scan([root])
    tables = [e for e in inventory.entries.values() if e.kind == "table"]
    assert {e.role for e in tables} == {"table"}
    assert tables[0].columns[:3] == ("CellID", "X_centroid", "Y_centroid")


def test_an_unreadable_file_is_unrecognised_not_fatal(tmp_path):
    root = tmp_path / "cohort"
    fx.image(root / "A.ome.tiff")
    (root / "broken.tif").write_bytes(b"not a tiff")
    inventory = dataset_scan.scan([root])
    broken = next(e for e in inventory.entries.values() if e.name == "broken.tif")
    assert broken.role is None and broken.reason


def test_after_the_budget_files_are_classified_by_name(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    inventory = dataset_scan.scan([root], budget_s=0)
    masks = [e for e in inventory.entries.values() if "mask" in e.name]
    assert masks and all(e.role == "mask" and e.budgeted for e in masks)


# -- as a background task ------------------------------------------------------


def _wait(token, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = dataset_scan.snapshot(token)
        if not found["pending"]:
            return found
        time.sleep(0.05)
    raise AssertionError("the scan did not finish")


def test_a_scan_task_reports_progress_and_ends_with_a_plan(tmp_path):
    root = fx.layout_b(tmp_path / "cohort")
    first = dataset_scan.start_scan([str(root)])
    assert first["token"].startswith("scan-")
    done = _wait(first["token"])
    assert done["status"] == "ready"
    assert done["progress"]["percent"] == 100
    assert [p["name"] for p in done["projects"]] == ["Sample01", "Sample02", "Sample03"]
    assert done["progress"]["candidates"] == 8


def test_adding_a_folder_walks_only_the_new_one(tmp_path, monkeypatch):
    first_root = fx.layout_a(tmp_path / "one")
    second_root = tmp_path / "two"
    fx.image(second_root / "D.ome.tiff")
    token = dataset_scan.start_scan([str(first_root)])["token"]
    _wait(token)
    calls = []
    real = import_proposal._detect_file
    monkeypatch.setattr(import_proposal, "_detect_file",
                        lambda path, *a, **k: calls.append(path) or real(path, *a, **k))
    dataset_scan.extend_scan(token, [str(second_root)])
    done = _wait(token)
    assert [str(p) for p in calls] == [str(second_root / "D.ome.tiff")]
    assert [p["name"] for p in done["projects"]] == ["A", "B", "C", "D"]


def test_overrides_are_kept_on_the_scan(tmp_path):
    root = fx.layout_a(tmp_path / "cohort")
    token = dataset_scan.start_scan([str(root)])["token"]
    _wait(token)
    dataset_scan.set_overrides(token, {"rename": {"p:a": "Renamed"}})
    assert dataset_scan.snapshot(token)["projects"][0]["name"] == "Renamed"


def test_cancelling_a_scan_keeps_a_usable_partial_plan(tmp_path, monkeypatch):
    root = fx.layout_a(tmp_path / "cohort")
    gate = threading.Event()
    real = import_proposal._detect_file

    def slow(path, *args, **kwargs):
        gate.wait(5)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(import_proposal, "_detect_file", slow)
    token = dataset_scan.start_scan([str(root)])["token"]
    time.sleep(0.2)
    gate.set()
    assert dataset_scan.cancel_scan(token)
    found = dataset_scan.snapshot(token)
    assert found["status"] == "cancelled" and not found["pending"]
    assert found["projects"]


def test_a_folder_that_is_not_there_is_refused(tmp_path):
    with pytest.raises(ValueError):
        dataset_scan.start_scan([str(tmp_path / "nope")])
    with pytest.raises(ValueError):
        dataset_scan.start_scan([])


def test_a_big_tree_reads_only_its_images(tmp_path, monkeypatch):
    root = fx.big_tree(tmp_path / "tree", files=1000, rasters=30)
    calls = []
    real = import_proposal._detect_file
    monkeypatch.setattr(import_proposal, "_detect_file",
                        lambda path, *a, **k: calls.append(path) or real(path, *a, **k))
    inventory = dataset_scan.scan([root])
    assert len(calls) == 30
    assert inventory.counts["files"] == 1030


@pytest.mark.perf
@pytest.mark.skipif(not os.environ.get("PLEXORA_PERF"), reason="set PLEXORA_PERF=1 to time")
def test_walking_five_thousand_files_is_quick(tmp_path):
    root = fx.big_tree(tmp_path / "tree", files=5000, rasters=150)
    started = time.monotonic()
    inventory = dataset_scan.walk([root], dataset_scan.Inventory())
    walked = time.monotonic() - started
    print(f"walk: {walked:.3f}s for {inventory.counts['files']} files")
    assert walked < 3


def test_a_folder_of_one_file_per_marker_is_one_unreadable_row(tmp_path, monkeypatch):
    """MIBI and CODEX exports write a folder per field with a TIFF per marker.
    Each file is one channel of one image, not a sample: read as samples they
    made one project per MARKER across every field. Plexora reads
    multichannel images, so the folder is said once, and its files beyond the
    few opened to decide are never read."""
    from plexora.server.models import dataset_plan

    root = tmp_path / "mibi"
    markers = ("CD3", "CD4", "CD8", "CD20", "CD45", "Ki67", "DAPI", "Vimentin")
    for fov in ("fov1", "fov2"):
        for marker in markers:
            fx.image(root / "image_data" / fov / f"{marker}.tif", size=64, channels=1)
    calls = []
    real = import_proposal._detect_file
    monkeypatch.setattr(import_proposal, "_detect_file",
                        lambda path, *a, **k: calls.append(path) or real(path, *a, **k))
    plan = dataset_plan.plan(dataset_scan.scan([root]), config={})
    assert plan["projects"] == [] and calls == []
    assert [u["rel"] for u in plan["unrecognised"]] == ["image_data/fov1", "image_data/fov2"]
    assert "one per marker" in plan["unrecognised"][0]["reason"]


def test_a_numbered_series_of_single_channel_images_is_still_samples(tmp_path):
    from plexora.server.models import dataset_plan

    root = tmp_path / "cores"
    for index in range(1, 9):
        fx.image(root / f"A{index}.tif", size=64, channels=1)
    plan = dataset_plan.plan(dataset_scan.scan([root]), config={})
    assert len(plan["projects"]) == 8 and plan["unrecognised"] == []
