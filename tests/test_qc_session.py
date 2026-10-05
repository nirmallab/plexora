"""A QC session driven end to end by a scripted agent that knows the truth.

The stand-in agent answers each packet from the scene's painted artifacts
(`plexora.ai.qc_scenes`): a careful expert (oracle) or one that always says
"clean" (lazy). What the tests pin is the server's side: every artifact
confirmed becomes an ROI of its class, clean channels close clean, every write
is receipted and undoable, packets carry no strictness, and the user's edits
win.
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from tests.qc_fixtures import make_qc_project

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _qc():
    registry.discover(["roi", "qc"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _grid_truth(mask, grid):
    ny, nx = grid["shape"]
    s = grid["cell_full_px"]
    out = np.zeros((ny, nx), dtype=bool)
    for iy in range(ny):
        for ix in range(nx):
            block = mask[int(iy * s):int((iy + 1) * s), int(ix * s):int((ix + 1) * s)]
            out[iy, ix] = block.size and block.mean() >= 0.3
    return out


class QCOracle:
    """Answers QC packets from the scene's truth, reading candidate masks from
    the session record (a real agent would read the pictures)."""

    def __init__(self, info, style="oracle"):
        self.info = info
        self.style = style

    def _record(self, session_id):
        from plexora.plugins.qc.server.engine import store

        return store().load(session_id)

    def _truth_for(self, record, unit):
        from plexora.plugins.qc.server import candidates as cand

        grid = record["scan"][unit["project"]]["grid"]
        mask = cand.decode_mask(unit["mask"])
        best = (0.0, None)
        for region in self.info["truth"]["regions"]:
            truth = _grid_truth(region["mask"], grid)
            inter = np.logical_and(mask, truth).sum()
            if not inter:
                continue
            score = max(inter / np.logical_or(mask, truth).sum(), inter / mask.sum())
            if set(region["channels"]) & set(unit.get("channels") or []) and score > best[0]:
                best = (score, region)
        return best

    def _confirm(self, record, unit):
        truth = self.info["truth"]
        score, region = self._truth_for(record, unit)
        if truth["channels"].get(unit.get("channel")) == "failed":
            return {"verdict": "artifact", "artifact_class": "empty_or_failed_channel",
                    "severity": "severe", "boundary": "covers", "scope": "channel",
                    "confidence": "sure"}
        if region is None or score < 0.2:
            return {"verdict": "not_artifact", "confidence": "sure"}
        scope = "all_channels" if len(region["channels"]) >= 5 else \
            ("channel" if len(region["channels"]) == 1 else None)
        return {"verdict": "artifact", "artifact_class": region["class"],
                "severity": "severe", "boundary": "covers", "scope": scope,
                "exclude_recommended": True, "confidence": "sure",
                "notes": f"The oracle saw {region['class'].replace('_', ' ')} here."}

    def answer(self, packet, session_id):
        kind = packet["kind"]
        ev = packet["evidence"]
        record = self._record(session_id)
        truth = self.info["truth"]
        if kind == "channel_audit":
            verdicts = {}
            for row in ev["rows"]:
                name = row["channel"]
                bad = [r for r in truth["regions"] if name in r["channels"]] or \
                    truth["channels"].get(name) == "failed"
                if self.style == "lazy" or not bad:
                    verdicts[name] = {"verdict": "clean"}
                    continue
                named = []
                for c in row.get("candidates") or []:
                    unit = record["units"][f"{ev['project']}::candidate::{c['id']}"]
                    score, _region = self._truth_for(record, unit)
                    if score >= 0.2 or truth["channels"].get(name) == "failed":
                        named.append(c["label"])
                verdicts[name] = {"verdict": "suspicious", "where": named or ["elsewhere"]}
            return {"kind": kind, "verdicts": verdicts}
        units = [record["units"][f"{ref['project']}::{ref['type']}::{ref['id']}"]
                 for ref in packet["units"]]
        unit = units[0]
        if kind == "artifact_confirm":
            if len(units) > 1:
                # A batched first look: one judgment per sheet row, by label.
                return {"kind": kind, "verdicts": {u["label"]: self._confirm(record, u)
                                                   for u in units}}
            return {"kind": kind, **self._confirm(record, unit)}
        if kind == "artifact_scope":
            _score, region = self._truth_for(record, unit)
            wanted = set(region["channels"]) if region else set()
            best = max(ev["options"], key=lambda o: len(wanted & set(o["channels"]))
                       / len(wanted | set(o["channels"])))
            return {"kind": kind, "chosen": best["id"], "confidence": "sure"}
        if kind == "artifact_localize":
            return {"kind": kind, "chosen": "current", "confidence": "fairly_sure"}
        if kind == "artifact_grid":
            return {"kind": kind, "cells": list(ev.get("pre_selected") or [])[:64]
                    or [ev.get("allowed", ["A1"])[0]], "severity": "moderate",
                    "confidence": "fairly_sure"}
        if kind == "final_qc_review":
            return {"kind": kind, "verdict": "consistent"}
        if kind == "score_review":
            return self._score_review(unit, ev)
        raise AssertionError(kind)

    #: The class of the painted region each check should find.
    CHECK_TRUTH = {"blur": "out_of_focus", "registration": "cross_cycle_registration_error",
                   "segmentation": "segmentation_error"}

    def _score_review(self, unit, ev):
        """Each row by where its places fall: inside a painted region of the
        check's class (in the check's channel) is the artifact."""
        if unit["check"] == "artifacts":
            # The Artifact Detector (on by default): any painted physical
            # artifact, on any channel, is what its places should show.
            from plexora.plugins.qc.server import schemas

            regions = [r for r in self.info["truth"]["regions"]
                       if schemas.category_of_class(r["class"]) == "tissue_acquisition"]
        else:
            target = self.CHECK_TRUTH[unit["check"]]
            regions = [r for r in self.info["truth"]["regions"] if r["class"] == target
                       and (unit["check"] == "segmentation"
                            or unit.get("channel") in r["channels"])]
        strata = {}
        for stratum, places in (unit.get("shown") or {}).get("places", {}).items():
            inside = 0
            for place in places:
                x, y = int(place["x"]), int(place["y"])
                inside += any(0 <= y < r["mask"].shape[0] and 0 <= x < r["mask"].shape[1]
                              and r["mask"][y, x] for r in regions)
            share = inside / max(1, len(places))
            strata[stratum] = "artifact" if share >= 0.6 else \
                ("normal" if share <= 0.2 else "mixed")
        answer = {"kind": "score_review", "strata": strata, "threshold": "accept",
                  "severity": "severe", "confidence": "sure"}
        if (ev.get("global") or {}).get("possible"):
            everywhere = unit["check"] == "blur" and self.info["truth"].get("global_blur")
            answer["whole_tissue"] = "artifact" if everywhere or any(
                r["mask"].mean() > 0.5 for r in regions) else "normal"
        return answer


def drive(session, session_id, agent, limit=80):
    seen = []
    result = ok(invoke(session, "qc_next", {"session_id": session_id, "wait_s": 20}))
    for _ in range(limit):
        if result["state"] != "decision":
            assert result["state"] in ("decided", "done"), result
            return seen
        packet = result["packet"]
        seen.append(packet)
        answered = ok(invoke(session, "qc_answer", {
            "session_id": session_id, "packet_id": packet["packet_id"],
            "answer": agent.answer(packet, session_id)}))
        result = answered["next"]
    raise AssertionError("the session did not finish")


def start(session, project="qcsynth", **options):
    started = ok(invoke(session, "qc_session_start", {"project": project,
                                                      "map_cell_um": 25.0, **options}))
    jobs.drain(180)
    return started


def rois_of(session, project="qcsynth"):
    return ok(invoke(session, "list_rois", {"project": project}))["rois"]


def classes_of(session, project="qcsynth"):
    """The classes (subtypes) of the regions QC wrote: the ROI category is one
    of the five, the class is kept in QC's record."""
    from plexora.plugins.qc.server import results

    meta = results.roi_meta(project)
    rows = [r for r in meta.to_dicts() if not r.get("deleted")] if meta.height else []
    out = {r["class"] for r in rows}
    # A consolidated ROI (one per category and action) is shown under its
    # category's class; each finding's own class is in its `findings`.
    live = {r["roi_id"] for r in rows}
    result = results.active(results.load(project)) or {}
    for candidate in (result.get("candidates") or {}).values():
        if candidate.get("roi_id") in live:
            out.update(f["class"] for f in candidate.get("findings") or [])
    return out


