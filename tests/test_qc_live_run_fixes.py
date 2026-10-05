"""What the first live AutoQC run on a large image (45k x 53k px, 40 channels)
found wrong, pinned so it stays fixed.

Each test names the symptom it guards: every channel flagged as a failed
stain, a mirror word the viewer refuses, a Pillow refusal passed off as the
agent's bad answer, a registration check scoring tissue that one cycle lost,
the start result repeating one answer schema five times, the sheets
drawing inverted rectangles when the scan map is finer than the panel, and
the bulk pass going silent for the whole 20-minute scan with nothing for the
agent panel to show but "0 of 89 channels".
"""

import numpy as np


def test_a_sparse_section_is_not_a_near_zero_plane():
    """The slide around the tissue is zero in every channel: measured over the
    whole plane, a section covering 42% of the image flagged all 40 channels
    `near_zero_plane`, which also hid them from every detector and from
    refinement ("no usable channel")."""
    from plexora.plugins.qc.server import scan

    plane = np.zeros((200, 200), dtype=np.float32)
    on = np.zeros_like(plane, dtype=bool)
    on[40:140, 40:120] = True               # 20% of the image is tissue
    rng = np.random.default_rng(0)
    plane[on] = rng.uniform(50, 400, on.sum())
    numbers = scan._overview_numbers(plane, on, ceiling=65535)
    assert numbers["zero_fraction"] < 0.01
    assert "near_zero_plane" not in numbers["flags"]


def test_the_mirror_speaks_the_viewers_cell_modes():
    from plexora.plugins.qc.server import mirror_script

    packet = {"kind": "artifact_confirm", "units": [{"project": "p", "type": "candidate",
                                                     "id": "c"}], "evidence": {}}
    unit = {"type": "candidate", "id": "c", "channel": "CD3", "bbox": [0, 0, 10, 10]}
    modes = [c["arguments"]["mode"] for c in mirror_script.script_for(
        packet, unit, None, current_project="p") if c["type"] == "set_cell_render_mode"]
    assert modes == ["none"]
    assert mirror_script.SETUP_IN_EFFECT["set_cell_render_mode"]({"cell_mode": "none"})


def test_a_check_review_shows_what_the_check_judged():
    """A score review drove nothing in the viewer: only candidates set
    channels. Registration shows its two cycles red and green, segmentation
    the DNA with the cells' outlines."""
    from plexora.plugins.qc.server import mirror_script

    calibration = {"nuclear": "DNA1", "channels": {n: {"window": [0.0, 1.0]}
                                                   for n in ("DNA1", "DNA2")}}
    refs = [{"project": "p", "type": "check", "id": "registration:DNA2"}]
    packet = {"kind": "score_review", "units": refs, "evidence": {}}
    unit = {"type": "check", "check": "registration", "channel": "DNA2", "reference": "DNA1"}
    script = mirror_script.script_for(packet, unit, calibration, current_project="p")
    shown = next(c for c in script if c["type"] == "set_channels")["arguments"]["channels"]
    assert [(c["name"], c["color"]) for c in shown] == list(
        zip(("DNA1", "DNA2"), mirror_script.REGISTRATION_COLORS))
    unit = {"type": "check", "check": "segmentation", "channel": "DNA1"}
    script = mirror_script.script_for({**packet, "units": [{**refs[0], "id": "s"}]}, unit,
                                      calibration, current_project="p")
    mode = next(c for c in script if c["type"] == "set_cell_render_mode")
    assert mode["arguments"]["mode"] == "outlines"


def test_the_reading_guide_names_each_schema_once():
    import json

    from plexora.plugins.qc.server import packets, schemas

    schemas_ = packets.reading_guide()["answer_schemas"]
    text = json.dumps(schemas_)
    # The class list lives in `agent_classes`, not in every field that takes one.
    assert text.count('"tissue_fold"') == 0
    assert "reading_guide.agent_classes" in text
    assert packets.reading_guide()["agent_classes"] == list(schemas.AGENT_CLASSES)


