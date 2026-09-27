"""Which expression matrix a project reads, and whether log1p is applied.

One module for the question every surface asks the same way -- the project
edit page, the requirements modal, a dataset's per-image form and an agent
(`set_expression_source`) -- so the two writes it takes (the matrix, the
transform) and the reload that makes them visible cannot drift apart.

A choice is spelled as the picker words it: `"X"` for the main matrix,
`"layer:<name>"` for one of `adata.layers`.
"""

from __future__ import annotations

import numpy as np


def parse_source(value) -> dict | None:
    """The read spec's `features` dict for a picker value, or None when the
    value is neither (`Project.with_feature_source` then leaves the spec)."""
    value = (value or "").strip()
    if value == "X":
        return {"source": "X"}
    if value.startswith("layer:") and value.split(":", 1)[1]:
        return {"source": "layer", "layer": value.split(":", 1)[1]}
    return None


def _with_known_layers(project):
    from plexora.server.models.adapters.inspection import source_layers

    if project.dataset and not project.dataset.layers:
        return project.with_layers(source_layers(project.dataset))
    return project


def options(project) -> list[dict]:
    """Every matrix the project can be read from, as `{value, label}`. A flat
    table has exactly one: its own values."""
    if not project.has_table:
        return []
    listed = _with_known_layers(project).feature_options
    return listed or [{"value": "X", "label": "the table's values"}]


def current(project) -> dict:
    return {"features_layer": project.feature_source,
            "features_log": bool(project.log_transformed),
            "confirmed": "features" in set(project.confirmed)}


def sample(project, source, n, seed=0) -> dict:
    """{marker: ndarray} of a seeded sample of one matrix, as stored (no log1p).
    `{}` when the table cannot be read here (a node-bound file)."""
    from plexora.server.models import adapters

    spec = project.dataset
    if spec is None:
        return {}
    try:
        values = adapters.sample_features(spec, source, n, seed)
    except (OSError, ValueError, KeyError, NotImplementedError, AttributeError):
        return {}
    markers = set(project.dataset.columns.markers or ()) if project.dataset.columns else set()
    if markers:
        values = {k: v for k, v in values.items() if k in markers} or values
    return {k: np.asarray(v, dtype=np.float32) for k, v in values.items()}


def apply(name, features_layer=None, features_log=None, *, confirm=False, data_root=None):
    """Point project `name` at a matrix and/or switch log1p, in one write;
    confirm the `features` requirement when asked. Returns (before, after,
    changed) as `current()` dicts; reloads the viewer's datasource when it is
    this project."""
    from plexora.server.models.project import Project

    project = Project.find(name, data_root)
    if project is None:
        raise KeyError(name)
    before = current(project)
    wants_layer = bool(features_layer) and features_layer != project.feature_source
    wants_log = features_log is not None and bool(features_log) != project.log_transformed
    layers = None
    if wants_layer and features_layer != "X":
        from plexora.server.models.adapters.inspection import source_layers

        layers = source_layers(project.dataset)

    def _change(record):
        if wants_layer:
            if layers is not None:
                record = record.with_layers(layers)
            record = record.with_feature_source(features_layer)
        if wants_log:
            record = record.with_log_transform(bool(features_log))
        if confirm:
            record = record.with_confirmed(("features",))
        return record

    updated = Project.mutate(name, _change, data_root)
    after = current(updated) if updated is not None else before
    changed = wants_layer or wants_log
    if changed:
        reload_if_loaded(name)
    return before, after, changed


def apply_choice(project, payload) -> bool:
    """A form's `features_layer` / `features_log` answer (the project edit
    page, the requirements modal, a dataset's image form). Returns whether
    anything changed -- both halves change what every number in the app is, so
    either one needs the datasource re-read, which the caller does."""
    from plexora.server.models.project import Project

    layer = payload.get("features_layer")
    has_log = "features_log" in payload
    log = bool(payload.get("features_log"))
    wants_layer = bool(layer) and layer != project.feature_source
    wants_log = has_log and log != project.log_transformed
    if not wants_layer and not wants_log:
        return False
    from plexora.server.models.adapters.inspection import source_layers

    # Read outside the write lock: the layer list may come off disk.
    layers = source_layers(project.dataset) if wants_layer else None

    def _change(current_):
        # The available list goes in first, so a project imported before it was
        # recorded validates the choice against the file.
        if wants_layer:
            current_ = current_.with_layers(layers).with_feature_source(layer)
        if wants_log:
            current_ = current_.with_log_transform(log)
        return current_

    Project.mutate(project.name, _change)
    return True


def reload_if_loaded(name) -> bool:
    """Re-read the viewer's datasource when it is project `name` (what an
    agent's change needs to reach an open tab). False when another project
    (or none) is loaded."""
    try:
        from plexora.server.models import data_model
    except Exception:  # pragma: no cover - a process without the viewer
        return False
    if not data_model.is_loaded(name):
        return False
    try:
        data_model.load_datasource(name, reload=True)
    except ValueError:
        return False
    return True
