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
                     "restore_viewer": 20.0,
                     # A tab showing a contrast check's channels holds the
                     # session's view commands and acknowledges them at once;
                     # one with a large list to merge can still take a moment.
                     "set_channels": 20.0}
#: The tab's caption while it eases each channel (`animate_contrast`).
DISPLAY_CAPTION = "Adjusting contrast for {name}"
#: Up to this many channels are adjusted at the tab's natural pace (a few
#: seconds each, as a person would); more (a `rule` session shows every
#: marker of its image at once) go briskly.
DISPLAY_PACED = 8
#: The channel slot each marker replaces in turn while the tab shows it: the
#: second, beside the nuclear stain in the first.
DISPLAY_SLOT = 1
#: The slot the nuclear stain is adjusted in, before the markers.
NUCLEAR_SLOT = 0
DEFAULT_TIMEOUT_S = 5.0
#: The first command of a script: long enough for a background tab to wake.
WAKE_TIMEOUT_S = 10.0
#: The width, in screen pixels, a tab is assumed to show the field it is
#: flown to at: the windows are the calibration's for that scale
#: (`calibration.window_at`), as QC's mirror does.
VIEW_PX = 1200

#: A look played as a person would make it (`preview_gate` with `show`;
#: gatingAgentShow.js): up to this many of its cells for the view to close in
#: on, and this many of the candidate gates on each side of the one shown, for
#: the slider to try on its way there.
SHOW_CELLS = 6
SHOW_STOPS = 2

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


def _engine_gate(unit):
    """The gate a blind look leaves on the tab's slider: v2's leader so far,
    or the gate it started from before any look -- {low, high, stops} or None,
    `stops` the candidate gates beside it. A `cell_panel` packet names no
    gate, so the reader stays blind; the tab is the user's, and its
    `get_state` reports stored gates, never a preview."""
    state = (unit or {}).get("hypotheses") or {}
    low = state.get("leader")
    if low is None:
        low = state.get("start")
    if low is None:
        return None
    points = [float(p["low"]) for p in state.get("points") or [] if p.get("low") is not None]
    return {"low": float(low), "high": unit.get("high"), "stops": _stops(points, float(low))}


def _stops(values, target):
    """The SHOW_STOPS candidate gates nearest `target` on each side, sorted."""
    below = sorted((v for v in values if v < target), reverse=True)[:SHOW_STOPS]
    above = sorted(v for v in values if v > target)[:SHOW_STOPS]
    return sorted(round(v, 6) for v in below + above)


def _show(project, marker, cells, field, stops, look):
    """`preview_gate`'s `show`: what the tab makes a person's look of -- the
    field and a few of its cells to close in on, the candidate gates to try
    on the way to the one shown, the look's number for this marker (a first
    look explores, a later one refines) and a seed that is the marker's own,
    so two markers are not shown with the same moves."""
    import zlib

    inside = [{"x": c["x"], "y": c["y"]} for c in cells
              if c.get("x") is not None and c.get("y") is not None and field
              and field["x"] <= c["x"] <= field["x"] + field["width"]
              and field["y"] <= c["y"] <= field["y"] + field["height"]]
    show = {"seed": zlib.crc32(f"{project}::{marker}".encode("utf-8")),
            "cells": inside[:SHOW_CELLS], "stops": list(stops)}
    if field:
        show["field"] = field
    if look:
        show["look"] = int(look)
    return show


