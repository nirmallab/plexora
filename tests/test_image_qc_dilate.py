"""`image_qc.dilate`: scipy's `binary_dilation` (default cross, nothing beyond
the edge) in numpy, so the tissue mask of the display calibration needs no
scipy -- on the website a stage loaded on first use, 1.1 s of a project's
opening. Held to scipy pixel for pixel."""

import numpy as np
import pytest


@pytest.mark.parametrize("iterations", [1, 2, 3])
def test_dilate_is_scipys_binary_dilation(iterations):
    from scipy.ndimage import binary_dilation

    from plexora.agent.evidence.image_qc import dilate

    rng = np.random.default_rng(iterations)
    masks = [rng.random((h, w)) < p for (h, w) in [(1, 1), (1, 7), (7, 1), (5, 5), (64, 48), (257, 129)]
             for p in (0.0, 0.01, 0.3, 1.0)]
    edge = np.zeros((9, 9), dtype=bool)
    edge[0, 0] = edge[8, 4] = edge[4, 8] = True
    for mask in masks + [edge]:
        assert np.array_equal(dilate(mask, iterations), binary_dilation(mask, iterations=iterations))


def test_the_tissue_mask_is_the_one_scipy_made():
    from scipy.ndimage import binary_dilation

    from plexora.agent.evidence import image_qc

    rng = np.random.default_rng(0)
    nuclear = rng.gamma(2.0, 50.0, (300, 200))
    nuclear[100:180, 40:150] += 3000
    logged = np.log1p(np.maximum(nuclear, 0))
    expected = binary_dilation(logged > image_qc.otsu_level(logged), iterations=2)
    assert np.array_equal(image_qc.tissue_mask(nuclear), expected)


def test_the_overviews_read_in_one_go_are_the_ones_read_one_at_a_time():
    """`read_overviews` (calibration's planes, one question to the page on
    the website) against `read_overview`, over a reader with `read_many`."""
    from plexora.agent.evidence import image_qc

    rng = np.random.default_rng(1)
    stack = rng.integers(0, 4000, (3, 40, 50), dtype=np.uint16)

    class Reader:
        levels = 1
        batches = []

        def channel_index(self, key):
            return {"a": 0, "b": 1, "c": 2}.get(key)

        def level_shape(self, level):
            return stack.shape[1:]

        def read(self, index, level, box):
            x0, y0, x1, y1 = box
            return stack[index, y0:y1, x0:x1], box

        def read_many(self, jobs):
            self.batches.append(len(jobs))
            return [self.read(*job) for job in jobs]

    reader = Reader()
    together = image_qc.read_overviews(reader, ["a", "c", "a"], 0)
    assert reader.batches == [2] and list(together) == ["a", "c"]
    for key in ("a", "c"):
        plane, level, ceiling = together[key]
        alone = image_qc.read_overview(reader, key, 0)
        assert np.array_equal(plane, alone[0]) and (level, ceiling) == alone[1:]
