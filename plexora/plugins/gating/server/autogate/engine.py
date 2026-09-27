"""The gating session's state machine: every transition decided by code.

A session holds one unit per (image, marker). The bulk pass (`bulk.py`)
profiles every unit and settles what numbers can settle: a clean bimodal
marker is accepted on its GMM gate (T1) and queued for a batched audit strip;
a technically failed or excluded one is closed; everything else waits for a
look. `next_packet` then hands the agent ONE decision -- the next unit in
gating order that needs one -- and `apply_answer` validates the reply and moves
the unit on, deterministically (see `TRANSITIONS` in the module docstring of
each handler). Thresholds are never typed by the agent: it judges, picks among
candidates the server proposed, or flags artifacts.

States of a unit:

    pending -> profiled -> accepted_t1 (awaiting the audit strip)
                        -> qc_confirm | awaiting_t2 | awaiting_t3 | awaiting_t4
                        -> awaiting_regression -> regression_confirm
    terminal: accepted | accepted_low_confidence | manual_review_recommended |
              technically_failed | not_binary | insufficient_information |
              skipped_locked | skipped_excluded | skipped_manual | skipped_no_marker

Writes happen only in `apply` mode, each one a child receipt of the session's
own operation (`<op>.<nnn>`) with an undo hint, plus a provenance row.
"""

from __future__ import annotations

import dataclasses
import json
import random

import numpy as np

from plexora.agent.errors import AgentError
from plexora.agent.sessions import budget as budgets
from plexora.agent.sessions.store import SessionStore
from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.autogate import answers as answer_models
from plexora.plugins.gating.server.autogate import profile as profmod
from plexora.plugins.gating.server.autogate import schemas, tableops

KIND = "gating"

WAITING = schemas.WAITING_STATES
TERMINAL = schemas.TERMINAL_STATES

#: The packets a unit's budget pays for: looks. Confirmations -- a technical
#: check, a whole-image check of a chosen gate, a carried gate -- are bounded
#: by the state machine itself and never stop a unit short of its answer.
BUDGETED_KINDS = ("t2_confirm", "t3_biological", "t4_candidates")

#: The packet kind each waiting state asks for.
ASKS = {"qc_confirm": "qc_confirm", "awaiting_t2": "t2_confirm",
        "awaiting_t3": "t3_biological", "awaiting_t4": "t4_candidates",
        "regression_confirm": "regression_confirm", "transfer_check": "transfer_check"}

#: [cal] the engine's own cut-points (defined with the vocabulary, so the
#: answer models can describe them without importing the engine).
ENGINE = schemas.ENGINE

SOFT_CAPPING = ("estimators_disagree", "unstable_fit", "size_correlated", "nuclear_bleed",
                "edge_enriched", "illumination_gradient_cells", "log_ambiguous", "pileup")


def store() -> SessionStore:
    return SessionStore(KIND)


class engine_for:
    """`with engine_for(call, session_id) as engine:` -- the session locked,
    loaded fresh, and saved when the block succeeds. Every mutation of a
    session goes through this, so the bulk job and the agent's answers never
    overwrite each other."""

    def __init__(self, call, session_id, *, st=None, save=True):
        self.call, self.session_id = call, session_id
        self.st = st or store()
        self.save_on_exit = save
        self._lock = None

    def __enter__(self):
        self._lock = self.st.lock(self.session_id)
        self._lock.__enter__()
        try:
            self.engine = Engine(self.call, self.session_id, st=self.st)
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


def unit_key(project, marker):
    return f"{project}::{marker}"


# -- small helpers ---------------------------------------------------------------


def _data(call, project):
    return call.session.data(project)


def _tool(capability):
    """The tool name an agent calls for a capability (hints never restate it)."""
    from plexora.agent import registry

    return registry.tool_name_of(capability)


def _now():
    from plexora.agent.audit import now_iso

    return now_iso()


