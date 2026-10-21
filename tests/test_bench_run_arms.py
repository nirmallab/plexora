"""tools/bench_gates/run_arms.py: the live campaign driver, with no model.

What is pinned: the plan (arms from `bench.VARIANTS`, a seed per run shared by
the arms, the arms' order turning, every session propose with reuse_answers
off); a dry run that prints the sessions and writes nothing; the refusal of a
live data directory; the 1 MB record rule, trimmed records scoring as the
originals do; and one campaign driven end to end through the harness against
the stand-in gateway, a scripted reader answering from the scene's truth.
"""

import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "bench_gates" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_arms = _load("run_arms")
score_gap = _load("score_gap")

COMMON = {"reuse_answers": False, "display_check": "off", "on_limit": "extend"}


def _registry(root, *names):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({n: {} for n in names}), encoding="utf-8")
    return root


def _argv(root, records, *extra, images="img1,img2"):
    return ["--campaign", "c1", "--images", images, "--data-root", str(root),
            "--records-root", str(records), *extra]


# -- the plan -------------------------------------------------------------------------------


def test_each_session_has_its_arms_options_and_its_runs_seed():
    arms = run_arms.parse_arms("classic,candidates,candidates_2")
    assert arms == {"classic": {"engine": "classic"}, "candidates": {"engine": "candidates"},
                    "candidates_2": {"engine": "candidates", "panel_looks": 2}}
    entries = run_arms.plan(["a", "b c"], arms, 3, 5, COMMON)
    assert len(entries) == 2 * 3 * 3
    assert [e["order"] for e in entries] == list(range(1, 19))
    assert len({e["key"] for e in entries}) == len(entries)
    for e in entries:
        assert e["seed"] == 5 + e["run"] - 1
        assert e["options"] == {**arms[e["arm"]], **COMMON, "seed": e["seed"]}
        assert e["record"] == f"{e['arm']}/{e['image'].replace(' ', '_')}__r{e['run']}.json"
    # The order of the arms turns one place each run, the same for every image.
    order = {(e["run"], e["image"]): [] for e in entries}
    for e in entries:
        order[(e["run"], e["image"])].append(e["arm"])
    assert order[(1, "a")] == order[(1, "b c")] == ["classic", "candidates", "candidates_2"]
    assert order[(2, "a")] == ["candidates", "candidates_2", "classic"]
    assert order[(3, "a")] == ["candidates_2", "classic", "candidates"]


def test_an_unknown_or_repeated_arm_and_a_repeated_image_are_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown variants"):
        run_arms.parse_arms("classic,bogus")
    with pytest.raises(ValueError, match="twice"):
        run_arms.parse_arms("classic,classic")
    with pytest.raises(ValueError, match="twice"):
        run_arms.read_images("a,b,a")
    with pytest.raises(ValueError, match="share a record name"):
        run_arms.plan(["a b", "a_b"], {"classic": {}}, 1, 0, COMMON)


def test_images_come_from_a_list_or_a_file(tmp_path):
    assert run_arms.read_images(" a, b ,") == ["a", "b"]
    listed = tmp_path / "images.txt"
    listed.write_text("# dev images\nlsp11385_2\n\nPCA_1  # the first\n", encoding="utf-8")
    assert run_arms.read_images(str(listed)) == ["lsp11385_2", "PCA_1"]


def test_a_dry_run_prints_the_sessions_and_writes_nothing(tmp_path):
    root = _registry(tmp_path / "scratch", "img1", "img2")
    records = tmp_path / "records"
    before = os.environ.get("PLEXORA_DATA_PATH")
    lines = []
    assert run_arms.main(_argv(root, records, "--dry-run"), emit=lines.append) == 0
    text = "\n".join(lines)
    assert "2 image(s) x 2 arm(s) x 3 run(s) = 12 sessions (12 to run)" in text
    assert '"reuse_answers" false' not in text and "reuse_answers false" in text
    assert "mode \"propose\"" in text
    sessions = [line for line in lines if " -> " in line]
    assert len(sessions) == 12
    assert any("run 2 seed 1" in s and "candidates" in s and "img2" in s
               and "candidates/img2__r2.json" in s for s in sessions)
    score = next(line for line in lines if line.startswith("score with:"))
    assert "score_gap.py --truth PERSONS_GATES.json" in score
    assert f"classic={records}/c1/classic/*.json" in score
    assert not records.exists()
    assert os.environ.get("PLEXORA_DATA_PATH") == before


def test_a_dry_run_names_the_images_the_data_root_lacks(tmp_path):
    root = _registry(tmp_path / "scratch", "img1")
    lines = []
    assert run_arms.main(_argv(root, tmp_path / "records", "--dry-run"), emit=lines.append) == 2
    assert any(line.startswith("not in") and line.split(": ", 1)[1].startswith("img2")
               for line in lines)


