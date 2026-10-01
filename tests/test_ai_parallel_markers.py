"""Several packets of one gating session out at once (parallel markers).

The engine hands out the decisions that do not stand on each other side by
side -- a marker waits for the partners it is judged beside that come earlier
in gating order, and for the partner it is gated `within`; the set-up packets
and the T1 audit strips go out alone -- and refuses an answer whose partner
gates changed while its packet was out. What is pinned: the same gates as the
serial run, more than one packet really out at once, the readiness and
exclusivity rules held at every issue, a stale answer refused and its decision
served again, records from before the map still loading, and the harness's
lanes writing the cached prefix once.
"""

import json
import threading

import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.agent.sessions import engine as base
from plexora.ai.harness.decision import GatingOptions, GatingRun
from plexora.ai.harness.trace import TraceStore
from plexora.plugins.gating.server.autogate import engine as engines
from plexora.plugins.gating.server.autogate import schemas
from tests.ai_harness_fixtures import FakeGateway
from tests.autogate_fixtures import make_gating_project
from tests.test_ai_harness import client, oracle_brain
from tests.test_gating_session import Oracle

pytestmark = pytest.mark.paid

#: CD3, CD4 and CD8 are partners of each other (the vocabulary's); the three
#: KI markers are unknown to it -- no partners -- and their populations
#: overlap like CD4's, so each needs looks of its own. They are what can be
#: out side by side. CD4 waits for CD3 (gating order is CD3, CD4, CD8), and
#: the audit strip CD3 and CD8 are accepted on goes out alone.
PANEL = ("CD3", "CD4", "CD8", "KIA", "KIB", "KIC")
#: Hard on purpose, as CD4 is in the shared scene: positive in one phenotype
#: each, the populations about 2.2 sd apart.
INDEPENDENT = {"KIA": (6.4, 5.5, 5.5, 5.5), "KIB": (5.5, 6.4, 5.5, 5.5),
               "KIC": (5.5, 5.5, 6.4, 5.5)}


@pytest.fixture
def scene(monkeypatch):
    from tests import autogate_fixtures

    for name, levels in INDEPENDENT.items():
        monkeypatch.setitem(autogate_fixtures.MARKER_LEVELS, name, levels)
        monkeypatch.setitem(autogate_fixtures.MARKER_SD, name, 0.4)

    def make(data_root, name):
        return make_gating_project(data_root, name=name, grid=32, size=1280, markers=PANEL)
    return make


@pytest.fixture(autouse=True)
def _gating():
    registry.discover(["gating"])


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _call(session, name="gating.answer"):
    from plexora.agent.audit import AuditLog
    from plexora.agent.policy import Policy
    from plexora.agent.receipts import operation_id
    from plexora.agent.registry import Call

    return Call(capability=registry.get(name), session=session, policy=Policy(),
                operation_id=operation_id(), audit=AuditLog(), arguments={})


def start(session, project, **options):
    started = ok(invoke(session, "gating_session_start", {"scope": "project",
                                                          "project": project, **options}))
    jobs.drain(120)
    return started["session_id"]


def finals(session, session_id):
    status = ok(invoke(session, "gating_session_status",
                       {"session_id": session_id}))
    return {u["marker"]: (u["state"], u.get("final"), u.get("confidence"))
            for u in status["units"]}, status


def drive_serial(session, session_id, agent, limit=80):
    result = ok(invoke(session, "gating_next", {"session_id": session_id, "wait_s": 20}))
    for _ in range(limit):
        if result["state"] != "decision":
            assert result["state"] in ("decided", "done"), result
            return
        packet = result["packet"]
        result = ok(invoke(session, "gating_answer", {
            "session_id": session_id, "packet_id": packet["packet_id"],
            "answer": agent.answer(packet)}))["next"]
    raise AssertionError("the serial session did not finish")


