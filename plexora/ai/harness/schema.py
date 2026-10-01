"""Answer models -> a JSON schema the provider's structured outputs accept.

Structured outputs take a subset of JSON Schema: every object closed
(`additionalProperties: false`), no numeric or length constraints, no external
or recursive `$ref`, at most 24 optional properties and 16 unions per request.
The pydantic schema is reduced to that subset; the constraints it loses are
still enforced, because every answer is validated locally against the real
model before it is submitted. A schema that cannot fit the limits is not sent
at all (`None`): the packet then asks for JSON in words, and local validation
does the rest.
"""

from __future__ import annotations

import copy

DROP = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength",
        "maxLength", "maxItems", "title", "discriminator", "examples", "uniqueItems", "minProperties",
        "maxProperties", "propertyNames"}
FORMATS = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
MAX_OPTIONAL = 24
MAX_UNIONS = 16


def _inline(node, defs, depth=0):
    if depth > 24:
        raise ValueError("schema nests too deeply (recursive?)")
    if isinstance(node, list):
        return [_inline(item, defs, depth + 1) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        if name not in defs:
            raise ValueError(f"unresolvable $ref {node['$ref']}")
        merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
        return _inline(merged, defs, depth + 1)
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


_CACHE: dict = {}


def for_kind(kind: str) -> dict | None:
    """The gating answer schema for a packet kind (memoised: byte-stable)."""
    if kind not in _CACHE:
        from plexora.plugins.gating.server.autogate import answers

        model = answers.BY_KIND.get(kind)
        _CACHE[kind] = structured(model.model_json_schema()) if model is not None else None
    return _CACHE[kind]
