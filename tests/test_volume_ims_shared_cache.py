"""Audit io-volume-7: two open `ImsVolumeSource`s of one file share chunk-cache
entries (one `cache_id`); closing a short-lived one (a project_scale lookup,
an agent call) must not drop the decoded chunks the viewer's is still using."""

import numpy as np

from plexora.server.volume import chunk_cache
from plexora.server.volume.sources.ims import open_ims_volume
from tests import volume_fixtures as vf


def test_closing_one_source_keeps_the_others_chunks(tmp_path):
    path = tmp_path / "v.ims"
    vf.write_ims(path)
    viewer = open_ims_volume(path)
    brief = open_ims_volume(path)
    try:
        assert viewer.cache_id == brief.cache_id
        key = (viewer.cache_id, 0, 0, 0, 0, 0, 0)
        chunk_cache.CACHE.put(key, np.zeros(4, np.uint16))
        brief.close()
        assert chunk_cache.CACHE.get(key) is not None
        brief.close()                                  # twice is still one close
        assert chunk_cache.CACHE.get(key) is not None
    finally:
        viewer.close()
    assert chunk_cache.CACHE.get(key) is None          # the last one out drops them