def drive_parallel(session_id, agent, lanes=3, limit=80):
    """`lanes` threads, each its own reader, pulling packets with
    `parallel=lanes` until the session is decided."""
    errors = []

    def lane(index):
        session = AgentSession()
        extra = {"parallel": lanes, **({"reader": f"r{index}"} if index else {})}
        try:
            result = ok(invoke(session, "gating_next",
                               {"session_id": session_id, "wait_s": 20, **extra}))
            for _ in range(limit * 4):
                state = result["state"]
                if state in ("decided", "done"):
                    return
                if state in ("busy", "bulk_running"):
                    result = ok(invoke(session, "gating_next",
                                       {"session_id": session_id, "wait_s": 20, **extra}))
                    continue
                assert state == "decision", result
                packet = result["packet"]
                result = ok(invoke(session, "gating_answer", {
                    "session_id": session_id, "packet_id": packet["packet_id"],
                    "answer": agent.answer(packet), **extra}))["next"]
            raise AssertionError(f"lane {index} did not finish")
        except BaseException as exc:          # surfaced in the test thread
            errors.append(exc)

    threads = [threading.Thread(target=lane, args=(i,)) for i in range(lanes)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(300)
    if errors:
        raise errors[0]


@pytest.fixture
def held(monkeypatch):
    """Every packet put out, with what was out beside it and the state of
    every unit it depends on at that moment (the engine holds the session's
    lock while it issues, so each row is a consistent snapshot)."""
    rows = []
    original = base.BaseEngine._hold

    def recording(self, packet_id, **kw):
        others = [dict(e, packet_id=pid) for pid, e in self.record["outstanding"].items()]
        deps = {}
        if isinstance(self, engines.Engine):
            for key in kw["units"]:
                unit = self.record["units"][key]
                deps[key] = {d["marker"]: (d["state"], bool(d.get("limit_request")))
                             for d in self.depends_on(unit)}
        rows.append({"packet_id": packet_id, "kind": kw["kind"], "units": list(kw["units"]),
                     "others": others, "deps": deps, "strict": kw.get("strict")})
        return original(self, packet_id, **kw)

    monkeypatch.setattr(base.BaseEngine, "_hold", recording)
    return rows


def _check_rules(rows):
    exclusive = engines.Engine.EXCLUSIVE_KINDS
    for row in rows:
        if row["kind"] in exclusive:
            assert not row["others"], ("an exclusive packet went out beside others", row)
        else:
            assert not [o for o in row["others"] if o["kind"] in exclusive], \
                ("a packet went out beside an exclusive one", row)
        busy = {k for o in row["others"] for k in o["units"]}
        assert not busy & set(row["units"]), ("a unit had two packets out", row)
        if row["kind"] != "t1_strip":
            for key, deps in row["deps"].items():
                for marker, (state, limited) in deps.items():
                    assert state in schemas.TERMINAL_STATES or limited, \
                        (f"{key} went out before {marker} was settled ({state})", row)


# -- end to end through the capabilities -------------------------------------------------


def test_parallel_markers_reach_the_serial_gates_with_several_packets_out(tmp_path, held, scene):
    infos = {name: scene(tmp_path, name)
             for name in ("gser", "gpar")}
    session = AgentSession()

    serial_id = start(session, "gser")
    drive_serial(session, serial_id, Oracle(infos["gser"]))
    serial, serial_status = finals(session, serial_id)
    serial_rows = list(held)
    held.clear()

    parallel_id = start(session, "gpar")
    drive_parallel(parallel_id, Oracle(infos["gpar"]), lanes=3)
    parallel, status = finals(session, parallel_id)

    assert set(serial) == set(PANEL)
    assert parallel == serial
    # Every marker settled (one of the hard ones honestly left for a person).
    assert all(state in schemas.TERMINAL_STATES for state, _f, _c in serial.values()), serial
    assert sum(state.startswith("accepted") for state, _f, _c in serial.values()) >= 5
    # Several packets were really out at once, and never more than asked for.
    assert 1 < status["outstanding_peak"] <= 3
    assert serial_status["outstanding_peak"] == 1
    assert not status["outstanding_packets"]
    _check_rules(held)
    assert any(len(row["others"]) >= 1 for row in held)
    # Serial packets are held as before: never checked against the ledger.
    assert serial_rows and not any(row["strict"] for row in serial_rows)
    # The readers each saw their own packets, stamped with their epoch.
    store = engines.store()
    stamped = {}
    for row in held:
        packet, _images = store.read_packet(parallel_id, row["packet_id"])
        stamped[row["packet_id"]] = packet.get("briefed")
    readers = {b["reader"] for b in stamped.values() if b}
    assert readers <= {"r1", "r2"} and readers, stamped
    record = store.load(parallel_id)
    for reader in readers:
        seen = record["readers"][reader]["briefed"]["seen"]
        assert all(stamped[pid] and stamped[pid]["reader"] == reader for pid in seen.values())
    finished = ok(invoke(session, "gating_session_finish", {"session_id": parallel_id}))
    assert finished["progress"]["units_done"] == len(PANEL)


# -- the engine's readiness rules -----------------------------------------------------------


def _bulked(session, scene, tmp_path, name="gpar"):
    """A session on the panel, its bulk pass done: CD3 and CD8 accepted at T1
    (awaiting their strip), CD4 and the KI markers waiting for a look."""
    scene(tmp_path, name)
    return start(session, name)


def _markers_of(picks):
    return [u["marker"] for _kind, units in picks for u in units]


def test_a_marker_waits_for_its_earlier_partners_and_the_one_it_is_gated_within(
        tmp_path, scene):
    session = AgentSession()
    sid = _bulked(session, scene, tmp_path)
    with engines.engine_for(_call(session), sid, save=False) as engine:
        engine.record["panel_pending"] = False
        unit = {u["marker"]: u for u in engine.units_of("gpar")}
        assert unit["CD3"]["state"] == unit["CD8"]["state"] == "accepted_t1"
        # Gating order is CD3, CD4, CD8: each waits for the partners before it.
        assert [d["marker"] for d in engine.depends_on(unit["CD4"])] == ["CD3"]
        assert {d["marker"] for d in engine.depends_on(unit["CD8"])} == {"CD3", "CD4"}
        assert engine.depends_on(unit["KIA"]) == []

        # CD3 still waits on its strip: CD4 may not go out yet; the
        # independent markers may, all three at once.
        assert _markers_of(engine.ready_units()[0]) == ["KIA", "KIB", "KIC"]
        unit["CD3"]["state"] = "accepted"
        assert _markers_of(engine.ready_units()[0]) == ["CD4", "KIA", "KIB", "KIC"]

        # A marker gated within a partner waits for that partner, wherever
        # it sits in the gating order.
        unit["KIA"]["condition"] = {"within": "CD4", "partner_gate": 1.0}
        assert [d["marker"] for d in engine.depends_on(unit["KIA"])] == ["CD4"]
        assert "KIA" not in _markers_of(engine.ready_units()[0])
        unit["CD4"]["state"] = "accepted"
        assert "KIA" in _markers_of(engine.ready_units()[0])


def test_setup_packets_and_strips_go_out_alone(tmp_path, scene):
    session = AgentSession()
    sid = _bulked(session, scene, tmp_path)

    def hold(engine, pid, kind, markers):
        engine.record["outstanding"][pid] = {
            "kind": kind, "units": [engines.unit_key("gpar", m) for m in markers],
            "reader": "x", "fingerprint": "", "strict": True, "seq": 0}

    with engines.engine_for(_call(session), sid, save=False) as engine:
        # The panel's set-up question comes first, and goes out alone.
        assert engine.ready_units() == ([("panel_context", [])], None)
        hold(engine, "pk_9001", "t2_confirm", ["KIA"])
        assert engine.ready_units() == ([], "busy")
        engine.record["outstanding"].clear()
        engine.record["panel_pending"] = False

        # Looks out: the strip CD3 and CD8 are waiting for does not join them.
        hold(engine, "pk_9001", "t2_confirm", ["KIA"])
        assert _markers_of(engine.ready_units()[0]) == ["KIB", "KIC"]
        hold(engine, "pk_9002", "t2_confirm", ["KIB"])
        hold(engine, "pk_9003", "t2_confirm", ["KIC"])
        assert engine.ready_units() == ([], "busy")
        # Once they are answered (and settled) the strip goes, and nothing
        # beside it.
        engine.record["outstanding"].clear()
        for marker in ("KIA", "KIB", "KIC"):
            engine.unit(engines.unit_key("gpar", marker))["state"] = "accepted"
        picks, _why = engine.ready_units()
        assert [(k, [u["marker"] for u in us]) for k, us in picks] == [
            ("t1_strip", ["CD3", "CD8"])]
        hold(engine, "pk_9004", "t1_strip", ["CD3", "CD8"])
        assert engine.ready_units() == ([], "busy")
        assert engine.issue(reader="r1", parallel=3) == (None, [], "busy")


def test_an_answer_whose_partner_gates_changed_is_refused_and_served_again(tmp_path, scene):
    info = scene(tmp_path, "gpar")
    session = AgentSession()
    sid = start(session, "gpar")
    agent = Oracle(info)
    args = {"session_id": sid, "parallel": 2}
    result = ok(invoke(session, "gating_next", {**args, "wait_s": 20}))
    for _ in range(40):
        assert result["state"] == "decision", result
        packet = result["packet"]
        if [u["marker"] for u in packet["units"]] == ["CD4"]:
            break
        ok(invoke(session, "gating_answer", {**args, "packet_id": packet["packet_id"],
                                             "answer": agent.answer(packet),
                                             "include_next": False}))
        result = ok(invoke(session, "gating_next", {**args, "wait_s": 20}))
    else:
        raise AssertionError("CD4 was never asked about")
    stale_id = packet["packet_id"]
    assert "CD3" in {p["partner"] for p in packet["evidence"]["partners"]}
    with engines.engine_for(_call(session), sid) as engine:
        entry = engine.record["outstanding"][stale_id]
        assert entry["strict"] and entry["fingerprint"]
        # The partner's gate moves while CD4's packet is out: what the packet
        # showed no longer holds.
        cd3 = engine.unit(engines.unit_key("gpar", "CD3"))
        cd3["final"] = float(cd3["final"]) * 1.2

    refused = ok(invoke(session, "gating_answer", {**args, "packet_id": stale_id,
                                                   "answer": agent.answer(packet)}))
    assert refused["applied"] is False
    assert refused["outcome"]["state"] == "reissue"
    assert refused["outcome"]["units"] == ["gpar::CD4"]
    fresh = refused["next"]["packet"]
    assert fresh["packet_id"] != stale_id and fresh["kind"] == packet["kind"]
    assert [u["marker"] for u in fresh["units"]] == ["CD4"]
    # Retrying the refused answer says the same; the fresh packet is answered.
    again = ok(invoke(session, "gating_answer", {**args, "packet_id": stale_id,
                                                 "answer": agent.answer(packet)}))
    assert again["outcome"]["state"] == "reissue" and again["outcome"]["already_applied"]
    applied = ok(invoke(session, "gating_answer", {**args, "packet_id": fresh["packet_id"],
                                                   "answer": agent.answer(fresh),
                                                   "include_next": False}))
    assert applied["applied"] is True
    record = engines.store().load(sid)
    assert record["reissued"] == 1
    assert stale_id not in record["outstanding"]
    events = [d for d in engines.store().decisions(sid) if d.get("event") == "stale"]
    assert [e["packet_id"] for e in events] == [stale_id]


def test_a_record_with_one_outstanding_packet_in_scalars_still_loads(tmp_path, scene):
    """A session stored by an earlier build: `outstanding_packet`,
    `outstanding_kind`, `outstanding_memo_key`, `invalid_answers` and
    `last_unit` as scalars on the record."""
    info = scene(tmp_path, "gpar")
    session = AgentSession()
    sid = start(session, "gpar")
    first = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}))["packet"]
    ok(invoke(session, "gating_answer", {"session_id": sid, "packet_id": first["packet_id"],
                                         "answer": Oracle(info).answer(first),
                                         "include_next": False}))
    packet = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}))["packet"]
    unit_key = engines.unit_key("gpar", packet["units"][0]["marker"])
    store = engines.store()
    record = store.load(sid)
    entry = record.pop("outstanding")[packet["packet_id"]]
    record.pop("readers", None)
    record.update(outstanding_packet=packet["packet_id"], outstanding_kind=entry["kind"],
                  outstanding_memo_key=entry["memo_key"], invalid_answers=1,
                  last_unit="gpar::CD3")
    store.save(record)

    # The same packet is served again, and the strike it already had counts.
    again = ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 0}))
    assert again["packet"]["packet_id"] == packet["packet_id"]
    migrated = store.load(sid)
    assert set(migrated["outstanding"]) == {packet["packet_id"]}
    held = migrated["outstanding"][packet["packet_id"]]
    assert held["units"] == [unit_key] and held["invalid_answers"] == 1
    assert held["reader"] == base.DEFAULT_READER and held["fingerprint"] is None
    assert migrated["readers"][base.DEFAULT_READER]["last_unit"] == "gpar::CD3"
    for gone in ("outstanding_memo_key", "invalid_answers", "last_unit"):
        assert gone not in migrated
    refused = invoke(session, "gating_answer", {"session_id": sid,
                                                "packet_id": packet["packet_id"],
                                                "answer": {"kind": packet["kind"],
                                                           "nonsense": True}})
    assert not refused["ok"] and "twice" in refused["error"]["message"]
    status = ok(invoke(session, "gating_session_status", {"session_id": sid}))
    states = {u["marker"]: u["state"] for u in status["units"]}
    assert states[packet["units"][0]["marker"]] == "manual_review_recommended"
    assert status["outstanding_packet"] is None and status["outstanding_packets"] == []


