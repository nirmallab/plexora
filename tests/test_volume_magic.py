"""Magic select on a z-stack outlines the plane on screen.

Before, it read the stack's default (middle) plane whatever the person was
looking at, and reused that embedding after they moved to another plane. The
model itself is not under test: `sam.embed` is replaced by a recorder of the
pixels it was handed.
"""

import numpy as np
import pytest

from plexora.agent import AgentSession
from plexora.datasource import register_image_datasource
from plexora.vision import sam
from plexora.vision import segment as segmenter
from tests import volume_fixtures as vf


@pytest.fixture
def seen(monkeypatch):
    pictures = []

    def embed(rgb):
        pictures.append(np.array(rgb, copy=True))
        return f"embedding {len(pictures)}"

    monkeypatch.setattr(sam, "embed", embed)
    segmenter.forget()
    yield pictures
    segmenter.forget()


def _embed(session, image_view):
    record = session.project("vol")
    size = (int(record.image.width), int(record.image.height))
    return segmenter._embedding_for(
        session, "vol", record, (0, 0, size[0], size[1]), np.array([[40.0, 40.0]]),
        [{"name": "CD8", "color": "#ffffff", "window": [0, 4000]}], {}, size,
        image_view=image_view)


def test_each_plane_is_embedded_from_its_own_pixels(tmp_path, seen):
    vf.write_ome_volume(tmp_path / "vol.ome.tif", axes="CZYX", channels=3, depth=8,
                        height=128, width=112, names=["DNA", "CD3", "CD8"])
    register_image_datasource("vol", tmp_path / "vol.ome.tif")
    session = AgentSession()

    first = _embed(session, "z0")
    last = _embed(session, "z7")
    assert first.key != last.key
    assert len(seen) == 2 and not np.array_equal(seen[0], seen[1])
    # The same plane again is the cached embedding, not a second read.
    assert _embed(session, "z0") is first and len(seen) == 2
    # No view at all is the image as it always was: the default plane.
    default = _embed(session, None)
    assert default.key not in (first.key, last.key)


def test_a_plane_the_stack_does_not_have_is_refused(tmp_path, seen):
    vf.write_ome_volume(tmp_path / "vol.ome.tif", axes="CZYX", channels=3, depth=8,
                        height=128, width=112, names=["DNA", "CD3", "CD8"])
    register_image_datasource("vol", tmp_path / "vol.ome.tif")
    with pytest.raises(segmenter.SegmentError) as raised:
        _embed(AgentSession(), "z99")
    assert raised.value.code == "invalid_input"
