"""A failed stain is a verdict on the channel, never a region: the final review
must not show it as one, and naming it there must not undo the verdict.

The scripted oracle of tests/test_qc_session.py answers every packet from the
scene's truth; here it answers the final review "inconsistent" and names every
region the review drew, as an agent unhappy with a region covering the whole
tissue would.
"""

import pytest

from plexora.agent import AgentSession, invoke
from tests.qc_fixtures import make_qc_project
from tests.test_qc_session import QCOracle, _qc, drive, ok, start  # noqa: F401 -- _qc is autouse

pytestmark = pytest.mark.pro


class _ObjectingOracle(QCOracle):
    """The oracle, except that the final review names every region it drew as
    over-excluded (once: a second review is answered consistent)."""

    def __init__(self, info):
        super().__init__(info)
        self.reviews = []

    def answer(self, packet, session_id):
        if packet["kind"] != "final_qc_review":
            return super().answer(packet, session_id)
        record = self._record(session_id)
        ref = packet["units"][0]
        unit = record["units"][f"{ref['project']}::{ref['type']}::{ref['id']}"]
        regions = list(unit.get("regions") or [])
        self.reviews.append(regions)
        if len(self.reviews) > 1 or not regions:
            return {"kind": "final_qc_review", "verdict": "consistent"}
        return {"kind": "final_qc_review", "verdict": "inconsistent",
                "concerns": [{"target": r["label"], "issue": "over_excluded",
                              "note": "this region covers far more than its artifact"}
                             for r in regions[:8]]}


def _channel_level_ids(session_id):
    from plexora.plugins.qc.server.engine import store

    return {u["id"] for u in store().load(session_id)["units"].values()
            if u["type"] == "candidate" and u.get("channel_level")}


def test_a_failed_channel_stays_failed_when_the_final_review_objects(tmp_path):
    """A failed stain decided on its audit row stays decided whatever the
    final review says: it is not drawn there as a whole-tissue region, and its
    marker stays unreliable in every cell."""
    from plexora.plugins.qc.server import results

    info = make_qc_project(tmp_path, artifacts=("empty_channel",))
    failed = [n for n, s in info["truth"]["channels"].items() if s == "failed"]
    assert failed
    session = AgentSession()
    sid = start(session)["session_id"]
    oracle = _ObjectingOracle(info)
    drive(session, sid, oracle)
    channel_level = _channel_level_ids(sid)
    assert channel_level, "the failed stain was not decided as a channel verdict"
    ok(invoke(session, "qc_session_finish", {"session_id": sid}))

    result = results.active(results.load("qcsynth"))
    verdicts = [c for c in (result.get("candidates") or {}).values()
                if c.get("channel_level")]
    assert verdicts and all(c.get("action") in ("exclude", "warn") for c in verdicts), \
        [(c.get("id"), c.get("class"), c.get("action")) for c in verdicts]
    channels = {c["name"]: c for c in result["channels"]}
    for name in failed:
        assert channels[name]["status"] == "failed", channels[name]
    cells = results.cells("qcsynth")
    assert cells.height == len(info["cells"])
    rows = cells["unreliable_markers"].to_list()
    for name in failed:
        unreliable = sum(name in (row or []) for row in rows)
        assert unreliable == len(rows), \
            f"{name} unreliable in {unreliable} of {len(rows)} cells"

    # The review sheet draws regions; a channel verdict has none.
    drawn = [r for review in oracle.reviews for r in review
             if r["candidate"] in channel_level]
    assert not drawn, f"the final review drew a channel verdict as a region: {drawn}"
