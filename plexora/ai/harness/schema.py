"""Plexora's answer models as structured-output schemas, one per packet kind
and one per task.

The mechanism is spatialagents' (`spatialagents.schema`): a pydantic answer
model's JSON schema reduced to the subset providers' structured outputs
accept (`structured`), with every map sent as a list of `{key, value}`
entries that `decode` turns back into the object before local validation.
What is Plexora's is which model answers which packet kind -- gating's and
QC's `answers.BY_KIND` -- how one task's kinds share one schema, and how an
answer that comes in two forms is sent only the form its packet asks for.

The documented limits (24 optional, 16 unions) are not the provider's only
ones: a schema under both can still be refused, "Schema is too complex", a
400 that would end a run. So every schema Plexora can send stays inside what
the provider is known to compile (`VERIFIED`, a test gate), and one it
refuses anyway is asked for in words instead (`too_complex`, `in_words`, in
the gateway client): the answer is validated against the same model either
way.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

from spatialagents.schema import DROP, FORMATS, MAP_NOTE, MAX_OPTIONAL, MAX_UNIONS, decode, structured

__all__ = ["DROP", "FORMATS", "IN_WORDS", "MAP_NOTE", "MAX_OPTIONAL", "MAX_UNIONS", "VERIFIED", "complexity",
           "decode", "for_form", "for_kind", "for_packet", "for_task", "in_words", "model_schema", "refuse",
           "refused", "sendable", "structured", "task_fields", "task_kinds", "too_complex"]

#: [cal] The largest answer schemas the provider is known to compile, by
#: spatialagents' own count (optional properties, unions): gating.planning's
#: task schema, 20 and 7, answered since 2026-10-15. Unions cost the most --
#: their compilation grows exponentially -- and QC's artifact_confirm with
#: both its forms at once, 17 and 10 (under the documented 24 and 16), was
#: refused on 2026-10-28. A schema past these is refused by the tests until
#: the provider has compiled it (tools/ai_schema_check.py) and they are raised.
VERIFIED = {"optional": 20, "unions": 7}


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
    (`gating` or `qc`), memoised so it is byte-stable. None for a kind whose
    answer has forms: it is only ever sent one of them (`for_form`)."""
    key = (workflow, kind)
    if key not in _CACHE:
        source = model_schema(kind, workflow)
        _CACHE[key] = structured(source) if source is not None and not _forms(kind, workflow) else None
    return _CACHE[key]


# -- an answer's forms -----------------------------------------------------------------------
#
# An answer model may come in forms (`FORMS`: name -> its fields, the first of
# which names the form), each a whole answer -- QC's artifact_confirm is one
# candidate's fields, or `verdicts` by label for a batch -- and say which a
# packet asks for (`form_of(packet)`). The provider is sent that form alone:
# the question, the evidence and the validation are the same, and a schema
# of every form at once is one the provider may not compile.


def _forms(kind: str, workflow: str) -> dict:
    return getattr(_models(workflow).get(kind), "FORMS", None) or {}


def _not_null(node: dict) -> dict:
    """`node` without its `null` alternative (and its null default)."""
    if not node.get("anyOf"):
        return node
    branches = [b for b in node["anyOf"] if b != {"type": "null"}]
    rest = {k: v for k, v in node.items() if k not in ("anyOf", "default")}
    return {**branches[0], **rest} if len(branches) == 1 else {**rest, "anyOf": branches}


def _narrowed(source: dict, fields: tuple) -> dict:
    """`source` with only `fields` (and `kind`); the first names the form,
    so it is required and never null."""
    properties = {name: node for name, node in (source.get("properties") or {}).items()
                  if name in fields or name == "kind"}
    properties[fields[0]] = _not_null(properties[fields[0]])
    required = [name for name in source.get("required") or () if name in properties]
    return {**source, "properties": properties,
            "required": required + ([fields[0]] if fields[0] not in required else [])}


_FORM_CACHE: dict = {}


def for_form(kind: str, form: str, workflow: str = "gating") -> dict | None:
    """The provider-ready schema of one form of a kind's answer (memoised)."""
    key = (workflow, kind, form)
    if key not in _FORM_CACHE:
        _FORM_CACHE[key] = structured(_narrowed(model_schema(kind, workflow),
                                                tuple(_forms(kind, workflow)[form])))
    return _FORM_CACHE[key]


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
        if source is None or _forms(kind, workflow):
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


def for_packet(kind: str, task_id: str | None, workflow: str = "gating",
               packet: dict | None = None) -> dict | None:
    """What a packet of `kind` in task `task_id` is sent: the form `packet`
    asks for when its answer has forms, else its task's schema when the task
    has one that covers the kind, else its own kind's. None for one the
    provider refused to compile (`refuse`): the packet asks in words."""
    forms = _forms(kind, workflow)
    if forms and packet is not None:
        chosen = for_form(kind, _models(workflow)[kind].form_of(packet), workflow)
    else:
        shared = for_task(task_id, workflow)
        chosen = shared if shared is not None and kind in shared["properties"]["kind"]["enum"] \
            else for_kind(kind, workflow)
    return None if refused(chosen) else chosen


def complexity(schema: dict) -> dict:
    """{optional, unions}: a provider-ready schema's count of the two
    things the provider limits (spatialagents' own measure)."""
    from spatialagents.schema import _count

    tally = {"optional": 0, "unions": 0}
    _count(schema, tally)
    return tally


def sendable() -> list:
    """[(label, schema)]: every schema the harness can send -- each kind's,
    each form of a kind that has forms, each task's shared one."""
    from plexora.ai import tasks

    out = []
    for workflow in ("gating", "qc"):
        for kind in sorted(_models(workflow)):
            for form in _forms(kind, workflow):
                out.append((f"{workflow}:{kind}:{form}", for_form(kind, form, workflow)))
            if not _forms(kind, workflow):
                out.append((f"{workflow}:{kind}", for_kind(kind, workflow)))
        for task_id in sorted(tasks.tasks()):
            shared = for_task(task_id, workflow)
            if shared is not None:
                out.append((f"{workflow}:task:{task_id}", shared))
    return [(label, s) for label, s in out if s is not None]


# -- a schema the provider will not compile ---------------------------------------------------

#: Fingerprints of the schemas the provider refused to compile in this
#: process: never sent again (`for_packet` gives None, so the packet asks in
#: words, as for a schema past the documented limits).
_REFUSED: set = set()
#: What the words fallback says before the schema (`in_words`).
IN_WORDS = ("Answer with one JSON object and nothing else. It is checked against this JSON "
            "schema: ")


def _fingerprint(schema: dict) -> str:
    return hashlib.sha1(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def too_complex(error) -> bool:
    """Whether a gateway refusal is the provider declining to compile the
    request's schema (a 400 relayed as `provider_rejected`)."""
    return getattr(error, "code", None) == "provider_rejected" \
        and "schema is too complex" in str(error).lower()


def refuse(schema: dict) -> None:
    _REFUSED.add(_fingerprint(schema))


def refused(schema: dict | None) -> bool:
    return bool(_REFUSED) and schema is not None and _fingerprint(schema) in _REFUSED


def in_words(request):
    """`request` with its output schema asked for in words, at the end of the
    last user turn: the same answer, which is validated as ever."""
    messages = [dict(m) for m in request.messages]
    last = messages[-1]
    content = last.get("content")
    blocks = list(content) if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
    blocks.append({"type": "text", "text": IN_WORDS + json.dumps(request.output_schema, sort_keys=True,
                                                                 separators=(",", ":"))})
    messages[-1] = {**last, "content": blocks}
    return dataclasses.replace(request, output_schema=None, messages=messages)