FIVE = {"qc_blur_focus", "qc_registration", "qc_segmentation", "qc_tissue_acquisition",
        "qc_staining_signal", "qc_review"}


def test_the_oracle_confirms_every_artifact_as_a_region_of_its_class(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation", "fold", "cycle_dropout"))
    session = AgentSession()
    started = start(session)
    packets = drive(session, started["session_id"], QCOracle(info))
    kinds = [p["kind"] for p in packets]
    assert kinds[0] == "channel_audit"
    assert "artifact_confirm" in kinds
    rois = rois_of(session)
    categories = {r["category_id"] for r in rois}
    assert categories <= FIVE and "qc_tissue_acquisition" in categories
    classes = classes_of(session)
    assert "saturation_or_clipping" in classes
    assert "tissue_fold" in classes or "autofluorescence" in classes
    assert "cycle_specific_tissue_loss" in classes or \
        "tissue_damage_or_detachment" in classes
    assert all(r["name"].startswith("QC ") for r in rois)
    # Every packet stayed small, carried at most two images, and no strictness.
    for packet in packets:
        assert len(packet["images"]) <= 2
        assert len(json.dumps(packet)) < 40_000
        text = json.dumps(packet["evidence"])
        assert "strict" not in text and "lenient" not in text
    finished = ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    assert finished["result"]["active"]
    # The agent's notes outlive the session: on the region's record and its ROI.
    noted = [r for r in ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["regions"]
             if ((r.get("ai_decision") or {}).get("notes") or "").startswith("The oracle saw")]
    assert noted
    assert any("\nagent: The oracle saw" in (r.get("notes") or "") for r in rois)
    status = ok(invoke(session, "qc_session_status", {"session_id": started["session_id"],
                                                       "units": "all"}))
    channels = {u["id"]: u for u in status["units"] if u["type"] == "channel"}
    assert channels["CD8"]["state"] in ("clean", "flagged")
    assert channels["CD3"]["state"] == "flagged"


def test_every_region_is_a_child_receipt_and_rollback_removes_them(tmp_path):
    make_qc_project(tmp_path, artifacts=("saturation",))
    info = make_qc_project(tmp_path, name="qcsynth", artifacts=("saturation",))
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    rois = rois_of(session)
    assert rois
    audit = [json.loads(line) for line in
             (tmp_path / ".agent" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    children = [line for line in audit if line.get("qc_session") == started["session_id"]
                and line["operation_id"].startswith(started["receipt"]["operation_id"] + ".")]
    assert len(children) == len(rois)
    assert all(line["receipt"]["undo_hint"]["tool"] == "delete_roi" for line in children)
    rolled = ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"],
                                                      "action": "rollback"}))
    assert len(rolled["undone"]) == len(rois) and not rolled["refused"]
    assert rois_of(session) == []


