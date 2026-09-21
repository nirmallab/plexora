"""A Xenium `morphology_focus/` folder, read as one multi-channel image.

From XOA 3.0 a run writes four stains as four separate one-channel OME-TIFFs
in one folder. Every reader in Plexora opens ONE path and reads `shape[0]` as
the channel count, so without `server/utils/xenium_focus.py` that is four
one-channel images, four cards, and no way to composite them.

The one-file case is here too, and it asserts the opposite: a folder holding a
single file resolves to that FILE, so the ordinary TIFF reader handles it.
Both answers produce the same picture; the split is about how much new
machinery stands between the user and it.
"""

import numpy as np
import pytest
import tifffile

from plexora import datasource
from plexora.server.models import data_model
from plexora.server.providers import local
from plexora.server.utils import spatial_scene, xenium_focus


def _focus_file(path, name, *, height=64, width=48, value=1, levels=0):
    """One `morphology_focus_NNNN.ome.tif`, named the way the instrument
    names its stains."""
    plane = np.full((height, width), value, dtype=np.uint16)
    metadata = {"Channel": {"Name": name}}
    if not levels:
        tifffile.imwrite(path, plane, ome=True, metadata=metadata,
                         photometric="minisblack")
        return plane
    with tifffile.TiffWriter(path, ome=True, bigtiff=True) as writer:
        writer.write(plane, subifds=levels, tile=(16, 16),
                     photometric="minisblack", metadata=metadata)
        for step in range(1, levels + 1):
            writer.write(plane[::2 ** step, ::2 ** step], subfiletype=1,
                         tile=(16, 16), photometric="minisblack")
    return plane


def _folder(tmp_path, names=("DAPI", "18S"), **kwargs):
    folder = tmp_path / "morphology_focus"
    folder.mkdir(exist_ok=True)
    planes = []
    for index, name in enumerate(names):
        planes.append(_focus_file(
            folder / f"morphology_focus_{index:04d}.ome.tif", name,
            value=index + 1, **kwargs))
    return folder, planes


# -- what counts as a folder --------------------------------------------


def test_several_channel_files_are_one_image(tmp_path):
    folder, _ = _folder(tmp_path, names=("DAPI", "18S", "ATP1A1", "AlphaSMA"))

    assert xenium_focus.is_focus_dir(folder)
    assert len(xenium_focus.focus_files(folder)) == 4


def test_a_folder_holding_one_file_is_left_to_the_plain_reader(tmp_path):
    """Not a judgement about the folder -- a routing decision.

    One file is an ordinary single-channel OME-TIFF, and the path every other
    import in Plexora takes has the mileage on it.
    """
    folder, _ = _folder(tmp_path, names=("DAPI",))

    assert not xenium_focus.is_focus_dir(folder)


def test_a_directory_of_unrelated_tiffs_is_not_a_focus_folder(tmp_path):
    folder = tmp_path / "images"
    folder.mkdir()
    for name in ("morphology_focus_0000.ome.tif", "morphology_focus_0001.ome.tif"):
        _focus_file(folder / name, "DAPI")

    # Matched on the folder's NAME as well as its contents.
    assert not xenium_focus.is_focus_dir(folder)


def test_channel_files_are_ordered_by_their_number_not_their_name(tmp_path):
    folder = tmp_path / "morphology_focus"
    folder.mkdir()
    for index in (0, 2, 10):
        _focus_file(folder / f"morphology_focus_{index:04d}.ome.tif", "x")

    found = [path.name for path in xenium_focus.focus_files(folder)]

    assert found == ["morphology_focus_0000.ome.tif",
                     "morphology_focus_0002.ome.tif",
                     "morphology_focus_0010.ome.tif"]


# -- reading it ---------------------------------------------------------


def test_the_folder_opens_as_one_pyramid_of_n_channels(tmp_path):
    folder, planes = _folder(tmp_path, names=("DAPI", "18S", "ATP1A1"))

    pyramid = xenium_focus.open_focus(folder)

    assert len(pyramid) == 1
    assert pyramid["0"].shape == (3, 64, 48)
    for index, plane in enumerate(planes):
        assert np.array_equal(np.asarray(pyramid["0"][index, 0:64, 0:48]), plane)


def test_the_level_chain_stops_where_the_shallowest_file_does(tmp_path):
    folder = tmp_path / "morphology_focus"
    folder.mkdir()
    _focus_file(folder / "morphology_focus_0000.ome.tif", "DAPI",
                height=128, width=128, levels=2)
    _focus_file(folder / "morphology_focus_0001.ome.tif", "18S",
                height=128, width=128, levels=1)

    pyramid = xenium_focus.open_focus(folder)

    assert len(pyramid) == 2
    assert pyramid.level_shapes == [[128, 128], [64, 64]]


def test_files_on_different_grids_are_refused(tmp_path):
    """Compositing stains that are not the same pixel grid puts them in the
    wrong place, which looks plausible. The one failure worth a hard stop."""
    folder = tmp_path / "morphology_focus"
    folder.mkdir()
    _focus_file(folder / "morphology_focus_0000.ome.tif", "DAPI", height=64, width=48)
    _focus_file(folder / "morphology_focus_0001.ome.tif", "18S", height=32, width=48)

    with pytest.raises(ValueError, match="same pixel grid"):
        xenium_focus.open_focus(folder)


def test_each_channel_is_named_by_its_own_file(tmp_path):
    folder, _ = _folder(tmp_path, names=("DAPI", "ATP1A1/CD45/E-Cadherin"))

    assert xenium_focus.channel_names(folder) == ["DAPI", "ATP1A1/CD45/E-Cadherin"]
    assert datasource.derive_image_channel_names(folder, 2) == (
        ["DAPI", "ATP1A1/CD45/E-Cadherin"], "image metadata")


# -- what the rest of the server sees -----------------------------------


def test_geometry_and_conversion_agree_about_the_folder(tmp_path):
    """A node answers "how big is it" from `image_geometry` while the primary
    records the conversion's numbers, and a project whose two disagree is
    rejected at the attach screen."""
    folder, _ = _folder(tmp_path, names=("DAPI", "18S", "ATP1A1"))

    geometry = local.image_geometry(folder)
    info = data_model.convertOmeTiff(str(folder))

    assert geometry["num_channels"] == info["num_channels"] == 3
    assert geometry["height"] == info["height"] == 64
    assert geometry["width"] == info["width"] == 48
    assert info["channel_names"] == ["morphology_focus_0",
                                     "morphology_focus_1",
                                     "morphology_focus_2"]


def test_a_tile_of_the_folder_reads_the_right_channel(tmp_path):
    folder, planes = _folder(tmp_path, names=("DAPI", "18S", "ATP1A1"))
    pyramid = xenium_focus.open_focus(folder)

    for index, plane in enumerate(planes):
        tile = data_model.read_tile(pyramid, index, 0, "0_0", 1024, 1024)
        assert np.array_equal(np.asarray(tile), plane)


def test_the_folder_is_fluorescence_without_anything_opening_it(tmp_path):
    folder, _ = _folder(tmp_path)

    detection = local.detect_image_type(folder)

    assert detection.verdict == "fluorescence"
    assert detection.confidence == "high"


# -- how a run points at it ---------------------------------------------


def test_a_run_whose_focus_folder_has_several_files_resolves_to_the_folder(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "experiment.xenium").write_text('{"pixel_size": 0.2125}', encoding="utf-8")
    (run / "morphology.ome.tif").write_bytes(b"x")
    _folder(run, names=("DAPI", "18S"))

    chosen = spatial_scene.xenium_image_path(run)

    assert chosen == run / "morphology_focus"