def test_a_library_refusal_is_an_internal_error(tmp_path):
    """Pillow refusing an inverted rectangle came back `invalid_input`, which
    reads as the agent's answer being wrong."""
    import os

    import plexora
    from plexora.agent.errors import as_agent_error

    inside = os.path.join(os.path.dirname(plexora.__file__), "_probe_draw.py")
    code = compile("from PIL import Image, ImageDraw\n"
                   "def draw():\n"
                   "    ImageDraw.Draw(Image.new('L', (4, 4))).rectangle((3, 0, 1, 1))\n",
                   inside, "exec")
    scope = {}
    exec(code, scope)
    try:
        scope["draw"]()
    except ValueError as exc:
        error = as_agent_error(exc)
    assert error.code == "internal_error"
    assert error.detail["called_from"].endswith("_probe_draw.py:3")
    try:
        raise ValueError("a bad argument of ours")
    except ValueError as exc:
        assert as_agent_error(exc).code == "invalid_input"


def test_a_place_only_one_cycle_has_is_not_misregistered():
    """Tissue lost in the later cycle (or a block of one cycle's background)
    disagrees everywhere and scored 1.0 -- the largest "misregistration" of
    the run lay outside the tissue."""
    from plexora.plugins.qc.server import registration

    a = np.zeros((120, 240), dtype=np.float32)
    b = np.zeros_like(a)
    for y in range(10, 110, 12):
        for x in range(10, 230, 12):
            a[y:y + 6, x:x + 6] = 1.0
    b[:, :120] = a[:, :120]                 # the right half is gone in cycle 2
    mapped = registration.mismatch_map(a, b, sigma_px=4, cell_px=10)
    nucleus = mapped["nucleus"]
    assert (nucleus[:, :10] >= registration.MAP_MIN_NUCLEUS).any()
    assert not (nucleus[:, 14:] >= registration.MAP_MIN_NUCLEUS).any()


def test_a_heat_panel_finer_than_its_pixels_draws(monkeypatch):
    """A 45k-px image's scan map has more cells than the panel has pixels:
    each cell's rectangle came out inverted and Pillow refused the sheet,
    which stuck the session."""
    from plexora.plugins.qc.server import sheets

    class Scan:
        def map(self, channel, metric):
            return np.ones((900, 900), dtype=np.float32)

        def shared(self, name):
            return None

        def tissue(self):
            return np.ones((900, 900), dtype=bool)

    mask = np.zeros((900, 900), dtype=bool)
    mask[100:140, 200:260] = True
    picture = sheets._heat_panel(Scan(), "DNA::median", mask, 288)
    assert picture.size == (288, 288)


def test_a_grid_answer_can_name_the_right_class():
    from plexora.plugins.qc.server import answers

    answer = answers.ArtifactGridAnswer(cells=["A1"], artifact_class="tissue_fold",
                                        severity="moderate")
    assert answer.artifact_class == "tissue_fold"


def _modules_result():
    return [
        {"index": 1, "channels": ["DNA_1", "CD3", "CD8"], "nuclear": "DNA_1"},
        {"index": 2, "channels": ["DNA_2", "CD20"], "nuclear": "DNA_2"}], {
        "seg_under": {"available": True, "state": "decided",
                      "decision": {"high": {"offset_steps": 0, "veto": False,
                                            "verdict": "accept"}}}}


def _seg_under_measurements(monkeypatch):
    """Segmentation QC's merge score on the table's first cells, without
    running Segmentation QC."""
    import numpy as np

    from plexora.plugins.qc.server.cells import calls

    def measured(ds, scan_meta, names):
        under = np.zeros(len(ds.table.geometry()))
        under[:20] = 0.9
        return {"seg_under": {"m_seg_under": under, "_column": "DNA_1", "_flag": 0.6}}

    monkeypatch.setattr(calls, "module_measurements", measured)


