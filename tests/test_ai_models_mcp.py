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
        "gating.planning", "gating.partner_review", "gating.marker_naming", "gating.display",
        "gating.image_inspection", "gating.stain_reader", "gating.classifier_check",
        "gating.final_validation"]}
    assert models_config.model_for("gating.biological_context") == "model-a"
    # A check's or a panel's later readers are workers of their own, on the
    # model of the task they read again.
    assert groups[1:8] == [{"model": "model-a", "tasks": ["gating.image_reader_2"]},
                           {"model": "model-a", "tasks": ["gating.image_reader_3"]},
                           {"model": "model-a", "tasks": ["gating.stain_reader_2"]},
                           {"model": "model-a", "tasks": ["gating.stain_reader_3"]},
                           {"model": "model-b", "tasks": ["gating.threshold_evaluation"]},
                           {"model": "model-b", "tasks": ["gating.panel_reader_2"]},
                           {"model": "model-b", "tasks": ["gating.panel_reader_3"]}]
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


def test_a_line_may_set_the_effort_a_worker_runs_at(models_file, tmp_path, monkeypatch):
    from plexora import paths

    models_file("gating:\n  default: {model: model-a, effort: low}\n"
                "  threshold_evaluation: {model: model-b, effort: HIGH}\n"
                "  final_validation: {model: model-b, effort: high}\n"
                "  image_inspection: {model: model-a, effort: high}\n"
                "  classifier_check: model-b\n"
                "qc:\n  blur: {effort: medium}\n")
    assert not models_config.load()["problems"]
    assert models_config.effort_for("gating.planning") == "low"            # the module default
    assert models_config.effort_for("gating.threshold_evaluation") == "high"
    assert models_config.effort_for("gating.classifier_check") == "low"    # model alone: default effort
    assert models_config.model_for("qc.blur") is None and models_config.effort_for("qc.blur") == "medium"
    assert models_config.mapped("qc")
    groups = models_config.groups("gating")
    # One worker per model and effort: Opus-at-high and Opus-at-low are two workers.
    # A second and a third panel reader are workers of their own on the
    # first reader's model and effort (`Task.reader_of`).
    # A whole-image check's second and third readers follow the image reader.
    # The stain check's later readers follow its first reader (the module default here).
    assert [(g["model"], g.get("effort")) for g in groups] == [
        ("model-a", "low"), ("model-a", "high"), ("model-a", "high"), ("model-a", "high"),
        ("model-a", "low"), ("model-a", "low"),
        ("model-b", "high"), ("model-b", "high"), ("model-b", "high"), ("model-b", "low")]
    assert [g["tasks"] for g in groups[2:6]] == [["gating.image_reader_2"],
                                                ["gating.image_reader_3"],
                                                ["gating.stain_reader_2"],
                                                ["gating.stain_reader_3"]]
    assert groups[6]["tasks"] == ["gating.threshold_evaluation", "gating.final_validation"]
    assert [g["tasks"] for g in groups[7:9]] == [["gating.panel_reader_2"],
                                                ["gating.panel_reader_3"]]
    monkeypatch.setattr(paths, "settings_path", lambda: tmp_path / "settings.json")
    block = delegation.block("gating_worker", session_id="gs_x")
    assert [w.get("effort") for w in block["workers"]] == ["low", "high", "high", "high", "low",
                                                           "low", "high", "high", "high", "low"]
    assert "`effort`" in block["how"]
    shown = []
    setup.models_command("show", out=shown.append)
    assert "  threshold_evaluation: model-b, effort high" in shown
    assert models_config.describe()["efforts"]["gating"]["gating.planning"] == "low"


