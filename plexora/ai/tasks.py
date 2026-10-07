"""The AI tasks Plexora asks a model to do, by module (`tasks.yaml`).

A gateway call names its task so the BioCognia gateway can serve each kind of
work with the model an administrator assigned to it. Inside Plexora a task is
`module.task` (`gating.threshold_evaluation`, `qc.blur`); on the wire it is
`plexora.<module>.<task>` (`wire_id`), because one gateway serves every
BioCognia product and routes by the longest match (`plexora.gating.*`,
`plexora.*`, `*`). The task is resolved from the module and the
decision-packet kind (`task_for`); a kind the registry does not map is sent as
`plexora.<module>.default`, which the gateway serves at the product's default
and records.

The registry names what a task needs (vision, reasoning), never a model or a
vendor. This file's `tasks.yaml` is the one editable source (invariant 15):
`fragment()` is what `tools/bioc_sync.py` uploads to the gateway's registry
under the `plexora` key, and `--check` fails when the gateway's copy differs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REGISTRY_PATH = Path(__file__).parent / "tasks.yaml"

#: A task id inside Plexora: `module.task`, lower snake case.
TASK_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$")
#: The product key every task is filed under on the gateway: the certificate's `aud`.
PRODUCT = "plexora"
#: A task id on the wire: `plexora.module.task` (the gateway's WIRE_TASK).
WIRE_TASK = re.compile(r"^[a-z][a-z0-9_]{1,31}\.[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$")
#: What a call whose kind no task maps is sent as, under its module.
FALLBACK_TASK = "default"
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
    #: Whether an agent driving a session over MCP ever answers it (a packet
    #: of the session); False for a call only Plexora's harness makes.
    mcp: bool = True


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
                                effort=effort, mcp=bool(t.get("mcp", True)))
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


def wire_id(task_id: str | None, *, module: str | None = None) -> str:
    """`gating.planning` as the gateway knows it: `plexora.gating.planning`.

    None (a packet kind no task maps) is `plexora.<module>.default`; the
    gateway serves an unknown task at the product's default and records it,
    so a Plexora release never waits for a gateway deploy."""
    if task_id and TASK_ID.match(task_id):
        return f"{PRODUCT}.{task_id}"
    head = module if isinstance(module, str) and re.match(r"^[a-z][a-z0-9_]{0,31}$", module) \
        else "app"
    return f"{PRODUCT}.{head}.{FALLBACK_TASK}"


def fragment() -> dict:
    """The registry as the gateway's `PUT /admin/api/ai/registry/plexora` takes
    it (`Fragment`, workers/ai/src/ai/tasks.ts in the platform):
    `{modules: {<module>: {label, tasks: {<task>: {label, blurb, max_tokens,
    effort, requires: {vision, reasoning}, kinds}}}}}`. Module and task order
    is the registry's; the admin page lists them in it. No capability class:
    the gateway derives that from `requires`."""
    registry = _registry()
    modules = {}
    for module, spec in (registry.get("modules") or {}).items():
        modules[module] = {"label": spec.get("label", module), "tasks": {}}
        for task in tasks().values():
            if task.module != module:
                continue
            modules[module]["tasks"][task.name] = {
                "label": task.label, "blurb": task.blurb, "max_tokens": task.max_tokens,
                "effort": task.effort, "requires": {"vision": task.vision, "reasoning": task.reasoning},
                "kinds": list(task.kinds)}
    return {"version": registry.get("version", 1), "modules": modules}


def to_json() -> str:
    """`fragment()` as text: deterministic, one trailing newline."""
    return json.dumps(fragment(), indent=2, ensure_ascii=False) + "\n"
