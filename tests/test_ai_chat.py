"""The conversational agent, its tools and its caches, against a stand-in gateway.

The model is a scripted brain behind `FakeGateway` that answers with
`tool_use` blocks the way a provider streams them. The tools are a handful of
test capabilities registered for the test (a read that counts its calls, a
receipted reversible write, a delete that needs `confirm`, a large read, a
slow read for parallelism) beside core's own. What is pinned: a read runs and
its result goes back to the model; a reversible write reports an undo id; a
destructive call waits for the user and runs on approve, is declined on deny;
the tools array never changes (`load_tool` returns definitions into the
history and `call_tool` runs them), and the newest message carries the second
cache breakpoint; sub-agents
run in parallel on the parent's prefix and return summaries; a credit refusal
pauses; tool results are cached by project revision and offloaded when large.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from pydantic import Field

from plexora.agent import registry, revision
from plexora.agent.policy import Policy
from plexora.agent.registry import Capability
from plexora.agent.schemas import AgentModel
from plexora.ai.harness import cache_plan, offload
from plexora.ai.harness.approvals import DECLINED, decide
from plexora.ai.harness.conversations import ChatService, ConversationStore
from plexora.ai.harness.gateway import GatewayClient, TokenSource
from plexora.ai.harness.runner import AgentRunner
from plexora.ai.harness.toolcache import ToolResultCache, cacheable
from plexora.ai.harness.tools import ToolAdapter
from plexora.ai.harness.trace import TraceStore
from tests.ai_harness_fixtures import FakeGateway, Reply, last_user_text, result_text, tool_results

# -- test capabilities ------------------------------------------------------------------

STATE = {"reads": 0, "value": 1, "erased": [], "slow_now": 0, "slow_peak": 0, "rev": 0, "meddle": False}
SLOW = threading.Lock()


class KeyInput(AgentModel):
    key: str = Field("x", description="Which value.")


class WriteInput(AgentModel):
    value: int


class GuardedInput(AgentModel):
    project: str
    value: int
    expected_revision: str | None = None


class EraseInput(AgentModel):
    what: str
    confirm: bool = False


def _read(call, inp):
    STATE["reads"] += 1
    return {"key": inp.key, "value": STATE["value"], "reads": STATE["reads"]}


def _write(call, inp):
    from plexora.agent.receipts import make_receipt

    before, STATE["value"] = STATE["value"], inp.value
    receipt = make_receipt(call, changed=True, before={"value": before}, after={"value": inp.value},
                           undo_hint={"tool": "tc_write", "arguments": {"value": before}})
    return {"receipt": receipt.model_dump(mode="json")}


def _guarded(call, inp):
    """A write guarded by a revision, like set_gate: r<n>, bumped by each write."""
    from plexora.agent.errors import AgentError

    current = f"r{STATE['rev']}"
    if inp.expected_revision is not None and inp.expected_revision != current:
        raise AgentError("conflict", "the saved state changed since it was read",
                         detail={"current_revision": current})
    STATE["rev"] += 1
    after = f"r{STATE['rev']}"
    if STATE["meddle"]:                     # someone else writes right after this one
        STATE["rev"] += 1
    return {"receipt": {"project": inp.project, "revision_before": current, "revision_after": after}}


def _erase(call, inp):
    STATE["erased"].append(inp.what)
    return {"erased": inp.what}


def _big(call, inp):
    return {"rows": [{"i": i, "marker": f"M{i}", "note": "padding " * 6} for i in range(900)]}


def _slow(call, inp):
    with SLOW:
        STATE["slow_now"] += 1
        STATE["slow_peak"] = max(STATE["slow_peak"], STATE["slow_now"])
    time.sleep(0.4)
    with SLOW:
        STATE["slow_now"] -= 1
    return {"key": inp.key, "done": True}


def _caps():
    def cap(**kw):
        return Capability(owner="testchat", entitlement="free", **kw)
    return [
        cap(name="testchat.read", tool_name="tc_read", purpose="Read a test value. Counts its calls.",
            permission="read", input_model=KeyInput, handler=_read),
        cap(name="testchat.big", tool_name="tc_big", purpose="A large read.", permission="read",
            input_model=KeyInput, handler=_big),
        cap(name="testchat.slow", tool_name="tc_slow", purpose="A slow read.", permission="read",
            input_model=KeyInput, handler=_slow),
        cap(name="testchat.rows", tool_name="tc_rows", purpose="Cell-level rows.", permission="read",
            egress="row_level", input_model=KeyInput, handler=_read),
        cap(name="testchat.write", tool_name="tc_write", purpose="Set the test value (reversible).",
            permission="reversible_write", input_model=WriteInput, handler=_write),
        cap(name="testchat.guarded", tool_name="tc_guarded", purpose="A revision-guarded write.",
            permission="reversible_write", input_model=GuardedInput, handler=_guarded),
        cap(name="testchat.erase", tool_name="tc_erase", purpose="Erase something for good.",
            permission="destructive", input_model=EraseInput, handler=_erase),
    ]


@pytest.fixture
def caps():
    registry.discover([])
    before = dict(registry._REGISTRY)
    added = _caps()
    for c in added:
        registry.register(c)
    STATE.update(reads=0, value=1, erased=[], slow_now=0, slow_peak=0, rev=0, meddle=False)
    yield {c.tool_name: c for c in added}
    # The registry as it was: these, and any plugin a test discovered, go.
    registry._REGISTRY.clear()
    registry._REGISTRY.update(before)


def client(gateway):
    return GatewayClient(gateway.url, tokens=TokenSource("PLXAI1.test"), sleep=lambda s: None)


def make_runner(gateway, tmp_path, *, policy=None, **options):
    store = ConversationStore(tmp_path / "conversations")
    runner = AgentRunner.create(store, gateway=client(gateway), policy=policy or Policy(),
                                trace=TraceStore(tmp_path / "trace.sqlite"),
                                cache=ToolResultCache(tmp_path / "toolcache"), poll_s=0.02, **options)
    return runner, store


def scripted(*steps):
    """A brain that answers the main agent's calls in order."""
    queue = list(steps)

    def brain(packet, body):
        step = queue.pop(0) if queue else Reply("done.")
        return step(body) if callable(step) else step
    return brain


