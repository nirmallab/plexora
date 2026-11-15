"""The revision check holds between processes, not just threads (audit pg-roi-3).

`plexora mcp serve` runs the ROI capabilities in its own process against the
same data root as the viewer. With a thread-only lock around `apply`, both
processes could read revision N and both write N+1: one side's regions were
lost and neither was told it conflicted. Here a real second Python process is
paused between its revision check and its write, and the viewer-side save that
arrives in that window must wait and then be refused -- not succeed and be
silently overwritten.
"""

import os
import subprocess
import sys
import textwrap

import numpy as np
import polars as pl
import pytest
import tifffile

from plexora.plugins.roi.server.repository import ConflictError, ROIRepository


def create(roi_id):
    """One region and its category, as one operation (test_roi_repository's)."""
    return {"op": "roi.bulk_create", "image": "default",
            "categories": [{"id": "c-1", "label": "Tumor", "color": "#e04c4c",
                            "sort_order": 0}],
            "features": [{"id": roi_id, "category_id": "c-1", "geometry": {
                "type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 0]]]}}]}


@pytest.fixture
def project(tmp_path):
    image_path = tmp_path / "image.tif"
    csv_path = tmp_path / "cells.csv"
    tifffile.imwrite(image_path, np.zeros((2, 64, 64), dtype=np.uint8))
    pl.DataFrame({"CellID": np.arange(4, dtype=np.uint32),
                  "X_centroid": np.linspace(5, 50, 4, dtype=np.float32),
                  "Y_centroid": np.linspace(5, 50, 4, dtype=np.float32),
                  "MarkerA": np.linspace(0, 3, 4, dtype=np.float32)}).write_csv(csv_path)
    from plexora import datasource as datasource_module

    datasource_module.register_datasource(
        name="roi_race", image=image_path, features=csv_path, x="X_centroid",
        y="Y_centroid", segmentation=None)
    return "roi_race"

SECOND_PROCESS = textwrap.dedent("""
    import sys, time
    from plexora.plugins.roi.server import repository as r

    def create(roi_id):
        return {"op": "roi.bulk_create", "image": "default",
                "categories": [{"id": "c-1", "label": "Tumor", "color": "#e04c4c",
                                "sort_order": 0}],
                "features": [{"id": roi_id, "category_id": "c-1", "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 0]]]}}]}

    real = r.ROIRepository._write

    def paused(self, state):
        print("checked", flush=True)  # revision checked, nothing written yet
        time.sleep(2.0)
        real(self, state)

    r.ROIRepository._write = paused
    print(r.ROIRepository(sys.argv[1]).apply(0, [create("from-agent")]), flush=True)
""")


def _second_process(*args):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    return subprocess.Popen([sys.executable, "-c", SECOND_PROCESS, *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_a_save_racing_another_process_is_refused_not_lost(project):
    name = project
    other = _second_process(name)
    try:
        line = other.stdout.readline().strip()
        assert line == "checked", other.stderr.read()

        with pytest.raises(ConflictError) as caught:
            ROIRepository(name).apply(0, [create("from-viewer")])
        assert caught.value.current_revision == 1
    finally:
        out, err = other.communicate(timeout=60)
    assert other.returncode == 0, err
    assert out.strip() == "1"

    state = ROIRepository(name).load()
    assert state["revision"] == 1
    assert [f["id"] for f in state["images"]["default"]["features"]] == ["from-agent"]
