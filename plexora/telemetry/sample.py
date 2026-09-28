"""A synthetic batch with one of every event, built from the schema itself.

It is the cross-language contract: `plexora telemetry sample --fixture`
writes it to `backend/test/fixtures/sample-batch.json`, and the Worker's
`fixture.test.ts` must accept every event in it. A schema change that the
Worker has not caught up with fails there, not in production.
"""

from __future__ import annotations

import json
from pathlib import Path

from plexora.telemetry import schema

FIXTURE = Path("backend") / "test" / "fixtures" / "sample-batch.json"
WINDOW = "2026-09-27T14"


def example(type_):
    if isinstance(type_, schema.Enum):
        return type_.values[min(1, len(type_.values) - 1)]
    if isinstance(type_, schema.Pattern):
        if type_.example is None:
            raise ValueError(f"pattern {type_.regex!r} has no example")
        return type_.example
    if isinstance(type_, schema.Int):
        return min(3, type_.maximum)
    if isinstance(type_, schema.Bool):
        return True
    if isinstance(type_, schema.Hist):
        return [3, 5, 2, 1, 0, 0, 0, 0, 1]
    if isinstance(type_, schema.ListOf):
        return [example(type_.of)]
    if isinstance(type_, schema.MapOf):
        return {example(type_.key): example(type_.value)}
    if isinstance(type_, schema.Struct):
        return {name: example(sub) for name, sub in type_.fields.items()}
    raise TypeError(type_)


def events(mode=schema.DIAGNOSTICS) -> list:
    found = []
    for name, spec in schema.EVENTS.items():
        if isinstance(spec, schema.Record):
            props = {key: example(field.type) for key, field in spec.props.items()
                     if schema.mode_allows(field.mode, mode)}
            found.append({"type": name, "window": WINDOW, "props": props})
            continue
        rows = []
        for key, key_spec in spec.keys.items():
            if not schema.mode_allows(key_spec.mode, mode):
                continue
            dims = {dim: example(field.type) for dim, field in key_spec.dims.items()
                    if schema.mode_allows(field.mode, mode)}
            row = {"k": key, "d": dims, "n": 12}
            if key_spec.agg == "hist":
                row["n"] = 12
                row["h"] = [3, 5, 2, 1, 0, 0, 0, 0, 1]
                row["s"] = 9876.5
                row["mx"] = 6012.0
            rows.append(row)
        found.append({"type": name, "window": WINDOW, "rows": rows})
    return found


def client(mode=schema.DIAGNOSTICS) -> dict:
    return {key: example(field.type) for key, field in schema.CLIENT_BLOCK.items()
            if key not in schema.RESERVED_LICENSE_FIELDS} | {"mode": mode}


def body(mode=schema.DIAGNOSTICS) -> dict:
    return {"schema": schema.SCHEMA_VERSION, "batch_id": "ab" * 16,
            "client": client(mode), "events": events(mode)}


def write_fixture(root=".") -> Path:
    path = Path(root) / FIXTURE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path
