"""QC regions are ROIs: the ROI plugin holds the geometry, QC the meaning.

A confirmed artifact is written as an ROI through the ROI plugin's own
revision-checked path (`ROIRepository.apply`), in one of five categories
(`qc_<category>`, "QC: Blur / focus issue"; `qc_review` for a region left for
review), named with its action and subtype ("QC exclude: out of focus ·
CD8"). What the ROI schema has no field for -- the class (the subtype),
scope, severity, confidence, detector, evidence, who made it -- is a
`roi_meta` row in QC's store keyed by the ROI's id, plus a `qc:<candidate_id>`
token in the notes as a fallback link. A region drawn by hand as a named
subtype carries `qc-class:<class>` in its notes until QC adopts it.

The user's edits win. `sync` compares every QC ROI with what QC wrote (a
geometry hash, never `updated_at`, which has one-second resolution): a deleted
region drops out of the cells' reasons, an edited one is kept as the user drew
it and never touched again, a region moved to another of the five takes that
category's default class (moved within its own category, it keeps its
subtype), one moved out of QC is no longer QC, and a locked one is approved.
A region the user drew in a `qc_*` category is adopted as a user-made
candidate -- which is how manual QC works without any AI.

A project QC wrote before the five existed has one category per class
(`qc_tissue_fold`, "QC: Tissue fold"). `migrate_categories` moves their
regions into the five in one revision, keeping each region's class.
"""

from __future__ import annotations

import re

from plexora.plugins.qc.server import polygons, results, schemas

NOTE_TOKEN = re.compile(r"(?:^|\s)qc:(cand_[0-9a-f]{10}|user_[0-9a-z_]+)\b")
#: The subtype a region drawn by hand was given, until QC adopts it.
CLASS_TOKEN = re.compile(r"(?:^|\s)qc-class:([a-z_]+)\b")


def _repo(ds):
    from plexora.plugins.roi.server.repository import ROIRepository

    return ROIRepository(ds.name)


def _features(state):
    from plexora.plugins.roi.server import schema

    return schema.image_entry(state, schema.DEFAULT_IMAGE)["features"]


def class_of(category_id, labels):
    """The artifact class of a category, by its `qc_*` id or its label: a
    legacy `qc_<class>` its class, one of the five its default class."""
    return schemas.class_of_category(category_id) or \
        schemas.class_of_label(labels.get(category_id))


def category_of(category_id, labels):
    """The category (one of the five, "review", a custom key) a feature's
    category stands for, or None when it is not QC's."""
    return schemas.category_key(category_id) or \
        schemas.category_of_label(labels.get(category_id))


def class_in(category_id, labels, prior=None):
    """The class of a region in a category, keeping `prior` (its subtype)
    while it is still a class of that category."""
    category = category_of(category_id, labels)
    if category is None:
        return None
    if prior in schemas.ARTIFACT_CLASSES and _category_of_class(prior) == category:
        return prior
    return class_of(category_id, labels)


def _category_of_class(klass):
    return schemas.category_of_class(klass)


def class_token(notes):
    """The `qc-class:<class>` a region drawn by hand carries, or None."""
    match = CLASS_TOKEN.search(notes or "")
    klass = match.group(1) if match else None
    return klass if klass in schemas.ARTIFACT_CLASSES else None


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


def _sort_order(key):
    if key in schemas.CATEGORY_IDS:
        return schemas.ROI_SORT_ORDER + schemas.CATEGORY_IDS.index(key)
    return schemas.ROI_SORT_ORDER + len(schemas.CATEGORY_IDS)


def _category_ops(state, keys, colors=None):
    """`category.create` ops for the `qc_<category>` categories of `keys`
    (the five, "review", or classes -- a class stands for its category) that
    `state` does not have yet."""
    existing = {c["id"] for c in state["categories"]}
    labels = {c["label"].casefold() for c in state["categories"]}
    ops = []
    for key in dict.fromkeys(schemas._as_key(k) for k in keys):
        if key is None or schemas.is_custom(key):
            continue  # a custom category is made by name (ensure_custom_category)
        category_id = schemas.roi_category_id(key)
        if category_id in existing:
            continue
        label = schemas.roi_category_label(key)
        if label.casefold() in labels:
            # The user named a category exactly like ours: use a variant.
            label = f"{label} (QC)"
        labels.add(label.casefold())
        existing.add(category_id)
        ops.append({"op": "category.create", "category": {
            "id": category_id, "label": label,
            "color": (colors or {}).get(key) or schemas.category_color(key),
            "sort_order": _sort_order(key)}})
    return ops


