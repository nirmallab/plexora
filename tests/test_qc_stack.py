"""QC of a z-stack as a stack (plugins/qc/capabilities_stack.py).

What has to hold: the stack checks run over a project's planes and keep what
they found; a finding on some planes is written as a QC region limited to
those planes, in the category its class belongs to; and the cells such a
region holds are only the ones on its planes (the cell table's z role) --
while a project whose table cannot say which plane a cell is on gets no
plane-limited region at all, with the reason.
"""

import numpy as np
import pytest
import tifffile
from scipy import ndimage

from plexora import datasets
from plexora.agent import AgentSession, registry
from plexora.api.dataset import project_data
from plexora.datasource import register_image_datasource
from plexora.server.models.project import Project

#: Plexora 3D's analysis paths (plexora/licensing/manifest.py `volume`): these
#: tests exercise them, so they run on a Pro test licence.
pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _plugins():
    registry.discover(["qc", "roi"])


def _invoke(name, arguments):
    session = AgentSession()
    answer = registry.invoke(session, name, arguments)
    assert answer["ok"], answer
    result = answer["result"]
    if isinstance(result, dict) and result.get("job_id") and "findings" not in result:
        done = registry.invoke(session, "job_wait", {"job_id": result["job_id"],
                                                     "timeout_s": 60})
        assert done["ok"], done
        job = done["result"]["job"]
        assert job["status"] == "done", job.get("error")
        result = job["result"]
    return result


def _blurred_stack(tmp_path, name="stackqc", measure=True, whole=0):
    """Two channels, eight planes; DNA's first two planes out of focus. Cells
    on planes 0-2, 3-5 and 5-7, and `whole` more on planes 2-5."""
    rng = np.random.default_rng(4)
    depth, size = 8, 96
    image = np.zeros((2, depth, size, size), dtype=np.float32)
    for c in range(2):
        for z in range(depth):
            plane = rng.normal(100, 4, (size, size))
            for _ in range(30):
                y, x = rng.integers(6, size - 6, 2)
                plane[y - 3:y + 3, x - 3:x + 3] += 1500
            image[c, z] = plane
    image[0, 0] = ndimage.gaussian_filter(image[0, 0], 4)
    image[0, 1] = ndimage.gaussian_filter(image[0, 1], 4)
    path = tmp_path / f"{name}.ome.tif"
    tifffile.imwrite(path, image.astype(np.uint16), ome=True, metadata={
        "axes": "CZYX", "PhysicalSizeX": 0.5, "PhysicalSizeY": 0.5, "PhysicalSizeZ": 2.0,
        "Channel": {"Name": ["DNA", "CD8"]}})
    labels = np.zeros((depth, size, size), dtype=np.uint16)
    labels[0:3, 10:20, 10:20] = 1
    labels[3:6, 40:50, 40:50] = 2
    labels[5:8, 70:80, 20:30] = 3
    for i in range(whole):
        labels[2:6, 82:90, 4 + i * 12:12 + i * 12] = 4 + i
    tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels)
    register_image_datasource(name, path)
    datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)
    if measure:
        from plexora.server.volume import objects

        objects.measure_project_objects(Project.find(name), units="physical")


def test_the_stack_checks_find_out_of_focus_planes_and_keep_them(tmp_path):
    _blurred_stack(tmp_path)
    result = _invoke("run_qc_3d", {"project": "stackqc",
                                   "checks": ["focus_profile", "segmentation_3d"]})
    focus = [f for f in result["findings"] if f["check"] == "focus_profile"]
    assert focus and focus[0]["channel"] == "DNA" and (focus[0]["z0"], focus[0]["z1"]) == (0, 1)
    assert focus[0]["class"] == "out_of_focus"
    objects = [f for f in result["findings"] if f["check"] == "segmentation_3d"]
    assert any(f["kind"] == "clipped_z" for f in objects), "cells 1 and 3 touch the ends"
    again = _invoke("get_qc_3d", {"project": "stackqc"})
    assert [f["id"] for f in again["findings"]] == [f["id"] for f in result["findings"]]


def test_a_finding_on_some_planes_is_a_region_on_them_holding_their_cells(tmp_path):
    from plexora.plugins.qc.server import propagate, roi_link
    from plexora.plugins.roi.server import schema as roi_schema
    from plexora.plugins.roi.server.repository import ROIRepository

    _blurred_stack(tmp_path, "planes")
    found = _invoke("run_qc_3d", {"project": "planes", "checks": ["focus_profile"]})
    focus = next(f for f in found["findings"] if f["check"] == "focus_profile")
    written = _invoke("write_qc_3d_regions", {"project": "planes", "findings": [focus["id"]]})
    assert written["written"] and not written["skipped"]
    features = ROIRepository("planes").load()["images"]["default"]["features"]
    region = next(f for f in features if f["id"] in {w if isinstance(w, str) else w.get("roi_id")
                                                     for w in written["written"]})
    assert region["extent"] == {"kind": "slab", "z0": 0, "z1": 1, "t": 0}
    assert region["category_id"].endswith("blur_focus")
    # Its cells: only cell 1 (planes 0-2, centroid on plane 1), not 2 or 3.
    ds = project_data("planes")
    pairs, _ = propagate.propagate(ds, [{"roi_id": region["id"], "geometry": region["geometry"],
                                         "extent": region["extent"]}])
    assert sorted(pairs["cell_id"].to_list()) == [1]
    assert roi_schema.extent_planes(region["extent"]) == (0, 1)
    # And QC's own list of live regions carries the planes to the cell calls.
    from plexora.plugins.qc.server import results

    active = results.ensure_active(results.load("planes"), "planes")
    live = roi_link.live_regions(ds, active)
    assert any(r.get("extent") == region["extent"] for r in live), live


