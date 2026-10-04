"""The user's task -> model file for agents driving Plexora over MCP.

What is pinned: the file's resolution (task line, module default, the agent's
choice) and its leniency; every served packet stamped with its task and the
mapped model; a reader scoped to some tasks given only theirs and told
`other_tasks` otherwise -- gating with scoped readers side by side reaching
the unscoped run's gates, QC handed between workers; the delegate block
listing one worker per model only when the file maps the module (and exactly
today's block when it does not); and the model each answer names, recorded.
"""

import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.ai import delegation, models_config, setup
from plexora.plugins.gating.server.autogate import engine as engines
from plexora.plugins.gating.server.autogate import schemas
from tests.test_ai_parallel_markers import PANEL, drive_serial, finals, scene, start  # noqa: F401
from tests.test_gating_session import Oracle


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


@pytest.fixture
def models_file(tmp_path, monkeypatch):
    """Write the models file (YAML text) and point Plexora at it."""
    path = tmp_path / "ai-models.yaml"
    monkeypatch.setenv(models_config.ENV_PATH, str(path))

    def write(text):
        path.write_text(text, encoding="utf-8")
        return path
    return write


# -- the file --------------------------------------------------------------------------


def test_a_task_line_wins_over_its_module_default_and_a_blank_is_the_agents(models_file):
    models_file("gating:\n  default: model-a\n  threshold_evaluation: model-b\n"
                "  planning:\nqc:\n  blur: model-c\n")
    assert models_config.model_for("gating.threshold_evaluation") == "model-b"
    assert models_config.model_for("gating.image_inspection") == "model-a"
    assert models_config.model_for("gating.planning") == "model-a"
    assert models_config.model_for("qc.blur") == "model-c"
    assert models_config.model_for("qc.segmentation") is None
    assert models_config.model_for("chat.turn") is None
    assert models_config.mapped("gating") and models_config.mapped("qc")
    assert not models_config.mapped("chat")
    groups = models_config.groups("gating")
    # The note interpreter is the harness's own call, never a packet: no worker for it.
    assert groups[0] == {"model": "model-a", "tasks": [
        "gating.planning", "gating.image_inspection", "gating.final_validation"]}
    assert models_config.model_for("gating.biological_context") == "model-a"
    assert groups[1] == {"model": "model-b", "tasks": ["gating.threshold_evaluation"]}
    qc = models_config.groups("qc")
    assert {"model": "model-c", "tasks": ["qc.blur"]} in qc
    assert next(g for g in qc if g["model"] is None)["tasks"][0] == "qc.planning"


def test_a_bad_file_is_reported_never_fatal_and_an_edit_applies_at_once(models_file):
    models_file("gating:\n  nope: x\n  blur: y\n  planning: [1, 2]\nchatty:\n  a: b\n"
                "qc:\n  blur: model-c\n")
    info = models_config.describe()
    assert info["exists"] and len(info["problems"]) == 4
    assert models_config.model_for("qc.blur") == "model-c"
    assert not models_config.mapped("gating")
    models_file(": : :\n")
    assert models_config.load()["problems"] and models_config.model_for("qc.blur") is None
    models_file("qc:\n  blur: model-d   \n")
    assert models_config.model_for("qc.blur") == "model-d"


def test_no_file_means_the_agent_chooses(tmp_path, monkeypatch):
    monkeypatch.setenv(models_config.ENV_PATH, str(tmp_path / "absent.yaml"))
    info = models_config.describe()
    assert not info["exists"] and not info["problems"]
    assert all(model is None for tasks in info["tasks"].values() for model in tasks.values())


def test_init_writes_a_template_naming_every_task_and_no_model(models_file, tmp_path):
    from plexora.ai import tasks

    lines = []
    assert setup.models_command("init", out=lines.append) == 0
    text = models_config.path().read_text(encoding="utf-8")
    for task in tasks.tasks().values():
        assert f"  {task.name}:" in text
    assert not models_config.load()["problems"]
    assert not any(models_config.mapped(m) for m in ("gating", "qc", "chat"))
    for vendor in ("claude", "anthropic", "sonnet", "opus", "haiku", "gpt", "openai", "gemini"):
        assert vendor not in text.lower()
    assert setup.models_command("init", out=lines.append) == 1       # never overwritten
    assert setup.models_command("init", force=True, out=lines.append) == 0
    assert setup.models_command("path", out=lines.append) == 0 and lines[-1] == str(
        models_config.path())
    models_file("qc:\n  nope: x\n")
    assert setup.models_command("show", out=lines.append) == 1


# -- the delegate block -------------------------------------------------------------------