def test_the_outstanding_map_keeps_the_newest_packet_in_the_scalars():
    class Store:
        def load(self, session_id):
            return {"session_id": session_id, "units": {}, "outstanding_packet": None}

    class Bare(base.BaseEngine):
        def unit_key_of(self, ref):
            return ref["id"]

    engine = Bare(None, "s1", st=Store())
    assert engine.record["outstanding"] == {} and engine.record["outstanding_packet"] is None
    engine.record["packet_seq"] = 1
    engine._hold("pk_0001", kind="a", units=["u1"], memo_key="m", reader="main",
                 fingerprint="")
    engine.record["packet_seq"] = 2
    engine._hold("pk_0002", kind="b", units=["u2"], memo_key="m", reader="r1",
                 fingerprint="f")
    assert engine.outstanding() == ["pk_0001", "pk_0002"]
    assert engine.outstanding("r1") == ["pk_0002"]
    assert (engine.record["outstanding_packet"], engine.record["outstanding_kind"]) == (
        "pk_0002", "b")
    assert engine.record["outstanding_peak"] == 2
    engine.release("pk_0002")
    assert engine.record["outstanding_packet"] == "pk_0001"
    engine.release_all()
    assert engine.record["outstanding_packet"] is None and engine.record["outstanding_peak"] == 2
    # A workflow without readiness rules issues one at a time.
    engine._hold("pk_0003", kind="a", units=["u3"], memo_key="m", reader="main",
                 fingerprint="")
    assert engine.next_ready("r1") == ("busy", [])
    # No ledger, no change to the memo key; a different ledger, a different key.
    assert engine._with_ledger("k", "") == "k"
    assert engine._with_ledger("k", "f1") != engine._with_ledger("k", "f2")
    # Per-reader epochs: a pointer may name only what this reader holds.
    engine.briefed("r1")["seen"]["packet:pk_0002"] = "pk_0002"
    assert engine.may_point_to("r1", "pk_0002") and not engine.may_point_to("main", "pk_0002")
    assert engine.new_epoch("r1") == 1 and not engine.may_point_to("r1", "pk_0002")
    engine.briefed()
    assert "briefed" in engine.record and "briefed" not in engine.reader("main")