def events_of(runner, text, *, on_event=None):
    out = []
    for event in runner.turn(text):
        out.append(event)
        if on_event:
            on_event(event)
    return out


def kinds(events):
    return [e["event"] for e in events]


# -- a read, a write, the prefix --------------------------------------------------------------


def test_a_read_tool_runs_and_its_result_goes_back_to_the_model(caps, tmp_path):
    brain = scripted(Reply("Let me look.", [{"name": "load_tool", "input": {"names": ["tc_read"]}}]),
                     Reply("", [{"name": "call_tool", "input": {"name": "tc_read", "arguments": {"key": "alpha"}}}]),
                     lambda body: Reply("The value is " + json.loads(result_text(tool_results(body)[0]))["value"]
                                        .__str__() + "."))
    with FakeGateway(brain) as gateway:
        runner, store = make_runner(gateway, tmp_path)
        events = events_of(runner, "What is alpha?")
        calls = gateway.calls
    assert kinds(events)[0] == "turn_started"
    assert events[-1]["event"] == "done" and events[-1]["text"] == "The value is 1."
    results = [e for e in events if e["event"] == "tool_result"]
    assert [(r["tool"], r["ok"]) for r in results] == [("load_tool", True), ("tc_read", True)]
    assert any(e["event"] == "text_delta" for e in events)
    assert sum(e["event"] == "usage" for e in events) == 3
    # The read's answer reached the model as a tool_result for that tool_use.
    third = calls[2]["body"]["request"]
    block = tool_results(calls[2]["body"])[0]
    assert block["tool_use_id"] == calls[1]["tool_uses"][0].get("id", "toolu_2_0")
    assert json.loads(result_text(block))["key"] == "alpha"
    assert third["messages"][0]["content"][-1]["text"] == "What is alpha?"
    # Every call is a chat call, with the conversation as its session.
    assert {c["body"]["capability"] for c in calls} == {"text_reasoning"}
    assert {c["body"]["task"] for c in calls} == {"chat.turn"}
    assert {c["body"]["context"]["feature"] for c in calls} == {"chat"}
    # The history and the record are on disk.
    assert store.load(runner.conversation_id)["state"] == "idle"
    assert len(store.load_messages(runner.conversation_id)) == 6


def test_the_conversation_is_sent_with_rolling_breakpoints_and_saved_without_them(caps, tmp_path):
    brain = scripted(Reply("", [{"name": "load_tool", "input": {"names": ["tc_read"]}}]),
                     Reply("", [{"name": "tc_read", "input": {"key": "alpha"}}]),
                     Reply("done"))
    with FakeGateway(brain) as gateway:
        runner, store = make_runner(gateway, tmp_path)
        events_of(runner, "What is alpha?")
        calls = [c["body"]["request"] for c in gateway.calls]
    for request in calls:
        messages = request["messages"]
        assert "cache_control" in messages[-1]["content"][-1]            # the newest turn, a tool_result too
        assert sum("cache_control" in b for m in messages for b in m["content"]) <= 2
    # The third call also marks the user turn before it (the first tool_result).
    third = calls[2]["messages"]
    assert [m["role"] for m in third] == ["user", "assistant", "user", "assistant", "user"]
    assert "cache_control" in third[2]["content"][-1] and "cache_control" not in third[0]["content"][-1]
    # The saved history carries none, and the warm calls read their earlier turns.
    saved = store.load_messages(runner.conversation_id)
    assert not any("cache_control" in b for m in saved for b in m["content"] if isinstance(b, dict))
    assert gateway.calls[2]["usage"]["cache_read"] > gateway.calls[1]["usage"]["cache_read"]


