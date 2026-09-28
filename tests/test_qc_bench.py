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
