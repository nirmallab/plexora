"""The review record a gate's provenance carries (`autogate.explain.build`):
only what the unit recorded as drawn or leaned on is named."""

import json

import numpy as np

from plexora.plugins.gating.server.autogate import explain

CHANNELS = {"CD3": "CD3_ch", "CD4": "CD4_ch", "CD8": "CD8_ch", "CD20": "CD20_ch",
            "CD45": "CD45_ch", "CD11c": "CD11c_ch", "CD68": "CD68_ch"}


def _unit(**extra):
    unit = {"marker": "CD8", "project": "p", "path": "panel", "packets": ["pk_0001", "pk_0002"],
            "used": {"packets": 2}, "final": 6.42,
            "context": {"partners": [{"marker": "CD3", "relation": "subset"},
                                     {"marker": "CD4", "relation": "exclusive"},
                                     {"marker": "CD20", "relation": "exclusive"},
                                     {"marker": "CD45", "relation": "subset"}]}}
    unit.update(extra)
    return unit


def _build(unit, **kw):
    kw.setdefault("state", "accepted")
    kw.setdefault("method", "ai_panel")
    kw.setdefault("confidence", "moderate")
    return explain.build(unit, channel_of=CHANNELS.get, **kw)


def _by_marker(review):
    return {s["marker"]: s for s in review["supporting"]}


def test_a_chosen_carrier_is_its_parent_lineage_and_a_skipped_one_is_not_used():
    unit = _unit(path="t2", packets=[], used={"packets": 0}, marker="PD1",
                 carriers={"listed": [{"marker": "CD3"}, {"marker": "CD45"}],
                           "chosen": [{"marker": "CD3", "enrichment": 4.21, "grade": "high",
                                       "fit": {"usable": True, "low": 6.6}}],
                           "skipped": [{"marker": "CD45", "why": "not gated in this run"}],
                           "start": {"low": 6.46, "before": 6.2}})
    review = _build(unit, method="ai_accepted")
    cd3 = _by_marker(review)["CD3"]
    assert cd3["kind"] == "parent_child" and cd3["roles"] == ["carrier"]
    assert "x4.2" in cd3["influence"] and "6.6" in cd3["influence"] \
        and "6.46" in cd3["influence"]
    assert cd3["channel"] == "CD3_ch" and cd3["display"] and cd3["grade"] == "high"
    assert "CD45" not in _by_marker(review)
    assert review["not_used"] == [{"marker": "CD45", "relation": "subset",
                                   "why": "not gated in this run"}]
    assert "Parent lineage CD3" in review["notes"]


def test_a_not_expected_tile_drawn_twice_and_plotted_is_a_mutual_exclusion():
    drawn = [{"look": 1, "kind": "cell_panel",
              "tiles": [{"marker": "CD4", "channel": "CD4 (tile)", "panel": "anti",
                         "relation": "exclusive", "gate": 5.1}],
              "plot": {"marker": "CD4", "relation": "exclusive"}},
             {"look": 2, "kind": "cell_panel",
              "tiles": [{"marker": "CD4", "channel": "CD4 (tile)", "panel": "anti",
                         "relation": "exclusive"}], "plot": None}]
    review = _build(_unit(partners_drawn=drawn))
    cd4 = _by_marker(review)["CD4"]
    assert cd4["kind"] == "mutual_exclusion" and cd4["looks"] == 2
    assert cd4["roles"] == ["tile_not_expected", "plot"]
    assert cd4["channel"] == "CD4 (tile)"
    assert "2 looks" in cd4["influence"] and "1 picture" in cd4["influence"]
    assert "Should exclude CD4" in review["notes"]


def test_a_look_that_was_never_sent_is_not_counted():
    drawn = [{"look": 3, "kind": "cell_panel",
              "tiles": [{"marker": "CD4", "channel": "CD4_ch", "panel": "anti",
                         "relation": "exclusive"}]}]
    assert _build(_unit(partners_drawn=drawn))["supporting"] == []


def test_a_subset_any_group_is_one_entry_a_member_drawn_together():
    tiles = [{"marker": m, "channel": CHANNELS[m], "panel": "co", "relation": "subset_any",
              "members": ["CD3", "CD20"]} for m in ("CD3", "CD20")]
    review = _build(_unit(marker="PDL1", partners_drawn=[
        {"look": 1, "kind": "cell_panel", "tiles": tiles, "plot": None}]))
    rows = _by_marker(review)
    assert set(rows) == {"CD3", "CD20"}
    assert "drawn together with CD20" in rows["CD3"]["influence"]
    assert "drawn together with CD3" in rows["CD20"]["influence"]
    assert all(r["relation"] == "subset_any" and r["kind"] == "parent_child"
               for r in rows.values())
    assert "Expected in any of" in review["notes"]


def test_no_partner_says_so():
    review = _build(_unit())
    assert review["supporting"] == []
    assert "No partner was drawn or leaned on: decided from CD8's own distribution and " \
           "2 looks at cells" in review["notes"]
    unlooked = _build(_unit(path="t1", packets=[], used={"packets": 0}), method="gmm",
                      confidence="high")
    assert "without a look" in unlooked["notes"]
    assert unlooked["notes"].startswith("Accepted at the Auto gate")


