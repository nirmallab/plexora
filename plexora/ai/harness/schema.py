"""Answer models -> a JSON schema the provider's structured outputs accept.

Structured outputs take a subset of JSON Schema: every object closed
(`additionalProperties: false`), no numeric or length constraints, no external
or recursive `$ref`, at most 24 optional properties and 16 unions per request.
The pydantic schema is reduced to that subset; the constraints it loses are
still enforced, because every answer is validated locally against the real
model before it is submitted. A schema that cannot fit the limits is not sent
at all (`None`): the packet then asks for JSON in words, and local validation
does the rest.

Maps (`dict[str, X]`: verdicts keyed by marker or channel, modules keyed by
name) have no closed form -- their keys are the packet's -- so a map is sent
as a list of `{key, value}` entries and `decode` turns such a list back into
the object the answer model expects before local validation. An answer that
already gives the object is left alone.
"""

from __future__ import annotations

import copy

DROP = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength",
        "maxLength", "maxItems", "title", "discriminator", "examples", "uniqueItems", "minProperties",
        "maxProperties", "propertyNames"}
FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
MAX_OPTIONAL = 24
MAX_UNIONS = 16


#: What a map's entries are called in the provider schema.
MAP_NOTE = "One entry per key, as {key, value}."


def _map_value(node):
    """The value schema of a map node (`additionalProperties: {...}` and no
    named properties), else None."""
    extra = node.get("additionalProperties") if isinstance(node, dict) else None
    return extra if isinstance(extra, dict) and not node.get("properties") else None


def _resolve(node, defs):
    while isinstance(node, dict) and "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        if name not in defs:
            raise ValueError(f"unresolvable $ref {node['$ref']}")
        node = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
    return node


def _inline(node, defs, depth=0):
    if depth > 24:
        raise ValueError("schema nests too deeply (recursive?)")
    if isinstance(node, list):
        return [_inline(item, defs, depth + 1) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _inline(_resolve(node, defs), defs, depth + 1)
    value = _map_value(node)
    if value is not None:
        described = " ".join(p for p in (node.get("description"), MAP_NOTE) if p)
        return {"type": "array", "description": described,
                "items": {"type": "object", "additionalProperties": False, "required": ["key", "value"],
                          "properties": {"key": {"type": "string"},
                                         "value": _inline(value, defs, depth + 1)}}}
    out = {}
    for key, value in node.items():
        if key in DROP or key in ("$defs", "definitions"):
            continue
        if key == "format" and value not in FORMATS:
            continue
        if key == "minItems" and isinstance(value, int) and value > 1:
            out[key] = 1
            continue
        if key == "pattern":
            continue        # regex support is partial; local validation enforces it
        if key == "default" and value is None:
            continue
        out[key] = _inline(value, defs, depth + 1) if key not in ("enum", "const", "required") else value
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
    return out


def _count(node, tally):
    if isinstance(node, list):
        for item in node:
            _count(item, tally)
        return
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict):
        required = set(node.get("required") or ())
        tally["optional"] += sum(1 for name in props if name not in required)
    if "anyOf" in node or isinstance(node.get("type"), list):
        tally["unions"] += 1
    for value in node.values():
        _count(value, tally)


def structured(model_schema: dict) -> dict | None:
    """The provider-ready schema, or None when it cannot fit the limits."""
    try:
        schema = _inline(model_schema, model_schema.get("$defs") or model_schema.get("definitions") or {})
    except ValueError:
        return None
    tally = {"optional": 0, "unions": 0}
    _count(schema, tally)
    if tally["optional"] > MAX_OPTIONAL or tally["unions"] > MAX_UNIONS:
        return None
    return schema


def _entries(value) -> bool:
    return isinstance(value, list) and all(isinstance(e, dict) and set(e) <= {"key", "value"} and "key" in e
                                           for e in value)


def _decode(value, node, defs, depth=0):
    if depth > 24 or not isinstance(node, dict):
        return value
    node = _resolve(node, defs)
    if "anyOf" in node:
        for branch in node["anyOf"]:
            branch = _resolve(branch, defs)
            if _map_value(branch) is not None and _entries(value):
                return _decode(value, branch, defs, depth + 1)
        for branch in node["anyOf"]:
            branch = _resolve(branch, defs)
            if isinstance(value, dict) and (branch.get("properties") or _map_value(branch) is not None):
                return _decode(value, branch, defs, depth + 1)
            if isinstance(value, list) and branch.get("type") == "array":
                return _decode(value, branch, defs, depth + 1)
        return value
    inner = _map_value(node)
    if inner is not None:
        if _entries(value):
            value = {str(e["key"]): e.get("value") for e in value}
        if isinstance(value, dict):
            return {k: _decode(v, inner, defs, depth + 1) for k, v in value.items()}
        return value
    props = node.get("properties")
    if isinstance(props, dict) and isinstance(value, dict):
        return {k: _decode(v, props[k], defs, depth + 1) if k in props else v for k, v in value.items()}
    if node.get("type") == "array" and isinstance(value, list) and isinstance(node.get("items"), dict):
        return [_decode(v, node["items"], defs, depth + 1) for v in value]
    return value


def decode(answer, model_schema: dict):
    """An answer given against `structured(model_schema)` back in the answer
    model's own shape: every map sent as `{key, value}` entries is an object
    again. Anything else passes through unchanged."""
    return _decode(answer, model_schema, model_schema.get("$defs") or model_schema.get("definitions") or {})


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