def ensure_categories(ds, keys, *, state=None):
    """Create the `qc_<category>` categories of `keys` (the five, "review",
    or classes, which stand for their category) that do not exist yet --
    moving a pre-categories project's regions into them first. Returns the
    ROI document's revision after."""
    repo = _repo(ds)
    state = state or repo.load()
    if migrate_categories(ds, state=state):
        state = repo.load()
    ops = _category_ops(state, keys)
    if not ops:
        return state["revision"]
    return repo.apply(state["revision"], ops)


def legacy_categories(state):
    """[(category, class)] of the pre-categories QC categories in an ROI
    document: a `qc_<class>` id, or a label that names a class ("QC: Tissue
    fold") on a category QC did not mint."""
    out = []
    for category in state.get("categories") or []:
        klass = schemas.legacy_class_of_category_id(category["id"])
        if klass is None and not str(category["id"]).startswith(schemas.ROI_CATEGORY_PREFIX) \
                and schemas.is_legacy_label(category.get("label")):
            klass = schemas.class_of_label(category["label"])
        if klass:
            out.append((category, klass))
    return out


def migrate_categories(ds, *, state=None):
    """Move every region of a pre-categories QC category into the one of the
    five its class belongs to, in ONE revision, and delete the emptied
    category. Each region keeps its class: where QC wrote it there, its
    `roi_meta` row already holds it (the row's `written_category_id` is moved
    in the same step, so the move is not mistaken for the user relabelling
    it); a region QC never adopted, or one the user moved there themselves,
    gets a `qc-class:` token in its notes, which `sync` reads as the class
    the user chose. A colour the user gave a legacy category is carried to
    its target when the target is made here.

    Returns None when nothing is legacy, else {moved, categories, revision}."""
    repo = _repo(ds)
    state = state or repo.load()
    legacy = legacy_categories(state)
    if not legacy:
        return None
    meta = results.roi_meta(ds.name)
    rows = {r["roi_id"]: r for r in meta.to_dicts()} if meta.height else {}
    colors = {}
    for category, klass in legacy:
        target = schemas.category_of_class(klass)
        if category.get("color") and category["color"].lower() != \
                schemas.CLASS_COLORS.get(klass, "").lower():
            colors.setdefault(target, category["color"])
    ops = _category_ops(state, [schemas.category_of_class(k) for _c, k in legacy], colors)
    moved = {}
    by_id = {category["id"]: klass for category, klass in legacy}
    for feature in _features(state):
        klass = by_id.get(feature["category_id"])
        if klass is None:
            continue
        moved[feature["id"]] = (klass, schemas.roi_category_id(schemas.category_of_class(klass)))
        row = rows.get(feature["id"])
        ours = row is not None and row.get("written_category_id") == feature["category_id"]
        if not ours and class_token(feature.get("notes")) != klass:
            notes = CLASS_TOKEN.sub("", feature.get("notes") or "").rstrip("\n")
            ops.append({"op": "roi.update_properties", "id": feature["id"], "changes": {
                "notes": f"{notes}\nqc-class:{klass}".lstrip("\n")}})
    for category, klass in legacy:
        ops.append({"op": "category.delete", "id": category["id"], "orphans": "reassign",
                    "reassign_to": schemas.roi_category_id(schemas.category_of_class(klass))})
    revision = repo.apply(state["revision"], ops)
    changed = []
    legacy_ids = {category["id"] for category, _k in legacy}
    for roi_id, (klass, target) in moved.items():
        row = rows.get(roi_id)
        # Only where QC wrote the region into the legacy category: a region
        # the user moved there is still the user's move for `sync` to see.
        if row is not None and row.get("written_category_id") in legacy_ids:
            row["written_category_id"] = target
            changed.append(row)
    if changed:
        results.upsert_roi_meta(ds.name, changed)
    return {"moved": sorted(moved), "categories": [c["id"] for c, _k in legacy],
            "revision": revision}


