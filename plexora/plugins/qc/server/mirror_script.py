"""What an open viewer is told to show for a QC packet -- derived from the packet.

`script_for` is a pure function of the packet (plus the stored display
calibration and what the tab already shows), so a golden test can pin it and
the tab shows what the agent is judging: the channel at the same calibrated
window, the camera on the candidate, its outline (`show_shapes`), and the
evidence thumbnail. `run` sends it, best effort (`sessions.mirror`).
"""

from __future__ import annotations

from plexora.plugins.qc.server import schemas

SETUP_IN_EFFECT = {
    "set_hd_mode": lambda state: bool(state.get("hd_mode")),
    "set_cell_render_mode": lambda state: state.get("cell_mode") == "none",
    "open_tool": lambda state: "qc" in (state.get("tools_open") or []),
}

OUTLINE = "#ff3df2"

#: The tab's width in screen pixels assumed when its windows are scaled to
#: the region it is flown to (`calibration.window_at`).
VIEW_PX = 1200

#: Registration shows the reference red and the comparison green, as its
#: sheets do: yellow where they agree.
REGISTRATION_COLORS = ("#ff3030", "#30ff30")

#: Packet kinds whose units are cell modules: the cells' outlines are drawn.
CELL_KINDS = ("cell_modules", "cell_intensity", "cell_area", "cycle_stability",
              "channel_outlier", "cell_segmentation")


def _check_channels(unit, calibration_record):
    """What a check's review is judged on, as viewer channels."""
    from plexora.agent.evidence import calibration

    windows = (calibration_record or {}).get("channels") or {}
    if unit.get("check") == "registration" and unit.get("reference") and unit.get("channel"):
        pair = (unit["reference"], unit["channel"])
        return [{"name": name, "color": colour, "window": windows[name]["window"],
                 "enabled": True} for name, colour in zip(pair, REGISTRATION_COLORS)
                if name in windows]
    if unit.get("channel"):
        return calibration.as_viewer_channels(calibration_record, unit["channel"], ())
    return []


def _cell_marker(packet, unit):
    evidence = packet.get("evidence") or {}
    modules = evidence.get("modules") or {}
    first = next(iter(modules.values()), None) if modules else evidence
    return (first or {}).get("marker") or (unit or {}).get("marker")


def _view_scale(kind, unit, units):
    """Full-resolution pixels per screen pixel of what the tab is flown to:
    the padded box of the candidate(s), the whole tissue for an audit; None
    (the calibrated window) when the camera does not move."""
    from plexora.agent.evidence import calibration

    if kind == "channel_audit":
        return calibration.SCALE_BLEND[1]
    batch = [u for u in (units or []) if u and u.get("type") == "candidate" and u.get("bbox")]
    if len(batch) > 1:
        boxes = [u["bbox"] for u in batch]
        box, factor = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                       max(b[2] for b in boxes), max(b[3] for b in boxes)), 1.3
    elif unit is not None and unit.get("type") == "candidate" and unit.get("bbox"):
        box, factor = unit["bbox"], 2.5
    else:
        return None
    return _padded(box, factor)["width"] / VIEW_PX


def _padded(box, factor=2.5):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    side = max(x1 - x0, y1 - y0) * factor
    return {"x": cx - side / 2, "y": cy - side / 2, "width": side, "height": side}