def test_load_tool_returns_definitions_and_the_tools_array_never_changes(caps, tmp_path):
    brain = scripted(Reply("", [{"name": "load_tool", "input": {"names": ["tc_read", "tc_write"]}}]),
                     Reply("", [{"name": "call_tool", "input": {"name": "tc_read", "arguments": {}}}]),
                     Reply("ok"))
    with FakeGateway(brain) as gateway:
        runner, _ = make_runner(gateway, tmp_path)
        events_of(runner, "hello")
        first, second, third = (c["body"]["request"] for c in gateway.calls)
        usage = [c["usage"] for c in gateway.calls]
    # One tools array for the whole conversation, so the cached prefix never moves.
    assert json.dumps(first["tools"]) == json.dumps(second["tools"]) == json.dumps(third["tools"])
    assert {"load_tool", "call_tool"} <= {t["name"] for t in first["tools"]}
    assert "tc_read" not in {t["name"] for t in first["tools"]}
    assert json.dumps(second["system"]) == json.dumps(first["system"])
    assert cache_plan.fingerprint([first["tools"], first["system"]]) == runner.record["prefix_fp"]
    # The definitions came back as load_tool's result, in the history.
    loaded = json.loads(result_text(tool_results(gateway.calls[1]["body"])[0]))
    assert [t["name"] for t in loaded["tools"]] == ["tc_read", "tc_write"] and loaded["call_with"] == "call_tool"
    assert all("input_schema" in t for t in loaded["tools"])
    # Names and purposes of the deferred tools are in the system prompt, not their schemas.
    catalog = first["system"][-1]["text"]
    assert "- tc_read: Read a test value" in catalog and "input_schema" not in catalog
    assert [i for i, b in enumerate(first["system"]) if "cache_control" in b] == [len(first["system"]) - 1]
    # Every call after the first reads the whole earlier conversation from cache.
    assert usage[1]["cache_read"] > 0 and usage[2]["cache_read"] > usage[1]["cache_read"]
    assert usage[2]["cache_write_5m"] < usage[1]["cache_read"]
    # A resumed conversation sends exactly the same tools and system, and remembers what was loaded.
    reopened = AgentRunner.open(runner.store, runner.conversation_id, gateway=runner.gateway, trace=runner.trace)
    assert reopened.adapter.definitions() == first["tools"] and reopened.system == first["system"]
    assert reopened.adapter.loaded == ["tc_read", "tc_write"]


def test_a_tool_called_before_it_was_loaded_runs_and_shows_its_definition_on_error(caps, tmp_path):
    brain = scripted(Reply("", [{"name": "call_tool", "input": {"name": "tc_write", "arguments": {"value": "x"}}}]),
                     Reply("", [{"name": "call_tool", "input": {"name": "nope", "arguments": {}}}]),
                     Reply("ok"))
    with FakeGateway(brain) as gateway:
        runner, _ = make_runner(gateway, tmp_path)
        events = events_of(runner, "set it")
        calls = gateway.calls
    bad = tool_results(calls[1]["body"])[0]
    assert bad.get("is_error") and "Definition:" in result_text(bad) and "input_schema" in result_text(bad)
    unknown = tool_results(calls[2]["body"])[0]
    assert unknown.get("is_error") and "catalog" in result_text(unknown)
    assert [e["tool"] for e in events if e["event"] == "tool_call"] == ["tc_write", "nope"]


def test_the_prefix_carries_nothing_volatile(caps, tmp_path):
    with FakeGateway(scripted(Reply("hi"))) as gateway:
        a, _ = make_runner(gateway, tmp_path / "a")
        b, _ = make_runner(gateway, tmp_path / "b")
    assert a.conversation_id != b.conversation_id
    assert a.system == b.system and a.adapter.definitions() == b.adapter.definitions()
    text = json.dumps(a.system)
    assert a.conversation_id not in text and str(time.gmtime().tm_year) not in text


def test_a_reversible_write_runs_and_reports_an_undo_id(caps, tmp_path):
    brain = scripted(Reply("", [{"name": "tc_write", "input": {"value": 7}}]), Reply("Set to 7."))
    with FakeGateway(brain) as gateway:
        runner, _ = make_runner(gateway, tmp_path)
        events = events_of(runner, "set it to 7")
    result = next(e for e in events if e["event"] == "tool_result")
    assert result["ok"] and result["permission"] == "reversible_write" and result["undo"] is True
    assert result["operation_id"].startswith("op_")
    assert STATE["value"] == 7
    from plexora.agent.audit import AuditLog

    assert AuditLog().find(result["operation_id"])["capability"] == "testchat.write"


# -- approvals ------------------------------------------------------------------------------


def _destructive_brain(via: str = "native"):
    use = ({"name": "tc_erase", "input": {"what": "region 3"}} if via == "native" else
           {"name": "call_tool", "input": {"name": "tc_erase", "arguments": {"what": "region 3"}}})
    return scripted(Reply("", [use]),
                    lambda body: Reply("declined" if tool_results(body)[0].get("is_error") else "erased"))


