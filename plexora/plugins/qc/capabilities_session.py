"""The QC session as tools: start it, fetch a decision, answer it, finish.

    qc_session_start  -> {session_id, job_id}   (the scan and detectors run as a job)
    qc_next           -> one decision packet (+ <= 2 images), or a status
    qc_answer         -> the outcome, and the next packet inline
    qc_session_status -> every unit's state, the budget, the limit questions
    qc_session_finish -> close | commit (propose mode) | cancel | rollback
    qc_report         -> the HTML / PDF report of a session's result

The loop an agent runs is `qc_next`, then `qc_answer` until the answer says
`decided`. Packets are self-contained, so a new conversation can pick a
session up with `qc_next(session_id)`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import Capability, tool_name_of
from plexora.agent.schemas import AgentModel
from plexora.agent.sessions import budget as budgets
from plexora.agent.sessions import mirror as mirroring
from plexora.agent.sessions import tools as session_tools
from plexora.plugins.qc.server import schemas

OWNER = "qc"
TAGS = ("qc", "quality", "artifact", "fold", "focus", "session", "workflow", "exclude",
        "control")
STATE = "plugin_store:qc"
#: Residual rows (found but not pursued) a brief status/finish shows before
#: the rest are folded into `residual_totals` -- a session can hold ~90.
MAX_RESIDUAL_ROWS = 20


def _allowance(key, description):
    low, high = budgets.UNIT_BOUNDS[key]
    return Field(schemas.QC_UNIT_DEFAULT[key], ge=low, le=high, description=description)


class QCBudget(AgentModel):
    packets: int | None = _allowance("packets", "Looks per candidate (confirm, localise, "
                                                "grid); the audit and scope are free.")
    images: int | None = _allowance("images", "Images per candidate, over its looks.")
    pixels: int | None = _allowance("pixels", "Image pixels per candidate, over its looks.")
    chars: int | None = _allowance("chars", "Packet JSON characters per candidate.")


def _limit_default(name):
    return session_tools.limit_default(schemas.LIMIT_ENV, schemas.LIMIT_DEFAULTS,
                                       schemas.LIMIT_POLICIES, name)


def _refine_default() -> bool:
    import os

    value = os.environ.get(schemas.REFINE_ENV, "").strip().lower()
    return value not in ("0", "false", "no", "off")


#: Which image checks a session runs, unless it says: "all", "none", or a
#: comma list of blur, registration, segmentation.
CHECKS_ENV = "PLEXORA_QC_CHECKS"


def _check_default(name):
    import os

    value = os.environ.get(CHECKS_ENV, "all").strip().lower()
    if value in ("", "all", "1", "true", "on"):
        return True
    if value in ("none", "0", "false", "off"):
        return False
    return name in {v.strip() for v in value.split(",")}


class QCChecks(AgentModel):
    """The image checks a session runs first. Each scores the tissue on its
    own grid; the session shows you places sampled across each score's
    distribution (`score_review`) and turns what you confirm into snug
    regions and cell calls. A check that cannot run here is skipped and
    says why."""

    blur: bool = Field(default_factory=lambda: _check_default("blur"),
                       description="Blur QC on the nuclear channels (supersedes the scan's "
                                   "focus detector).")
    registration: bool = Field(default_factory=lambda: _check_default("registration"),
                               description="Registration Check of each nuclear channel "
                                           "against the reference (supersedes the scan's "
                                           "registration detector).")
    segmentation: bool = Field(default_factory=lambda: _check_default("segmentation"),
                               description="Segmentation QC, when the project has a mask: "
                                           "merged, split, too large, too small and "
                                           "irregular cells, and where they cluster.")
    blur_channels: list[str] | None = Field(
        None, max_length=12, description="The channels Blur QC scores (default: the "
                                         "session's nuclear channels, at most twelve).")
    max_registration_pairs: int | None = Field(
        None, ge=1, le=24, description="At most this many cycles compared to the reference "
                                       "(default six, the first ones).")
    max_pixels: int | None = Field(
        None, ge=1_000_000, description="Skip Segmentation QC on an image of more "
                                        "full-resolution pixels than this.")


class QCSessionOptions(AgentModel):
    scope: Literal["project"] = Field("project", description="One image (a dataset scope "
                                      "comes later).")
    project: str
    channels: list[str] | None = Field(None, description="Default: every channel of the image.")
    mode: Literal["apply", "propose"] = Field(
        "apply", description="apply: confirmed regions are written as ROIs as they are "
                             "decided (each receipted and undoable); propose: nothing is "
                             "written until qc_session_finish(action=commit).")
    strictness: Literal["lenient", "standard", "strict", "custom"] = Field(
        "standard", description="How readily a confirmed artifact excludes cells. Changing "
                                "it later re-derives every call without asking again.")
    custom_thresholds: dict[str, float] | None = Field(
        None, description="With strictness=custom: keys of the strictness table, each "
                          "within the lenient..strict band.")
    cells: bool = Field(True, description="Also flag cells when the project has a table.")
    detectors: list[str] | None = Field(None, description="Default: every detector.")
    map_cell_um: float | None = Field(None, ge=5.0, le=500.0,
                                      description="The scan's map cell, microns a side "
                                                  "(default 50).")
    mirror: bool | None = Field(
        None, description="Show each decision in an open viewer too. Default (null): on when "
                          "one Plexora tab is open to drive (the one showing this image, else "
                          "the only one), off otherwise -- the start result's `mirror.reason` "
                          "says why. true: ask for it (and say why it cannot be); false: never.")
    view_id: str | None = None
    mirror_delay_ms: int = Field(mirroring.DEFAULT_DELAY_MS, ge=0, le=5000)
    image_format: Literal["webp", "png"] = "webp"
    reading: Literal["once", "every_packet"] = Field(
        "once", description="once: the reading guide comes with the start and status; "
                            "every_packet: each packet repeats the texts it needs.")
    known_guide: str | None = Field(None, description="The `guide_version` of a reading guide "
                                    "you already hold: it is then not sent again.")
    budget: QCBudget = Field(default_factory=QCBudget, description="What each candidate may "
                                                                   "spend.")
    agent: str = Field("agent", max_length=80, description="Who answers (the model, say). "
                       "Earlier answers to identical packets are reused only from the same "
                       "agent.")
    reuse_answers: bool = Field(True, description="Answer a packet identical to one this agent "
                                "answered before with that answer, without asking.")
    on_limit: Literal[schemas.LIMIT_POLICIES] = Field(
        default_factory=lambda: _limit_default("on_limit"),
        description="When a candidate reaches its allowance while the evidence still says "
                    "to go on: ask the user, extend, or stop (manual review). Default from "
                    "PLEXORA_QC_ON_LIMIT.")
    max_extensions: int = Field(default_factory=lambda: _limit_default("max_extensions"),
                                ge=0, le=10)
    refine: bool = Field(
        default_factory=_refine_default,
        description="Trace each confirmed artifact's own pixels inside the outline you "
                    "judged, and write that (the outline is where to look; normal tissue "
                    "inside it is kept). false: write the outline itself. Default from "
                    "PLEXORA_QC_REFINE (on).")
    refine_margin_um: float | None = Field(
        None, ge=0.0, le=50.0, description="The margin grown round a traced artifact, "
                                           "microns (default: the class's own, 3-10).")
    checks: QCChecks = Field(default_factory=QCChecks, description="The image checks run "
                             "first (blur, registration, segmentation): their scores find "
                             "the problems, you judge sampled places. Default from "
                             "PLEXORA_QC_CHECKS (all).")
    seed: int = 0


def option_defaults() -> dict:
    return QCSessionOptions.model_construct(project="").model_dump(mode="json")


def _guide(reading, known=None):
    from plexora.plugins.qc.server import packets

    if reading != "once":
        return {}
    version = packets.guide_version()
    if known == version:
        return {"guide_version": version, "reading_guide": "unchanged (you hold it)"}
    return {"guide_version": version, "reading_guide": packets.reading_guide(),
            "reading_note": "every packet's `evidence.guide` names the entries of this guide "
                            "it relies on, and `answer_schema.see` its answer schema"}


def session_uri(session_id):
    return f"plexora://qc/session/{session_id}"


def _now():
    from plexora.agent.audit import now_iso

    return now_iso()


def _announce(call, record, session_id, event, /, **payload):
    from plexora.plugins.qc.server import events

    images = record.get("images") if isinstance(record, dict) else [record]
    events.announce(call.notify, images, session_id, event, **payload)


LABELS = {"unit_noun": "channel", "subject_noun": "image", "finish_tool": "qc_session_finish",
          "outcomes": {"clean": "clean", "flagged": "artifact found",
                       "failed_channel": "failed channel",
                       "confirmed_exclude": "region excluded", "confirmed_warn": "region warned",
                       "confirmed_noted": "region noted", "dismissed": "not an artifact",
                       "manual_review_recommended": "for manual review",
                       "user_kept": "kept by you", "reviewed": "reviewed"}}


def start(call, inp):
    from plexora.agent import registry
    from plexora.agent.sessions.store import new_session_id
    from plexora.plugins.qc.server import engine as engines
    from plexora.plugins.qc.server import results, strictness

    record_ = call.session.project(inp.project)
    if record_.image.is_blank:
        raise AgentError("unsupported_modality", "this sample has no image to check")
    try:
        registry.get("roi.create")
    except Exception:
        raise AgentError("capability_unavailable", "QC writes its regions as ROIs; the ROI "
                         "plugin is not available in this process") from None
    table = strictness.thresholds(inp.strictness, inp.custom_thresholds)
    channel_records = list(record_.image.real_channels)
    names = [c.get("fullname") or c.get("name") for c in channel_records]
    if inp.channels:
        unknown = [c for c in inp.channels if c not in names]
        if unknown:
            raise AgentError("invalid_input", f"not channels of this image: {unknown}",
                             detail={"channels": names[:100]})
        names = [n for n in names if n in inp.channels]
    if not names:
        raise AgentError("invalid_input", "no channels to check")
    session_id = new_session_id("qs")
    project = inp.project
    units = {}
    for order, name in enumerate(names):
        units[engines.unit_key(project, "channel", name)] = {
            "type": "channel", "project": project, "id": name, "order": order,
            "state": "pending"}
    units[engines.unit_key(project, "final", "final")] = {
        "type": "final", "project": project, "id": "final", "state": "pending"}
    has_table = bool(record_.has_table)
    for unit in plan_checks(call.session, record_, project, names, inp.checks):
        units[engines.unit_key(project, "check", unit["id"])] = unit
    segmentation = any(u.get("check") == "segmentation" for u in units.values())
    if inp.cells and has_table:
        from plexora.plugins.qc.server.cells import modules as cell_modules

        for module in cell_modules.planned(call.session, project, segmentation=segmentation):
            units[engines.unit_key(project, "cells", module)] = {
                "type": "cells", "project": project, "id": module, "module": module,
                "state": "pending"}
    result_id = results.new_result_id()
    options = inp.model_dump(mode="json")
    if inp.map_cell_um:
        options["scan_params"] = {"cell_um": float(inp.map_cell_um)}
    # The user's cycle groups (`set_qc_cycles`) enter the scan's fingerprint.
    options["cycles_override"] = results.load(project).get("cycles_override")
    record = {
        "session_id": session_id, "kind": "qc", "created_at": _now(),
        "principal": getattr(call.policy, "principal", None) or "agent",
        "options": options, "scope": "project", "state": "created",
        "operation_id": call.operation_id, "images": [project], "order": names,
        "units": units, "result_id": result_id, "has_table": has_table,
        "strictness": {"preset": inp.strictness, "custom": inp.custom_thresholds,
                       "thresholds": table},
        "used": budgets.empty(), "receipts": [], "packet_seq": 0, "write_seq": 0,
        "mirror": session_tools.mirror_at_start(call, inp.mirror, inp.view_id,
                                                "qc.session_status", project=inp.project),
    }
    with results.lock(project):
        document = results.load(project)
        result = results.new_result(project, session_id=session_id,
                                    strictness={"preset": inp.strictness,
                                                "thresholds": inp.custom_thresholds},
                                    agent={"id": inp.agent,
                                           "principal": record["principal"]})
        result["result_id"] = result_id
        result["origin"] = "session"
        results.put_result(document, result)
        results.save(project, document)
    st = engines.store()
    st.create(record)
    st.sweep()
    receipt = make_receipt(call, changed=False, before=None,
                           after={"session_id": session_id, "project": project,
                                  "channels": names, "result_id": result_id},
                           persistent_state=STATE, reversible=False,
                           extra={"qc_session": session_id, "note": "the session's writes are "
                                  "receipted as <operation_id>.<nnn>"})
    job = TOOLS.submit_bulk(call, session_id)
    with engines.engine_for(call, session_id) as engine:
        engine.record["bulk_job_id"] = job["job_id"]
        phase = engines.phase_for(engine.record)
        progress = engine.progress()
    _announce(call, record, session_id, "started", phase=phase, progress=progress,
              order=names, images=[project], mode=inp.mode,
              view_id=record["mirror"].get("view_id"), labels=LABELS, job_id=job["job_id"])
    return {"session_id": session_id, "job_id": job["job_id"], "project": project,
            "channels": names, "n_units": len(units), "mode": inp.mode,
            "strictness": inp.strictness, "result_id": result_id,
            "cells": any(u["type"] == "cells" for u in units.values()),
            "checks": [u["id"] for u in units.values() if u["type"] == "check"],
            "mirror": {k: record["mirror"].get(k) for k in ("status", "requested", "view_id",
                                                            "reason", "last_error", "hint")
                       if record["mirror"].get(k)},
            "receipt": receipt.model_dump(mode="json"), "resource": session_uri(session_id),
            **_guide(inp.reading, inp.known_guide),
            "next": f"{tool_name_of('qc.next')}(session_id) -- the first packet (a channel "
                    "audit) comes once the scan is done; answer each with "
                    f"{tool_name_of('qc.answer')}"}


def plan_checks(session, record, project, names, checks) -> list:
    """The check units a session runs, read from the channel names alone (no
    pixel is read): Blur QC per nuclear channel, the Registration Check of
    each nuclear channel against the reference, Segmentation QC once when
    there is a mask. Each unit starts `pending`; the bulk pass scores it."""
    from plexora.agent import presets
    from plexora.plugins.qc.server import blur, registration

    checks = checks or QCChecks()
    units = []

    def unit(check, key, **extra):
        return {"type": "check", "project": project, "id": f"{check}:{key}", "check": check,
                "state": "pending", "offset_steps": 0, "rounds": 0, **extra}

    nuclear = [n for n in presets.nuclear_channels(names)]
    if checks.blur:
        chosen = [c for c in (checks.blur_channels or nuclear) if c in names][:blur.MAX_SHOWN]
        for name in chosen:
            units.append(unit("blur", name, channel=name, channels=[name]))
    if checks.registration and len(nuclear) > 1:
        state = registration.resolve(registration.load_state(project),
                                     registration.channel_names(record))
        reference = state.get("reference") if state.get("reference") in names else nuclear[0]
        others = [n for n in nuclear if n != reference]
        limit = checks.max_registration_pairs or int(schemas.ENGINE["max_registration_pairs"])
        for name in others[:limit]:
            units.append(unit("registration", name, channel=name, reference=reference,
                              channels=[reference, name]))
    if checks.segmentation and getattr(record.segmentation, "available", False):
        units.append(unit("segmentation", "calls", channel=None, channels=[],
                          max_pixels=checks.max_pixels))
    return units


# -- the shared loop, bound to QC -----------------------------------------------------


class QCTools(session_tools.SessionTools):
    NEXT = "qc.next"
    ANSWER = "qc.answer"
    STATUS = "qc.session_status"
    FINISH = "qc.session_finish"
    REPORT = "qc.report"
    BULK = "qc.session_bulk"
    STATE = STATE
    FINISHED_STATES = schemas.FINISHED_STATES
    TERMINAL_STATES = schemas.TERMINAL_STATES
    LOOK_KINDS = schemas.LOOK_KINDS
    UNIT_NOUN = "candidate"

    def store(self):
        from plexora.plugins.qc.server.engine import store

        return store()

    def engine_for(self, call, session_id, *, st=None, save=True):
        from plexora.plugins.qc.server.engine import engine_for

        return engine_for(call, session_id, st=st, save=save)

    def announce(self, call, record, session_id, event, /, **payload):
        _announce(call, record, session_id, event, **payload)

    def phase_for(self, record, *, mirroring=False):
        from plexora.plugins.qc.server.engine import phase_for

        return phase_for(record, mirroring=mirroring)

    def summary_of(self, record):
        from plexora.plugins.qc.server.engine import summary_of

        return summary_of(record)

    def unit_word(self, ref):
        if ref.get("type") == "check":
            return str(ref.get("id")).replace(":", " on ")
        return str(ref.get("id")) if ref.get("type") != "candidate" else "a region"

    def subject(self, packet):
        try:
            return packet_subject(packet) or super().subject(packet)
        except Exception:  # an event's wording never breaks a session
            return super().subject(packet)

    def issued_extra(self, packet):
        try:
            return {"evidence": packet_evidence(packet)}
        except Exception:
            return {}

    def answered_extra(self, outcome, closed, kind):
        try:
            text = answer_narration(outcome, closed, kind)
        except Exception:
            text = ""
        return {"narration": text} if text else {}

    def unit_row(self, unit):
        keys = ("type", "id", "state", "reason", "channel", "class_hint", "class", "action",
                "roi_id", "score", "cycle", "module", "check", "threshold", "offset_steps",
                "threshold_source")
        return {k: unit.get(k) for k in keys if unit.get(k) is not None}

    def closed_event(self, unit):
        return {"unit": unit.get("id"), "unit_type": unit.get("type"), "state": unit["state"],
                "reason": unit.get("reason"), "channel": unit.get("channel"),
                "outcome_text": LABELS["outcomes"].get(unit["state"], unit["state"]),
                "marker": unit.get("channel") or unit.get("id")}

    def mirror_run(self, call, session_id, packet):
        from plexora.plugins.qc.server import mirror_script

        return mirror_script.run(call, session_id, packet)

    def mirror_teardown(self, call, session_id, reason):
        from plexora.plugins.qc.server import mirror_script

        return mirror_script.run_teardown(call, session_id, reason)

    def record_limit_answers(self, st, session_id, record, answers):
        return record_limit_answers(st, session_id, record, answers)

    def guide(self, reading, known=None):
        return _guide(reading, known)

    def status_extra(self, engine, detail="brief"):
        record = engine.record
        residual = record.get("residual") or []
        out = {"result_id": record.get("result_id"),
               "checks": {u["id"]: {"check": u.get("check"), "state": u["state"],
                                    "reason": u.get("reason"),
                                    "threshold": u.get("threshold"),
                                    "threshold_source": u.get("threshold_source"),
                                    "offset_steps": u.get("offset_steps")}
                         for u in record["units"].values() if u.get("type") == "check"},
               "scan": {k: {kk: v.get(kk) for kk in ("fingerprint", "reused", "tissue",
                                                     "skipped_detectors", "cycles")}
                       for k, v in (record.get("scan") or {}).items()},
               # Found but not pursued: the top rows (the rest were a session
               # with ~90 of them, most of it candidates nobody will ask
               # about) plus totals, which `detail=full` keeps as the whole
               # list.
               "residual": residual if detail == "full" else residual[:MAX_RESIDUAL_ROWS],
               "residual_totals": {"rows": len(residual),
                                   "candidates": sum(int(r.get("n") or 0) for r in residual),
                                   "cells": sum(int(r.get("cells") or 0) for r in residual)}}
        if detail == "full":
            out["strictness"] = record.get("strictness")
            out["vocabulary"] = {"terminal_states": list(schemas.TERMINAL_STATES),
                                 "artifact_classes": list(schemas.AGENT_CLASSES)}
        return out

    def status_input_model(self):
        return StatusInput

    def commit(self, call, engine):
        written = []
        engine.options["mode"] = "apply"
        try:
            for unit in engine.units_of("candidate"):
                if unit.get("proposed") and unit["state"] in schemas.WRITTEN_STATES:
                    unit["proposed"] = False
                    klass = "uncertain_manual_review" \
                        if unit["state"] == "manual_review_recommended" else unit.get("class")
                    op = engine.write_candidate(unit, klass=klass,
                                                action=unit.get("action") or "warn")
                    if op:
                        written.append(op)
        finally:
            engine.options["mode"] = "propose"
        return written

    def on_finished(self, call, engine, action, out):
        from plexora.plugins.qc.server import finalize

        out["result"] = finalize.finish_result(call, engine, action)


def packet_subject(packet) -> str:
    """What the agent card names a packet by: the candidate's label, class and
    channel ("c7 · excessive background · CD3"), the channels of an audit, the
    kind of a cell look."""
    evidence = packet.get("evidence") or {}
    candidate = evidence.get("candidate") or {}
    if candidate:
        words = schemas.CLASS_WORDS.get(candidate.get("class_hint"), "")
        channels = list(candidate.get("channels") or [])
        where = ", ".join(channels[:2]) + (f" +{len(channels) - 2}" if len(channels) > 2
                                           else "")
        return " · ".join(p for p in (candidate.get("label"), words, where) if p)
    batch = evidence.get("candidates") or []
    if batch:
        labels = [c.get("label") for c in batch if c.get("label")]
        classes = {schemas.CLASS_WORDS.get(c.get("class_hint"), "") for c in batch} - {""}
        return " · ".join(p for p in (", ".join(labels),
                                      classes.pop() if len(classes) == 1 else
                                      f"{len(batch)} candidates") if p)
    if packet.get("kind") == "cell_modules":
        modules = list((evidence.get("modules") or {}).keys())
        return ", ".join(m.replace("channel_outlier:", "") for m in modules[:4]) + (
            f" +{len(modules) - 4}" if len(modules) > 4 else "")
    if packet.get("kind") == "channel_audit":
        rows = evidence.get("rows") or []
        names = [r.get("channel") for r in rows if r.get("channel")]
        if len(names) <= 3:
            return ", ".join(names)
        return f"{len(names)} channels"
    if packet.get("kind") == "final_qc_review":
        return "the whole image"
    if packet.get("kind") == "score_review":
        words = schemas.CHECK_WORDS.get(evidence.get("check"), evidence.get("check") or "")
        on = evidence.get("channel") or "the mask"
        return " · ".join(p for p in (words, on) if p)
    if packet.get("kind") == "cell_segmentation":
        return ", ".join((evidence.get("modules") or {}).keys()) or "segmentation"
    return ""


def packet_evidence(packet) -> list:
    """The packet's images, as the agent card shows them: each one by the
    artifact id `GET /agent/v1/captures/<id>` serves -- the exact picture the
    model was sent, whether or not a viewer is mirrored."""
    from plexora.plugins.qc.server import packets

    label = packets.evidence_label(packet)
    out = []
    for image in packet.get("images") or []:
        artifact_id = image.get("artifact_id")
        if not artifact_id:
            continue
        out.append({"artifact_id": str(artifact_id), "role": image.get("role"),
                    "caption": str(image.get("caption") or label or "")[:300],
                    "title": label, "width": image.get("width"),
                    "height": image.get("height")})
    return out


_VERBS = {"confirmed_exclude": "confirmed", "confirmed_warn": "confirmed",
          "confirmed_noted": "confirmed", "dismissed": "dismissed", "merged": "merged",
          "manual_review_recommended": "left for manual review", "user_kept": "kept as yours"}


def answer_narration(outcome, closed, kind) -> str:
    """The answer in the user's words: what it closed ("c7 confirmed:
    excessive background, warn"), else what it moved on."""
    lines = []
    for unit in closed or []:
        state = unit.get("state")
        if unit.get("type") == "candidate":
            name = unit.get("label") or "A region"
            words = schemas.CLASS_WORDS.get(unit.get("class") or unit.get("class_hint"), "")
            if state in ("dismissed",):
                lines.append(f"{name} dismissed: not an artifact")
            elif state in _VERBS:
                action = schemas.ACTION_WORDS.get(unit.get("action"), unit.get("action"))
                detail = ", ".join(p for p in (words, action) if p)
                lines.append(f"{name} {_VERBS[state]}" + (f": {detail}" if detail else ""))
            else:
                lines.append(f"{name} {LABELS['outcomes'].get(state, state)}")
        else:
            name = unit.get("channel") or unit.get("id") or "A unit"
            lines.append(f"{name} {LABELS['outcomes'].get(state, str(state).replace('_', ' '))}")
    if lines:
        text = "; ".join(lines[:4])
        return text + (f" (+{len(lines) - 4} more)" if len(lines) > 4 else "") + "."
    state = (outcome or {}).get("state")
    if kind == "channel_audit":
        return "Channel audit read; the flagged regions are looked at next."
    if kind == "score_review":
        moved = (outcome or {}).get("offset_steps")
        if (outcome or {}).get("state") == "awaiting_score_review":
            return f"Bar moved a step ({moved:+d} from the automatic one); looking again."
        found = (outcome or {}).get("regions") or {}
        return (f"Check settled: {found.get('decided', 0)} region(s) decided, "
                f"{found.get('to_confirm', 0)} to confirm.")
    if state:
        return f"Answer taken: {str(state).replace('_', ' ')}."
    return ""


