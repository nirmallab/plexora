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
              technically_failed | no_positive_population | not_binary |
              insufficient_information |
              skipped_locked | skipped_excluded | skipped_manual | skipped_no_marker

Writes happen only in `apply` mode, each one a child receipt of the session's
own operation (`<op>.<nnn>`) with an undo hint, plus a provenance row. A unit
that ends `technically_failed` or `no_positive_population` is written too, as
an empty gate at the column's maximum (`EMPTY_GATE_METHOD`), so every cell of
that marker reads negative and the reason is kept beside it.
"""

from __future__ import annotations

import random

import numpy as np

from plexora.agent import cell_exclusions
from plexora.agent.sessions import budget as budgets
from plexora.agent.sessions.engine import BaseEngine, EngineContext
from plexora.agent.sessions.store import SessionStore
from plexora.plugins.gating.server import model
from plexora.plugins.gating.server.autogate import answers as answer_models
from plexora.plugins.gating.server.autogate import profile as profmod
from plexora.plugins.gating.server.autogate import memo, schemas, tableops

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

#: The provenance method an empty gate is written with, per terminal state.
EMPTY_GATE_METHOD = {"technically_failed": "failed_marker",
                     "no_positive_population": "no_positive_population"}

SOFT_CAPPING = ("estimators_disagree", "unstable_fit", "size_correlated", "nuclear_bleed",
                "edge_enriched", "illumination_gradient_cells", "log_ambiguous", "pileup")


def store() -> SessionStore:
    return SessionStore(KIND)


class engine_for(EngineContext):
    """`with engine_for(call, session_id) as engine:` -- the session locked,
    loaded fresh, and saved when the block succeeds. Every mutation of a
    session goes through this, so the bulk job and the agent's answers never
    overwrite each other."""

    def default_store(self):
        return store()

    def make(self):
        return Engine(self.call, self.session_id, st=self.st)


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
    if image_led_waiver(unit):
        # A membrane (or cytoplasmic, extracellular) stain is under-represented
        # by a nucleus-based cell mean, so the distribution's shape says less
        # about it than a confident look the whole-image check agreed with.
        waived = sorted(set(soft) & set(schemas.IMAGE_LED_RELAXED))
        soft = [f for f in soft if f not in schemas.IMAGE_LED_RELAXED]
        if waived:
            unit["confidence_notes"] = [
                f"{', '.join(waived)} did not cap confidence: a "
                f"{(unit.get('context') or {}).get('compartment')} stain is judged from the "
                "image, and the look and the whole-image check agreed"]
    # T4's distance from the GMM gate, in the steps its candidates took; a
    # gate at a partner's negative control stands on that control, not on
    # the distance (`ctrl:` steps).
    step_sd = unit.get("delta_step_sd")
    delta = abs(step_sd if step_sd is not None else (unit.get("delta_bg_sd") or 0.0))
    if str(unit.get("chosen_step") or "").startswith("ctrl:"):
        delta = 0.0
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
    if cell_exclusions.heavy(unit.get("qc_exclusion")):
        # Most of the image failed QC: what remains may not be representative.
        # A cap, never a route -- the evidence is judged as usual.
        cap = "moderate" if cap == "high" else cap
    failed = (unit.get("regression") or {}).get("failed") or []
    if failed and not unit.get("regression_confirmed"):
        cap = "low"
    if unit.get("no_image_channel") or (unit.get("context") or {}).get("source") == "ai" \
            or unit.get("condition"):
        # A conditional gate ("positive only within CD45+") is written as the
        # plain gate at its threshold: it stands on the partner's gate too.
        cap = "moderate" if cap == "high" else cap
    if order.index(level) > order.index(cap):
        level = cap
    return level


def image_led_waiver(unit) -> bool:
    """Whether an image-led compartment's shape flags stop capping confidence
    (`schemas.COMPARTMENT_POLICY`). The image has to say so twice: a confident
    look confirmed by a second one (beside a reference, or the candidates)
    with the numeric checks passing, or a whole-image check confirmed by eye.
    One first look alone never lifts the cap -- that would let an agent that
    answers "about right" to everything buy high confidence."""
    compartment = (unit.get("context") or {}).get("compartment")
    policy = schemas.COMPARTMENT_POLICY.get(compartment) or {}
    if not policy.get("image_led") or unit.get("path") not in ("t2", "t3", "t4"):
        return False
    if (unit.get("ai_confidence") or 0) < ENGINE["t2_min_confidence"]:
        return False
    if unit.get("regression_confirmed"):
        return True
    regression = unit.get("regression") or {}
    return unit.get("path") in ("t3", "t4") and bool(regression.get("ok"))


def state_for(confidence) -> str:
    return "accepted" if confidence in ("high", "moderate") else "accepted_low_confidence"


# -- the engine -------------------------------------------------------------------


class Engine(BaseEngine):
    """One session, loaded; mutate through methods, then `save()`."""

    TERMINAL = TERMINAL
    ASKS = ASKS
    BUDGETED_KINDS = BUDGETED_KINDS
    SETUP_KINDS = schemas.SETUP_KINDS
    INVALID_ANSWERS = ENGINE["invalid_answers"]
    ANSWER_CAPABILITY = "gating.answer"
    NEXT_CAPABILITY = "gating.next"
    UNIT_NOUN = "marker"

    def __init__(self, call, session_id, *, st=None):
        super().__init__(call, session_id, st=st or store())

    # -- the workflow's hooks (`BaseEngine`) --

    def option_defaults(self):
        from plexora.plugins.gating.capabilities_session import option_defaults

        return option_defaults()

    def unit_key_of(self, ref):
        return unit_key(ref["project"], ref["marker"])

    def unit_ref(self, unit):
        return {"project": unit["project"], "marker": unit["marker"]}

    def unit_label(self, unit):
        return unit["marker"]

    def ref_label(self, ref):
        return ref["marker"]

    def builders(self):
        from plexora.plugins.gating.server.autogate import packets

        return packets.BUILDERS

    def transitions(self):
        from plexora.plugins.gating.server.autogate import transitions

        return transitions.APPLY

    def answer_type(self):
        return answer_models.Answer

    def schema_for(self, kind):
        return answer_models.schema_for(kind)

    def memo_key(self, packet, images):
        # A QC change changes which cells the numbers and pictures describe:
        # an answer given before it is not replayed after it.
        qc = (packet.get("evidence") or {}).get("qc_exclusion") or {}
        extra = (f"qc:{qc.get('mode')}:{qc.get('fingerprint')}",) if qc.get("applied") else ()
        return memo.key(packet, images, versions=extra)

    def memo_get(self, project, key):
        return memo.get(project, key, self.options["agent"])

    def memo_put(self, project, key, answer, **about):
        memo.put(project, key, self.options["agent"], answer, **about)

    def narrate(self, packet):
        from plexora.plugins.gating.server.autogate import packets

        return packets.narrate(packet)

    def lean(self, packet):
        from plexora.plugins.gating.server.autogate import packets

        self._qc_brief(packet)
        return packets.lean(packet, self.options)

    def _qc_brief(self, packet):
        """`evidence.qc_exclusion`: which cells this packet's numbers and
        pictures were drawn from (QC failures left out), in a few fields."""
        evidence = packet.get("evidence")
        if not isinstance(evidence, dict) or not evidence.get("project"):
            return
        try:
            ds = _data(self.call, evidence["project"])
            block = cell_exclusions.describe(ds, marker=evidence.get("marker"))
        except Exception:
            return
        if not block.get("applied"):
            return
        brief = {k: block[k] for k in ("mode", "applied", "n_left_out", "fraction",
                                       "fingerprint", "warning") if k in block}
        evidence["qc_exclusion"] = brief

    def limit_request(self, unit, why, granted):
        used = unit.get("used") or budgets.empty()
        return {"marker": unit["marker"], "project": unit["project"], "why": why,
                "words": schemas.LIMIT_WORDS.get(why, why),
                "looks": int(used.get("packets", 0)), "rounds": int(unit.get("rounds", 0)),
                "extension": granted + 1, "max_extensions": int(self.options["max_extensions"]),
                "proposed": unit.get("candidate"), "announced": False}

    def units_of(self, project):
        return [self.record["units"][unit_key(project, m)] for m in self.record["order"]
                if unit_key(project, m) in self.record["units"]]

    def pixel_for(self, project):
        """What one pixel of `project` is worth for this session's pictures:
        the image's own calibration, else the session's value once set up
        (`pixel_setup`; `source: "estimated"`, drawn as approximate), else
        the estimate while the set-up waits, else None."""
        from plexora.server.utils import pixel_scale

        own = pixel_scale.pixel_size(self.call.session.project(project))
        if own:
            return own
        entry = ((self.record.get("pixel") or {}).get("projects") or {}).get(project) or {}
        value = entry.get("value") or (entry.get("estimate") or {}).get("microns_per_pixel")
        if not value:
            return None
        return {"value": float(value), "unit": "µm", "source": "estimated",
                "basis": entry.get("basis") or "estimate"}

    def lattice_for(self, unit):
        """The unit's lattice (`lattice.build`), built the first time a look
        needs it and frozen: every later look -- whatever the answers were --
        chooses among the same points."""
        from plexora.plugins.gating.server.autogate import lattice as latmod

        lat = unit.get("lattice")
        if lat is not None and lat.get("version") == latmod.VERSION:
            return lat
        ds = _data(self.call, unit["project"])
        refs = self.references_ready(unit)
        unit["lattice"] = latmod.build(
            ds, unit["marker"], gmm=unit.get("gmm"),
            estimators_raw=(unit.get("summary") or {}).get("estimators_raw"),
            references=refs, high=unit.get("high"), seed=int(self.options["seed"]))
        unit["depends_on"] = {r["marker"]: float(r["gate"]) for r in refs}
        return unit["lattice"]

    def field_um(self):
        return float(self.options["field_um"])

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

    def write(self, unit, low, *, method, confidence, state, tier, empty=False):
        """Store `low` for the unit's marker (apply mode) with a child receipt
        and a provenance row. Returns the receipt's operation id or None.

        `empty=True` writes the empty gate of a failed or all-negative marker:
        low == high at the column's maximum (exact, not snapped), `low` ignored."""
        from plexora.agent.receipts import make_receipt
        from plexora.plugins.gating.capabilities import gate_undo_hint
        from plexora.plugins.gating.server.autogate import provenance

        ds = _data(self.call, unit["project"])
        marker = unit["marker"]
        gate = model.get_gate(ds, marker)
        desc = model._description(ds).get(marker) or {}
        if empty:
            top = float(desc.get("max") if desc.get("max") is not None else gate["high"])
            snapped_low = snapped_high = top
            method = EMPTY_GATE_METHOD.get(state, method)
            confidence = confidence or state_confidence(state)
        else:
            high = unit.get("high") if unit.get("high") is not None else gate["high"]
            snapped_low, snapped_high = model.snap_to_grid(low, high, desc)
            if not snapped_low < snapped_high:
                snapped_low, snapped_high = float(low), float(high)
        unit["final"] = snapped_low
        unit["confidence"] = confidence
        # Which QC calls the estimate stood on: a QC change afterwards makes
        # this gate stale (`capabilities_autogate.stale_qc`).
        qc_now = cell_exclusions.describe(ds, marker=marker)
        detail = {"qc_exclusion": {"mode": qc_now.get("mode"),
                                   "applied": bool(qc_now.get("applied")),
                                   "fingerprint": qc_now.get("fingerprint"),
                                   "n_left_out": qc_now.get("n_left_out")},
                  "qc_flags": unit.get("flags"), "bio_flags": unit.get("bio_flags"),
                  "artifact_flags": unit.get("artifact_flags"),
                  "references": unit.get("references"), "decisions": unit.get("packets"),
                  "artifacts": unit.get("artifacts"), "regression": unit.get("regression"),
                  "class": unit.get("class"), "t1_score": (unit.get("t1") or {}).get("score"),
                  "reason": unit.get("reason"),
                  "confidence_notes": unit.get("confidence_notes")}
        if unit.get("condition"):
            detail["condition"] = unit["condition"]
        depends = unit.get("depends_on") or {r["marker"]: r["gate"]
                                             for r in unit.get("reference_gates") or []}
        if depends:
            # The partner gates this one stood on: `gating_qc` flags it when
            # one of them changes later (`stale_dependencies`).
            detail["depends_on"] = depends
        if empty:
            detail["flags"] = sorted(set(unit.get("flags") or []) | {method})
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
                                                           snapped_high, empty=empty)
        except model.GateLocked as exc:
            self.close(unit, "skipped_locked", str(exc), write=False)
            return None
        child = self._child(ds.name)
        receipt = make_receipt(
            child, changed=before != after, before=before, after=after,
            revision_before=revision_before, revision_after=revision_after,
            persistent_state="plugin_store:gating",
            undo_hint=gate_undo_hint(ds.name, marker, before, revision_after),
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
        if state in schemas.EMPTY_GATE_STATES and unit.get("seen") is not None:
            # A failed or all-negative marker gets its empty gate, so every
            # cell reads negative and the reason sits beside it.
            self.write(unit, None, method=EMPTY_GATE_METHOD[state], confidence=confidence,
                       state=state, tier=tier or "T2", empty=True)
            if unit["state"] in TERMINAL:     # locked, or edited meanwhile
                return
            write = False
            self._close_tail(unit, state, reason, confidence)
            return
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
        self._close_tail(unit, state, reason, confidence)

    def _close_tail(self, unit, state, reason, confidence):
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
            # In the units the candidates step in (`candidates.candidate_thresholds`):
            # the spread of the population the gate moved into.
            sd_into = m.get("sd_pos") if unit["delta_fit"] > 0 else m["sd_bg"]
            unit["delta_step_sd"] = unit["delta_fit"] / sd_into if sd_into else None
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

    def rounds_left(self, unit) -> bool:
        """Whether the unit may have another round of candidates."""
        limit = ENGINE["t4_rounds"] * (1 + int(unit.get("extensions") or 0))
        return int(unit.get("rounds", 0)) < limit

    def close_at_limit(self, unit, why, because):
        """Stopped short of a conclusion: flagged for manual review, the best
        gate the evidence reached proposed but not written."""
        unit.pop("limit_request", None)
        proposed = unit.get("candidate")
        where = "" if proposed is None else f" the best gate reached ({proposed:.4g}) is " \
            "proposed, not written;"
        self.close(unit, "manual_review_recommended",
                   f"stopped at {schemas.LIMIT_WORDS.get(why, why)} before a confident "
                   f"conclusion ({because});{where} a person should judge this marker",
                   proposed=proposed)

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
        if kind == "t4_candidates" and not self.rounds_left(unit) \
                and not self.limit_reached(unit, "rounds"):
            return None
        if kind in BUDGETED_KINDS and self.over_budget(unit) \
                and not self.limit_reached(unit, "budget"):
            return None
        return kind

    def next_unit(self):
        """(kind, [units]) of the next decision, or (None, [])."""
        record = self.record
        if (record.get("expression") or {}).get("status") == "pending":
            return "expression_setup", []
        if (record.get("pixel") or {}).get("status") == "pending":
            return "pixel_setup", []
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
        waiting = self.waiting_for_user()
        if waiting:
            return "wait_user", waiting
        return None, []

    # -- packets (issue, replay, rerender: `BaseEngine`) --

    def progress(self):
        units = list(self.record["units"].values())
        done = sum(1 for u in units if u["state"] in TERMINAL)
        by_state = {}
        for unit in units:
            by_state[unit["state"]] = by_state.get(unit["state"], 0) + 1
        return {"units_done": done, "units_total": len(units), "by_state": by_state,
                "not_profiled": by_state.get("pending", 0),
                "waiting_for_user": [u["marker"] for u in self.waiting_for_user()],
                "images": len(self.record["images"]),
                "bulk": {"job_id": self.record.get("bulk_job_id"),
                         "state": self.record.get("state")}}

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
    return {"technically_failed": "failed_qc", "no_positive_population": "low",
            "manual_review_recommended": "manual_review",
            "not_binary": "manual_review", "insufficient_information": "manual_review"}.get(
        state)


# -- what a viewer is told (pure: the Flask route uses these without an Engine) --


def phase_for(record, *, mirroring=False) -> str:
    """What the agent is doing, in `schemas.PHASES`: planning (set-up
    packets), inspecting (a look being shown in the viewer) or thinking (a look
    out for an answer), validating (a whole-image check), analyzing (the
    deterministic pass), summarizing (nothing left, or finished)."""
    if record.get("state") in schemas.FINISHED_STATES:
        return "summarizing"
    kind = record.get("outstanding_kind")
    if kind in schemas.SETUP_KINDS or record.get("panel_pending") \
            or record.get("state") == "needs_setup":
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
    """The run in counts, every bucket named by a `schemas` tuple: what the
    viewer's completed state says."""
    units = list((record.get("units") or {}).values())
    by_state = {}
    for unit in units:
        by_state[unit.get("state")] = by_state.get(unit.get("state"), 0) + 1

    def count(states):
        return sum(by_state.get(s, 0) for s in states)

    empty = count(schemas.EMPTY_GATE_STATES)
    low = by_state.get("accepted_low_confidence", 0)
    review = count(schemas.REVIEW_STATES) - low - empty
    return {"units_total": len(units), "units_done": count(TERMINAL),
            "accepted": count(schemas.ACCEPTED_STATES), "accepted_low_confidence": low,
            "review": review + count(("not_binary", "insufficient_information")),
            "empty": empty, "failed": by_state.get("technically_failed", 0),
            "skipped": count(schemas.SKIPPED_STATES),
            "written": sum(1 for u in units if u.get("receipts")),
            "proposed": sum(1 for u in units if u.get("proposed") is not None
                            and u.get("state") not in schemas.WRITTEN_STATES),
            "questions": len(record.get("questions") or []),
            # Answers taken from an earlier run of the same agent on identical
            # evidence (`memo`), and markers given more looks than the default.
            "replayed": len(record.get("replayed") or []),
            "extended": sum(1 for u in units if u.get("extensions")),
            "by_state": by_state}


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
