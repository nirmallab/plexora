"""A project's warm-up never loads that project back over the one opened since.

Every load starts `_warm_datasource_caches` on a thread of its own: the
description, then each channel's stats and contrast fit, a step at a time, for
seconds on a local disk and minutes from a file share. A user who opens
project A and then project B before A's warm-up is done has left A. Each of the
warm-up's remaining steps used to call `ensure_loaded(A)`, which loaded A again
over B -- the image reopened, every cache cleared, `load_generation` bumped --
and B's next request loaded B again, while B's own warm-up did the same to A.

The website's warm-up already stops once its project is no longer the loaded
one (`_warm_on_page`). The desktop's, here on threads, must too.
"""

import numpy as np
import pytest
import tifffile

from plexora import datasource
from plexora.server.models import data_model
from tests.helpers import use_data_root

#: conftest.py stubs the warm-up for every test (its thread outliving the test
#: that started it); this test is about it, so it holds its own reference.
_warm_up = data_model._warm_datasource_caches

LEFT = "warm_left"
OPENED = "warm_opened"


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    use_data_root(monkeypatch, data_dir)
    rng = np.random.default_rng(5)
    for name, count in ((LEFT, 4), (OPENED, 2)):
        image = tmp_path / f"{name}.tif"
        tifffile.imwrite(image, rng.gamma(2.0, 150.0, size=(count, 96, 80)).astype(np.uint16),
                         photometric="minisblack")
        datasource.register_image_datasource(name=name, image=image, data_dir=data_dir)
    # Every channel shown, so the warm-up has a step for each.
    monkeypatch.setattr(data_model, "get_saved_channel_list", lambda ds: [
        {"channel": c["fullname"], "channel_active": True}
        for c in data_model.config[ds]["imageData"]])
    data_model.load_datasource(LEFT)
    yield


def test_the_warm_up_of_a_project_left_does_not_load_it_again(two_projects, monkeypatch):
    loads = []
    real_load = data_model.load_datasource

    def counting_load(name, *args, **kwargs):
        loads.append(name)
        return real_load(name, *args, **kwargs)

    monkeypatch.setattr(data_model, "load_datasource", counting_load)

    # The user opens the other project once the left one's warm-up has taken
    # its first step (the description), as a thread would have by then.
    real_description = data_model.get_datasource_description
    switched = []

    def description_then_switch(name, *args, **kwargs):
        result = real_description(name, *args, **kwargs)
        if name == LEFT and not switched:
            switched.append(True)
            data_model.load_datasource(OPENED)
        return result

    monkeypatch.setattr(data_model, "get_datasource_description", description_then_switch)

    _warm_up(LEFT)

    assert switched, "the warm-up never reached its description step"
    # The only load is the user's own; the rest of the left project's warm-up
    # stopped rather than loading it back.
    assert loads == [OPENED]
    assert data_model.is_loaded(OPENED)
