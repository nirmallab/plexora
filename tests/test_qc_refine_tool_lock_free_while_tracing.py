"""`refine_qc_roi(all: true)` leaves the project's QC lock free while a
region's outline is being traced.

Tracing reads pixels and, for a physical artifact, runs magic select's model:
seconds a region on a CPU, minutes for a result with tens of regions. Every
other QC writer (drawing a region and refreshing, approve, strictness, the
write_*_regions tools, a gating run's re-derive in another process) takes the
same lock, so the trace itself must run outside it; only reading the targets
and applying each finished trace need the lock.
"""

import json
import threading

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.qc_fixtures import make_qc_project

pytestmark = pytest.mark.pro

PROJECT = "qcrefinelockfree"


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _box(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1],
                                                [x0, y0]]]}


def _another_writer_gets_the_lock(results):
    """What a second QC writer on another thread sees: can it take the lock?"""
    got = {}

    def writer():
        held = results.lock(PROJECT).acquire(timeout=3)
        got["acquired"] = held
        if held:
            results.lock(PROJECT).release()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    thread.join(10)
    return got.get("acquired", False)


def test_other_qc_writers_are_not_blocked_while_every_region_is_traced(tmp_path, monkeypatch):
    from plexora.plugins.qc.server import refine_sam, results

    info = make_qc_project(tmp_path, name=PROJECT, artifacts=("fold",))
    session = AgentSession()
    region = next(r for r in info["truth"]["regions"] if r["name"] == "fold")
    ys, xs = np.nonzero(region["mask"])
    cx, cy = int(np.median(xs)), int(np.median(ys))
    for dx in (-60, 60):
        ok(invoke(session, "create_roi", {"project": PROJECT, "category": "QC: Tissue fold",
                                          "geometry": _box(cx + dx - 90, cy - 90,
                                                           cx + dx + 90, cy + 90)}))
    ok(invoke(session, "refresh_qc", {"project": PROJECT}))
    ok(invoke(session, "profile_image_qc", {"project": PROJECT}))
    jobs.drain(180)

    seen = []
    real_trace = refine_sam.trace

    def tracing(*args, **kwargs):
        # The model would be running here: is the QC lock free meanwhile?
        seen.append(_another_writer_gets_the_lock(results))
        return real_trace(*args, **kwargs)

    monkeypatch.setattr(refine_sam, "trace", tracing)
    traced = ok(invoke(session, "refine_qc_roi", {"project": PROJECT, "all": True}))
    assert len(seen) == 2, traced
    assert all(seen), (
        f"another QC writer could not take the QC lock while a region was traced "
        f"(per region: {seen}): refine_qc_roi holds it through every region's inference")
    assert traced["refined"], traced
