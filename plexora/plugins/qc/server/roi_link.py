"""QC regions are ROIs: the ROI plugin holds the geometry, QC the meaning.

A confirmed artifact is written as an ROI through the ROI plugin's own
revision-checked path (`ROIRepository.apply`), in one category per artifact
class (`qc_<class>`, "QC: Out of focus"), named with its action ("QC exclude:
out of focus · CD8"). What the ROI schema has no field for -- class, scope,
severity, confidence, detector, evidence, who made it -- is a `roi_meta` row
in QC's store keyed by the ROI's id, plus a `qc:<candidate_id>` token in the
notes as a fallback link.

The user's edits win. `sync` compares every QC ROI with what QC wrote (a
geometry hash, never `updated_at`, which has one-second resolution): a deleted
region drops out of the cells' reasons, an edited one is kept as the user drew
it and never touched again, a region moved to another `qc_*` category takes
that class, one moved out of QC is no longer QC, and a locked one is
approved. A region the user drew in a `qc_*` category is adopted as a
user-made candidate -- which is how manual QC works without any AI.
"""

from __future__ import annotations

import re

from plexora.plugins.qc.server import polygons, results, schemas

NOTE_TOKEN = re.compile(r"(?:^|\s)qc:(cand_[0-9a-f]{10}|user_[0-9a-z_]+)\b")


def _repo(ds):
    from plexora.plugins.roi.server.repository import ROIRepository

    return ROIRepository(ds.name)


def _features(state):
    from plexora.plugins.roi.server import schema

    return schema.image_entry(state, schema.DEFAULT_IMAGE)["features"]


def class_of(category_id, labels):
    """The artifact class of a category, by its `qc_<class>` id or its label."""
    return schemas.class_of_category(category_id) or \
        schemas.class_of_label(labels.get(category_id))


def _labels(state):
    return {c["id"]: c["label"] for c in state["categories"]}


def tell_roi_panel(call, project, what="qc"):
    """Tell an open ROI panel that its regions changed (it reloads them): a
    receipt announces under QC's name, and the ROI panel listens for its own."""
    if call is None or getattr(call, "notify", None) is None:
        return False
    try:
        return bool(call.notify(project, "roi", f"roi.{what}", {"by": "qc"}))
    except Exception:
        return False


def ensure_categories(ds, classes, *, state=None):
    """Create the `qc_<class>` categories that do not exist yet; returns the
    ROI document's revision after."""
    repo = _repo(ds)
    state = state or repo.load()
    existing = {c["id"] for c in state["categories"]}
    labels = {c["label"].casefold(): c["id"] for c in state["categories"]}
    ops = []
    for klass in dict.fromkeys(classes):
        category_id = schemas.roi_category_id(klass)
        if category_id in existing:
            continue
        label = schemas.roi_category_label(klass)
        if label.casefold() in labels:
            # The user named a category exactly like ours: use a variant.
            label = f"{label} (QC)"
        ops.append({"op": "category.create", "category": {
            "id": category_id, "label": label,
            "color": schemas.CLASS_COLORS.get(klass, "#fbbf24"),
            "sort_order": schemas.ROI_SORT_ORDER + schemas.ARTIFACT_CLASSES.index(klass)}})
    if not ops:
        return state["revision"]
    return repo.apply(state["revision"], ops)


def notes_for(candidate, *, session_id=None):
    decision = candidate.get("ai_decision") or {}
    parts = [schemas.CLASS_WORDS.get(candidate["class"], candidate["class"]),
             candidate.get("scope") or "channel"]
    if decision.get("severity"):
        parts.append(f"severity {decision['severity']}")
    if decision.get("confidence"):
        parts.append(f"confidence {decision['confidence']}")
    if candidate.get("detector"):
        parts.append(f"detector {candidate['detector']} v{candidate.get('detector_version')}")
    if session_id:
        parts.append(f"session {session_id}")
    return " · ".join(parts) + f"\nqc:{candidate['id']}"


