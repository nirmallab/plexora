"""The routing bench's client half: a candidate route gated end to end through
the dev route, scored against synthetic truth, and submitted for the gateway
to judge.

The "model" is `plexora.ai.bench.TruthAgent` (answers from the scene's known
phenotypes) behind `FakeGateway`, so the numbers are the pipeline's ceiling:
what is pinned is that every call names the candidate on the dev route, the
metrics the gateway's bar is written in come out, and a route that leaves a
marker ungated scores zero for it rather than being skipped.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from plexora.ai import bench, bench_data
from plexora.ai.harness import route_bench
from plexora.ai.harness.gateway import GatewayClient, TokenSource
from tests.ai_harness_fixtures import FakeGateway

ROOT = Path(__file__).resolve().parents[1]
MARKERS = ("CD3", "CD8")


def test_the_bench_versions_match_the_gateway_bar():
    catalog = (ROOT / "licensing" / "src" / "ai" / "catalog.ts").read_text(encoding="utf-8")
    for feature, version in route_bench.BENCH_VERSIONS.items():
        assert re.search(rf"\b{feature}: \{{ version: '{re.escape(version)}'", catalog), feature


def test_a_route_is_named_as_provider_and_model():
    assert route_bench.split_route("openrouter/anthropic/claude-opus-5-5") == ("openrouter",
                                                                              "anthropic/claude-opus-5-5")
    with pytest.raises(ValueError):
        route_bench.split_route("claude-opus-5-5")


def test_an_ungated_marker_scores_zero_and_a_failed_image_fails_the_bar():
    rows = [{"marker": "CD3", "gate": 1.0, "f1": 0.9}, {"marker": "CD8", "gate": None},
            {"marker": "*code*", "code_agreement": 0.99}]
    images = [{"status": "failed", "packets": 4, "model_calls": 4, "invalid_answers": 1,
               "tokens": {"input": 100, "cache_read": 80}, "charged_micro": 1000, "seconds": 2.0}]
    m = route_bench.metrics(rows, images)
    assert m["marker_f1"] == pytest.approx(0.45)
    assert m["code_agreement"] == 0.0
    assert m["failure_rate"] == 1.0 and m["invalid_answer_rate"] == 0.25 and m["cache_hit_ratio"] == 0.8


@pytest.mark.paid
def test_a_candidate_route_is_gated_through_the_dev_route_and_scored(monkeypatch):
    made = {}
    register = bench_data.register

    def capture(root, name, **kw):
        made[name] = register(root, name, **kw)
        return made[name]

    monkeypatch.setattr(bench_data, "register", capture)
    agents = {}

    def brain(packet, body):
        name = next(iter(made))
        agent = agents.setdefault(name, bench.TruthAgent(made[name]["values"], made[name]["truth"]))
        return agent.answer(packet)

    with FakeGateway(brain) as gateway:
        client = GatewayClient(gateway.url, tokens=TokenSource("BIOCAI1.test"), dev=True, sleep=lambda s: None)
        evaluation = route_bench.bench_route("openai/gpt-test", scenarios=("easy",), markers=MARKERS,
                                             gateway=client, grid=16, size=512)
    assert {c["path"] for c in gateway.calls} == {"/v1/ai/dev/messages"}
    assert {c["body"]["model"] for c in gateway.calls} == {"openai/gpt-test"}
    assert not any("capability" in c["body"] for c in gateway.calls)
    assert evaluation["provider"] == "openai" and evaluation["model"] == "gpt-test"
    assert evaluation["bench_version"] == "gating-1"
    m = evaluation["metrics"]
    assert evaluation["images"][0]["status"] == "done", evaluation["images"]
    assert m["code_agreement"] >= 0.95 and m["marker_f1"] >= 0.9, m
    assert m["invalid_answer_rate"] == 0.0 and m["failure_rate"] == 0.0
    usage = [c["usage"] for c in gateway.calls]
    read = sum(u["cache_read"] for u in usage)
    total = sum(u["input_uncached"] + u["cache_read"] + u["cache_write_5m"] for u in usage)
    assert m["cache_hit_ratio"] == pytest.approx(read / total, abs=1e-3)
    assert m["model_calls"] == len(gateway.calls)


def test_an_evaluation_is_submitted_with_the_admin_token_and_numbers_only():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data = json.dumps({"id": 7, "passed": True, "misses": []}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        evaluation = {"feature": "gating", "capability": "vision_judgement", "provider": "openai",
                      "model": "gpt-test", "bench_version": "gating-1", "dataset_ids": ["synthetic:easy"],
                      "plexora_version": "0.0.27", "images": [{"reason": "kept local"}],
                      "metrics": {"code_agreement": 0.99, "bte_per_packet": None}}
        verdict = route_bench.submit(evaluation, admin_token="adm",
                                     base_url=f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
        server.server_close()
    assert verdict["passed"] is True
    assert seen["path"] == "/admin/api/ai/evaluations" and seen["auth"] == "Bearer adm"
    assert seen["body"]["metrics"] == {"code_agreement": 0.99}
    assert "images" not in seen["body"]


def test_the_cli_runs_the_bench_and_writes_the_evaluation(monkeypatch, tmp_path, capsys):
    import argparse

    from plexora.ai.harness import cli

    evaluation = {"feature": "gating", "capability": "vision_judgement", "provider": "openai", "model": "m",
                  "metrics": {"code_agreement": 1.0, "marker_f1": 1.0, "invalid_answer_rate": 0.0,
                              "failure_rate": 0.0, "cache_hit_ratio": 0.5, "model_calls": 3,
                              "cost_micro_per_image": 10_000, "seconds_per_image": 1.0}}
    monkeypatch.setattr(route_bench, "bench_route", lambda *a, **kw: evaluation)
    out = tmp_path / "eval.json"
    args = argparse.Namespace(route="openai/m", feature="gating", capability="vision_judgement",
                              synthetic="easy", markers=None, seed=0, grid=16, size=512, submit=False,
                              gateway="http://127.0.0.1:1", out=str(out))
    assert cli.route_bench_command(args) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["model"] == "m"
    assert "code agreement 1.000" in capsys.readouterr().out
    args.submit = True
    monkeypatch.delenv("PLEXORA_ADMIN_TOKEN", raising=False)
    assert cli.route_bench_command(args) == 2