def ensure_custom_category(ds, words):
    """The `qc_custom_<slug>` category for a QC category the user named, made
    when it does not exist yet. Returns {key, category_id, label, words,
    color, revision}, or None when `words` names nothing."""
    words = " ".join(str(words or "").split())[:schemas.MAX_CUSTOM_WORDS]
    key = schemas.custom_key(words)
    if key is None:
        return None
    category_id = schemas.roi_category_id(key)
    repo = _repo(ds)
    state = repo.load()
    found = next((c for c in state["categories"] if c["id"] == category_id), None)
    if found is not None:
        return {"key": key, "category_id": category_id, "label": found["label"],
                "words": custom_words(found["label"]), "color": found.get("color"),
                "revision": state["revision"]}
    label = f"QC: {words[:1].upper()}{words[1:]}"
    if label.casefold() in {c["label"].casefold() for c in state["categories"]}:
        label = f"{label} (QC)"
    color = schemas.custom_color(key)
    customs = sum(1 for c in state["categories"] if schemas.is_custom(
        schemas.category_key(c["id"])))
    revision = repo.apply(state["revision"], [{"op": "category.create", "category": {
        "id": category_id, "label": label, "color": color,
        "sort_order": schemas.ROI_SORT_ORDER + 10 + customs}}])
    return {"key": key, "category_id": category_id, "label": label,
            "words": custom_words(label), "color": color, "revision": revision}


def custom_words(label) -> str:
    """"QC: Pen mark (QC)" -> "Pen mark": how a custom category reads."""
    text = str(label or "").strip()
    if text.casefold().startswith("qc:"):
        text = text[3:].strip()
    if text.casefold().endswith("(qc)"):
        text = text[:-4].strip()
    return text


def custom_categories(ds) -> list:
    """[{id, words, color, default_color, label}] of every custom QC category
    the ROI document has, in the order they were made."""
    try:
        state = _repo(ds).load()
    except Exception:  # no ROI document yet: none
        return []
    out = []
    for category in sorted(state.get("categories") or [],
                           key=lambda c: c.get("sort_order") or 0):
        key = schemas.category_key(category["id"])
        if schemas.is_custom(key):
            out.append({"id": key, "words": custom_words(category["label"]),
                        "color": category.get("color") or schemas.custom_color(key),
                        "default_color": schemas.custom_color(key),
                        "label": category["label"]})
    return out


def category_colors(ds) -> dict:
    """{category: colour} of every QC category the ROI document has (the
    five, "review", custom keys) -- the one colour a QC category is drawn
    in, whichever panel changed it."""
    try:
        state = _repo(ds).load()
    except Exception:  # no ROI document yet: every category at its default
        return {}
    out = {}
    for category in state.get("categories") or []:
        key = schemas.category_key(category["id"])
        if key and category.get("color") and not schemas.legacy_class_of_category_id(
                category["id"]):
            out[key] = category["color"]
    return out


def set_category_color(ds, key, color=None):
    """Recolour a QC category (one of the five, "review", or a class, which
    stands for its category -- made if QC never wrote it); `None` puts back
    its default. A custom category's key (`custom_<slug>`) recolours that
    category, which must exist. Returns the colour set."""
    key = schemas._as_key(key) or key
    color = color or schemas.category_color(key)
    ensure_categories(ds, [key])
    repo = _repo(ds)
    state = repo.load()
    repo.apply(state["revision"], [{"op": "category.update",
                                    "id": schemas.roi_category_id(key),
                                    "changes": {"color": color}}])
    return color


def category_label(ds, key):
    """The label QC's category for `key` has in this ROI document (it may be
    the "(QC)" variant), made when it does not exist."""
    ensure_categories(ds, [key])
    state = _repo(ds).load()
    category_id = schemas.roi_category_id(key)
    found = next((c for c in state["categories"] if c["id"] == category_id), None)
    return found["label"] if found else schemas.roi_category_label(key)


def notes_for(candidate, *, session_id=None):
    decision = candidate.get("ai_decision") or {}
    parts = [schemas.CLASS_WORDS.get(candidate["class"], candidate["class"]),
             candidate.get("scope") or "channel"]
    metrics = candidate.get("metrics") or {}
    if metrics.get("threshold") is not None and candidate.get("origin") == "check":
        parts.append(f"score {_fmt(candidate.get('score'))} at threshold "
                     f"{_fmt(metrics['threshold'])} ({metrics.get('threshold_source') or 'auto'})")
    if decision.get("severity"):
        parts.append(f"severity {decision['severity']}")
    if decision.get("confidence"):
        parts.append(f"confidence {decision['confidence']}")
    if candidate.get("detector"):
        parts.append(f"detector {candidate['detector']} v{candidate.get('detector_version')}")
    if session_id:
        parts.append(f"session {session_id}")
    traced = trace_note(candidate)
    said = agent_note(candidate)
    found = findings_note(candidate)
    return (" · ".join(parts) + (f"\n{found}" if found else "")
            + (f"\n{traced}" if traced else "")
            + (f"\n{said}" if said else "") + f"\nqc:{candidate['id']}")


