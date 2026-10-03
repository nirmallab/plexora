"""Plexora's own agent harness, end to end against a stand-in gateway.

The model is the scripted Oracle from `test_gating_session.py`, answering the
packets the harness sends it; the gateway is `FakeGateway`, which streams the
real wire format and emulates a prompt cache. What is pinned: a session is
gated with no external agent; one structured call per packet; rolling
workers; a byte-stable prefix the cache actually reads; credit refusals pause
rather than fail, and a paused session resumes; the user's own pause parks
the run instead of ending it; invalid answers get one repair
turn; the dev route and the run quote are used as asked.
"""

import json
import threading
import time

import pytest

from plexora.agent import AgentSession, invoke, registry
from plexora.ai import tasks
from plexora.ai.harness import cache_plan, prefix, schema
from plexora.ai.harness.decision import GatingOptions, GatingRun, run_many
from plexora.ai.harness.gateway import GatewayClient, GatewayError, TokenSource
from plexora.ai.harness.orchestrator import Blackboard, Scheduler, TaskGraph
from plexora.ai.harness.trace import TraceStore
from plexora.ai.harness.wire import Usage, text_block, with_breakpoints
from tests.ai_harness_fixtures import FakeGateway, _tokens
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


@pytest.mark.parametrize("workflow", ["gating", "qc"])
def test_every_required_field_is_a_property_the_model_can_send(workflow):
    # A field named like a schema keyword (t2_confirm's `plausibility.pattern`)
    # was once dropped from `properties` but left `required`: no answer could pass.
    def walk(node, kind):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                missing = set(node.get("required") or ()) - set(node["properties"])
                assert not missing, (workflow, kind, missing)
            for v in node.values():
                walk(v, kind)
        elif isinstance(node, list):
            for v in node:
                walk(v, kind)

    for kind in schema._models(workflow):
        walk(schema.for_kind(kind, workflow), kind)
    pattern = schema.for_kind("t2_confirm")["properties"]["plausibility"]["properties"]["pattern"]
    assert "membrane" in pattern["enum"]


def test_the_cache_monitor_calls_a_warm_call_that_misses_the_prefix_a_miss():
    monitor = cache_plan.CacheMonitor()
    assert monitor.observe("fp", 5000, Usage(cache_write_5m=5200, input_uncached=10), warm=False) == "cold"
    assert monitor.observe("fp", 5000, Usage(cache_read=5100, cache_write_5m=300), warm=True) == "hit"
    assert monitor.observe("fp", 5000, Usage(cache_write_5m=5400), warm=True) == "miss"
    # A new worker on a prefix another worker already wrote must read it.
    assert monitor.observe("fp", 5000, Usage(cache_read=5000), warm=False) == "hit"
    assert monitor.counts == {"cold": 1, "hit": 2, "miss": 1, "uncached": 0}


def test_the_cache_monitor_grades_against_the_size_the_provider_reports_not_the_estimate():
    # The characters estimate overstates a dense prefix, and an OpenAI-style cache
    # reads in 128-token blocks: 4608 of an estimated 6239 is the whole prefix.
    monitor = cache_plan.CacheMonitor()
    assert monitor.observe("fp", 6239, Usage(input_uncached=6400), warm=False) == "cold"
    assert monitor.observe("fp", 6239, Usage(cache_read=4608, input_uncached=1800), warm=True) == "hit"
    assert monitor.observe("fp", 6239, Usage(cache_read=4608, input_uncached=2100), warm=True) == "hit"
    # Once a size is known, losing most of it is a miss again.
    assert monitor.observe("fp", 6239, Usage(cache_read=1024, input_uncached=5600), warm=True) == "miss"
    assert monitor.observe("fp", 6239, Usage(input_uncached=6700), warm=True) == "miss"


def test_a_provider_that_never_reports_caching_is_uncached_not_a_miss_per_call(caplog):
    monitor = cache_plan.CacheMonitor()
    assert monitor.observe("fp", 6239, Usage(input_uncached=6400), warm=False) == "cold"
    with caplog.at_level("INFO", logger="plexora.ai.harness"):
        assert monitor.observe("fp", 6239, Usage(input_uncached=6500), warm=True) == "uncached"
        assert monitor.observe("fp", 6239, Usage(input_uncached=6600), warm=True) == "uncached"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert sum("not reported" in r.getMessage() for r in caplog.records) == 1
    assert monitor.counts["uncached"] == 2