def test_a_dismissed_cell_reason_flags_nothing_and_undo_puts_it_back(tmp_path, monkeypatch):
    """After AutoQC the user could delete a wrong region but not a wrong cell
    reason, marker flag or channel verdict: every row the panel lists can now
    be set aside, recorded, and restored."""
    import json

    from plexora.agent import AgentSession, invoke, registry
    from plexora.plugins.qc.server import results
    from tests.qc_fixtures import make_qc_project

    def ok(answer):
        assert answer["ok"], json.dumps(answer.get("error"), default=str)[:2000]
        return answer["result"]

    registry.discover(["roi", "qc"])
    make_qc_project(tmp_path, artifacts=("cycle_dropout",))
    session = AgentSession()
    ok(invoke(session, "create_roi", {"project": "qcsynth", "category": "QC: Tissue fold",
                                      "geometry": {"type": "Polygon", "coordinates": [[
                                          [200, 200], [500, 200], [500, 500], [200, 500],
                                          [200, 200]]]}}))
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    _seg_under_measurements(monkeypatch)
    cycles, modules = _modules_result()
    with results.lock("qcsynth"):
        document = results.load("qcsynth")
        result = results.active(document)
        result["cycles"] = cycles
        result.setdefault("cells", {})["modules"] = modules
        result["channels"] = [{"name": "CD3", "status": "flagged", "reason": "looked dim"}]
        results.put_result(document, result)
        results.save("qcsynth", document)
    ok(invoke(session, "refresh_qc", {"project": "qcsynth"}))
    by_reason = results.active(results.load("qcsynth"))["cells"]["by_reason"]
    reason = next(r for r in by_reason if not r.startswith("region:"))

    # A region's cells follow the region: refused, pointing at the region.
    refused = invoke(session, "dismiss_qc_finding", {"project": "qcsynth",
                                                     "finding": "cell_reason",
                                                     "reason": "region:tissue_fold"})
    assert refused["error"]["code"] == "invalid_input"

    done = ok(invoke(session, "dismiss_qc_finding", {"project": "qcsynth",
                                                     "finding": "cell_reason",
                                                     "reason": reason}))
    assert done["dismissed"] and reason not in done["cells"]["by_reason"]
    assert "region:tissue_fold" in done["cells"]["by_reason"]
    cells = results.cells("qcsynth")
    assert not cells["reasons"].list.contains(reason).any()
    stored = results.active(results.load("qcsynth"))
    assert stored["user_dismissed"][0]["by"] == "user"
    assert stored["cells"]["evidence"][reason]["dismissed"]

    ok(invoke(session, "undo_operation", {"operation_id": done["receipt"]["operation_id"]}))
    stored = results.active(results.load("qcsynth"))
    assert not stored["user_dismissed"] and stored["cells"]["by_reason"][reason]

    channel = ok(invoke(session, "dismiss_qc_finding", {"project": "qcsynth",
                                                        "finding": "channel",
                                                        "channel": "CD3"}))
    shown = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["channels"]
    assert shown[0]["status"] == "clean" and shown[0]["user_state"]["was"] == "flagged"
    ok(invoke(session, "undo_operation", {"operation_id": channel["receipt"]["operation_id"]}))
    shown = ok(invoke(session, "get_qc_results", {"project": "qcsynth"}))["channels"]
    assert shown[0]["status"] == "flagged" and "user_state" not in shown[0]


def test_run_all_ticks_progress_between_detectors_and_stops_there(monkeypatch):
    """A 20-minute detector pass never ticked: the only sign of life was the
    scan's own "scanned" message, then silence until every detector had run.
    `run_all` now ticks once per detector, ahead of running it, and checks
    for a stop there too -- so a session stopped mid-pass never runs the
    rest of the detectors."""
    import pytest

    from plexora.plugins.qc.server import detectors as detectors_mod

    class _Stub:
        def __init__(self, name):
            self.name, self.version = name, "1"

        def available(self, context):
            return True, None

        def run(self, context):
            return []

    stubs = [_Stub("a"), _Stub("b"), _Stub("c")]
    ticks = []
    calls = {"cancelled": 0}

    def progress(done, total, name):
        ticks.append((done, total, name))

    def cancelled():
        calls["cancelled"] += 1
        if calls["cancelled"] == 2:
            raise RuntimeError("stopped from the viewer")

    monkeypatch.setattr(detectors_mod, "all_detectors", lambda: stubs)
    with pytest.raises(RuntimeError):
        detectors_mod.run_all(object(), progress=progress, cancelled=cancelled)
    assert ticks == [(0, 3, "a")] and calls["cancelled"] == 2


