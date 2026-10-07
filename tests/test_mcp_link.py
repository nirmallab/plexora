"""Finding a viewer must never slow headless work.

The MCP process reads its link to a Plexora server on every tool call, every
receipt and every session event. With no server running, that read must cost
nothing: discovery runs on a background thread, at most once per back-off
window (longer each time nothing is found), and `notify` only ever sends to
the link it already has. The search itself is short: each URL once, probed in
parallel, a dead port on this machine given a fraction of a second, and a
record whose process is gone dropped from servers.json.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("mcp")

from plexora.agent import attach, jobs  # noqa: E402
from plexora.mcp import server as mcp_server  # noqa: E402
from plexora.mcp.server import Runtime  # noqa: E402
from plexora.server.models import server_records  # noqa: E402


class Finder:
    """A stand-in `find_server` that counts its calls (optionally slow)."""

    def __init__(self, answer=None, delay=0.0, gate=None):
        self.calls = 0
        self.answer = answer
        self.delay = delay
        self.gate = gate

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
        if self.delay:
            time.sleep(self.delay)
        return self.answer


class Link:
    """A stand-in ServerLink that records what it is told."""

    def __init__(self, url="http://127.0.0.1:9/"):
        self.base_url = url
        self.unreachable_at = None
        self.told = []

    def notify(self, project, plugin, kind, payload=None):
        self.told.append({"project": project, "plugin": plugin, "kind": kind,
                          "payload": dict(payload or {})})
        return True

    def describe(self):
        return {"url": self.base_url}


@pytest.fixture
def finder(monkeypatch):
    found = Finder()
    monkeypatch.setattr(attach, "find_server", found)
    # The registry's mtimes do not move unless a test moves them.
    monkeypatch.setattr(mcp_server, "_registry_stamp", lambda: ("still",))
    return found


def _runtime(**kwargs):
    return Runtime(names=["gating"], **kwargs)


def test_reads_within_the_back_off_search_once_and_never_inline(finder):
    finder.delay = 0.5
    runtime = _runtime(rediscover=True)
    runtime._next_search = 0.0      # due now
    started = time.perf_counter()
    for _ in range(500):
        assert runtime.link is None
        assert runtime.notify("p", "qc", "qc.session", {"event": "phase"}) is False
    # 1000 reads while a (slow) search runs: none of them waited for it.
    assert time.perf_counter() - started < 0.4
    runtime.wait_for_search(5)
    for _ in range(500):
        runtime.link
        runtime.notify("p", "roi", "roi.create")
    runtime.wait_for_search(5)
    assert finder.calls == 1 and runtime.searches == 1
    # Nothing found: the next look is a back-off away, and the one after
    # that further still.
    assert runtime._next_search - time.monotonic() >= Runtime.BACKOFF_S - 1
    runtime._next_search = 0.0
    runtime.link
    runtime.wait_for_search(5)
    assert finder.calls == 2
    assert runtime._next_search - time.monotonic() >= 2 * Runtime.BACKOFF_S - 1
    assert Runtime.BACKOFF_S >= 60


def test_notify_never_waits_on_a_search(finder):
    gate = threading.Event()
    finder.gate = gate
    runtime = _runtime(rediscover=True)
    runtime._next_search = 0.0
    try:
        started = time.perf_counter()
        for _ in range(200):
            runtime.notify("p", "gating", "gating.session", {"event": "answered"})
        assert time.perf_counter() - started < 0.2   # the search is stuck; notify is not
        assert finder.calls == 1
    finally:
        gate.set()
        runtime.wait_for_search(5)


def test_a_search_that_is_not_due_does_not_run(finder):
    runtime = _runtime(rediscover=True)        # nothing found at start: backed off
    for _ in range(1000):
        runtime.notify("p", "qc", "qc.session", {"event": "issued"})
        runtime.link
    assert finder.calls == 0


def test_a_found_server_is_used_and_a_newer_announcement_is_looked_for(finder, monkeypatch):
    link = Link()
    finder.answer = link
    stamp = ["one"]
    monkeypatch.setattr(mcp_server, "_registry_stamp", lambda: (stamp[0],))
    runtime = _runtime(rediscover=True)
    runtime.WATCH_S = 0.0
    assert runtime.link is None and finder.calls == 0
    stamp[0] = "two"                        # a server announced itself
    runtime.link
    runtime.wait_for_search(5)
    assert finder.calls == 1 and runtime.link is link
    assert runtime.notify("p", "qc", "qc.session", {"event": "answered"}) is True
    assert link.told[-1]["kind"] == "qc.session"


def test_a_link_found_gone_is_not_sent_to_and_is_replaced(finder):
    dead = Link("http://127.0.0.1:1/")
    runtime = _runtime(link=dead, rediscover=True)
    dead.unreachable_at = time.monotonic()  # a notify found nobody there
    assert runtime.notify("p", "qc", "qc.session") is False
    assert dead.told == []
    runtime.wait_for_search(5)
    assert finder.calls == 1 and runtime.link is None


def test_without_rediscovery_the_link_is_left_alone(finder):
    link = Link()
    runtime = _runtime(link=link)
    runtime._next_search = 0.0
    for _ in range(100):
        assert runtime.link is link
    assert finder.calls == 0


# -- the search ------------------------------------------------------------------


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_candidates_are_each_url_once_newest_first(tmp_path, monkeypatch):
    monkeypatch.delenv("PLEXORA_SERVER_URL", raising=False)
    me = os.getpid()
    (tmp_path / "servers.json").write_text(json.dumps({
        "1": {"pid": me, "port": 8931, "token": "old", "started": 1},
        "2": {"pid": me, "port": 8931, "token": "new", "started": 3},
        "3": {"pid": me, "port": 8000, "token": "x", "started": 2},
        "4": {"pid": me, "port": 8931, "token": "mid", "started": 2}}))
    links = list(attach._candidates())
    assert [link.base_url for link in links] == ["http://127.0.0.1:8931/",
                                                 "http://127.0.0.1:8000/"]
    assert links[0].token == "new"
    monkeypatch.setenv("PLEXORA_SERVER_URL", "http://127.0.0.1:8000")
    assert [link.base_url for link in attach._candidates()] == [
        "http://127.0.0.1:8000/", "http://127.0.0.1:8931/"]


def test_a_search_over_dead_ports_is_short(tmp_path, monkeypatch):
    monkeypatch.delenv("PLEXORA_SERVER_URL", raising=False)
    me = os.getpid()
    records = {str(i): {"pid": me, "port": _free_port(), "token": None, "started": i}
               for i in range(1, 9)}
    (tmp_path / "servers.json").write_text(json.dumps(records))
    started = time.perf_counter()
    assert attach.find_server() is None
    # Eight dead ports probed together, each given LOCAL_CONNECT_TIMEOUT_S --
    # not two seconds apiece one after another.
    assert time.perf_counter() - started < 1.5


# -- servers.json ----------------------------------------------------------------


def _dead_pid():
    """A pid that was a process and is not any more."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_a_dead_pid_is_not_alive_on_this_platform():
    pid = _dead_pid()
    assert server_records._alive(pid) is False
    assert server_records._alive(os.getpid()) is True
    assert server_records._alive(0) is False and server_records._alive("x") is False