# -- the harness ------------------------------------------------------------------------------


def test_the_harness_gates_with_parallel_markers_and_writes_the_prefix_once(tmp_path, scene):
    infos = {name: scene(tmp_path, name) for name in ("gser", "gpar")}
    summaries = {}
    with FakeGateway(oracle_brain(infos["gser"])) as gateway:
        summaries["gser"] = GatingRun(GatingOptions(project="gser"), gateway=client(gateway),
                                      trace=TraceStore(tmp_path / "s.sqlite")).run()
    with FakeGateway(oracle_brain(infos["gpar"])) as gateway:
        summaries["gpar"] = GatingRun(GatingOptions(project="gpar", parallel_markers=3),
                                      gateway=client(gateway),
                                      trace=TraceStore(tmp_path / "p.sqlite")).run()
        calls = list(gateway.calls)
    serial, parallel = summaries["gser"], summaries["gpar"]
    assert serial["status"] == parallel["status"] == "done", (serial, parallel)
    session = AgentSession()
    assert finals(session, parallel["session_id"])[0] == finals(session, serial["session_id"])[0]
    assert parallel["parallel_markers"] == 3 and parallel["peak_outstanding"] > 1
    assert "parallel_markers" not in serial
    # One structured call per packet, as in a serial run.
    assert parallel["model_calls"] == parallel["packets"] == len(calls)
    assert parallel["invalid_answers"] == 0
    assert len({c["idempotency_key"] for c in calls}) == len(calls)
    # Each lane had its own conversations: no call carries another marker's
    # packet; and the prefix was written to the cache once, by the first call.
    for call in calls:
        packets = [json.loads(m["content"][-1]["text"])
                   for m in call["body"]["request"]["messages"] if m["role"] == "user"]
        # (The panel's set-up packet concerns no marker and starts no worker.)
        packets = [p for p in packets if p["units"]]
        first = {u["marker"] for u in packets[0]["units"]} if packets else set()
        for packet in packets[1:]:
            assert {u["marker"] for u in packet["units"]} <= first
    assert len([c for c in calls if c["usage"]["cache_read"] == 0]) == 1
    assert parallel["cache"]["verdicts"]["miss"] == 0
    readers = {(json.loads(c["body"]["request"]["messages"][-1]["content"][-1]["text"])
                .get("briefed") or {}).get("reader") for c in calls}
    assert readers - {None}, "only one lane ever answered"