@pytest.mark.parametrize("via", ["native", "call_tool"])
@pytest.mark.parametrize("approve", [True, False])
def test_a_destructive_call_waits_for_the_user(caps, tmp_path, approve, via):
    # Through call_tool too: the approval is asked for the tool it names.
    with FakeGateway(_destructive_brain(via)) as gateway:
        runner, store = make_runner(gateway, tmp_path)
        seen = {}

        def answer(event):
            if event["event"] == "approval_requested":
                control = store.control(runner.conversation_id)
                seen["paused"] = (control["paused"], control["paused_by"])
                seen["erased_before"] = list(STATE["erased"])
                decide(store, runner.conversation_id, event["approval_id"], approve)

        events = events_of(runner, "delete region 3", on_event=answer)
        last = gateway.calls[-1]["body"]
    assert seen == {"paused": (True, "approval"), "erased_before": []}
    asked = next(e for e in events if e["event"] == "approval_requested")
    assert asked["tool"] == "tc_erase" and asked["permission"] == "destructive"
    result = next(e for e in events if e["event"] == "tool_result")
    block = tool_results(last)[0]
    if approve:
        assert result["ok"] and STATE["erased"] == ["region 3"] and result["source"] == "approved"
        assert events[-1]["text"] == "erased"
    else:
        assert not result["ok"] and STATE["erased"] == [] and result["source"] == "declined"
        assert block.get("is_error") and result_text(block) == DECLINED
        assert events[-1]["text"] == "declined"
    assert store.control(runner.conversation_id)["paused"] is False


def test_a_stop_while_waiting_for_approval_ends_the_turn(caps, tmp_path):
    with FakeGateway(_destructive_brain()) as gateway:
        runner, store = make_runner(gateway, tmp_path)

        def stop(event):
            if event["event"] == "approval_requested":
                ChatService(store).control(runner.conversation_id, "stop")

        events = events_of(runner, "delete region 3", on_event=stop)
    assert "stopped" in kinds(events) and STATE["erased"] == []
    assert store.load(runner.conversation_id)["state"] == "stopped"


# -- credit ----------------------------------------------------------------------------------


def test_a_credit_refusal_pauses_the_conversation_and_the_next_message_continues(caps, tmp_path):
    with FakeGateway(scripted(Reply("Back again."))) as gateway:
        runner, store = make_runner(gateway, tmp_path)
        gateway.fail_with = [(402, "insufficient_credits")]
        events = events_of(runner, "hello")
        paused = next(e for e in events if e["event"] == "paused")
        assert paused["reason"] == "insufficient_credits"
        control = store.control(runner.conversation_id)
        assert control["paused"] and control["paused_by"] == "credits"
        assert store.load(runner.conversation_id)["state"] == "paused"
        events = events_of(runner, "try again")
        sent = gateway.calls[-1]["body"]["request"]["messages"]
    assert events[-1]["event"] == "done" and events[-1]["text"] == "Back again."
    # Both words reached the model in one user turn, so roles still alternate.
    assert [b["text"] for b in sent[0]["content"]] == ["hello", "try again"] and len(sent) == 1


# -- sub-agents ---------------------------------------------------------------------------------


def test_spawn_agents_runs_two_sub_agents_in_parallel_and_returns_their_summaries(caps, tmp_path):
    def brain(packet, body):
        sub = body["context"]["agent"] == "chat_subagent"
        results = tool_results(body)
        if sub:
            brief = body["request"]["messages"][0]["content"][0]["text"]
            name = "left" if "left half" in brief else "right"
            if not results:
                return Reply("", [{"name": "tc_slow", "input": {"key": name}}])
            return Reply(json.dumps({"summary": f"{name} half read", "findings": [name]}))
        if not results:
            return Reply("", [{"name": "spawn_agents", "input": {"agents": [
                {"id": "left", "role": "reader", "brief": "Read the left half.", "tools": ["tc_slow"]},
                {"id": "right", "role": "reader", "brief": "Read the right half.", "tools": ["tc_slow"]}]}}])
        return Reply("Both halves: " + result_text(results[0]))

    with FakeGateway(brain) as gateway:
        runner, _ = make_runner(gateway, tmp_path)
        events = events_of(runner, "read both halves")
        calls = gateway.calls
    assert STATE["slow_peak"] == 2
    final = json.loads(events[-1]["text"].split("Both halves: ", 1)[1])
    assert {a["agent"]: a["summary"] for a in final["agents"]} == {"left": "left half read",
                                                                   "right": "right half read"}
    assert all(a["state"] == "done" and a["model_calls"] == 2 for a in final["agents"])
    # The fork rule: every sub-agent call sends the parent's exact tools and system.
    main = calls[0]["body"]["request"]
    for call in calls:
        request = call["body"]["request"]
        assert request["tools"] == main["tools"] and request["system"] == main["system"]
    subs = [c for c in calls if c["body"]["context"]["agent"] == "chat_subagent"]
    assert {c["body"]["task"] for c in subs} == {"chat.subagent"}
    assert len(subs) == 4
    assert {e["agent"] for e in events if e["event"] == "agent_finished"} == {"left", "right"}
    # A sub-agent's first call reads the prefix the parent wrote.
    assert all(c["usage"]["cache_read"] > 0 for c in subs)