def test_a_bad_effort_is_a_problem_and_the_line_is_dropped(models_file):
    models_file("gating:\n  planning: {model: model-a, effort: extreme}\n"
                "  final_validation: {model: model-b, speed: fast}\n"
                "  threshold_evaluation: {model: [1], effort: high}\n")
    problems = models_config.load()["problems"]
    assert len(problems) == 3 and "effort is one of" in problems[0]
    for task in ("planning", "final_validation", "threshold_evaluation"):
        assert models_config.model_for(f"gating.{task}") is None
        assert models_config.effort_for(f"gating.{task}") is None
    assert not models_config.mapped("gating")


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
    first, image_2, image_3, stain_2, stain_3, second, reader_2, reader_3 = block["workers"]
    # The stain check's later readers: one worker each, the first stain reader's model.
    assert [stain_2["tasks"], stain_3["tasks"]] == [["gating.stain_reader_2"],
                                                     ["gating.stain_reader_3"]]
    # A whole-image check's later readers: one worker each, the image reader's model.
    assert [image_2["tasks"], image_3["tasks"]] == [["gating.image_reader_2"],
                                                     ["gating.image_reader_3"]]
    assert image_2["model"] is None and image_3["model"] is None
    # The panel readers after the first: one worker each, the first reader's model.
    assert [reader_2["tasks"], reader_3["tasks"]] == [["gating.panel_reader_2"],
                                                       ["gating.panel_reader_3"]]
    assert reader_2["model"] == reader_3["model"] == "model-b"
    assert len({w["reader"] for w in block["workers"]}) == 8
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
    assert [w["launch"] for w in later["workers"]] == ["on_demand", "on_demand", "on_demand",
                                                       "on_demand", "on_demand",
                                                       "now", "on_demand", "on_demand"]
    assert "`on_demand`" in block["how"]


