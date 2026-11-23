"""The stain-check and visual-pass benches (`plexora.ai.stain_bench`,
`plexora.ai.visual_bench`): readings settle as a session settles them, the
scores the gateway's bars read, and the visual pass scored on painted
regions."""

import json

import numpy as np
import pytest

from plexora.ai import stain_bench, visual_bench

LABELS = [{"image": "img", "channel": c, "label": label} for c, label in (
    ("A", "failed"), ("B", "failed"), ("C", "real"), ("D", "real"), ("E", "weak"), ("F", "real"))]


def _answer(verdict):
    return json.dumps({"expectation": "x", "observed": "y", "structure": "no", "verdict": verdict,
                       "why": "z"})


def _bench(tmp_path, readings):
    items = [{"project": "img", "channel": c, "folder": f"img/{c}"} for c in readings]
    (tmp_path / "items.json").write_text(json.dumps({"bench_version": stain_bench.VERSION,
                                                     "items": items}))
    for channel, said in readings.items():
        folder = tmp_path / "img" / channel
        folder.mkdir(parents=True)
        (folder / "question.txt").write_text("q")
        for n, verdict in enumerate(said, 1):
            (folder / f"answer_{n}.json").write_text(
                verdict if verdict.startswith("{") or verdict == "garbage" else _answer(verdict))
    return tmp_path


def test_a_failed_first_reading_is_read_again_blind_until_it_settles(tmp_path):
    bench = _bench(tmp_path, {"A": ["failed"], "B": ["failed", "real"], "C": ["real"],
                              "D": ["failed", "failed"], "E": [], "F": ["failed", "weak", "weak"]})
    due = {q.parent.name: a.name for q, a in stain_bench.pending(bench)}
    # A's first `failed` wants a second reader, B's split a third; E has none yet.
    assert due == {"A": "answer_2.json", "B": "answer_3.json", "E": "answer_1.json"}


def test_the_score_holds_failures_caught_against_working_stains_called_failed(tmp_path):
    bench = _bench(tmp_path, {"A": ["failed", "failed"], "B": ["failed", "real", "real"],
                              "C": ["real"], "D": ["failed", "real", "failed"],
                              "E": ["failed", "failed"], "F": ["weak"]})
    m = stain_bench.score(bench, LABELS)["metrics"]
    assert m["failed_recall"] == 0.5          # A caught; B settled real
    assert m["real_failed_rate"] == round(1 / 3, 4)   # D, best of three
    assert m["weak_failed_rate"] == 1.0       # reported, not barred
    assert m["invalid_answer_rate"] == 0 and m["failure_rate"] == 0


def test_an_unparsable_answer_or_an_unsettled_channel_is_counted(tmp_path):
    bench = _bench(tmp_path, {"A": ["garbage"], "B": ["failed"], "C": ["real"], "D": ["real"],
                              "E": ["weak"], "F": ["real"]})
    m = stain_bench.score(bench, LABELS)["metrics"]
    assert m["invalid_answer_rate"] > 0 and m["failure_rate"] == round(2 / 6, 4)


def test_the_bench_labels_are_the_owners_two_images():
    labels = stain_bench.load_labels()
    assert {r["image"] for r in labels} == {"lsp35766_p193_2", "imc-immucan-example"}
    failed = sorted(r["channel"] for r in labels if r["label"] == "failed")
    assert len(failed) == 9 and "panCK" in failed and "LAG3 / LAG33" in failed


def test_visual_counts_match_a_painted_region_and_fault_an_outline_on_intact_tissue():
    grid = {"shape": [10, 10], "cell_full_px": 10}
    mask = np.zeros((100, 100), bool)
    mask[20:60, 20:60] = True
    truth = {"regions": [{"class": "tissue_fold", "mask": mask}]}
    on = np.zeros((10, 10), bool)
    on[2:6, 2:6] = True
    off = np.zeros((10, 10), bool)
    off[8:10, 8:10] = True
    counted = visual_bench._counts([{"class": "tissue_fold", "mask": on},
                                    {"class": "tissue_fold", "mask": off}], truth, grid)
    assert (counted["matched"], counted["scored"], counted["on"], counted["outlines"]) == (1, 1, 1, 2)
    # An outline of a class that cannot be a fold does not find it.
    wrong = visual_bench._counts([{"class": "out_of_focus", "mask": on}], truth, grid)
    assert wrong["matched"] == 0


@pytest.mark.pro
def test_the_truth_reader_outlines_every_painted_region(tmp_path, monkeypatch):
    from plexora import paths
    from plexora.agent import AgentSession, registry
    from plexora.plugins.qc.server import refine_sam

    if not refine_sam.available():
        pytest.skip("magic select is not installed")
    monkeypatch.setenv("PLEXORA_DATA_PATH", str(tmp_path))
    paths.reset()
    registry.discover(["roi", "qc"])
    scenes = (("fold", ("fold",), 21), ("clean", (), 23))
    visual_bench.prepare(tmp_path, scenes)
    session = AgentSession(table_limit=4)
    try:
        sessions = {}
        for scene, _a, _s in scenes:
            sid = visual_bench.start(session, scene)
            visual_bench.drive_truth(session, sid, scene, visual_bench.truth_of(tmp_path, scene))
            sessions[scene] = sid
        m = visual_bench.score(tmp_path, sessions, session=session)["metrics"]
    finally:
        session.close()
    assert m["region_recall"] == 1.0 and m["region_precision"] == 1.0 and m["failure_rate"] == 0
