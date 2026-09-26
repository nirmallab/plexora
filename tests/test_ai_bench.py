"""`plexora ai bench gating`: the arms run, the scoring is right, files land."""

import json

import numpy as np
import pytest

from plexora.ai import bench


def test_classification_scores_a_perfect_and_a_wrong_gate():
    values = np.array([1, 2, 3, 10, 11, 12], dtype=np.float32)
    truth = values > 5
    perfect = bench.classification(values, truth, 5.0)
    assert perfect["f1"] == 1.0 and perfect["kappa"] == 1.0 and perfect["mcc"] == 1.0
    low = bench.classification(values, truth, 1.5)
    assert low["fp"] == 2 and low["fraction_error"] == pytest.approx(2 / 6)
    assert bench.code_agreement({"A": values}, {"A": truth}, {"A": 5.0}) == 1.0


def test_the_synthetic_benchmark_runs_every_arm(tmp_path):
    rows = bench.run_synthetic(["easy"], "oracle", markers=("CD3", "CD8", "CD4"), grid=16,
                               size=640)
    arms = {row["arm"] for row in rows}
    assert arms == {"gmm", "profile", "session"}
    summary = bench.summarise(rows)
    # The oracle accepts a gate within ~1% of the cells of the best one, so a
    # session may sit a cell or two from an already-good GMM gate -- never far.
    assert summary["session"]["code_agreement"] >= summary["gmm"]["code_agreement"] - 0.02
    assert summary["session"]["code_agreement"] > 0.9
    assert summary["cost"]["packets"] >= 1
    out = tmp_path / "bench"
    assert bench.bench_command(synthetic=["easy"], markers=["CD8"], out=out, grid=12,
                               size=480, emit=lambda *_: None) == 0
    saved = json.loads((out / "results.json").read_text())
    assert saved["summary"] and (out / "summary.md").read_text().startswith("# Synthetic")
