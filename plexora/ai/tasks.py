"""The AI tasks Plexora asks a model to do, by module (`tasks.yaml`).

A gateway call names its task -- `gating.threshold_evaluation`, `qc.blur` --
so the gateway can serve each kind of work with the model an administrator
assigned to it. The task is resolved from the module and the decision-packet
kind (`task_for`); a kind the registry does not map sends no task, and the
gateway serves it at the module's default.

The registry names what a task needs (vision, reasoning), never a model or a
vendor. `to_json()` is what `tools/ai_tasks_sync.py` writes into the licence
Worker (licensing/src/ai/tasks.json), so both sides read one list.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REGISTRY_PATH = Path(__file__).parent / "tasks.yaml"

#: A task id on the wire: `module.task`, lower snake case (the gateway's own check).
TASK_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$")
#: A task's default effort; the gateway maps it onto each model's own levels.
EFFORTS = ("low", "medium", "high")


@dataclass(frozen=True)
class Task:
    id: str
    module: str
    name: str
    label: str
    blurb: str
    vision: bool
    reasoning: bool
    capability: str
    max_tokens: int
    kinds: tuple[str, ...]
    effort: str = "medium"


@lru_cache(maxsize=1)
def _registry() -> dict:
    import yaml

    with open(REGISTRY_PATH, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {"version": 0, "modules": {}}


@lru_cache(maxsize=1)
def tasks() -> dict[str, Task]:
    """Every task, by id, in registry order."""
    out: dict[str, Task] = {}
    for module, spec in (_registry().get("modules") or {}).items():
        for name, t in (spec.get("tasks") or {}).items():
            requires = t.get("requires") or {}
            task_id = f"{module}.{name}"
            effort = str(t.get("effort", "medium"))
            if effort not in EFFORTS:
                raise ValueError(f"{task_id}: effort is one of {', '.join(EFFORTS)}, not {effort!r}")
            out[task_id] = Task(id=task_id, module=module, name=name, label=t.get("label", name),
                                blurb=t.get("blurb", ""), vision=bool(requires.get("vision")),
                                reasoning=bool(requires.get("reasoning")), capability=t["capability"],
                                max_tokens=int(t["max_tokens"]), kinds=tuple(str(k) for k in t.get("kinds") or ()),
                                effort=effort)
    return out


@lru_cache(maxsize=1)
def _by_kind() -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for task in tasks().values():
        for kind in task.kinds:
            index[(task.module, kind)] = task.id
    return index


def task_for(module: str, kind: str, **fields) -> str | None:
    """The task a packet of this kind belongs to, or None when the registry does not map it.

    A kind with a discriminating field (`check` on a QC score review) is tried
    as `kind:value` first, then as the bare kind."""
    index = _by_kind()
    for name, value in fields.items():
        if value is not None and (module, f"{kind}:{value}") in index:
            return index[(module, f"{kind}:{value}")]
    return index.get((module, kind))


def to_json() -> str:
    """The registry as the licence Worker reads it: deterministic, one trailing newline."""
    registry = _registry()
    modules = {}
    for module, spec in (registry.get("modules") or {}).items():
        modules[module] = {"label": spec.get("label", module), "tasks": {}}
        for task in tasks().values():
            if task.module != module:
                continue
            modules[module]["tasks"][task.name] = {
                "label": task.label, "blurb": task.blurb, "capability": task.capability,
                "max_tokens": task.max_tokens, "effort": task.effort,
                "requires": {"vision": task.vision, "reasoning": task.reasoning}, "kinds": list(task.kinds)}
    # Not sorted: module and task order is the registry's, and the admin page lists them in it.
    body = {"version": registry.get("version", 1), "modules": modules}
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"
