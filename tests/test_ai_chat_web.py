"""Plexora AI's chat on the website's terms, under CPython.

The website's worker has one thread: work is cooperative
(`PLEXORA_WORK=cooperative`, plexora/_work.py), spatialagents' threads and
sleeps go through `_work` (plexora/browser/agents.py), and every event a
conversation records is sent to the page (`boot.chat_events`), which holds
the panel's poll and answers it while the turn runs
(services/pythonClient.js). This runs the same app in that mode against the
stand-in gateway, with a list in the page's place:

- the panel's EventSource is refused with 406, its cue to poll;
- a poll is answered at once -- `held: false` when there is nothing new --
  and never held inside the request;
- the user's message is answered before its turn runs; the turn runs when
  the worker steps (`cooperative.step`), and each event reaches the page as
  it is recorded, the streamed text before the whole of it;
- a turn waiting for the user's approval waits through `_work.pause`, the
  worker's idle, and the approval -- a request answered in that gap, as the
  website's worker answers one -- lets it go on to make the call.
"""

from __future__ import annotations

import time

import pytest

from plexora import _work
from plexora.browser import agents, boot, cooperative
from tests.ai_harness_fixtures import FakeGateway
from tests.test_ai_chat import STATE, _destructive_brain, caps, http, service  # noqa: F401 -- fixtures


@pytest.fixture
def website(monkeypatch):
    """This process as the website's worker: cooperative work, the stand-ins
    installed (and taken out after), conversations' events sent to `posted`."""
    from plexora.ai.harness import conversations

    monkeypatch.setenv("PLEXORA_WORK", "cooperative")
    for name, attributes in agents.PATCHED.items():
        module = __import__(name, fromlist=["_"])
        for attribute in attributes:
            if hasattr(module, attribute):
                monkeypatch.setattr(module, attribute, getattr(module, attribute))
    replaced = agents.install()
    posted = []
    boot.chat_events(posted.append)
    yield {"posted": posted, "replaced": replaced}
    conversations.set_observer(None)
    _work.set_idle(None)


def _events(posted, cid):
    return [m["event"] for m in posted if m["kind"] == "chat" and m["conversation"] == cid]


@pytest.mark.pro
def test_a_turn_runs_between_requests_and_the_page_is_told_as_it_goes(caps, service, http, website):
    assert website["replaced"]["spatialagents.conversations"] == ["threading"]
    with FakeGateway(_destructive_brain()) as gateway:
        made = service(gateway)
        cid = http.post("/ai/v1/conversations", json={"title": "web", "viewer": True}).get_json()["conversation_id"]

        stream = http.get(f"/ai/v1/conversations/{cid}/events?after=0", headers={"Accept": "text/event-stream"})
        assert stream.status_code == 406 and stream.get_json()["error"]["code"] == "not_acceptable"

        started = time.monotonic()
        first = http.get(f"/ai/v1/conversations/{cid}/events?after=0&wait=20").get_json()
        assert [e["event"] for e in first["events"]] == ["disclosure"] and "held" not in first
        quiet = http.get(f"/ai/v1/conversations/{cid}/events?after={first['after']}&wait=20").get_json()
        assert quiet["ok"] and quiet["events"] == [] and quiet["held"] is False
        assert time.monotonic() - started < 5                  # neither poll was held here

        sent = http.post(f"/ai/v1/conversations/{cid}/messages", json={"text": "delete it"}).get_json()
        assert sent["ok"], sent
        assert _work.pending(background=False) == 1             # the turn waits for the worker
        assert STATE["erased"] == []

        # The worker's idle while the turn waits for the user: the page's
        # requests are answered -- a poll, then the approval.
        answered = {}

        def idle(seconds):
            pending = made.store.control(cid).get("approvals") or {}
            waiting = [aid for aid, a in pending.items() if a.get("status") == "pending"]
            if not waiting or "approve" in answered:
                return
            polled = http.get(f"/ai/v1/conversations/{cid}/events?after={first['after']}&wait=20").get_json()
            answered["poll"] = [e["event"] for e in polled["events"]]
            answered["approve"] = http.post(f"/ai/v1/conversations/{cid}/approve", json={
                "approval_id": waiting[0], "decision": "approve"}).get_json()

        _work.set_idle(idle)
        outcome = cooperative.step(budget_ms=60_000)
        assert outcome["ran"] == 1 and outcome["pending"] == 0
    assert STATE["erased"] == ["region 3"]
    assert "approval_requested" in answered["poll"]
    assert answered["approve"]["ok"] and answered["approve"]["approval"]["status"] == "approved"

    told = _events(website["posted"], cid)
    names = [e["event"] for e in told]
    assert names[0] == "disclosure" and "user_message" in names and names[-1] == "turn_finished"
    for earlier, later in (("turn_started", "approval_requested"), ("approval_requested", "approval_decided"),
                           ("approval_decided", "tool_result"), ("text_delta", "text")):
        assert names.index(earlier) < names.index(later), names
    seqs = [e["seq"] for e in told]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # What the page was told is what the conversation recorded (less the deltas, kept live only).
    kept = [e["seq"] for e in made.store.decisions(cid)]
    assert kept == [e["seq"] for e in told if e["event"] != "text_delta"]


def test_the_stand_ins_go_once_spatialagents_takes_a_spawn_and_a_sleep():
    """plexora/browser/agents.py replaces spatialagents' `threading` and
    `time` because it has no hook for them. When the installed spatialagents
    has one (`spatialagents.runtime`), `install()` uses it, and PATCHED and
    the stand-ins are dead code: remove them."""
    assert not agents.hooked(), "spatialagents.runtime exists: drop agents.PATCHED and the stand-ins"
