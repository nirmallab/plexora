"""Writing a check's regions as QC ROIs: one place for the free check tools
(`capabilities_checks`) and the session's own writes (the background,
`background.py`) -- a server module cannot import a capabilities module.

A region a check wrote before is replaced on the next write for the same
keys (channels, comparisons, categories), unless the user made it theirs:
edited, locked, renamed (renaming is choosing the action) or moved it.
"""

from __future__ import annotations

from plexora.agent.receipts import make_receipt


def check_candidate(detector, version, klass, key, region, *, channels, scope,
                    threshold, action="exclude", cycles=(), extra_metrics=None,
                    score_key="max", trace="method", cell_um=None):
    """A check's region as a result candidate the user asked to write: its
    action pinned as an approval pins one, so a strictness change never
    renames it."""
    import hashlib

    from plexora.plugins.qc.server import refine, results

    # A map region's outline is the check's score map, and says so; a
    # detector's object is its own traced outline.
    refinement = {"status": "map", "method": refine.MAP_METHODS.get(klass, "score_map"),
                  "kept_fraction": 1.0, "refine_um": cell_um,
                  "reason": "the check's score map is the outline"} if trace == "map" else \
        dict(OBJECT_REFINEMENT) if trace == "object" else None
    return {"id": "cand_" + hashlib.sha1(f"{detector}:{key}".encode()).hexdigest()[:10],
            "trace": trace, "cell_um": cell_um, "refinement": refinement,
            "detector": detector, "detector_version": version, "class": klass,
            "class_alternatives": [], "scope": scope, "channels": list(channels),
            "cycles": [int(c) for c in cycles], "geometry": region["geometry"],
            "envelope_geometry": region["geometry"],
            # A check's score is not a severity: the region row would print it as one.
            "severity": None, "score": region.get(score_key),
            "metrics": {**threshold, **(extra_metrics or {})},
            "measurement": {}, "ai_decision": None, "evidence_artifacts": [],
            "origin": "check", "fine_grid": True,
            "created_by": detector, "state": "confirmed", "action": action,
            "created_at": results.now_iso(),
            "user_state": {"approved": True, "approved_action": action}}


#: The refinement record of a region that is a detector's own traced object.
OBJECT_REFINEMENT = {"status": "detector", "method": "artifact_detector", "kept_fraction": 1.0,
                     "reason": "the detector's own traced outline is the region"}


def replaceable(row, feature, klass, action=None):
    """Whether a region a check wrote before may be replaced: nobody edited,
    locked or moved it, and -- for a check that pins one action -- nobody
    renamed it to another (renaming is choosing the action). `klass` is the
    class the check writes, or the set of them."""
    classes = {klass} if isinstance(klass, str) else set(klass)
    if row.get("user_edited") or row.get("locked") or feature.get("locked") \
            or row.get("removed_from_qc") or row.get("class") not in classes:
        return False
    return action is None or row.get("approved_action") == action


def write_regions(call, project, *, detector, klass, covered, work, row_keys,
                  pinned="exclude", result_id=None, cells=True, session_id=None):
    """Write a check's regions as ROIs, replacing the ones it wrote before for
    the same keys (channels, comparisons) unless the user made them theirs.

    `work` is [(key, [candidate record])]; `row_keys(row)` the keys an
    earlier row covers. The candidates go into the active result, or into
    `result_id`'s (a session writing into its own result before it is
    active); `cells=False` leaves the cells to be derived later (the
    session derives them when it finishes). Returns (written, kept, removed,
    receipts, per_key, revisions)."""
    import dataclasses

    from plexora.plugins.qc.server import results, roi_link, strictness
    from plexora.plugins.roi.server.repository import ConflictError, ROIRepository

    ds = call.session.image_data(project)
    written, kept, removed, receipts = [], [], [], []
    per_key = {}
    with results.lock(project):
        document = results.load(project)
        roi_link.sync(ds, document)
        result = results.get_result(project, document, result_id) if result_id else None
        if result is None:
            result = results.ensure_active(document, project)
        meta = results.roi_meta(project)
        repo = ROIRepository(ds.name)
        state = repo.load()
        revision_before = state["revision"]
        features = {f["id"]: f for f in roi_link._features(state)}
        replace = []
        for row in (meta.to_dicts() if meta.height else []):
            feature = features.get(row["roi_id"])
            if row.get("detector") != detector or row.get("deleted") or feature is None:
                continue
            if not covered.intersection(row_keys(row)):
                continue
            if not replaceable(row, feature, klass, pinned):
                kept.append(row["roi_id"])
                continue
            replace.append(feature)
        if replace:
            ids = [f["id"] for f in replace]
            repo.apply(state["revision"], [{"op": "roi.bulk_delete", "ids": ids}])
            results.drop_roi_meta(project, ids)
            candidates = result.setdefault("candidates", {})
            for key in [k for k, c in candidates.items() if c.get("roi_id") in set(ids)]:
                del candidates[key]
            removed = [{"roi_id": f["id"], "name": f.get("name"),
                        "geometry": f.get("geometry")} for f in replace]
        rows = []
        k = 0
        for key, records in work:
            mine = per_key.setdefault(key, {"written": []})
            for record in records:
                k += 1
                action = record.get("action") or "exclude"
                try:
                    before, after, feature = roi_link.create(ds, record, action=action,
                                                             session_id=session_id)
                except ConflictError:
                    before, after, feature = roi_link.create(ds, record, action=action,
                                                             session_id=session_id)
                child = dataclasses.replace(call, operation_id=f"{call.operation_id}.{k:03d}",
                                            receipted=False, notify=None,
                                            extras=dict(call.extras))
                receipt = make_receipt(
                    child, changed=True, before=None,
                    after={"roi_id": feature["id"], "name": feature["name"], "key": key},
                    revision_before=before, revision_after=after,
                    persistent_state="plugin_store:roi",
                    undo_hint={"tool": "delete_roi", "arguments": {
                        "project": project, "roi_id": feature["id"], "confirm": True}},
                    extra={"parent_operation_id": call.operation_id,
                           "artifact_class": record["class"], "action": action})
                receipts.append(receipt.operation_id)
                record["roi_id"] = feature["id"]
                record["action_by_strictness"] = strictness.actions_by_preset(record)
                result.setdefault("candidates", {})[record["id"]] = record
                rows.append({**roi_link.meta_row(record, feature, result=result,
                                                 session_id=session_id, action=action,
                                                 strictness=None, agent=None,
                                                 operation_id=receipt.operation_id,
                                                 created_by=detector),
                             "approved": True, "approved_action": action})
                written.append(feature["id"])
                mine["written"].append(feature["id"])
        if rows:
            results.upsert_roi_meta(project, rows)
        results.put_result(document, result)
        results.save(project, document)
        revision_after = repo.load()["revision"]
    if written or removed:
        roi_link.tell_roi_panel(call, project, "create")
        if cells and call.session.project(project).has_table:
            from plexora.plugins.qc.server.cells import calls

            calls.write_for_active(call, project)
    return written, kept, removed, receipts, per_key, (revision_before, revision_after)


def write_receipt(call, written, kept, removed, receipts, revisions):
    changed = bool(written or removed)
    receipt = make_receipt(
        call, changed=changed, before={"removed": [r["roi_id"] for r in removed]},
        after={"written": written, "kept": kept}, revision_before=revisions[0],
        revision_after=revisions[1], persistent_state="plugin_store:roi",
        reversible=True, undo_hint={"note": "each region has its own receipt (children): "
                                    "undo_operation on one deletes that region",
                                    "children": receipts},
        extra={"children": receipts})
    return receipt.model_dump(mode="json")