TOOLS = QCTools()
StatusInput = session_tools.status_input(schemas.LIMIT_DECISIONS)


def record_limit_answers(st, session_id, record, answers) -> dict:
    """Merge `{unit key | candidate id: continue | stop}` into the control file."""
    keys = {}
    by_id = {u.get("id"): k for k, u in record["units"].items()}
    for name, decision in (answers or {}).items():
        if name in record["units"]:
            keys[name] = decision
        elif name in by_id:
            keys[by_id[name]] = decision
        else:
            raise AgentError("invalid_input", f"{name!r} is not a unit of this session",
                             detail={"units": sorted(by_id)[:50]})
    control = st.control(session_id)
    st.set_control(session_id, limit_answers={**(control.get("limit_answers") or {}), **keys})
    return keys


def bulk(call, inp):
    from plexora.plugins.qc.server import bulk as bulk_pass

    return bulk_pass.run(call, inp)


def next_packet(call, inp):
    return TOOLS.next_packet(call, inp)


def answer(call, inp):
    return TOOLS.answer(call, inp)


def status(call, inp):
    return TOOLS.status(call, inp)


def finish(call, inp):
    return TOOLS.finish(call, inp)


class ReportInput(AgentModel):
    session_id: str | None = Field(None, description="A session; or give `project` for its "
                                   "active result.")
    project: str | None = None
    format: Literal["html", "pdf", "both"] = "both"


