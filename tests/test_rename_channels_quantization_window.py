"""A channel rename must not hand a plane another plane's quantization window.

The default 8-bit tiles are quantized with each channel's full-resolution
ceiling (data_model.get_channel_quantization_window), and that ceiling is
persisted so a restart does not re-read every plane. The usual reason to rename
channels is that the names were in the wrong order -- so after a rename that
swaps two names, each name now points at the OTHER plane, and the window served
for it must be that plane's own. Serving the window measured for whichever
plane used to carry the name saturates a brighter plane to solid 255 and
crushes a dimmer one into a few grey levels.
"""

import numpy as np
import tifffile

import plexora
from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from tests import volume_fixtures as vf


def _rename(names, datasource):
    response = plexora.app.test_client().post(
        "/rename_channels", json={"datasource": datasource, "names": names})
    assert response.status_code == 200, response.get_json()


def test_swapped_names_get_the_window_of_the_plane_they_now_name(tmp_path):
    image = tmp_path / "image.tif"
    planes = np.zeros((2, 256, 256), dtype=np.uint16)
    planes[0, 30:60, 30:60] = 500
    planes[0, 10, 10] = 1000          # plane 0's ceiling
    planes[1, 30:60, 30:60] = 20000
    planes[1, 20, 20] = 50000         # plane 1's ceiling
    tifffile.imwrite(image, planes)
    register_image_datasource("qwin_swap", image, channel_names=["CD4", "CD8"])

    data_model.load_datasource("qwin_swap")
    # Both channels viewed once under the first (wrong) names: their windows
    # are measured and persisted.
    assert data_model.get_channel_quantization_window("CD4", "qwin_swap") == (0.0, 1000.0)
    assert data_model.get_channel_quantization_window("CD8", "qwin_swap") == (0.0, 50000.0)

    # The panel was mislabelled: plane 0 is CD8 and plane 1 is CD4.
    _rename(["CD8", "CD4"], "qwin_swap")

    plane_1 = data_model.quantization_window_of(data_model.channels, 1)
    plane_0 = data_model.quantization_window_of(data_model.channels, 0)
    assert data_model.real_channel_index("CD4", "qwin_swap") == 1
    assert data_model.get_channel_quantization_window("CD4", "qwin_swap") == plane_1
    assert data_model.get_channel_quantization_window("CD8", "qwin_swap") == plane_0


def test_swapped_names_get_the_volume_window_of_the_plane_they_now_name(tmp_path):
    path, _data = vf.write_ome_volume(tmp_path / "qwin_vol.ome.tif", axes="CZYX",
                                      channels=2, depth=6, levels=2)
    register_image_datasource("qwin_vol", path, channel_names=["A", "B"])
    data_model.load_datasource("qwin_vol")

    before = {name: data_model.get_channel_quantization_window(name, "qwin_vol", "volume")
              for name in ("A", "B")}
    assert before["A"] != before["B"]

    _rename(["B", "A"], "qwin_vol")

    # Plane 1 is called A now, so A's volume window is what B's was.
    assert data_model.get_channel_quantization_window("A", "qwin_vol", "volume") == before["B"]
    assert data_model.get_channel_quantization_window("B", "qwin_vol", "volume") == before["A"]
