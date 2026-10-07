"""What the first delegated AutoQC run on a large image (lsp11385, 45k x 53k
px, 40 channels, 2026-10-04) got wrong, pinned so it stays fixed.

The run flagged all 40 channels and warned 42% of the cells. Not the models:
one tile-seam candidate merged over forty audit rows was kept by a row
`uncertain` about its own aggregates, could not be judged at cell scale, and
its manual-review region warned 81k cells; and the deeper looks drew black (a sparse stain at a window
anchored on bright cells) or biased (cycle 1 twice as bright as cycle 2).
"""

import numpy as np


# -- the audit ----------------------------------------------------------------------------


class _Engine:
    """Just what `transitions.apply_audit` touches."""

    def __init__(self, units):
        self.record = {"units": {f"p::{u['type']}::{u['id']}": u for u in units}}
        self.settled = 0

    def unit_key_of(self, ref):
        return f"{ref['project']}::{ref['type']}::{ref['id']}"

    def units_of(self, type_, project=None):
        return [u for u in self.record["units"].values() if u["type"] == type_]

    def channel_unit(self, name, project=None):
        return self.record["units"].get(f"p::channel::{name}")

    def close(self, unit, state, reason):
        unit["state"], unit["reason"] = state, reason

    def settle_channels(self):
        self.settled += 1

    #: The image's channels, for the class rules an audit note runs.
    call = type("Call", (), {"session": type("Session", (), {"project": staticmethod(
        lambda name: type("Record", (), {"image": type("Image", (), {
            "real_channels": [{"name": "Ki67"}, {"name": "Ecad"}]})})())})()})()


def _audit(ki67_where):
    from plexora.plugins.qc.server import transitions
    from plexora.plugins.qc.server.answers import ChannelAuditAnswer

    channels = [{"type": "channel", "project": "p", "id": n, "state": "awaiting_audit"}
                for n in ("Ki67", "Ecad")]
    aggregate = {"type": "candidate", "project": "p", "id": "cand_agg", "label": "c8",
                 "class_hint": "saturation_or_clipping", "audit_channels": ["Ki67"],
                 "score": 0.5, "state": "awaiting_audit"}
    seam = {"type": "candidate", "project": "p", "id": "cand_seam", "label": "c1",
            "class_hint": "stitching_or_tile_seam", "audit_channels": ["Ki67", "Ecad"],
            "score": 1.0, "state": "awaiting_audit"}
    engine = _Engine(channels + [aggregate, seam])
    packet = {"units": [{"project": "p", "type": "channel", "id": n} for n in ("Ki67", "Ecad")],
              "evidence": {"rows": [
                  {"channel": "Ki67", "candidates": [{"label": "c8", "id": "cand_agg"},
                                                     {"label": "c1", "id": "cand_seam"}]},
                  {"channel": "Ecad", "candidates": [{"label": "c1", "id": "cand_seam"}]}]}}
    answer = ChannelAuditAnswer(verdicts={
        "Ki67": {"verdict": "uncertain", "class_hint": "saturation_or_clipping",
                 "where": ki67_where},
        "Ecad": {"verdict": "clean"}})
    transitions.apply_audit(engine, packet, answer)
    return aggregate, seam


def test_an_uncertain_row_keeps_only_the_outlines_it_names():
    """A row unsure of its aggregates kept a seam merged over forty rows that
    the other rows had called clean."""
    aggregate, seam = _audit(["c8"])
    assert aggregate["state"] == "awaiting_confirm"
    assert seam["state"] == "dismissed"


def test_an_uncertain_row_naming_nothing_keeps_every_outline():
    aggregate, seam = _audit([])
    assert aggregate["state"] == "awaiting_confirm"
    assert seam["state"] == "awaiting_confirm"


def _audit_bare(verdict):
    """One row with no outline on it, answered `verdict`."""
    from plexora.plugins.qc.server import transitions
    from plexora.plugins.qc.server.answers import ChannelAuditAnswer

    channel = {"type": "channel", "project": "p", "id": "Ki67", "state": "awaiting_audit"}
    engine = _Engine([channel])
    packet = {"units": [{"project": "p", "type": "channel", "id": "Ki67"}],
              "evidence": {"rows": [{"channel": "Ki67", "candidates": []}]}}
    transitions.apply_audit(engine, packet, ChannelAuditAnswer(verdicts={"Ki67": verdict}))
    return engine, channel


def test_a_suspicious_row_with_nothing_outlined_flags_the_channel_and_opens_nothing():
    """Staining is settled on the row: no candidate is opened over the tissue."""
    engine, channel = _audit_bare({"verdict": "suspicious", "class_hint": "antibody_aggregate",
                                   "where": ["elsewhere"]})
    assert not engine.units_of("candidate")
    note = channel["audit_note"]
    assert note["state"] == "flagged" and note["class"] == "antibody_aggregate"
    assert "antibody aggregate" in note["reason"] and "no outline" in note["reason"]


def test_an_uncertain_row_with_nothing_outlined_is_left_for_manual_review():
    engine, channel = _audit_bare({"verdict": "uncertain"})
    assert not engine.units_of("candidate")
    assert channel["audit_note"]["state"] == "manual_review_recommended"


# -- channels and manual review ---------------------------------------------------------


