"""The visual-pass bench: how well a model outlines the large artifacts the
detectors missed (`qc.visual_scan`, the `visual_scan` packet).

The visual pass is the coordinator's own: while its packet is out the model
looks at the whole tissue, zooms, picks channels and writes outlines with
Plexora's tools (`render_artifact_overview`, `inspect_artifact_channels`,
`segment_qc_roi` with the session's id), then answers done. So the bench
runs it as a session does, over Plexora's MCP tools, on synthetic scenes
(`bench_qc.register`) whose large artifacts are painted into known places:
two folds, two places of lost tissue, and a clean scene that should get
no outline. The
detectors are off (`CHECKS`): every region scored is one the model drew.

Scored on the map grid, as the QC bench scores regions (`bench_qc`):

* region_recall      painted regions matched at IoU >= `bench_qc.MATCH_IOU`
                     by an outline of an accepted class (`bench_qc.ACCEPTED`).
* region_precision   outlines lying mostly on a painted artifact (an outline
                     on the clean scene, or on intact tissue, is wrong).
* region_iou         mean IoU of the matches, reported.
* failure_rate       scenes whose visual pass did not finish.

The truth reader (`run_truth`) outlines each painted region with a box round
it through the same tool: the ceiling the outlining itself allows, against
which the bar is set (a fixed bar set without that ceiling may be one no
reader can pass).

    plexora ai visual-bench --prepare ROOT      # the scenes, in data root ROOT
    plexora ai visual-bench --truth ROOT        # the truth reader, in-process
    plexora ai visual-bench --start ROOT SCENE  # a session for a model (prints its id)
    plexora ai visual-bench --score ROOT --sessions FILE   # {scene: session id}
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

FEATURE = "qc.visual_scan"
VERSION = "visual-1"
#: (scene, painted artifacts, seed).
SCENES = (("fold", ("fold",), 21), ("damage", ("dark_region",), 22), ("clean", (), 23),
          ("fold_b", ("fold",), 24), ("damage_b", ("dark_region",), 25))
CHECKS = {"blur": False, "registration": False, "segmentation": False, "artifacts": False,
          "stain": False, "visual": True}
#: The tools a model's visual pass may call (their MCP names).
TOOLS = ("qc_next", "qc_answer", "qc_session_status", "render_artifact_overview",
         "inspect_artifact_channels", "segment_qc_roi")
BRIEF = (
    "You are running the visual pass of Plexora QC session {sid} on project {project}. Call "
    "qc_next with session_id {sid}. Each packet's question says what to do: for the visual pass, "
    "look at the whole tissue, use the tools the packet names to zoom, pick channels and outline "
    "each large artifact no outline covers (segment_qc_roi with session_id {sid}; preview, "
    "then write), then answer the packet with qc_answer. Answer any other packet as it asks. "
    "Stop when qc_next or qc_answer says the session is decided or done.")


def _project(scene: str) -> str:
    return f"visualbench_{scene}"


def _truth_dir(root) -> Path:
    return Path(root) / "visual_bench"


def prepare(root, scenes=SCENES) -> list:
    """Register each scene in data root `root` (PLEXORA_DATA_PATH must point
    at it) and keep its painted regions under `root/visual_bench`."""
    from plexora.ai import bench_qc

    folder = _truth_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for scene, artifacts, seed in scenes:
        made = bench_qc.register(root, _project(scene), artifacts, seed=seed)
        regions = made["truth"]["regions"]
        np.savez_compressed(folder / f"{scene}.npz",
                            **{f"r{i}": r["mask"].astype(bool) for i, r in enumerate(regions)})
        (folder / f"{scene}.json").write_text(json.dumps({
            "scene": scene, "project": _project(scene), "artifacts": list(artifacts),
            "regions": [{"class": r["class"], "channels": list(r.get("channels") or [])}
                        for r in regions]}, indent=1), encoding="utf-8")
        out.append({"scene": scene, "project": _project(scene), "regions": len(regions)})
    return out


def truth_of(root, scene) -> dict:
    """{regions: [{class, mask}]} as `prepare` kept them."""
    folder = _truth_dir(root)
    meta = json.loads((folder / f"{scene}.json").read_text(encoding="utf-8"))
    masks = np.load(folder / f"{scene}.npz")
    return {"regions": [{**r, "mask": masks[f"r{i}"]} for i, r in enumerate(meta["regions"])]}


def start(session, scene, *, cell_um=25.0) -> str:
    """A QC session with only the visual pass on, its scan done: its id."""
    from plexora.agent import invoke, jobs

    answer = invoke(session, "qc_session_start", {"project": _project(scene), "map_cell_um": cell_um,
                                                  "checks": CHECKS, "on_limit": "extend",
                                                  "agent": "visual_bench"})
    if not answer["ok"]:
        raise RuntimeError(answer["error"])
    jobs.drain(600)
    return answer["result"]["session_id"]


def _box(mask) -> dict:
    ys, xs = np.nonzero(mask)
    return {"x": int(xs.min()), "y": int(ys.min()), "width": int(xs.max() - xs.min()) + 1,
            "height": int(ys.max() - ys.min()) + 1}


def drive_truth(session, sid, scene, truth) -> int:
    """Answer the session as the truth reader: each painted region outlined
    with a box round it through `segment_qc_roi`; every other packet by the
    QC bench's truth agent. Returns the packets answered."""
    from plexora.agent import invoke
    from plexora.ai import bench_qc

    agent = bench_qc.QCTruthAgent({"regions": [], "channels": {}, "cells": {}}, "oracle")
    state = invoke(session, "qc_next", {"session_id": sid, "wait_s": 20})["result"]
    packets = 0
    while state.get("state") == "decision" and packets < 50:
        packets += 1
        packet = state["packet"]
        if packet["kind"] == "visual_scan":
            for region in truth["regions"]:
                if region["class"] not in bench_qc.ACCEPTED:
                    continue
                written = invoke(session, "segment_qc_roi", {
                    "project": _project(scene), "box": _box(region["mask"]),
                    "artifact_class": region["class"], "session_id": sid, "mode": "new",
                    "reasoning": "the truth reader: a box round the painted region"})
                if not written["ok"]:
                    raise RuntimeError(written["error"])
            reply = {"kind": "visual_scan", "status": "done"}
        else:
            reply = agent.answer(packet, sid)
        answered = invoke(session, "qc_answer", {"session_id": sid, "packet_id": packet["packet_id"],
                                                 "answer": reply})
        if not answered["ok"]:
            raise RuntimeError(answered["error"])
        state = answered["result"]["next"]
    return packets


