"""The Open Project card's thumbnail closes the image it read (audit lead
sc-dm2-5). `data_model._local_thumbnail_plane` opened the TIFF and left it to
a GC cycle to close; on Windows an open file cannot be moved or deleted, so a
project delete right after the Samples page drew its card left files behind."""

import numpy as np
import tifffile

from plexora.server.models import data_model
from plexora.server.utils import tiff_series


def test_the_thumbnail_read_closes_the_tiff(tmp_path, monkeypatch):
    path = tmp_path / "image.ome.tif"
    tifffile.imwrite(path, np.arange(2 * 256 * 256, dtype=np.uint16).reshape(2, 256, 256))
    opened = []
    real = tiff_series.ImageTiff

    class Recording(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

    monkeypatch.setattr(tiff_series, "ImageTiff", Recording)
    plane = data_model._local_thumbnail_plane(str(path))
    assert plane is not None and plane.shape == (256, 256)
    assert opened, "the TIFF reader was not the one used"
    assert all(tiff.filehandle.closed for tiff in opened)
