"""The AI task registry (plexora/ai/tasks.yaml): every packet a gating or QC
run sends maps to a task, the licence Worker's copy is current, and nothing in
it names a model or a vendor."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from plexora.ai import tasks

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = {"vision_judgement", "vision_routine", "text_routine", "text_reasoning"}


def test_the_workers_copy_is_current():
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "ai_tasks_sync.py"), "check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((ROOT / "licensing" / "src" / "ai" / "tasks.json").read_text())["modules"]


def test_every_task_is_well_formed():
    registry = tasks.tasks()
    assert {t.module for t in registry.values()} == {"gating", "qc", "chat"}
    for task in registry.values():
        assert tasks.TASK_ID.match(task.id), task.id
        assert task.capability in CAPABILITIES, task.id
        assert task.max_tokens > 0 and task.kinds and task.label
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