def findings_note(candidate) -> str | None:
    """A consolidated ROI's findings, one clause each: what, in which
    channels, and how much of the ROI it covers when not all of it."""
    findings = candidate.get("findings") or []
    if len(findings) < 2 and not any(not f.get("primary") for f in findings):
        return None
    clauses = []
    seen = set()
    for finding in sorted(findings, key=lambda f: (not f.get("primary"), -f.get("share", 0))):
        words = schemas.CLASS_WORDS.get(finding["class"], finding["class"])
        channels = ", ".join(finding.get("channels") or []) or "all channels"
        key = (words, channels)
        if key in seen:
            continue
        seen.add(key)
        share = finding.get("share") or 0.0
        part = "" if share >= 0.95 else f" ({round(100 * share)}% of it)"
        clauses.append(f"{words} in {channels}{part}")
    return "Findings: " + "; ".join(clauses)


def agent_note(candidate) -> str | None:
    """One line of the agent's own notes on the region, with anything that
    reads as one of QC's tokens taken out (a note never relinks an ROI)."""
    from plexora.plugins.qc.server import provenance

    text = provenance.notes_text(candidate.get("notes"))
    if not text:
        return None
    text = CLASS_TOKEN.sub(" ", NOTE_TOKEN.sub(" ", text))
    text = " ".join(text.split())
    return f"agent: {text}" if text else None


def _fmt(value):
    try:
        return f"{float(value):.3g}"
    except (TypeError, ValueError):
        return "?"


TRACE_LINE = re.compile(r"^(?:traced|outline): ")


def trace_note(candidate) -> str | None:
    """One line on how the outline was made: traced (and how much of the
    envelope it keeps), or the envelope itself and why."""
    refinement = candidate.get("refinement") or {}
    status = refinement.get("status")
    if not status:
        return None
    if status == "refined":
        kept = refinement.get("kept_fraction")
        share = f", keeps {100 * kept:.0f} % of the envelope" if kept is not None else ""
        return f"traced: {refinement.get('method')}{share}"
    if status == "map":
        cell = refinement.get("refine_um")
        at = f" at {cell:.3g} um" if isinstance(cell, (int, float)) else ""
        return f"outline: {refinement.get('method')} score map{at}"
    return f"outline: envelope ({refinement.get('reason') or status})"


def _with_trace_note(notes, candidate):
    """`notes` with its tracing line replaced -- the user's own text kept,
    the line placed before the `qc:` link when there is one."""
    lines = [line for line in (notes or "").split("\n") if not TRACE_LINE.match(line)]
    traced = trace_note(candidate)
    if traced:
        at = next((i for i, line in enumerate(lines) if NOTE_TOKEN.search(line)), len(lines))
        lines.insert(at, traced)
    return "\n".join(lines).strip("\n")


def name_for(action, candidate) -> str:
    """A QC ROI's name: a consolidated one by its category and how many
    regions it holds (`schemas.layer_name`), any other by its class and
    channels."""
    if candidate.get("findings"):
        return schemas.layer_name(action, schemas.category_of_class(candidate["class"]),
                                  candidate["findings"])
    return schemas.roi_name(action, candidate["class"], list(candidate.get("channels") or []))


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
               "name": name_for(action, candidate),
               "geometry": candidate["geometry"],
               "notes": notes_for(candidate, session_id=session_id)}
    # An outline magic select made (the agent's, or the tracer's model method)
    # says so in the ROI panel too.
    if candidate.get("method") in ("sam", "sam_agent") \
            or (candidate.get("refinement") or {}).get("method") == "sam":
        feature["flags"] = {"self_intersecting": False, "method": "sam"}
    after = repo.apply(base, [{"op": "roi.create", "feature": feature}])
    _, summary = service.get_roi(ds, roi_id)
    return base, after, summary


def rename(ds, roi_id, action, candidate):
    """Change the action a QC ROI's name carries. Returns (before, after,
    old_name) or None when nothing changed."""
    from plexora.plugins.roi.server import service

    name = name_for(action, candidate)
    base, after, before_summary, _after = service.update_roi(ds, roi_id, name=name)
    if base == after:
        return None
    return base, after, before_summary["name"]