def create(ds, candidate, *, action, session_id=None):
    """Write one candidate as an ROI. Returns (before_rev, after_rev, feature
    summary). The candidate carries `geometry` (GeoJSON, full-res px)."""
    from plexora.plugins.roi.server import service

    klass = candidate["class"]
    ensure_categories(ds, [klass])
    repo = _repo(ds)
    state = repo.load()
    base = state["revision"]
    roi_id = service.new_id("qcroi")
    feature = {"id": roi_id, "category_id": schemas.roi_category_id(klass),
               "name": schemas.roi_name(action, klass, list(candidate.get("channels") or [])),
               "geometry": candidate["geometry"],
               "notes": notes_for(candidate, session_id=session_id)}
    after = repo.apply(base, [{"op": "roi.create", "feature": feature}])
    _, summary = service.get_roi(ds, roi_id)
    return base, after, summary


def rename(ds, roi_id, action, candidate):
    """Change the action a QC ROI's name carries. Returns (before, after,
    old_name) or None when nothing changed."""
    from plexora.plugins.roi.server import service

    name = schemas.roi_name(action, candidate["class"], list(candidate.get("channels") or []))
    base, after, before_summary, _after = service.update_roi(ds, roi_id, name=name)
    if base == after:
        return None
    return base, after, before_summary["name"]


def update(ds, roi_id, action, candidate):
    """A QC ROI decided again: its name (action), category (class) and outline
    follow the new decision -- except what the user made theirs (a region they
    edited, relabelled, locked or approved is left as they have it)."""
    from plexora.plugins.roi.server import service

    meta = results.roi_meta(ds.name)
    row = next((r for r in meta.to_dicts() if r["roi_id"] == roi_id), None) \
        if meta.height else None
    if row and (row.get("user_edited") or row.get("approved") or row.get("locked")):
        return rename(ds, roi_id, action, candidate) if not row.get("approved") else None
    ensure_categories(ds, [candidate["class"]])
    name = schemas.roi_name(action, candidate["class"], list(candidate.get("channels") or []))
    geometry = candidate.get("geometry")
    base, after, before, now = service.update_roi(
        ds, roi_id, name=name, category=schemas.roi_category_label(candidate["class"]),
        geometry=geometry)
    if row is not None:
        row.update(action=action, **{"class": candidate["class"]},
                   written_category_id=now["category_id"],
                   written_geometry_hash=polygons.geometry_hash(geometry)
                   if geometry else row.get("written_geometry_hash"))
        results.upsert_roi_meta(ds.name, [row])
    return None if base == after else (base, after, before["name"])


def meta_row(candidate, summary, *, result, session_id, action, strictness, agent,
             operation_id, created_by="agent"):
    decision = candidate.get("ai_decision") or {}
    return {"roi_id": summary["id"], "candidate_id": candidate["id"],
            "result_id": result["result_id"], "session_id": session_id,
            "class": candidate["class"], "channels": list(candidate.get("channels") or []),
            "cycles": [int(c) for c in candidate.get("cycles") or []],
            "scope": candidate.get("scope"), "severity": decision.get("severity"),
            "confidence": decision.get("confidence"), "action": action,
            "strictness_used": strictness, "detector": candidate.get("detector"),
            "detector_version": candidate.get("detector_version"),
            "evidence_artifacts": list(candidate.get("evidence_artifacts") or []),
            "agent_id": agent, "created_by": created_by, "created_at": results.now_iso(),
            "user_edited": False, "approved": False, "approved_action": None,
            "locked": bool(summary.get("locked")), "deleted": False, "removed_from_qc": False,
            "written_geometry_hash": polygons.geometry_hash(candidate["geometry"]),
            "written_category_id": summary["category_id"], "operation_id": operation_id}


# -- the user's edits ------------------------------------------------------------------


