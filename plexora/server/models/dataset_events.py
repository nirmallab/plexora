"""A project's cell table changed underneath Plexora: catch up, then tell the tabs.

Plexora reads a project's table, it does not own it. When the table is an
AnnData store that SCIMAP Pro (or a notebook, or plain `anndata`) also writes
-- a `phenotype` column after `sc.phenotype`, a neighbourhood label after
`sp.spatialCluster` -- nothing in Plexora used to notice: the list of `obs`
columns was recorded at registration, the metadata caches held on, and an open
tab offered yesterday's columns until it was reloaded by hand.

`on_dataset_changed(project, payload)` is the one place that catches up. The
payload names what changed as sections, in the shared protocol's vocabulary
(spatialbridge): `obs:<column>`, `obsm:<key>`, `uns:<key>`, `X`,
`layers:<name>`, `var`, or `file` when the whole store was replaced.

- `obs|obsm|uns` -> re-read the file's own vocabularies
  (`datasource.refresh_described_spec`, obs and var only, never the matrix)
  and drop the annotation caches (`data_model.forget_metadata`).
- `X|layers|var|file` -> reload the datasource when this process has it
  loaded; the matrix is what every gate and fit reads.
- then `revision.bump(project)`, so a remembered agent read is stale.

Called from three places: the bridge's `bridge_event` capability (a peer said
so), `POST /agent/v1/events` and `api.notify_viewers` when the event is
`core`/`dataset.changed` (the server's own caches, before the tabs hear of
it). The first passes `notify` so the tabs are told; the other two publish the
event themselves right after.
"""

from __future__ import annotations

KIND = "dataset.changed"
PLUGIN = "core"

#: Sections whose change leaves the matrix alone.
METADATA_PREFIXES = ("obs", "obsm", "uns")
#: Sections that change what every marker value reads.
MATRIX_PREFIXES = ("X", "layers", "var", "file")


def sections_of(payload) -> list:
    """The section names a payload carries, as strings; [] when it names none."""
    sections = (payload or {}).get("sections") if isinstance(payload, dict) else None
    if isinstance(sections, str):
        sections = [sections]
    return [str(s) for s in (sections or []) if str(s).strip()]


def _prefix(section: str) -> str:
    return section.split(":", 1)[0].split("/", 1)[0]


def classify(sections) -> dict:
    """{metadata, matrix, gates, rois}: what a change touches. No sections at
    all is read as "something in obs": the cheap refresh, never a reload."""
    sections = list(sections or [])
    prefixes = {_prefix(s) for s in sections}
    return {
        "metadata": not sections or bool(prefixes & set(METADATA_PREFIXES)),
        "matrix": bool(prefixes & set(MATRIX_PREFIXES)),
        "gates": any(s.startswith("uns:gates") for s in sections),
        "rois": any(s.startswith(("obs:rois_", "uns:plexora/rois", "obs:ROI"))
                    for s in sections),
    }


def on_dataset_changed(project, payload=None, *, notify=None) -> dict:
    """Bring this process up to date with a table that changed; see the module.

    `notify(project, plugin, kind, payload) -> int|bool` tells the open tabs
    (`api.notify_viewers`, or an attached server's link from an MCP process).
    None when the caller publishes the event itself.

    Never raises for a project it cannot refresh: the event is still worth
    delivering, and the answer says what was not done and why.
    """
    from plexora.server.models.project import Project

    payload = dict(payload or {})
    record = Project.find(project) if project else None
    if record is None:
        return {"handled": False, "project": project, "reason": "no such project"}
    sections = sections_of(payload)
    touched = classify(sections)
    out = {"handled": True, "project": project, "sections": sections, "refreshed": [],
           "reloaded": False, "errors": []}
    if touched["metadata"] and record.has_table:
        from plexora import datasource
        from plexora.server.models import data_model

        try:
            described = datasource.refresh_described_spec(project)
            if described is not None:
                out["refreshed"].append("obs_columns")
                out["obs_columns_added"] = described["added"]
                out["obs_columns_removed"] = described["removed"]
        except Exception as exc:  # a store mid-write, a node asleep: say so, carry on
            out["errors"].append(f"obs columns not re-read: {exc}")
        dropped = data_model.forget_metadata(project)
        out["refreshed"].append("metadata_cache")
        if dropped.get("record"):
            out["refreshed"].append("loaded_record")
    if touched["matrix"] and record.has_table:
        from plexora.server.models import data_model

        if data_model.is_loaded(project):
            try:
                data_model.load_datasource(project, reload=True)
                out["reloaded"] = True
            except Exception as exc:
                out["errors"].append(f"table not reloaded: {exc}")
    try:
        from plexora.agent import revision

        revision.bump(project)
    except OSError:
        pass
    if notify is not None:
        body = {**payload, "sections": sections, "refreshed": out["refreshed"],
                "reloaded": out["reloaded"],
                **({"obs_columns_added": out["obs_columns_added"]}
                   if out.get("obs_columns_added") else {})}
        try:
            out["delivered"] = int(notify(project, PLUGIN, KIND, body) or 0)
        except Exception as exc:
            out["delivered"] = 0
            out["errors"].append(f"open tabs not told: {exc}")
    return out


def is_dataset_changed(plugin, kind) -> bool:
    return plugin == PLUGIN and kind == KIND


def before_publish(project, plugin, kind, payload) -> None:
    """The hook `POST /agent/v1/events` and `api.notify_viewers` run before
    they publish: a `core`/`dataset.changed` event refreshes this server's
    caches first, so a tab that re-fetches on the event reads the new state.
    Anything else passes through untouched. Never raises."""
    if not is_dataset_changed(plugin, kind) or not project:
        return
    try:
        on_dataset_changed(project, payload)
    except Exception:  # the event still goes out; a tab can reload by hand
        pass