def _counts(regions, truth, grid) -> dict:
    """matched / scored painted regions, and outlines on / all outlines."""
    from plexora.ai import bench_qc

    scored = [r for r in truth["regions"] if r["class"] in bench_qc.ACCEPTED]
    matched, ious = 0, []
    for region in scored:
        truth_mask = bench_qc._grid_truth(region["mask"], grid)
        best = 0.0
        for predicted in regions:
            if predicted["class"] not in bench_qc.ACCEPTED[region["class"]]:
                continue
            union = np.logical_or(predicted["mask"], truth_mask).sum()
            best = max(best, float(np.logical_and(predicted["mask"], truth_mask).sum() / union)
                       if union else 0.0)
        if best >= bench_qc.MATCH_IOU:
            matched += 1
            ious.append(best)
    every = np.zeros(tuple(grid["shape"]), dtype=bool)
    for region in truth["regions"]:
        every |= bench_qc._grid_truth(region["mask"], grid, cover=0.1)
    on = sum(1 for p in regions if p["mask"].any()
             and np.logical_and(p["mask"], every).sum() >= 0.5 * p["mask"].sum())
    return {"scored": len(scored), "matched": matched, "outlines": len(regions), "on": on,
            "ious": ious}


def _drawn(result: dict, written) -> dict:
    """The result with only the regions the visual pass wrote (its unit's
    `written` candidates): the scan's own candidates, confirmed in the
    channel audit, are the detectors' finds, not the pass's."""
    written = set(written)
    kept = {}
    for cid, candidate in (result.get("candidates") or {}).items():
        if candidate.get("findings"):
            findings = [f for f in candidate["findings"] if f.get("candidate_id") in written]
            if findings:
                kept[cid] = {**candidate, "findings": findings}
        elif cid in written:
            kept[cid] = candidate
    # A consolidated finding is scored through its source candidate.
    for cid in written:
        if cid in (result.get("candidates") or {}) and cid not in kept:
            kept[cid] = result["candidates"][cid]
    return {**result, "candidates": kept}


def score(root, sessions: dict, *, session=None) -> dict:
    """{metrics, rows}: each scene's session (`sessions` {scene: id}) scored
    on the regions its result holds, pooled over the scenes."""
    from plexora.agent import AgentSession, invoke
    from plexora.ai import bench_qc
    from plexora.plugins.qc.server import results
    from plexora.plugins.qc.server.engine import store

    own = session is None
    session = session or AgentSession()
    rows, totals, failed = [], {"scored": 0, "matched": 0, "outlines": 0, "on": 0}, 0
    ious = []
    try:
        for scene, sid in sessions.items():
            project = _project(scene)
            record = store().load(sid)
            visual = next((u for u in record["units"].values() if u.get("check") == "visual"), {})
            state = visual.get("state")
            if state in (None, "pending", "awaiting_visual_scan"):
                failed += 1
            if record.get("state") != "done":
                invoke(session, "qc_session_finish", {"session_id": sid})
                record = store().load(sid)
            result = _drawn(results.active(results.load(project)) or {}, visual.get("written") or [])
            grid = record["scan"][project]["grid"]
            regions = bench_qc._regions_of_result(session, project, result, grid)
            counted = _counts(regions, truth_of(root, scene), grid)
            ious += counted.pop("ious")
            for key in totals:
                totals[key] += counted[key]
            rows.append({"scene": scene, "session_id": sid, "state": state, **counted,
                         "classes": [r["class"] for r in regions]})
    finally:
        if own:
            session.close()
    n = len(sessions) or 1
    return {"metrics": {
        "region_recall": round(totals["matched"] / totals["scored"], 4) if totals["scored"] else None,
        "region_precision": round(totals["on"] / totals["outlines"], 4) if totals["outlines"] else 0.0,
        "region_iou": round(float(np.mean(ious)), 4) if ious else None,
        "regions": totals["scored"], "outlines": totals["outlines"],
        "failure_rate": round(failed / n, 4)}, "rows": rows}


def run_truth(root) -> dict:
    """The truth reader on every prepared scene, in-process: the ceiling."""
    from plexora.agent import AgentSession, registry

    registry.discover(["roi", "qc"])
    session = AgentSession(table_limit=4)
    sessions = {}
    try:
        for scene, _artifacts, _seed in SCENES:
            sid = start(session, scene)
            drive_truth(session, sid, scene, truth_of(root, scene))
            sessions[scene] = sid
        return {**score(root, sessions, session=session), "sessions": sessions}
    finally:
        session.close()