def test_a_sub_agent_is_held_to_its_tools_and_cannot_spawn_below_the_depth_bound(caps, tmp_path):
    def brain(packet, body):
        results = tool_results(body)
        if body["context"]["agent"] == "chat_subagent":
            if not results:
                return Reply("", [{"name": "tc_write", "input": {"value": 9}},
                                  {"name": "spawn_agents", "input": {"agents": [{"role": "x", "brief": "y"}]}}])
            return Reply(json.dumps({"summary": " | ".join(result_text(r) for r in results)}))
        if not results:
            return Reply("", [{"name": "spawn_agents", "input": {"agents": [{"role": "r", "brief": "go"}]}}])
        return Reply(result_text(results[0]))

    with FakeGateway(brain) as gateway:
        runner, _ = make_runner(gateway, tmp_path)
        events = events_of(runner, "go")
    summary = json.loads(events[-1]["text"])["agents"][0]["summary"]
    assert "not one of the tools this sub-agent was given" in summary
    assert "may not spawn below depth 1" in summary
    assert STATE["value"] == 1


def test_wait_false_returns_ids_and_await_agents_collects_them(caps, tmp_path):
    def brain(packet, body):
        results = tool_results(body)
        if body["context"]["agent"] == "chat_subagent":
            return Reply(json.dumps({"summary": "fine"}))
        if not results:
            return Reply("", [{"name": "spawn_agents", "input": {"wait": False, "agents": [
                {"id": "a1", "role": "r", "brief": "one"}, {"id": "a2", "role": "r", "brief": "two",
                                                            "depends_on": ["a1"]}]}}])
        text = result_text(results[0])
        if "started" in text:
            return Reply("", [{"name": "await_agents", "input": {"ids": json.loads(text)["started"]}}])
        return Reply(text)

    with FakeGateway(brain) as gateway:
        runner, _ = make_runner(gateway, tmp_path)
        events = events_of(runner, "fan out")
    agents = json.loads(events[-1]["text"])["agents"]
    assert [(a["agent"], a["summary"]) for a in agents] == [("a1", "fine"), ("a2", "fine")]


def test_the_board_is_shared_between_agents(caps, tmp_path):
    def brain(packet, body):
        results = tool_results(body)
        if body["context"]["agent"] == "chat_subagent":
            if not results:
                return Reply("", [{"name": "post_board", "input": {"key": "gate:CD3", "value": 5.9}}])
            return Reply(json.dumps({"summary": "posted"}))
        if not results:
            return Reply("", [{"name": "spawn_agents", "input": {"agents": [{"role": "r", "brief": "post"}]}}])
        if "agents" in result_text(results[0]):
            return Reply("", [{"name": "read_board", "input": {"keys": ["gate:*"]}}])
        return Reply(result_text(results[0]))

    with FakeGateway(brain) as gateway:
        runner, store = make_runner(gateway, tmp_path)
        events = events_of(runner, "board")
    assert json.loads(events[-1]["text"]) == {"facts": {"gate:CD3": 5.9}}
    assert store.load(runner.conversation_id)["board"]["gate:CD3"] == 5.9


# -- compaction ---------------------------------------------------------------------------------


def test_a_long_conversation_is_compacted_without_touching_the_prefix(caps, tmp_path):
    with FakeGateway(lambda packet, body: Reply("x" * 3000)) as gateway:
        runner, _ = make_runner(gateway, tmp_path, compact_at_tokens=3000)
        compacted = []
        for n in range(6):
            compacted += [e for e in events_of(runner, f"question {n} " + "y" * 2000) if e["event"] == "compacted"]
        calls = gateway.calls
    assert compacted
    last = calls[-1]["body"]["request"]
    assert last["system"] == calls[0]["body"]["request"]["system"]
    assert last["tools"] == calls[0]["body"]["request"]["tools"]
    first_text = last["messages"][0]["content"][0]["text"]
    assert first_text.startswith("[Summary of the earlier conversation") and "User: question 0" in first_text
    assert last["messages"][0]["role"] == "user"


# -- the tool-result cache and offload ----------------------------------------------------------------


def test_a_read_is_cached_until_a_receipted_write_bumps_the_revision(caps, tmp_path):
    from plexora.agent import AgentSession

    cache = ToolResultCache(tmp_path / "cache")
    adapter = ToolAdapter(AgentSession(), policy=Policy(), cache=cache)
    first = adapter.execute("t1", "tc_read", {"key": "a"})
    second = adapter.execute("t2", "tc_read", {"key": "a"})
    assert (first.source, second.source) == ("live", "cache")
    assert STATE["reads"] == 1 and second.content == first.content
    other = adapter.execute("t3", "tc_read", {"key": "b"})          # other arguments, other entry
    assert other.source == "live" and STATE["reads"] == 2
    token = revision.token(None)
    write = adapter.execute("t4", "tc_write", {"value": 3})
    assert write.source == "live" and revision.token(None) != token
    again = adapter.execute("t5", "tc_read", {"key": "a"})
    assert again.source == "live" and STATE["reads"] == 3
    assert json.loads(again.content[0]["text"])["value"] == 3
    # Writes are never cached.
    assert adapter.execute("t6", "tc_write", {"value": 3}).source == "live"
    assert cache.hits == 1