def test_the_bulk_progress_announcer_throttles(monkeypatch):
    """The bulk pass's only announcement was one deduped `phase: analyzing`
    with no progress at all. `_progress_announcer` must fold two calls this
    close together into one -- a 20-minute pass must not flood `qc.session`
    events -- unless the step's message changed, so it never looks stuck
    either."""
    from plexora.plugins.qc import capabilities_session
    from plexora.plugins.qc.server import bulk

    class _FakeEngine:
        def __init__(self):
            self.record = {"images": ["p"]}

        def progress(self):
            return {"bulk": {"job_id": "j", "state": "bulk_running"}}

    class _FakeContext:
        def __init__(self, engine):
            self.engine = engine

        def __enter__(self):
            return self.engine

        def __exit__(self, *exc):
            return False

    engine = _FakeEngine()
    seen = []
    clock = [0.0]
    monkeypatch.setattr(bulk, "engine_for", lambda call, session_id: _FakeContext(engine))
    monkeypatch.setattr(capabilities_session.TOOLS, "phase",
                        lambda call, record, session_id, phase, **payload: seen.append(payload))
    monkeypatch.setattr(bulk.time, "monotonic", lambda: clock[0])

    announce = bulk._progress_announcer(object(), "qs_1")
    announce("scanning", "CD3", done=1, total=10)
    announce("scanning", "CD3", done=2, total=10)      # same message, < 2s: folded
    clock[0] = 1.0
    announce("scanning", "CD4", done=3, total=10)      # message changed: at once
    clock[0] = 1.5
    announce("scanning", "CD4", done=4, total=10)      # same message, < 2s: folded
    clock[0] = 3.6
    announce("scanning", "CD4", done=5, total=10)      # same message, >= 2s later

    assert len(seen) == 3
    assert engine.record["bulk_progress"] == {"stage": "scanning", "message": "CD4",
                                              "done": 5, "total": 10}
    assert seen[0]["progress"]["bulk"]["job_id"] == "j"


def test_the_candidate_merge_only_compares_places_that_meet():
    """The aggregate detector raised ~5,400 specks over 40 channels and the
    pairwise merge ran whole-grid numpy for every pair: the bulk job sat at
    "scanned TubbIII" for twenty minutes. The pruned merge groups exactly as
    the pairwise one did."""
    from plexora.plugins.qc.server import candidates as cand
    from plexora.plugins.qc.server.detectors.base import Candidate

    rng = np.random.default_rng(3)
    raw = []
    for k in range(120):
        mask = np.zeros((60, 60), dtype=bool)
        y, x = rng.integers(0, 54, 2)
        h, w = rng.integers(1, 7, 2)
        mask[y:y + h, x:x + w] = True
        raw.append(Candidate(detector="aggregate", detector_version="1",
                             class_hint=("antibody_aggregate", "tissue_fold")[k % 2],
                             scope_hint="channel", channels=(f"M{k % 5}",), mask=mask,
                             score=float(rng.uniform(0.3, 1)), severity=0.5))

    def brute(items):
        items = [c for c in items if c.mask.any()]
        parent = list(range(len(items)))

        def find(i):
            while parent[i] != i:
                i = parent[i]
            return i
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if not cand.compatible(items[i], items[j]):
                    continue
                a, b = items[i].mask, items[j].mask
                iou, contain = cand._iou(a, b)
                ratio = max(a.sum(), b.sum()) / max(1, min(a.sum(), b.sum()))
                if iou >= cand.MERGE_IOU or (contain >= cand.MERGE_CONTAIN
                                             and ratio <= cand.MERGE_SIZE_RATIO):
                    parent[find(j)] = find(i)
        return sorted(sorted(id(items[i]) for i in range(len(items)) if find(i) == root)
                      for root in {find(i) for i in range(len(items))})

    expected = sorted(len(group) for group in brute(raw))
    assert max(expected) > 1                  # some did merge
    merged = cand.merge(raw)
    assert sorted(len(c.merged_from) or 1 for c in merged) == expected