def _full(ds, roi_id):
    from plexora.plugins.roi.server import geometry as geometry_rules
    from plexora.plugins.roi.server import service

    return service.get_roi(ds, roi_id, max_vertices=geometry_rules.MAX_VERTICES)[1]


def undo_arguments(project, before, after_revision, *, reshaped):
    """`update_roi` arguments that put a region back as `before` was (the ROI
    plugin's own undo hint, restated): (arguments, partial)."""
    undo = {"project": project, "roi_id": before["id"], "name": before["name"],
            "notes": before["notes"], "category": before["category"],
            "visible": before["visible"], "locked": before["locked"],
            "base_revision": after_revision}
    partial = bool(reshaped and before.get("geometry") is None)
    if reshaped and not partial:
        undo["geometry"] = before["geometry"]
    return undo, partial


def update(ds, roi_id, action, candidate):
    """A QC ROI decided again: its name (action), category (class) and outline
    follow the new decision -- except what the user made theirs (a region they
    edited, relabelled, locked or approved is left as they have it).

    Returns None when nothing changed, else `{revision_before, revision_after,
    before, after, reshaped}` -- `before`/`after` the ROI summaries, `before`
    with its whole outline so the receipt's undo can put it back."""
    from plexora.plugins.roi.server import service

    meta = results.roi_meta(ds.name)
    row = next((r for r in meta.to_dicts() if r["roi_id"] == roi_id), None) \
        if meta.height else None
    if row and (row.get("user_edited") or row.get("approved") or row.get("locked")):
        if row.get("approved"):
            return None
        before = _full(ds, roi_id)
        renamed = rename(ds, roi_id, action, candidate)
        if renamed is None:
            return None
        return {"revision_before": renamed[0], "revision_after": renamed[1],
                "before": before, "after": _full(ds, roi_id), "reshaped": False}
    label = category_label(ds, candidate["class"])
    name = name_for(action, candidate)
    geometry = candidate.get("geometry")
    before_full = _full(ds, roi_id)
    base, after, before, now = service.update_roi(
        ds, roi_id, name=name, category=label,
        geometry=geometry, notes=_with_trace_note(before_full.get("notes"), candidate))
    if row is not None:
        row.update(action=action, **{"class": candidate["class"]},
                   written_category_id=now["category_id"],
                   written_geometry_hash=polygons.geometry_hash(geometry)
                   if geometry else row.get("written_geometry_hash"))
        results.upsert_roi_meta(ds.name, [row])
    if base == after:
        return None
    return {"revision_before": base, "revision_after": after, "before": before_full,
            "after": now, "reshaped": geometry is not None}


