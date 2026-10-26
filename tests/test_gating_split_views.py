"""The split looks (`SessionOptions.split_views`): a recheck of a marker that is
not dim draws each cell as DNA | marker | combined, the marker from the
display's black to the gate at mid-grey.

The 2026-10-20 A/B (lsp11385 + p193, one run per arm) found the split look
helped the rechecks of markers with a clear separation and hurt dim markers,
whose background then draws grey everywhere; this option is that finding,
off until it is confirmed on other images (~/gatevision/splitab).
"""

from types import SimpleNamespace

import pytest

from plexora.agent import AgentSession, cell_exclusions, registry
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_tiles import _start


@pytest.fixture(autouse=True)
def _plugins():
    registry.discover(["gating"])
    cell_exclusions._reset_for_tests()
    yield
    cell_exclusions._reset_for_tests()


def test_the_split_collage_draws_dna_marker_and_both_and_redraws_byte_for_byte(tmp_path):
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import cells as cellmod

    make_gating_project(tmp_path, grid=16, size=640)
    session = AgentSession()
    try:
        ds = session.data("gsynth")
        c = cellmod.cells(ds)
        picked = [{"cell_id": int(c.ids[i]), "x": float(c.xs[i]), "y": float(c.ys[i]),
                   "value": 1.0} for i in range(0, 60, 5)]
        rows = [{"label": "a", "key": "below", "cells": picked[:6]},
                {"label": "b", "key": "above", "cells": picked[6:]}]
        out = collage.render_collage(session, ds, layout="split", rows=rows, marker="CD8",
                                     gate=1.5, fmt="png", store=False)
        manifest = out["manifest"]
        assert manifest["panels"] == ["nuclear", "marker_gate", "merge"]
        assert tuple(manifest["size"]) == collage.layout_size("split", rows=2)
        assert manifest["size"][0] <= collage.LAYOUTS["split"]["max_width"]
        assert collage.rerender(session, ds, manifest)["png"] == out["png"]
    finally:
        session.close()


def _split(mode):
    from plexora.plugins.gating.server.autogate.engine import Engine

    fake = SimpleNamespace(options={"split_views": mode})
    return lambda unit, look: Engine.split_on(fake, unit, look)


def test_only_a_bright_markers_recheck_is_split():
    split = _split("bright_rechecks")
    bright = {"class": "weakly_bimodal", "flags": []}
    assert split(bright, "t4")
    assert not split(bright, "t2")                                   # a first look
    assert not split(bright, "t3")
    assert not split({"class": "continuous", "flags": []}, "t4")     # no valley: dim
    assert not split({"class": "long_tail", "flags": []}, "t4")
    assert not split({"class": "bimodal", "flags": ["weak_signal"]}, "t4")
    assert not _split("off")(bright, "t4")


def test_split_views_default_off():
    from plexora.plugins.gating import capabilities_session as cs

    assert cs.SessionOptions().split_views == "off"
    assert cs.option_defaults()["split_views"] == "off"


@pytest.mark.pro
def test_a_session_splits_its_bright_markers_rechecks_only(tmp_path):
    from plexora.agent import artifacts
    from plexora.plugins.gating.server.autogate import engine as engines
    from tests.test_gating_session import Oracle, drive

    info = make_gating_project(tmp_path, grid=24, size=960, markers=("CD3", "CD8", "CD4"))
    session = AgentSession()
    try:
        started = _start(session, "gsynth", mode="propose", agent="split-bright",
                         split_views="bright_rechecks", reuse_answers=False)
        seen = drive(session, started["session_id"], Oracle(info))
        record = engines.store().load(started["session_id"])
        dim = {u["marker"] for u in record["units"].values()
               if "weak_signal" in (u.get("flags") or ())
               or u.get("class") in ("continuous", "long_tail")}
        looks = [p for p in seen if p["kind"] in ("t2_confirm", "t4_candidates")]
        assert any(p["kind"] == "t4_candidates" and p["evidence"]["marker"] not in dim
                   for p in looks)
        for packet in looks:
            guide = packet["evidence"].get("guide") or []
            layout = artifacts.get(packet["images"][0]["artifact_id"])[1]["manifest"]["layout"]
            split = packet["kind"] == "t4_candidates" \
                and packet["evidence"]["marker"] not in dim
            assert (layout == "split") == split
            assert ("collage_split" in guide) == split
    finally:
        session.close()