def qc_report(call, inp):
    from plexora.plugins.qc.server import report

    return report.write_report(call, session_id=inp.session_id, project=inp.project,
                               fmt=inp.format)


def capabilities():
    def cap(**kwargs):
        kwargs.setdefault("tags", TAGS)
        kwargs.setdefault("entitlement", "ai:qc:session")
        return Capability(owner=OWNER, version="1", **kwargs)

    return [
        cap(name="qc.session_start", tool_name="qc_session_start",
            purpose="Quality-control an image automatically. Starts a QC session: every "
                    "channel is scanned and candidate artifacts found deterministically (a "
                    "job); you then audit every channel at a glance and judge the candidates, "
                    "one packet at a time, through qc_next / qc_answer. Confirmed artifacts "
                    "become ROIs, traced to the artifact's own pixels inside the outline "
                    "you judged; cells get pass/fail.",
            permission="reversible_write", input_model=QCSessionOptions, handler=start,
            writes=("rois", "qc"), persistent=True, egress="metadata",
            reads=("image", "table", "mask", "rois")),
        cap(name="qc.session_bulk", tool_name="qc_session_bulk",
            purpose="The deterministic pass of a QC session (started by qc_session_start; "
                    "call it yourself only to resume one).",
            permission="reversible_write", input_model=session_tools.BulkInput, handler=bulk,
            writes=("qc",), persistent=True, execution="job", egress="aggregates",
            reads=("image", "table")),
        cap(name="qc.next", tool_name="qc_next",
            purpose="The QC session's next decision packet: one question, compact numbers, "
                    "at most two images, and the answer schema. The same packet again if it "
                    "is still unanswered.",
            permission="reversible_write", input_model=session_tools.NextInput,
            handler=next_packet, writes=("rois", "qc"), persistent=True, visual_output=True,
            egress="rendered_pixels", reads=("image", "table", "mask", "rois")),
        cap(name="qc.answer", tool_name="qc_answer",
            purpose="Answer the outstanding QC packet with a typed judgement; the server "
                    "moves the channel or region on (writing its ROI when it is decided) and "
                    "returns the next packet.",
            permission="reversible_write", input_model=session_tools.AnswerInput,
            handler=answer, writes=("rois", "qc"), persistent=True, visual_output=True,
            egress="rendered_pixels", reads=("image", "table", "mask", "rois")),
        cap(name="qc.session_status", tool_name="qc_session_status",
            purpose="A QC session's units (channels, candidate regions, cell modules), what "
                    "it has spent, its limit questions, mirroring; or the recent sessions. "
                    "Can pause or resume it.",
            permission="read", input_model=StatusInput, handler=status, egress="aggregates"),
        cap(name="qc.session_finish", tool_name="qc_session_finish",
            purpose="Finish a QC session: close it (its result becomes the project's active "
                    "QC), commit a propose-mode session's regions, cancel its scan, or roll "
                    "back every region it wrote.",
            permission="reversible_write", input_model=session_tools.FinishInput,
            handler=finish, writes=("rois", "qc"), persistent=True, egress="aggregates"),
        cap(name="qc.report", tool_name="qc_report",
            purpose="The QC report, HTML and/or PDF: every channel, every region with its "
                    "class and action, the excluded tissue and cells with their "
                    "denominators, the cell modules, the provenance.",
            permission="read", input_model=ReportInput, handler=qc_report,
            egress="rendered_pixels", reads=("rois", "qc", "image"),
            tags=TAGS + ("report", "pdf", "html", "review", "provenance")),
    ]