def sync(ds, document, *, save=True) -> dict:
    """Reconcile QC's view of its regions with the ROI document (the user
    wins); returns {adopted, edited, deleted, relabelled, removed, locked}."""
    report = {"adopted": [], "edited": [], "deleted": [], "relabelled": [], "removed": [],
              "locked": []}
    try:
        state = _repo(ds).load()
    except Exception as exc:  # an unreadable ROI document is reported, never "fixed"
        report["error"] = str(exc)
        return report
    features = {f["id"]: f for f in _features(state)}
    labels = _labels(state)
    meta = results.roi_meta(ds.name)
    result = results.active(document)
    rows = meta.to_dicts() if meta.height else []
    known = set()
    changed_rows = []
    live_ids = {c.get("roi_id") for c in ((result or {}).get("candidates") or {}).values()}
    for row in rows:
        known.add(row["roi_id"])
        feature = features.get(row["roi_id"])
        # A row of another result (an earlier session found the same place,
        # so the same candidate id) speaks for its own region only.
        candidate = (result or {}).get("candidates", {}).get(row["candidate_id"]) \
            if result and (row.get("result_id") == result.get("result_id")
                           or row["roi_id"] in live_ids) else None
        if candidate is not None and candidate.get("roi_id") not in (None, row["roi_id"]):
            candidate = None
        user = (candidate or {}).setdefault("user_state", {}) if candidate else {}
        if feature is None:
            if not row.get("deleted"):
                row["deleted"] = True
                changed_rows.append(row)
                report["deleted"].append(row["roi_id"])
            if candidate is not None:
                user["deleted"] = True
            continue
        klass = class_of(feature["category_id"], labels)
        if feature["category_id"] != row.get("written_category_id"):
            if klass is None:
                if not row.get("removed_from_qc"):
                    row["removed_from_qc"] = True
                    changed_rows.append(row)
                    report["removed"].append(row["roi_id"])
                if candidate is not None:
                    user["removed_from_qc"] = True
            elif klass != row.get("class"):
                row["class"] = klass
                row["written_category_id"] = feature["category_id"]
                row["removed_from_qc"] = False
                changed_rows.append(row)
                report["relabelled"].append(row["roi_id"])
                if candidate is not None:
                    candidate["class"] = klass
                    user["relabelled"] = True
                    user["removed_from_qc"] = False
        elif row.get("removed_from_qc"):
            row["removed_from_qc"] = False
            changed_rows.append(row)
            if candidate is not None:
                user["removed_from_qc"] = False
        if polygons.geometry_hash(feature["geometry"]) != row.get("written_geometry_hash") \
                and not row.get("user_edited"):
            row["user_edited"] = True
            changed_rows.append(row)
            report["edited"].append(row["roi_id"])
        if candidate is not None and row.get("user_edited"):
            user["edited"] = True
            candidate["geometry"] = feature["geometry"]
        # Renaming "QC exclude: ..." to "QC warn: ..." in the ROI panel is
        # the user choosing the action: it is pinned, like an approval.
        named = schemas.action_of_name(feature.get("name"))
        if named and named != row.get("action") and named != row.get("approved_action"):
            row["approved"] = True
            row["approved_action"] = named
            row["action"] = named
            changed_rows.append(row)
            report.setdefault("renamed", []).append(row["roi_id"])
            if candidate is not None:
                candidate["action"] = named
        if bool(feature.get("locked")) != bool(row.get("locked")):
            row["locked"] = bool(feature.get("locked"))
            if row["locked"]:
                row["approved"] = True
                report["locked"].append(row["roi_id"])
            changed_rows.append(row)
        if candidate is not None:
            user["locked"] = bool(row.get("locked"))
            user["approved"] = bool(row.get("approved"))
            if row.get("approved_action"):
                user["approved_action"] = row["approved_action"]
    # Adoption: regions in a QC category QC did not write.
    adopted_rows = []
    for roi_id, feature in features.items():
        klass = class_of(feature["category_id"], labels)
        if klass is None or roi_id in known:
            continue
        match = NOTE_TOKEN.search(feature.get("notes") or "")
        if result is None:
            result = results.ensure_active(document, ds.name)
        token = match.group(1) if match else None
        owner = (result.get("candidates") or {}).get(token) if token else None
        # A copy of a QC region (notes and all) does not take over the
        # candidate of a region that still exists.
        candidate_id = token if owner is not None and (
            not owner.get("roi_id") or owner["roi_id"] not in features) \
            else f"user_{roi_id}".lower()
        candidate = result.setdefault("candidates", {}).get(candidate_id)
        if candidate is None:
            candidate = {"id": candidate_id, "detector": "user", "detector_version": "1",
                         "class": klass, "class_alternatives": [], "scope": "all_channels",
                         "channels": [], "cycles": [], "geometry": feature["geometry"],
                         "severity": None, "score": None, "metrics": {},
                         "measurement": {}, "ai_decision": None, "evidence_artifacts": [],
                         "roi_id": roi_id, "created_by": "user", "state": "user_kept",
                         "user_state": {"created_by": "user"},
                         "created_at": results.now_iso()}
            result["candidates"][candidate_id] = candidate
        else:
            candidate["roi_id"] = roi_id
        action = schemas.action_of_name(feature.get("name")) or "exclude"
        candidate["action"] = action
        adopted_rows.append({
            "roi_id": roi_id, "candidate_id": candidate_id, "result_id": result["result_id"],
            "session_id": None, "class": klass, "channels": [], "cycles": [],
            "scope": "all_channels", "severity": None, "confidence": None, "action": action,
            "strictness_used": None, "detector": "user", "detector_version": "1",
            "evidence_artifacts": [], "agent_id": None, "created_by": "user",
            "created_at": results.now_iso(), "user_edited": False, "approved": True,
            "approved_action": action, "locked": bool(feature.get("locked")),
            "deleted": False, "removed_from_qc": False,
            "written_geometry_hash": polygons.geometry_hash(feature["geometry"]),
            "written_category_id": feature["category_id"], "operation_id": None})
        report["adopted"].append(roi_id)
    # A read (save=False) writes nothing at all: half a sync -- the rows
    # without the candidates they point at -- would lose an adoption.
    if save and (changed_rows or adopted_rows):
        results.upsert_roi_meta(ds.name, [*changed_rows, *adopted_rows])
    if save and (changed_rows or adopted_rows) and result is not None:
        results.put_result(document, result)
        results.save(ds.name, document)
    if result is not None and any(report[k] for k in report if k != "error"):
        result.setdefault("warnings", []).extend(
            {"code": f"user_{k}", "roi_ids": v} for k, v in report.items()
            if v and k != "error")
    return report