def _metrics(profile):
    fit = profile.get("fit") or {}
    sep = fit.get("separation") or {}
    return {"d": sep.get("ashman_d"), "valley": sep.get("valley_ratio"),
            "overlap": sep.get("overlap_mass"),
            "disagree": (fit.get("estimators") or {}).get("disagree_share"),
            "pf_spread": (fit.get("boot") or {}).get("pf_spread"),
            "mu_bg": (fit.get("background") or {}).get("mean"),
            "sd_bg": (fit.get("background") or {}).get("sd"),
            "mu_pos": (fit.get("positive") or {}).get("mean"),
            "sd_pos": (fit.get("positive") or {}).get("sd"),
            "fraction": profile.get("positive_fraction"),
            "s2b": fit.get("signal_to_background")}


def compact_profile(profile):
    """What a packet says about a marker's distribution: ~600 characters."""
    fit = profile.get("fit") or {}
    est = fit.get("estimators") or {}
    col_space = profile.get("fit_space")
    m = _metrics(profile)
    to_raw = (lambda v: float(np.expm1(v))) if col_space == "log1p" else (lambda v: v)
    return tableops.jsonable({
        "class": profile.get("distribution_class"),
        "t1_score": (profile.get("t1") or {}).get("score"),
        "t1_reasons": (profile.get("t1") or {}).get("reasons"),
        "separation_d": m["d"], "valley": m["valley"], "overlap": m["overlap"],
        "estimators_raw": {k: to_raw(v) for k, v in est.items()
                           if k in ("gmm3", "gmm2", "kde", "otsu", "bg_outlier")
                           and v is not None},
        "estimators_disagree_share": m["disagree"],
        "gmm_gate": fit.get("gate_raw"), "positive_fraction": m["fraction"],
        "n_positive": profile.get("n_positive"),
        "n_cells": (profile.get("counts") or {}).get("n_finite"),
        "signal_to_background": m["s2b"], "flags": profile.get("flags"),
        "quantiles_raw": dict(zip([f"p{p:g}" for p in profmod.QGRID],
                                  (profile.get("quantiles") or {}).get("raw") or [])),
        "fit_space": col_space,
    })


def confidence_for(unit) -> str:
    """The confidence rule table (plan E.6), from what the unit recorded."""
    e = ENGINE
    path = unit.get("path")
    m = unit.get("metrics") or {}
    c_ai = unit.get("ai_confidence")
    artifacts = len(set(unit.get("artifact_flags") or []))
    soft = [f for f in unit.get("flags") or [] if f in SOFT_CAPPING]
    delta = abs(unit.get("delta_bg_sd") or 0.0)
    d = m.get("d") or 0.0
    if path == "t1":
        level = "high" if unit.get("audited") else "moderate"
    elif path == "transfer":
        level = unit.get("transfer_confidence") or "moderate"
    elif path in ("t2", "t3"):
        if (c_ai or 0) >= e["high_ai"] and d >= profmod.THRESHOLDS["weak_d"] and not artifacts:
            level = "high"
        elif (c_ai or 0) >= e["moderate_ai"] and artifacts <= 1:
            level = "moderate"
        else:
            level = "low"
    elif path == "t4":
        if delta <= e["delta_high"] and (c_ai or 0) >= e["high_ai"] and not artifacts:
            level = "high"
        elif delta <= e["delta_moderate"] and (c_ai or 0) >= e["moderate_ai"] \
                and artifacts <= 1 and d >= profmod.THRESHOLDS["weak_d"]:
            level = "moderate"
        else:
            level = "low"
    elif path == "budget":
        level = "low"
    else:
        level = "low"
    order = ("low", "moderate", "high")
    cap = "high"
    if soft or unit.get("regression_confirmed") or artifacts >= 1:
        cap = "moderate"
    failed = (unit.get("regression") or {}).get("failed") or []
    if failed and not unit.get("regression_confirmed"):
        cap = "low"
    if unit.get("no_image_channel") or (unit.get("context") or {}).get("source") == "ai":
        cap = "moderate" if cap == "high" else cap
    if order.index(level) > order.index(cap):
        level = cap
    return level


def state_for(confidence) -> str:
    return "accepted" if confidence in ("high", "moderate") else "accepted_low_confidence"


# -- the engine -------------------------------------------------------------------


