"""`plexora ai bench qc`: the synthetic benchmark runs, and the numbers hold.

What is pinned is the floor a change must not go below: the detectors find
every painted artifact, and a careful agent keeps the regions that are real,
drops the ones that are not, and removes almost no good cell.
"""

import pytest

from plexora.ai import bench_qc


def test_the_detectors_find_every_painted_artifact():
    rows = bench_qc.run_synthetic(["saturation", "dropout"], arms=("detect",))
    summary = bench_qc.summarise(rows)
    assert summary["detect"]["region_recall"] == 1.0


@pytest.mark.paid
def test_a_careful_agent_keeps_the_real_regions_and_the_good_cells():
    rows = bench_qc.run_synthetic(["clean", "saturation", "mixed"], "oracle",
                                  arms=("detect", "session"))
    summary = bench_qc.summarise(rows)
    session, detect = summary["session"], summary["detect"]
    assert session["region_recall"] >= 0.9, rows
    assert session["region_precision"] >= detect["region_precision"], summary
    assert session["false_removal"] is not None and session["false_removal"] < 0.08, rows
    assert session["channel_accuracy"] >= 0.9, rows
    text = bench_qc.to_markdown(summary, rows, title="t")
    assert "| session |" in text and "| detect |" in text


@pytest.mark.paid
def test_a_lazy_agent_finds_less_than_a_careful_one():
    careful = bench_qc.summarise(bench_qc.run_synthetic(["mixed"], "oracle",
                                                        arms=("session",)))
    lazy = bench_qc.summarise(bench_qc.run_synthetic(["mixed"], "lazy", arms=("session",)))
    assert (lazy["session"]["region_recall"] or 0) < careful["session"]["region_recall"]


@pytest.mark.paid
def test_traced_regions_match_the_artifacts_better_than_their_envelopes():
    # (Aggregates are judged per channel, never outlined: no region to trace.)
    rows = bench_qc.run_synthetic(["fold", "saturation", "damage"], "oracle",
                                  arms=("session",))
    for row in rows:
        assert row["region_iou_px"] is not None, row
        assert row["region_iou_px"] >= row["envelope_iou_px"], row
    summary = bench_qc.summarise(rows)["session"]
    # The aggregates' specks traced inside their envelope were the gap; the
    # physical artifacts left are the Artifact Detector's own snug objects.
    assert summary["region_iou_px"] >= summary["envelope_iou_px"], summary
    assert summary["excess_fraction"] < 0.5, rows
    assert "region_iou_px" in bench_qc.to_markdown(bench_qc.summarise(rows), rows, title="t")


@pytest.mark.paid
def test_staining_is_found_per_channel_and_a_failed_stain_fails_its_marker():
    """Staining is judged per channel, never outlined: the aggregates'
    channel is not called clean, and a failed stain's marker is unreliable
    in every cell."""
    rows = bench_qc.run_synthetic(["failed_channel", "aggregates"], "oracle",
                                  arms=("session",))
    by = {row["scenario"]: row for row in rows}
    assert by["failed_channel"]["failed_marker_flagged"] == 1.0, rows
    assert by["aggregates"]["staining_channel_recall"] == 1.0, rows