def test_dead_records_are_dropped_from_servers_json(tmp_path):
    dead = _dead_pid()
    (tmp_path / "servers.json").write_text(json.dumps({
        str(dead): {"pid": dead, "port": 8931, "token": None, "started": 1},
        str(os.getpid()): {"pid": os.getpid(), "port": 8932, "token": "t", "started": 2}}))
    found = server_records.records(root=tmp_path)
    assert [r["url"] for r in found] == ["http://127.0.0.1:8932/"]
    kept = json.loads((tmp_path / "servers.json").read_text())
    assert list(kept) == [str(os.getpid())]
    # Reading again finds the file clean and leaves it alone.
    before = (tmp_path / "servers.json").stat().st_mtime_ns
    server_records.records(root=tmp_path)
    assert (tmp_path / "servers.json").stat().st_mtime_ns == before


# -- a whole session, headless -----------------------------------------------------


def _ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:2000]
    return result["result"]


def _gate(runtime, limit=60):
    """Start a gating session on the synthetic project and answer every
    packet "about right" through the runtime, as an MCP client would."""
    from tests.autogate_fixtures import make_gating_project  # noqa: F401 (made by caller)
    from tests.test_gating_session import Oracle

    started = _ok(runtime.invoke("gating_session_start", {"scope": "project",
                                                           "project": "gsynth"}))
    jobs.drain(120)
    sid = started["session_id"]
    agent = Oracle(runtime.info, style="lazy")
    result = _ok(runtime.invoke("gating_next", {"session_id": sid, "wait_s": 20}))
    answers = 0
    for _ in range(limit):
        if result["state"] != "decision":
            return answers
        packet = result["packet"]
        result = _ok(runtime.invoke("gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"],
            "answer": agent.answer(packet)}))["next"]
        answers += 1
    raise AssertionError("the session did not finish")


@pytest.mark.paid
def test_a_gating_session_with_no_server_looks_for_one_once(tmp_path, finder):
    from tests.autogate_fixtures import make_gating_project

    info = make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    runtime = _runtime(rediscover=True)
    runtime.info = info
    runtime._next_search = 0.0             # a search is due as the session starts
    answers = _gate(runtime)
    runtime.wait_for_search(5)
    assert answers >= 1
    assert finder.calls == 1               # once per back-off window, not per event


@pytest.mark.paid
def test_a_gating_answer_is_one_event_carrying_the_markers_it_closed(tmp_path, finder):
    from tests.autogate_fixtures import make_gating_project

    info = make_gating_project(tmp_path, grid=24, size=1024, markers=("CD3", "CD4"))
    heard = Link()                 # what an attached server would be sent
    runtime = _runtime()
    runtime.notify = heard.notify  # no viewer to mirror into; every event recorded
    runtime.info = info
    _gate(runtime)
    session = [e["payload"] for e in heard.told if e["kind"] == "gating.session"]
    assert not [p for p in session if p["event"] == "unit_closed"]
    answered = [p for p in session if p["event"] == "answered"]
    assert answered and all(isinstance(p["closed"], list) for p in answered)
    closed = [unit for p in answered for unit in p["closed"]]
    assert {unit["marker"] for unit in closed} == {"CD3", "CD4"}
    assert all({"marker", "project", "state", "reason"} <= set(unit) for unit in closed)
    assert finder.calls == 0


def test_a_link_whose_server_is_gone_says_so():
    link = attach.ServerLink(f"http://127.0.0.1:{_free_port()}/")
    started = time.perf_counter()
    assert link.accepts() is False
    assert time.perf_counter() - started < 1.0
    assert link.notify("p", "qc", "qc.session") is False
    assert link.unreachable_at is not None
