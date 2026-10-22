"""Replaying the candidates engine's blind cell panels (`cell_panel`).

A gating session is reproducible: the same scene, code and seed issue the
same packets, so a stored session can be asked again and its pictures drawn
again from their manifests (`plexora ai bench gating --replay-session <id>
--rerender`, plexora/ai/bench.py) -- which shows what a change to the
renderer, the calibration or the sampler moved before any model is asked
about a new picture. Pinned here for the panels:

- two sessions on one synthetic scene with one seed issue the same first
  panel: its question, its numbers, the cell behind each number, and the
  picture byte for byte;
- a stored panel drawn again from its manifest gives the stored bytes, and
  every number captioned in it is the cell code scored under that number.

The scene (`bench_data` `dense_spill_uncal`) is uncalibrated, so its
pictures are drawn at the session's estimated pixel size -- which the
manifest must carry for a redraw to match -- and its panels carry the
partner panels (layout `t2m`).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from plexora.agent import AgentSession, artifacts, jobs, registry

pytestmark = pytest.mark.paid

MARKERS = ("CD3", "CD8", "CD20", "CD4", "FOXP3")


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def _scene(tmp_path):
    from plexora.ai import bench_data

    return bench_data.register_spatial(tmp_path, "bench_panels", scenario="dense_spill_uncal",
                                       size=1024, seed=0, markers=MARKERS)


def _reader(made):
    from plexora.ai import bench

    return bench.TruthAgent(made["values"], made["truth"], "oracle", ids=made["ids"])


def _run(made) -> str:
    """One candidates session to its end in a fresh agent session, every
    panel read by the scene's own per-cell truth; the session's id."""
    from plexora.ai import bench

    session = AgentSession(table_limit=4)
    try:
        _units, _status, _seconds, sid = bench.run_session(
            session, made["name"], MARKERS, _reader(made), mode="propose",
            options={"engine": "v2", "seed": 0, "agent": "bench:candidates",
                     "reuse_answers": False})
    finally:
        session.close()
        jobs.drain(120)
    return sid


def _first_panel(sid):
    """(the stored packet, its pictures' bytes) of the session's first panel."""
    from plexora.plugins.gating.server.autogate import engine

    store = engine.store()
    first = next(e["packet_id"] for e in store.decisions(sid)
                 if e.get("event") == "issued" and e["kind"] == "cell_panel")
    return store.read_packet(sid, first)


def _sha(data) -> str:
    return hashlib.sha256(data).hexdigest()


def test_one_scene_and_one_seed_issue_the_same_first_panel(tmp_path):
    made = _scene(tmp_path)
    first_sid, second_sid = _run(made), _run(made)
    assert first_sid != second_sid
    (a, a_images), (b, b_images) = _first_panel(first_sid), _first_panel(second_sid)
    assert a["units"] == b["units"]
    assert a["question"] == b["question"]
    assert a["allowed"] == b["allowed"]
    # The cells behind the numbers, in the panel's order, with their values
    # and the interval (or sentinel) each was drawn for.
    assert a["_panel"] == b["_panel"]
    # Each session wrote its own copy of what the reader was sent.
    assert a_images and [(_sha(d), f) for d, f in a_images] == \
        [(_sha(d), f) for d, f in b_images]
    # The stored picture, keyed by its bytes and its manifest: one picture.
    assert a["images"][0]["artifact_id"] == b["images"][0]["artifact_id"]


def test_a_stored_panel_draws_again_byte_for_byte_from_its_manifest(tmp_path):
    from plexora.agent.evidence import collage
    from plexora.ai import bench

    made = _scene(tmp_path)
    sid = _run(made)
    session = AgentSession(table_limit=4)
    try:
        rows = bench.replay_session(sid, _reader(made), rerender=True, session=session)
        panels = [r for r in rows if r["kind"] == "cell_panel"]
        assert panels
        # The same truthful reader labels every cell as it did.
        assert all(r["agreement"] == 1.0 for r in panels), panels
        drawn = [i for r in panels for i in r["images"]]
        assert drawn and all(i["rerendered"] and i["identical"] and i["numbers_match"]
                             for i in drawn), drawn
        png, sidecar = artifacts.get(drawn[0]["artifact_id"])
        manifest = sidecar["manifest"]
        assert manifest["layout"] == "t2m" and manifest["request"]["partners"]
        # The manifest is served to any client (`get_artifact`): it says where
        # each cell is and its number, never its value or why it was picked.
        placed = [c for row in manifest["rows"] for c in row["cells"]]
        assert placed and all(c.get("value") is None and "why" not in c for c in placed)
        assert "clear negative" not in json.dumps(manifest)
        # Drawn at the session's estimate, not the image's (absent)
        # calibration: without it in the manifest the redraw differs.
        assert manifest["request"]["pixel"]["source"] == "estimated"
        bare = {**manifest, "request": {k: v for k, v in manifest["request"].items()
                                        if k != "pixel"}}
        assert collage.rerender(session, session.data(made["name"]), bare)["png"] != png
    finally:
        session.close()
    text = bench.replay_markdown(rows, title="replay")
    assert f"| cell_panel | {len(panels)} | {len(panels)} | 1.000 | {len(drawn)} | " \
           f"{len(drawn)} |" in text
    assert f"numbers are the cells code scored: {len(drawn)} of {len(drawn)}" in text