def test_a_whole_tissue_panel_takes_the_overview_window_and_a_crop_the_cells():
    """The audit's tiles drew every marker black: the cell-anchored window is
    read at level 0, and a whole-tissue tile's coarse pixels average a bright
    cell away. A panel takes the window for its scale."""
    from plexora.agent.evidence import calibration
    from plexora.agent.render_spec import ChannelSpec
    from plexora.plugins.qc.server import sheets

    stats = {"p30": 10.0, "p50": 200.0, "p99": 3000.0, "p995": 4000.0, "p999": 6000.0,
             "max": 30000.0, "cell_cap": 11000.0,
             "cell_window": {"low": 300.0, "high": 12000.0}}
    record = {"channels": {"CD4": {"role": "marker", "window": [300.0, 12000.0],
                                   "stats": stats}}}
    overview = calibration.overview_window(record, "CD4")
    assert overview == [200.0, 4000.0]
    fine, coarse = calibration.SCALE_BLEND
    assert calibration.window_at(record, "CD4", fine) == [300.0, 12000.0]
    assert calibration.window_at(record, "CD4", coarse * 4) == overview
    middle = calibration.window_at(record, "CD4", (fine * coarse) ** 0.5)
    assert 4000.0 < middle[1] < 12000.0 and 200.0 < middle[0] < 300.0

    channel = sheets._channel("CD4", "#ffffff", record)
    assert channel.window == [300.0, 12000.0]
    whole, = sheets._at_scale([channel], {"x": 0, "y": 0, "width": 40000, "height": 40000}, 256)
    crop, = sheets._at_scale([channel], {"x": 0, "y": 0, "width": 300, "height": 300}, 384)
    assert whole.window == overview and crop.window == [300.0, 12000.0]
    # No stats (an old or hand-made record): the calibrated window, unchanged.
    bare = {"channels": {"CD4": {"window": [1.0, 2.0]}}}
    assert calibration.window_at(bare, "CD4", 1000.0) == [1.0, 2.0]
    assert sheets._at_scale([ChannelSpec(name="CD4", window=[1.0, 2.0])],
                            {"x": 0, "y": 0, "width": 9000, "height": 9000}, 100)[0].window \
        == [1.0, 2.0]


def test_the_mirror_turns_hd_on_once_and_scales_the_tab_to_the_audit():
    from plexora.plugins.qc.server import mirror_script

    stats = {"p30": 10.0, "p50": 200.0, "p99": 3000.0, "p995": 4000.0, "p999": 6000.0,
             "max": 30000.0, "cell_window": {"low": 300.0, "high": 12000.0}}
    record = {"channels": {"CD4": {"role": "marker", "window": [300.0, 12000.0],
                                   "stats": stats}}}
    packet = {"kind": "channel_audit", "units": [{"project": "p", "type": "channel",
                                                  "id": "CD4"}],
              "evidence": {"rows": [{"channel": "CD4"}]}}
    script = mirror_script.script_for(packet, None, record, current_project="p")
    assert script[0] == {"type": "set_hd_mode", "arguments": {"enabled": True}}
    shown = next(c for c in script if c["type"] == "set_channels")["arguments"]["channels"]
    assert shown[0]["window"] == [200.0, 4000.0]
    again = mirror_script.script_for(packet, None, record, current_project="p",
                                     viewer_state={"project": "p", "hd_mode": True})
    assert "set_hd_mode" not in [c["type"] for c in again]
