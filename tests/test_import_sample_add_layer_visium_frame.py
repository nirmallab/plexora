"""A Visium HD table or mask added later lands where a fresh import puts it.

Space Ranger states every position in the run's FULL-RES microscope frame,
and a sample imported with the run's hires PNG is drawn in that picture, a
`tissue_hires_scalef` of it. A fresh import composes that scale into the
table's coordinates and the cell polygons' transform. "+ Add Layer" (and a
drop onto the open sample, and the requirements modal) goes through
`import_sample.add_layers`, and has to do the same: a project that switches
from 8 um bins to the segmented cells there must not move its cells out by
1 / tissue_hires_scalef over the H&E.
"""

import numpy as np
import pytest

from plexora.server.models import import_sample
from plexora.server.models.project import Project

from tests.tenx_fixtures import HIRES_SCALEF, write_visium_hd_run


#: A Visium HD run is detected only with `transcriptomics:visium`.
pytestmark = pytest.mark.pro

pytest.importorskip("pyarrow")


@pytest.fixture
def run(tmp_path):
    return write_visium_hd_run(tmp_path / "Pancreas_addlayer_run", grid=40,
                               levels=(2, 8, 16), segmented=True)


def _bins_then_cells(outs, name):
    """Import the run as 8 um bins, then add its segmented cells later."""
    result = import_sample.import_sample(
        [str(outs)], answers={"bin-size": "8"}, name=name)
    project = Project.load(result["name"])
    # The fresh import's own answer, which "+ Add Layer" has to match.
    assert project.bundles[0]["frameScale"] == pytest.approx(HIRES_SCALEF)
    assert project.dataset.coordinates["scale"] == pytest.approx(HIRES_SCALEF)

    import_sample.add_layers(result["name"], [str(outs)],
                             answers={"bin-size": "cells"})
    return Project.load(result["name"])


def test_a_cells_table_added_later_is_scaled_into_the_hires_picture(run):
    outs, _ = run
    project = _bins_then_cells(outs, "visium_addlayer_table")
    assert project.dataset.src.endswith(".h5ad")
    assert project.dataset.obs_id_field == "cell_id"
    assert project.dataset.coordinates.get("scale") == pytest.approx(
        HIRES_SCALEF), project.dataset.coordinates


def test_cell_polygons_added_later_are_scaled_into_the_hires_picture(run):
    outs, _ = run
    project = _bins_then_cells(outs, "visium_addlayer_mask")
    segmentation = project.segmentation
    assert str(segmentation.source).endswith("cell_segmentations.geojson")
    assert segmentation.transform is not None, (
        "the cell polygons were attached with no transform: drawn in full-res "
        "pixels over the hires picture")
    assert np.allclose(segmentation.transform,
                       (HIRES_SCALEF, 0, 0, HIRES_SCALEF, 0, 0))