def test_a_model_is_named_only_on_the_dev_route(tmp_path):
    root = _registry(tmp_path / "scratch", "img1", "img2")
    lines = []
    assert run_arms.main(_argv(root, tmp_path / "r", "--model", "m", "--dry-run"),
                         emit=lines.append) == 2
    assert "--dev" in lines[0]


# -- the live install -----------------------------------------------------------------------


def test_the_live_data_directory_is_refused_and_a_scratch_root_is_not(tmp_path, monkeypatch):
    from platformdirs import user_data_dir

    from plexora import paths

    default = Path(user_data_dir(paths.APP_NAME, appauthor=False)).resolve()
    mine = tmp_path / "mine"
    # The settings file (the conftest's, in tmp_path) names the account's own directory.
    paths.settings_path().write_text(json.dumps({"data_dir": str(mine)}), encoding="utf-8")
    suggested = tmp_path / "suggested"
    monkeypatch.setenv(paths.ENV_DATA_PATH_DEFAULT, str(suggested))
    records = tmp_path / "records"
    for root, said in ((default, "is the live data directory"),
                       (default / "bench", "is inside the live data directory"),
                       (mine, "is the live data directory"),
                       (mine / "copy", "is inside"),
                       (tmp_path, "holds the live data directory"),
                       (suggested, "the connection's suggestion")):
        lines = []
        assert run_arms.main(_argv(root, records), emit=lines.append) == 2, root
        assert said in lines[0] and "copy of the projects or a scratch root" in lines[0], lines
    assert not records.exists()
    scratch = tmp_path.parent / f"{tmp_path.name}-scratch"
    assert run_arms.check_data_root(scratch) == scratch.resolve()


# -- records --------------------------------------------------------------------------------


def _unit(project, marker, *, notes=(), picture=None):
    unit = {"project": project, "marker": marker, "state": "accepted", "final": 6.1, "high": 9.0,
            "start": {"low": 5.8}, "lattice": {"points": [{"low": 5.5}, {"low": 6.0}, {"low": 6.6}]},
            "packets": ["pk_0001", "pk_0002"],
            "hypotheses": {"status": "resolved", "leader": 1, "observations": [
                {"packet_id": "pk_0001", "labels": {"1": "own_stain", "2": "not_own_stain"}}]},
            "notes": list(notes), "scoring": {"background": {"ceiling": 5.9}}}
    if picture is not None:
        unit["evidence_plan"] = {"picture": picture, "why": "a look"}
    return unit


def _write(path, record):
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_a_small_record_is_copied_byte_for_byte(tmp_path):
    source = _write(tmp_path / "session.json", {"session_id": "gs_1", "images": ["a"],
                                                "units": {"a:CD3": _unit("a", "CD3")}})
    meta = run_arms.store_record(source, tmp_path / "out" / "classic" / "a__r1.json")
    assert meta == {"bytes": source.stat().st_size, "trimmed": False}
    assert (tmp_path / "out" / "classic" / "a__r1.json").read_bytes() == source.read_bytes()


def test_a_record_over_1_mb_loses_its_payloads_first(tmp_path):
    record = {"session_id": "gs_1", "images": ["a"], "units": {
        "a:CD3": _unit("a", "CD3", picture="iVBORw0KGgo" + "A" * 1_200_000),
        "a:CD8": _unit("a", "CD8")}}
    source = _write(tmp_path / "session.json", record)
    dest = tmp_path / "out.json"
    meta = run_arms.store_record(source, dest)
    assert meta["trimmed"] and meta["bytes"] <= run_arms.MAX_BYTES and "over_limit" not in meta
    assert meta["dropped"] == ["units.*.evidence_plan.picture"]
    kept = json.loads(dest.read_text(encoding="utf-8"))
    # Only the payload went: the rest of the unit, the scoring with it, stays.
    assert kept["units"]["a:CD3"]["evidence_plan"] == {"why": "a look"}
    assert kept["units"]["a:CD3"]["scoring"] == record["units"]["a:CD3"]["scoring"]
    assert kept["images"] == ["a"]
    assert kept["_trimmed"]["from_bytes"] > run_arms.MAX_BYTES


def test_a_record_still_over_keeps_what_the_scorers_read_and_scores_the_same(tmp_path):
    prose = ["the cells at 6.8 read as unstained, the band beside them as stroma " * 40] * 120
    record = {"session_id": "gs_1", "images": ["a"], "options": {"engine": "candidates"},
              "units": {f"a:M{i}": _unit("a", f"M{i}", notes=prose) for i in range(4)}}
    source = _write(tmp_path / "session.json", record)
    assert source.stat().st_size > run_arms.MAX_BYTES
    dest = tmp_path / "out.json"
    meta = run_arms.store_record(source, dest)
    assert meta["trimmed"] and meta["bytes"] <= run_arms.MAX_BYTES
    assert "units.*.notes" in meta["dropped"] and "units.*.scoring" in meta["dropped"]
    kept = json.loads(dest.read_text(encoding="utf-8"))
    unit = kept["units"]["a:M0"]
    assert set(unit) <= set(run_arms.UNIT_KEEP)
    assert unit["hypotheses"] == record["units"]["a:M0"]["hypotheses"]
    assert kept["options"] == record["options"]
    truth = {"a": {f"M{i}": 6.0 for i in range(4)}}
    assert score_gap.rows_for("x", [str(dest)], truth) == score_gap.rows_for("x", [str(source)], truth)


