"""A failed channel the user calls clean must stop flagging its marker.

AutoQC's verdict that a stain failed is a channel-level candidate
(`empty_or_failed_channel`, action exclude) that flags its marker
unreliable in every cell. `dismiss_qc_finding(finding="channel")` is the
user's "this channel is fine": the channel row turned clean, but the cells
kept the marker flagged, so strict gating still left out every cell for it.
"""

import json

from plexora.agent import AgentSession, invoke, registry


def ok(answer):
    assert answer["ok"], json.dumps(answer.get("error"), default=str)[:2000]
    return answer["result"]


def _flags_for(cells, marker):
    return sorted({f for row in cells.iter_rows(named=True)
                   for f in (row["marker_flags"] or []) if f.startswith(f"{marker}|")})


def test_a_dismissed_failed_channel_no_longer_flags_its_marker_in_the_cells(tmp_path):
    from plexora.plugins.qc.server import exclusions, results
    from tests.qc_fixtures import make_qc_project

    registry.discover(["roi", "qc"])
    info = make_qc_project(tmp_path)
    marker = next(c for c in info["channels"] if not c.upper().startswith("DNA"))
    session = AgentSession()

    with results.lock("qcsynth"):
        document = results.load("qcsynth")
        results.ensure_active(document, "qcsynth")
        result = results.active(document)
        result["channels"] = [{"name": marker, "status": "failed_channel"}]
        result.setdefault("candidates", {})["failed1"] = {
            "id": "failed1", "class": "empty_or_failed_channel", "channels": [marker],
            "channel_level": True, "action": "exclude", "state": "confirmed_exclude",
            "roi_id": None, "user_state": {}, "origin": "detector"}
        results.put_result(document, result)
        results.save("qcsynth", document)
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))

    # The failed verdict flags the marker in every cell.
    flagged = _flags_for(results.cells("qcsynth"), marker)
    assert flagged == [f"{marker}|region:empty_or_failed_channel|exclude"], flagged
    n_cells = results.cells("qcsynth").height
    by_marker = exclusions.summary(session.data("qcsynth"), "strict")["by_marker"]
    assert by_marker.get(marker) == n_cells, by_marker

    # The user: "that channel is fine".
    ok(invoke(session, "dismiss_qc_finding", {"project": "qcsynth", "finding": "channel",
                                              "channel": marker}))
    shown = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["channels"]
    assert shown[0]["status"] == "clean"

    # The cells must follow the channel's verdict, at once and after a refresh.
    after_dismiss = _flags_for(results.cells("qcsynth"), marker)
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    after_refresh = _flags_for(results.cells("qcsynth"), marker)
    by_marker = exclusions.summary(session.data("qcsynth"), "strict")["by_marker"]
    assert (after_dismiss, after_refresh, by_marker.get(marker)) == ([], [], None), (
        after_dismiss, after_refresh, by_marker)
