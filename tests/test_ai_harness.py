"""Plexora's own agent harness, end to end against a stand-in gateway.

The model is the scripted Oracle from `test_gating_session.py`, answering the
packets the harness sends it; the gateway is `FakeGateway`, which streams the
real wire format and emulates a prompt cache. What is pinned: a session is
gated with no external agent; one structured call per packet; rolling
workers; a byte-stable prefix the cache actually reads; credit refusals pause
rather than fail, and a paused session resumes; invalid answers get one repair
turn; the dev route and the run quote are used as asked.
"""

import json
import threading
import time

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.ai.harness import cache_plan, prefix, schema
from plexora.ai.harness.decision import GatingOptions, GatingRun, run_many
from plexora.ai.harness.gateway import GatewayClient, GatewayError, TokenSource
from plexora.ai.harness.orchestrator import Blackboard, Scheduler, TaskGraph
from plexora.ai.harness.trace import TraceStore
from plexora.ai.harness.wire import Usage
from tests.ai_harness_fixtures import FakeGateway
from tests.autogate_fixtures import make_gating_project
from tests.test_gating_session import Oracle

MARKERS = ("CD3", "CD8", "CD20")
#: Hard enough that the session goes past the T1 strips (t2/t3/t4 packets).
HARD = ("CD3", "CD8", "CD20", "CD4", "FOXP3")


def client(gateway, **kw):
    return GatewayClient(gateway.url, tokens=TokenSource("PLXAI1.test"), sleep=lambda s: None, **kw)


def oracle_brain(info):
    oracle = Oracle(info)

    def brain(packet, body):
        return oracle.answer(packet)
    return brain


# -- pieces that need no licence ------------------------------------------------------


def test_the_prefix_is_byte_stable_and_carries_nothing_volatile():
    first, second = prefix.gating_prefix(), prefix.gating_prefix()
    assert first == second
    assert cache_plan.fingerprint(first) == cache_plan.fingerprint(second)
    text = json.dumps(first)
    assert "session" not in prefix.IDENTITY.lower() or "gs_" not in text
    assert str(time.gmtime().tm_year) not in prefix.IDENTITY
    # Exactly one explicit breakpoint, on the last block.
    marks = [i for i, block in enumerate(first) if "cache_control" in block]
    assert marks == [len(first) - 1]
    assert cache_plan.expected_tokens(first) > 1024     # long enough to be cacheable at all


def test_every_answer_kind_has_a_provider_ready_schema():
    from plexora.plugins.gating.server.autogate import answers

    for kind in answers.BY_KIND:
        s = schema.for_kind(kind)
        assert s is not None, kind
        dumped = json.dumps(s)
        for banned in ('"$ref"', '"minimum"', '"maximum"', '"maxLength"', '"title"'):
            assert banned not in dumped, (kind, banned)

        def closed(node):
            if isinstance(node, dict):
                if node.get("type") == "object" or "properties" in node:
                    assert node.get("additionalProperties") is False, kind
                for v in node.values():
                    closed(v)
            elif isinstance(node, list):
                for v in node:
                    closed(v)
        closed(s)


def test_the_cache_monitor_calls_a_warm_call_that_misses_the_prefix_a_miss():
    monitor = cache_plan.CacheMonitor()
    assert monitor.observe("fp", 5000, Usage(cache_write_5m=5200, input_uncached=10), warm=False) == "cold"
    assert monitor.observe("fp", 5000, Usage(cache_read=5100, cache_write_5m=300), warm=True) == "hit"
    assert monitor.observe("fp", 5000, Usage(cache_write_5m=5400), warm=True) == "miss"
    # A new worker on a prefix another worker already wrote must read it.
    assert monitor.observe("fp", 5000, Usage(cache_read=5000), warm=False) == "hit"
    assert monitor.counts == {"cold": 1, "hit": 2, "miss": 1}