def test_a_session_that_did_not_start_as_planned_is_named():
    entry = run_arms.plan(["a"], {"candidates": {"engine": "candidates"}}, 1, 0, COMMON)[0]
    stored = {"options": {**COMMON, "engine": "candidates", "seed": 0, "mode": "propose"}}
    assert run_arms.started_as_planned(stored, entry) == []
    stored["options"].update(engine="classic", mode="apply")
    assert run_arms.started_as_planned(stored, entry) == [
        "engine: 'candidates' != 'classic'", "mode: 'propose' != 'apply'"]


# -- end to end ----------------------------------------------------------------------------


@pytest.mark.paid
def test_a_campaign_runs_each_arm_through_the_harness_and_writes_no_gate(tmp_path):
    from plexora.agent import registry
    from plexora.ai.bench import TruthAgent
    from plexora.ai.harness.gateway import GatewayClient, TokenSource
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import engine as engines
    from tests.ai_harness_fixtures import FakeGateway
    from tests.autogate_fixtures import make_gating_project

    registry.discover(["gating"])
    info = make_gating_project(tmp_path, markers=("CD3", "CD8", "CD20"))
    ids = [c["id"] for c in info["cells"]]
    values = {m: np.array([c[m] for c in info["cells"]], dtype=np.float32) for m in info["truth"]}
    truth = {m: np.isin(ids, cells) for m, cells in info["truth"].items()}
    reader = TruthAgent(values, truth, "oracle", ids=ids)

    def brain(packet, body):
        if packet.get("kind") == "cell_panel":
            # The panel's cells as code holds them, which the reader is never sent.
            stored, _ = engines.store().read_packet(body["context"]["session_id"],
                                                    packet["packet_id"])
            packet = {**packet, "_panel": stored.get("_panel")}
        return reader.answer(packet)

    person = tmp_path / "person.json"
    person.write_text(json.dumps({"gsynth": {m: reader.best(m)[0] for m in truth}}),
                      encoding="utf-8")
    records = tmp_path / "_records"
    argv = ["--campaign", "e2e", "--images", "gsynth", "--arms", "classic,candidates",
            "--runs", "1", "--seed", "4", "--markers", "CD3,CD8", "--data-root", str(tmp_path),
            "--records-root", str(records), "--truth", str(person)]
    lines = []
    with FakeGateway(brain) as gateway:
        client = GatewayClient(gateway.url, tokens=TokenSource("BIOCAI1.test"), sleep=lambda s: None)
        assert run_arms.main(argv, gateway=client, emit=lines.append) == 0, lines
        calls = len(gateway.calls)
        assert calls > 0
        # Run again: every session has its record, so nothing is asked twice.
        assert run_arms.main(argv, gateway=client, emit=lines.append) == 0, lines
        assert len(gateway.calls) == calls
    assert os.environ["PLEXORA_DATA_PATH"] == str(tmp_path)

    manifest = json.loads((records / "e2e" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["arms"] == {"classic": {"engine": "classic"},
                                "candidates": {"engine": "candidates"}}
    assert manifest["seeds"] == [4] and manifest["ended"] and manifest["code"]["git_sha"]
    assert manifest["reader_models"] == ["approved-model-a"] and manifest["one_reader_model"]
    assert manifest["attempts"] == [] and manifest["records"]["trimmed"] == []
    assert set(manifest["sessions"]) == {"classic/gsynth/r1", "candidates/gsynth/r1"}
    for key, entry in manifest["sessions"].items():
        arm = key.split("/")[0]
        assert entry["status"] == "done" and entry["gates_unchanged"], entry
        assert entry["seed"] == 4 and entry["mismatched"] == []
        assert entry["reader_models"] == ["approved-model-a"]
        assert entry["models_by_task"]
        stored = json.loads((records / "e2e" / entry["record"]).read_text(encoding="utf-8"))
        assert stored["session_id"] == entry["session_id"]
        options = stored["options"]
        assert (options["engine"], options["mode"], options["reuse_answers"], options["seed"],
                options["display_check"], options["on_limit"]) == \
            (manifest["arms"][arm]["engine"], "propose", False, 4, "off", "extend")
    assert model.get_saved_gating_list("gsynth") is None
    scored = json.loads((records / "e2e" / "score_gap.json").read_text(encoding="utf-8"))
    assert set(scored["summaries"]) == {"classic", "candidates"}
    assert scored["summaries"]["classic"]["scored"] == 2

    # Another plan under the same name is refused before anything runs.
    lines = []
    assert run_arms.main([*argv[:7], "2", *argv[8:]], gateway=None, emit=lines.append) == 2
    assert "holds another plan" in lines[-1] and "runs" in lines[-1]