def test_a_manual_review_over_much_of_the_tissue_is_noted_not_warned():
    """An unjudgeable envelope over the epithelium of both sections (12% of
    the tissue) warned 81,227 of 202,049 cells."""
    from plexora.plugins.qc.server import strictness

    big = {"state": "manual_review_recommended", "measurement": {"tissue_fraction": 0.124}}
    small = {"state": "manual_review_recommended", "measurement": {"tissue_fraction": 0.002}}
    assert set(strictness.actions_by_preset(big).values()) == {"ignore"}
    assert set(strictness.actions_by_preset(small).values()) == {"warn"}
    assert strictness.manual_review_action(None)["action"] == "warn"
    decided = strictness.decide_artifact({"artifact_class": "uncertain_manual_review"},
                                         {"tissue_fraction": 0.5},
                                         strictness.thresholds("strict"))
    assert decided["action"] == "ignore"


# -- the evidence ---------------------------------------------------------------------


class _Scan:
    """A 10 x 10 map grid of 100 px cells: tissue on the left half."""

    def __init__(self, p10, p99, median):
        self.grid = {"cell_full_px": 100.0, "image_size": [1000, 1000]}
        self._maps = {"p10": p10, "p99": p99, "median": median}
        self.meta = {"cycles": {"cycles": [{"nuclear": "DNA1"}, {"nuclear": "DNA2"}]}}

    def map(self, name, metric):
        return self._maps[metric]

    def tissue(self):
        t = np.zeros((10, 10), dtype=np.float32)
        t[:, :5] = 1.0
        return t

    def nuclear(self):
        return "DNA1"


def _maps():
    p10 = np.zeros((10, 10)); p99 = np.zeros((10, 10)); median = np.zeros((10, 10))
    p10[:, :5], median[:, :5], p99[:, :5] = 150.0, 260.0, 500.0   # dim diffuse tissue
    return p10, p99, median


def test_a_crop_the_calibrated_window_draws_black_is_stretched_to_what_is_there():
    """CD45's cell-anchored window [499, 16014] drew a diffuse patch of ~250
    black, so the agent said cannot_tell three times."""
    from plexora.plugins.qc.server import sheets

    scan = _Scan(*_maps())
    window = sheets.stretch_window(scan, "CD45", {"x": 100, "y": 100, "width": 250,
                                                  "height": 250})
    assert window == [150.0, 500.0]


def test_an_empty_crop_is_not_stretched_into_noise():
    """Off the tissue, or no brighter than the tissue's own dim end, a crop
    stays black: empty, and truly so."""
    from plexora.plugins.qc.server import sheets

    p10, p99, median = _maps()
    p99[:, 7] = 3.0                                     # padding noise on the slide
    scan = _Scan(p10, p99, median)
    assert sheets.stretch_window(scan, "CD45", {"x": 650, "y": 100, "width": 250,
                                                "height": 250}) is None
    p99[:, 2] = 200.0                                   # below the tissue's p10 median
    p10[:, 2] = 150.0
    assert sheets.stretch_window(scan, "CD45", {"x": 200, "y": 100, "width": 90,
                                                "height": 90}) is None


def test_two_cycles_nuclear_stains_are_drawn_at_matched_windows():
    """The reference nuclear stain keeps one dim overview window, a later
    cycle's is anchored on its cells: drawn each at its own, cycle 1 was ~2x
    brighter at cell scale and every red / green crop read as cycle-2 loss."""
    from plexora.agent.render_spec import ChannelSpec
    from plexora.plugins.qc.server import sheets

    calibration = {"channels": {
        "DNA1": {"window": [352.0, 3224.0], "stats": {"p50_tissue": 429.0}},
        "DNA2": {"window": [1194.0, 5238.0], "stats": {"p50_tissue": 399.0}}}}
    comparison = ChannelSpec(name="DNA2", color="#2bd46f", window=[1194.0, 5238.0])
    reference = sheets.matched_reference(calibration, "DNA1", comparison, "#ff2d2d")
    ratio = 399.0 / 429.0
    assert reference.window == [1194.0 / ratio, 5238.0 / ratio]
    # Two stains too unlike to match keep their own windows.
    calibration["channels"]["DNA2"]["stats"]["p50_tissue"] = 4000.0
    alone = sheets.matched_reference(calibration, "DNA1", comparison, "#ff2d2d")
    assert alone.window != reference.window
    assert sheets.cycle_nuclear(_Scan(*_maps()), "DNA2")
    assert not sheets.cycle_nuclear(_Scan(*_maps()), "DNA1")


def test_narration_names_a_compacted_channel_set_in_words():
    """The brief compacts a cycle's channels to "cycle 2"; narration indexed
    it and the agent card read "a suspected tissue loss in a cycle in c."."""
    from plexora.plugins.qc.server import packets

    def said(channels):
        return packets.narrate({"kind": "artifact_confirm", "evidence": {"candidate": {
            "channels": channels, "class_hint": "cycle_specific_tissue_loss"}}})

    assert said("cycle 2").endswith("in cycle 2.")
    assert said("all_channels").endswith("in every channel.")
    assert said(["CD45", "CD3e"]).endswith("in CD45.")
    assert said([]).endswith("in this image.")


def test_the_agent_card_names_a_compacted_channel_set_in_words():
    """The card's subject joined the characters of "cycle 2": "c, y +5"."""
    from plexora.plugins.qc.capabilities_session import packet_subject

    def subject(channels):
        return packet_subject({"kind": "artifact_confirm", "evidence": {"candidate": {
            "label": "c44", "channels": channels,
            "class_hint": "cycle_specific_tissue_loss"}}})

    assert subject("cycle 2") == "c44 · tissue loss in a cycle · cycle 2"
    assert subject("all_channels").endswith("· every channel")
    assert subject(["CD3", "CD4", "CD8"]) == "c44 · tissue loss in a cycle · CD3, CD4 +1"