def test_a_clean_scene_writes_nothing(tmp_path):
    info = make_qc_project(tmp_path, artifacts=())
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    assert rois_of(session) == []
    finished = ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    assert finished["summary"]["regions"]["exclude"] == 0


def test_propose_mode_writes_on_commit_only(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = start(session, mode="propose")
    drive(session, started["session_id"], QCOracle(info))
    assert rois_of(session) == []
    committed = ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"],
                                                         "action": "commit"}))
    assert committed["written"]
    assert rois_of(session)


def test_a_rerun_replays_every_answer_from_the_memo(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    first = start(session)
    asked = drive(session, first["session_id"], QCOracle(info))
    ok(invoke(session, "qc_session_finish", {"session_id": first["session_id"],
                                             "action": "rollback"}))
    second = start(session)
    again = drive(session, second["session_id"], QCOracle(info))
    assert len(again) < len(asked)
    status = ok(invoke(session, "qc_session_status", {"session_id": second["session_id"]}))
    assert status["replayed"] >= 1
    assert rois_of(session)


def test_stale_and_invalid_answers_are_refused(tmp_path):
    make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    result = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))
    packet = result["packet"]
    assert packet["kind"] == "channel_audit"
    # A verdict missing for a row is refused with the rows it needs.
    refused = invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                            "answer": {"kind": "channel_audit", "verdicts": {}}})
    assert not refused["ok"] and refused["error"]["code"] == "invalid_input"
    assert refused["error"]["detail"]["missing"]
    # The same packet comes back until it is answered.
    again = ok(invoke(session, "qc_next", {"session_id": sid}))
    assert again["packet"]["packet_id"] == packet["packet_id"]
    stale = invoke(session, "qc_answer", {"session_id": sid, "packet_id": "pk_9999",
                                          "answer": {"kind": "channel_audit", "verdicts": {}}})
    assert not stale["ok"] and stale["error"]["code"] == "conflict"