@pytest.fixture
def project_p(monkeypatch):
    """A project named P, as far as the registry's lookup is concerned
    (tc_guarded declares no requirements, so the record is never read)."""
    from plexora.agent.session import AgentSession

    monkeypatch.setattr(AgentSession, "project", lambda self, name: object())


def _two_guarded_writes():
    return scripted(Reply("", [{"name": "tc_guarded", "input": {"project": "P", "value": 1, "expected_revision": "r0"}},
                               {"name": "tc_guarded", "input": {"project": "P", "value": 2, "expected_revision": "r0"}}]),
                    Reply("done"))


def test_writes_sent_together_carry_the_revision_their_own_earlier_write_made(caps, project_p, tmp_path):
    with FakeGateway(_two_guarded_writes()) as gateway:
        runner, _ = make_runner(gateway, tmp_path)
        events = events_of(runner, "gate both")
    results = [e for e in events if e["event"] == "tool_result" and e["tool"] == "tc_guarded"]
    calls = [e for e in events if e["event"] == "tool_call" and e["tool"] == "tc_guarded"]
    assert [r["ok"] for r in results] == [True, True]
    assert [c["arguments"]["expected_revision"] for c in calls] == ["r0", "r1"]
    assert STATE["rev"] == 2


def test_a_change_from_elsewhere_between_them_still_conflicts(caps, project_p, tmp_path):
    STATE["meddle"] = True
    with FakeGateway(_two_guarded_writes()) as gateway:
        runner, _ = make_runner(gateway, tmp_path)
        events = events_of(runner, "gate both")
    results = [e for e in events if e["event"] == "tool_result" and e["tool"] == "tc_guarded"]
    assert [r["ok"] for r in results] == [True, False]
    assert results[1]["summary"].startswith("conflict")


def test_server_info_answers_in_a_conversation_as_skill_dataset_triage_expects(caps):
    from plexora.agent import AgentSession

    adapter = ToolAdapter(AgentSession(), policy=Policy())
    outcome = adapter.execute("t1", "server_info", {})
    info = json.loads(outcome.content[0]["text"])
    assert not outcome.is_error and outcome.source == "local"
    assert {"plexora_version", "data_root", "policy", "skills", "license"} <= set(info)
    assert "dataset-triage" in info["skills"] and info["n_tools"] == len(adapter.catalog)
    via = adapter.execute("t2", "call_tool", {"name": "server_info", "arguments": {}})
    assert not via.is_error


def test_what_is_never_cached(caps):
    assert cacheable(caps["tc_read"])
    assert not cacheable(caps["tc_write"]) and not cacheable(caps["tc_erase"])
    assert not cacheable(caps["tc_rows"])                                # row_level egress
    viewer = next(c for c in registry.all_capabilities() if c.viewer_required)
    assert not cacheable(viewer)


def test_a_project_write_invalidates_that_project_only():
    a, b = revision.token("A"), revision.token("B")
    revision.bump("A")
    assert revision.token("A") != a and revision.token("B") == b
    revision.bump(None)
    assert revision.token("B") != b


def test_cache_hits_are_recorded_in_the_trace(caps, tmp_path):
    brain = scripted(Reply("", [{"name": "tc_read", "input": {"key": "k"}}]),
                     Reply("", [{"name": "tc_read", "input": {"key": "k"}}]), Reply("same"))
    with FakeGateway(brain) as gateway:
        runner, _ = make_runner(gateway, tmp_path)
        events_of(runner, "twice")
    sources = [row["source"] for row in runner.trace.tool_calls(runner.conversation_id)]
    assert sources == ["live", "cache"]
    assert runner.trace.cache_report(runner.conversation_id)["tool_calls"] == {"live": 1, "cache": 1}