def script_for(packet, unit, calibration_record, *, current_project=None, viewer_state=None,
               units=None):
    """[{type, arguments}] for one packet. `unit` is the packet's first unit as
    the session stores it (bbox, geometry, variants); None for an audit.
    `units` is every unit a batched packet carries: the view fits them all and
    each gets its outline."""
    from plexora.agent.evidence import calibration

    if viewer_state and current_project is None:
        current_project = viewer_state.get("project")
    kind = packet.get("kind")
    refs = packet.get("units") or []
    if not refs:
        return []
    project = refs[0]["project"]
    evidence = packet.get("evidence") or {}
    script = []
    if current_project != project:
        script.append({"type": "open_project", "arguments": {"project": project, "tool": "qc",
                                                             "carry": True}})
    # 16-bit tiles, as the evidence was drawn from the raw planes: a dim
    # channel's steps are not crushed into a few 8-bit levels.
    script.append({"type": "set_hd_mode", "arguments": {"enabled": True}})
    channel = None
    channels = None
    if unit is not None and unit.get("type") == "candidate":
        channel = unit.get("channel")
    elif unit is not None and unit.get("type") == "check":
        channel = unit.get("channel")
        channels = _check_channels(unit, calibration_record) if calibration_record else None
    elif kind in CELL_KINDS:
        channel = _cell_marker(packet, unit)
    elif kind == "channel_audit":
        rows = evidence.get("rows") or []
        channel = rows[0]["channel"] if rows else None
    if channel and calibration_record and channels is None:
        channels = calibration.as_viewer_channels(calibration_record, channel, (),
                                                  px_per_px=_view_scale(kind, unit, units))
    if channels:
        script.append({"type": "set_channels", "arguments": {
            "mode": "replace", "persist": False,
            "channels": [{k: c[k] for k in ("name", "color", "window", "enabled")}
                         for c in channels]}})
    # Cells and the mask are what cell packets and a segmentation review judge.
    outlines = kind in CELL_KINDS or (unit is not None and unit.get("check") == "segmentation")
    script.append({"type": "set_cell_render_mode",
                   "arguments": {"mode": "outlines" if outlines else "none"}})
    shapes = []
    batch = [u for u in (units or []) if u and u.get("type") == "candidate" and u.get("bbox")]
    if len(batch) > 1:
        boxes = [u["bbox"] for u in batch]
        script.append({"type": "fit_region", "arguments": _padded(
            (min(b[0] for b in boxes), min(b[1] for b in boxes),
             max(b[2] for b in boxes), max(b[3] for b in boxes)), factor=1.3)})
        for member in batch:
            geometry = member.get("geometry") or (
                (member.get("variants") or {}).get("standard") or {}).get("geometry")
            if geometry:
                shapes.append({"id": member["id"][:32], "geometry": geometry,
                               "color": schemas.CLASS_COLORS.get(member.get("class_hint"),
                                                                 OUTLINE),
                               "label": member.get("label") or ""})
    elif unit is not None and unit.get("type") == "candidate" and unit.get("bbox"):
        script.append({"type": "fit_region", "arguments": _padded(unit["bbox"])})
        variants = unit.get("variants") or {}
        if kind == "artifact_localize" and variants:
            from plexora.plugins.qc.server import sheets

            for name, variant in variants.items():
                if name in sheets.VARIANT_LETTERS:
                    shapes.append({"id": name, "geometry": variant["geometry"],
                                   "color": sheets.VARIANT_COLORS[name],
                                   "label": sheets.VARIANT_LETTERS[name]})
        elif kind == "artifact_grid" and unit.get("grid_spec"):
            for square in unit["grid_spec"]["squares"][:32]:
                x0, y0, x1, y1 = square["bounds"]
                shapes.append({"id": square["id"], "bounds": {"x": x0, "y": y0,
                                                              "width": x1 - x0,
                                                              "height": y1 - y0},
                               "color": "#22e6e6", "label": square["id"]})
        else:
            geometry = unit.get("geometry") or (variants.get("standard") or {}).get("geometry")
            if geometry:
                klass = (unit.get("decision") or {}).get("artifact_class") \
                    or unit.get("class_hint")
                shapes.append({"id": unit["id"][:32], "geometry": geometry,
                               "color": schemas.CLASS_COLORS.get(klass, OUTLINE),
                               "label": unit.get("label") or ""})
    elif kind == "final_qc_review" and unit is not None:
        for region in (unit.get("regions_geometry") or [])[:32]:
            shapes.append(region)
    if shapes:
        script.append({"type": "show_shapes", "arguments": {"shapes": shapes[:32],
                                                            "ttl_ms": 120_000, "clear": True}})
    images = packet.get("images") or []
    if images and images[0].get("artifact_id"):
        from plexora.plugins.qc.server import packets

        script.append({"type": "show_evidence", "arguments": {
            "artifact_id": images[0]["artifact_id"],
            "caption": packets.evidence_label(packet)[:300], "subject": channel or project,
            "kind": kind, "url": f"agent/v1/captures/{images[0]['artifact_id']}"}})
    switching = any(c["type"] == "open_project" for c in script)
    if viewer_state and not switching:
        script = [c for c in script if not SETUP_IN_EFFECT.get(c["type"],
                                                                lambda _s: False)(viewer_state)]
    return script


def run(call, session_id, packet):
    from plexora.agent.evidence import calibration
    from plexora.agent.sessions import mirror
    from plexora.plugins.qc.server.engine import engine_for

    with engine_for(call, session_id, save=False) as engine:
        options = dict(engine.options)
        view_id = (engine.record.get("mirror") or {}).get("view_id") or options["view_id"]
        refs = packet.get("units") or []
        units = [dict(u) for u in (engine.record["units"].get(engine.unit_key_of(r))
                                   for r in refs) if u]
        unit = units[0] if units else None
    opened = mirror.open_view(call, view_id)
    if isinstance(opened, dict):
        return opened
    control, view, state = opened
    project = (packet.get("units") or [{}])[0].get("project")
    script = script_for(packet, unit, calibration.load(project) if project else None,
                        current_project=(state or {}).get("project") or view.get("project"),
                        viewer_state=state, units=units)
    return mirror.send_script(control, view, script, delay_ms=options["mirror_delay_ms"])


def teardown_script(reason) -> list:
    return [{"type": "show_shapes", "arguments": {"shapes": [], "clear": True}},
            {"type": "restore_viewer", "arguments": {"reason": str(reason)}}]


def run_teardown(call, session_id, reason):
    from plexora.agent.sessions import mirror
    from plexora.plugins.qc.server.engine import engine_for

    with engine_for(call, session_id, save=False) as engine:
        view_id = (engine.record.get("mirror") or {}).get("view_id") \
            or engine.options["view_id"]
    opened = mirror.open_view(call, view_id)
    if isinstance(opened, dict):
        return opened
    control, view, _state = opened
    return mirror.send_script(control, view, teardown_script(reason), delay_ms=0)