def user_wins(candidate) -> bool:
    user = candidate.get("user_state") or {}
    return bool(candidate.get("created_by") == "user" or user.get("created_by") == "user"
                or user.get("edited") or user.get("approved") or user.get("locked")
                or user.get("relabelled")
                or user.get("deleted") or user.get("removed_from_qc"))


def live_regions(ds, result) -> list:
    """The QC regions that count for the cells now: [{roi_id, geometry,
    class, action}] -- deleted and removed ones left out, geometry as the
    ROI document holds it (the user's, when edited)."""
    try:
        state = _repo(ds).load()
    except Exception:
        return []
    features = {f["id"]: f for f in _features(state)}
    labels = _labels(state)
    out = []
    for candidate in (result or {}).get("candidates", {}).values():
        roi_id = candidate.get("roi_id")
        user = candidate.get("user_state") or {}
        if not roi_id or user.get("deleted") or user.get("removed_from_qc"):
            continue
        feature = features.get(roi_id)
        if feature is None:
            continue
        klass = class_of(feature["category_id"], labels) or candidate["class"]
        out.append({"roi_id": roi_id, "candidate_id": candidate["id"], "class": klass,
                    "action": candidate.get("action") or "exclude",
                    "geometry": feature["geometry"],
                    "channels": list(candidate.get("channels") or [])})
    return out