def test_without_a_z_column_no_plane_limited_region_is_written(tmp_path):
    _blurred_stack(tmp_path, "noz", measure=False)
    found = _invoke("run_qc_3d", {"project": "noz", "checks": ["focus_profile"]})
    assert any(f["check"] == "focus_profile" for f in found["findings"])
    answer = registry.invoke(AgentSession(), "write_qc_3d_regions", {"project": "noz"})
    skipped = (answer.get("result") or {}).get("skipped") or []
    assert (not answer["ok"]) or (skipped and "z column" in skipped[0]["why"]), answer


def test_an_object_review_judges_cells_from_their_views_and_writes_the_bad_ones(tmp_path):
    from plexora.plugins.roi.server.repository import ROIRepository

    _blurred_stack(tmp_path, "review")
    started = _invoke("object_review_start", {"project": "review", "reasons": ["clipped_z"]})
    assert started["objects"] == 2 and started["state"] == "open"
    assert started["delegate"]["role"] == "object_review_worker"
    assert "review-objects" in started["delegate"]["brief"]
    review = started["review_id"]
    first = _invoke("object_review_next", {"project": "review", "review_id": review})
    assert first["object"]["object_id"] in (1, 3) and "clipped_z" in first["object"]["reasons"]
    assert first.get("_images") or first.get("image_inline") is False
    verdicts = {}
    for _ in range(2):
        packet = _invoke("object_review_next", {"project": "review", "review_id": review})
        cell = packet["object"]["object_id"]
        verdicts[cell] = "clipped" if cell == 1 else "ok"
        _invoke("object_review_answer", {"project": "review", "review_id": review,
                                         "object_id": cell, "verdict": verdicts[cell],
                                         "note": "test"})
    assert _invoke("object_review_next", {"project": "review", "review_id": review})["object"] \
        is None
    finished = _invoke("object_review_finish", {"project": "review", "review_id": review})
    assert finished["not_one_cell"] == [1] and finished["verdicts"] == {"clipped": 1, "ok": 1}
    features = ROIRepository("review").load()["images"]["default"]["features"]
    region = next(f for f in features if f["id"] == finished["region"]["roi_id"])
    assert region["solid"]["kind"] == "labels" and region["solid"]["ids"] == [1]
    assert region["category_id"].endswith("segmentation") and "qc-class:" in region["notes"]


def test_cells_cut_off_by_the_stacks_ends_are_left_out_of_estimation_under_strict(tmp_path):
    from plexora.agent import cell_exclusions
    from plexora.plugins.qc.server import exclusions

    _blurred_stack(tmp_path, "clipped", whole=2)
    ds = project_data("clipped")
    strict = cell_exclusions.for_project(ds, mode_value="strict")
    assert strict is not None and strict.whole.tolist() == [1, 3], "cells 1 and 3 touch the ends"
    with cell_exclusions.mode("strict"):
        block = cell_exclusions.describe(ds, strict)
    assert block["applied"] and block["n_left_out"] == 2 and block["n_clipped_z"] == 2
    assert exclusions.summary(ds, "strict")["by_reason"] == {"clipped_z": 2}
    # `exclude` keeps them: a cut-off cell is doubtful, not wrong.
    assert cell_exclusions.for_project(ds, mode_value="exclude") is None


def test_a_stack_that_cuts_off_most_of_its_cells_leaves_none_out(tmp_path):
    from plexora.agent import cell_exclusions

    _blurred_stack(tmp_path, "thin")            # two of three cells touch an end
    assert cell_exclusions.for_project(project_data("thin"), mode_value="strict") is None


def test_a_table_measured_from_another_mask_leaves_no_cell_out(tmp_path):
    import json

    from plexora import paths
    from plexora.agent import cell_exclusions
    from plexora.server.volume import objects

    _blurred_stack(tmp_path, "othermask", whole=2)
    manifest = paths.project_state_dir("othermask") / objects.MANIFEST_NAME
    record = json.loads(manifest.read_text())
    record["labels"] = "/somewhere/else.zarr"
    manifest.write_text(json.dumps(record))
    table = paths.project_state_dir("othermask") / objects.TABLE_NAME
    table.touch()                     # a new stamp, so the answer is read again
    assert cell_exclusions.for_project(project_data("othermask"), mode_value="strict") is None
