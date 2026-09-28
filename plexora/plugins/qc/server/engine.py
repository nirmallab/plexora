"""The QC session's state machine: every transition decided by code.

A session holds units of four types for its image:

- **channel**: one per image channel. The bulk pass scans it; the agent audits
  it on a sheet with the others (`channel_audit`); it closes clean, flagged or
  failed once its candidates are settled.
- **candidate**: one per region a detector proposed (or the agent pointed
  at). It waits for its channel's audit, then is confirmed (`artifact_confirm`,
  up to three looks), scoped across channels (`artifact_scope`), localised
  (`artifact_localize`, then `artifact_grid`) and decided: confirmed with an
  action (exclude, warn, noted), dismissed, or sent to manual review.
- **cells**: one per cell-QC module, when the project has a table.
- **final**: one review of the whole picture before the session closes.

    candidate: awaiting_audit -> awaiting_confirm -> [awaiting_scope]
               -> [awaiting_localize -> awaiting_grid] -> DECIDE
    terminal:  confirmed_exclude | confirmed_warn | confirmed_noted | dismissed |
               merged | manual_review_recommended | user_kept | skipped_locked

Deciding is strictness-free: the agent's judgment is stored, and the action is
`strictness.decide_artifact` of it under the session's preset -- so a later
preset change re-derives every action without asking again. Writes happen only
in `apply` mode, each one an ROI written through the ROI plugin as a child
receipt of the session's operation (`<op>.<nnn>`) with an undo hint.
"""

from __future__ import annotations

import numpy as np

from plexora.agent.errors import AgentError
from plexora.agent.sessions import budget as budgets
from plexora.agent.sessions.engine import BaseEngine, EngineContext
from plexora.agent.sessions.store import SessionStore
from plexora.plugins.qc.server import candidates as cand
from plexora.plugins.qc.server import answers as answer_models
from plexora.plugins.qc.server import schemas, strictness

KIND = "qc"
TERMINAL = schemas.TERMINAL_STATES
ENGINE = schemas.ENGINE
ASKS = schemas.ASKS
BUDGETED_KINDS = schemas.BUDGETED_KINDS

#: Candidate states the engine asks about, in the order a unit passes them.
CANDIDATE_ASKS = ("awaiting_confirm", "awaiting_scope", "awaiting_localize", "awaiting_grid")


def store() -> SessionStore:
    return SessionStore(KIND)


def unit_key(project, type_, id_):
    return f"{project}::{type_}::{id_}"


class engine_for(EngineContext):
    """`with engine_for(call, session_id) as engine:` -- locked, fresh, saved."""

    def default_store(self):
        return store()

    def make(self):
        return QCEngine(self.call, self.session_id, st=self.st)


def memo_versions():
    from plexora.agent.evidence import calibration
    from plexora.plugins.qc.server import detectors, sheets

    return (schemas.SCAN_VERSION, schemas.CELLS_VERSION, calibration.VERSION, sheets.RENDERER,
            *sorted(f"{k}:{v}" for k, v in detectors.versions().items()))


