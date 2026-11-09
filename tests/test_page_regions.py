"""On the website, the rectangles a piece of work reads off a picked TIFF are
read by the page (`source_image.SourceImage.read_many` ->
`providers.browser.page_regions` -> services/browserNode.js `regionsOf`).

Auto-contrast's cell crops read hundreds of small windows, each decoding the
compressed tiles under it: on the desktop four at a time natively, on the
website one at a time in WebAssembly (1.5-2.4 s a channel on LSP11385). The
page's workers read them side by side with the browser's own zlib
(tests/test_tiff_writer.py holds their pixels to numpy). Held here: the same
`(array, clipped)` as `read` for every rectangle, clipped as `read` clips,
one question for all of them, Python reading whatever the page declines, and
nothing changed off the website.
"""

import numpy as np
import pytest
import tifffile

from plexora import _work, datasource
from plexora.server.providers import browser as page
from plexora.server.utils import source_image
from tests.helpers import use_data_root


def project_data(name):
    from plexora.api.dataset import project_data as data

    return data(name)

NAME = "regions"


@pytest.fixture
def image(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    rng = np.random.default_rng(5)
    planes = rng.integers(0, 4000, size=(3, 300, 260), dtype=np.uint16)
    path = tmp_path / f"{NAME}.tif"
    tifffile.imwrite(path, planes, tile=(64, 64), compression="zlib", photometric="minisblack")
    datasource.register_image_datasource(name=NAME, image=path, data_dir=data_dir)
    return path, planes


class Page:
    """The page's workers, played by tifffile over the same file."""

    def __init__(self, monkeypatch, path, declines=()):
        self.asked = []
        self.declines = set(declines)
        self.path = path
        monkeypatch.setattr(page, "is_page_file", lambda p: str(p) == str(path))
        monkeypatch.setattr(page, "page_regions", self)

    def __call__(self, path, levels, dtype, reads):
        self.asked.append({"path": path, "levels": levels, "dtype": str(dtype), "reads": list(reads)})
        stack = tifffile.imread(self.path)
        return [None if i in self.declines else stack[c, y0:y1, x0:x1]
                for i, (c, _level, (x0, y0, x1, y1)) in enumerate(reads)]


JOBS = [(0, 0, (5, 7, 40, 33)), (2, 0, (250, 290, 300, 330)), (1, 0, (-10, -4, 20, 12)),
        (1, 0, (400, 400, 420, 420)), (2, 0, (60.4, 61.6, 129.2, 130.9))]


def test_every_rectangle_is_reads_own_in_one_question(image, monkeypatch):
    path, planes = image
    seen = Page(monkeypatch, path)
    with source_image.SHELF.reader(project_data(NAME)) as reader:
        many = reader.read_many(JOBS)
        one_by_one = [reader.read(*job) for job in JOBS]

    assert len(seen.asked) == 1
    asked = seen.asked[0]
    assert asked["dtype"] == "uint16" and asked["levels"] == [(3, 300, 260)]
    # Clipped as `read` clips; the rectangle off the image is `read`'s own.
    assert [box for _c, _l, box in asked["reads"]] == [(5, 7, 40, 33), (250, 290, 260, 300), (0, 0, 20, 12),
                                                       (60, 61, 130, 131)]
    for (pixels, clipped), (expected, clipped_here) in zip(many, one_by_one):
        assert clipped == clipped_here and np.array_equal(pixels, expected)


def test_what_the_page_declines_python_reads(image, monkeypatch):
    path, _planes = image
    Page(monkeypatch, path, declines={1})
    with source_image.SHELF.reader(project_data(NAME)) as reader:
        many = reader.read_many(JOBS[:3])
        one_by_one = [reader.read(*job) for job in JOBS[:3]]
    for (pixels, clipped), (expected, clipped_here) in zip(many, one_by_one):
        assert clipped == clipped_here and np.array_equal(pixels, expected)


def test_off_the_website_nothing_is_asked(image, monkeypatch):
    path, _planes = image
    seen = Page(monkeypatch, path)
    monkeypatch.setattr(page, "is_page_file", lambda p: False)
    with source_image.SHELF.reader(project_data(NAME)) as reader:
        reader.read_many(JOBS)
    assert seen.asked == []


def test_the_cell_crops_ask_the_page_on_the_website_only(image, monkeypatch):
    from plexora.agent.evidence import crops

    path, _planes = image
    asked = []
    real = source_image.SourceImage.read_many
    monkeypatch.setattr(source_image.SourceImage, "read_many",
                        lambda self, jobs: asked.append(len(jobs)) or real(self, jobs))
    reads = [((0, 0, 32, 32), [], "a", 0), ((40, 40, 72, 72), [], "a", 1)]

    def run():
        planes = None
        if not _work.threaded() and len(reads) > 1:
            with source_image.SHELF.reader(project_data(NAME)) as reader:
                planes = reader.read_many([(index, 0, block) for block, _o, _n, index in reads])
        return planes

    # The branch crops._read_plane_crops takes, by its own condition.
    assert "read_many" in crops._read_plane_crops.__code__.co_names
    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    assert run() is not None and asked == [2]
    monkeypatch.setenv("PLEXORA_WORK", "threads")
    assert run() is None and asked == [2]


def test_the_pages_answer_becomes_arrays(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    block = np.arange(12, dtype=np.uint16).reshape(3, 4)
    questions = []

    def asker(kind, args):
        questions.append((kind, args))
        return {"regions": [{"values": memoryview(block.tobytes()), "width": 4, "height": 3},
                            {"refused": "a big-endian file with a predictor"}]}

    monkeypatch.setattr(page, "_asker", asker)
    got = page.page_regions("/picked/p/a.ome.tif", [(2, 10, 10)], np.dtype("uint16"),
                            [(0, 0, (1, 2, 5, 5)), (1, 0, (0, 0, 2, 2))])
    assert questions[0][0] == "regions"
    assert questions[0][1]["reads"] == [{"channel": 0, "level": 0, "box": [1, 2, 5, 5]},
                                        {"channel": 1, "level": 0, "box": [0, 0, 2, 2]}]
    assert np.array_equal(got[0], block) and got[1] is None
    # Not a page file, or not the website: Python's own.
    assert page.page_regions("/Users/x/a.tif", [(2, 10, 10)], "uint16", [(0, 0, (0, 0, 1, 1))]) is None
    monkeypatch.setenv("PLEXORA_BACKEND", "server")
    assert page.page_regions("/picked/p/a.ome.tif", [(2, 10, 10)], "uint16", [(0, 0, (0, 0, 1, 1))]) is None


def test_a_masks_rectangles_are_read_padded_as_read_region_pads(tmp_path, monkeypatch):
    """The labels under the crops (`LocalSegmentationProvider.read_regions`):
    the page's for a picked TIFF mask, padded with 0 off the mask as
    `read_region` pads; Python's for whatever the page does not answer."""
    from plexora.server.providers.local import LocalSegmentationProvider

    labels = np.arange(1, 90 * 70 + 1, dtype=np.uint32).reshape(90, 70)
    path = tmp_path / "mask.tif"
    tifffile.imwrite(path, labels, tile=(32, 32), compression="zlib")
    provider = LocalSegmentationProvider(str(path))
    boxes = [(5, 6, 30, 40), (60, 80, 100, 120), (-8, -3, 10, 12), (200, 200, 210, 210)]
    asked = []

    def page_regions(p, levels, dtype, reads):
        asked.append((levels, str(dtype), list(reads)))
        return [None if i == 1 else labels[y0:y1, x0:x1] for i, (_c, _l, (x0, y0, x1, y1)) in enumerate(reads)]

    monkeypatch.setattr(page, "is_page_file", lambda p: str(p) == str(path))
    monkeypatch.setattr(page, "page_regions", page_regions)
    got = provider.read_regions(0, boxes)

    assert asked[0][0] == [(1, 90, 70)] and asked[0][1] == "uint32"
    assert [box for _c, _l, box in asked[0][2]] == [(5, 6, 30, 40), (60, 80, 70, 90), (0, 0, 10, 12)]
    for box, block in zip(boxes, got):
        expected = provider.read_region(0, box)
        assert block.dtype == expected.dtype and np.array_equal(block, expected), box
