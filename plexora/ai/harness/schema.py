"""Plexora's answer models as structured-output schemas, one per packet kind
and one per task.

The mechanism is spatialagents' (`spatialagents.schema`): a pydantic answer
model's JSON schema reduced to the subset providers' structured outputs
accept (`structured`), with every map sent as a list of `{key, value}`
entries that `decode` turns back into the object before local validation.
What is Plexora's is which model answers which packet kind -- gating's and
QC's `answers.BY_KIND` -- and how one task's kinds share one schema.
"""

from __future__ import annotations

from spatialagents.schema import DROP, FORMATS, MAP_NOTE, MAX_OPTIONAL, MAX_UNIONS, decode, structured

__all__ = ["DROP", "FORMATS", "MAP_NOTE", "MAX_OPTIONAL", "MAX_UNIONS", "decode", "for_kind", "for_packet",
           "for_task", "model_schema", "structured", "task_fields", "task_kinds"]


def _models(workflow: str) -> dict:
    if workflow == "qc":
        from plexora.plugins.qc.server import answers
    else:
        from plexora.plugins.gating.server.autogate import answers
    return answers.BY_KIND


_CACHE: dict = {}
_MODEL_SCHEMAS: dict = {}


def model_schema(kind: str, workflow: str = "gating") -> dict | None:
    """The answer model's own JSON schema for a packet kind (memoised)."""
    key = (workflow, kind)
    if key not in _MODEL_SCHEMAS:
        model = _models(workflow).get(kind)
        _MODEL_SCHEMAS[key] = model.model_json_schema() if model is not None else None
    return _MODEL_SCHEMAS[key]


def for_kind(kind: str, workflow: str = "gating") -> dict | None:
    """The provider-ready answer schema for a packet kind of a workflow
    (`gating` or `qc`), memoised so it is byte-stable."""
    key = (workflow, kind)
    if key not in _CACHE:
        source = model_schema(kind, workflow)
        _CACHE[key] = structured(source) if source is not None else None
    return _CACHE[key]


# -- one schema per task ---------------------------------------------------------------------
#
# On Anthropic the output schema is part of the cached prefix, ahead of the
# system prompt: two packet kinds with two schemas are two prefixes, and a
# worker that answers a t2 and then a t3 packet re-writes its whole history
# at the second (2026-10-03 benchmark: every first call of a kind read 0).
# A task's kinds -- image inspection's t1/t2/t3/QC looks -- are therefore sent
# ONE schema: `kind` an enum of them, every field of any of them, a field
# required only where every kind requires it. Each answer is still validated
# against its own kind's model. A task whose union does not fit the limits
# keeps a schema per kind.


def _kinds_of(task_id: str, workflow: str) -> tuple:
    from plexora.ai import tasks

    task = tasks.tasks().get(task_id or "")
    if task is None:
        return ()
    known = _models(workflow)
    return tuple(dict.fromkeys(k.split(":", 1)[0] for k in task.kinds if k.split(":", 1)[0] in known))


def _merge(kinds: tuple, workflow: str) -> dict | None:
    """The answer models of `kinds` as one JSON schema, or None when two of
    them give one field different shapes."""
    properties: dict = {}
    defs: dict = {}
    owners: dict = {}
    required = None
    for kind in kinds:
        source = model_schema(kind, workflow)
        if source is None:
            return None
        for name, node in (source.get("$defs") or {}).items():
            if defs.setdefault(name, node) != node:
                return None
        for name, node in (source.get("properties") or {}).items():
            if name == "kind":
                continue
            node = {k: v for k, v in node.items() if k != "title"}
            if properties.setdefault(name, node) != node:
                return None
            owners.setdefault(name, []).append(kind)
        mine = set(source.get("required") or ()) - {"kind"}
        required = mine if required is None else required & mine
    for name, users in owners.items():
        if len(users) < len(kinds):
            only = ", ".join(users)
            properties[name] = {**properties[name], "description": " ".join(
                p for p in (properties[name].get("description"), f"Only for kind {only}.") if p)}
    merged = {"type": "object", "properties": {"kind": {"type": "string", "enum": list(kinds)}, **properties},
              "required": ["kind", *sorted(required or ())]}
    if defs:
        merged["$defs"] = defs
    return merged


_TASK_CACHE: dict = {}


def for_task(task_id: str | None, workflow: str = "gating") -> dict | None:
    """The provider-ready schema every packet of a task is sent (memoised, so
    byte-stable), or None when the task has one kind or its kinds do not fit
    one schema: the caller then sends `for_kind`."""
    key = (workflow, task_id)
    if key not in _TASK_CACHE:
        kinds = _kinds_of(task_id, workflow)
        merged = _merge(kinds, workflow) if len(kinds) > 1 else None
        _TASK_CACHE[key] = structured(merged) if merged is not None else None
    return _TASK_CACHE[key]


def task_kinds(task_id: str | None, workflow: str = "gating") -> tuple:
    """The kinds that share `task_id`'s one schema, or () when it has none."""
    shared = for_task(task_id, workflow)
    return tuple(shared["properties"]["kind"]["enum"]) if shared is not None else ()


def task_fields(task_id: str | None, workflow: str = "gating") -> set:
    """Every field the task's one schema offers (any of its kinds'), or none."""
    shared = for_task(task_id, workflow)
    return set(shared["properties"]) - {"kind"} if shared is not None else set()


def for_packet(kind: str, task_id: str | None, workflow: str = "gating") -> dict | None:
    """What a packet of `kind` in task `task_id` is sent: its task's schema
    when the task has one that covers the kind, else its own kind's."""
    shared = for_task(task_id, workflow)
    if shared is not None and kind in shared["properties"]["kind"]["enum"]:
        return shared
    return for_kind(kind, workflow)