def test_cells_in_a_lost_region_fail_and_the_calls_are_stored(tmp_path):
    from plexora.plugins.qc.server import results

    info = make_qc_project(tmp_path, artifacts=("cycle_dropout",))
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    cells = results.cells("qcsynth")
    assert cells is not None and cells.height == len(info["cells"])
    lost = {cid for cid, reasons in info["truth"]["cells"].items() if "cycle_loss" in reasons}
    assert lost
    failed = set(cells.filter(~cells["pass"])["cell_id"].to_list())
    assert len(lost & failed) >= 0.8 * len(lost), (len(lost & failed), len(lost))
    # Cells far from the dropout mostly pass.
    assert len(failed - lost) <= 0.1 * cells.height
    reasons = {r for row in cells.filter(~cells["pass"])["reasons"].to_list() for r in row}
    assert reasons & {"region:cycle_specific_tissue_loss", "region:tissue_damage_or_detachment",
                      "region:cross_cycle_registration_error"}
    row = cells.filter(~cells["pass"]).row(0, named=True)
    assert row["primary_reason"] in row["reasons"]


def test_the_report_states_its_denominators(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation", "cycle_dropout"))
    session = AgentSession()
    started = start(session)
    drive(session, started["session_id"], QCOracle(info))
    ok(invoke(session, "qc_session_finish", {"session_id": started["session_id"]}))
    report = ok(invoke(session, "qc_report", {"session_id": started["session_id"]}))
    text = open(report["html"], encoding="utf-8").read()
    assert "Tissue excluded" in text and "data:image/png;base64," in text
    assert "union of excluded regions on tissue" in text
    assert open(report["pdf"], "rb").read(4) == b"%PDF"
    d = report["denominators"]
    assert 0 < d["excluded_tissue_fraction"] < 0.5
    assert d["cells"] == len(info["cells"]) and d["cells_excluded"] > 0


def test_an_audit_answer_may_only_name_its_own_rows_labels(tmp_path):
    make_qc_project(tmp_path, artifacts=("saturation", "aggregates"))
    session = AgentSession()
    sid = start(session)["session_id"]
    packet = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))["packet"]
    rows = packet["evidence"]["rows"]
    labelled = [r for r in rows if r.get("candidates")]
    assert labelled
    foreign = labelled[0]["candidates"][0]["label"]
    other = next(r["channel"] for r in rows if r["channel"] != labelled[0]["channel"])
    verdicts = {r["channel"]: {"verdict": "clean"} for r in rows}
    verdicts[other] = {"verdict": "suspicious", "where": [foreign]}
    refused = invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                            "answer": {"kind": "channel_audit",
                                                       "verdicts": verdicts}})
    assert refused["error"]["code"] == "invalid_input"


def test_candidates_of_an_unreadable_audit_are_still_looked_at(tmp_path, monkeypatch):
    """Two unreadable answers close the audited channels for review; their
    candidates are not stranded -- each gets its own look, and the session
    still reaches its final review."""
    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    sid = start(session)["session_id"]
    packet = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))["packet"]
    for _ in range(2):
        invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                      "answer": {"kind": "channel_audit", "verdicts": "x"}})
    kinds = [p["kind"] for p in drive(session, sid, QCOracle(info))]
    assert "artifact_confirm" in kinds and kinds[-1] == "final_qc_review", kinds


# -- what the viewer is told --------------------------------------------------------


class Heard:
    """A `notify` that records every event instead of sending it."""

    def __init__(self):
        self.events = []

    def __call__(self, project, plugin, kind, payload=None):
        self.events.append({"project": project, "plugin": plugin, "kind": kind,
                            "payload": dict(payload or {})})
        return True

    def session(self, event):
        return [e["payload"] for e in self.events
                if e["kind"] == "qc.session" and e["payload"].get("event") == event]