class QCEngine(BaseEngine):
    TERMINAL = TERMINAL
    ASKS = ASKS
    BUDGETED_KINDS = BUDGETED_KINDS
    SETUP_KINDS = schemas.SETUP_KINDS
    INVALID_ANSWERS = ENGINE["invalid_answers"]
    ANSWER_CAPABILITY = "qc.answer"
    NEXT_CAPABILITY = "qc.next"
    UNIT_NOUN = "candidate"
    UNIT_DEFAULT = schemas.QC_UNIT_DEFAULT

    def __init__(self, call, session_id, *, st=None):
        super().__init__(call, session_id, st=st or store())
        self._scans = {}

    # -- hooks --

    def option_defaults(self):
        from plexora.plugins.qc.capabilities_session import option_defaults

        return option_defaults()

    def unit_key_of(self, ref):
        return unit_key(ref["project"], ref["type"], ref["id"])

    def unit_ref(self, unit):
        return {"project": unit["project"], "type": unit["type"], "id": unit["id"]}

    def unit_label(self, unit):
        if unit["type"] == "candidate":
            return f"{unit.get('class_hint')} in {unit.get('channel')}"
        return str(unit["id"])

    def ref_label(self, ref):
        return f"{ref['type']}:{ref['id']}"

    def builders(self):
        from plexora.plugins.qc.server import packets

        return packets.BUILDERS

    def transitions(self):
        from plexora.plugins.qc.server import transitions

        return transitions.APPLY

    def answer_type(self):
        return answer_models.Answer

    def schema_for(self, kind):
        return answer_models.schema_for(kind)

    def memo_key(self, packet, images):
        from plexora.agent.sessions import memo

        return memo.key(packet, images, versions=memo_versions())

    def memo_get(self, project, key):
        from plexora.agent.sessions import memo

        return memo.get(KIND, project, key, self.options["agent"])

    def memo_put(self, project, key, answer, **about):
        from plexora.agent.sessions import memo

        memo.put(KIND, project, key, self.options["agent"], answer, **about)

    def narrate(self, packet):
        from plexora.plugins.qc.server import packets

        return packets.narrate(packet)

    def lean(self, packet):
        from plexora.plugins.qc.server import packets

        return packets.lean(packet, self.options)

    def limit_request(self, unit, why, granted):
        used = unit.get("used") or budgets.empty()
        words = schemas.CLASS_WORDS.get(unit.get("class_hint"), "region")
        return {"unit": self.key_of(unit), "candidate": unit.get("id"),
                "label": f"The {words} in {unit.get('channel') or 'this image'}",
                "project": unit["project"], "channel": unit.get("channel"),
                "class_hint": unit.get("class_hint"), "why": why,
                "words": schemas.LIMIT_WORDS.get(why, why),
                "looks": int(used.get("packets", 0)), "extension": granted + 1,
                "max_extensions": int(self.options["max_extensions"]), "announced": False}

    def close_at_limit(self, unit, why, because):
        unit.pop("limit_request", None)
        self.manual_review(unit, f"stopped at {schemas.LIMIT_WORDS.get(why, why)} before a "
                                 f"confident conclusion ({because}); a person should judge "
                                 "this region")

    # -- access --

    @property
    def project(self):
        return self.record["images"][0]

    def units_of(self, type_, project=None):
        project = project or self.project
        return [u for u in self.record["units"].values()
                if u["type"] == type_ and u["project"] == project]

    def channel_unit(self, name, project=None):
        return self.record["units"].get(unit_key(project or self.project, "channel", name))

    def scan(self, project=None):
        from plexora.plugins.qc.server import scan as scanmod

        project = project or self.project
        if project in self._scans:
            return self._scans[project]
        entry = (self.record.get("scan") or {}).get(project) or {}
        found = scanmod._MEMORY.get((project, entry.get("fingerprint"))) \
            or scanmod.load(project, entry.get("fingerprint"))
        if found is None:
            raise AgentError("precondition_missing", "the session's scan is not available; "
                             "resume the bulk pass", detail={"project": project})
        self._scans[project] = found
        return found

    def mask_of(self, unit):
        return cand.decode_mask(unit["mask"])

    def pixel_for(self, project=None):
        from plexora.server.utils import pixel_scale

        return pixel_scale.pixel_size(self.call.session.project(project or self.project))

    def calibration(self, project=None):
        from plexora.agent.evidence import calibration

        project = project or self.project
        names = [c["name"] for c in self.scan(project).channels]
        try:
            return calibration.current(self.call.session, project, names)
        except AgentError:
            return None

    def table(self):
        table = self.record.get("strictness") or {}
        return strictness.thresholds(table.get("preset", "standard"), table.get("custom"))

    # -- closing --

    def close(self, unit, state, reason, **_kwargs):
        unit["state"] = state
        unit["reason"] = reason
        unit.pop("limit_request", None)
        self.log(event="closed", unit=self.key_of(unit), state=state, reason=reason)

    def manual_review(self, unit, reason):
        """Stopped short of a conclusion: a candidate becomes a warning region
        of class `uncertain_manual_review` (never an exclusion), so evidence
        never vanishes because a budget ran out."""
        if unit["type"] != "candidate":
            self.close(unit, "manual_review_recommended", reason)
            return
        decision = dict(unit.get("decision") or {})
        decision.update(manual_review=True, artifact_class="uncertain_manual_review")
        unit["decision"] = decision
        unit["action"] = "warn"
        self.close(unit, "manual_review_recommended", reason)
        self.write_candidate(unit, klass="uncertain_manual_review", action="warn")

    def check_user_edit(self, unit) -> bool:
        """True (and the unit closed) when the user took the region over."""
        if unit["state"] in TERMINAL:
            return False
        locked = self.store.control(self.id).get("locked_units") or []
        if self.key_of(unit) in locked or unit.get("id") in locked:
            self.close(unit, "user_kept", "taken over by the user in the viewer")
            return True
        return False

    # -- deciding a candidate --

    def decide(self, unit):
        """Put a confirmed candidate in its terminal state and write its ROI."""
        decision = unit.get("decision") or {}
        klass = decision.get("artifact_class") or unit.get("class_hint") or "other_technical"
        decision["artifact_class"] = klass
        unit["decision"] = decision
        unit["class"] = klass
        merged_into = self._merge_target(unit)
        if merged_into is not None:
            merged_into.setdefault("merged", []).append(unit["id"])
            merged_into["channels"] = list(dict.fromkeys(
                [*merged_into.get("channels", []), *unit.get("channels", [])]))
            self.close(unit, "merged", f"the same region as {merged_into['id']}")
            return
        action = strictness.decide_artifact(decision, unit.get("measurement"),
                                            self.table())["action"]
        unit["action"] = action
        state = {"exclude": "confirmed_exclude", "warn": "confirmed_warn",
                 "ignore": "confirmed_noted"}[action]
        self.close(unit, state, f"confirmed {schemas.CLASS_WORDS.get(klass, klass)}; "
                                f"{action}")
        self.write_candidate(unit, klass=klass, action=action)

    def _merge_target(self, unit):
        mask = self.mask_of(unit)
        for other in self.units_of("candidate", unit["project"]):
            if other is unit or other["state"] not in schemas.CONFIRMED_STATES:
                continue
            if (other.get("class") or other.get("class_hint")) != \
                    (unit.get("decision") or {}).get("artifact_class"):
                continue
            theirs = self.mask_of(other)
            inter = np.logical_and(mask, theirs).sum()
            union = np.logical_or(mask, theirs).sum()
            smaller = min(mask.sum(), theirs.sum())
            # The same artifact seen from another channel or detector: the
            # same place (IoU), or one lying almost wholly inside the other.
            if union and (inter / union >= ENGINE["merge_iou"]
                          or (smaller and inter / smaller >= ENGINE["merge_contain"])):
                return other
        return None

    def geometry_of(self, unit):
        """The outline a decided candidate is written with."""
        from plexora.plugins.qc.server import polygons

        if unit.get("geometry"):
            return unit["geometry"]
        chosen = unit.get("variant") or "standard"
        variants = unit.get("variants") or {}
        if chosen in variants:
            return variants[chosen]["geometry"]
        scan = self.scan(unit["project"])
        dilate = {"tight": 0, "standard": 1, "generous": 2}.get(chosen, 1)
        return polygons.mask_to_geometry(self.mask_of(unit), scan.grid, dilate_cells=dilate)

    def write_candidate(self, unit, *, klass, action):
        """Write (apply mode) or propose the candidate's ROI; records it in
        the session's result either way."""
        from plexora.agent.receipts import make_receipt
        from plexora.plugins.qc.server import results, roi_link

        geometry = self.geometry_of(unit)
        if geometry is None:
            unit["write_error"] = "no geometry"
            return None
        record = self._candidate_record(unit, klass=klass, action=action, geometry=geometry)
        unit["geometry"] = geometry
        if self.options["mode"] != "apply":
            unit["proposed"] = True
            self._store_candidate(record)
            return None
        ds = self.call.session.image_data(unit["project"])
        try:
            if unit.get("roi_id"):
                changed = roi_link.rename(ds, unit["roi_id"], action, record)
                record["roi_id"] = unit["roi_id"]
                self._store_candidate(record)
                return changed
            before, after, summary = roi_link.create(ds, record, action=action,
                                                     session_id=self.id)
        except Exception as exc:
            from plexora.plugins.roi.server.repository import ConflictError

            if isinstance(exc, ConflictError):
                # The panel saved meanwhile: read again and write once more.
                before, after, summary = roi_link.create(ds, record, action=action,
                                                         session_id=self.id)
            else:
                unit["write_error"] = str(exc)
                self.log(event="write_failed", unit=self.key_of(unit), error=str(exc))
                return None
        child = self._child(unit["project"])
        receipt = make_receipt(
            child, changed=True, before=None, after={"roi_id": summary["id"],
                                                     "name": summary["name"]},
            revision_before=before, revision_after=after, persistent_state="plugin_store:roi",
            undo_hint={"tool": "delete_roi", "arguments": {
                "project": unit["project"], "roi_id": summary["id"], "confirm": True,
                "base_revision": after}},
            extra={"parent_operation_id": self.record["operation_id"], "qc_session": self.id,
                   "candidate_id": unit["id"], "artifact_class": klass, "action": action})
        unit["roi_id"] = summary["id"]
        unit["written"] = {"revision": after, "category_id": summary["category_id"]}
        unit.setdefault("receipts", []).append(receipt.operation_id)
        self.record.setdefault("receipts", []).append(receipt.operation_id)
        record["roi_id"] = summary["id"]
        self._store_candidate(record)
        roi_link.tell_roi_panel(self.call, unit["project"], "create")
        results.upsert_roi_meta(unit["project"], [roi_link.meta_row(
            record, summary, result={"result_id": self.record["result_id"]},
            session_id=self.id, action=action,
            strictness=(self.record.get("strictness") or {}).get("preset"),
            agent=self.options["agent"], operation_id=receipt.operation_id)])
        return receipt.operation_id

    def _candidate_record(self, unit, *, klass, action, geometry):
        from plexora.plugins.qc.server import results

        decision = unit.get("decision") or {}
        return {"id": unit["id"], "detector": unit.get("detector"),
                "detector_version": unit.get("detector_version"), "class": klass,
                "class_alternatives": list(unit.get("alternatives") or []),
                "scope": decision.get("scope") or unit.get("scope_hint"),
                "channels": list(unit.get("channels") or []),
                "cycles": list(unit.get("cycles") or []), "geometry": geometry,
                "variant_chosen": unit.get("variant") or "standard",
                "severity": unit.get("severity"), "score": unit.get("score"),
                "metrics": unit.get("metrics") or {},
                "measurement": unit.get("measurement") or {},
                "ai_decision": {k: decision.get(k) for k in (
                    "verdict", "artifact_class", "severity", "confidence", "boundary",
                    "scope", "exclude_recommended", "manual_review")},
                "evidence_artifacts": list(unit.get("artifacts") or []),
                "roi_id": unit.get("roi_id"), "action": action, "state": unit["state"],
                "created_by": "agent", "user_state": {}, "session_id": self.id,
                "created_at": results.now_iso()}

    def _store_candidate(self, record):
        from plexora.plugins.qc.server import results

        project = self.project
        with results.lock(project):
            document = results.load(project)
            result = results.get_result(project, document, self.record["result_id"])
            if result is None:
                result = results.new_result(project, session_id=self.id)
                result["result_id"] = self.record["result_id"]
            record["action_by_strictness"] = strictness.actions_by_preset(record)
            result.setdefault("candidates", {})[record["id"]] = record
            results.put_result(document, result)
            results.save(project, document)

    # -- settling ------------------------------------------------------------------

    def settle_channels(self):
        """Close every channel whose candidates are all settled."""
        for channel in self.units_of("channel"):
            if channel["state"] != "awaiting_candidates":
                continue
            mine = [u for u in self.units_of("candidate")
                    if u.get("audit_channel") == channel["id"]]
            if any(u["state"] not in TERMINAL for u in mine):
                continue
            confirmed = [u for u in mine if u["state"] in schemas.CONFIRMED_STATES
                         or u["state"] == "manual_review_recommended"]
            failed = [u for u in confirmed if (u.get("class") or "") == "empty_or_failed_channel"
                      and u["state"] == "confirmed_exclude"]
            if failed:
                self.close(channel, "failed_channel", "confirmed as a failed channel")
            elif confirmed:
                self.close(channel, "flagged", f"{len(confirmed)} region(s) confirmed")
            else:
                self.close(channel, "clean", channel.get("reason") or "no artifact confirmed")

    # -- choosing the next decision ------------------------------------------------------

    def _candidate_kind(self, unit):
        if self.check_user_edit(unit):
            return None
        if unit["state"] not in CANDIDATE_ASKS:
            return None
        kind = ASKS[unit["state"]]
        if not self.wants(unit, kind):
            return None
        return kind

    def next_unit(self):
        record = self.record
        bulk_running = record.get("state") == "bulk_running"
        self.settle_channels()
        last = record["units"].get(record.get("last_unit") or "")
        if last is not None and last["type"] == "candidate" and last["state"] in CANDIDATE_ASKS:
            kind = self._candidate_kind(last)
            if kind:
                return kind, [last]
        project = self.project
        channels = sorted(self.units_of("channel", project), key=lambda u: u["order"])
        batch = int(ENGINE["audit_batch"]) * int(ENGINE["audit_sheets_per_packet"])
        ready = [u for u in channels if u["state"] == "scanned"]
        waiting_scan = any(u["state"] == "pending" for u in channels)
        if ready:
            if len(ready) >= batch or not (bulk_running and waiting_scan):
                return "channel_audit", ready[:batch]
            return "wait", []
        if bulk_running and waiting_scan:
            return "wait", []
        order = {u["id"]: u["order"] for u in channels}
        candidates = sorted(self.units_of("candidate", project),
                            key=lambda u: (order.get(u.get("audit_channel"), 0),
                                           -float(u.get("score") or 0), u["id"]))
        for unit in candidates:
            kind = self._candidate_kind(unit)
            if kind:
                return kind, [unit]
        self.settle_channels()
        if bulk_running:
            return "wait", []
        open_candidates = [u for u in candidates if u["state"] not in TERMINAL]
        if not open_candidates:
            for unit in self.units_of("cells", project):
                if self.check_user_edit(unit):
                    continue
                if unit["state"] == "awaiting_look":
                    kind = schemas.CELL_KINDS[unit["module"].split(":", 1)[0]]
                    if self.wants(unit, kind):
                        return kind, [unit]
        waiting = self.waiting_for_user()
        others_open = [u for u in record["units"].values()
                       if u["type"] != "final" and u["state"] not in TERMINAL]
        final = record["units"].get(unit_key(project, "final", "final"))
        if final is not None and final["state"] in ("pending", "awaiting_review") \
                and not others_open:
            final["state"] = "awaiting_review"
            return "final_qc_review", [final]
        if waiting:
            return "wait_user", waiting
        return None, []

    def progress(self):
        out = super().progress()
        counts = {}
        for unit in self.record["units"].values():
            bucket = counts.setdefault(unit["type"], {"done": 0, "total": 0})
            bucket["total"] += 1
            bucket["done"] += int(unit["state"] in TERMINAL)
        out["by_type"] = counts
        out["not_scanned"] = sum(1 for u in self.units_of("channel") if u["state"] == "pending")
        return out


