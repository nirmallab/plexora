"""What an open viewer is told to show for a packet -- derived from the evidence.

The invariant: the collage manifest is the only source of what the viewer is
shown. `script_for` is a pure function of the packet and its manifest (plus
the stored display calibration), so the tab shows the same image, the same
channels at the same windows and colours, the same candidate gate and the
same cells the agent is looking at -- and a golden test can pin the script.
`run` sends it (best effort; `plexora.agent.sessions.mirror`).
"""

from __future__ import annotations


def _cells(manifest, limit=24):
    cells = []
    for row in (manifest or {}).get("rows") or []:
        for cell in row.get("cells") or []:
            caption = f"#{cell['cell_id']}"
            if cell.get("value") is not None:
                from plexora.agent.evidence.collage import compact_number

                caption += f" {compact_number(cell['value'])}"
                if cell.get("call") in ("positive", "negative"):
                    caption += "+" if cell["call"] == "positive" else "-"
            cells.append({"id": int(cell["cell_id"]), "caption": caption,
                          "x": cell.get("x"), "y": cell.get("y")})
    return cells[:limit]


def _field_around(cells, pad=120.0):
    xs = [c["x"] for c in cells if c.get("x") is not None]
    ys = [c["y"] for c in cells if c.get("y") is not None]
    if not xs:
        return None
    # The densest few: a box around the first near-gate cell and its closest
    # companions, not the whole slide's spread.
    cx, cy = xs[0], ys[0]
    near = sorted(zip(xs, ys), key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)[:6]
    x0 = min(p[0] for p in near) - pad
    y0 = min(p[1] for p in near) - pad
    x1 = max(p[0] for p in near) + pad
    y1 = max(p[1] for p in near) + pad
    side = max(x1 - x0, y1 - y0)
    return {"x": x0, "y": y0, "width": side, "height": side}


def script_for(packet, manifest, calibration_record, *, current_project=None):
    """[{type, arguments}] for one packet."""
    from plexora.agent.evidence import calibration

    kind = packet.get("kind")
    units = packet.get("units") or []
    if not units:
        return []
    project = units[0]["project"]
    marker = units[0]["marker"]
    evidence = packet.get("evidence") or {}
    script = []
    if current_project != project:
        script.append({"type": "open_project", "arguments": {"project": project,
                                                             "tool": "gating", "carry": True}})
    script.append({"type": "set_hd_mode", "arguments": {"enabled": True}})
    references = [r["marker"] for r in evidence.get("references") or []]
    channels = calibration.as_viewer_channels(calibration_record, marker, references)
    if channels:
        script.append({"type": "set_channels", "arguments": {
            "mode": "replace", "persist": False,
            "channels": [{k: c[k] for k in ("name", "color", "window", "enabled")}
                         for c in channels]}})
    script.append({"type": "set_cell_render_mode", "arguments": {"mode": "outlines"}})
    script.append({"type": "open_tool", "arguments": {"tool": "gating"}})
    script.append({"type": "set_active_marker", "arguments": {"marker": marker}})
    candidate = (evidence.get("candidate") or {}).get("low") or evidence.get("current") \
        or evidence.get("final") or ((evidence.get("this") or {}).get("aligned_gate"))
    high = (evidence.get("candidate") or {}).get("high")
    if candidate is not None:
        script.append({"type": "preview_gate", "arguments": {
            "marker": marker, "low": float(candidate), "high": high, "persist": False}})
    cells = _cells(manifest)
    field = _field_around(cells)
    if field:
        script.append({"type": "fit_region", "arguments": field})
    if cells:
        script.append({"type": "highlight_cells", "arguments": {
            "cells": [{"id": c["id"], "caption": c["caption"], "x": c["x"], "y": c["y"]}
                      for c in cells if c.get("x") is not None],
            "ttl_ms": 120_000, "clear": True}})
    if kind == "t4_candidates":
        for item in sorted(evidence.get("candidates") or [], key=lambda c: c["low"]):
            script.append({"type": "preview_gate", "arguments": {
                "marker": marker, "low": float(item["low"]), "high": high, "persist": False}})
    images = packet.get("images") or []
    if images and images[0].get("artifact_id"):
        script.append({"type": "show_evidence", "arguments": {
            "artifact_id": images[0]["artifact_id"],
            "caption": str(packet.get("question") or "")[:300],
            "url": f"agent/v1/captures/{images[0]['artifact_id']}"}})
    return script


def run(call, session_id, packet):
    """Send the packet's script to the session's viewer; returns the mirror's
    status (`ok`, `degraded`, `off`)."""
    from plexora.agent import viewer
    from plexora.agent.errors import AgentError
    from plexora.agent.evidence import calibration
    from plexora.agent.sessions import mirror
    from plexora.plugins.gating.server.autogate import engine as engines

    control = viewer.connect(call.link)
    if control is None:
        return {"status": "off", "errors": [{"code": "viewer_not_available"}]}
    with engines.engine_for(call, session_id, save=False) as engine:
        options = dict(engine.options)
        view_id = (engine.record.get("mirror") or {}).get("view_id") or options.get("view_id")
        unit = engine.record["units"].get(engines.unit_key(packet["units"][0]["project"],
                                                           packet["units"][0]["marker"])) \
            if packet.get("units") else None
        manifest = (unit or {}).get("last_manifest")
    try:
        view = viewer.resolve_view(control, view_id)
    except AgentError as exc:
        return {"status": "off", "errors": [{"code": exc.code, "message": exc.message}]}
    project = packet["units"][0]["project"] if packet.get("units") else None
    script = script_for(packet, manifest, calibration.load(project) if project else None,
                        current_project=view.get("project"))

    def send(type, arguments):
        return control.send(view["view_id"], type, arguments, timeout=5)

    result = mirror.run_script(send, script, delay_ms=options.get("mirror_delay_ms", 600))
    result["view_id"] = view["view_id"]
    return result
