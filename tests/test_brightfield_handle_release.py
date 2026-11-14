"""A brightfield slide's file is closed by whoever opened it.

`brightfield.open_rgb` keeps the slide's open file on the pyramid because
every level reads lazily through it. The readers that open one for a single
job -- an import, a node's "how big is it", a Figure Builder or agent-render
panel -- have to close it when the job is done, as `SourceImage.close` says
it does: "released here rather than left to the garbage collector -- on
Windows a held handle keeps the file from being deleted". tifffile's pages
refer back to their file, so a dropped pyramid's handle survives until a
cyclic collection that may not come for a long time.

Each test records the handles `open_rgb` opened and checks they are closed
once the caller is done, with the cyclic collector off so it cannot tidy up
behind the caller.
"""

import gc
import os

import pytest

from plexora.datasource import register_image_datasource
from plexora.server.models import data_model
from plexora.server.providers import local
from plexora.server.utils import brightfield
from tests.brightfield_fixtures import write_rgb_ome_tiff


@pytest.fixture
def opened_handles(monkeypatch):
    """Every file handle `brightfield.open_rgb` opens during the test."""
    handles = []
    real = brightfield.open_rgb

    def recording(path, extension=None):
        pyramid = real(path, extension=extension)
        handles.append(pyramid._handle)
        return pyramid

    monkeypatch.setattr(brightfield, "open_rgb", recording)
    return handles


@pytest.fixture
def no_cyclic_gc():
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()
        gc.collect()


def _still_open(handles):
    return [handle for handle in handles
            if handle is not None and not handle.filehandle.closed]


def test_importing_a_slide_closes_the_file_it_read(tmp_path, opened_handles,
                                                   no_cyclic_gc):
    """`convertOmeTiff`'s brightfield branch reads the slide's geometry and
    lets go of it: the file can be deleted or replaced straight after."""
    path = write_rgb_ome_tiff(tmp_path / "import.ome.tif", height=512, width=640)

    info = data_model._convert_brightfield_image(str(path))

    assert info["width"] == 640 and info["height"] == 512
    assert opened_handles, "the import did not read the slide through open_rgb"
    assert _still_open(opened_handles) == []
    os.remove(path)                    # WinError 32 while a handle is held


def test_a_node_asking_a_slides_size_closes_the_file(tmp_path, opened_handles,
                                                     no_cyclic_gc):
    """`image_geometry` answers "how big is it" for a node from a one-shot
    open; the open must not outlive the answer."""
    path = write_rgb_ome_tiff(tmp_path / "size.ome.tif", height=512, width=640)

    geometry = local.image_geometry(str(path), rgb=True)

    assert geometry
    assert opened_handles, "the size was not read through open_rgb"
    assert _still_open(opened_handles) == []
    os.remove(path)


def test_a_figure_reader_of_a_slide_closes_the_file(tmp_path, opened_handles):
    """A Figure Builder / agent-render reader of an H&E project reads a panel
    and closes: what it opened is closed with it, as `SourceImage.close`
    documents for every pyramid it opens."""
    from plexora.plugins.figure_builder.server import render

    image = write_rgb_ome_tiff(tmp_path / "figure.ome.tif", height=512, width=640)
    register_image_datasource("he_handle_release", image)
    gc.collect()
    del opened_handles[:]              # the import's own opens are the test above

    gc.disable()
    try:
        with render.SourceImage("he_handle_release") as source:
            assert source.is_brightfield
            picture, _report = render.render_panel(
                source,
                {"viewport": {"x": 0, "y": 0, "w": 640, "h": 512}, "channels": []},
                160, 128)
        assert picture.size == (160, 128)
        assert opened_handles, "the reader did not open the slide through open_rgb"
        assert _still_open(opened_handles) == []
    finally:
        gc.enable()
        gc.collect()