def test_the_client_retries_an_outage_with_the_same_key_and_never_retries_credit():
    with FakeGateway(lambda packet, body: {"kind": "x"}) as gateway:
        gateway.fail_with = [(503, "provider_unavailable"), (429, "provider_rate_limited")]
        from plexora.ai.harness.wire import ModelRequest

        request = ModelRequest(capability="vision_judgement", system=[{"type": "text", "text": "s"}],
                               messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
        response = client(gateway).messages(request, idempotency_key="k-123456")
        assert response.json() == {"kind": "x"}
        assert response.usage.output_tokens >= 1
        assert [c["idempotency_key"] for c in gateway.calls] == ["k-123456"]

        gateway.fail_with = [(402, "insufficient_credits")]
        with pytest.raises(GatewayError) as caught:
            client(gateway).messages(request, idempotency_key="k-654321")
        assert caught.value.code == "insufficient_credits"
        assert len(gateway.calls) == 1


def test_the_dev_flag_uses_the_dev_route_and_may_name_a_model():
    with FakeGateway(lambda packet, body: {"ok": True}) as gateway:
        from plexora.ai.harness.wire import ModelRequest

        request = ModelRequest(capability="vision_judgement", system=[], model="claude-sonnet-5",
                               messages=[{"role": "user", "content": "hi"}])
        dev = client(gateway, dev=True).messages(request, idempotency_key="dev-000001")
        assert gateway.calls[-1]["path"] == "/v1/ai/dev/messages"
        assert gateway.calls[-1]["body"]["model"] == "claude-sonnet-5"
        assert dev.billing == "dev" and dev.cost_micro == dev.price_micro
        client(gateway).messages(request, idempotency_key="paid-00001")
        assert gateway.calls[-1]["path"] == "/v1/ai/messages"
        assert "model" not in gateway.calls[-1]["body"]


# -- the orchestrator --------------------------------------------------------------------


def test_the_scheduler_runs_independent_tasks_in_parallel_and_respects_dependencies():
    graph = TaskGraph()
    order, lock = [], threading.Lock()
    barrier = threading.Barrier(3, timeout=5)

    def leaf(name):
        def run(ctx):
            barrier.wait()                      # all three must be running at once
            with lock:
                order.append(name)
            return name.upper()
        return run

    a, b, c = (graph.add(n, leaf(n)) for n in ("a", "b", "c"))

    def join(ctx):
        with lock:
            order.append("join")
        return [ctx.result_of(t) for t in (a, b, c)]

    graph.add("join", join, depends_on=[a, b, c])
    result = Scheduler(graph, max_parallel=3).run()
    assert result["done"] == 4 and result["peak_parallel"] == 3
    assert order[-1] == "join"
    assert graph.tasks["join"].result == ["A", "B", "C"]


def test_a_failed_task_cancels_hard_dependents_but_not_soft_ones():
    graph = TaskGraph()
    bad = graph.add("bad", lambda ctx: 1 / 0)
    graph.add("hard", lambda ctx: "never", depends_on=[bad])
    graph.add("soft", lambda ctx: ctx.result_of("bad"), soft=[bad])
    result = Scheduler(graph).run()
    states = {k: v["state"] for k, v in result["tasks"].items()}
    assert states == {"bad": "failed", "hard": "cancelled", "soft": "done"}
    assert graph.tasks["soft"].result is None


def test_tasks_spawn_children_and_wait_on_the_blackboard():
    graph = TaskGraph()
    board = Blackboard()

    def parent(ctx):
        for i in range(3):
            ctx.spawn(lambda c, i=i: c.board.post(f"part:{i}", i * i, by=c.task.id), task_id=f"child{i}")
        return "spawned"

    graph.add("parent", parent)
    graph.add("reduce", lambda ctx: sum(ctx.board.facts("part:").values()), depends_on=["parent"],
              ready_when=lambda b: len(b.facts("part:")) == 3)
    result = Scheduler(graph, board=board, max_parallel=4).run()
    assert result["done"] == 5
    assert graph.tasks["reduce"].result == 0 + 1 + 4
    assert graph.tasks["child1"].parent == "parent" and graph.tasks["child1"].depth == 1


def test_spawning_is_bounded_by_depth_and_the_first_task_warms_before_the_rest():
    graph = TaskGraph()
    started = []

    def deep(ctx):
        ctx.spawn(lambda c: c.spawn(lambda d: d.spawn(lambda e: None)))
    graph.add("root", deep)
    result = Scheduler(graph, max_depth=2).run()
    assert result["failed"] == 1                   # the third level was refused

    graph = TaskGraph()

    def work(name):
        def run(ctx):
            started.append((name, time.monotonic()))
            time.sleep(0.15)
            ctx.warm()
        return run
    for n in ("first", "second", "third"):
        graph.add(n, work(n))
    Scheduler(graph, max_parallel=3, stagger_first=True).run()
    times = dict(started)
    assert times["second"] - times["first"] >= 0.1 and times["third"] - times["first"] >= 0.1


# -- end to end ------------------------------------------------------------------------


@pytest.fixture
def gating(tmp_path):
    registry.discover(["gating"])
    return make_gating_project(tmp_path, markers=MARKERS)


def _final_units(session_id):
    status = invoke(AgentSession(), "gating_session_status", {"session_id": session_id})
    assert status["ok"], status
    return {u["marker"]: u for u in status["result"]["units"]}


@pytest.fixture
def hard(tmp_path):
    registry.discover(["gating"])
    return make_gating_project(tmp_path, grid=32, size=1280, markers=HARD)


@pytest.mark.paid
def test_the_harness_gates_a_project_with_no_external_agent(hard, tmp_path):
    with FakeGateway(oracle_brain(hard)) as gateway:
        trace = TraceStore(tmp_path / "trace.sqlite")
        events = []
        summary = GatingRun(GatingOptions(project="gsynth"), gateway=client(gateway), trace=trace,
                            on_event=events.append).run()
    assert summary["status"] == "done", summary
    final = _final_units(summary["session_id"])
    assert set(final) == set(HARD)
    for marker in HARD:
        assert final[marker]["state"] in ("accepted", "accepted_low_confidence"), final[marker]
    kinds = {json.loads(c["body"]["request"]["messages"][-1]["content"][-1]["text"])["kind"]
             for c in gateway.calls}
    assert "t1_strip" in kinds and len(kinds) >= 2, kinds

    # One structured call per packet, through a declared run, never a model id.
    assert summary["model_calls"] == summary["packets"] == len(gateway.calls)
    assert summary["invalid_answers"] == 0
    for call in gateway.calls:
        body = call["body"]
        assert "model" not in body
        assert body["capability"] == "vision_judgement"
        assert body["request"]["output_schema"]["additionalProperties"] is False
        assert body["context"]["run_id"] == "run_1"
        assert body["context"]["session_id"] == summary["session_id"]
        assert "answer_schema" not in body["request"]["messages"][-1]["content"][-1]["text"]
    assert len({c["idempotency_key"] for c in gateway.calls}) == len(gateway.calls)
    assert gateway.runs["run_1"]["status"] == "finished"
    assert gateway.runs["run_1"]["units"] == len(HARD)

    # Rolling workers (one marker each by default): a worker's later packets
    # only ever concern the markers its first packet did.
    assert summary["workers"] >= 2
    for call in gateway.calls:
        packets = [json.loads(m["content"][-1]["text"]) for m in call["body"]["request"]["messages"]
                   if m["role"] == "user"]
        first = {u["marker"] for u in packets[0]["units"]}
        for packet in packets[1:]:
            assert {u["marker"] for u in packet["units"]} <= first
        assert len(packets) <= 8

    # The prefix is identical on every call, and from the second call on the
    # cache reads it; there is no miss anywhere.
    systems = {json.dumps(c["body"]["request"]["system"], sort_keys=True) for c in gateway.calls}
    assert len(systems) == 1
    assert summary["cache"]["verdicts"]["miss"] == 0
    assert summary["cache"]["verdicts"]["cold"] == 1
    for call in gateway.calls[1:]:
        assert call["usage"]["cache_read"] >= cache_plan.expected_tokens(prefix.gating_prefix()) * 0.8

    # The local trace has every call, linked by gateway request id.
    report = trace.cache_report(summary["run_id"])
    assert report["calls"] == len(gateway.calls) and report["verdicts"].get("miss", 0) == 0
    assert all(c["gateway_request_id"] for c in trace.calls(summary["run_id"]))
    assert [e["event"] for e in events][:2] == ["started", "quoted"]


@pytest.mark.paid
def test_running_out_of_credit_pauses_the_session_and_it_resumes(hard, tmp_path):
    with FakeGateway(oracle_brain(hard)) as gateway:
        trace = TraceStore(tmp_path / "trace.sqlite")
        gateway.fail_with = []
        original = gateway._messages

        def broke_after_two(handler, body):
            if len(gateway.calls) >= 2 and not getattr(gateway, "topped_up", False):
                return handler._json(402, {"error": {"code": "insufficient_credits", "message": "top up"}})
            return original(handler, body)
        gateway._messages = broke_after_two
        first = GatingRun(GatingOptions(project="gsynth"), gateway=client(gateway), trace=trace).run()
        assert first["status"] == "paused" and first["reason"] == "insufficient_credits"
        held = invoke(AgentSession(), "gating_next", {"session_id": first["session_id"], "wait_s": 0})
        assert held["result"]["state"] == "paused"

        gateway.topped_up = True
        invoke(AgentSession(), "gating_session_status", {"session_id": first["session_id"], "pause": False})
        second = GatingRun(GatingOptions(project="gsynth", resume_session=first["session_id"], declare_run=False),
                           gateway=client(gateway), trace=trace).run()
    assert second["status"] == "done", second
    final = _final_units(first["session_id"])
    assert all(final[m]["state"] in ("accepted", "accepted_low_confidence") for m in HARD)


@pytest.mark.paid
def test_an_invalid_answer_gets_one_repair_turn_inside_its_worker(gating, tmp_path):
    oracle = Oracle(gating)
    spoiled = {"done": False}

    def brain(packet, body):
        last = body["request"]["messages"][-1]["content"][-1]["text"]
        if not spoiled["done"] and not last.startswith("That answer"):
            spoiled["done"] = True
            return "this is not json"
        return oracle.answer(packet)

    with FakeGateway(brain) as gateway:
        summary = GatingRun(GatingOptions(project="gsynth"), gateway=client(gateway),
                            trace=TraceStore(tmp_path / "t.sqlite")).run()
    assert summary["status"] == "done", summary
    assert summary["invalid_answers"] == 1
    assert summary["model_calls"] == summary["packets"] + 1
    repair = gateway.calls[1]["body"]["request"]["messages"]
    assert repair[-1]["content"][0]["text"].startswith("That answer is not valid")
    assert repair[-2]["role"] == "assistant"


@pytest.mark.paid
def test_several_projects_are_gated_in_parallel_with_one_prefix_write(tmp_path):
    registry.discover(["gating"])
    infos = {name: make_gating_project(tmp_path, name=name, markers=("CD3", "CD8"), seed=i)
             for i, name in enumerate(("gsa", "gsb", "gsc"))}

    def brain(packet, body):
        project = packet["units"][0]["project"]
        return Oracle(infos[project]).answer(packet)

    with FakeGateway(brain) as gateway:
        report = run_many(list(infos), GatingOptions(project=""), gateway=client(gateway), parallel=3,
                          trace=TraceStore(tmp_path / "t.sqlite"))
    assert report["done"] == 3 and report["failed"] == 0, report
    assert report["peak_parallel"] >= 2
    # The system prompt was written to the cache by the first call only.
    writes = [c for c in gateway.calls if c["usage"]["cache_read"] == 0]
    assert len(writes) == 1
    for summary in report["sessions"].values():
        assert summary["cache"]["verdicts"]["miss"] == 0