def test_a_session_with_lanes_hands_out_one_worker_per_lane_launched_together(
        models_file, tmp_path, monkeypatch):
    """Lanes (`SessionOptions.lanes`): one worker per lane, each a reader of
    its own passing `parallel`, all launched in one message -- and counting its
    own packets, since the session's markers done move with every lane."""
    from plexora import paths

    monkeypatch.setattr(paths, "settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.delenv("PLEXORA_MODEL_JUDGEMENT", raising=False)
    assert delegation.block("gating_worker", session_id="gs_x", lanes=1) == \
        delegation.block("gating_worker", session_id="gs_x")
    block = delegation.block("gating_worker", session_id="gs_x", lanes=3)
    assert "brief" not in block and "workers" not in block
    assert [lane["reader"] for lane in block["lanes"]] == ["lane1", "lane2", "lane3"]
    assert {lane["launch"] for lane in block["lanes"]} == {"now"}
    for lane in block["lanes"]:
        assert f"reader: {lane['reader']}, parallel: 3" in lane["brief"]
        assert "packets, then stop" in lane["brief"] and "gs_x" in lane["brief"]
    assert "ONE message" in block["how"]
    # QC takes lanes too: the engine hands each only what no other lane holds.
    qc = delegation.block("qc_worker", session_id="qs_x", lanes=3)
    assert [lane["reader"] for lane in qc["lanes"]] == ["lane1", "lane2", "lane3"]


# -- gating: scoped readers -------------------------------------------------------------------


LOOKS = ["gating.planning", "gating.partner_review", "gating.marker_naming",
         "gating.image_inspection"]
JUDGES = ["gating.threshold_evaluation", "gating.final_validation"]
#: A panel read again goes to a reader that never saw it: one worker per reading.
SECOND, THIRD = ["gating.panel_reader_2"], ["gating.panel_reader_3"]
#: A whole-image check answered again: likewise, on the image reader's model.
IMAGE_2, IMAGE_3 = ["gating.image_reader_2"], ["gating.image_reader_3"]


def drive_scoped(session_id, agent, limit=400, answer_scope=True):
    """The workers, each scoped to its tasks, taking turns as a coordinator
    would relaunch them: each runs until `other_tasks` (or busy), then the
    next goes. Returns {reader: [(task, model, kind), ...]} of what each got.
    `answer_scope=False`: the workers pass their scope to `gating_next` only,
    never to `gating_answer` (what a worker did on 2026-10-03)."""
    session = AgentSession()
    readers = {"looks": LOOKS, "judges": JUDGES, "second": SECOND, "third": THIRD,
               "image_2": IMAGE_2, "image_3": IMAGE_3}
    order = list(readers)
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
        assert idle < 2 * len(order) + 2, ("every worker stood idle", result)
        turn = order[(order.index(turn) + 1) % len(order)]
    raise AssertionError("the scoped session did not finish")


@pytest.mark.pro
def test_scoped_readers_reach_the_unscoped_gates_each_given_only_its_tasks(
        tmp_path, scene, models_file):  # noqa: F811
    registry.discover(["gating"])
    models_file("gating:\n  default: looks-model\n"
                "  threshold_evaluation: {model: judge-model, effort: high}\n"
                "  final_validation: {model: judge-model, effort: high}\n")
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
    for reader, tasks in (("looks", LOOKS), ("judges", JUDGES), ("second", SECOND),
                          ("third", THIRD), ("image_2", IMAGE_2), ("image_3", IMAGE_3)):
        for task, model, kind in got[reader]:
            assert task in tasks, (reader, task, kind)
            assert model == ("looks-model" if task in LOOKS + IMAGE_2 + IMAGE_3
                             else "judge-model")
    assert any(kind == "t4_candidates" for _t, _m, kind in got["judges"])
    # Which model answered each task, as the answers named it.
    models = status["models"]
    assert models["gating.threshold_evaluation"]["asked"] == "judge-model"
    assert set(models["gating.threshold_evaluation"]["answered_by"]) == {"judges-model"}
    assert set(models["gating.image_inspection"]["answered_by"]) == {"looks-model"}
    assert all(state in schemas.TERMINAL_STATES for state, _f, _c in scoped.values())
    # Each packet carries the effort its task runs at; the looks have none (the agent's own).
    store = engines.store()
    for packet_id, entry in store.load(scoped_id)["models"].items():
        packet, _images = store.read_packet(scoped_id, packet_id)
        assert packet.get("effort") == (None if entry["task"] in LOOKS + IMAGE_2 + IMAGE_3
                                        else "high"), entry


@pytest.mark.pro
def test_an_answer_that_names_no_scope_is_followed_in_its_packets_scope(
        tmp_path, scene, models_file):  # noqa: F811
    # The packet after an answer is drawn for the reader its packet went to, in
    # that reader's tasks: never another task's packet left with nobody to take it.
    registry.discover(["gating"])
    models_file("gating:\n  default: looks-model\n  threshold_evaluation: judge-model\n"
                "  final_validation: judge-model\n")
    info = scene(tmp_path, "gnoscope")
    session = AgentSession()
    sid = start(session, "gnoscope", engine="v2")
    got = drive_scoped(sid, Oracle(info), answer_scope=False)
    for reader, tasks in (("looks", LOOKS), ("judges", JUDGES), ("second", SECOND),
                          ("third", THIRD), ("image_2", IMAGE_2), ("image_3", IMAGE_3)):
        for task, _model, kind in got[reader]:
            assert task in tasks, (reader, task, kind)
    # v2's cell panels choose its gates: the judges' task reads them.
    assert any(kind == "cell_panel" for _t, _m, kind in got["judges"])


@pytest.mark.pro
def test_a_packet_the_coordinator_took_and_left_goes_to_the_worker_whose_task_it_is(
        tmp_path, scene, models_file):  # noqa: F811
    # lsp35766_p193_2, 2026-11-23: QC's visual-pass coordinator called next
    # without a reader, was handed a stain check, and stopped; every stain
    # worker heard "busy" and the session stood still. The worker whose task
    # the packet is takes it over, unchanged, and the session runs on.
    registry.discover(["gating"])
    models_file("gating:\n  default: looks-model\n  threshold_evaluation: judge-model\n"
                "  final_validation: judge-model\n")
    info = scene(tmp_path, "ghandover")
    session = AgentSession()
    sid = start(session, "ghandover", engine="v2")
    taken = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}))
    assert taken["state"] == "decision"
    left = taken["packet"]
    task = left.get("task")
    owner = next(name for name, tasks in (("looks", LOOKS), ("judges", JUDGES))
                 if task in tasks)
    scope = {"reader": owner, "tasks": LOOKS if owner == "looks" else JUDGES}
    again = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20, **scope}))
    assert again["state"] == "decision"
    assert again["packet"]["packet_id"] == left["packet_id"]
    record = engines.store().load(sid)
    assert record["outstanding"][left["packet_id"]]["reader"] == owner
    got = drive_scoped(sid, Oracle(info))
    assert got[owner]