def test_the_block_is_unchanged_without_the_file_and_lists_workers_with_it(
        models_file, tmp_path, monkeypatch):
    from plexora import paths

    monkeypatch.setattr(paths, "settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.delenv("PLEXORA_MODEL_JUDGEMENT", raising=False)
    plain = delegation.block("gating_worker", session_id="gs_x")
    assert "workers" not in plain and "tasks:" not in plain["brief"]
    models_file("qc:\n  blur: model-c\n")
    assert delegation.block("gating_worker", session_id="gs_x") == plain
    models_file("gating:\n  threshold_evaluation: model-b\n  final_validation: model-b\n")
    block = delegation.block("gating_worker", session_id="gs_x")
    assert "brief" not in block and block["model"] is None
    first, second = block["workers"]
    assert first["model"] is None and first["pick"] == delegation.TIERS["judgement"]
    assert "gating.image_inspection" in first["tasks"]
    assert second == {**second, "model": "model-b",
                      "tasks": ["gating.threshold_evaluation", "gating.final_validation"]}
    assert "tasks: gating.threshold_evaluation, gating.final_validation" in second["brief"]
    assert f"reader: {second['reader']}" in second["brief"] and "gs_x" in second["brief"]
    assert first["reader"] != second["reader"]
    # Only the worker whose packets come first is launched at once; the other waits for its turn.
    assert (first["launch"], second["launch"]) == ("now", "on_demand")
    assert "first_task" not in first["brief"]
    later = delegation.block("gating_worker", first_task="gating.threshold_evaluation", session_id="gs_x")
    assert [w["launch"] for w in later["workers"]] == ["on_demand", "now"]
    assert "`on_demand`" in block["how"]


# -- gating: scoped readers -------------------------------------------------------------------


LOOKS = ["gating.planning", "gating.image_inspection"]
JUDGES = ["gating.threshold_evaluation", "gating.final_validation"]


def drive_scoped(session_id, agent, limit=400, answer_scope=True):
    """Two workers, each scoped to its tasks, taking turns as a coordinator
    would relaunch them: each runs until `other_tasks` (or busy), then the
    other goes. Returns {reader: [(task, model, kind), ...]} of what each got.
    `answer_scope=False`: the workers pass their scope to `gating_next` only,
    never to `gating_answer` (what a worker did on 2026-10-03)."""
    session = AgentSession()
    readers = {"looks": LOOKS, "judges": JUDGES}
    got = {name: [] for name in readers}
    turn, idle = "looks", 0
    for _ in range(limit):
        scope = {"reader": turn, "tasks": readers[turn]}
        result = ok(invoke(session, "gating_next", {"session_id": session_id, "wait_s": 20,
                                                    **scope}))
        while result["state"] == "decision":
            idle = 0
            packet = result["packet"]
            got[turn].append((packet.get("task"), packet.get("model"), packet["kind"]))
            result = ok(invoke(session, "gating_answer", {
                "session_id": session_id, "packet_id": packet["packet_id"],
                "answer": agent.answer(packet), "model": f"{turn}-model",
                **(scope if answer_scope else {})}))["next"]
        if result["state"] in ("decided", "done"):
            return got
        assert result["state"] in ("other_tasks", "busy", "bulk_running"), result
        if result["state"] == "other_tasks":
            assert result["needs"]["task"] not in readers[turn]
        idle += 1
        assert idle < 6, ("both workers stood idle", result)
        turn = "judges" if turn == "looks" else "looks"
    raise AssertionError("the scoped session did not finish")


@pytest.mark.paid
def test_scoped_readers_reach_the_unscoped_gates_each_given_only_its_tasks(
        tmp_path, scene, models_file):  # noqa: F811
    registry.discover(["gating"])
    models_file("gating:\n  default: looks-model\n  threshold_evaluation: judge-model\n"
                "  final_validation: judge-model\n")
    infos = {name: scene(tmp_path, name) for name in ("gser", "gscope")}
    session = AgentSession()
    serial_id = start(session, "gser")
    drive_serial(session, serial_id, Oracle(infos["gser"]))
    serial, _status = finals(session, serial_id)

    scoped_id = start(session, "gscope")
    got = drive_scoped(scoped_id, Oracle(infos["gscope"]))
    scoped, status = finals(session, scoped_id)
    assert scoped == serial
    assert got["looks"] and got["judges"], got
    for reader, tasks in (("looks", LOOKS), ("judges", JUDGES)):
        for task, model, kind in got[reader]:
            assert task in tasks, (reader, task, kind)
            assert model == ("judge-model" if task in JUDGES else "looks-model")
    assert any(kind == "t4_candidates" for _t, _m, kind in got["judges"])
    # Which model answered each task, as the answers named it.
    models = status["models"]
    assert models["gating.threshold_evaluation"]["asked"] == "judge-model"
    assert set(models["gating.threshold_evaluation"]["answered_by"]) == {"judges-model"}
    assert set(models["gating.image_inspection"]["answered_by"]) == {"looks-model"}
    assert all(state in schemas.TERMINAL_STATES for state, _f, _c in scoped.values())


@pytest.mark.paid
def test_an_answer_that_names_no_scope_is_followed_in_its_packets_scope(
        tmp_path, scene, models_file):  # noqa: F811
    # The packet after an answer is drawn for the reader its packet went to, in
    # that reader's tasks: never another task's packet left with nobody to take it.
    registry.discover(["gating"])
    models_file("gating:\n  default: looks-model\n  threshold_evaluation: judge-model\n"
                "  final_validation: judge-model\n")
    info = scene(tmp_path, "gnoscope")
    session = AgentSession()
    sid = start(session, "gnoscope")
    got = drive_scoped(sid, Oracle(info), answer_scope=False)
    for reader, tasks in (("looks", LOOKS), ("judges", JUDGES)):
        for task, _model, kind in got[reader]:
            assert task in tasks, (reader, task, kind)
    assert any(kind == "t4_candidates" for _t, _m, kind in got["judges"])


@pytest.mark.paid
def test_an_unscoped_run_stamps_the_task_and_leaves_the_model_to_the_agent(
        tmp_path, scene, monkeypatch):  # noqa: F811
    registry.discover(["gating"])
    monkeypatch.setenv(models_config.ENV_PATH, str(tmp_path / "absent.yaml"))
    info = scene(tmp_path, "gplain")
    session = AgentSession()
    sid = start(session, "gplain")
    drive_serial(session, sid, Oracle(info))
    store = engines.store()
    record = store.load(sid)
    assert record["models"]
    for packet_id, entry in record["models"].items():
        packet, _images = store.read_packet(sid, packet_id)
        assert packet["task"] == entry["task"] and "model" not in packet
        assert entry["asked"] is None
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert all(set(row["answered_by"]) <= {"unstated"} for row in status["models"].values())


# -- QC: workers handed between --------------------------------------------------------------


@pytest.mark.paid
def test_qc_workers_scoped_by_task_take_turns_to_the_end(tmp_path, models_file):
    from tests.qc_fixtures import make_qc_project
    from tests.test_qc_session import QCOracle
    from tests.test_qc_session import start as qc_start

    registry.discover(["qc"])
    models_file("qc:\n  default: routine-model\n  artifact_inspection: judge-model\n"
                "  final_review: judge-model\n")
    info = make_qc_project(tmp_path, artifacts=("saturation", "fold"))
    session = AgentSession()
    started = qc_start(session)
    sid = started["session_id"]
    workers = started["delegate"]["workers"]
    assert [w["model"] for w in workers] == ["routine-model", "judge-model"]
    agent = QCOracle(info)
    got = {w["reader"]: [] for w in workers}
    which = {task: w for w in workers for task in w["tasks"]}
    worker = workers[0]
    for _ in range(200):
        scope = {"reader": worker["reader"], "tasks": worker["tasks"]}
        result = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20, **scope}))
        while result["state"] == "decision":
            packet = result["packet"]
            assert packet["task"] in worker["tasks"] and packet["model"] == worker["model"]
            got[worker["reader"]].append(packet["kind"])
            result = ok(invoke(session, "qc_answer", {
                "session_id": sid, "packet_id": packet["packet_id"],
                "answer": agent.answer(packet, sid), "model": worker["model"],
                **scope}))["next"]
        if result["state"] == "decided":
            break
        if result["state"] == "other_tasks":
            worker = which[result["needs"]["task"]]
            assert result["needs"]["model"] == worker["model"]
    else:
        raise AssertionError("the QC session did not finish")
    assert all(got.values()), got
    assert "artifact_confirm" in got[workers[1]["reader"]]
    status = ok(invoke(session, "qc_session_status", {"session_id": sid}))
    assert "delegate" in status
    assert set(status["models"]["qc.artifact_inspection"]["answered_by"]) == {"judge-model"}
    ok(invoke(session, "qc_session_finish", {"session_id": sid}))


def test_qc_has_no_delegate_without_a_mapped_qc_task(models_file):
    from plexora.plugins.qc.capabilities_session import TOOLS

    models_file("gating:\n  default: model-a\n")
    assert TOOLS.delegate_block({"state": "running"}, "qs_x") == {}
    models_file("qc:\n  blur: model-c\n")
    block = TOOLS.delegate_block({"state": "running"}, "qs_x")["delegate"]
    assert block["skill"] == "qc-packets" and block["workers"]
