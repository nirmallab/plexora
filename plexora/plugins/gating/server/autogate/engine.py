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

Several readers may answer one session at once (`gating_next(parallel=N)`):
`ready_units` then says which decisions may go out beside the packets already
out. A marker waits until every partner it could be judged beside that comes
earlier in gating order -- and the partner it is gated `within` -- is
terminal; the set-up packets and the T1 audit strips go out alone. Each
packet keeps the fingerprint of the partner gates it was built on
(`ledger_fingerprint`), and an answer to one whose partners changed is
refused and the decision served again.

States of a unit:

    pending -> profiled -> accepted_t1 (awaiting the audit strip)
                        -> qc_confirm | awaiting_t2 | awaiting_t3 | awaiting_t4
                        -> awaiting_regression -> regression_confirm
    terminal: accepted | accepted_low_confidence | manual_review_recommended |
              technically_failed | no_positive_population | not_binary |
              insufficient_information |
              skipped_locked | skipped_excluded | skipped_manual | skipped_no_marker |
              skipped_no_channel (no image channel: never profiled, never written) |
              display_set (an auto-contrast run: brightness settled, never gated)

Writes happen only in `apply` mode, each one a child receipt of the session's
own operation (`<op>.<nnn>`) with an undo hint, plus a provenance row. A unit
that ends `technically_failed` or `no_positive_population` is written too, as
an empty gate at the column's maximum (`EMPTY_GATE_METHOD`), so every cell of
that marker reads negative and the reason is kept beside it.
"""

from __future__ import annotations

import hashlib
import json
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
BUDGETED_KINDS = ("t2_confirm", "t3_biological", "t4_candidates", "cell_panel")

#: The packet kind each waiting state asks for.
ASKS = {"qc_confirm": "qc_confirm", "awaiting_absence": "absence_check",
        "awaiting_t2": "t2_confirm", "awaiting_t3": "t3_biological",
        "awaiting_t4": "t4_candidates", "awaiting_panel": "cell_panel",
        "regression_confirm": "regression_confirm", "transfer_check": "transfer_check"}

#: [cal] the engine's own cut-points (defined with the vocabulary, so the
#: answer models can describe them without importing the engine).
ENGINE = schemas.ENGINE

#: The provenance method of a gate written for a unit closed for review: the
#: best gate the evidence reached, tagged so the sidebar and reports say a
#: person should check it.
REVIEW_METHOD = "needs_review"

#: The terminal states that judge the marker itself: empty, failed, or for a
#: person to check. Code never reaches one on numbers alone -- a look stands
#: behind it (`close`) -- unless the close says why none could
#: (`unlooked`: the user changed the gate, the data could not be read, the
#: question is the user's). lsp35766_p193 CD8a (2026-10-18) was closed for
#: review by the lineage numbers before any look, at its scored start.
JUDGED_STATES = ("manual_review_recommended", "no_positive_population", "technically_failed",
                 "insufficient_information", "not_binary")

#: The provenance method an empty gate is written with, per terminal state.
EMPTY_GATE_METHOD = {"technically_failed": "failed_marker",
                     "no_positive_population": "no_positive_population"}

SOFT_CAPPING = ("estimators_disagree", "unstable_fit", "size_correlated", "nuclear_bleed",
                "edge_enriched", "illumination_gradient_cells", "log_ambiguous", "pileup")

#: The regression failures a purity push answers (`Engine.purity_push`):
#: the partners check, with the spatial checks an over-low gate drags along.
PURITY_PUSHABLE = frozenset(("partners", "edge_ratio", "spatial_tiles"))

#: The regression checks that say where positives are, not how many: when
#: only these fail, the whole-image look may hold or reject the gate, never
#: move it (`regression_confirm`).
SPATIAL_CHECKS = frozenset(("edge_ratio", "spatial_tiles"))

#: [cal] A population is contaminated (`Engine.lineage_purity`) when this
#: share of its brightest positives sits on an exclusive partner, and that
#: share is this many times the partner's share of all cells. On the PCA
#: benchmark the two markers the user called absent sat at 0.63 and 0.74
#: (8-10 times); every other immune marker at 0.20 or less (1.2 times).
#: `min_top`: the brightest cells a share is read off -- fewer than the
#: purity push calibrates on (`bivariate.THRESHOLDS["control_min_cells"]`): a
#: share near 0.6 is clear on a few dozen cells, and an absent marker has few
#: positives.
LINEAGE = {"contaminated_top": 0.5, "contaminated_vs_prevalence": 3.0, "min_top": 30}


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
        elif (c_ai or 0) >= e["moderate_ai"] and d >= profmod.THRESHOLDS["weak_d"] \
                and not artifacts and biology_agrees(unit):
            # Intensity, a reliable partner, the image and the expected
            # biology all agree: more than a confident look alone.
            level = "high"
        elif (c_ai or 0) >= e["moderate_ai"]:
            # How many technical remarks a look wrote is not how good the gate
            # is: any remark caps the gate at moderate (below), never lower --
            # a low gate is no reference, and its dependants would lean on a
            # weaker partner instead.
            level = "moderate"
        else:
            level = "low"
    elif path == "t4":
        if delta <= e["delta_high"] and (c_ai or 0) >= e["high_ai"] and not artifacts:
            level = "high"
        elif delta <= e["delta_moderate"] and (c_ai or 0) >= e["moderate_ai"] \
                and d >= profmod.THRESHOLDS["weak_d"]:
            level = "moderate"
        else:
            level = "low"
    elif path == "panel":
        from plexora.plugins.gating.server.autogate import hypotheses

        level = hypotheses.confidence(unit)
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
    if "pattern_mismatch" in (unit.get("look_flags") or ()) or unit.get("t4_stopped_mixed"):
        # The stain was not the compartment's pattern, or the gate stopped at
        # a row that held both kinds of cell: a doubt, never a route.
        cap = "moderate" if cap == "high" else cap
    if unit.get("lineage_seen_real"):
        # The brightest positives sit on an exclusive lineage by the numbers,
        # and the whole image says the stain is real: the population stands,
        # the doubt about where its edge is does too.
        cap = "moderate" if cap == "high" else cap
    failed = (unit.get("regression") or {}).get("failed") or []
    if failed and not unit.get("regression_confirmed"):
        cap = "low"
    if (unit.get("context") or {}).get("source") == "ai" or unit.get("condition"):
        # A conditional gate ("positive only within CD45+") is written as the
        # plain gate at its threshold: it stands on the partner's gate too.
        cap = "moderate" if cap == "high" else cap
    if order.index(level) > order.index(cap):
        level = cap
    return level


def biology_agrees(unit) -> bool:
    """Every line of evidence agrees: the look saw the stain pattern the
    marker's compartment has, and the start was placed by a partner curve on
    a reliable gate."""
    compartment = (unit.get("context") or {}).get("compartment")
    fits = (schemas.COMPARTMENT_POLICY.get(compartment) or {}).get("patterns") or ()
    if (unit.get("look") or {}).get("pattern") not in fits:
        return False
    scored = unit.get("scoring") or {}
    if scored.get("basis") != "coexpression":
        return False
    lead = scored.get("leading")
    return any(p.get("marker") == lead and p.get("gate_confidence") in ("high", "moderate")
               for p in scored.get("partners") or [])


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


def start_low(unit):
    """The gate the unit's looks started from: the scored proposal, or the
    Auto gate. Distances (regression, T4 confidence) are measured from it."""
    start = unit.get("start") or {}
    return start.get("low") if start.get("low") is not None else unit.get("gmm")


def compact_scoring(result, partners) -> dict:
    """What a unit keeps of `scoring.score` (and a packet shows): the
    proposal, its basis, and each candidate with its scores, rounded."""
    def r(value):
        return None if value is None else float(f"{float(value):.4g}")

    candidates = []
    for cand in result.get("candidates") or []:
        entry = {"id": cand["id"], "family": cand["family"], "low": r(cand["low"]),
                 "fraction": r(cand.get("fraction")), "score": r(cand.get("score")),
                 "components": {k: r(v) for k, v in (cand.get("components") or {}).items()}}
        for key in ("robust", "relation", "elasticity", "rise"):
            if cand.get(key) is not None:
                entry[key] = r(cand[key]) if isinstance(cand[key], float) else cand[key]
        candidates.append(entry)
    background = result.get("background") or {}
    return {"version": result.get("version"), "proposal": r(result.get("proposal")),
            "basis": result.get("proposal_source"), "leading": result.get("leading"),
            "background": {k: v if isinstance(v, str) else r(v) for k, v in background.items()},
            "region": result.get("region"), "notes": list(result.get("notes") or []),
            "control": None if not result.get("control") else {
                "value": r(result["control"]["value"]), "by": result["control"]["by"],
                "n": result["control"]["n"]},
            "partners": [{k: p[k] for k in ("marker", "relation", "direction",
                                            "gate_confidence", "weight", "derived",
                                            "down_weighted") if p.get(k) is not None}
                         for p in partners],
            "candidates": candidates}


# -- the engine -------------------------------------------------------------------


def judged_limits(unit, col):
    """(lo, hi) in the fit space: the gate may not cross an interval a look
    at candidates judged -- one judged `own_stain` keeps the gate below it,
    one judged `unstained` / `neighbour_only` above it. Only intervals on the
    side the unit's candidate left them on count (`Engine.clamp_to_judged`)."""
    g = float(col.to_fit(unit["candidate"]))
    lo, hi = -np.inf, np.inf
    for a, b, word in unit.get("judged") or ():
        fa, fb = float(col.to_fit(a)), float(col.to_fit(b))
        if word == "own_stain" and fa >= g - 1e-9:
            hi = min(hi, fa)
        elif word in ("unstained", "neighbour_only") and fb <= g + 1e-9:
            lo = max(lo, fb)
    return lo, hi


