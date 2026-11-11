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


# -- XOA 4.0: files named after their stain -----------------------------
#
# XOA 4.0 renamed `morphology_focus_000N.ome.tif` to `ch000N_<stain>.ome.tif`.
# Unrecognised, a four-stain v4 run fell through to the first `*.ome.tif` and
# imported as DAPI alone, with nothing said.

XOA4_STAINS = ("dapi", "atp1a1_cd45_e-cadherin", "18s", "alphasma_vimentin")
XOA4_OME = ("DAPI", "ATP1A1/CD45/E-Cadherin", "18S", "AlphaSMA/Vimentin")


def _plain_file(path, *, height=64, width=48, value=1):
    """An OME-TIFF whose channel carries no Name."""
    plane = np.full((height, width), value, dtype=np.uint16)
    tifffile.imwrite(path, plane, ome=True, photometric="minisblack")
    return plane


def _v4_folder(root, stains=XOA4_STAINS, ome_names=XOA4_OME, order=None):
    folder = root / "morphology_focus"
    folder.mkdir(exist_ok=True)
    planes = {}
    # Written out of order, so a directory listing's order cannot pass the
    # test for the channel number's.
    for index in (order or reversed(range(len(stains)))):
        path = folder / f"ch{index:04d}_{stains[index]}.ome.tif"
        if ome_names is None:
            planes[index] = _plain_file(path, value=index + 1)
        else:
            planes[index] = _focus_file(path, ome_names[index], value=index + 1)
    return folder, [planes[index] for index in sorted(planes)]


def test_xoa4_stain_named_files_are_one_image(tmp_path):
    folder, planes = _v4_folder(tmp_path)

    assert xenium_focus.is_focus_dir(folder)
    assert [path.name for path in xenium_focus.focus_files(folder)] == [
        f"ch{index:04d}_{stain}.ome.tif" for index, stain in enumerate(XOA4_STAINS)]
    pyramid = xenium_focus.open_focus(folder)
    assert pyramid["0"].shape == (4, 64, 48)
    for index, plane in enumerate(planes):
        assert np.array_equal(np.asarray(pyramid["0"][index, 0:64, 0:48]), plane)


def test_xoa4_channels_are_ordered_by_number_past_nine(tmp_path):
    folder = tmp_path / "morphology_focus"
    folder.mkdir()
    for index in (10, 2, 0):
        _focus_file(folder / f"ch{index:04d}_stain{index}.ome.tif", f"S{index}")

    assert [path.name for path in xenium_focus.focus_files(folder)] == [
        "ch0000_stain0.ome.tif", "ch0002_stain2.ome.tif", "ch0010_stain10.ome.tif"]


def test_xoa4_channels_are_named_by_their_ome_xml(tmp_path):
    folder, _ = _v4_folder(tmp_path)

    assert xenium_focus.channel_names(folder) == list(XOA4_OME)
    assert datasource.derive_image_channel_names(folder, 4) == (
        list(XOA4_OME), "image metadata")
    assert xenium_focus.open_focus(folder).channel_names == list(XOA4_OME)


def test_xoa4_channels_without_ome_names_are_named_by_their_file(tmp_path):
    folder, _ = _v4_folder(tmp_path, ome_names=None)

    assert xenium_focus.channel_names(folder) == [
        "DAPI", "ATP1A1/CD45/E-CADHERIN", "18S", "ALPHASMA/VIMENTIN"]


def test_an_xoa4_run_resolves_to_the_folder_through_its_manifest(tmp_path):
    """The v4 manifest names the DAPI file inside the folder; the folder is
    what it describes."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "experiment.xenium").write_text(
        '{"images": {"morphology_focus_filepath": '
        '"morphology_focus/ch0000_dapi.ome.tif"}}', encoding="utf-8")
    _v4_folder(run)

    assert spatial_scene.xenium_image_path(run) == run / "morphology_focus"


def test_an_xoa4_run_without_a_manifest_resolves_to_the_folder(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "morphology.ome.tif").write_bytes(b"x")
    _v4_folder(run)

    assert spatial_scene.xenium_image_path(run) == run / "morphology_focus"


def test_a_one_stain_xoa4_folder_is_left_to_the_plain_reader(tmp_path):
    """The real XOA 4.0 tiny bundle: `ch0000_dapi.ome.tif` alone."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "experiment.xenium").write_text(
        '{"images": {"morphology_focus_filepath": '
        '"morphology_focus/ch0000_dapi.ome.tif"}}', encoding="utf-8")
    folder, _ = _v4_folder(run, stains=("dapi",), ome_names=("DAPI",))

    assert not xenium_focus.is_focus_dir(folder)
    assert spatial_scene.xenium_image_path(run) == folder / "ch0000_dapi.ome.tif"
    assert spatial_scene._xenium_path(run, "morphology_focus") == \
        folder / "ch0000_dapi.ome.tif"


def test_unrecognised_names_are_not_channels(tmp_path):
    folder, _ = _v4_folder(tmp_path, stains=("dapi", "18s"), ome_names=("DAPI", "18S"))
    for stray in ("notes.ome.tif", "chA_dapi.ome.tif", "ch0002_x.ome.tif.bak",
                  "ch0003.ome.tif", "morphology_focus.ome.tif"):
        _focus_file(folder / stray, "stray")

    assert [path.name for path in xenium_focus.focus_files(folder)] == [
        "ch0000_dapi.ome.tif", "ch0001_18s.ome.tif"]
    assert xenium_focus.open_focus(folder)["0"].shape[0] == 2


def test_a_folder_mixing_both_namings_is_not_assembled(tmp_path):
    """Two runs' files copied into one folder: `morphology_focus_0001` and
    `ch0001_...` both claim channel 1. Composing them would put a stain in the
    other's slot, so it is not one image -- one file is opened instead."""
    run = tmp_path / "run"
    run.mkdir()
    folder = run / "morphology_focus"
    folder.mkdir()
    _focus_file(folder / "morphology_focus_0000.ome.tif", "DAPI")
    _focus_file(folder / "morphology_focus_0001.ome.tif", "18S")
    _focus_file(folder / "ch0000_dapi.ome.tif", "DAPI")
    _focus_file(folder / "ch0001_atp1a1_cd45_e-cadherin.ome.tif", "ATP1A1/CD45/E-Cadherin")

    assert xenium_focus.focus_files(folder) == []
    assert not xenium_focus.is_focus_dir(folder)
    chosen = spatial_scene.xenium_image_path(run)
    assert chosen is not None and chosen.is_file()


def test_two_files_claiming_one_channel_are_not_assembled(tmp_path):
    folder = tmp_path / "morphology_focus"
    folder.mkdir()
    _focus_file(folder / "ch0000_dapi.ome.tif", "DAPI")
    _focus_file(folder / "ch0001_18s.ome.tif", "18S")
    _focus_file(folder / "ch0001_vimentin.ome.tif", "Vimentin")

    assert xenium_focus.focus_files(folder) == []
    assert not xenium_focus.is_focus_dir(folder)