def drive_heard(session, session_id, agent, notify, limit=80):
    result = ok(invoke(session, "qc_next", {"session_id": session_id, "wait_s": 20},
                       notify=notify))
    for _ in range(limit):
        if result["state"] != "decision":
            return
        packet = result["packet"]
        result = ok(invoke(session, "qc_answer", {
            "session_id": session_id, "packet_id": packet["packet_id"],
            "answer": agent.answer(packet, session_id)}, notify=notify))["next"]
    raise AssertionError("the session did not finish")


def test_issued_and_answered_events_carry_what_the_agent_card_shows(tmp_path):
    from plexora.agent import artifacts

    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    heard = Heard()
    started = ok(invoke(session, "qc_session_start", {"project": "qcsynth",
                                                      "map_cell_um": 25.0}, notify=heard))
    jobs.drain(180)
    # No server attached: auto mirror stays off, and says why.
    assert started["mirror"]["status"] == "off"
    assert started["mirror"]["requested"] == "auto"
    assert started["mirror"]["reason"].startswith("no viewer")
    drive_heard(session, started["session_id"], QCOracle(info), heard)
    issued = heard.session("issued")
    assert issued
    for payload in issued:
        assert payload["narration"] and payload["subject"]
        assert payload["evidence"], payload
        for image in payload["evidence"]:
            png, _sidecar = artifacts.get(image["artifact_id"])   # what the route serves
            assert png[:8] == b"\x89PNG\r\n\x1a\n"
            assert image["width"] > 0 and image["height"] > 0 and image["caption"]
    confirms = [p for p in issued if p["kind"] == "artifact_confirm"]
    # "c7 · class · channel" for a scan candidate; an Artifact Detector region
    # (on by default) has no audit label: "class · channel".
    assert confirms and all(" · " in p["subject"]
                            and p["subject"].rsplit(" · ", 1)[-1] in info["channels"]
                            for p in confirms), [p["subject"] for p in confirms]
    answered = heard.session("answered")
    assert answered and all(p.get("narration") for p in answered)
    assert any(" confirmed: " in p["narration"] for p in answered), \
        [p["narration"] for p in answered]
    # Every region written reached an open ROI overlay under ROI's own name.
    told = [e for e in heard.events if e["plugin"] == "roi" and e["kind"] == "roi.create"]
    assert len(told) == len(rois_of(session)) and told


def _call_for(session, name="qc.answer"):
    from plexora.agent.audit import AuditLog
    from plexora.agent.policy import Policy
    from plexora.agent.receipts import operation_id
    from plexora.agent.registry import Call

    return Call(capability=registry.get(name), session=session, policy=Policy(),
                operation_id=operation_id(), audit=AuditLog(), arguments={})


def test_a_region_decided_again_is_receipted_and_undoable(tmp_path):
    from plexora.plugins.qc.server.engine import engine_for

    info = make_qc_project(tmp_path, artifacts=("saturation",))
    session = AgentSession()
    started = start(session)
    sid = started["session_id"]
    drive(session, sid, QCOracle(info))
    rois = rois_of(session)
    assert rois
    heard = Heard()
    call = _call_for(session)
    call.notify = heard
    with engine_for(call, sid) as engine:
        unit = next(u for u in engine.units_of("candidate") if u.get("roi_id"))
        roi_id, first = unit["roi_id"], list(unit["receipts"])
        new_action = "ignore" if unit.get("action") != "ignore" else "warn"
        before_count = len(engine.record["receipts"])
        op = engine.write_candidate(unit, klass=unit.get("class") or unit["class_hint"],
                                    action=new_action)
        assert op and op not in first
        assert unit["receipts"][-1] == op
        assert engine.record["receipts"][-1] == op
        assert len(engine.record["receipts"]) == before_count + 1
    audit = {line["operation_id"]: line for line in
             (json.loads(x) for x in
              (tmp_path / ".agent" / "audit.jsonl").read_text(encoding="utf-8").splitlines())
             if line.get("status") == "ok"}
    line = audit[op]
    receipt = line["receipt"]
    old = next(r for r in rois if r["id"] == roi_id)
    assert receipt["changed"] and receipt["persistent_state"] == "plugin_store:roi"
    assert receipt["before"]["name"] == old["name"]
    assert receipt["after"]["name"] != old["name"] and receipt["after"]["roi_id"] == roi_id
    assert receipt["undo_hint"]["tool"] == "update_roi"
    assert receipt["undo_hint"]["arguments"]["name"] == old["name"]
    assert receipt["revision_after"] != receipt["revision_before"]
    assert line["qc_session"] == sid and line["rewrite"] is True
    assert any(e["plugin"] == "roi" and e["kind"] == "roi.update" for e in heard.events)
    undone = ok(invoke(session, "undo_operation", {"operation_id": op}))
    assert undone["undone"] == op
    now = next(r for r in rois_of(session) if r["id"] == roi_id)
    assert now["name"] == old["name"]