class Engine:
    """One session, loaded; mutate through methods, then `save()`."""

    def __init__(self, call, session_id, *, st=None):
        self.call = call
        self.store = st or store()
        self.id = session_id
        self.record = self.store.load(session_id)
        # A session stored by an older build lacks newer options: completed
        # with their defaults, so nothing downstream needs a fallback.
        from plexora.plugins.gating.capabilities_session import option_defaults

        options = self.record.setdefault("options", {})
        for key, value in option_defaults().items():
            options.setdefault(key, value)

    # -- persistence --

    def save(self):
        self.store.save(self.record)

    def log(self, **entry):
        self.store.log(self.id, entry)

    @property
    def options(self):
        return self.record["options"]

    def units_of(self, project):
        return [self.record["units"][unit_key(project, m)] for m in self.record["order"]
                if unit_key(project, m) in self.record["units"]]

    def unit(self, key):
        return self.record["units"][key]

    # -- the child receipt of one write --

    def _child(self, project):
        seq = int(self.record.get("write_seq", 0)) + 1
        self.record["write_seq"] = seq
        return dataclasses.replace(
            self.call, operation_id=f"{self.record['operation_id']}.{seq:03d}",
            project_name=project, _data=None, receipted=False,
            extras=dict(self.call.extras))

    def check_user_edit(self, unit) -> bool:
        """True (and the unit closed) when the user changed the gate in the
        viewer since the session read or wrote it."""
        if unit["state"] in TERMINAL:
            return False
        ds = _data(self.call, unit["project"])
        gate = model.get_gate(ds, unit["marker"])
        current = [gate["low"], gate["high"]]
        known = [unit.get("seen"), unit.get("written")]
        if any(k is not None and _same_pair(current, k) for k in known):
            return False
        unit["user_edited"] = True
        self.close(unit, "manual_review_recommended",
                   "the gate was changed in the viewer while the session was deciding it; "
                   "the user's gate is kept")
        return True

    def write(self, unit, low, *, method, confidence, state, tier):
        """Store `low` for the unit's marker (apply mode) with a child receipt
        and a provenance row. Returns the receipt's operation id or None."""
        from plexora.agent.receipts import make_receipt
        from plexora.plugins.gating.server.autogate import provenance

        ds = _data(self.call, unit["project"])
        marker = unit["marker"]
        gate = model.get_gate(ds, marker)
        high = unit.get("high") if unit.get("high") is not None else gate["high"]
        desc = model._description(ds).get(marker) or {}
        snapped_low, snapped_high = model.snap_to_grid(low, high, desc)
        if not snapped_low < snapped_high:
            snapped_low, snapped_high = float(low), float(high)
        unit["final"] = snapped_low
        unit["confidence"] = confidence
        detail = {"qc_flags": unit.get("flags"), "bio_flags": unit.get("bio_flags"),
                  "artifact_flags": unit.get("artifact_flags"),
                  "references": unit.get("references"), "decisions": unit.get("packets"),
                  "artifacts": unit.get("artifacts"), "regression": unit.get("regression"),
                  "class": unit.get("class"), "t1_score": (unit.get("t1") or {}).get("score"),
                  "reason": unit.get("reason")}
        row = {"status": "accepted", "method": method, "tier": tier,
               "confidence": confidence, "state": state, "gmm_proposal": unit.get("gmm"),
               "delta_fit": unit.get("delta_fit"), "delta_fraction": unit.get("delta_fraction"),
               "refinement_rounds": int(unit.get("rounds") or 0), "session_id": self.id,
               "principal": getattr(self.call.policy, "principal", None) or "agent"}
        if self.options["mode"] != "apply":
            unit["proposed"] = snapped_low
            provenance.record(ds.name, marker, **{**row, "status": "proposed",
                                                  "proposed_low": snapped_low},
                              detail=detail)
            return None
        current = [gate["low"], gate["high"]]
        if not any(k is not None and _same_pair(current, k)
                   for k in (unit.get("seen"), unit.get("written"))):
            unit["user_edited"] = True
            self.close(unit, "manual_review_recommended",
                       "the gate was changed in the viewer while the session was deciding "
                       "it; the user's gate is kept")
            return None
        if _same_pair(current, [snapped_low, snapped_high]):
            # Already the stored gate (a T1 gate confirmed by the audit sheet):
            # nothing to write, so no receipt that would undo to itself.
            unit["written"] = current
            provenance.record(ds.name, marker, **row, value_low=current[0],
                              value_high=current[1], written_low=current[0],
                              written_high=current[1], detail=detail)
            return None
        revision_before = model.revision(ds)
        try:
            before, after, revision_after = model.set_gate(ds, marker, snapped_low,
                                                           snapped_high)
        except model.GateLocked as exc:
            self.close(unit, "skipped_locked", str(exc), write=False)
            return None
        child = self._child(ds.name)
        receipt = make_receipt(
            child, changed=before != after, before=before, after=after,
            revision_before=revision_before, revision_after=revision_after,
            persistent_state="plugin_store:gating",
            undo_hint={"tool": "set_gate", "arguments": {
                "project": ds.name, "marker": marker, "low": before["low"],
                "high": before["high"], "expected_revision": revision_after}},
            extra={"parent_operation_id": self.record["operation_id"],
                   "gating_session": self.id, "marker": marker, "method": method,
                   "confidence": confidence})
        unit["written"] = [after["low"], after["high"]]
        unit.setdefault("receipts", []).append(receipt.operation_id)
        self.record.setdefault("receipts", []).append(receipt.operation_id)
        provenance.record(ds.name, marker, **row, value_low=after["low"],
                          value_high=after["high"], written_low=after["low"],
                          written_high=after["high"], operation_id=receipt.operation_id,
                          detail=detail)
        return receipt.operation_id

    def close(self, unit, state, reason, *, write=False, confidence=None, method=None,
              tier=None, low=None, proposed=None):
        """Put a unit in a terminal state (writing `low` first when asked).

        `proposed` records a threshold without writing it -- the best the
        evidence reached when it could not be accepted."""
        unit["reason"] = reason
        if proposed is not None:
            unit["proposed"] = float(proposed)
        if write and low is not None:
            self.write(unit, low, method=method or "ai_accepted",
                       confidence=confidence or "low", state=state, tier=tier or "T2")
            if unit["state"] in TERMINAL:     # the write itself closed it (locked)
                return
        else:
            from plexora.plugins.gating.server.autogate import provenance

            try:
                ds = _data(self.call, unit["project"])
                if unit["marker"] in ds.table.markers and state not in (
                        "skipped_locked", "skipped_excluded", "skipped_no_marker"):
                    existing = provenance.read(ds.name).get(unit["marker"]) or {}
                    if existing.get("status") not in ("locked", "approved", "excluded"):
                        # A kept manual gate is recorded as the user's, so the
                        # next session skips it again instead of taking a
                        # proposal row for the agent's own work.
                        method = "manual" if state == "skipped_manual" else existing.get(
                            "method")
                        fields = {"state": state, "session_id": self.id,
                                  "confidence": confidence or state_confidence(state),
                                  "status": existing.get("status") or "proposed",
                                  "method": method, "gmm_proposal": unit.get("gmm")}
                        if proposed is not None:
                            fields["proposed_low"] = float(proposed)
                        provenance.record(ds.name, unit["marker"], **fields,
                                          detail={"reason": reason,
                                                  "flags": unit.get("flags"),
                                                  "question": unit.get("question")})
            except Exception:  # provenance is a record, never a reason to fail
                pass
        unit["state"] = state
        unit.pop("last_manifest", None)
        if confidence:
            unit["confidence"] = confidence
        elif unit.get("confidence") is None:
            unit["confidence"] = state_confidence(state)
        self.log(event="closed", unit=unit_key(unit["project"], unit["marker"]),
                 state=state, reason=reason, confidence=unit.get("confidence"))

    def finalize(self, unit, *, method):
        """Accept the unit's current final threshold with the rule-table confidence.

        Never against a direction on record (`transitions` module docstring):
        such a unit is closed with its gate proposed instead."""
        direction = unit.get("direction")
        if direction:
            self.close(unit, "insufficient_information",
                       f"the last look said the gate is too "
                       f"{'low' if direction == 'up' else 'high'} and no candidate was chosen; "
                       "nothing written", proposed=unit.get("candidate"))
            return
        confidence = confidence_for(unit)
        state = state_for(confidence)
        tier = {"t1": "T1", "t2": "T2", "t3": "T3", "t4": "T4",
                "transfer": "T1", "budget": "T2"}.get(unit.get("path"), "T2")
        low = unit["candidate"]
        self.write(unit, low, method=method, confidence=confidence, state=state, tier=tier)
        if unit["state"] in TERMINAL:
            return
        unit["state"] = state
        unit["tier"] = tier
        unit.pop("last_manifest", None)
        unit["reason"] = unit.get("reason") or f"accepted at {tier}"
        self.log(event="accepted", unit=unit_key(unit["project"], unit["marker"]),
                 state=state, confidence=confidence, low=unit.get("final"), tier=tier)

    # -- regression (deterministic) --

    def run_regression(self, unit):
        ds = _data(self.call, unit["project"])
        partners = [{"partner": r["marker"], "gate": r["gate"], "relation": r["relation"]}
                    for r in unit.get("reference_gates") or []]
        prior = (unit.get("context") or {}).get("expected_fraction")
        result = tableops.local_or_node(ds, "gating.autogate.regression", {
            "marker": unit["marker"], "final": unit["candidate"], "gmm": unit["gmm"],
            "prior": prior, "partners": partners})
        unit["regression"] = {"ok": result["ok"], "failed": result["failed"],
                              "fraction": result["fraction"],
                              "checks": result["checks"]}
        m = unit.get("metrics") or {}
        col = profmod.column(ds, unit["marker"])
        if m.get("sd_bg"):
            unit["delta_fit"] = float(col.to_fit(unit["candidate"]) - col.to_fit(unit["gmm"]))
            unit["delta_bg_sd"] = unit["delta_fit"] / m["sd_bg"]
        n_gmm = col.n_positive(unit["gmm"])
        n_final = col.n_positive(unit["candidate"])
        unit["delta_fraction"] = (n_final - n_gmm) / col.n_finite if col.n_finite else None
        return result

    def settle(self, unit):
        """Advance a unit through every deterministic step it is waiting on."""
        if unit["state"] == "awaiting_regression":
            result = self.run_regression(unit)
            if result["ok"] or unit.get("regression_confirmed") \
                    or unit.get("no_image_channel"):
                # Nothing to look at for a column without a channel: the
                # failed checks cap its confidence instead (confidence_for).
                self.finalize(unit, method=unit.get("method") or "ai_accepted")
            else:
                unit["state"] = "regression_confirm"

    # -- budget --

    def over_budget(self, unit) -> bool:
        allowance = self.options["budget"] or budgets.UNIT_DEFAULT
        return bool(budgets.exhausted(unit.get("used") or budgets.empty(), allowance))

    def close_on_budget(self, unit):
        """Out of budget: accept the best clean candidate at low confidence, or
        stop with the question open. Never against the agent: when the last
        look said which way the gate is wrong and no candidate was chosen
        since, the current gate is proposed, not written."""
        direction = unit.get("direction")
        if direction:
            self.close(unit, "insufficient_information",
                       f"budget spent; the last look said the gate is too "
                       f"{'low' if direction == 'up' else 'high'} and no candidate was "
                       "chosen; nothing written", proposed=unit.get("candidate"))
            return
        unit["path"] = "budget"
        self.run_regression(unit)
        if unit["regression"]["ok"]:
            unit["reason"] = "budget spent; the current candidate passes every numeric check"
            self.finalize(unit, method=unit.get("method") or "gmm")
        else:
            self.close(unit, "insufficient_information",
                       "budget spent before the evidence settled the gate; nothing written",
                       proposed=unit.get("candidate"))

    # -- references --

    def references_ready(self, unit):
        """The unit's usable references, refreshed from what this run has
        gated (accepted at moderate or better) and from gates the user set and
        the run kept (skipped as manual or locked). Stored on the unit as
        `reference_gates` and returned."""
        from plexora.plugins.gating.server.autogate import context

        ds = _data(self.call, unit["project"])
        panel = context.for_project(ds)
        gated, gates = {}, {}
        for other in self.units_of(unit["project"]):
            if other is unit:
                continue
            state, conf = other["state"], other.get("confidence")
            if state == "accepted_t1":
                conf = "high"
            if state in ("accepted", "accepted_t1") and conf in ("high", "moderate"):
                gated[other["marker"]] = conf
                gates[other["marker"]] = other.get("final") or other.get("candidate")
            elif state in ("skipped_manual", "skipped_locked") and other.get("thresholded") \
                    and other.get("seen"):
                gated[other["marker"]] = "high"
                gates[other["marker"]] = other["seen"][0]
        refs = context.references_for(panel, unit["marker"], gated)
        unit["reference_gates"] = [{"marker": r["marker"], "relation": r["relation"],
                                    "gate": gates[r["marker"]],
                                    "confidence": gated[r["marker"]]}
                                   for r in refs if gates.get(r["marker"]) is not None]
        return unit["reference_gates"]

    # -- choosing the next decision --

    def _decision(self, unit):
        """The packet kind a profiled, open unit needs now, or None when it
        settled, closed or waits on the audit sheet."""
        if self.check_user_edit(unit):
            return None
        if unit["state"] == "awaiting_regression":
            self.settle(unit)
        if unit["state"] in TERMINAL or unit["state"] not in ASKS:
            return None
        kind = ASKS[unit["state"]]
        if kind in BUDGETED_KINDS and self.over_budget(unit):
            self.close_on_budget(unit)
            return None
        return kind

    def next_unit(self):
        """(kind, [units]) of the next decision, or (None, [])."""
        record = self.record
        if record.get("panel_pending"):
            return "panel_context", []
        # Stay on the marker being refined: its next look follows its last,
        # so the evidence (and a mirrored viewer) does not jump between markers.
        last = record["units"].get(record.get("last_unit") or "")
        if last is not None and (last["state"] in ASKS
                                 or last["state"] == "awaiting_regression"):
            kind = self._decision(last)
            if kind:
                return kind, [last]
        bulk_running = record.get("state") == "bulk_running"
        for project in record["images"]:
            units = self.units_of(project)
            if project != record.get("reference_image") and any(
                    u["state"] == "transfer_pending" for u in units):
                from plexora.plugins.gating.server.autogate import transfer

                if not transfer.reference_done(self):
                    return ("wait", []) if bulk_running or any(
                        u["state"] not in TERMINAL for u in self.units_of(
                            record["reference_image"])) else (None, [])
                transfer.decide_image(self, project)
            strip = []
            for unit in units:
                if unit["state"] in TERMINAL:
                    continue
                if unit["state"] == "pending":
                    # Gating order matters -- a later marker may need this one
                    # as its reference -- so nothing past a unit the bulk pass
                    # has not reached yet is asked about.
                    if bulk_running:
                        if strip:
                            return "t1_strip", strip
                        return "wait", []
                    self.close(unit, "manual_review_recommended",
                               "the deterministic pass did not reach this marker")
                    continue
                kind = self._decision(unit)
                if kind:
                    return kind, [unit]
                if unit["state"] == "accepted_t1":
                    strip.append(unit)
                    if len(strip) >= ENGINE["strip_batch"]:
                        return "t1_strip", strip
            if strip:
                return "t1_strip", strip
        return None, []

    # -- packets --

    def issue(self):
        """Build, store and charge the next packet: (packet, images, "packet"),
        or (None, [], "wait") while the bulk pass has not reached the next
        marker, or (None, [], "done") when nothing is left to decide."""
        from plexora.plugins.gating.server.autogate import packets

        while True:
            kind, units = self.next_unit()
            if kind is None:
                return None, [], "done"
            if kind == "wait":
                return None, [], "wait"
            builder = packets.BUILDERS[kind]
            try:
                built = builder(self, units)
            except AgentError as exc:
                if kind == "panel_context" or not units:
                    raise
                for unit in units:
                    self.close(unit, "manual_review_recommended",
                               f"the evidence for this marker could not be drawn: {exc.message}")
                continue
            if built is None:
                continue          # the builder closed the unit(s) itself
            packet, images = built
            if len(images) > packets.MAX_IMAGES:
                raise AgentError("internal_error", f"a {kind} packet drew {len(images)} images; "
                                 f"at most {packets.MAX_IMAGES} are sent")
            seq = int(self.record.get("packet_seq", 0)) + 1
            self.record["packet_seq"] = seq
            packet_id = f"pk_{seq:04d}"
            packet.update({"session_id": self.id, "packet_id": packet_id, "kind": kind,
                           "units": [{"project": u["project"], "marker": u["marker"]}
                                     for u in units],
                           "answer_schema": answer_models.schema_for(kind),
                           "answer_with": f"{_tool('gating.answer')} {{session_id, "
                                          "packet_id, answer: {kind, ...}}"})
            sizes = [tuple(size) for _data_, _fmt, size in images]
            packet["images"] = [{"role": meta.get("role"), "caption": meta.get("caption"),
                                 "artifact_id": meta.get("artifact_id"),
                                 "width": size[0], "height": size[1],
                                 "estimated_vision_tokens": budgets.vision_tokens(
                                     size[0] * size[1])}
                                for (_d, _f, size), meta in zip(images,
                                                                packet.pop("_image_meta", []))]
            budgets.trim(packet)
            cost = budgets.packet_cost(packet, sizes)
            for unit in units:
                if kind in BUDGETED_KINDS:
                    share = {k: -(-v // max(1, len(units))) for k, v in cost.items()}
                    unit["used"] = budgets.add(unit.get("used") or budgets.empty(), share)
                unit.setdefault("packets", []).append(packet_id)
            self.record["used"] = budgets.add(self.record.get("used") or budgets.empty(),
                                              cost)
            self.record["outstanding_packet"] = packet_id
            self.record["outstanding_kind"] = kind
            packet["budget"] = {"session_used": self.record["used"],
                                "this_packet": cost}
            packet["progress"] = self.progress()
            self.store.write_packet(self.id, packet, [(d, f) for d, f, _s in images])
            self.log(event="issued", packet_id=packet_id, kind=kind, units=packet["units"],
                     cost=cost)
            return packet, [(d, f) for d, f, _s in images], "packet"

    def rerender(self, packet_id):
        """Draw the outstanding packet's evidence again (after a renderer
        change, or when its images were lost): the same packet id, kind,
        units, schema and charge, new images. Returns (packet, images), or
        (None, []) when its builder closed the unit instead."""
        from plexora.plugins.gating.server.autogate import packets

        packet, _images = self.store.read_packet(self.id, packet_id)
        units = [self.record["units"][unit_key(u["project"], u["marker"])]
                 for u in packet["units"]
                 if unit_key(u["project"], u["marker"]) in self.record["units"]]
        built = packets.BUILDERS[packet["kind"]](self, units)
        if built is None:
            self.record["outstanding_packet"] = None
            self.record["outstanding_kind"] = None
            self.log(event="rerendered", packet_id=packet_id, closed=True)
            return None, []
        fresh, images = built
        kept = {k: packet[k] for k in ("session_id", "packet_id", "kind", "units",
                                       "answer_schema", "answer_with", "budget", "progress")
                if k in packet}
        fresh.update(kept)
        fresh["images"] = [{"role": meta.get("role"), "caption": meta.get("caption"),
                            "artifact_id": meta.get("artifact_id"),
                            "width": size[0], "height": size[1],
                            "estimated_vision_tokens": budgets.vision_tokens(size[0] * size[1])}
                           for (_d, _f, size), meta in zip(images, fresh.pop("_image_meta", []))]
        fresh["rerendered"] = int(packet.get("rerendered") or 0) + 1
        budgets.trim(fresh)
        self.store.write_packet(self.id, fresh, [(d, f) for d, f, _s in images])
        self.log(event="rerendered", packet_id=packet_id, times=fresh["rerendered"])
        return fresh, [(d, f) for d, f, _s in images]

    def progress(self):
        units = list(self.record["units"].values())
        done = sum(1 for u in units if u["state"] in TERMINAL)
        by_state = {}
        for unit in units:
            by_state[unit["state"]] = by_state.get(unit["state"], 0) + 1
        return {"units_done": done, "units_total": len(units), "by_state": by_state,
                "not_profiled": by_state.get("pending", 0),
                "images": len(self.record["images"]),
                "bulk": {"job_id": self.record.get("bulk_job_id"),
                         "state": self.record.get("state")}}

    # -- answers --

    def apply(self, packet_id, raw_answer):
        """Validate and apply one answer; returns the outcome dict."""
        from pydantic import TypeAdapter, ValidationError

        record = self.record
        applied = record.setdefault("applied", {})
        if packet_id in applied:
            return {**applied[packet_id], "already_applied": True}
        if record.get("outstanding_packet") != packet_id:
            raise AgentError("conflict", f"{packet_id} is not the outstanding packet",
                             detail={"outstanding": record.get("outstanding_packet"),
                                     "hint": f"call {_tool('gating.next')} for the current "
                                             "packet"})
        kind = record.get("outstanding_kind")
        try:
            answer = TypeAdapter(answer_models.Answer).validate_python(raw_answer)
        except ValidationError as exc:
            record["invalid_answers"] = int(record.get("invalid_answers", 0)) + 1
            errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg")}
                      for e in exc.errors()][:10]
            if record["invalid_answers"] >= ENGINE["invalid_answers"]:
                packet, _images = self.store.read_packet(self.id, packet_id)
                for ref in packet["units"]:
                    unit = record["units"].get(unit_key(ref["project"], ref["marker"]))
                    if unit and unit["state"] not in TERMINAL:
                        self.close(unit, "manual_review_recommended",
                                   "two answers to its packet could not be read")
                record["outstanding_packet"] = None
                record["invalid_answers"] = 0
                self.save()
                raise AgentError("invalid_input", "the answer did not validate twice; the "
                                 "marker was sent to manual review",
                                 detail={"errors": errors})
            self.save()
            raise AgentError("invalid_input", "the answer did not validate",
                             detail={"errors": errors,
                                     "schema": answer_models.schema_for(kind)})
        if answer.kind != kind:
            raise AgentError("invalid_input", f"this packet asks for a {kind} answer, not "
                             f"{answer.kind}", detail={"schema": answer_models.schema_for(kind)})
        record["invalid_answers"] = 0
        packet, _images = self.store.read_packet(self.id, packet_id)
        from plexora.plugins.gating.server.autogate import transitions

        outcome = transitions.APPLY[kind](self, packet, answer)
        if len(packet["units"]) == 1:
            ref = packet["units"][0]
            record["last_unit"] = unit_key(ref["project"], ref["marker"])
        record["outstanding_packet"] = None
        record["outstanding_kind"] = None
        applied[packet_id] = outcome
        self.log(event="answered", packet_id=packet_id, kind=kind,
                 answer=json.loads(answer.model_dump_json()), outcome=outcome)
        return outcome

    # -- questions for the user --

    def ask(self, unit, ask_user):
        """Store a question the data cannot settle; close the unit if the
        session asks users at all (max_tier 5)."""
        question = {"unit": unit_key(unit["project"], unit["marker"]),
                    "question": ask_user.question, "options": ask_user.options,
                    "why": ask_user.why, "asked_at": _now()}
        self.record.setdefault("questions", []).append(question)
        unit["question"] = question
        if int(self.options["max_tier"]) >= 5:
            self.close(unit, "insufficient_information", f"needs the user: {ask_user.question}")
            return True
        return False


def state_confidence(state):
    return {"technically_failed": "failed_qc", "manual_review_recommended": "manual_review",
            "not_binary": "manual_review", "insufficient_information": "manual_review"}.get(
        state)


def _same_pair(a, b):
    if a is None or b is None:
        return False
    return all(x is not None and y is not None and abs(float(x) - float(y))
               <= 1e-9 * max(1.0, abs(float(x))) for x, y in zip(a, b))


def seeded_order(items, seed):
    """A permutation of `items` fixed by `seed` (positional-bias guard)."""
    items = list(items)
    random.Random(seed).shuffle(items)
    return items
