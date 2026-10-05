"""The QC session's state machine: every transition decided by code.

A session holds units of five types for its image:

- **channel**: one per image channel. The bulk pass scans it; the agent audits
  it on a sheet with the others (`channel_audit`); it closes clean, flagged or
  failed once its candidates are settled.
- **candidate**: one per region a detector proposed (or the agent pointed
  at). It waits for its channel's audit, then is confirmed (`artifact_confirm`,
  up to three looks), scoped across channels (`artifact_scope`), localised
  (`artifact_localize`, then `artifact_grid`) and decided: confirmed with an
  action (exclude, warn, noted), dismissed, or sent to manual review. First
  looks go up to `confirm_batch` to a packet (`_confirm_batch`).
- **check**: one per image check and channel (Blur QC per nuclear channel,
  the Registration Check per comparison, Segmentation QC once). The bulk
  pass scores it; once the audit is done the agent judges places sampled
  across its score (`score_review`, up to `score_rounds` looks as the bar
  moves), and its regions are decided, confirmed or dropped
  (`transitions.apply_score_review`) -- before the detector candidates, so a
  check's region takes in the same place a detector also found.
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
#: The order the image checks' score reviews are served in.
REVIEW_ORDER = ("registration", "blur", "segmentation", "artifacts")


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
    from plexora.plugins.qc.server import checks_bulk, detectors, score_fields, sheets

    return (schemas.SCAN_VERSION, schemas.CELLS_VERSION, calibration.VERSION, sheets.RENDERER,
            f"checks:{checks_bulk.VERSION}", f"score_fields:{score_fields.VERSION}",
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
    MODULE = "qc"
    UNIT_DEFAULT = schemas.QC_UNIT_DEFAULT

    def __init__(self, call, session_id, *, st=None):
        super().__init__(call, session_id, st=st or store())
        self._scans = {}
        self._reviewing = False

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
        if unit["type"] == "check":
            return f"{unit.get('check')} check on {unit.get('channel') or 'the mask'}"
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

    def trim(self, packet):
        from plexora.plugins.qc.server import packets

        return packets.trim(packet)

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
        # A candidate the shared harness sends to review (evidence it could
        # not draw, answers it could not read) becomes a warning region like
        # any other: evidence never vanishes.
        if unit["type"] == "candidate" and state == "manual_review_recommended" \
                and not self._reviewing:
            self.manual_review(unit, reason)
            return
        unit["state"] = state
        unit["reason"] = reason
        unit.pop("limit_request", None)
        self.log(event="closed", unit=self.key_of(unit), state=state, reason=reason)
        if unit["type"] == "channel" and state in TERMINAL:
            # A channel closed without its audit (the sheet could not be
            # drawn, the answers not read): its candidates get a look each.
            for candidate in self.units_of("candidate", unit["project"]):
                if unit["id"] in cand.audit_channels(candidate) and \
                        candidate["state"] == "awaiting_audit":
                    candidate["state"] = "awaiting_confirm"
        if unit["type"] == "candidate" and state in ("dismissed", "merged") \
                and unit.get("roi_id"):
            # A region written before (and reopened by the final review) that
            # is no longer an artifact stops excluding: it is noted, not removed.
            unit["action"] = "ignore"
            self.write_candidate(unit, klass=unit.get("class") or unit.get("class_hint")
                                 or "other_technical", action="ignore")

    def manual_review(self, unit, reason):
        """Stopped short of a conclusion: a candidate becomes a warning region
        of class `uncertain_manual_review` (never an exclusion; only noted
        when it is a large share of the tissue, `strictness.manual_review_action`),
        so evidence never vanishes because a budget ran out."""
        if unit["type"] != "candidate":
            self.close(unit, "manual_review_recommended", reason)
            return
        decision = dict(unit.get("decision") or {})
        decision.update(manual_review=True, artifact_class="uncertain_manual_review")
        unit["decision"] = decision
        action = strictness.manual_review_action(unit.get("measurement"))["action"]
        unit["action"] = action
        self._reviewing = True
        try:
            self.close(unit, "manual_review_recommended", reason)
        finally:
            self._reviewing = False
        self.write_candidate(unit, klass="uncertain_manual_review", action=action)

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
        # A class is kept only when the evidence it needs exists (class_rules).
        from plexora.plugins.qc.server import class_rules

        record = self.call.session.project(unit["project"])
        klass, adjusted = class_rules.supported_class(
            klass, channels=unit.get("channels") or [],
            image_channels=[c.get("fullname") or c.get("name")
                            for c in record.image.real_channels],
            hint=unit.get("class_hint"))
        if adjusted:
            decision["class_adjusted"] = adjusted
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
        # The pixels are traced before the action is derived: how much tissue
        # a region removes is the trace's area, not its envelope's.
        self.refine_unit(unit)
        unit.setdefault("measurement", {})["support"] = strictness.measured_support(unit)
        action = strictness.decide_artifact(decision, unit.get("measurement"),
                                            self.table())["action"]
        unit["action"] = action
        state = {"exclude": "confirmed_exclude", "warn": "confirmed_warn",
                 "ignore": "confirmed_noted"}[action]
        self.close(unit, state, f"confirmed {schemas.CLASS_WORDS.get(klass, klass)}; "
                                f"{action}")
        self.write_candidate(unit, klass=klass, action=action)

    def _explained_by(self, unit):
        """A region already excluded that explains this candidate before its
        first look: it lies `merge_contain` inside it, the region's class
        explains a place no later than the candidate's would
        (`consolidate.rank`), and the region covers the candidate's channels
        (or every cell, a whole-cell class). Its cells are excluded whatever
        the look would say, so no packet is spent on it. None otherwise."""
        from plexora.plugins.qc.server import consolidate

        mask = self.mask_of(unit)
        mine = mask.sum()
        if not mine:
            return None
        own = consolidate.rank(unit.get("class_hint"))
        channels = set(unit.get("channels") or ())
        for other in self.units_of("candidate", unit["project"]):
            if other is unit or other["state"] != "confirmed_exclude":
                continue
            klass = other.get("class") or other.get("class_hint")
            if consolidate.rank(klass) > own:
                continue
            if klass not in schemas.WHOLE_CELL_CLASSES \
                    and not channels <= set(other.get("channels") or ()):
                continue
            if np.logical_and(mask, self.mask_of(other)).sum() / mine >= ENGINE["merge_contain"]:
                return other
        return None

    def _found_by_check(self, unit):
        """A region an image check confirmed in the same category on the
        channel the audit called suspicious `elsewhere`: the audit's question
        is answered (the audit sheet is drawn before the checks' regions
        exist). None otherwise -- the grid question stands."""
        category = schemas.category_of_class(unit.get("class_hint"))
        channel = unit.get("audit_channel") or unit.get("channel")
        for other in self.units_of("candidate", unit["project"]):
            if other is unit or other.get("origin") != "check" \
                    or other["state"] not in schemas.CONFIRMED_STATES:
                continue
            klass = other.get("class") or other.get("class_hint")
            if schemas.category_of_class(klass) != category:
                continue
            channels = other.get("channels") or []
            if not channels or channel in channels:
                return other
        return None

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
            mine = mask.sum()
            # The same artifact seen from another channel or detector: the
            # same place (IoU), or this one lying almost wholly inside the
            # one already written. Never the other way round: a whole fold
            # decided after a fragment of it found by another detector is
            # written, and the fragment is consolidated into it at the end
            # (`consolidate`) -- merged away, the fold's outline was lost.
            if union and (inter / union >= ENGINE["merge_iou"]
                          or (mine and inter / mine >= ENGINE["merge_contain"])):
                return other
        return None

    def envelope_of(self, unit):
        """(GeoJSON, map mask) of the outline the agent judged: the squares it
        named, else the variant it chose (standard unless it chose), else the
        candidate's cells with the variant's margin. Where to trace, and what
        is written when there is nothing to trace."""
        from plexora.plugins.qc.server import polygons

        scan = self.scan(unit["project"])
        mask = self.mask_of(unit)
        chosen = unit.get("variant") or "standard"
        dilate = {"tight": 0, "standard": 1, "generous": 2}.get(chosen)
        closing = 1 if chosen == "generous" else 0
        if unit.get("envelope_geometry"):
            geometry = unit["envelope_geometry"]
            return geometry, polygons.geometry_to_grid(geometry, scan.grid, touch=True)
        variants = unit.get("variants") or {}
        if chosen in variants:
            geometry = variants[chosen]["geometry"]
            if dilate is not None:
                return geometry, polygons._close(polygons._dilate(mask, dilate), closing)
            return geometry, polygons.geometry_to_grid(geometry, scan.grid, touch=True)
        grown = polygons._close(polygons._dilate(mask, 1 if dilate is None else dilate),
                                closing)
        return polygons.mask_to_geometry(grown, scan.grid), grown

    def refine_unit(self, unit):
        """Trace the artifact inside its envelope and make that the outline
        written (`unit["geometry"]`), with the trace's record in
        `unit["refinement"]`. Never raises: a trace that fails, or finds
        nothing the guards trust, leaves the envelope as the outline."""
        from plexora.plugins.qc.server import polygons, refine

        envelope, envelope_mask = self.envelope_of(unit)
        unit["envelope_geometry"] = envelope
        measurement = unit.setdefault("measurement", {})
        if envelope is None:
            return None
        if unit.get("trace") == "map" and unit.get("detector") == "registration" \
                and not unit.get("whole_tissue") and self.options.get("refine", True):
            traced = self._nuclei_outline(unit, envelope)
            if traced is not None:
                return traced
        if unit.get("trace") == "object" and self.options.get("refine", True):
            traced = self._object_outline(unit, envelope, envelope_mask)
            if traced is not None:
                return traced
        if unit.get("trace") in ("map", "none", "object"):
            return self._map_outline(unit, envelope)
        margin = self.options.get("refine_margin_um")
        scan = self.scan(unit["project"])
        from plexora.plugins.qc.server import refine_sam

        key = [scan.meta.get("fingerprint"), polygons.geometry_hash(envelope),
               refine.VERSION, margin, (unit.get("decision") or {}).get("artifact_class")]
        # The segmentation model, when it can trace: a key of its own, so a
        # session that gains (or loses) the model retraces.
        version = refine_sam.model_version()
        if version is not None:
            key.append(f"sam:{version}")
        held = unit.get("refinement") or {}
        if held.get("key") == key and unit.get("geometry"):
            return held
        if not self.options.get("refine", True):
            record = {"status": "not_applicable", "reason": "tracing is off for this session",
                      "method": None, "kept_fraction": 1.0}
            geometry = envelope
        else:
            try:
                geometry, record = self._trace(unit, envelope, envelope_mask, margin)
            except Exception as exc:  # noqa: BLE001 -- the envelope is always a safe outline
                self.log(event="refine_failed", unit=self.key_of(unit), error=str(exc))
                geometry = envelope
                record = {"status": "fallback", "reason": f"tracing failed: {exc}",
                          "method": refine.method_for(unit.get("class")), "kept_fraction": 1.0}
        record.update(key=key, margin_um=margin)
        unit["geometry"] = geometry
        unit["refinement"] = record
        tissue_px = float((scan.meta.get("tissue") or {}).get("area_px") or 0.0)
        fraction = measurement.get("tissue_fraction")
        if record.get("status") == "refined" and tissue_px > 0:
            measurement["refined_fraction"] = min(1.0, polygons.area_of(geometry) / tissue_px)
        else:
            measurement["refined_fraction"] = fraction
        return record

    def _nuclei_outline(self, unit, envelope):
        """A registration or one-cycle region outlined by the reference
        nuclei it holds (`nuclei_trace`); None keeps the map outline, with
        why noted on the unit."""
        from plexora.plugins.qc.server import nuclei_trace, polygons

        scan = self.scan(unit["project"])
        key = ["nuclei", nuclei_trace.VERSION, scan.meta.get("fingerprint"),
               polygons.geometry_hash(envelope)]
        held = unit.get("refinement") or {}
        if held.get("key") == key and unit.get("geometry"):
            return held
        pixel = self.pixel_for(unit["project"])
        try:
            from plexora.server.utils import source_image

            with source_image.SHELF.reader(self.call.session.image_data(unit["project"])) \
                    as source:
                geometry, record = nuclei_trace.trace(
                    self.call.session, unit["project"], unit, envelope, scan, source,
                    pixel_um=float(pixel["value"]) if pixel else None)
        except Exception as exc:  # noqa: BLE001 -- the map outline is always safe
            self.log(event="nuclei_trace_failed", unit=self.key_of(unit), error=str(exc))
            unit["nuclei_trace"] = {"status": "fallback", "reason": f"failed: {exc}"}
            return None
        if geometry is None:
            unit["nuclei_trace"] = record
            return None
        record["key"] = key
        unit["geometry"] = geometry
        unit["refinement"] = record
        measurement = unit.setdefault("measurement", {})
        tissue_px = float((scan.meta.get("tissue") or {}).get("area_px") or 0.0)
        if tissue_px > 0:
            measurement["refined_fraction"] = min(1.0, polygons.area_of(geometry) / tissue_px)
        return record

    def _object_outline(self, unit, envelope, envelope_mask):
        """An Artifact Detector region outlined by the segmentation model,
        when it is there, traces the class, and its outline passes the guards
        (`refine_sam.refine_object`); None keeps the detector's own outline,
        with why noted on the unit."""
        from plexora.plugins.qc.server import polygons, refine_sam

        version = refine_sam.model_version()
        if version is None:
            return None
        scan = self.scan(unit["project"])
        key = ["object", f"sam:{version}", scan.meta.get("fingerprint"),
               polygons.geometry_hash(envelope),
               (unit.get("decision") or {}).get("artifact_class")]
        held = unit.get("refinement") or {}
        if held.get("key") == key and unit.get("geometry"):
            return held
        pixel = self.pixel_for(unit["project"])
        try:
            result = refine_sam.refine_object(
                unit, envelope_mask, scan, self.call.session.image_data(unit["project"]),
                outline=envelope, pixel_um=float(pixel["value"]) if pixel else None)
        except Exception as exc:  # noqa: BLE001 -- the detector's outline is always safe
            self.log(event="object_sam_failed", unit=self.key_of(unit), error=str(exc))
            unit["sam_trace"] = {"status": "failed", "reason": str(exc)[:200]}
            return None
        if result.method != "sam":
            unit["sam_trace"] = result.params.get("sam") or {
                "status": "not_applicable", "reason": "the model does not trace this class"}
            return None
        record = result.to_record()
        record["key"] = key
        unit["geometry"] = result.geometry
        unit["refinement"] = record
        measurement = unit.setdefault("measurement", {})
        tissue_px = float((scan.meta.get("tissue") or {}).get("area_px") or 0.0)
        if tissue_px > 0:
            measurement["refined_fraction"] = min(1.0, polygons.area_of(result.geometry)
                                                  / tissue_px)
        return record

    def _map_outline(self, unit, envelope):
        """A check region whose outline is its score map's (`map`: nothing
        in the pixels to trace -- a misregistration, a cluster of badly
        segmented cells) or the tissue (`none`: the whole tissue): written
        as it is, and said so."""
        from plexora.plugins.qc.server import polygons, refine

        measurement = unit.setdefault("measurement", {})
        klass = (unit.get("decision") or {}).get("artifact_class") or unit.get("class_hint")
        if unit.get("trace") == "none":
            record = {"status": "not_applicable", "method": None, "kept_fraction": 1.0,
                      "reason": "the whole tissue: its outline is the region"}
        elif unit.get("trace") == "object":
            from plexora.plugins.qc.capabilities_checks import OBJECT_REFINEMENT

            record = dict(OBJECT_REFINEMENT)
        else:
            record = {"status": "map", "method": refine.MAP_METHODS.get(klass, "score_map"),
                      "kept_fraction": 1.0, "refine_um": unit.get("cell_um"),
                      "reason": "the check's score map is the outline"}
        record["key"] = [unit.get("trace"), polygons.geometry_hash(envelope)]
        unit["geometry"] = envelope
        unit["refinement"] = record
        scan = self.scan(unit["project"])
        tissue_px = float((scan.meta.get("tissue") or {}).get("area_px") or 0.0)
        if record["status"] in ("map", "detector") and tissue_px > 0:
            measurement["refined_fraction"] = min(1.0, polygons.area_of(envelope) / tissue_px)
        else:
            measurement["refined_fraction"] = measurement.get("tissue_fraction")
        return record

    def _trace(self, unit, envelope, envelope_mask, margin):
        """(geometry, record) of one trace: the localize packet's own trace
        when it was drawn for this envelope, else the pixels read now."""
        from plexora.plugins.qc.server import polygons

        held = unit.get("localize_trace") or {}
        if held.get("geometry") and held.get("status") == "refined" \
                and held.get("margin_um") == margin \
                and held.get("class") == (unit.get("decision") or {}).get("artifact_class"):
            clipped = polygons.clip_to(held["geometry"], envelope)
            if clipped is not None:
                record = {k: v for k, v in held.items() if k != "geometry"}
                area = polygons.area_of(clipped)
                envelope_area = polygons.area_of(envelope)
                record.update(clipped_from="bbox", area_px2=area, envelope_area_px2=envelope_area,
                              kept_fraction=area / envelope_area if envelope_area else 1.0)
                return clipped, record
        from plexora.plugins.qc.server import refine_sam

        scan = self.scan(unit["project"])
        pixel = self.pixel_for(unit["project"])
        options = {"margin_um": margin} if margin is not None else {}
        # Classical trace and (for a physical artifact, when the model is
        # there) the model's: read inside the reader lock, inferred outside.
        result = refine_sam.trace(unit, envelope_mask, scan,
                                  self.call.session.image_data(unit["project"]),
                                  pixel_um=float(pixel["value"]) if pixel else None,
                                  envelope=envelope, options=options)
        if result.status != "refined":
            self.log(event="refine_fallback" if result.status == "fallback"
                     else "refine_skipped", unit=self.key_of(unit), reason=result.reason)
        return result.geometry or envelope, result.to_record()

    def write_candidate(self, unit, *, klass, action):
        """Write (apply mode) or propose the candidate's ROI; records it in
        the session's result either way."""
        from plexora.agent.receipts import make_receipt
        from plexora.plugins.qc.server import results, roi_link

        geometry = unit.get("geometry")
        if not geometry:
            geometry, _mask = self.envelope_of(unit)
        unit.setdefault("envelope_geometry", geometry)
        if geometry is None:
            unit["write_error"] = "no geometry"
            return None
        record = self._candidate_record(unit, klass=klass, action=action, geometry=geometry)
        unit["geometry"] = geometry
        if unit.get("channel_level"):
            # A verdict on whole channels (a cycle out of register
            # everywhere): recorded, never drawn -- the cells read it from
            # the result (`cells.calls`).
            record["channel_level"] = True
            self._store_candidate(record)
            return None
        if self.options["mode"] != "apply":
            unit["proposed"] = True
            self._store_candidate(record)
            return None
        ds = self.call.session.image_data(unit["project"])
        if unit.get("roi_id"):
            # Decided again (the final review reopened it): the region takes
            # the new action, class and outline -- unless the user has made
            # it theirs. Receipted and undoable like the first write.
            return self._rewrite_candidate(ds, unit, record, klass=klass, action=action)
        try:
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

    def _rewrite_candidate(self, ds, unit, record, *, klass, action):
        """The update path of `write_candidate`: the region QC wrote before
        follows the new decision, with a receipt (before/after, an
        `update_roi` undo) appended exactly as a first write's is. Returns
        the receipt's operation id, or None when nothing changed."""
        from plexora.agent.receipts import make_receipt
        from plexora.plugins.qc.server import roi_link
        from plexora.plugins.roi.server.repository import ConflictError

        roi_id = unit["roi_id"]
        try:
            try:
                changed = roi_link.update(ds, roi_id, action, record)
            except ConflictError:
                # The panel saved meanwhile: read again and write once more.
                changed = roi_link.update(ds, roi_id, action, record)
        except Exception as exc:
            unit["write_error"] = str(exc)
            self.log(event="write_failed", unit=self.key_of(unit), error=str(exc))
            return None
        record["roi_id"] = roi_id
        if changed is None:
            self._store_candidate(record)
            return None
        before, after = changed["before"], changed["after"]
        told = roi_link.tell_roi_panel(self.call, unit["project"], "update")
        undo, partial = roi_link.undo_arguments(unit["project"], before,
                                                changed["revision_after"],
                                                reshaped=changed["reshaped"])
        hint = {"tool": "update_roi", "arguments": undo}
        if partial:
            hint["partial"] = True

        def brief(summary):
            return {"roi_id": summary["id"], "name": summary["name"],
                    "category_id": summary.get("category_id"),
                    "category": summary.get("category")}

        receipt = make_receipt(
            self._child(unit["project"]), changed=True, before=brief(before),
            after=brief(after), revision_before=changed["revision_before"],
            revision_after=changed["revision_after"], persistent_state="plugin_store:roi",
            reversible=not partial, undo_hint=hint,
            extra={"parent_operation_id": self.record["operation_id"], "qc_session": self.id,
                   "candidate_id": unit["id"], "artifact_class": klass, "action": action,
                   "rewrite": True, "roi_panel_notified": bool(told)})
        unit["written"] = {"revision": changed["revision_after"],
                           "category_id": after.get("category_id")}
        unit.setdefault("receipts", []).append(receipt.operation_id)
        self.record.setdefault("receipts", []).append(receipt.operation_id)
        self._store_candidate(record)
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
                "envelope_geometry": unit.get("envelope_geometry") or geometry,
                "refinement": {k: v for k, v in (unit.get("refinement") or {}).items()
                               if k != "key"} or None,
                "primary_metric": unit.get("primary_metric") or "",
                "peak": unit.get("peak"),
                "variant_chosen": unit.get("variant") or "standard",
                "severity": unit.get("severity"), "score": unit.get("score"),
                "metrics": unit.get("metrics") or {},
                "measurement": unit.get("measurement") or {},
                "ai_decision": {k: decision.get(k) for k in (
                    "verdict", "artifact_class", "severity", "confidence", "boundary",
                    "scope", "exclude_recommended", "manual_review", "source")},
                "evidence_artifacts": list(unit.get("artifacts") or []),
                # The agent's own words on every look it took (one per answer,
                # at most 300 characters each): the hover card's
                # interpretation line and the export's `ai_notes`.
                "notes": list(dict.fromkeys(unit.get("notes") or [])),
                "origin": unit.get("origin") or "detector",
                **({"check_unit": unit.get("check_unit"), "fine_grid": True,
                    "trace": unit.get("trace"), "cell_um": unit.get("cell_um"),
                    "whole_tissue": bool(unit.get("whole_tissue"))}
                   if unit.get("origin") == "check" else {}),
                **({"findings": unit["findings"],
                    "consolidated_from": list(unit.get("consolidated_from") or [])}
                   if unit.get("findings") else {}),
                **({"channel_level": True} if unit.get("channel_level") else {}),
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

    def remove_roi(self, unit, *, reason):
        """Delete the ROI QC wrote for `unit`, receipted (its undo writes it
        back); the unit keeps the id as `was_roi_id`. False when it could
        not be removed (it is gone already, or the store refused)."""
        from plexora.agent.receipts import make_receipt
        from plexora.plugins.qc.server import results, roi_link
        from plexora.plugins.roi.server import service
        from plexora.plugins.roi.server.repository import ConflictError

        roi_id = unit.get("roi_id")
        if not roi_id:
            return False
        ds = self.call.session.image_data(unit["project"])
        try:
            try:
                before, after, deleted = service.delete_roi(ds, roi_id)
            except ConflictError:
                before, after, deleted = service.delete_roi(ds, roi_id)
        except Exception as exc:  # noqa: BLE001 -- a region left standing is still correct
            self.log(event="remove_failed", unit=self.key_of(unit), error=str(exc))
            return False
        receipt = make_receipt(
            self._child(unit["project"]), changed=True, before={"roi_id": roi_id,
                                                                "name": deleted.get("name")},
            after=None, revision_before=before, revision_after=after,
            persistent_state="plugin_store:roi", reversible=False,
            undo_hint={"tool": "create_roi", "arguments": {
                "project": unit["project"], "category": deleted.get("category"),
                "geometry": deleted.get("geometry"), "name": deleted.get("name"),
                "notes": deleted.get("notes"), "base_revision": after}},
            extra={"parent_operation_id": self.record["operation_id"], "qc_session": self.id,
                   "candidate_id": unit["id"], "removed": reason})
        unit["was_roi_id"] = roi_id
        unit.pop("roi_id", None)
        unit.setdefault("receipts", []).append(receipt.operation_id)
        self.record.setdefault("receipts", []).append(receipt.operation_id)
        results.drop_roi_meta(unit["project"], [roi_id])
        roi_link.tell_roi_panel(self.call, unit["project"], "delete")
        return True

    def restore_record(self, unit, **fields):
        """Update the result's record of `unit` after its ROI moved elsewhere
        (`roi_id` follows the unit; `fields` are added)."""
        from plexora.plugins.qc.server import results

        project = self.project
        with results.lock(project):
            document = results.load(project)
            result = results.get_result(project, document, self.record["result_id"])
            record = ((result or {}).get("candidates") or {}).get(unit["id"])
            if record is None:
                return
            record["roi_id"] = unit.get("roi_id")
            if unit.get("was_roi_id"):
                record["was_roi_id"] = unit["was_roi_id"]
            record.update(fields)
            results.put_result(document, result)
            results.save(project, document)

    # -- settling ------------------------------------------------------------------

    def settle_channels(self):
        """Close every channel whose candidates are all settled."""
        checks = [u for u in self.units_of("check") if u["state"] not in TERMINAL]
        for channel in self.units_of("channel"):
            if channel["state"] != "awaiting_candidates":
                continue
            mine = [u for u in self.units_of("candidate")
                    if channel["id"] in cand.audit_channels(u)]
            if any(u["state"] not in TERMINAL for u in mine):
                continue
            # A channel an image check scores is settled once the check is.
            if any(channel["id"] in (c.get("channels") or [c.get("channel")]) for c in checks):
                continue
            # A candidate shown on several channels' rows flags the channels
            # its scope kept (its lead, when the scope was never narrowed).
            confirmed = [u for u in mine if (u["state"] in schemas.CONFIRMED_STATES
                                             or u["state"] == "manual_review_recommended")
                         and u["state"] != "confirmed_noted"
                         and channel["id"] in (u.get("channels") or [u.get("audit_channel")])]
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
        if unit.get("held_for"):
            # A check's region waiting for its probes (check_candidates).
            from plexora.plugins.qc.server import check_candidates

            if check_candidates.still_held(self, unit) or unit["state"] not in CANDIDATE_ASKS:
                return None
        if unit.get("detector") == "audit" and not unit.get("localized"):
            found = self._found_by_check(unit)
            if found is not None:
                found.setdefault("merged", []).append(unit["id"])
                self.close(unit, "merged", f"the {found.get('check_unit') or 'check'} already "
                                           f"outlined it on this channel ({found['id']})")
                return None
        if self._batchable(unit) and unit.get("origin") != "check":
            explained = self._explained_by(unit)
            if explained is not None:
                explained.setdefault("merged", []).append(unit["id"])
                self.close(unit, "merged", f"inside {explained['id']}, already excluded: "
                                           "its cells are excluded whatever a look would say")
                return None
        kind = ASKS[unit["state"]]
        if not self.wants(unit, kind):
            return None
        return kind

    @staticmethod
    def _batchable(unit):
        """A first look at a candidate: batched with others on one sheet. A
        deeper look (`need_more_evidence`), or one the final review reopened,
        stays a packet of its own."""
        return unit["state"] == "awaiting_confirm" and not int(unit.get("level") or 0) \
            and not unit.get("reopened")

    def _confirm_batch(self, first, candidates=None):
        """`first` and up to `confirm_batch - 1` other first looks: the same
        class first, then the same channel, then in the session's order."""
        size = int(ENGINE["confirm_batch"])
        if size <= 1 or not self._batchable(first):
            return [first]
        if candidates is None:
            candidates = sorted(self.units_of("candidate", first["project"]),
                                key=lambda u: (-float(u.get("score") or 0), u["id"]))
        klass, channel = first.get("class_hint"), first.get("channel")
        others = [u for u in candidates if u is not first and self._batchable(u)]
        others.sort(key=lambda u: (u.get("class_hint") != klass, u.get("channel") != channel))
        batch = [first]
        for unit in others:
            if len(batch) >= size:
                break
            if self._candidate_kind(unit) == "artifact_confirm":
                batch.append(unit)
        return batch

    def next_unit(self):
        record = self.record
        bulk_running = record.get("state") == "bulk_running"
        self.settle_channels()
        last = self.last_unit()
        if last is not None and last["type"] == "candidate" and last["state"] in CANDIDATE_ASKS:
            kind = self._candidate_kind(last)
            if kind:
                return kind, self._confirm_batch(last) if kind == "artifact_confirm" \
                    else [last]
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
        # The image checks, before the detector candidates: a check's regions
        # are decided from one look at its score, and take in the detector
        # candidates at the same place. Registration first: a field out of
        # register explains its place before focus does (`consolidate.rank`).
        rank = {c: i for i, c in enumerate(REVIEW_ORDER)}
        for unit in sorted(self.units_of("check", project),
                           key=lambda u: (rank.get(u.get("check"), 9),
                                          order.get(u.get("channel"), 0), u["id"])):
            if unit["state"] == "awaiting_score_review" and not self.check_user_edit(unit) \
                    and self.wants(unit, "score_review"):
                return "score_review", [unit]
        candidates = sorted(self.units_of("candidate", project),
                            key=lambda u: (order.get(u.get("audit_channel"), 0),
                                           -float(u.get("score") or 0), u["id"]))
        for unit in candidates:
            kind = self._candidate_kind(unit)
            if kind:
                return kind, self._confirm_batch(unit, candidates) \
                    if kind == "artifact_confirm" else [unit]
        self.settle_channels()
        if bulk_running:
            return "wait", []
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
        # The bulk pass's own stage (bulk.py's `_progress_announcer`), folded
        # in beside the job id and state `BaseEngine.progress` already puts
        # in `bulk` -- so a caller who only reads `progress` still sees it.
        if self.record.get("bulk_progress"):
            out["bulk"] = {**out["bulk"], **self.record["bulk_progress"]}
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
    # A finding consolidated into a category ROI is counted once, as that ROI.
    cands = [u for u in units if u.get("type") == "candidate"
             and not u.get("consolidated_into")]
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
            "text": _summary_text(channels, cands, record.get("planning_notes")),
            "planning_notes": list(record.get("planning_notes") or []),
            "proposed": sum(1 for u in cands if u.get("proposed")),
            "replayed": len(record.get("replayed") or []),
            "by_state": by_state}


def _summary_text(channels, cands, notes=None):
    """The panel's one line for a finished QC session, naming any check that
    was not run (`planning_notes`): silence on a check is not a clean result."""
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
    not_run = [n["check"] for n in notes or () if n.get("status") == "not_run"]
    if not_run:
        parts.append(f"not checked: {', '.join(not_run)}")
    return " · ".join(parts)
