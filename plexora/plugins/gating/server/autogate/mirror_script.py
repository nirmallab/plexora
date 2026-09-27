"""What an open viewer is told to show for a packet -- derived from the evidence.

The invariant: the collage manifest is the only source of what the viewer is
shown. `script_for` is a pure function of the packet and its manifest (plus
the stored display calibration), so the tab shows the same image, the same
channels at the same windows and colours, the same candidate gate and the
same cells the agent is looking at -- and a golden test can pin the script.
`run` sends it (best effort; `plexora.agent.sessions.mirror`), after a
`get_state` that wakes a throttled background tab and says what it already
shows: a command already in effect (HD on, outlines drawn, the gating tool
open) is not sent again, and one that rebuilds tiles is given the time it takes.
"""

from __future__ import annotations

#: Seconds a command may take to acknowledge. Switching image or HD mode
#: rebuilds every tile and is acknowledged when that finishes.
COMMAND_TIMEOUT_S = {"open_project": 20.0, "set_hd_mode": 20.0, "show_evidence": 10.0,
                     "restore_viewer": 20.0}
DEFAULT_TIMEOUT_S = 5.0
#: The first command of a script: long enough for a background tab to wake.
WAKE_TIMEOUT_S = 10.0

#: Commands that set up the view rather than show this packet: skipped when the
#: tab reports it is already in that state (and the project is not changing).
SETUP_IN_EFFECT = {
    "set_hd_mode": lambda state: bool(state.get("hd_mode")),
    "set_cell_render_mode": lambda state: state.get("cell_mode") == "outlines",
    "open_tool": lambda state: "gating" in (state.get("tools_open") or []),
}


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


def script_for(packet, manifest, calibration_record, *, current_project=None,
               viewer_state=None):
    """[{type, arguments}] for one packet. `viewer_state` (the tab's
    `get_state`) drops set-up commands already in effect."""
    from plexora.agent.evidence import calibration

    if viewer_state and current_project is None:
        current_project = viewer_state.get("project")

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
            "subject": marker, "kind": kind,
            "url": f"agent/v1/captures/{images[0]['artifact_id']}"}})
    switching = any(c["type"] == "open_project" for c in script)
    if viewer_state and not switching:
        script = [c for c in script if not SETUP_IN_EFFECT.get(c["type"],
                                                                lambda _s: False)(viewer_state)]
    return script


def run(call, session_id, packet):
    """Send the packet's script to the session's viewer; returns
    {status (`ok`, `degraded`, `off`), sent, errors, view_id}."""
    from plexora.agent import viewer
    from plexora.agent.errors import AgentError
    from plexora.agent.evidence import calibration
    from plexora.agent.sessions import mirror
    from plexora.plugins.gating.server.autogate import engine as engines

    try:
        control = viewer.require(call.link)
    except AgentError as exc:
        return {"status": "off", "sent": 0,
                "errors": [{"code": exc.code, "message": exc.message}]}
    with engines.engine_for(call, session_id, save=False) as engine:
        options = dict(engine.options)
        view_id = (engine.record.get("mirror") or {}).get("view_id") or options["view_id"]
        unit = engine.record["units"].get(engines.unit_key(packet["units"][0]["project"],
                                                           packet["units"][0]["marker"])) \
            if packet.get("units") else None
        manifest = (unit or {}).get("last_manifest")
    try:
        view = viewer.resolve_view(control, view_id)
    except AgentError as exc:
        return {"status": "off", "sent": 0,
                "errors": [{"code": exc.code, "message": exc.message}]}
    # Wakes a throttled tab, and says what it already shows. Not an error of
    # the script when it fails: the script is then sent whole.
    state = None
    try:
        state = (control.send(view["view_id"], "get_state", {},
                              timeout=WAKE_TIMEOUT_S) or {}).get("result")
    except AgentError as exc:
        if exc.code == "viewer_not_available":
            return {"status": "off", "sent": 0, "view_id": view["view_id"],
                    "errors": [{"code": exc.code, "message": exc.message}]}
    project = packet["units"][0]["project"] if packet.get("units") else None
    script = script_for(packet, manifest, calibration.load(project) if project else None,
                        current_project=(state or {}).get("project") or view.get("project"),
                        viewer_state=state)

    def send(type, arguments):
        return control.send(view["view_id"], type, arguments,
                            timeout=COMMAND_TIMEOUT_S.get(type, DEFAULT_TIMEOUT_S))

    result = mirror.run_script(send, script, delay_ms=options["mirror_delay_ms"])
    result["view_id"] = view["view_id"]
    return result


def teardown_script(reason) -> list:
    """What the tab is told when a session ends: put the view back as the
    agent found it (the bridge's lease) and clear what it drew."""
    return [{"type": "restore_viewer", "arguments": {"reason": str(reason)}}]


def run_teardown(call, session_id, reason):
    """Send the teardown to the session's viewer (best effort); same result
    shape as `run`."""
    from plexora.agent import viewer
    from plexora.agent.errors import AgentError
    from plexora.agent.sessions import mirror
    from plexora.plugins.gating.server.autogate import engine as engines

    try:
        control = viewer.require(call.link)
    except AgentError as exc:
        return {"status": "off", "sent": 0,
                "errors": [{"code": exc.code, "message": exc.message}]}
    with engines.engine_for(call, session_id, save=False) as engine:
        view_id = (engine.record.get("mirror") or {}).get("view_id") \
            or engine.options["view_id"]
    try:
        view = viewer.resolve_view(control, view_id)
    except AgentError as exc:
        return {"status": "off", "sent": 0,
                "errors": [{"code": exc.code, "message": exc.message}]}

    def send(type, arguments):
        return control.send(view["view_id"], type, arguments,
                            timeout=COMMAND_TIMEOUT_S.get(type, DEFAULT_TIMEOUT_S))

    result = mirror.run_script(send, teardown_script(reason), delay_ms=0)
    result["view_id"] = view["view_id"]
    return result
