"""The end-to-end pipeline runner (`tools/ai_e2e.py`).

The pieces that need no Worker run always. The full stub run -- the licence
Worker under `wrangler dev --local`, a fake OpenRouter, the real harness --
spawns workerd and takes about a minute, so it runs only with PLEXORA_E2E=1.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from plexora.ai.harness.wire import ModelResponse, Usage

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ai_e2e", ROOT / "tools" / "ai_e2e.py")
ai_e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai_e2e)


def _model(id, tools=True, structured=True, vision=True, context=1000):
    return {"id": id, "tools": tools, "structured": structured, "vision": vision, "context": context}


def test_the_test_models_are_the_ones_that_exercise_most_of_the_pipeline():
    models = [_model("a/big:free", structured=False, context=10**6), _model("b/all:free"),
              _model("c/guard-safety:free", context=10**7), _model("b/other:free", context=5),
              _model("d/text:free", vision=False)]
    vision = ai_e2e.pick(models, need=("vision",))
    assert vision["id"] == "b/all:free"
    # The fallback prefers another vendor, so one vendor's outage does not take both.
    assert ai_e2e.pick(models, need=("vision",), avoid={"b/all:free"})["id"] == "a/big:free"
    assert ai_e2e.pick([_model("x/guard:free")], need=("tools",)) is None


def test_a_reply_wrapped_in_reasoning_and_a_fence_still_parses():
    raw = '<think>maybe {"kind": "wrong"}</think>\nSure!\n```json\n{"kind": "t2_confirm"}\n```'
    response = ModelResponse(text=raw, stop_reason="end_turn", usage=Usage(), gateway_request_id="r",
                             status="ok", price_micro=0, charged_micro=0, cost_micro=None, billing="credits",
                             model=None, balance={}, run=None)
    assert response.json() == {"kind": "t2_confirm"}


def test_the_report_lists_every_check_and_call():
    report = {"mode": "stub", "started": "t0", "finished": "t1", "models": {"text": {"id": "m"}},
              "checks": [{"check": "stream", "status": "pass", "seconds": 0.1, "detail": {}},
                         {"check": "gating", "status": "fail", "seconds": 1, "detail": {"error": "a|b"}}],
              "calls": [{"feature": "e2e", "capability": "text_routine", "billing": "credits", "model": "m",
                         "status": "ok", "failure_class": None, "attempts": 3, "failover": 0,
                         "input_uncached": 1, "cache_read": 2, "output_tokens": 3, "charged_micro": 4,
                         "started_at_ms": 10, "finished_at_ms": 25}]}
    text = ai_e2e.markdown(report)
    assert "| gating | FAIL | 1 | a/b |" in text
    assert "| e2e | text_routine | credits | m | ok | 3 | 0 | 1 | 2 | 3 | 4 | 15 |" in text


@pytest.mark.skipif(os.environ.get("PLEXORA_E2E") != "1", reason="spawns wrangler dev; set PLEXORA_E2E=1")
def test_the_whole_pipeline_runs_against_a_stub_provider(tmp_path):
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "ai_e2e.py"), "--stub", "--out",
                           str(tmp_path / "report")], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    for check in ("stream", "structured", "retries", "shadow", "tool_use", "gating", "qc", "failover",
                  "accounting"):
        assert f"| {check} | PASS |" in report, report
