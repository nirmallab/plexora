"""The import's guess whether an .ims is a mask, kept to named channels in a wide file.

`ims.mask_verdict` reads each channel's middle window to tell ids from stain.
In a file of more than `GUESS_CHANNELS` channels that was a level-0 read per
channel -- a 349 GB, 70-channel file over SFTP fetched 2.2 GB to learn that
70 named stains are not a mask. Past GUESS_CHANNELS only the channels whose
names say mask are read, unless the FILE's name says mask. A mask channel
left unnamed among many is still found when the user says the file is a mask
(`ImsLabels`, through `label_channel`, which reads every channel). Files of
GUESS_CHANNELS channels or fewer are guessed exactly as before
(tests/test_ims_masks.py).
"""

import numpy as np
import pytest

from plexora.server.volume.sources import ims

from tests.test_ims_masks import _cells, _stain, write_ims

STAINS = ["DAPI", "CD3", "CD4", "CD8", "CD20", "CD68"]


def _stains(names):
    return [(name, _stain(seed=index)) for index, name in enumerate(names)]


@pytest.fixture
def windows_read(monkeypatch):
    """The channels whose window the guess read, and every window it judged."""
    read = []
    judged = []
    window = ims._window
    looks = ims.looks_like_labels

    def reading(handle, channel, levels):
        read.append(int(channel))
        return window(handle, channel, levels)

    def judging(array):
        judged.append(array.shape)
        return looks(array)

    monkeypatch.setattr(ims, "_window", reading)
    monkeypatch.setattr(ims, "looks_like_labels", judging)
    return read, judged


def test_guess_channels_is_four():
    assert ims.GUESS_CHANNELS == 4


def test_a_wide_file_of_named_stains_is_an_image_without_a_window_read(tmp_path, windows_read):
    assert len(STAINS) > ims.GUESS_CHANNELS
    path = write_ims(tmp_path / "panel.ims", _stains(STAINS))
    read, judged = windows_read

    assert ims.mask_verdict(path) is False
    assert read == []
    assert judged == []


def test_a_wide_file_with_a_channel_named_mask_reads_that_channel_only(tmp_path, windows_read):
    channels = _stains(STAINS)
    channels.insert(3, ("cell mask", _cells()))
    path = write_ims(tmp_path / "panel.ims", channels)
    read, judged = windows_read

    assert ims.mask_verdict(path) is True
    assert read == [3]
    assert len(judged) == 1


def test_a_wide_file_with_an_unnamed_channel_of_ids_is_guessed_an_image_but_opens_as_a_mask(
        tmp_path, windows_read):
    channels = _stains(STAINS)
    channels.insert(2, (ims.UNNAMED, _cells()))
    path = write_ims(tmp_path / "panel.ims", channels)
    read, _judged = windows_read

    # The guess reads names only here, and no name says mask.
    assert ims.mask_verdict(path) is False
    assert read == []

    # Said to be a mask (the import's Image/Mask switch): every channel is
    # read, and the one of ids is found.
    labels = ims.ImsLabels(path)
    try:
        assert labels.channel == 2
        assert sorted(set(read)) == list(range(len(channels)))
        assert np.array_equal(np.asarray(labels.plane(0, 1)), _cells()[1])
    finally:
        labels.close()


def test_label_channel_reads_every_channel_of_a_wide_file(tmp_path, windows_read):
    channels = _stains(STAINS)
    channels.append(("", _cells()))
    path = write_ims(tmp_path / "panel.ims", channels)
    read, _judged = windows_read
    handle, stream = ims._open_file(path)
    try:
        assert ims.label_channel(handle, path, stream) == len(channels) - 1
    finally:
        ims._close_file(handle, stream)
    assert sorted(read) == list(range(len(channels)))


def test_a_wide_file_whose_own_name_says_mask_reads_every_channel(tmp_path, windows_read):
    channels = _stains(STAINS)
    channels.insert(4, (ims.UNNAMED, _cells()))
    path = write_ims(tmp_path / "cells_mask.ims", channels)
    read, judged = windows_read

    # The file's name says mask, and one channel holds ids: a mask, as before.
    assert ims.mask_verdict(path) is True
    assert sorted(read) == list(range(len(channels)))
    assert len(judged) == len(channels)


def test_a_wide_file_named_mask_of_stains_only_is_still_an_image(tmp_path, windows_read):
    path = write_ims(tmp_path / "cells_mask.ims", _stains(STAINS))
    read, _judged = windows_read

    assert ims.mask_verdict(path) is False
    assert sorted(read) == list(range(len(STAINS)))


# -- GUESS_CHANNELS or fewer: as before -------------------------------------------

def test_at_guess_channels_every_channel_is_read_and_an_unnamed_mask_is_asked(
        tmp_path, windows_read):
    channels = _stains(STAINS[:ims.GUESS_CHANNELS - 1]) + [(ims.UNNAMED, _cells())]
    assert len(channels) == ims.GUESS_CHANNELS
    path = write_ims(tmp_path / "panel.ims", channels)
    read, _judged = windows_read

    # Stains with one channel of ids beside them, named by nothing: asked.
    assert ims.mask_verdict(path) is None
    assert sorted(read) == list(range(len(channels)))


def test_at_guess_channels_named_stains_are_read_and_are_an_image(tmp_path, windows_read):
    path = write_ims(tmp_path / "panel.ims", _stains(STAINS[:ims.GUESS_CHANNELS]))
    read, _judged = windows_read

    assert ims.mask_verdict(path) is False
    assert sorted(read) == list(range(ims.GUESS_CHANNELS))


def test_one_channel_of_ids_is_a_mask(tmp_path, windows_read):
    path = write_ims(tmp_path / "panel.ims", [(ims.UNNAMED, _cells())])
    read, _judged = windows_read

    assert ims.mask_verdict(path) is True
    assert read == [0]