def test_a_template_sized_read_is_not_a_cached_prefix_and_a_best_effort_cache_warns_once(caplog):
    # SayGM's TEE models, run air_8c856d5fd8e6: every call reports 4-7 tokens
    # cached (the chat template), and now and then the whole prompt.
    monitor = cache_plan.CacheMonitor()
    assert monitor.observe("fp", 6000, Usage(input_uncached=5650, cache_read=5), warm=False) == "cold"
    with caplog.at_level("INFO", logger="plexora.ai.harness"):
        # Only the template, ever: the provider is not caching this prefix, not missing it.
        assert monitor.observe("fp", 6000, Usage(input_uncached=7260, cache_read=6), warm=True) == "uncached"
        assert monitor.observe("fp", 6000, Usage(input_uncached=109, cache_read=6696), warm=True) == "hit"
        assert monitor.observe("fp", 6000, Usage(input_uncached=8690, cache_read=4), warm=True) == "miss"
        assert monitor.observe("fp", 6000, Usage(input_uncached=8890, cache_read=5), warm=True) == "miss"
    # A warm call's read includes its history, so it never sets the prefix size.
    assert "fp" not in monitor.cached
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "read 4 of ~6000" in warnings[0], warnings
    assert monitor.counts == {"cold": 1, "hit": 1, "miss": 2, "uncached": 1}


def test_a_read_of_prefix_and_history_does_not_raise_the_learned_prefix_size():
    monitor = cache_plan.CacheMonitor()
    assert monitor.observe("fp", 6000, Usage(input_uncached=50, cache_write_5m=7000), warm=False) == "cold"
    # The same worker reads the prefix and its first turn: a hit, and no size learned.
    assert monitor.observe("fp", 6000, Usage(input_uncached=40, cache_read=7000, cache_write_5m=900),
                           warm=True) == "hit"
    assert "fp" not in monitor.cached
    # A new worker reads only the system prompt: that is the prefix's size.
    assert monitor.observe("fp", 6000, Usage(input_uncached=1500, cache_read=4600), warm=False) == "hit"
    assert monitor.cached["fp"] == 4600
    # Later warm reads of prefix and history leave it there.
    assert monitor.observe("fp", 6000, Usage(cache_read=9000, cache_write_5m=800), warm=True) == "hit"
    assert monitor.cached["fp"] == 4600


