"""Suite-wide fixtures.

At the repository root rather than under tests/, because `testpaths` spans two
trees -- tests/ and plexora/plugins/*/tests/ -- and both load datasources.
"""

import pytest

from plexora.server.models import data_model


@pytest.fixture(autouse=True)
def _no_background_cache_warmup(monkeypatch):
    """Stop load_datasource's cache-warming thread for the duration of a test.

    load_datasource() ends by spawning a DAEMON thread that walks every channel
    calling get_image_channel_stats and get_channel_gmm. In the app that is the
    point: it moves ~17 s of GaussianMixture fitting off the first request.

    Under pytest it is a race. The thread outlives the test that started it,
    and every function it calls goes through _ensure_loaded(), which can
    reassign data_model.config wholesale and bump load_generation. By the time
    it gets there the fixture that pointed config_json_path at a tmp_path has
    already torn down, so it reloads against whatever the *next* test set up
    and overwrites that test's config from underneath it. The symptom is a
    KeyError in some unrelated file, and which file depends on wall-clock
    timing -- so it moves whenever anything is added to the suite. It was
    reached by adding tests/test_channel_overview.py and tests/test_mini_map.py,
    neither of which goes anywhere near segmentation, and it landed in
    tests/test_segmentation_mapping.py.

    Nothing under test asserts that warming happens, and disabling it changes
    no result: every value it precomputes is computed on demand anyway by the
    same functions, just later. It only makes them slower, which in a test is
    free.
    """
    monkeypatch.setattr(data_model, "_warm_datasource_caches", lambda *args, **kwargs: None)
