"""What a gating session SENDS: each brief once per reader, trimmed sheets,
and the delegation block -- never a different gate.

The stored packet is the self-contained one (memo keys, replays, the report);
`packets.as_sent` is a view of it for one reader, a conversation. A reader is
renewed by `gating_session_status` without the current `known_guide`, so an
`{as_in: packet_id}` only ever names a packet that reader holds.
"""

import copy
import json

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.plugins.gating.server.autogate import packets, sheet

pytestmark = pytest.mark.paid


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def _packet(pid, **evidence):
    return {"packet_id": pid, "kind": "t2_confirm", "session_id": "gs_x",
            "question": "[skin (tissue); user] CD45 (p): is the gate right?",
            "narration": "the viewer's sentence", "budget": {"this_packet": {"packets": 1}},
            "_sheet_rows": {"fields": "f" * 12},
            "evidence": {"biology": {"contexts": ["skin (tissue)"], "said": {
                "tissue": "skin", "disease": "primary cutaneous melanoma",
                "notes": "as stated by the user"}}, **evidence}}


def _record(**options):
    return {"options": {"reading": "once", "evidence": "delta", "sheets": "trim", **options}}


HIERARCHY = {"stage": "broad", "children": ["CD3e", "CD8a", "CD4", "FOXP3", "CD68", "CD163",
                                            "CD11c", "CD20", "TIGIT", "LAG3"],
             "not_gated_yet": ["CD3e"]}


def send(packet, record, **kwargs):
    return packets.as_sent(packet, record, record["options"], **kwargs)


def test_a_brief_goes_once_then_as_a_pointer_and_again_to_a_new_reader():
    record = _record()
    first = _packet("pk_0001", hierarchy=dict(HIERARCHY))
    stored = copy.deepcopy(first)
    sent = send(first, record)
    assert first == stored                                   # storage untouched
    assert sent["evidence"]["hierarchy"]["children"] == HIERARCHY["children"]
    assert sent["question"].startswith("CD45")               # the prefix is in biology
    assert "budget" not in sent and "narration" not in sent and "_sheet_rows" not in sent
    later = send(_packet("pk_0002", hierarchy={**HIERARCHY, "not_gated_yet": []}), record)
    assert later["evidence"]["hierarchy"]["children"] == {"as_in": "pk_0001"}
    assert later["evidence"]["biology"]["said"] == {"as_in": "pk_0001"}
    assert "not_gated_yet" not in later["evidence"]["hierarchy"] or \
        later["evidence"]["hierarchy"]["not_gated_yet"] == []
    # The same packet served again is never pointed at itself.
    assert send(first, record)["evidence"]["hierarchy"]["children"] == HIERARCHY["children"]
    packets.new_reader(record)
    fresh = send(_packet("pk_0003", hierarchy=dict(HIERARCHY)), record)
    assert fresh["evidence"]["hierarchy"]["children"] == HIERARCHY["children"]
    # A pointer resolves to the value it stands for, from what the reader holds.
    packets.new_reader(record)
    held = {}
    for one in (first, _packet("pk_0004", hierarchy=dict(HIERARCHY))):
        packets.resolve_as_in(send(one, record), held)
    assert held["pk_0004"]["_as_in"]
    assert held["pk_0004"]["evidence"]["hierarchy"] == HIERARCHY


def test_full_sends_the_stored_packet_and_short_values_are_never_pointers():
    record = _record(evidence="full")
    one = _packet("pk_0001", hierarchy=dict(HIERARCHY))
    send(one, record)
    again = send(_packet("pk_0002", hierarchy=dict(HIERARCHY)), record)
    assert again["evidence"]["hierarchy"] == HIERARCHY
    assert again["question"].startswith("[skin")
    delta = _record()
    for pid in ("pk_0001", "pk_0002"):
        out = send(_packet(pid, marker="CD45", round=1), delta)
    assert out["evidence"]["marker"] == "CD45" and out["evidence"]["round"] == 1


def test_trims_drop_score_components_and_passing_checks():
    record = _record()
    packet = _packet("pk_0001", candidates=[{"id": "c1", "low": 7.2, "score": 0.7,
                                             "components": {"distribution": 1.0}}],
                     checks=[{"name": "a", "ok": True}, {"name": "b", "ok": False},
                             {"name": "prior_fraction", "value": 0.1}])
    sent = send(packet, record, full=True)
    assert sent["evidence"]["candidates"] == [{"id": "c1", "low": 7.2, "score": 0.7}]
    assert [c["name"] for c in sent["evidence"]["checks"]] == ["b", "prior_fraction"]
    assert sent["evidence"]["checks_passed"] == 1
    assert packet["evidence"]["candidates"][0]["components"]      # stored kept


def test_a_sent_sheet_row_is_registered_for_its_reader():
    record = _record()
    send(_packet("pk_0001"), record)
    assert packets.briefed(record)["seen"]["sheet.fields:" + "f" * 12] == "pk_0001"
    packets.new_reader(record)
    assert packets.briefed(record)["seen"] == {}


def test_a_trimmed_sheet_is_shorter_by_its_left_out_row():
    width, full = sheet.sheet_size()
    assert sheet.sheet_size(("fields",))[0] == width
    fields, slide = sheet.sheet_size(("fields",))[1], sheet.sheet_size(("slide",))[1]
    assert fields < full and slide < full
    assert fields + slide == full + sheet.TITLE_H - sheet.GAP


# -- a whole session: the same gates, fewer characters and pixels ------------------