def test_a_large_result_is_offloaded_and_read_back_in_slices(caps, tmp_path):
    from plexora.agent import AgentSession

    adapter = ToolAdapter(AgentSession(), policy=Policy(), offload_root=tmp_path / "off")
    outcome = adapter.execute("t1", "tc_big", {})
    stub = json.loads(outcome.content[0]["text"])
    assert stub["offloaded"] and stub["artifact_id"] == outcome.offloaded and stub["kind"] == "tool_result"
    assert stub["size"] > 4000 * offload.CHARS_PER_TOKEN and len(outcome.content[0]["text"]) < 4000
    stored = (tmp_path / "off" / f"{stub['artifact_id']}.txt").read_text(encoding="utf-8")
    assert stored.startswith(stub["head"]) and stored.endswith(stub["tail"])
    piece = adapter.execute("t2", "read_artifact", {"artifact_id": stub["artifact_id"], "start": 100,
                                                    "end": 400})
    assert json.loads(piece.content[0]["text"])["text"] == stored[100:400]
    found = json.loads(adapter.execute("t3", "read_artifact", {"artifact_id": stub["artifact_id"],
                                                               "query": '"M417"'}).content[0]["text"])
    assert found["n_matches"] == 1 and '"M417"' in found["matches"][0]["text"]
    # The same result is stored once, under the same id.
    assert adapter.execute("t4", "tc_big", {}).offloaded == stub["artifact_id"]
    assert adapter.execute("t5", "read_artifact", {"artifact_id": "off_nope"}).is_error


def test_the_catalog_follows_the_policy(caps):
    loose = {e["tool"] for e in ToolAdapter(None, policy=Policy()).catalog}
    assert {"tc_read", "tc_write", "tc_erase"} <= loose
    assert "tc_rows" not in loose                                     # row_level is not allowed by default
    assert not any(t.startswith("ai_chat") for t in loose)
    reads = {e["tool"] for e in ToolAdapter(None, policy=Policy(allow_writes=False)).catalog}
    assert "tc_read" in reads and "tc_write" not in reads and "tc_erase" not in reads
    names = [e["tool"] for e in ToolAdapter(None, policy=Policy()).catalog]
    assert names == sorted(names)


# -- the capabilities and the service ------------------------------------------------------------------


@pytest.fixture
def service(tmp_path):
    from plexora.ai.harness import conversations

    def install(gateway):
        made = ChatService(ConversationStore(tmp_path / "conversations"), gateway_factory=lambda: client(gateway),
                           cache=ToolResultCache(tmp_path / "cache"), trace=TraceStore(tmp_path / "trace.sqlite"),
                           runner_options={"poll_s": 0.02})
        conversations.set_service(made)
        return made
    yield install
    conversations.set_service(None)


@pytest.mark.paid
def test_the_chat_capabilities_drive_a_conversation(caps, service):
    from plexora.agent import AgentSession, invoke

    with FakeGateway(scripted(Reply("", [{"name": "tc_read", "input": {}}]), Reply("one"))) as gateway:
        service(gateway)
        session = AgentSession()
        started = invoke(session, "ai_chat_start", {"title": "t"})
        assert started["ok"], started
        cid = started["result"]["conversation_id"]
        sent = invoke(session, "ai_chat_send", {"conversation_id": cid, "text": "hi", "wait_s": 10})
        assert sent["ok"], sent
        history = invoke(session, "ai_chat_history", {"conversation_id": cid})["result"]
    names = [e["event"] for e in history["events"]]
    assert names[0] == "disclosure" and history["events"][0]["text"] == "AI-generated; verify before relying on it."
    assert "user_message" in names and "tool_result" in names and names[-1] == "turn_finished"
    assert "text_delta" not in names                                     # deltas are live only
    assert history["charged_micro"] > 0 and history["state"] == "idle"
    listed = invoke(session, "ai_chat_history", {})["result"]["conversations"]
    assert [c["session_id"] for c in listed] == [cid]


def test_the_chat_capabilities_are_paid():
    from plexora.agent import AgentSession, invoke

    registry.discover([])
    refused = invoke(AgentSession(), "ai_chat_start", {})
    assert not refused["ok"] and refused["error"]["code"] == "license_required"


@pytest.mark.paid
def test_approving_a_delete_needs_the_users_policy(caps, service):
    from plexora.agent import AgentSession, invoke

    with FakeGateway(_destructive_brain()) as gateway:
        made = service(gateway)
        session = AgentSession()
        cid = invoke(session, "ai_chat_start", {})["result"]["conversation_id"]
        invoke(session, "ai_chat_send", {"conversation_id": cid, "text": "delete it"})
        approval = _wait_for_approval(made, cid)
        refused = invoke(session, "ai_chat_approve", {"conversation_id": cid, "approval_id": approval,
                                                      "decision": "approve"})
        assert not refused["ok"] and refused["error"]["code"] == "permission_required"
        ok = invoke(session, "ai_chat_approve", {"conversation_id": cid, "approval_id": approval,
                                                 "decision": "approve"}, policy=Policy(allow_destructive=True))
        assert ok["ok"], ok
        assert made.wait(cid, 10)
    assert STATE["erased"] == ["region 3"]