def test_what_was_only_considered_never_supports_the_gate():
    unit = _unit(
        carriers={"chosen": [], "skipped": [{"marker": "CD3", "why": "x0.9 once cleaned"}]},
        evidence_plan={"used": [{"marker": "CD45", "relation": "subset", "grade": "high",
                                 "weight": 1.0}],
                       "avoided": [{"marker": "CD20", "relation": "exclusive",
                                    "why": "failed"}],
                       "not_gated_yet": ["CD11c"]},
        scoring={"basis": "mixture", "partners": [{"marker": "CD68", "relation": "exclusive",
                                                    "weight": 0.5}]},
        start={"source": "gmm", "low": 6.0})
    review = _build(unit)
    assert review["supporting"] == []
    assert [n["marker"] for n in review["not_used"]] == ["CD3", "CD20", "CD11c"]
    assert review["not_used"][2]["why"] == "not gated yet"


def test_a_scored_start_names_its_partners_but_does_not_switch_them_on():
    unit = _unit(scoring={"basis": "mixture", "partners": [
        {"marker": "CD68", "relation": "exclusive", "weight": 0.5, "down_weighted": True}]},
        start={"source": "scoring", "low": 6.0})
    cd68 = _by_marker(_build(unit))["CD68"]
    assert cd68["roles"] == ["scored_start"] and cd68["display"] is False
    assert "weight 0.50, down-weighted" in cd68["influence"]


def test_a_review_gate_says_why_and_what_stays_uncertain():
    unit = _unit(
        needs_review=True, reason="cell panels: the reads did not settle",
        regression={"ok": False, "failed": ["partners"], "checks": [
            {"name": "partners", "ok": False,
             "value": [{"partner": "CD4", "relation": "exclusive", "excess": 0.31}]}]},
        reference_gates=[{"marker": "CD4", "relation": "exclusive", "gate": 5.1,
                          "confidence": "high"}],
        hypotheses={"observations": [{}, {}], "support": {"posterior": {"plateau": [
            {"low": 6.4, "p": 0.55}, {"low": 6.9, "p": 0.35}]}}},
        notes=["dim membrane rings on most cells"])
    review = _build(unit, state="manual_review_recommended", method="needs_review",
                    confidence="manual_review")
    assert review["needs_review"] is True
    assert review["uncertainty"][0] == "cell panels: the reads did not settle"
    assert "failed its own checks: partners" in review["uncertainty"]
    assert any(u.startswith("the reads leave 6.4 (P 0.55)") for u in review["uncertainty"])
    assert review["uncertainty"][-1].startswith("the reader's last note: dim membrane")
    assert review["notes"].startswith("Written for review at the best gate reached (6.42): "
                                      "cell panels: the reads did not settle.")
    assert "Needs review: failed its own checks: partners" in review["notes"]
    cd4 = _by_marker(review)["CD4"]
    assert cd4["roles"] == ["purity_check"] and "the check failed on it (0.31 excess)" \
        in cd4["influence"]


def test_a_failed_stain_names_no_partner():
    unit = _unit(reason="no stain on any cell", packets=["pk_0001"], used={"packets": 1})
    review = _build(unit, state="technically_failed", method="failed_marker",
                    confidence="failed_qc")
    assert review["notes"].startswith("The stain failed: no stain on any cell.")
    assert review["supporting"] == [] and "No partner" not in review["notes"]


def test_a_conditional_gate_names_its_lineage():
    unit = _unit(path="t2", condition={"within": "CD45", "relation": "subset",
                                       "n_positive_within": np.int64(812),
                                       "n_positive_outside": np.int64(40)})
    review = _build(unit, method="ai_conditional")
    cd45 = _by_marker(review)["CD45"]
    assert cd45["roles"] == ["condition"] and "812 positives inside, 40 outside" \
        in cd45["influence"]
    assert review["notes"].startswith("Fitted within CD45+ cells only, after 2 looks.")


def test_a_contaminated_lineage_is_a_check_and_an_uncertainty():
    row = {"partner": "CD20", "at_top": np.float64(0.62), "prevalence": np.float64(0.12),
           "excess": 0.4, "contaminated": True}
    review = _build(_unit(lineage=[row], lineage_suspect=row))
    cd20 = _by_marker(review)["CD20"]
    assert cd20["roles"] == ["lineage_check"] and cd20["kind"] == "mutual_exclusion"
    assert "62% of the brightest CD8 positives sit on CD20, against 12% of all cells" \
        in cd20["influence"]
    assert any("62% of the brightest positives sit on CD20" in u for u in review["uncertainty"])


def test_the_record_is_bounded_and_round_trips_as_plain_json():
    many = [{"look": 1, "kind": "cell_panel", "tiles": [
        {"marker": f"M{i}", "channel": f"M{i}_ch", "panel": "co" if i % 2 else "anti",
         "relation": "subset" if i % 2 else "exclusive", "gate": np.float32(1.5)}
        for i in range(12)], "plot": None}]
    unit = _unit(partners_drawn=many, reason="x " * 400, needs_review=True,
                 confidence_notes=["y " * 300] * 6,
                 carriers={"chosen": [{"marker": "CD3", "enrichment": np.float64(3.5),
                                       "fit": {"usable": True, "low": np.float32(6.1)}}],
                           "start": {"low": np.float64(6.2)}})
    review = _build(unit, state="manual_review_recommended", method="needs_review")
    assert len(review["supporting"]) == explain.MAX_SUPPORTING
    assert len(review["notes"]) <= explain.MAX_NOTES
    assert len(review["uncertainty"]) <= explain.MAX_UNCERTAINTY
    assert all(len(u) <= explain.MAX_LINE for u in review["uncertainty"])
    back = json.loads(json.dumps(review))
    assert back == review
    cd3 = _by_marker(back)["CD3"]
    assert isinstance(cd3["enrichment"], float) and isinstance(back["looks"], int)
