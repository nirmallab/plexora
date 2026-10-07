"""The AI task registry (plexora/ai/tasks.yaml): every packet a gating or QC
run sends maps to a task, the fragment the gateway's registry is fed is the
platform's shape, and nothing in it names a model or a vendor."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from plexora.ai import tasks

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = {"vision_judgement", "vision_routine", "text_routine", "text_reasoning"}


def test_the_fragment_is_the_gateways_shape():
    """`PUT /admin/api/ai/registry/plexora` takes `{modules: {<module>: {label,
    tasks: {<task>: {label, blurb?, max_tokens?, effort?, requires?, kinds?}}}}}`
    (the platform's `Fragment`), and every task id it yields is a wire id."""
    fragment = tasks.fragment()
    assert set(fragment["modules"]) == {"gating", "qc", "chat"}
    allowed = {"label", "blurb", "max_tokens", "effort", "requires", "kinds"}
    for module, spec in fragment["modules"].items():
        assert spec["label"] and spec["tasks"]
        for name, task in spec["tasks"].items():
            assert set(task) <= allowed, (module, name)
            assert task["label"] and task["max_tokens"] > 0 and task["effort"] in tasks.EFFORTS
            assert set(task["requires"]) == {"vision", "reasoning"}
            assert tasks.WIRE_TASK.match(tasks.wire_id(f"{module}.{name}"))
    assert json.loads(tasks.to_json()) == fragment


def test_wire_ids_carry_the_product():
    assert tasks.wire_id("gating.planning") == "plexora.gating.planning"
    assert tasks.wire_id(None, module="qc") == "plexora.qc.default"
    assert tasks.wire_id(None) == "plexora.app.default"
    for task_id in tasks.tasks():
        assert tasks.WIRE_TASK.match(tasks.wire_id(task_id))


def test_the_sync_tool_prints_what_it_would_upload():
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "bioc_sync.py"), "--print"],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["tasks"] == tasks.fragment()
    assert payload["manifest"]["id"] == "plexora"


def test_every_task_is_well_formed():
    registry = tasks.tasks()
    assert {t.module for t in registry.values()} == {"gating", "qc", "chat"}
    for task in registry.values():
        assert tasks.TASK_ID.match(task.id), task.id
        assert task.capability in CAPABILITIES, task.id
        assert task.max_tokens > 0 and task.kinds and task.label
        assert task.effort in tasks.EFFORTS
    kinds = [(t.module, k) for t in registry.values() for k in t.kinds]
    assert len(kinds) == len(set(kinds)), "a packet kind belongs to two tasks of one module"


def test_every_gating_and_qc_packet_kind_maps_to_a_task():
    from plexora.plugins.gating.server.autogate import schemas as gating
    from plexora.plugins.qc.server import schemas as qc

    for kind in gating.SETUP_KINDS + gating.LOOK_KINDS + gating.CHECK_KINDS:
        assert tasks.task_for("gating", kind), kind
    for kind in qc.PACKET_KINDS:
        if kind == "score_review":
            continue
        assert tasks.task_for("qc", kind), kind
    for check, task in (("blur", "qc.blur"), ("registration", "qc.registration"),
                        ("segmentation", "qc.segmentation"), ("artifacts", "qc.artifact_inspection")):
        assert tasks.task_for("qc", "score_review", check=check) == task
    assert tasks.task_for("gating", "t4_candidates") == "gating.threshold_evaluation"
    assert tasks.task_for("gating", "context") == "gating.biological_context"
    assert tasks.task_for("qc", "no_such_kind") is None


def test_the_registry_names_no_model_and_no_vendor():
    blob = (Path(tasks.REGISTRY_PATH).read_text() + tasks.to_json()).lower()
    for vendor in ("claude", "anthropic", "sonnet", "opus", "haiku", "gpt", "openai", "gemini", "openrouter",
                   "orcarouter", "saygm"):
        assert vendor not in blob, vendor
