"""Starting a gating session on a dataset reads each image's cell table once.

`gating_session_start` is a plain (non-job) call: it picks the reference
image, the markers, the channel match, the QC blocks and the units before it
returns. Each of those walks every image through `AgentSession.data`, which
holds only a few tables (`table_limit`); when a dataset has more images than
that, each walk in the same order evicts the table the next walk asks for
first, and every image's whole table is read from disk again on every pass.
What is pinned here is the count: the call itself reads an image's table at
most twice, however many images the dataset has.
"""

import threading
from collections import Counter

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.autogate_fixtures import make_gating_dataset

#: The gating session is a Pro (AI) capability.
pytestmark = pytest.mark.pro


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def test_a_dataset_start_reads_each_images_table_at_most_twice(tmp_path, monkeypatch):
    from plexora.server.providers.local import LocalTableProvider

    made = make_gating_dataset(tmp_path, name="sixfold", shifts=(0.0,) * 6, grid=8,
                               size=256)
    caller = threading.get_ident()
    loads = Counter()
    real_load = LocalTableProvider.load

    def counted(self, *args, **kwargs):
        # Only the start call's own reads: the bulk pass it queues runs as a
        # job on another thread and is allowed to read every table again.
        if threading.get_ident() == caller:
            loads[self._name] += 1
        return real_load(self, *args, **kwargs)

    monkeypatch.setattr(LocalTableProvider, "load", counted)
    session = AgentSession()
    result = invoke(session, "gating_session_start", {"engine": "v1", "scope": "dataset",
                                                      "dataset": "sixfold"})
    monkeypatch.setattr(LocalTableProvider, "load", real_load)
    jobs.drain(180)
    assert result["ok"], result.get("error")
    assert len(result["result"]["images"]) == len(made) == 6
    total = sum(loads.values())
    assert total <= 2 * len(made), (
        f"the start call read {total} whole tables for {len(made)} images: {dict(loads)}")