def test_with_breakpoints_marks_the_newest_turn_and_the_previous_user_turn_on_a_copy():
    history = [{"role": "user", "content": [{"type": "image", "source": {}}, text_block("packet 1")]},
               {"role": "assistant", "content": [text_block("answer 1")]},
               {"role": "user", "content": [text_block("packet 2")]}]
    before = json.dumps(history)
    sent = with_breakpoints(history)
    assert json.dumps(history) == before                      # the stored history is untouched
    marked = [(i, j) for i, m in enumerate(sent) for j, b in enumerate(m["content"]) if "cache_control" in b]
    assert marked == [(0, 1), (2, 0)]
    assert with_breakpoints([]) == []
    one = with_breakpoints([{"role": "user", "content": [text_block("only")]}])
    assert one[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert with_breakpoints([{"role": "user", "content": "plain"}]) == [{"role": "user", "content": "plain"}]


@pytest.mark.paid
def test_each_call_reads_its_history_from_cache_through_rolling_breakpoints(gating, tmp_path):
    oracle = Oracle(gating)
    spoiled = {"done": False}

    def brain(packet, body):
        last = body["request"]["messages"][-1]["content"][-1]["text"]
        if not spoiled["done"] and not last.startswith("That answer"):
            spoiled["done"] = True
            return "this is not json"                            # one repair turn
        return oracle.answer(packet)

    with FakeGateway(brain) as gateway:
        summary = GatingRun(GatingOptions(project="gsynth", units_per_worker=10), gateway=client(gateway),
                            trace=TraceStore(tmp_path / "t.sqlite")).run()
    assert summary["status"] == "done", summary
    for call in gateway.calls:
        request = call["body"]["request"]
        messages = request["messages"]
        marks = [(i, j) for i, m in enumerate(messages) for j, b in enumerate(m["content"])
                 if "cache_control" in b]
        system_marks = sum("cache_control" in b for b in request["system"])
        assert system_marks + len(marks) <= 4
        assert (len(messages) - 1, len(messages[-1]["content"]) - 1) in marks
        earlier = [i for i in range(len(messages) - 1) if messages[i]["role"] == "user"]
        if earlier:
            assert (earlier[-1], len(messages[earlier[-1]]["content"]) - 1) in marks
    # The repair call marks the repair message, the packet before it as well.
    repair = gateway.calls[1]["body"]["request"]["messages"]
    assert repair[-1]["content"][-1]["text"].startswith("That answer is not valid")
    assert "cache_control" in repair[-1]["content"][-1] and "cache_control" in repair[-3]["content"][-1]
    # Warm calls read more than the system prompt: their own earlier turns too.
    system = _tokens(gateway.calls[0]["body"]["request"]["system"])
    warm = [c for c in gateway.calls[1:] if len(c["body"]["request"]["messages"]) > 1]
    assert warm and all(c["usage"]["cache_read"] > system for c in warm), \
        [(c["usage"], len(c["body"]["request"]["messages"])) for c in gateway.calls]


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


def test_a_call_names_its_task_and_an_older_gateway_is_asked_again_without_it():
    with FakeGateway(lambda packet, body: {"kind": "x"}) as gateway:
        from plexora.ai.harness.wire import ModelRequest

        request = ModelRequest(capability="vision_judgement", system=[], task="qc.blur",
                               messages=[{"role": "user", "content": "hi"}])
        gc = client(gateway)
        response = gc.messages(request, idempotency_key="task-00001")
        assert gateway.calls[-1]["body"]["task"] == "qc.blur"
        assert (response.model, response.provider) == ("approved-model-a", "provider-x")
        gateway.refuse_task = True
        gc.messages(request, idempotency_key="task-00002")
        assert "task" not in gateway.calls[-1]["body"]
        assert gateway.calls[-1]["idempotency_key"] == "task-00002"
        # Remembered: the next call does not ask twice.
        before = len(gateway.calls)
        gc.messages(request, idempotency_key="task-00003")
        assert len(gateway.calls) == before + 1


def test_the_trace_gains_the_task_columns_on_an_older_database(tmp_path):
    import sqlite3

    path = tmp_path / "trace.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE model_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
                   "worker INTEGER NOT NULL, seq INTEGER NOT NULL, packet_id TEXT, kind TEXT, capability TEXT, "
                   "prefix_fp TEXT, verdict TEXT, input_uncached INTEGER, cache_read INTEGER, cache_write INTEGER, "
                   "output_tokens INTEGER, price_micro INTEGER, charged_micro INTEGER, cost_micro INTEGER, "
                   "gateway_request_id TEXT, latency_ms INTEGER, valid INTEGER, at REAL NOT NULL)")
    store = TraceStore(path)
    TraceStore(path)                                    # twice is harmless
    store.call("r1", kind="t4_candidates", task="gating.threshold_evaluation", model="m", provider="p")
    assert store.calls("r1")[0]["task"] == "gating.threshold_evaluation"


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
        # Each packet names its task, so the gateway can serve each with its own model.
        kind = json.loads(body["request"]["messages"][-1]["content"][-1]["text"])["kind"]
        assert body["task"] == tasks.task_for("gating", kind), kind
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
    # A worker's later packets also read its earlier packets from cache (the
    # moving breakpoint on the newest message), so only the new packet is written.
    later = [c for c in gateway.calls if len(c["body"]["request"]["messages"]) >= 3]
    assert later, "no worker answered a second packet"
    for call in later:
        # Everything up to the previous call's newest message comes from cache.
        before = call["body"]["request"]["messages"][:-2]
        system = call["body"]["request"]["system"]
        assert call["usage"]["cache_read"] >= _tokens(system) + sum(_tokens(m) for m in before)

    # The local trace has every call, linked by gateway request id.
    report = trace.cache_report(summary["run_id"])
    assert report["calls"] == len(gateway.calls) and report["verdicts"].get("miss", 0) == 0
    assert all(c["gateway_request_id"] for c in trace.calls(summary["run_id"]))
    # ...and what the gateway says served each one, by its own names.
    assert {(c["model"], c["provider"]) for c in trace.calls(summary["run_id"])} == {("approved-model-a", "provider-x")}
    assert "gating.image_inspection" in {c["task"] for c in trace.calls(summary["run_id"])}
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


def _pausing_brain(info, act, *, on_call=2):
    """The Oracle, with the session paused by `act(store, session_id)` while
    the `on_call`th model call is out."""
    from plexora.plugins.gating.server.autogate import engine

    oracle = Oracle(info)
    seen = {"n": 0}

    def brain(packet, body):
        seen["n"] += 1
        if seen["n"] == on_call:
            act(engine.store(), body["context"]["session_id"])
        return oracle.answer(packet)
    return brain


def _resume_later(store, session_id, delay=0.4):
    threading.Timer(delay, lambda: store.set_control(session_id, paused=False,
                                                     paused_by=None)).start()