def _wait_for_approval(service, cid, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = service.store.control(cid).get("approvals") or {}
        for approval_id, approval in pending.items():
            if approval.get("status") == "pending":
                return approval_id
        time.sleep(0.02)
    raise AssertionError("no approval was requested")


# -- the HTTP blueprint ----------------------------------------------------------------------------------


@pytest.fixture
def http():
    import plexora

    return plexora.app.test_client()


@pytest.mark.paid
def test_the_blueprint_starts_sends_streams_and_approves(caps, service, http):
    with FakeGateway(_destructive_brain()) as gateway:
        made = service(gateway)
        started = http.post("/ai/v1/conversations", json={"title": "panel"}).get_json()
        assert started["ok"], started
        cid = started["conversation_id"]
        assert http.post(f"/ai/v1/conversations/{cid}/messages", json={"text": "delete it"}).get_json()["ok"]
        approval = _wait_for_approval(made, cid)
        polled = http.get(f"/ai/v1/conversations/{cid}/events?after=0&wait=1").get_json()
        assert any(e["event"] == "approval_requested" for e in polled["events"])
        assert polled["control"]["pending_approvals"][0]["approval_id"] == approval
        answer = http.post(f"/ai/v1/conversations/{cid}/approve",
                           json={"approval_id": approval, "decision": "approve"}).get_json()
        assert answer["ok"] and answer["approval"]["status"] == "approved"
        assert made.wait(cid, 10)
        stream = http.get(f"/ai/v1/conversations/{cid}/events?after=0&until_idle=1",
                          headers={"Accept": "text/event-stream"})
        assert stream.mimetype == "text/event-stream"
        body = stream.get_data(as_text=True)
    names = [line[len("event: "):] for line in body.splitlines() if line.startswith("event: ")]
    assert names[0] == "disclosure" and "approval_requested" in names and "tool_result" in names
    assert names[-2:] == ["turn_finished", "idle"]
    assert STATE["erased"] == ["region 3"]
    control = http.post(f"/ai/v1/conversations/{cid}/control", json={"action": "pause"}).get_json()
    assert control["control"]["paused"] is True
    bad = http.post(f"/ai/v1/conversations/{cid}/control", json={"action": "explode"})
    assert bad.status_code == 400


def test_the_blueprint_is_paid(http):
    refused = http.post("/ai/v1/conversations", json={})
    assert refused.status_code == 403
    assert refused.get_json()["license"]["code"] == "license_required"


# -- the CLI ----------------------------------------------------------------------------------------------


def test_plexora_ai_chat_is_a_repl(caps, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from plexora.ai.harness import cli

    brain = scripted(Reply("", [{"name": "tc_erase", "input": {"what": "r"}}]), Reply("gone"))
    with FakeGateway(brain) as gateway:
        monkeypatch.setenv("PLEXORA_AI_TOKEN", "PLXAI1.test")
        monkeypatch.setenv("PLEXORA_AI_GATEWAY", gateway.url)
        answers = iter(["erase r", "y", ""])
        lines = []
        code = cli.chat_command(SimpleNamespace(resume=None, dev=False, model=None, gateway=None),
                                read=lambda prompt: next(answers), write=lines.append)
    assert code == 0 and STATE["erased"] == ["r"]
    assert "verify before relying on it" in lines[0]
    assert any("tc_erase (destructive)" in line for line in lines)
    assert "gone" in capsys.readouterr().out


@pytest.mark.paid
def test_the_undo_chip_route_undoes_a_write_the_conversation_made(caps, service, http):
    brain = scripted(Reply("", [{"name": "tc_write", "input": {"value": 5}}]), Reply("set"), Reply("noted"))
    with FakeGateway(brain) as gateway:
        made = service(gateway)
        cid = http.post("/ai/v1/conversations", json={}).get_json()["conversation_id"]
        http.post(f"/ai/v1/conversations/{cid}/messages", json={"text": "set 5"})
        assert made.wait(cid, 10)
        result = next(e for e in made.store.decisions(cid) if e.get("event") == "tool_result")
        assert STATE["value"] == 5 and result["undo"]
        foreign = http.post(f"/ai/v1/conversations/{cid}/undo", json={"operation_id": "op_not_ours"})
        assert foreign.status_code == 400
        undone = http.post(f"/ai/v1/conversations/{cid}/undo", json={"operation_id": result["operation_id"]})
        assert undone.get_json()["ok"], undone.get_json()
        assert STATE["value"] == 1
        http.post(f"/ai/v1/conversations/{cid}/messages", json={"text": "and now?"})
        assert made.wait(cid, 10)
        told = gateway.calls[-1]["body"]["request"]["messages"][-1]["content"][-1]["text"]
    assert told.startswith(f"[The user undid operation {result['operation_id']}") and told.endswith("and now?")


def test_a_gateway_failure_ends_the_turn_with_an_error_and_the_conversation_goes_on(caps, tmp_path):
    with FakeGateway(scripted(Reply("fine now"))) as gateway:
        runner, store = make_runner(gateway, tmp_path)
        gateway.fail_with = [(400, "provider_rejected")]
        events = events_of(runner, "hello")
        assert [e["code"] for e in events if e["event"] == "error"] == ["provider_rejected"]
        assert "stopped" not in kinds(events) and store.load(runner.conversation_id)["state"] == "idle"
        assert not store.control(runner.conversation_id)["paused"]
        assert events_of(runner, "again")[-1]["text"] == "fine now"