def script_for(packet, manifest, calibration_record, *, current_project=None,
               viewer_state=None, tool="gating", gate=None):
    """[{type, arguments}] for one packet. `viewer_state` (the tab's
    `get_state`) drops set-up commands already in effect; `tool` is the tool
    an image is opened with (None: a contrast run, which needs none); `gate`
    ({low, high}, `_engine_gate`) is shown when the packet carries none."""
    from plexora.agent.evidence import calibration
    from plexora.plugins.gating.server.autogate import packets, schemas

    if viewer_state and current_project is None:
        current_project = viewer_state.get("project")

    kind = packet.get("kind")
    units = packet.get("units") or []
    if kind in schemas.USER_SETUP_KINDS:
        return setup_script(packet, current_project=current_project)
    if kind == "display_setup":
        return display_packet_script(packet, current_project=current_project, tool=tool)
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
    references = [r["marker"] if isinstance(r, dict) else r
                  for r in evidence.get("references") or []]
    cells = _cells(manifest)
    field = _field_around(cells)
    channels = calibration.as_viewer_channels(
        calibration_record, marker, references,
        px_per_px=(field["width"] / VIEW_PX) if field else None)
    if channels:
        script.append({"type": "set_channels", "arguments": {
            "mode": "replace", "persist": False,
            "channels": [{k: c[k] for k in ("name", "color", "window", "curve", "enabled")
                          if k in c} for c in channels]}})
    script.append({"type": "set_cell_render_mode", "arguments": {"mode": "outlines"}})
    script.append({"type": "open_tool", "arguments": {"tool": "gating"}})
    script.append({"type": "set_active_marker", "arguments": {"marker": marker}})
    candidate = (evidence.get("candidate") or {}).get("low") or evidence.get("current") \
        or evidence.get("final") or ((evidence.get("this") or {}).get("aligned_gate"))
    high = (evidence.get("candidate") or {}).get("high")
    stops = []
    if candidate is None and gate:
        candidate, high = gate["low"], gate.get("high")
        stops = gate.get("stops") or []
    show = None
    if candidate is not None:
        if kind == "t4_candidates":
            stops = _stops([float(c["low"]) for c in evidence.get("candidates") or []],
                           float(candidate))
        # The tab flies to the field and tries the gates itself, at a
        # person's pace, while the session goes on: no `fit_region` cut.
        show = _show(project, marker, cells, field, stops, evidence.get("look"))
        script.append({"type": "preview_gate", "arguments": {
            "marker": marker, "low": float(candidate), "high": high, "persist": False,
            "show": show}})
    if field and show is None:
        script.append({"type": "fit_region", "arguments": field})
    if cells:
        script.append({"type": "highlight_cells", "arguments": {
            "cells": [{"id": c["id"], "caption": c["caption"], "x": c["x"], "y": c["y"]}
                      for c in cells if c.get("x") is not None],
            "ttl_ms": 120_000, "clear": True}})
    images = packet.get("images") or []
    if images and images[0].get("artifact_id"):
        script.append({"type": "show_evidence", "arguments": {
            "artifact_id": images[0]["artifact_id"],
            # What the picture is, for the user -- not the agent's question.
            "caption": packets.evidence_label(packet)[:300],
            "subject": marker, "kind": kind,
            "url": f"agent/v1/captures/{images[0]['artifact_id']}"}})
    switching = any(c["type"] == "open_project" for c in script)
    if viewer_state and not switching:
        script = [c for c in script if not SETUP_IN_EFFECT.get(c["type"],
                                                                lambda _s: False)(viewer_state)]
    return script


def setup_script(packet, *, current_project=None) -> list:
    """A set-up question the user answers in the tab (`schemas.USER_SETUP_KINDS`:
    which matrix to gate). The gating panel cannot open quietly until it is
    answered -- the panel route asks for the unconfirmed input instead -- so
    the tab is told to put its own requirements prompt to the user
    (`open_tool` with `ask`), not to open the panel and fail."""
    projects = (packet.get("evidence") or {}).get("projects") or []
    if not projects:
        return []
    script = []
    if current_project != projects[0]:
        # No `tool`: opening a tool while switching takes the eager path, which
        # does not ask -- the prompt comes from the `open_tool` after it.
        script.append({"type": "open_project", "arguments": {"project": projects[0],
                                                             "carry": True}})
    script.append({"type": "open_tool", "arguments": {"tool": "gating", "ask": True}})
    return script


def display_packet_script(packet, *, current_project=None, tool="gating") -> list:
    """A contrast check going out: the tab on its image, in HD (an asinh
    curve stretches the dark end, where 8-bit tiles have the fewest steps).
    The channels move when the check is answered (`display_script`). A
    contrast run (`tool` None) opens no tool: the channel panel is the show."""
    project = (packet.get("evidence") or {}).get("project")
    if not project:
        return []
    script = []
    if current_project != project:
        arguments = {"project": project, "carry": True}
        if tool:
            arguments["tool"] = tool
        script.append({"type": "open_project", "arguments": arguments})
    script.append({"type": "set_hd_mode", "arguments": {"enabled": True}})
    return script


def display_script(calibration_record, channels) -> list:
    """`animate_contrast` for `channels` at the windows and curves the canvas
    keeps (`calibration.view_spec`): the tab puts each marker in turn in the
    second channel slot -- the nuclear stain in the first, in its own colour
    -- and drags its slider there, captioned, while the session goes on (the
    command is acknowledged at once, and the tab holds the session's later
    view commands until it is done). A marker carries one of its bright cells
    (`focus`) for the tab to zoom in on. [] when none has an entry."""
    from plexora.agent.evidence import calibration

    entries = (calibration_record or {}).get("channels") or {}
    items = []
    for name in dict.fromkeys(channels):
        entry = entries.get(name) or {}
        spec = calibration.view_spec(entry)
        if spec is None:
            continue
        item = {"name": name, "window": spec["window"],
                "curve": calibration.viewer_curve(spec["curve"]),
                # The same choice for a tab whose Log brightness is off.
                "linear_window": spec["linear_window"]}
        if entry.get("role") == "nuclear":
            item["slot"] = NUCLEAR_SLOT
        else:
            item["color"] = calibration.MARKER_COLOR
            focus = _focus_cell(entry)
            if focus is not None:
                item["focus"] = focus
        items.append(item)
    if not items:
        return []
    markers = [item for item in items if "slot" not in item]
    pace = "brisk" if len(markers) > DISPLAY_PACED else "natural"
    return [{"type": "animate_contrast", "arguments": {
        "channels": items, "caption": DISPLAY_CAPTION, "slot": DISPLAY_SLOT, "pace": pace}}]