class Engine(BaseEngine):
    """One session, loaded; mutate through methods, then `save()`."""

    TERMINAL = TERMINAL
    ASKS = ASKS
    BUDGETED_KINDS = BUDGETED_KINDS
    SETUP_KINDS = schemas.SETUP_KINDS
    #: Out alone: a set-up answer changes what every packet says, and an audit
    #: strip settles several markers at once.
    EXCLUSIVE_KINDS = schemas.SETUP_KINDS + schemas.PLAN_KINDS + ("t1_strip",)
    INVALID_ANSWERS = ENGINE["invalid_answers"]
    ANSWER_CAPABILITY = "gating.answer"
    NEXT_CAPABILITY = "gating.next"
    UNIT_NOUN = "marker"
    MODULE = "gating"

    def __init__(self, call, session_id, *, st=None):
        super().__init__(call, session_id, st=st or store())

    def _migrate(self):
        """A session stored while gating had its per-cell model (removed
        2026-10-12) resumes without it: a unit waiting on the model's
        correction goes back to its regression, a correction packet still
        out is dropped, and the old `hybrid` option's tiles become `tiles`.
        Whatever else the model left on the record (`cellcall`,
        `model_withheld`, a unit's `model`) is never read."""
        super()._migrate()
        record = self.record
        options = record["options"]
        if "classifier" not in record:
            # Stored before the classifier layer (2026-10-19): it resumes
            # without one, as it was started.
            record["classifier"] = {"schema": 0}
            options["classifier"] = "off"
        # Stored under an engine's first name (classic, candidates): it keeps
        # that engine (one stored before the option is v1, `option_defaults`).
        options["engine"] = schemas.engine_name(options["engine"])
        if "hybrid" in options:
            if options.pop("hybrid") in ("tiles", "full"):
                options["tiles"] = True
        for unit in (record.get("units") or {}).values():
            if unit.get("state") == "awaiting_correction":
                unit["state"] = "awaiting_regression"
        held = record["outstanding"]
        dropped = [pid for pid, entry in held.items() if entry.get("kind") == "correction_check"]
        for packet_id in dropped:
            del held[packet_id]
        if dropped:
            self._sync_outstanding()

    # -- the workflow's hooks (`BaseEngine`) --

    def option_defaults(self):
        """What a stored session's missing options are completed with: today's
        defaults, except the engine -- a session stored before the engine
        option (2026-10-20) ran v1, whatever the default is now."""
        from plexora.plugins.gating.capabilities_session import option_defaults

        return {**option_defaults(), "engine": "v1"}

    def unit_key_of(self, ref):
        return unit_key(ref["project"], ref["marker"])

    # -- the multi-view looks (`SessionOptions.tiles`) --

    def tiles_on(self) -> bool:
        """Whether the session draws its looks as the 2 x 2 multi-view block
        (`SessionOptions.tiles`)."""
        return bool(self.options["tiles"])

    def split_row(self, unit, a, b):
        """The points a mixed row from `a` to `b` is split into
        (`lattice.split`), joined to the unit's frozen lattice; [] when the
        row is no wider than `lattice.SPLIT_MIN_SD` background sds."""
        from plexora.plugins.gating.server.autogate import lattice as latmod

        sd = (((unit.get("scoring") or {}).get("background") or {}).get("sd_fit")
              or (unit.get("metrics") or {}).get("sd_bg"))
        if not sd:
            return []
        ds = _data(self.call, unit["project"])
        return latmod.split(self.lattice_for(unit), ds, unit["marker"], a, b, core_sd=float(sd),
                            high=unit.get("high"))

    def partner_panels(self, unit):
        """{co, anti, gates, markers} for a multi-view look (`collage`'s `co` /
        `anti` panels): one expected and one not-expected partner of the
        marker (`partners.partner_plan`: gated partners first), by image channel, with the gates this run holds for
        them; {} when the marker has none."""
        from plexora.plugins.gating.server.autogate import partners, views

        ds = _data(self.call, unit["project"])
        entries = self.panel_for(unit["project"]).get("entries") or {}
        channels = {m: views.image_channel(ds, m) for m in ds.table.markers}
        present = {m for m, c in channels.items() if c and m != unit["marker"]}
        book = self.ledger(unit["project"], without=unit)
        gated = {m: e["gate"] for m, e in book.items()
                 if e["grade"] not in ("failed", "pending") and e.get("gate") is not None}
        plan = partners.partner_plan(entries, unit["marker"], present, {}, 1, set(gated))
        out = {"markers": {}, "gates": {}}
        for p in plan:
            if p["kind"] in out["markers"]:
                continue
            out["markers"][p["kind"]] = p["marker"]
            out[p["kind"]] = channels[p["marker"]]
            if p["marker"] in gated:
                out["gates"][channels[p["marker"]]] = float(gated[p["marker"]])
        return out if out["markers"] else {}

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
        if self.tiles_on():
            # The multi-view looks are other pictures: off, the key is today's.
            extra += ("tiles:1",)
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
                                       "fingerprint", "warning", "n_not_called",
                                       "channel_verdict", "digest") if k in block}
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
        start = unit.get("start") or {}
        unit["lattice"] = latmod.build(
            ds, unit["marker"], gmm=unit.get("gmm"),
            estimators_raw=(unit.get("summary") or {}).get("estimators_raw"),
            references=refs, high=unit.get("high"), seed=int(self.options["seed"]),
            scoring=unit.get("scoring"),
            start=start.get("low") if start.get("source") == "scoring" else None)
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
                   "the user's gate is kept", unlooked="user_edit")
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
        if unit.get("hypotheses"):
            from plexora.plugins.gating.server.autogate import hypotheses

            # The candidates, the looks and why they stopped (`hypotheses`).
            detail["hypotheses"] = hypotheses.provenance(unit)
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
                       "it; the user's gate is kept", unlooked="user_edit")
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
              tier=None, low=None, proposed=None, unlooked=None):
        """Put a unit in a terminal state (writing `low` first when asked).

        `proposed` is the best threshold the evidence reached when it could
        not be accepted. A unit closed for review (`schemas.REVIEW_WRITE_STATES`)
        is written there anyway, tagged `needs_review` with confidence
        `manual_review` (`_review_gate`): every gated marker ends with a value.

        A judgement (`JUDGED_STATES`) on a unit no packet has shown is a bug
        in the path that made it, unless `unlooked` says why no look could
        settle it; it is logged (`closed_without_look`) and marked on the unit."""
        if state in JUDGED_STATES and not unit.get("packets") and unlooked is None:
            unit["closed_unlooked"] = True
            self.log(event="closed_without_look",
                     unit=unit_key(unit["project"], unit["marker"]), state=state,
                     reason=reason)
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
        best = self._review_gate(unit, state, proposed)
        if not write and best is not None:
            # Every marker that is gated ends with a value: one the evidence
            # could not settle is written at the best gate it reached, tagged
            # for manual review, never left at its full range.
            write, low, method = True, best, REVIEW_METHOD
            confidence = "manual_review"
            unit["needs_review"] = True
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
                        "skipped_locked", "skipped_excluded", "skipped_no_marker",
                        "skipped_no_channel", "display_set"):
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

    def _review_gate(self, unit, state, proposed):
        """The gate a unit closed for review is written at: the proposal it
        reached, else its current candidate, the scored start or the Auto
        gate. None when it must not be written -- not a review state, the user
        changed the gate in the viewer (theirs stands), or nothing was ever
        estimated."""
        if state not in schemas.REVIEW_WRITE_STATES or unit.get("user_edited") \
                or unit.get("seen") is None:
            return None
        for value in (proposed, unit.get("candidate"), start_low(unit), unit.get("gmm")):
            if value is not None:
                return float(value)
        return None

    def _close_tail(self, unit, state, reason, confidence):
        unit["state"] = state
        unit.pop("last_manifest", None)
        if confidence:
            unit["confidence"] = confidence
        elif unit.get("confidence") is None:
            unit["confidence"] = state_confidence(state)
        self.log(event="closed", unit=unit_key(unit["project"], unit["marker"]),
                 state=state, reason=reason, confidence=unit.get("confidence"))
        from plexora.plugins.gating.server.autogate import cellcalls

        cellcalls.after_write(self, unit)
        try:
            self.reference_failed(unit)
        except Exception:  # the evidence graph is a guide; closing must not fail on it
            pass
        try:
            from plexora.plugins.gating.server.autogate import replan

            replan.after_close(self, unit)
        except Exception as exc:  # re-planning is a guide too; it is logged, never raised
            self.log(event="replan_error", unit=unit_key(unit["project"], unit["marker"]),
                     error=str(exc)[:200])

    def finalize(self, unit, *, method):
        """Accept the unit's current final threshold with the rule-table confidence.

        Never against a direction on record (`transitions` module docstring):
        such a unit is closed for review instead, its gate written and tagged."""
        direction = unit.get("direction")
        if direction:
            self.close(unit, "insufficient_information",
                       f"the last look said the gate is too "
                       f"{'low' if direction == 'up' else 'high'} and no candidate was chosen; "
                       "written there for manual review", proposed=unit.get("candidate"))
            return
        from plexora.plugins.gating.server.autogate import cellcalls

        if unit.get("path") != "panel" and cellcalls.check(self, unit):
            # The classifier disagrees with this gate beside it: the cells it
            # flags get a look first (once per marker; `cellcalls.check`).
            return
        confidence = confidence_for(unit)
        state = state_for(confidence)
        tier = {"t1": "T1", "t2": "T2", "t3": "T3", "t4": "T4", "panel": "T2",
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
        cellcalls.after_write(self, unit)
        try:
            from plexora.plugins.gating.server.autogate import replan

            replan.after_accept(self, unit)
        except Exception as exc:
            self.log(event="replan_error", unit=unit_key(unit["project"], unit["marker"]),
                     error=str(exc)[:200])

    # -- regression (deterministic) --

    def run_regression(self, unit):
        ds = _data(self.call, unit["project"])
        partners = [{"partner": r["marker"], "gate": r["gate"], "relation": r["relation"]}
                    for r in unit.get("reference_gates") or []]
        prior = (unit.get("context") or {}).get("expected_fraction")
        anchor = start_low(unit)
        result = tableops.local_or_node(ds, "gating.autogate.regression", {
            "marker": unit["marker"], "final": unit["candidate"], "gmm": anchor,
            "prior": prior, "partners": partners,
            # The band the looks walk, so a gate the lattice offered is never
            # failed for leaving the mixture's narrower one.
            "guard": (self.lattice_for(unit).get("guard")
                      if (unit.get("start") or {}).get("source") == "scoring" else None)})
        result = self._with_lineage(unit, result, {p["partner"] for p in partners})
        unit["regression"] = {"ok": result["ok"], "failed": result["failed"],
                              "fraction": result["fraction"],
                              "checks": result["checks"],
                              "spatial_only": bool(result["failed"])
                              and set(result["failed"]) <= SPATIAL_CHECKS}
        m = unit.get("metrics") or {}
        col = profmod.column(ds, unit["marker"])
        if m.get("sd_bg"):
            unit["delta_fit"] = float(col.to_fit(unit["candidate"]) - col.to_fit(anchor))
            unit["delta_bg_sd"] = unit["delta_fit"] / m["sd_bg"]
            # In the units the candidates step in (`candidates.candidate_thresholds`):
            # the spread of the population the gate moved into.
            sd_into = m.get("sd_pos") if unit["delta_fit"] > 0 else m["sd_bg"]
            unit["delta_step_sd"] = unit["delta_fit"] / sd_into if sd_into else None
        n_gmm = col.n_positive(anchor)
        n_final = col.n_positive(unit["candidate"])
        unit["delta_fraction"] = (n_final - n_gmm) / col.n_finite if col.n_finite else None
        return result

    def lineage_purity(self, unit, low=None) -> list:
        """How pure the gate's positives are against every marker that should
        exclude them -- each exclusive relation the panel's tree gives the
        marker, stated or derived (`hierarchy.relations`: a subset inherits
        its parents' exclusions), whose partner this run gated at moderate or
        better. Computed here, not on the node (`bivariate.purity_excess`
        reads the columns through the table handle).

        Each row is {partner, derived, cross_lineage, at_gate, at_top, excess,
        prevalence, contaminated}: `prevalence` is the partner's share of all
        cells, and `contaminated` says even the brightest positives sit on the
        partner (`LINEAGE`) -- a population the self-calibrated excess cannot
        judge, because its top is as impure as its gate. Only across lineages
        (`cross_lineage`: the two share no parent in the tree -- PD1 within
        CD45 against SOX10): siblings of one lineage (CD8 and CD4 within CD3)
        light each other in a dense aggregate, and their overlap is spill, not
        a population that is not there."""
        from plexora.plugins.gating.server.autogate import bivariate, context, hierarchy

        ds = _data(self.call, unit["project"])
        low = unit.get("candidate") if low is None else low
        if low is None:
            return []
        panel = self.panel_for(unit["project"])
        book = self.ledger(unit["project"], without=unit)
        markers = set(ds.table.markers)

        def lineage(marker):
            return {marker} | {r["marker"] for r in hierarchy.relations(panel, marker)
                               if r["relation"] == "subset" and r["direction"] == "forward"}

        own = lineage(unit["marker"])
        out = []
        for rel in hierarchy.relations(panel, unit["marker"]):
            # Every exclusion the tree gives, low ones too: a low one (PD1, a
            # moderate subset of CD45, inherits CD45's SOX10 exclusion at low)
            # only ever raises a question (`replan`), and the push below
            # takes moderate or better.
            if rel["relation"] != "exclusive" or rel["direction"] != "forward":
                continue
            entry = book.get(rel["marker"]) or {}
            if entry.get("grade") not in hierarchy.RELIABLE or entry.get("gate") is None \
                    or rel["marker"] not in markers:
                continue
            try:
                purity = bivariate.purity_excess(ds, unit["marker"], float(low), rel["marker"],
                                                 float(entry["gate"]), relation="exclusive",
                                                 min_top=LINEAGE["min_top"])
            except Exception:
                purity = None
            if purity is None:
                continue
            prevalence = float(profmod.column(ds, rel["marker"]).fraction(entry["gate"]) or 0.0)
            cross = not (own & lineage(rel["marker"]))
            contaminated = cross and purity["at_top"] >= LINEAGE["contaminated_top"] and \
                purity["at_top"] >= LINEAGE["contaminated_vs_prevalence"] * prevalence
            out.append({"partner": rel["marker"], "derived": bool(rel["derived"]),
                        "confidence": rel["confidence"], "cross_lineage": cross,
                        **{k: round(v, 4) if isinstance(v, float) else v
                           for k, v in purity.items()},
                        "prevalence": round(prevalence, 4), "contaminated": bool(contaminated)})
        return out

    def _with_lineage(self, unit, result, checked):
        """The regression's result with the lineage rows added: an excess
        against an exclusive partner the node did not check fails `partners`
        (so the purity push applies), and a contaminated population fails a
        `lineage` check of its own."""
        from plexora.plugins.gating.server.autogate import bivariate, context, regression

        try:
            rows = self.lineage_purity(unit)
        except Exception as exc:
            self.log(event="lineage_error", unit=unit_key(unit["project"], unit["marker"]),
                     error=str(exc)[:200])
            return result
        unit["lineage"] = rows
        result = {**result, "checks": [dict(c) for c in result.get("checks") or []],
                  "failed": list(result.get("failed") or [])}
        worse = [r for r in rows if r["partner"] not in checked
                 and context.CONFIDENCE_RANK.get(r.get("confidence"), 0) >= 1
                 and r["n_top"] >= bivariate.THRESHOLDS["control_min_cells"]
                 and r["excess"] > regression.THRESHOLDS["partner_tolerance"]]
        if worse:
            for check in result["checks"]:
                if check["name"] == "partners":
                    check["ok"] = False
                    check["value"] = list(check.get("value") or []) + [
                        {"relation": "exclusive", **r} for r in worse]
            if "partners" not in result["failed"]:
                result["failed"].append("partners")
        contaminated = [r for r in rows if r["contaminated"]]
        if contaminated and not unit.get("lineage_ack"):
            worst = max(contaminated, key=lambda r: r["at_top"])
            unit["lineage_contamination"] = worst
            result["checks"].append({"name": "lineage", "ok": False, "value": contaminated,
                                     "limit": dict(LINEAGE)})
            result["failed"].append("lineage")
        result["ok"] = not result["failed"]
        return result

    def close_contaminated(self, unit):
        """Close a unit whose brightest positives sit on an exclusive lineage
        for review, and say so (`replan` puts it to the agent)."""
        c = unit["lineage_contamination"]
        self.close(unit, "manual_review_recommended",
                   f"even the brightest {unit['marker']} positives sit on {c['partner']}, which "
                   f"should exclude them ({c['at_top']:.0%} of them, against {c['prevalence']:.0%} "
                   "of all cells): the whole population is suspect",
                   proposed=unit.get("candidate"))

    def lineage_screen(self, unit) -> None:
        """Before the first look: whether the population's brightest cells sit
        on an exclusive lineage, recorded (`lineage_suspect`) and nothing more.
        The looks gate the marker as any other; a population still on that
        lineage at the regression is shown on the whole image before anything
        closes it (`lineage_by_eye`). It used to close the unit for review
        here: lsp35766_p193 CD8a (2026-10-18) ended at its scored start with no
        look, on a low-confidence exclusion (CD11b, derived through CD3), and
        the whole-image check replan sent it to later called its signal real.
        Once per unit."""
        if unit.get("lineage_screened") or unit.get("lineage_ack") \
                or (unit.get("used") or {}).get("packets"):
            return
        unit["lineage_screened"] = True
        try:
            rows = self.lineage_purity(unit)
        except Exception as exc:
            self.log(event="lineage_error", unit=unit_key(unit["project"], unit["marker"]),
                     error=str(exc)[:200])
            return
        unit["lineage"] = rows
        contaminated = [r for r in rows if r["contaminated"]]
        if contaminated:
            unit["lineage_suspect"] = max(contaminated, key=lambda r: r["at_top"])
            self.log(event="lineage_suspect", unit=unit_key(unit["project"], unit["marker"]),
                     before_look=True, **unit["lineage_suspect"])

    def lineage_by_eye(self, unit) -> bool:
        """A gated population whose brightest cells still sit on an exclusive
        lineage gets one whole-image look (`qc_confirm`) before anything
        closes it: a real stain keeps the gate its looks reached
        (`replan.after_whole_image`), an empty or failed one closes on that
        picture. False -- the review close then stands on that look -- when a
        whole-image look has judged it already, or there is no gate to show."""
        from plexora.plugins.gating.server.autogate import replan

        if unit.get("whole_image") is not None:
            return False
        return replan.check_whole_image(self, unit)

    def _to_panel(self, unit):
        """`cell_panel` for a unit the candidates engine decides (its first
        look, or a look reopened since its panels closed), None to keep the
        classic look: a conditional gate, or a lattice with one candidate."""
        from plexora.plugins.gating.server.autogate import hypotheses

        if not hypotheses.active(self, unit):
            return None
        state = unit.get("hypotheses")
        if state is not None and state.get("status") is not None:
            unit.setdefault("hypotheses_history", []).append(state)
            unit.pop("hypotheses")
        hypotheses.start(self, unit)
        if not hypotheses.active(self, unit):
            return None
        unit["state"] = "awaiting_panel"
        return "cell_panel"

    def settle(self, unit):
        """Advance a unit through every deterministic step it is waiting on."""
        if unit["state"] == "awaiting_regression" and unit.get("path") == "panel":
            from plexora.plugins.gating.server.autogate import hypotheses

            hypotheses.settle(self, unit)
            return
        if unit["state"] == "awaiting_regression":
            result = self.run_regression(unit)
            if "lineage" in result["failed"]:
                if not self.lineage_by_eye(unit):
                    self.close_contaminated(unit)
                return
            if result["ok"] or unit.get("regression_confirmed"):
                self.finalize(unit, method=unit.get("method") or "ai_accepted")
            elif self.purity_capped(unit, result):
                self.finalize(unit, method=unit.get("method") or "ai_accepted")
            elif not self.purity_push(unit, result):
                unit["state"] = "regression_confirm"

    def purity_capped(self, unit, result) -> bool:
        """After the one purity push, purity failing again and nothing else
        caps the gate's confidence at low (`confidence_for`: a failed check
        not confirmed by eye) instead of asking for a whole-image look: on
        lsp11385_2 32 of 44 such looks said "holds", on the PCA benchmark 42
        of 62, and none of the PCA's other answers improved a gate (its
        "too low" moved Ecad away; its "artifact" closed a real CD8a). A
        spatial check failing too still gets the look: edge bands show there."""
        if not unit.get("purity_pushed") or set(result.get("failed") or ()) != {"partners"}:
            return False
        unit["purity_capped"] = True
        self.log(event="purity_capped", unit=unit_key(unit["project"], unit["marker"]),
                 candidate=unit.get("candidate"))
        return True

    def purity_push(self, unit, result) -> bool:
        """A gate whose positives are less pure against a partner than its
        own brightest positives (`regression`'s `partners` check) is too low:
        sent up the lattice once, by code, to the candidates a look then
        judges row by row. Only when nothing but purity (and the spatial
        checks a purity miss drags along) failed; a second failure of purity
        alone caps confidence (`purity_capped`), any other goes to the
        whole-image look. Not a look's direction, so the
        oscillation guard (`transitions._to_t4`) never counts it."""
        failed = set(result.get("failed") or [])
        if "partners" not in failed or not failed <= PURITY_PUSHABLE \
                or unit.get("purity_pushed") or unit.get("condition") \
                or int(self.options["max_tier"]) < 4 or not self.rounds_left(unit):
            return False
        unit["purity_pushed"] = True
        unit.setdefault("code_moves", []).append("up:purity")
        unit["direction"] = "up"
        unit["magnitude"] = None
        unit["span_from"] = unit["candidate"]
        unit["state"] = "awaiting_t4"
        self.log(event="purity_push", unit=unit_key(unit["project"], unit["marker"]),
                 candidate=unit["candidate"],
                 partners=next((c.get("value") for c in result.get("checks") or []
                                if c["name"] == "partners"), None))
        return True

    # -- budget --

    def rounds_left(self, unit) -> bool:
        """Whether the unit may have another round of candidates."""
        limit = ENGINE["t4_rounds"] * (1 + int(unit.get("extensions") or 0))
        return int(unit.get("rounds", 0)) < limit

    def close_at_limit(self, unit, why, because):
        """Stopped short of a conclusion: flagged for manual review, the best
        gate the evidence reached written and tagged `needs_review`."""
        unit.pop("limit_request", None)
        proposed = unit.get("candidate")
        where = "" if proposed is None else f" the best gate reached ({proposed:.4g}) is " \
            "written for manual review;"
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
        refs, panel, grades = self._references_with_basis(unit)
        unit["reference_gates"] = refs
        self.plan_evidence(unit, panel=panel, grades=grades)
        return unit["reference_gates"]

    def _references(self, unit):
        """`references_ready` without storing them or planning evidence (what
        `ledger_fingerprint` hashes, so a packet out in parallel is stale
        exactly when its references would change)."""
        return self._references_with_basis(unit)[0]

    def _references_with_basis(self, unit):
        """(references, panel, grades): the ledger's graded gates of the image,
        failures excluded, as `context.references_for` picks among them."""
        from plexora.plugins.gating.server.autogate import context

        panel = self.panel_for(unit["project"])
        book = self.ledger(unit["project"], without=unit)
        grades = {m: e["grade"] for m, e in book.items()}
        refs = context.references_for(panel, unit["marker"], grades)
        return ([{"marker": r["marker"], "relation": r["relation"], "gate": book[r["marker"]]["gate"],
                  "confidence": r["grade"], "weight": r["weight"], "derived": r["derived"]}
                 for r in refs if book[r["marker"]]["gate"] is not None], panel, grades)

    def task_of(self, kind, units):
        """A look at candidates that is a classifier check is its own task,
        `gating.classifier_check` (`tasks.yaml`), so its model is set apart
        from the candidate look's."""
        from plexora.ai import tasks
        from plexora.plugins.gating.server.autogate import cellcalls

        if kind == "t4_candidates" and any(cellcalls.pending(u) for u in units or ()):
            return tasks.task_for(self.MODULE, kind, check="classifier")
        return super().task_of(kind, units)

    def panel_for(self, project):
        """The image's panel context with the session's biology laid over it
        (`biology.overlay`: the tissue's and disease's relations, for this
        session only)."""
        from plexora.plugins.gating.server.autogate import biology, context

        return biology.overlay(context.for_project(_data(self.call, project)),
                               self.record.get("biology"))

    def ledger(self, project, *, without=None):
        """{marker: {grade, why, state, gate}}: what each gate of the image
        has earned as evidence (`hierarchy.grade`). Failures stay here for
        the whole run, so no later marker leans on them."""
        from plexora.plugins.gating.server.autogate import hierarchy

        return hierarchy.ledger([u for u in self.units_of(project) if u is not without])

    def plan_evidence(self, unit, *, panel=None, grades=None):
        """Store on the unit its place in the hierarchy and the evidence it
        may lean on (`hierarchy.plan`): what its packets say and what the
        look's channels are chosen from."""
        from plexora.plugins.gating.server.autogate import context, hierarchy

        if panel is None:
            panel = self.panel_for(unit["project"])
        if grades is None:
            grades = {m: e["grade"] for m, e in
                      self.ledger(unit["project"], without=unit).items()}
        unit["evidence_plan"] = hierarchy.plan(panel, hierarchy.build(panel), unit["marker"],
                                               grades)
        return unit["evidence_plan"]

    def evidence_gates(self, unit):
        """{marker: (gate, grade)}: every marker of the unit's image this run
        decided, graded by `hierarchy.grade` -- failed ones included, so the
        selection can say it avoided them. The scoring's partners, wider than
        the two references a look plots."""
        return {m: (e["gate"], e["grade"])
                for m, e in self.ledger(unit["project"], without=unit).items()
                if e["grade"] != "pending"}

    def propose(self, unit, *, rescore=False):
        """Score the unit's candidate thresholds (`scoring.score`) before its
        first look, and start from the proposal: the cells every later look is
        shown are chosen around the start, so a start in the background (a
        mixture that split off noise) is one no look can recover from. The
        Auto gate stays on the unit (`gmm`) and among the candidates.

        Once per unit; `rescore` again (a look said the marker is continuous,
        so it is scored as one). Never moves a conditional gate."""
        from plexora.plugins.gating.server.autogate import context, scoring

        if unit.get("condition") or unit.get("gmm") is None:
            return None
        if unit.get("scoring") is not None and not rescore:
            return unit["scoring"]
        ds = _data(self.call, unit["project"])
        panel = self.panel_for(unit["project"])
        partners = scoring.evidence_partners(panel, unit["marker"], self.evidence_gates(unit))
        try:
            result = scoring.score(ds, unit["marker"], gmm=unit["gmm"],
                                   separation_d=(unit.get("metrics") or {}).get("d"),
                                   partners=partners, continuous=bool(unit.get("continuous")))
        except Exception as exc:  # the look still has the Auto gate to start from
            unit["scoring"] = {"version": scoring.VERSION, "error": str(exc)[:200],
                               "candidates": []}
            return unit["scoring"]
        unit["scoring"] = compact_scoring(result, partners)
        self.plan_evidence(unit, panel=panel)
        proposal = result.get("proposal")
        unit.pop("lattice", None)
        if proposal is None:
            unit["start"] = {"source": "gmm", "low": unit["gmm"]}
            return unit["scoring"]
        if unit.get("judged") or unit.get("judged_rows"):
            # A rescore after a look (the marker turned out continuous) never
            # crosses a row a look judged (`judged_limits`).
            proposal = self.clamp_to_judged(unit, ds, float(proposal), why="rescore")
        unit["start"] = {"source": "scoring", "low": float(proposal),
                         "basis": result.get("proposal_source"),
                         "leading": result.get("leading")}
        unit["candidate"] = float(proposal)
        unit["source"] = "scoring"
        self.log(event="scored", unit=unit_key(unit["project"], unit["marker"]),
                 proposal=float(proposal), gmm=unit["gmm"],
                 basis=result.get("proposal_source"), leading=result.get("leading"))
        return unit["scoring"]

    def clamp_to_judged(self, unit, ds, low, *, why):
        """`low` clipped so it crosses no row a look judged -- a first look's
        rows (`judged_rows`) or a look at candidates' intervals (`judged`):
        one called `own_stain` keeps a gate below it, one called `unstained`
        or `neighbour_only` above it (`judged_limits`, relative to the
        unit's current candidate) -- and the clip logged. The rule for every
        refit code makes after a look (a conditional refit, a rescore)."""
        judged = list(unit.get("judged") or []) + list(unit.get("judged_rows") or [])
        if not judged or unit.get("candidate") is None:
            return low
        col = profmod.column(ds, unit["marker"])
        lo, hi = judged_limits({**unit, "judged": judged}, col)
        fit = float(col.to_fit(low))
        clipped = min(max(fit, lo), hi)
        if clipped == fit:
            return low
        out = float(col.from_fit(clipped))
        unit.setdefault("clamps", []).append({"why": why, "from": float(low), "to": out})
        self.log(event="clamped", unit=unit_key(unit["project"], unit["marker"]), why=why,
                 proposed=float(low), to=out)
        return out

    def defer_for_children(self, unit) -> bool:
        """Whether a unit waits, before its first look, for its children.

        The hierarchy runs both ways: a parent whose own evidence is weak
        (no robust partner curve, a poorly separated mixture) and whose
        children (markers whose positives are a subset of its own) are still
        open is set aside until they close, then re-scored with them -- their
        positives say where its own begin (`scoring`, `children`). Once per
        unit, never after a look, and never on a child that waits on the
        user, so it cannot stall the run."""
        from plexora.plugins.gating.server.autogate import profile as profmod

        deferred = unit.get("deferred")
        if deferred == "resumed" or (unit.get("used") or {}).get("packets"):
            return False
        if deferred is None:
            scoring_ = unit.get("scoring") or {}
            weak = scoring_.get("basis") != "coexpression" and \
                ((unit.get("metrics") or {}).get("d") or 0.0) < profmod.THRESHOLDS["weak_d"]
            if not weak or not self.options["defer_parents"]:
                unit["deferred"] = "resumed"
                return False
        children = set((unit.get("evidence_plan") or {}).get("children") or [])
        waiting = {u["marker"] for u in self.waiting_for_user()}
        open_ = [u["marker"] for u in self.units_of(unit["project"])
                 if u["marker"] in children and u["marker"] not in waiting
                 and (u["state"] in ASKS or u["state"] in ("pending", "awaiting_regression"))]
        if open_ and deferred is None:
            unit["deferred"] = "waiting"
            self.log(event="deferred", unit=unit_key(unit["project"], unit["marker"]),
                     waiting_for=sorted(open_))
            return True
        if open_:
            return True
        if deferred == "waiting":
            unit["deferred"] = "resumed"
            self.propose(unit, rescore=True)
            self.log(event="resumed", unit=unit_key(unit["project"], unit["marker"]),
                     proposal=(unit.get("start") or {}).get("low"))
            return False
        unit["deferred"] = "resumed"
        return False

    def reference_failed(self, unit):
        """A unit closed with a gate no one may lean on (`hierarchy.grade`
        failed): every open unit of its image that has not been looked at yet
        and was scored against it is re-scored without it; one already past
        its first look or closed records that it stood on a failed gate."""
        from plexora.plugins.gating.server.autogate import hierarchy

        grade_, _why = hierarchy.grade(unit)
        if grade_ != "failed":
            return
        marker = unit["marker"]
        for other in self.units_of(unit["project"]):
            if other is unit:
                continue
            leaned = {p["marker"] for p in (other.get("scoring") or {}).get("partners") or []}
            leaned |= {r["marker"] for r in other.get("reference_gates") or []}
            if marker not in leaned:
                continue
            key = unit_key(other["project"], other["marker"])
            if other["state"] in ASKS and not (other.get("used") or {}).get("packets"):
                other.pop("reference_gates", None)
                self.propose(other, rescore=True)
                self.log(event="rescored", unit=key, because=f"{marker} failed")
            else:
                flagged = other.setdefault("stood_on_failed", [])
                if marker not in flagged:
                    flagged.append(marker)
                self.log(event="reference_failed", unit=key, reference=marker)

    def ledger_fingerprint(self, units) -> str:
        """The partner gates the units' packet stands on -- each unit's
        references as `references_ready` gives them now, and the gate a
        conditional unit is gated within -- as a short hash; "" when none. A
        change means the packet's numbers and pictures describe gates that
        no longer hold."""
        rows = []
        for unit in units:
            if unit.get("project") is None or unit.get("marker") is None:
                continue
            for ref in self._references(unit):
                rows.append([unit["project"], unit["marker"], ref["marker"], ref["relation"],
                             ref["gate"], ref["confidence"]])
            condition = unit.get("condition") or {}
            if condition.get("within"):
                rows.append([unit["project"], unit["marker"], "within", condition["within"],
                             condition.get("partner_gate")])
        if not rows:
            return ""
        blob = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]

    # -- readiness: several packets out at once --

    def _links(self, project):
        """{marker: markers it could be judged beside, or that could be judged
        beside it}: every partner `context.references_for` might pick, either
        way round, among this session's markers of `project`."""
        from plexora.plugins.gating.server.autogate import context

        from plexora.plugins.gating.server.autogate import hierarchy

        panel = self.panel_for(project)
        entries = panel.get("entries") or {}
        markers = {u["marker"] for u in self.units_of(project)}
        links = {m: set() for m in markers}
        for marker in markers:
            for partner in (entries.get(marker) or {}).get("partners") or []:
                ref = partner.get("marker")
                if ref not in markers or ref == marker:
                    continue
                if context.CONFIDENCE_RANK.get(partner.get("confidence"), 0) < 1:
                    continue
                if (entries.get(ref) or {}).get("role") in ("state", "signalling"):
                    continue
                links[marker].add(ref)
                links[ref].add(marker)
            # The exclusions the tree derives (a subset inherits its parents':
            # CD20 within CD45 excludes what CD45 excludes): the lineage check
            # needs those partners' gates, so a look waits for them too
            # (`lineage_screen`).
            for rel in hierarchy.relations(panel, marker):
                ref = rel["marker"]
                if rel["relation"] != "exclusive" or not rel["derived"] or ref not in markers \
                        or context.CONFIDENCE_RANK.get(rel["confidence"], 0) < 1 \
                        or (entries.get(ref) or {}).get("role") in ("state", "signalling"):
                    continue
                links[marker].add(ref)
                links[ref].add(marker)
        return links

    def depends_on(self, unit, links=None):
        """The units `unit` waits for before a look of it may go out: its
        linked partners earlier in gating order (`_links`), and the partner a
        conditional unit is gated within. Later partners wait for it instead,
        so two packets out at once never stand on each other."""
        project = unit["project"]
        links = self._links(project) if links is None else links
        rank = {m: i for i, m in enumerate(self.record["order"])}
        mine = rank.get(unit["marker"], 0)
        wanted = {m for m in links.get(unit["marker"], ()) if rank.get(m, 0) < mine}
        within = (unit.get("condition") or {}).get("within")
        if within:
            wanted.add(within)
        return [self.record["units"][unit_key(project, m)] for m in sorted(wanted)
                if unit_key(project, m) in self.record["units"]]

    def _settled(self, unit, links):
        """Whether every unit `unit` depends on is terminal (or held on a
        limit question, as the serial order passes it by too)."""
        return all(other["state"] in TERMINAL or other.get("limit_request")
                   for other in self.depends_on(unit, links))

    def ready_units(self):
        """(decisions, why): the decisions that may go out now beside the
        packets already out, in gating order, as [(kind, [units])] -- and,
        when that is empty, why: `busy` (what is left waits on an outstanding
        packet), `wait` (on the bulk pass), `wait_user`, or None (nothing
        left). The serial order's rules (`next_unit`) hold within it: set-up
        first, nothing past a unit the bulk pass has not reached, the image's
        strip before the next image."""
        record = self.record
        held = record["outstanding"]
        busy = {key for entry in held.values() for key in entry.get("units") or ()}
        if any(entry.get("kind") in self.EXCLUSIVE_KINDS for entry in held.values()):
            return [], "busy"
        display_due, display_waiting = self.display_due()
        for kind, pending in (
                ("expression_setup", (record.get("expression") or {}).get("status") == "pending"),
                ("pixel_setup", (record.get("pixel") or {}).get("status") == "pending"),
                ("panel_context", bool(record.get("panel_pending"))),
                ("display_setup", bool(display_due)),
                ("replan", self._replan_due())):
            if pending:
                return ([], "busy") if held else ([(kind, [])], None)
        bulk_running = record.get("state") == "bulk_running"
        picks = []

        def blocked(why):
            if picks:
                return picks, None
            return [], "busy" if held else why

        for project in record["images"]:
            if project in display_waiting:
                # Its looks are drawn with the brightness its check settles.
                return blocked("wait")
            units = self.units_of(project)
            if project != record.get("reference_image") and any(
                    u["state"] == "transfer_pending" for u in units):
                from plexora.plugins.gating.server.autogate import transfer

                if not transfer.reference_done(self):
                    open_ref = any(u["state"] not in TERMINAL
                                   for u in self.units_of(record["reference_image"]))
                    return blocked("wait" if bulk_running or open_ref else None)
                transfer.decide_image(self, project)
            links = self._links(project)
            strip, due = [], False
            for unit in units:
                if unit["state"] in TERMINAL or self.key_of(unit) in busy:
                    continue
                if unit["state"] == "pending":
                    if bulk_running:
                        due = True
                        break
                    self.close(unit, "manual_review_recommended",
                               "the deterministic pass did not reach this marker",
                               unlooked="not_profiled")
                    continue
                if unit["state"] != "accepted_t1" and not self._settled(unit, links):
                    continue
                kind = self._decision(unit)
                if kind:
                    picks.append((kind, [unit]))
                    continue
                if unit["state"] == "accepted_t1":
                    strip.append(unit)
                    if len(strip) >= ENGINE["strip_batch"]:
                        due = True
                        break
            if strip:
                # The strip goes out alone, once what is out is answered, and
                # before this image's later markers or the next image.
                if picks:
                    return picks, None
                return ([], "busy") if held else ([("t1_strip", strip)], None)
            if due:
                return blocked("wait")
        if picks:
            return picks, None
        if held:
            return [], "busy"
        if self.waiting_for_user():
            return [], "wait_user"
        return [], None

    # -- the contrast check (`display_setup`) --

    def display_due(self):
        """(due, waiting): images whose contrast check may go out now (their
        calibration is stored, or the bulk pass is not running to store it),
        and images whose check waits on the bulk pass's calibration. Both
        empty when the session does not check (`display.status` not pending)."""
        display = self.record.get("display") or {}
        if display.get("status") != "pending":
            return [], []
        projects = display.get("projects") or {}
        calibrated = self.record.get("calibration") or {}
        bulk_running = self.record.get("state") == "bulk_running"
        due, waiting = [], []
        for project in self.record["images"]:
            if (projects.get(project) or {}).get("status") != "pending":
                continue
            if project in calibrated or not bulk_running:
                due.append(project)
            else:
                waiting.append(project)
        return due, waiting

    def settle_display(self):
        """The check's overall status from its images': applied once none is
        pending (`unavailable` when every image was)."""
        display = self.record.get("display") or {}
        if display.get("status") != "pending":
            return
        states = [e.get("status") for e in (display.get("projects") or {}).values()]
        if "pending" in states:
            self.close_display_units()
            return
        display["status"] = "unavailable" if states and all(
            s == "unavailable" for s in states) else "applied"
        self.close_display_units()

    def close_display_units(self):
        """An auto-contrast run's markers (`purpose: contrast`) close
        `display_set` once their image's brightness is settled -- by the check,
        by the rule, or with no check possible: nothing else is decided for
        them. Nothing in a gating session."""
        if self.options["purpose"] != "contrast":
            return
        projects = (self.record.get("display") or {}).get("projects") or {}
        for project in self.record["images"]:
            entry = projects.get(project) or {}
            status = entry.get("status")
            if status in (None, "pending"):
                continue
            reason = (f"no contrast check: {entry.get('reason') or 'unavailable'}"
                      if status == "unavailable" else "brightness settled")
            for unit in self.units_of(project):
                if unit["state"] not in TERMINAL:
                    self.close(unit, "display_set", reason)

    def struck_out(self, packet):
        """A contrast check answered wrongly twice keeps the rule's windows
        and moves on (`transitions.strike_display`)."""
        if packet.get("kind") == "display_setup":
            from plexora.plugins.gating.server.autogate import transitions

            transitions.strike_display(self, packet)

    def _replan_due(self) -> bool:
        """Whether a `replan` packet waits to go out (`replan.pending`): not
        while the bulk pass runs -- a failure then is a profile's, and the
        markers it would reorder are not profiled yet."""
        from plexora.plugins.gating.server.autogate import replan

        return replan.pending(self.record) and replan.enabled(self) \
            and self.record.get("state") != "bulk_running"

    def next_ready(self, reader):
        """The next decision for `reader` beside the packets already out:
        the marker it answered last while that one still needs a look (its
        looks follow on, as in `next_unit`), else the first of
        `ready_units` -- of those, only what the reader is scoped to
        (`accepts`): a reader of other tasks never holds back this one's."""
        held = self.record["outstanding"]
        last = self.last_unit(reader)
        if last is not None and not any(e.get("kind") in self.EXCLUSIVE_KINDS
                                        for e in held.values()) \
                and self.key_of(last) not in {k for e in held.values() for k in e["units"]} \
                and (last["state"] in ASKS or last["state"] == "awaiting_regression") \
                and self._settled(last, self._links(last["project"])):
            kind = self._decision(last)
            if kind and self.accepts(kind, [last]):
                return kind, [last]
        picks, why = self.ready_units()
        mine = [pick for pick in picks if self.accepts(*pick)]
        if mine:
            return mine[0]
        if picks:
            # Ready, but all another task's: nothing for this reader now.
            return (None, []) if not held else ("busy", [])
        if why is None and not held:
            # Nothing ready and nothing out: the serial order decides (it
            # never waits on a unit no packet will settle).
            return self.next_unit()
        if why == "wait_user":
            return "wait_user", self.waiting_for_user()
        return why, []

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
        # The absence check is a marker's first look when it has one: the same
        # scored start and wait for children come first. The lineage screen
        # waits for the cell look: whether any cell carries the stain comes
        # before whose cells the brightest are (LSP11761 PD1, absent to the
        # person, was closed for review by the screen and never shown).
        first = kind in ("t2_confirm", "absence_check", "cell_panel")
        if first and unit.get("scoring") is None:
            self.propose(unit)
        if kind == "t2_confirm" and schemas.engine_name(self.options["engine"]) == "v2":
            kind = self._to_panel(unit) or kind
        if kind in ("t2_confirm", "cell_panel"):
            self.lineage_screen(unit)
        if first and self.defer_for_children(unit):
            return None
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
        display_due, display_waiting = self.display_due()
        if display_due:
            return "display_setup", []
        if self._replan_due():
            return "replan", []
        # Stay on the marker being refined: its next look follows its last,
        # so the evidence (and a mirrored viewer) does not jump between markers.
        last = self.last_unit()
        if last is not None and (last["state"] in ASKS
                                 or last["state"] == "awaiting_regression"):
            kind = self._decision(last)
            if kind:
                return kind, [last]
        bulk_running = record.get("state") == "bulk_running"
        for project in record["images"]:
            if project in display_waiting:
                return "wait", []
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
                               "the deterministic pass did not reach this marker",
                               unlooked="not_profiled")
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
                # The bulk pass's last marker (`bulk._progress_announcer`):
                # what core's agent panel shows while the pass runs.
                "bulk": {"job_id": self.record.get("bulk_job_id"),
                         "state": self.record.get("state"),
                         **(self.record.get("bulk_progress") or {})}}

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
            self.close(unit, "insufficient_information", f"needs the user: {ask_user.question}",
                       unlooked="needs_user")
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
    if kind in schemas.SETUP_KINDS or kind in schemas.PLAN_KINDS or record.get("panel_pending") \
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
