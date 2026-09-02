"""Staged progress for the segmentation job.

The complaint these exist for: the bar sat at 0% for minutes. Everything before
the tile loop -- deciding whether the supplied mask is servable as-is, adopting
an already-derived pyramid, probing shape and dtype, and above all the
full-plane read (60 s locally, 179 s streaming for the Orion mask) -- ran with
nothing reported, and users read that as a hang.
"""

import numpy as np
import pytest
import tifffile as tf

from plexora.server.models.data_model import (
    SEGMENTATION_STAGES,
    _staged_reporter,
    resolve_outline_segmentation,
)


def _collect():
    seen = []
    stage, report = _staged_reporter(
        SEGMENTATION_STAGES, lambda percent, key, message: seen.append((percent, key, message)))
    return seen, stage, report


def test_the_bands_are_ordered_contiguous_and_end_below_a_hundred():
    """A gap between bands is a bar that jumps; an overlap is a bar that goes
    backwards. 100 is reserved for the job actually finishing."""
    bands = list(SEGMENTATION_STAGES.values())
    for (_, end, _label), (start, _, _next_label) in zip(bands, bands[1:]):
        assert end == start
    assert bands[0][0] == 0
    assert bands[-1][1] < 100


def test_entering_a_stage_moves_the_bar_and_names_it():
    seen, stage, _report = _collect()

    stage("preparing")

    percent, key, message = seen[-1]
    assert key == "preparing"
    assert percent == SEGMENTATION_STAGES["preparing"][0]
    # The name is the whole point: this stage has no countable total, so the
    # words are the only thing distinguishing it from a hang.
    assert "Reading the mask" in message


def test_a_stage_maps_its_own_fraction_into_its_own_band():
    seen, stage, report = _collect()
    start, end, _ = SEGMENTATION_STAGES["building"]

    stage("building")
    report(1, 4)
    report(4, 4)

    percents = [percent for percent, key, _ in seen if key == "building"]
    assert percents[0] == start
    assert percents[-1] == end
    assert all(start <= percent <= end for percent in percents)


def test_progress_never_walks_backwards():
    """A stage entered late, or one reporting fewer units than the last tick,
    must not move the bar back -- that reads as the job restarting."""
    seen, stage, report = _collect()

    stage("building")
    report(3, 4)
    report(1, 4)          # a smaller fraction than the previous tick
    stage("inspecting")   # an earlier band, entered out of order

    percents = [percent for percent, _, _ in seen]
    assert percents == sorted(percents)


def test_only_real_changes_are_reported():
    """The tile loop calls back once per written tile -- thousands of times on a
    large pyramid -- and every one of those used to touch the job record."""
    seen, stage, report = _collect()

    stage("building")
    for _ in range(500):
        report(1, 1000)

    assert len(seen) == 1


def _write_mask(path, size=2048, cells=200):
    mask = np.zeros((size, size), dtype=np.uint32)
    for i in range(1, cells):
        y, x = divmod(i, 16)
        mask[y * 120:y * 120 + 100, x * 120:x * 120 + 100] = i
    tf.imwrite(path, mask)
    return path


def test_the_conversion_reports_every_stage_in_order(tmp_path):
    """End to end, through the real pyramid builder.

    The stage that matters is `preparing`: that is the full-plane read, and it
    is where the bar used to sit at zero with nothing said.
    """
    source = _write_mask(tmp_path / "mask.tiff")
    seen, stage, report = _collect()

    stage("loading")
    resolve_outline_segmentation(source, tmp_path / "derived",
                                 progress_callback=report, stage_callback=stage,
                                 mode="filled")

    order = []
    for _percent, key, _message in seen:
        if not order or order[-1] != key:
            order.append(key)
    assert order == ["loading", "inspecting", "preparing", "building", "writing"]

    # And nothing in the run is left at zero except the very first tick.
    during_read = [percent for percent, key, _ in seen if key == "preparing"]
    assert during_read and min(during_read) > 0


def test_a_conversion_with_no_stage_callback_still_works(tmp_path):
    """`stage_callback` is optional everywhere it is threaded -- the CLI's own
    pyramid call passes only `progress_callback`."""
    source = _write_mask(tmp_path / "plain.tiff", size=1024, cells=40)
    ticks = []

    resolve_outline_segmentation(source, tmp_path / "derived2",
                                 progress_callback=lambda done, total: ticks.append((done, total)),
                                 mode="filled")

    assert ticks and ticks[-1][0] == ticks[-1][1]