@pytest.mark.paid
def test_a_viewer_pause_parks_the_run_and_the_answer_is_applied_once(gating, tmp_path, monkeypatch):
    from plexora.ai.harness import decision

    monkeypatch.setattr(decision, "PARK_POLL_S", 0.05)

    def pause(store, session_id):
        store.set_control(session_id, paused=True, paused_by="viewer")
        _resume_later(store, session_id)

    events = []
    with FakeGateway(_pausing_brain(gating, pause)) as gateway:
        summary = GatingRun(GatingOptions(project="gsynth"), gateway=client(gateway),
                            trace=TraceStore(tmp_path / "t.sqlite"), on_event=events.append).run()
    assert summary["status"] == "done", summary
    # The answer held while parked went in once: no second model call for it.
    assert summary["model_calls"] == summary["packets"] == len(gateway.calls)
    packet_ids = [c["packet_id"] for c in gateway.calls]
    assert len(packet_ids) == len(set(packet_ids))
    names = [e["event"] for e in events]
    assert "parked" in names and "paused" not in names
    assert len({e["run_id"] for e in events}) == 1


@pytest.mark.paid
def test_take_over_parks_the_run_detached_and_resume_goes_on_in_the_background(gating, tmp_path,
                                                                                monkeypatch):
    from plexora.agent.sessions import control as session_control
    from plexora.ai.harness import decision
    from plexora.plugins.gating.capabilities_session import record_limit_answers
    from plexora.plugins.gating.server.autogate import engine, schemas

    monkeypatch.setattr(decision, "PARK_POLL_S", 0.05)
    told = []

    def take_over(store, session_id):
        session_control.handle(store, session_id, {"action": "take_over"},
                               tell_tabs=lambda event, record=None, **p: told.append((event, p)),
                               summary_of=engine.summary_of,
                               record_limit_answers=record_limit_answers,
                               limit_decisions=schemas.LIMIT_DECISIONS)
        assert store.control(session_id)["viewer_detached"] is True
        _resume_later(store, session_id)

    with FakeGateway(_pausing_brain(gating, take_over)) as gateway:
        summary = GatingRun(GatingOptions(project="gsynth"), gateway=client(gateway),
                            trace=TraceStore(tmp_path / "t.sqlite")).run()
    assert summary["status"] == "done", summary
    assert summary["model_calls"] == summary["packets"] == len(gateway.calls)
    event, payload = told[0]
    assert event == "control" and payload["taken_over"] is True
    assert payload["paused"] is True and payload["viewer_attached"] is False
    # Resume does not re-attach: the run finished in the background.
    assert engine.store().control(summary["session_id"])["viewer_detached"] is True


@pytest.mark.paid
def test_a_stop_while_parked_ends_the_run_with_no_further_call(gating, tmp_path, monkeypatch):
    from plexora.ai.harness import decision

    monkeypatch.setattr(decision, "PARK_POLL_S", 0.05)

    def pause_then_stop(store, session_id):
        store.set_control(session_id, paused=True, paused_by="viewer")
        threading.Timer(0.4, lambda: store.set_control(session_id, stopped=True,
                                                       stopped_by="viewer", paused=False)).start()

    with FakeGateway(_pausing_brain(gating, pause_then_stop)) as gateway:
        summary = GatingRun(GatingOptions(project="gsynth"), gateway=client(gateway),
                            trace=TraceStore(tmp_path / "t.sqlite")).run()
        calls = len(gateway.calls)
    assert summary["status"] == "stopped", summary
    assert calls == 2


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
def test_an_answer_that_stalls_in_blank_space_is_cut_off_and_repaired(gating, tmp_path, caplog):
    # SayGM, run air_582df209dfec: `"rows":` and then whitespace to max_tokens.
    oracle = Oracle(gating)
    stalled = {"done": False}

    def brain(packet, body):
        last = body["request"]["messages"][-1]["content"][-1]["text"]
        if not stalled["done"] and not last.startswith("That answer"):
            stalled["done"] = True
            return '{"kind": "t2_confirm", "rows": ' + "\n  " * 2000
        return oracle.answer(packet)

    with caplog.at_level("WARNING", logger="plexora.ai.harness"), FakeGateway(brain) as gateway:
        summary = GatingRun(GatingOptions(project="gsynth"), gateway=client(gateway),
                            trace=TraceStore(tmp_path / "t.sqlite")).run()
    assert summary["status"] == "done", summary
    assert summary["invalid_answers"] == 1
    assert any("ran on in blank space" in r.getMessage() for r in caplog.records)
    repair = gateway.calls[1]["body"]["request"]["messages"]
    assert "stalled in blank space" in repair[-1]["content"][0]["text"]
    assert len(repair[-2]["content"][0]["text"]) < 100      # the blank run is not sent back


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
