"""A large Olympus OIB/OIF is loaded into a temporary memory-mapped file, as
`base.WholeArrayReader` promises for every whole-array reader: one over
`MEMORY_BYTES` is asked of the package with `out="memmap"` (OIR's reader does
this already; `oiffile`'s `OifFile.asarray(series=0, **kwargs)` hands `out` on
to tifffile's `TiffSequence.asarray`, which takes it).

The fake `oiffile` here takes keyword arguments the way the real one does and
records them, so the test sees what the reader asked for.
"""

from types import SimpleNamespace

import numpy as np

from plexora.server.utils import formats
from plexora.server.utils.formats import base
from tests.formats_fakes import coded, install, touch

H, W = 8, 12


def _install_oiffile(monkeypatch, *, axes, shape):
    array = coded(shape, np.uint16)
    state = SimpleNamespace(loads=[])

    class OifFile:
        def __init__(self, path):
            self.path = str(path)
            self.axes, self.shape, self.dtype = axes, tuple(shape), array.dtype
            self.mainfile = {"Channel 1 Parameters": {"DyeName": "A"},
                             "Channel 2 Parameters": {"DyeName": "B"}}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def asarray(self, series=0, **kwargs):
            state.loads.append(dict(kwargs))
            return array.copy()

    module = install(monkeypatch, "oiffile", OifFile=OifFile)
    module.state, module.array = state, array
    return module


def test_oib_a_large_image_is_loaded_into_a_memory_file(monkeypatch, tmp_path):
    module = _install_oiffile(monkeypatch, axes="ZCYX", shape=(3, 2, H, W))
    path = touch(tmp_path, "big.oib")
    monkeypatch.setattr(base.WholeArrayReader, "MEMORY_BYTES", 10)
    reader = formats.open_reader(path)
    assert reader._nbytes() > base.WholeArrayReader.MEMORY_BYTES
    np.testing.assert_array_equal(reader.read(0, 1, 0, 4, 0, 4, z=2), module.array[2, 1, 0:4, 0:4])
    assert [load.get("out") for load in module.state.loads] == ["memmap"]


def test_oib_a_small_image_is_loaded_into_memory(monkeypatch, tmp_path):
    module = _install_oiffile(monkeypatch, axes="ZCYX", shape=(3, 2, H, W))
    path = touch(tmp_path, "small.oib")
    reader = formats.open_reader(path)
    reader.read(0, 0, 0, 4, 0, 4)
    assert [load.get("out") for load in module.state.loads] == [None]
