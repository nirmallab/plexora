"""Channel names read from a list written *beside* the image.

An Akoya / CODEX export puts the panel in `channelNames.txt` at the root of the
region folder and nothing at all in the TIFF, so Plexora named those channels
"Channel 1..92" while QuPath -- which goes through Bio-Formats and reads that
file -- opened the same stack with HOECHST1 and CD8 already on the list.

The tier is deliberately timid, and these tests pin the timidity as much as the
feature: it is consulted only where the answer was previously None, it accepts
only a single column that accounts for every channel, and a file that needs a
question asked leaves the names generic rather than guessing at them.
"""

import numpy as np
import tifffile

from plexora.datasource import derive_image_channel_names

PANEL = ["DAPI", "CD3", "CD8", "PD1"]


def _write_image(path, channels=4, size=32, ome_channel_names=None):
    data = np.zeros((channels, size, size), dtype=np.uint8)
    if ome_channel_names is not None:
        tifffile.imwrite(path, data, ome=True,
                         metadata={"Channel": {"Name": ome_channel_names}})
    else:
        tifffile.imwrite(path, data)


def _write_sidecar(path, names, header=None):
    lines = ([header] if header else []) + list(names)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_names_come_from_a_list_beside_the_image(tmp_path):
    image = tmp_path / "image.tif"
    _write_image(image)
    _write_sidecar(tmp_path / "channelNames.txt", PANEL)

    names, source = derive_image_channel_names(image, n_channels=4)

    assert names == PANEL
    assert source == "image metadata"


def test_names_come_from_the_parent_directory_too(tmp_path):
    # The CODEX layout: the list sits at the region root and the stacks sit in
    # a subdirectory (`bestFocus/`, or whatever the processing step wrote).
    stacks = tmp_path / "bestFocus"
    stacks.mkdir()
    image = stacks / "reg001.tif"
    _write_image(image)
    _write_sidecar(tmp_path / "channelNames.txt", PANEL)

    names, _ = derive_image_channel_names(image, n_channels=4)

    assert names == PANEL


def test_the_image_own_directory_wins_over_the_parent(tmp_path):
    stacks = tmp_path / "bestFocus"
    stacks.mkdir()
    image = stacks / "reg001.tif"
    _write_image(image)
    _write_sidecar(tmp_path / "channelNames.txt", ["far", "far", "far", "far"])
    _write_sidecar(stacks / "channelNames.txt", PANEL)

    names, _ = derive_image_channel_names(image, n_channels=4)

    assert names == PANEL


def test_a_header_row_is_recognised(tmp_path):
    image = tmp_path / "image.tif"
    _write_image(image)
    _write_sidecar(tmp_path / "channelNames.txt", PANEL, header="marker")

    names, _ = derive_image_channel_names(image, n_channels=4)

    assert names == PANEL


def test_a_list_that_does_not_account_for_every_channel_is_refused(tmp_path):
    # Mislabelling a panel is worse than not labelling it: a wrong name is
    # read as a fact and a generic one is read as "nobody said".
    image = tmp_path / "image.tif"
    _write_image(image, channels=4)
    _write_sidecar(tmp_path / "channelNames.txt", ["DAPI", "CD3"])

    names, source = derive_image_channel_names(image, n_channels=4)

    assert names == ["Channel 1", "Channel 2", "Channel 3", "Channel 4"]
    assert source == "generic"


def test_a_multi_column_table_is_left_for_the_user_to_map(tmp_path):
    # Which column holds the marker is a question, and the upload modal is
    # where it gets asked. Answering it here would be a guess.
    image = tmp_path / "image.tif"
    _write_image(image, channels=4)
    (tmp_path / "channelNames.txt").write_text(
        "1,DAPI\n2,CD3\n3,CD8\n4,PD1\n", encoding="utf-8")

    names, source = derive_image_channel_names(image, n_channels=4)

    assert source == "generic"


def test_metadata_inside_the_file_still_outranks_the_sidecar(tmp_path):
    # The tier is a fallback, not an override -- no project that already
    # resolved names can have them change underneath it.
    image = tmp_path / "image.ome.tif"
    _write_image(image, ome_channel_names=PANEL)
    _write_sidecar(tmp_path / "channelNames.txt",
                   ["wrong", "wrong", "wrong", "wrong"])

    names, _ = derive_image_channel_names(image, n_channels=4)

    assert names == PANEL


def test_no_sidecar_is_still_generic_names(tmp_path):
    image = tmp_path / "image.tif"
    _write_image(image, channels=2)

    names, source = derive_image_channel_names(image, n_channels=2)

    assert names == ["Channel 1", "Channel 2"]
    assert source == "generic"


def test_an_unreadable_sidecar_is_not_a_failed_import(tmp_path):
    image = tmp_path / "image.tif"
    _write_image(image, channels=2)
    (tmp_path / "channelNames.txt").write_bytes(b"\xff\xfe\x00garbage\x00")

    names, source = derive_image_channel_names(image, n_channels=2)

    assert source in ("generic", "image metadata")
    assert len(names) == 2