def test_first_looks_share_packets(tmp_path):
    """The packet count of a whole session, answered deterministically: the
    first looks at candidates come several to a sheet (answered by label),
    so this scene takes about ten packets where it took 23 with one decision
    each."""
    info = make_qc_project(tmp_path, artifacts=("saturation", "aggregates",
                                                "blur_local"))
    session = AgentSession()
    sid = start(session, checks={"segmentation": False})["session_id"]
    packets = drive(session, sid, QCOracle(info))
    kinds = [p["kind"] for p in packets]
    assert len(packets) <= 13, kinds
    batched = [p for p in packets if p["kind"] == "artifact_confirm" and len(p["units"]) > 1]
    assert batched, kinds
    for packet in batched:
        assert len(packet["units"]) <= 4 and len(packet["images"]) == 1
        assert packet["images"][0]["role"] == "confirm_batch_sheet"
        labels = packet["evidence"]["labels"]
        assert len(set(labels)) == len(labels) == len(packet["units"])
    # Nothing was lost by asking less: the painted artifacts are still regions.
    categories = {r["category_id"] for r in rois_of(session)}
    assert categories <= FIVE and "qc_blur_focus" in categories
    classes = classes_of(session)
    assert "saturation_or_clipping" in classes and "out_of_focus" in classes


def test_a_batched_answer_must_name_every_candidate(tmp_path):
    info = make_qc_project(tmp_path, artifacts=("saturation", "aggregates", "blur_local"))
    session = AgentSession()
    sid = start(session)["session_id"]
    agent = QCOracle(info)
    result = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))
    while result["state"] == "decision":
        packet = result["packet"]
        if packet["kind"] == "artifact_confirm" and len(packet["units"]) > 1:
            break
        result = ok(invoke(session, "qc_answer", {
            "session_id": sid, "packet_id": packet["packet_id"],
            "answer": agent.answer(packet, sid)}))["next"]
    else:
        raise AssertionError("no batched first look")
    answer = agent.answer(packet, sid)
    first = sorted(answer["verdicts"])[0]
    partial = {"kind": "artifact_confirm",
               "verdicts": {first: answer["verdicts"][first]}}
    refused = invoke(session, "qc_answer", {"session_id": sid,
                                            "packet_id": packet["packet_id"],
                                            "answer": partial})
    assert refused["error"]["code"] == "invalid_input"
    assert refused["error"]["detail"]["missing"]
    single = invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                           "answer": {"kind": "artifact_confirm",
                                                      "verdict": "not_artifact"}})
    assert single["error"]["code"] == "invalid_input"
    # Two invalid answers are two strikes: the packet is released to manual
    # review, so even the full answer now finds it gone. (Every other test
    # here answers a batched first look with the full answer.)
    late = invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                         "answer": answer})
    assert late["error"]["code"] == "conflict"
    assert late["error"]["detail"]["outstanding"] != packet["packet_id"]


def test_a_round_core_asks_nothing_about_its_rim(tmp_path):
    """A TMA core's rim is a steep edge in every channel; it used to be a tile
    seam candidate per channel, each forced to a look on a clean row."""
    from plexora.plugins.qc.server import scan
    from plexora.plugins.qc.server.engine import store

    scan._MEMORY.clear()
    info = make_qc_project(tmp_path, artifacts=(), shape="round")
    session = AgentSession()
    sid = start(session)["session_id"]
    packets = drive(session, sid, QCOracle(info))
    record = store().load(sid)
    seams = [u for u in record["units"].values() if u["type"] == "candidate"
             and u.get("class_hint") == "stitching_or_tile_seam"]
    assert not seams
    assert len(packets) <= 6, [p["kind"] for p in packets]
    assert rois_of(session) == []
