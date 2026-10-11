"""Cells in micrometres, for a hand-off that measures distance in tissue.

What has to hold: an object table measured in µm is read as measured; any
other table's pixel columns are scaled by the image's pixel size (and z
step, for a table that places cells in depth); an unknown step is refused by
name rather than defaulted; nothing is written to the table; and a hand-off
FROM Plexora says which columns hold the cell's position and in what units.
"""

import numpy as np
import pytest
import tifffile

from plexora import datasets
from plexora.agent import AgentSession, registry
from plexora.agent.coordinates import coordinates_for
from plexora.api.dataset import CoordinatesUnavailable, project_data
from plexora.datasource import register_datasource, register_image_datasource
from plexora.server.models.project import Project
from tests import volume_fixtures as vf


@pytest.fixture(autouse=True)
def _core():
    registry.discover([])


def _object_project(tmp_path, name="objects"):
    from plexora.server.volume import objects

    path, _ = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=1,
                                  depth=6, height=64, width=64, names=["DNA"], physical_z=2.0)
    labels = np.zeros((6, 64, 64), dtype=np.uint16)
    labels[1:4, 10:20, 10:20] = 5
    labels[3:6, 30:40, 30:46] = 9
    tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels)
    register_image_datasource(name, path)
    datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)
    objects.measure_project_objects(Project.find(name), units="physical")


def _flat_project(tmp_path, name="flat", pixel_size=None):
    path, _ = vf.write_ome_volume(tmp_path / f"{name}.ome.tif", axes="CZYX", channels=1,
                                  depth=1, height=64, width=64, names=["DNA"],
                                  physical_z=None, physical_xy=pixel_size)
    table = tmp_path / f"{name}.csv"
    table.write_text("CellID,X,Y,DNA\n1,10,20,5\n2,30,40,7\n", encoding="utf-8")
    register_datasource(name, path, table, x="X", y="Y", id_column="CellID")


def test_a_measured_object_table_is_read_as_measured(tmp_path):
    _object_project(tmp_path)
    frame = project_data("objects").table.physical_coordinates()
    assert set(frame.columns) >= {"cell_id", "X_um", "Y_um", "Z_um"}
    rows = {row["cell_id"]: row for row in frame.iter_rows(named=True)}
    # Cell 5: x 10..19 at 0.5 µm a pixel -> centre 7.25 µm; planes 1..3 at 2 µm -> 4 µm.
    assert rows[5]["X_um"] == pytest.approx(7.25) and rows[5]["Z_um"] == pytest.approx(4.0)
    handed = coordinates_for(Project.find("objects"))
    assert handed["coordinates"] == {"units": "µm",
                                     "columns": {"x": "x_um", "y": "y_um", "z": "z_um"}}
    assert handed["roles"]["z"] == "Z" and handed["roles"]["cell_id"] == "label"


def test_a_pixel_table_is_scaled_by_the_pixel_size(tmp_path):
    _flat_project(tmp_path, pixel_size=0.5)
    frame = project_data("flat").table.physical_coordinates()
    assert frame["X_um"].to_list() == [5.0, 15.0] and frame["Y_um"].to_list() == [10.0, 20.0]
    assert "Z_um" not in frame.columns
    handed = coordinates_for(Project.find("flat"))["coordinates"]
    assert handed["units"] == "pixels" and handed["um_per_unit"] == {"x": 0.5, "y": 0.5}
    assert handed["columns"] == {"x": "X", "y": "Y"} and "missing" not in handed


def test_an_unknown_step_is_refused_by_name(tmp_path):
    _flat_project(tmp_path, name="nosize", pixel_size=None)
    with pytest.raises(CoordinatesUnavailable) as refused:
        project_data("nosize").table.physical_coordinates()
    assert refused.value.missing == "pixel_size"
    handed = coordinates_for(Project.find("nosize"))["coordinates"]
    assert handed["missing"] == ["pixel_size"] and handed["um_per_unit"] == {}


def test_binding_a_table_records_its_depth_and_frame_columns(tmp_path):
    from tests.scimappro_fixtures import make_anndata_project

    made = make_anndata_project(tmp_path, register=False)
    roles = {"cell_id": "CellID", "x": "X_centroid", "y": "Y_centroid",
             "image_id": "imageid", "z": "Z_centroid"}
    answer = registry.invoke(AgentSession(), "bind_project", {
        "table": made["table"], "image": made["image_path"], "image_id": "slide_A",
        "roles": roles, "name": "deep"})
    assert answer["ok"], answer
    record = Project.load("deep")
    assert record.roles.get("z") == "Z_centroid"
    assert "role:z" in set(record.confirmed)