def retrace(ds, roi_id, candidate, geometry):
    """Give a QC region a new traced outline (and the note that says so),
    whoever shaped it last: a retrace is asked for, so it is the user's
    choice. The region is QC's shape again afterwards (`user_edited` off).

    Returns `{revision_before, revision_after, before, after}` -- `before`
    with its whole outline, for the receipt's undo -- or None when nothing
    changed."""
    from plexora.plugins.roi.server import service

    before_full = _full(ds, roi_id)
    notes = _with_trace_note(before_full.get("notes"), candidate)
    base, after, _before, now = service.update_roi(ds, roi_id, geometry=geometry, notes=notes)
    meta = results.roi_meta(ds.name)
    row = next((r for r in meta.to_dicts() if r["roi_id"] == roi_id), None) \
        if meta.height else None
    if row is not None:
        row.update(written_geometry_hash=polygons.geometry_hash(geometry), user_edited=False)
        results.upsert_roi_meta(ds.name, [row])
    if base == after:
        return None
    return {"revision_before": base, "revision_after": after, "before": before_full,
            "after": now}


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
    wins); returns {adopted, edited, deleted, relabelled, removed, locked}.

    A pre-categories document is migrated first (`migrated`); a read
    (`save=False`) writes nothing and reports the legacy category ids under
    `legacy` instead, so its caller can ask for a sync that writes."""
    report = {"adopted": [], "edited": [], "deleted": [], "relabelled": [], "removed": [],
              "locked": []}
    try:
        state = _repo(ds).load()
        if save:
            migrated = migrate_categories(ds, state=state)
            if migrated:
                report["migrated"] = migrated["moved"]
                state = _repo(ds).load()
        else:
            legacy = [c["id"] for c, _k in legacy_categories(state)]
            if legacy:
                report["legacy"] = legacy
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
        if feature["category_id"] != row.get("written_category_id"):
            # Moved within its own category (a migration's half-write, or the
            # "(QC)" twin): the subtype stands. Moved to another of the five:
            # the subtype the user named there (a legacy category's
            # `qc-class:` token), else that category's default class.
            named = class_token(feature.get("notes"))
            klass = class_in(feature["category_id"], labels, named) \
                if named and class_in(feature["category_id"], labels, named) == named \
                else class_in(feature["category_id"], labels, row.get("class"))
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
            else:
                row["written_category_id"] = feature["category_id"]
                row["removed_from_qc"] = False
                changed_rows.append(row)
                if candidate is not None:
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
            # A reshape says how it was made (magic select's refine, or a
            # vertex drag of a shape drawn with a known tool).
            drawn = (feature.get("flags") or {}).get("method")
            if drawn in schemas.REGION_METHODS:
                candidate["method"] = drawn
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
        if roi_id in known:
            continue
        # A subtype named when it was drawn is kept while it belongs here.
        klass = class_in(feature["category_id"], labels, class_token(feature.get("notes")))
        if klass is None:
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
            drawn = (feature.get("flags") or {}).get("method")
            if drawn in schemas.REGION_METHODS:
                candidate["method"] = drawn
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
        klass = class_in(feature["category_id"], labels, candidate.get("class")) \
            or candidate["class"]
        out.append({"roi_id": roi_id, "candidate_id": candidate["id"], "class": klass,
                    "category": category_of(feature["category_id"], labels)
                    or schemas.category_of_class(klass),
                    "category_id": feature["category_id"],
                    "category_label": labels.get(feature["category_id"]),
                    "name": feature.get("name") or "",
                    "created_by": candidate.get("created_by")
                    or (user.get("created_by") if isinstance(user, dict) else None),
                    "action": candidate.get("action") or "exclude",
                    "geometry": feature["geometry"],
                    "channels": list(candidate.get("channels") or [])})
    return out


# -- what each cell is a member of ------------------------------------------------------
#
# A consolidated ROI (`consolidate.py`) is one place on the tissue holding
# several findings -- a misregistration and the blur over part of it. The ROI
# is what the user sees and edits; the cells follow each FINDING, inside its
# own outline and clipped to the ROI as it stands now, with the finding's own
# class, channels and (current) action. Every other QC ROI is its own one
# finding. The membership keys are the ROI id, or `<roi id>#<n>` for the n-th
# finding of a consolidated one (`parent_roi_id` names the ROI).


def membership_meta(result) -> dict:
    """{key: candidate-like record} of every finding the cells follow."""
    candidates = (result or {}).get("candidates") or {}
    out = {}
    for candidate in candidates.values():
        roi_id = candidate.get("roi_id")
        user = candidate.get("user_state") or {}
        if not roi_id or user.get("deleted") or user.get("removed_from_qc"):
            continue
        findings = candidate.get("findings")
        if not findings:
            out[roi_id] = candidate
            continue
        for index, finding in enumerate(findings):
            source = candidates.get(finding.get("candidate_id")) or {}
            merged = {**finding, **{k: source[k] for k in ("action", "class", "scope",
                                                           "channels", "cycles")
                                    if source.get(k) is not None}}
            out[f"{roi_id}#{index}"] = {**merged, "id": finding.get("candidate_id"),
                                        "roi_id": f"{roi_id}#{index}",
                                        "parent_roi_id": roi_id,
                                        "geometry": finding.get("geometry")}
    return out


def parent_of(key) -> str:
    """The ROI a membership key belongs to."""
    return str(key).split("#", 1)[0]


def membership_regions(ds, result) -> list:
    """`live_regions` with every consolidated ROI expanded into its findings:
    [{roi_id (the membership key), parent_roi_id, geometry, class, action}]."""
    from plexora.plugins.qc.server import polygons

    live = {r["roi_id"]: r for r in live_regions(ds, result)}
    out = []
    for key, meta in membership_meta(result).items():
        parent = meta.get("parent_roi_id")
        region = live.get(parent or key)
        if region is None:
            continue
        if parent is None:
            out.append(region)
            continue
        geometry = polygons.clip_to(meta.get("geometry"), region["geometry"]) \
            if meta.get("geometry") else None
        if geometry is None:
            continue
        out.append({**region, "roi_id": key, "parent_roi_id": parent,
                    "candidate_id": meta.get("id"), "class": meta.get("class"),
                    "action": meta.get("action") or "exclude", "geometry": geometry,
                    "channels": list(meta.get("channels") or [])})
    return out