@pytest.mark.pro
def test_an_unscoped_run_stamps_the_task_and_leaves_the_model_to_the_agent(
        tmp_path, scene, monkeypatch):  # noqa: F811
    registry.discover(["gating"])
    monkeypatch.setenv(models_config.ENV_PATH, str(tmp_path / "absent.yaml"))
    info = scene(tmp_path, "gplain")
    session = AgentSession()
    sid = start(session, "gplain", engine="v2")
    drive_serial(session, sid, Oracle(info))
    store = engines.store()
    record = store.load(sid)
    assert record["models"]
    for packet_id, entry in record["models"].items():
        packet, _images = store.read_packet(sid, packet_id)
        assert packet["task"] == entry["task"] and "model" not in packet and "effort" not in packet
        assert entry["asked"] is None and "effort" not in entry
    status = ok(invoke(session, "gating_session_status", {"detail": "full", "session_id": sid}))
    assert all(set(row["answered_by"]) <= {"unstated"} for row in status["models"].values())


# -- QC: workers handed between --------------------------------------------------------------


@pytest.mark.pro
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
    # The stain check's second and third readers (`reader_of`) are workers of
    # their own on the model of the task they read again; a worker that read
    # a picture once would not read it again blind.
    assert [w["model"] for w in workers] == ["routine-model", "judge-model",
                                             "routine-model", "routine-model"]
    assert [w["tasks"] for w in workers[2:]] == [["qc.stain_reader_2"], ["qc.stain_reader_3"]]
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
    # A later reader works only when a stain is read failed first.
    assert all(got[w["reader"]] for w in workers[:2]), got
    assert "artifact_confirm" in got[workers[1]["reader"]]
    status = ok(invoke(session, "qc_session_status", {"detail": "full", "session_id": sid}))
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


@pytest.mark.pro
def test_a_qc_packet_the_coordinator_left_goes_to_its_worker(tmp_path, models_file):
    # lsp35766_p193_2, 2026-11-23: the visual-pass coordinator, calling qc_next
    # without a reader, was handed a stain check and stopped; the stain worker
    # heard "busy" for good. It now takes the packet over.
    from tests.qc_fixtures import make_qc_project
    from tests.test_qc_session import start as qc_start

    registry.discover(["qc", "roi"])
    models_file("qc:\n  default: routine-model\n  artifact_inspection: judge-model\n"
                "  final_review: judge-model\n")
    make_qc_project(tmp_path, artifacts=("saturation", "fold"))
    session = AgentSession()
    started = qc_start(session)
    sid = started["session_id"]
    taken = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20}))
    assert taken["state"] == "decision"
    left = taken["packet"]
    worker = next(w for w in started["delegate"]["workers"] if left["task"] in w["tasks"])
    again = ok(invoke(session, "qc_next", {"session_id": sid, "wait_s": 20,
                                           "reader": worker["reader"], "tasks": worker["tasks"]}))
    assert again["state"] == "decision"
    assert again["packet"]["packet_id"] == left["packet_id"]