def _run(tmp_path, name, options):
    from plexora.ai import bench
    from tests.autogate_fixtures import make_gating_project
    from tests.test_gating_session import Oracle

    project = f"gsynth_{name}"
    info = make_gating_project(tmp_path, name=project, grid=32, size=1280,
                               markers=("CD3", "CD8", "CD20", "CD4"))
    session = AgentSession()
    units, status, _seconds, _sid = bench.run_session(
        session, project, ["CD3", "CD8", "CD20", "CD4"], Oracle(info),
        options={"reuse_answers": False, **options})
    return ({m: (u["state"], u.get("final")) for m, u in units.items()},
            status["sent_to_agent"])


def test_delta_and_trimmed_sheets_reach_the_same_gates_for_less(tmp_path, monkeypatch):
    gates_full, sent_full = _run(tmp_path, "full", {"evidence": "full", "sheets": "full"})
    gates_lean, sent_lean = _run(tmp_path, "lean", {})
    assert gates_lean == gates_full
    assert sent_lean["packets"] == sent_full["packets"]
    assert sent_lean["chars"] < sent_full["chars"]
    assert sent_lean["pixels"] <= sent_full["pixels"]


def test_status_without_the_guide_is_a_new_reader_and_carries_the_delegate(tmp_path):
    from tests.autogate_fixtures import make_gating_project
    from tests.test_gating_session import ok, start

    make_gating_project(tmp_path, grid=32, size=1280, markers=("CD3", "CD20"))
    session = AgentSession()
    started = start(session, markers=["CD3", "CD20"])
    sid = started["session_id"]
    delegate = started["delegate"]
    # The coordinator hands the packets out: it is not sent the guide.
    assert isinstance(started["reading_guide"], str) and started["guide_version"]
    assert delegate["tier"] == "judgement" and delegate["skill"] == "gate-packets"
    assert sid in delegate["brief"] and "gating_answer" in delegate["tools"]
    fresh = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert fresh["reader"] >= 1 and "delegate" in fresh
    held = ok(invoke(session, "gating_session_status", {
        "session_id": sid, "known_guide": started["guide_version"]}))
    assert "reader" not in held
    off = start(session, markers=["CD3"], delegate=False)
    assert "delegate" not in off


# -- the tiers: named by the work, never by a vendor's model ------------------------


def test_the_package_names_no_model_and_the_user_maps_the_tiers(tmp_path, monkeypatch):
    from plexora import paths
    from plexora.ai import delegation, setup

    monkeypatch.setattr(paths, "settings_path", lambda: tmp_path / "settings.json")
    for tier in delegation.TIERS:
        monkeypatch.delenv(f"{delegation.ENV_PREFIX}{tier.upper()}", raising=False)
    block = delegation.block("gating_worker", session_id="gs_x")
    blob = json.dumps({"tiers": delegation.TIERS, "roles": delegation.ROLES,
                       "block": block}).lower()
    for vendor in ("claude", "anthropic", "sonnet", "opus", "haiku", "gpt", "openai",
                   "gemini"):
        assert vendor not in blob
    assert block["model"] is None and block["pick"] == delegation.TIERS["judgement"]
    lines = []
    assert setup.tiers_command(["routine=small-model"], out=lines.append) == 0
    assert delegation.model_for("routine") == "small-model"
    monkeypatch.setenv("PLEXORA_MODEL_ROUTINE", "from-env")
    assert delegation.model_for("routine") == "from-env"
    assert setup.tiers_command(["nope=x"], out=lines.append) == 2
    assert setup.tiers_command(["routine="], out=lines.append) == 0
    monkeypatch.delenv("PLEXORA_MODEL_ROUTINE")
    assert delegation.model_for("routine") is None


def test_setup_installs_one_agent_per_role(tmp_path):
    from plexora.ai import delegation, setup

    written = setup._install_agents(tmp_path, dry_run=False)
    assert len(written) == len(delegation.ROLES)
    text = open(written[0], encoding="utf-8").read()
    assert text.startswith("---\nname: plexora-gating-worker\n")
    assert "mcp__plexora__gating_answer" in text and "\nmodel:" not in text


def test_a_reference_run_file_is_truth_for_its_accepted_markers(tmp_path):
    from plexora.ai import bench

    path = tmp_path / "ref.json"
    path.write_text(json.dumps({"project": "p", "markers": {
        "CD3": {"low": 1.5, "state": "accepted"},
        "CD4": {"low": 2.0, "state": "accepted_low_confidence"},
        "CD8": {"low": 3.0, "state": "manual_review_recommended"}}}), encoding="utf-8")
    assert bench.reference_gates(path, "p") == {"CD3": 1.5, "CD4": 2.0}
    with pytest.raises(RuntimeError):
        bench.reference_gates(path, "other")


def test_the_repository_worker_agent_is_the_generated_one():
    """This repository's own Claude Code worker is `agent_file`'s output, so
    developing here exercises exactly what `plexora ai setup claude` installs."""
    from pathlib import Path

    from plexora.ai import delegation

    root = Path(__file__).resolve().parent.parent / ".claude" / "agents"
    for role in delegation.ROLES:
        path = root / f"{delegation.agent_name(role)}.md"
        assert path.read_text(encoding="utf-8") == delegation.agent_file(role), (
            f"regenerate {path.name}: python -c \"from pathlib import Path; from plexora.ai "
            "import setup; setup._install_agents(Path('.claude/agents'), False)\"")