def _focus_cell(entry):
    """One of the bright cells the calibration read a marker's titre from,
    {x, y} in image pixels -- the brightest -- or None."""
    cells = [c for c in ((entry.get("cells") or {}).get("bright") or [])
             if isinstance(c, dict) and c.get("x") is not None and c.get("y") is not None]
    if not cells:
        return None
    best = max(cells, key=lambda c: float(c.get("value") or 0.0))
    return {"x": round(float(best["x"]), 1), "y": round(float(best["y"]), 1)}


def _unshown(engine, project, nuclear=None):
    """The channels of `project` whose settled brightness the tab has not
    been shown, taken off the record (shown once). A `rule` session shows
    every marker of the image once. The nuclear stain (`nuclear`) leads the
    first show that has markers in it."""
    from plexora.plugins.gating.server.autogate import views

    entry = ((engine.record.get("display") or {}).get("projects") or {}).get(project)
    if not entry:
        return []
    channels = list(entry.get("unshown") or [])
    if entry.pop("show_all", False):
        ds = engine.call.session.data(project)
        for unit in engine.units_of(project):
            if str(unit["state"]).startswith("skipped"):
                continue
            channel = views.image_channel(ds, unit["marker"])
            if channel:
                channels.append(channel)
    entry["unshown"] = []
    if nuclear and channels and not entry.get("nuclear_shown"):
        entry["nuclear_shown"] = True
        channels.insert(0, nuclear)
    return list(dict.fromkeys(channels))


def _packet_project(packet):
    if packet.get("units"):
        return packet["units"][0]["project"]
    evidence = packet.get("evidence") or {}
    # A set-up packet names its image: one (`project`, a contrast check) or
    # every image waiting on it (`projects`).
    return evidence.get("project") or (evidence.get("projects") or [None])[0]


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
        gate = _engine_gate(unit)
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
    project = _packet_project(packet)
    stored = calibration.load(project) if project else None
    script = script_for(packet, manifest, stored,
                        current_project=(state or {}).get("project") or view.get("project"),
                        viewer_state=state, tool=_tool(options), gate=gate)
    if project:
        # What a contrast check settled, eased in channel by channel before
        # this packet's own channels (the tab plays it while the session asks
        # its next question; a later `set_channels` waits for it).
        with engines.engine_for(call, session_id) as engine:
            shown = _unshown(engine, project, nuclear=(stored or {}).get("nuclear"))
        animate = display_script(stored, shown)
        if animate:
            at = next((i for i, c in enumerate(script)
                       if c["type"] not in ("open_project", "set_hd_mode")), len(script))
            if not any(c["type"] == "set_hd_mode" for c in script[:at]) \
                    and not (state or {}).get("hd_mode"):
                animate = [{"type": "set_hd_mode", "arguments": {"enabled": True}}, *animate]
            script = script[:at] + animate + script[at:]

    def send(type, arguments):
        return control.send(view["view_id"], type, arguments,
                            timeout=COMMAND_TIMEOUT_S.get(type, DEFAULT_TIMEOUT_S))

    result = mirror.run_script(send, script, delay_ms=options["mirror_delay_ms"],
                               keep_going=mirror.attached(engines.store(), session_id))
    result["view_id"] = view["view_id"]
    return result


def _tool(options):
    """The tool the tab opens an image with: gating's, or none for a contrast
    run (`purpose: contrast`), whose show is the channel panel itself."""
    return None if options["purpose"] == "contrast" else "gating"


def run_display(call, session_id, project):
    """The tab shown what a contrast check just settled for `project`, right
    after the answer rather than with the next packet: the show starts while
    the session moves on, and a contrast run, which has no next packet, still
    plays it. Same result shape as `run`; {status: off} without a viewer."""
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
    try:
        view = viewer.resolve_view(control, view_id)
    except AgentError as exc:
        return {"status": "off", "sent": 0,
                "errors": [{"code": exc.code, "message": exc.message}]}
    stored = calibration.load(project)
    with engines.engine_for(call, session_id) as engine:
        shown = _unshown(engine, project, nuclear=(stored or {}).get("nuclear"))
    animate = display_script(stored, shown)
    if not animate:
        return {"status": "ok", "sent": 0, "view_id": view["view_id"]}
    state = None
    try:
        state = (control.send(view["view_id"], "get_state", {},
                              timeout=WAKE_TIMEOUT_S) or {}).get("result")
    except AgentError as exc:
        if exc.code == "viewer_not_available":
            return {"status": "off", "sent": 0, "view_id": view["view_id"],
                    "errors": [{"code": exc.code, "message": exc.message}]}
    script = []
    if (state or {}).get("project", view.get("project")) != project:
        arguments = {"project": project, "carry": True}
        if _tool(options):
            arguments["tool"] = _tool(options)
        script.append({"type": "open_project", "arguments": arguments})
    if not (state or {}).get("hd_mode"):
        script.append({"type": "set_hd_mode", "arguments": {"enabled": True}})
    script.extend(animate)

    def send(type, arguments):
        return control.send(view["view_id"], type, arguments,
                            timeout=COMMAND_TIMEOUT_S.get(type, DEFAULT_TIMEOUT_S))

    result = mirror.run_script(send, script, delay_ms=options["mirror_delay_ms"],
                               keep_going=mirror.attached(engines.store(), session_id))
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
