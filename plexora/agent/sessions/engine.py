"""The harness every decision-session engine shares.

A workflow (automatic gating, image quality control) keeps one record per
session on disk (`store.SessionStore`) holding a set of units -- a marker of an
image, a channel, an artifact candidate -- each in a state its workflow names.
The workflow decides which unit needs a decision next (`next_unit`), builds its
packet (`builders()`), and applies a typed answer (`transitions()`); what is
the same for every workflow lives here:

- issuing a packet: the outstanding-packet guard, ids, the answer schema, the
  images' metadata, the charge to the session and to each unit, the memo key,
  and replaying an identical packet's earlier answer (`memo`);
- several packets out at once (`issue(parallel=...)`): each held in
  `record["outstanding"]` with the reader it went to and the ledger
  fingerprint it was built on, the workflow's `next_ready` choosing what may
  go out beside the others, and `EXCLUSIVE_KINDS` going out alone;
- applying an answer: idempotence, the outstanding-packet check, the ledger
  check (a packet whose ledger changed is refused and served afresh),
  validation, two unreadable answers closing the unit for manual review, the
  memo write;
- drawing an outstanding packet again (`rerender`);
- the allowance: what a unit may spend, and the limit policy (ask the user,
  extend, or stop) when it reaches it while the evidence still says go on;
- the child receipt `<operation_id>.<nnn>` of each write.

`EngineContext` is the `with` block every mutation goes through: the session
locked, loaded fresh and saved when the block succeeds.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

from plexora.agent.errors import AgentError
from plexora.agent.sessions import budget as budgets

#: The reader a packet goes to when the caller names none -- every packet,
#: before sessions had readers. A reader is one conversation answering
#: packets; several answer one session in parallel (`issue(parallel=...)`).
DEFAULT_READER = "main"


class EngineContext:
    """`with <workflow>.engine_for(call, session_id) as engine:` -- the session
    locked, loaded fresh, and saved when the block succeeds (or raised an
    error marked `save`). Subclasses say which engine (`make`)."""

    def __init__(self, call, session_id, *, st=None, save=True):
        self.call, self.session_id = call, session_id
        self.st = st or self.default_store()
        self.save_on_exit = save
        self._lock = None

    def default_store(self):
        raise NotImplementedError

    def make(self):
        raise NotImplementedError

    def __enter__(self):
        self._lock = self.st.lock(self.session_id)
        self._lock.__enter__()
        try:
            self.engine = self.make()
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise
        return self.engine

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.save_on_exit and (exc_type is None or getattr(exc, "save", False)):
                self.engine.save()
        finally:
            self._lock.__exit__(exc_type, exc, tb)
        return False


def tool_name(capability):
    """The tool name an agent calls for a capability (hints never restate it)."""
    from plexora.agent import registry

    return registry.tool_name_of(capability)


class BaseEngine:
    """One session, loaded; mutate through methods, then `save()`.

    Class attributes a workflow sets: `TERMINAL` (terminal unit states),
    `ASKS` (waiting state -> packet kind), `BUDGETED_KINDS` (the looks a unit's
    allowance pays for), `SETUP_KINDS` (packets whose builder failing fails
    the call), `INVALID_ANSWERS` (unreadable answers before manual review),
    `MAX_IMAGES`, `ANSWER_CAPABILITY` / `NEXT_CAPABILITY`, `UNIT_NOUN`,
    `UNIT_DEFAULT` (a unit's allowance when the session sets none),
    `EXCLUSIVE_KINDS` (packets nothing else is out beside), `MODULE` (the AI
    module its packets' tasks belong to, `plexora/ai/tasks.yaml`).

    The packets out for an answer are `record["outstanding"]`:
    `{packet_id: {kind, memo_key, units, reader, fingerprint, strict,
    invalid_answers, seq}}`. `outstanding_packet` / `outstanding_kind` beside
    it name the newest one, for what shows a single packet (the viewer's
    phase, the session resource, status)."""

    TERMINAL: tuple = ()
    ASKS: dict = {}
    BUDGETED_KINDS: tuple = ()
    SETUP_KINDS: tuple = ()
    EXCLUSIVE_KINDS: tuple = ()
    INVALID_ANSWERS = 2
    MAX_IMAGES = 2
    ANSWER_CAPABILITY = ""
    NEXT_CAPABILITY = ""
    UNIT_NOUN = "unit"
    UNIT_DEFAULT = budgets.UNIT_DEFAULT
    MODULE = ""

    def __init__(self, call, session_id, *, st):
        self.call = call
        self.store = st
        self.id = session_id
        self.record = self.store.load(session_id)
        # A session stored by an older build lacks newer options: completed
        # with their defaults, so nothing downstream needs a fallback.
        options = self.record.setdefault("options", {})
        for key, value in self.option_defaults().items():
            options.setdefault(key, value)
        self._migrate()

    def _migrate(self):
        """A record stored when one packet at most was out kept it in three
        scalars, with the unreadable-answer count and the last unit answered
        beside them: moved into the `outstanding` map and the default reader.
        A migrated packet has no ledger fingerprint, so it is never refused
        as stale."""
        record = self.record
        if not isinstance(record.get("outstanding"), dict):
            held = record["outstanding"] = {}
            packet_id = record.get("outstanding_packet")
            if packet_id:
                try:
                    packet, _images = self.store.read_packet(self.id, packet_id)
                    keys = [self.unit_key_of(u) for u in packet.get("units") or []]
                except Exception:  # an unreadable packet still holds its place
                    keys = []
                held[packet_id] = {"kind": record.get("outstanding_kind"),
                                   "memo_key": record.get("outstanding_memo_key"),
                                   "units": keys, "reader": DEFAULT_READER,
                                   "fingerprint": None,
                                   "invalid_answers": int(record.get("invalid_answers") or 0),
                                   "seq": int(record.get("packet_seq") or 0)}
        for key in ("outstanding_memo_key", "invalid_answers"):
            record.pop(key, None)
        if "last_unit" in record:
            last = record.pop("last_unit")
            if last:
                self.reader(DEFAULT_READER)["last_unit"] = last
        self._sync_outstanding()

    # -- what a workflow supplies ------------------------------------------------

    def option_defaults(self) -> dict:
        return {}

    def unit_key_of(self, ref) -> str:
        """The record key of the unit a packet's `units` entry names."""
        raise NotImplementedError

    def unit_ref(self, unit) -> dict:
        """What a packet's `units` list says about a unit (JSON, small)."""
        raise NotImplementedError

    def unit_label(self, unit) -> str:
        """A unit in a word or two (the memo's `units`, limit questions)."""
        raise NotImplementedError

    def ref_label(self, ref) -> str:
        """A packet's `units` entry in a word or two (the memo's `units`)."""
        return self.unit_key_of(ref)

    def builders(self) -> dict:
        raise NotImplementedError

    def transitions(self) -> dict:
        raise NotImplementedError

    def answer_type(self):
        raise NotImplementedError

    def schema_for(self, kind) -> dict:
        raise NotImplementedError

    def memo_key(self, packet, images) -> str:
        raise NotImplementedError

    def memo_get(self, project, key):
        raise NotImplementedError

    def memo_put(self, project, key, answer, **about):
        raise NotImplementedError

    def narrate(self, packet) -> str:
        return ""

    def lean(self, packet):
        return packet

    def trim(self, packet):
        return budgets.trim(packet)

    def next_unit(self):
        """(kind, [units]) of the next decision; kind may be `wait` (the bulk
        pass has not reached it), `wait_user` (units held on a limit
        question) or None (nothing left)."""
        raise NotImplementedError

    def next_ready(self, reader):
        """`next_unit` beside packets already out: the next decision that may
        be issued now, or `busy` when what is left waits on an answer. A
        workflow with no readiness rules of its own issues one at a time."""
        if self.record["outstanding"]:
            return "busy", []
        return self.next_unit()

    # -- tasks: which AI task a decision is, and which a reader takes --

    def task_of(self, kind, units) -> str | None:
        """The AI task (`module.task`) a decision of `kind` on `units` is."""
        from plexora.ai import tasks

        check = next((u.get("check") for u in units or () if u.get("check")), None)
        return tasks.task_for(self.MODULE, kind, check=check)

    def accepts(self, kind, units) -> bool:
        """Whether the reader being served takes this decision: always, unless
        it was scoped to some tasks (`issue(tasks=...)`). A scoped reader
        passing one by is remembered (`needs`) so it can say who should."""
        scope = getattr(self, "_scope", None)
        if scope is None or kind in (None, "wait", "wait_user", "busy"):
            return True
        task = self.task_of(kind, units)
        if task in scope:
            return True
        if getattr(self, "_needs", None) is None:
            self._needs = task
        return False

    def ledger_fingerprint(self, units) -> str:
        """A hash of the decided state the units' packet is built on (the
        partner gates a marker is judged beside, say), or "" when it stands
        on nothing another packet's answer can change."""
        return ""

    def close(self, unit, state, reason, **kwargs):
        raise NotImplementedError

    def close_at_limit(self, unit, why, because):
        raise NotImplementedError

    def limit_request(self, unit, why, granted) -> dict:
        """What the user is asked when `unit` reaches `why` (budget, rounds)."""
        used = unit.get("used") or budgets.empty()
        return {"unit": self.unit_label(unit), "why": why,
                "looks": int(used.get("packets", 0)), "rounds": int(unit.get("rounds", 0)),
                "extension": granted + 1,
                "max_extensions": int(self.options["max_extensions"]), "announced": False}

    def evidence_failed(self, unit, exc) -> str:
        return f"the evidence for this {self.UNIT_NOUN} could not be drawn: {exc.message}"

    # -- persistence --

    def save(self):
        self.store.save(self.record)

    def log(self, **entry):
        self.store.log(self.id, entry)

    @property
    def options(self):
        return self.record["options"]

    def unit(self, key):
        return self.record["units"][key]

    def key_of(self, unit) -> str:
        return self.unit_key_of(self.unit_ref(unit))

    # -- the child receipt of one write --

    def _child(self, project):
        seq = int(self.record.get("write_seq", 0)) + 1
        self.record["write_seq"] = seq
        return dataclasses.replace(
            self.call, operation_id=f"{self.record['operation_id']}.{seq:03d}",
            project_name=project, _data=None, receipted=False,
            extras=dict(self.call.extras))

    # -- budget --

    def allowance(self, unit) -> dict:
        """What the unit may spend now: the session's allowance, once more for
        every extension the unit was granted (`limit_reached`)."""
        base = self.options.get("budget") or self.UNIT_DEFAULT
        return budgets.scaled(base, 1 + int(unit.get("extensions") or 0))

    def over_budget(self, unit) -> bool:
        return bool(budgets.exhausted(unit.get("used") or budgets.empty(),
                                      self.allowance(unit)))

    def limit_reached(self, unit, why) -> bool:
        """The unit wants another look but has reached `why`; True when it may
        go on now. The session's policy (`options["on_limit"]`) decides:
        `extend` grants another allowance, `ask` holds the unit while the user
        is asked (the rest of the session goes on), `stop` flags it for
        review. Past `max_extensions`, or on a no, the unit is closed by
        `close_at_limit` -- never accepted because it ran out."""
        policy = self.options["on_limit"]
        granted = int(unit.get("extensions") or 0)
        key = self.key_of(unit)
        request = unit.get("limit_request")
        if granted >= int(self.options["max_extensions"]):
            self.close_at_limit(unit, why, f"the most extensions a {self.UNIT_NOUN} may have "
                                           "were used")
            return False
        decision = None
        if policy == "extend":
            decision = "continue"
        elif policy == "stop":
            decision = "stop"
        else:
            answers = self.store.control(self.id).get("limit_answers") or {}
            decision = answers.get(key)
            if decision is not None:
                self.store.set_control(self.id, limit_answers={
                    k: v for k, v in answers.items() if k != key})
        if decision == "continue":
            unit["extensions"] = granted + 1
            unit.pop("limit_request", None)
            unit.setdefault("limit_log", []).append(
                {"why": why, "decision": "continue", "by": "policy" if policy == "extend"
                 else "user", "extension": granted + 1})
            self.log(event="limit_extended", unit=key, why=why, extension=granted + 1,
                     policy=policy)
            return True
        if decision == "stop":
            unit.pop("limit_request", None)
            self.close_at_limit(unit, why, "the user chose to stop" if policy == "ask"
                                else "this session stops at its limits")
            return False
        if request is None:
            unit["limit_request"] = self.limit_request(unit, why, granted)
            self.log(event="limit_reached", unit=key, why=why)
        return False

    def waiting_for_user(self) -> list:
        """The units held on a limit question, oldest first."""
        return [u for u in self.record["units"].values()
                if u.get("limit_request") and u["state"] not in self.TERMINAL]

    def wants(self, unit, kind) -> bool:
        """Whether a budgeted look of `kind` may be issued for `unit` now
        (False when the allowance is spent and the limit policy holds it)."""
        if kind in self.BUDGETED_KINDS and self.over_budget(unit) \
                and not self.limit_reached(unit, "budget"):
            return False
        return True

    # -- outstanding packets and their readers --

    def reader(self, name=None) -> dict:
        """A reader's bookkeeping: the unit it answered last (`last_unit`)
        and what it was briefed (`briefed`)."""
        return self.record.setdefault("readers", {}).setdefault(name or DEFAULT_READER, {})

    def last_unit(self, reader=None):
        """The unit `reader` answered last, or None: its next look follows on."""
        return self.record["units"].get(self.reader(reader).get("last_unit") or "")

    def briefed(self, reader=None) -> dict:
        """{epoch, seen}: what `reader` was sent in its current epoch. An
        evidence pointer (`as_in`) may name only a packet seen here, so it
        never refers to one another reader -- another worker's conversation
        -- was sent. The default reader's sits where a single reader's
        always did (`record["briefed"]`)."""
        if (reader or DEFAULT_READER) == DEFAULT_READER:
            return self.record.setdefault("briefed", {"epoch": 0, "seen": {}})
        return self.reader(reader).setdefault("briefed", {"epoch": 0, "seen": {}})

    def new_epoch(self, reader=None) -> int:
        """A fresh conversation for `reader`: nothing it was sent before may
        be pointed at. Returns the new epoch."""
        brief = self.briefed(reader)
        brief["epoch"] = int(brief.get("epoch", 0)) + 1
        brief["seen"] = {}
        return brief["epoch"]

    def may_point_to(self, reader, packet_id) -> bool:
        """Whether `reader` holds `packet_id` in its current epoch."""
        return f"packet:{packet_id}" in self.briefed(reader)["seen"]

    def scope_of(self, packet_id, reader=None, tasks=None):
        """(reader, tasks) to serve the packet that follows an answer: what
        the answer named, else the reader the packet went to and the tasks it
        was scoped to. A worker that passes its `reader` and `tasks` to
        `*_next` but not to `*_answer` would otherwise draw the next packet
        as the default reader, unscoped -- another model's packet, held where
        its own worker can never get it (2026-10-03 benchmark, run C2: an Opus
        worker polled `busy` for 3.5 minutes on a t4 the Sonnet worker's
        answer had drawn)."""
        if reader:
            return reader, tasks
        issued = (self.record["outstanding"].get(packet_id) or {}).get("reader")
        if not issued or issued == DEFAULT_READER:
            return None, tasks
        return issued, tasks or self.reader(issued).get("tasks")

    def outstanding(self, reader=None) -> list:
        """The packet ids out for an answer, oldest first: `reader`'s, or
        everyone's when None."""
        held = self.record["outstanding"]
        return [pid for pid, entry in sorted(held.items(), key=lambda kv: kv[1].get("seq", 0))
                if reader is None or (entry.get("reader") or DEFAULT_READER) == reader]

    def _hold(self, packet_id, *, kind, units, memo_key, reader, fingerprint, strict=False):
        self.record["outstanding"][packet_id] = {
            "kind": kind, "memo_key": memo_key, "units": units, "reader": reader,
            "fingerprint": fingerprint, "strict": bool(strict), "invalid_answers": 0,
            "seq": int(self.record.get("packet_seq") or 0)}
        self.record["outstanding_peak"] = max(int(self.record.get("outstanding_peak") or 0),
                                              len(self.record["outstanding"]))
        self._sync_outstanding()

    def release(self, packet_id):
        """Forget an outstanding packet (answered, closed, or withdrawn)."""
        self.record["outstanding"].pop(packet_id, None)
        self._sync_outstanding()

    def release_all(self, kinds=None):
        """Forget every outstanding packet (of `kinds`, when given)."""
        for packet_id, entry in list(self.record["outstanding"].items()):
            if kinds is None or entry.get("kind") in kinds:
                self.record["outstanding"].pop(packet_id, None)
        self._sync_outstanding()

    def _sync_outstanding(self):
        held = self.record["outstanding"]
        newest = self.outstanding()[-1] if held else None
        self.record["outstanding_packet"] = newest
        self.record["outstanding_kind"] = held[newest]["kind"] if newest else None

    def _with_ledger(self, memo_key, fingerprint):
        """The memo key with the ledger it was built on: the same evidence
        beside a different set of decided gates is a different question."""
        if not fingerprint:
            return memo_key
        return hashlib.sha256(f"{memo_key}:ledger:{fingerprint}".encode("utf-8")).hexdigest()

    # -- packets --

    def _image_rows(self, images, metas):
        return [{"role": meta.get("role"), "caption": meta.get("caption"),
                 "artifact_id": meta.get("artifact_id"),
                 "width": size[0], "height": size[1],
                 "estimated_vision_tokens": budgets.vision_tokens(size[0] * size[1])}
                for (_d, _f, size), meta in zip(images, metas)]

    def issue(self, unit=None, *, reader=None, parallel=None, tasks=None):
        """Build, store and charge the next packet: (packet, images, "packet"),
        or (None, [], "wait") while the bulk pass has not reached the next
        unit, (None, units, "wait_user") while units wait on the user, or
        (None, [], "done") when nothing is left to decide.

        The packet goes to `reader` (`DEFAULT_READER` when None). With
        `parallel` (the most packets out at once) -- or whenever another
        reader's packets are out -- the decision is one `next_ready` allows
        beside them, and (None, [], "busy") says the rest wait on an answer.
        `unit=(kind, units)` issues exactly that decision first.

        A packet issued beside others is held to its ledger (`strict`): its
        answer is refused when the gates it was built on changed meanwhile.
        One issued alone keeps today's single-packet behaviour.

        `tasks` (task ids) scopes the reader: it is given only decisions of
        those tasks, through `next_ready` so readers of other tasks may hold
        packets beside it, and (None, [], "other_tasks") says what is ready
        now is another task's (`self.needs` names it)."""
        reader = reader or DEFAULT_READER
        parallel = int(parallel) if parallel and int(parallel) > 1 else None
        self._scope = frozenset(tasks) if tasks else None
        if tasks:
            # Kept, so an answer that names no reader is followed in its scope (`scope_of`).
            self.reader(reader)["tasks"] = sorted(tasks)
        self.needs = None
        try:
            return self._issue(unit, reader, parallel)
        finally:
            self.needs = getattr(self, "_needs", None) if self._scope is not None else None
            self._scope = self._needs = None

    def _issue(self, unit, reader, parallel):
        wanted = unit
        while True:
            held = self.record["outstanding"]
            strict = bool(parallel or held)
            if wanted is not None:
                (kind, units), wanted = wanted, None
            elif parallel or held or self._scope is not None:
                limit = int(parallel or 0) or len(held) + 1
                if len(held) >= limit:
                    return None, [], "busy"
                self._needs = None
                kind, units = self.next_ready(reader)
            else:
                kind, units = self.next_unit()
            if not self.accepts(kind, units):
                return None, [], "other_tasks"
            if kind is None:
                if self._scope is not None and self._needs:
                    return None, [], "other_tasks"
                return None, [], "done"
            if kind == "wait":
                return None, [], "wait"
            if kind == "busy":
                return None, [], "busy"
            if kind == "wait_user":
                return None, units, "wait_user"
            builder = self.builders()[kind]
            try:
                built = builder(self, units)
            except AgentError as exc:
                if kind in self.SETUP_KINDS or not units:
                    raise
                for unit in units:
                    self.close(unit, "manual_review_recommended",
                               self.evidence_failed(unit, exc))
                continue
            if built is None:
                continue          # the builder closed the unit(s) itself
            packet, images = built
            if len(images) > self.MAX_IMAGES:
                raise AgentError("internal_error", f"a {kind} packet drew {len(images)} images; "
                                 f"at most {self.MAX_IMAGES} are sent")
            seq = int(self.record.get("packet_seq", 0)) + 1
            self.record["packet_seq"] = seq
            packet_id = f"pk_{seq:04d}"
            packet.update({"session_id": self.id, "packet_id": packet_id, "kind": kind,
                           "units": [self.unit_ref(u) for u in units],
                           "answer_schema": self.schema_for(kind),
                           "answer_with": f"{tool_name(self.ANSWER_CAPABILITY)} {{session_id, "
                                          "packet_id, answer: {kind, ...}}"})
            packet["narration"] = self.narrate(packet)
            sizes = [tuple(size) for _data_, _fmt, size in images]
            packet["images"] = self._image_rows(images, packet.pop("_image_meta", []))
            self.lean(packet)
            self.trim(packet)
            fingerprint = self.ledger_fingerprint(units)
            memo_key = self._with_ledger(self.memo_key(packet, [(d, f) for d, f, _s in images]),
                                         fingerprint)
            self._stamp_task(packet, kind, units)
            cost = budgets.packet_cost(packet, sizes)
            for unit in units:
                if kind in self.BUDGETED_KINDS:
                    share = {k: -(-v // max(1, len(units))) for k, v in cost.items()}
                    unit["used"] = budgets.add(unit.get("used") or budgets.empty(), share)
                unit.setdefault("packets", []).append(packet_id)
            self.record["used"] = budgets.add(self.record.get("used") or budgets.empty(),
                                              cost)
            self._hold(packet_id, kind=kind, units=[self.key_of(u) for u in units],
                       memo_key=memo_key, reader=reader, fingerprint=fingerprint,
                       strict=strict)
            # Just this packet's charge and the count: the status tool has the
            # rest, and a packet is read once per decision.
            packet["budget"] = {"this_packet": cost}
            progress = self.progress()
            packet["progress"] = {k: progress[k] for k in ("units_done", "units_total")}
            brief = self.briefed(reader)
            brief["seen"][f"packet:{packet_id}"] = packet_id
            if reader != DEFAULT_READER:
                # Which conversation the packet went to, in which of its epochs
                # (`briefed`); a single reader's packets stay as they were.
                packet["briefed"] = {"reader": reader, "epoch": int(brief.get("epoch", 0))}
            self.store.write_packet(self.id, packet, [(d, f) for d, f, _s in images])
            self.log(event="issued", packet_id=packet_id, kind=kind, units=packet["units"],
                     cost=cost, **({"reader": reader} if reader != DEFAULT_READER else {}))
            if self._replay(packet, memo_key):
                continue      # answered as before; on to the next decision
            return packet, [(d, f) for d, f, _s in images], "packet"

    def _stamp_task(self, packet, kind, units):
        """The packet's AI task, and the model the user's file maps it to
        (`plexora/ai/models_config.py`) -- after the memo key, so neither
        changes which answers replay."""
        from plexora.ai import models_config

        task = self.task_of(kind, units)
        if task:
            packet["task"] = task
            model = models_config.model_for(task)
            if model:
                packet["model"] = model
            self.record.setdefault("models", {})[packet["packet_id"]] = {
                "task": task, "asked": model}

    def answered_by(self, packet_id, model):
        """Record the model the answering agent says it runs on (advisory:
        Plexora cannot see a client's model)."""
        entry = (self.record.get("models") or {}).get(packet_id)
        if entry is not None:
            entry["answered"] = True
            if model:
                entry["model"] = str(model)[:80]

    def models_used(self) -> dict:
        """Per task: the model the user's file asked for, and how many
        packets each model answered (`unstated` when the agent named none).
        Empty for a session no packet of which was stamped."""
        out: dict = {}
        for entry in (self.record.get("models") or {}).values():
            row = out.setdefault(entry["task"], {"asked": entry.get("asked"), "answered_by": {}})
            if entry.get("asked") and not row["asked"]:
                row["asked"] = entry["asked"]
            if entry.get("answered"):
                name = entry.get("model") or "unstated"
                row["answered_by"][name] = row["answered_by"].get(name, 0) + 1
        return out

    def _memo_project(self, packet):
        refs = packet.get("units") or []
        return refs[0]["project"] if refs else self.record["images"][0]

    def _replay(self, packet, memo_key) -> bool:
        """Apply the answer this agent gave to the identical packet before,
        when the session reuses answers. False when there is none, or it no
        longer applies (the packet then goes to the agent)."""
        if not self.options["reuse_answers"]:
            return False
        found = self.memo_get(self._memo_project(packet), memo_key)
        if not found:
            return False
        packet_id = packet["packet_id"]
        entry = dict(self.record["outstanding"][packet_id])
        try:
            self.apply(packet_id, found["answer"], replayed=True)
        except AgentError as exc:
            self.record["outstanding"][packet_id] = {**entry, "invalid_answers": 0}
            self._sync_outstanding()
            self.log(event="replay_refused", packet_id=packet_id, reason=exc.message)
            return False
        self.record.setdefault("replayed", []).append(packet_id)
        for ref in packet.get("units") or []:
            unit = self.record["units"].get(self.unit_key_of(ref))
            if unit is not None:
                unit["replayed"] = int(unit.get("replayed") or 0) + 1
        return True

    def rerender(self, packet_id):
        """Draw the outstanding packet's evidence again: the same packet id,
        kind, units, schema and charge, new images. Returns (packet, images),
        or (None, []) when its builder closed the unit instead."""
        packet, _images = self.store.read_packet(self.id, packet_id)
        units = [self.record["units"][self.unit_key_of(u)] for u in packet["units"]
                 if self.unit_key_of(u) in self.record["units"]]
        built = self.builders()[packet["kind"]](self, units)
        if built is None:
            self.release(packet_id)
            self.log(event="rerendered", packet_id=packet_id, closed=True)
            return None, []
        fresh, images = built
        kept = {k: packet[k] for k in ("session_id", "packet_id", "kind", "units",
                                       "answer_schema", "answer_with", "budget", "progress",
                                       "briefed", "task", "model")
                if k in packet}
        fresh.update(kept)
        fresh["images"] = self._image_rows(images, fresh.pop("_image_meta", []))
        fresh["rerendered"] = int(packet.get("rerendered") or 0) + 1
        self.lean(fresh)
        self.trim(fresh)
        self.store.write_packet(self.id, fresh, [(d, f) for d, f, _s in images])
        self.log(event="rerendered", packet_id=packet_id, times=fresh["rerendered"])
        return fresh, [(d, f) for d, f, _s in images]

    def progress(self):
        units = list(self.record["units"].values())
        done = sum(1 for u in units if u["state"] in self.TERMINAL)
        by_state = {}
        for unit in units:
            by_state[unit["state"]] = by_state.get(unit["state"], 0) + 1
        return {"units_done": done, "units_total": len(units), "by_state": by_state,
                "waiting_for_user": [self.unit_label(u) for u in self.waiting_for_user()],
                "images": len(self.record["images"]),
                "bulk": {"job_id": self.record.get("bulk_job_id"),
                         "state": self.record.get("state")}}

    # -- answers --

    def apply(self, packet_id, raw_answer, *, replayed=False):
        """Validate and apply one answer; returns the outcome dict. The answer
        is kept for identical packets (memo) unless it was itself replayed."""
        from pydantic import TypeAdapter, ValidationError

        record = self.record
        applied = record.setdefault("applied", {})
        if packet_id in applied:
            return {**applied[packet_id], "already_applied": True}
        entry = record["outstanding"].get(packet_id)
        if entry is None:
            raise AgentError("conflict", f"{packet_id} is not the outstanding packet",
                             detail={"outstanding": record.get("outstanding_packet"),
                                     "hint": f"call {tool_name(self.NEXT_CAPABILITY)} for the "
                                             "current packet"})
        kind = entry.get("kind")
        reader = entry.get("reader") or DEFAULT_READER
        if entry.get("strict") and entry.get("fingerprint") is not None:
            stale = self._stale(packet_id, entry)
            if stale is not None:
                return stale
        try:
            answer = TypeAdapter(self.answer_type()).validate_python(raw_answer)
        except ValidationError as exc:
            entry["invalid_answers"] = int(entry.get("invalid_answers", 0)) + 1
            errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg")}
                      for e in exc.errors()][:10]
            if entry["invalid_answers"] >= self.INVALID_ANSWERS:
                packet, _images = self.store.read_packet(self.id, packet_id)
                for ref in packet["units"]:
                    unit = record["units"].get(self.unit_key_of(ref))
                    if unit and unit["state"] not in self.TERMINAL:
                        self.close(unit, "manual_review_recommended",
                                   "two answers to its packet could not be read")
                self.release(packet_id)
                self.save()
                raise AgentError("invalid_input", "the answer did not validate twice; the "
                                 f"{self.UNIT_NOUN} was sent to manual review",
                                 detail={"errors": errors})
            self.save()
            raise AgentError("invalid_input", "the answer did not validate",
                             detail={"errors": errors, "schema": self.schema_for(kind)})
        if answer.kind != kind:
            raise AgentError("invalid_input", f"this packet asks for a {kind} answer, not "
                             f"{answer.kind}", detail={"schema": self.schema_for(kind)})
        entry["invalid_answers"] = 0
        packet, _images = self.store.read_packet(self.id, packet_id)
        memo_key = entry.get("memo_key")
        outcome = self.transitions()[kind](self, packet, answer)
        if memo_key and not replayed:
            self.memo_put(self._memo_project(packet), memo_key,
                          json.loads(answer.model_dump_json(exclude_none=True)), kind=kind,
                          session_id=self.id, packet_id=packet_id,
                          units=[self.ref_label(u) for u in packet.get("units") or []])
        if len(packet["units"]) == 1:
            self.reader(reader)["last_unit"] = self.unit_key_of(packet["units"][0])
        self.release(packet_id)
        if replayed:
            outcome = {**outcome, "replayed": True}
        applied[packet_id] = outcome
        self.log(event="replayed" if replayed else "answered", packet_id=packet_id, kind=kind,
                 answer=json.loads(answer.model_dump_json()), outcome=outcome)
        return outcome

    def _stale(self, packet_id, entry):
        """None while the ledger the packet was built on stands. Otherwise the
        packet is withdrawn and the answer refused with a `reissue` outcome --
        a judgement of evidence that no longer holds is never applied -- and
        the unit becomes its reader's last, so that reader's next packet is
        the same decision drawn on the current gates."""
        units = [self.record["units"][k] for k in entry.get("units") or ()
                 if k in self.record["units"]]
        now = self.ledger_fingerprint(units)
        if now == entry["fingerprint"]:
            return None
        reader = entry.get("reader") or DEFAULT_READER
        self.release(packet_id)
        if len(units) == 1:
            self.reader(reader)["last_unit"] = entry["units"][0]
        outcome = {"state": "reissue", "units": list(entry.get("units") or ()),
                   "reason": "a gate this packet was built on changed while it was out; "
                             "the same decision is served again on the current gates",
                   "built_on": entry["fingerprint"], "now": now}
        self.record.setdefault("applied", {})[packet_id] = outcome
        self.record["reissued"] = int(self.record.get("reissued") or 0) + 1
        self.log(event="stale", packet_id=packet_id, kind=entry.get("kind"), reader=reader,
                 built_on=entry["fingerprint"], now=now)
        return outcome