# -- what a viewer is told (pure: the route uses these without an engine) -------------


def phase_for(record, *, mirroring=False) -> str:
    if record.get("state") in schemas.FINISHED_STATES:
        return "summarizing"
    kind = record.get("outstanding_kind")
    if kind in schemas.SETUP_KINDS:
        return "planning"
    if kind in schemas.LOOK_KINDS:
        return "inspecting" if mirroring else "thinking"
    if kind in schemas.CHECK_KINDS:
        return "validating"
    units = list((record.get("units") or {}).values())
    if units and all(u.get("state") in TERMINAL for u in units):
        return "summarizing"
    return "analyzing"


def summary_of(record) -> dict:
    units = list((record.get("units") or {}).values())
    by_state = {}
    for unit in units:
        by_state[unit.get("state")] = by_state.get(unit.get("state"), 0) + 1
    channels = [u for u in units if u.get("type") == "channel"]
    cands = [u for u in units if u.get("type") == "candidate"]
    return {"units_total": len(units), "units_done": sum(1 for u in units
                                                          if u.get("state") in TERMINAL),
            "channels": {s: sum(1 for u in channels if u.get("state") == s)
                         for s in ("clean", "flagged", "failed_channel",
                                   "manual_review_recommended") if any(
                u.get("state") == s for u in channels)},
            "regions": {"exclude": sum(1 for u in cands if u.get("state") == "confirmed_exclude"),
                        "warn": sum(1 for u in cands if u.get("state") in (
                            "confirmed_warn", "manual_review_recommended")),
                        "noted": sum(1 for u in cands if u.get("state") == "confirmed_noted"),
                        "dismissed": sum(1 for u in cands if u.get("state") == "dismissed")},
            "written": sum(1 for u in cands if u.get("roi_id")),
            "text": _summary_text(channels, cands),
            "proposed": sum(1 for u in cands if u.get("proposed")),
            "replayed": len(record.get("replayed") or []),
            "by_state": by_state}


def _summary_text(channels, cands):
    """The panel's one line for a finished QC session."""
    parts = []
    if channels:
        clean = sum(1 for u in channels if u.get("state") == "clean")
        parts.append(f"{len(channels)} channels ({clean} clean)")
    excluded = sum(1 for u in cands if u.get("state") == "confirmed_exclude")
    warned = sum(1 for u in cands if u.get("state") in ("confirmed_warn",
                                                         "manual_review_recommended"))
    if excluded:
        parts.append(f"{excluded} region{'s' if excluded != 1 else ''} excluded")
    if warned:
        parts.append(f"{warned} flagged for a look")
    if not excluded and not warned:
        parts.append("no artifact confirmed")
    return " · ".join(parts)
