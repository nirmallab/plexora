"""The user's task -> model mapping for agents that drive Plexora over MCP.

A run Plexora makes itself (`ai_run_session`) is served by the gateway, which
an administrator routes per task. A session an agent drives over MCP is
answered by the agent's own models, which Plexora cannot choose -- but the
user can, in one hand-edited file:

    gating:
      default: <model>             # any gating task not named below
      threshold_evaluation: <model>
    qc:
      artifact_inspection: <model>

The task names are the gateway's (`tasks.yaml`), so one vocabulary serves
both. A value is passed to the agent as it is -- a name *its* client
understands; Plexora never names a model. A task left out (and no module
`default`) is the agent's choice, as it is with no file at all.

The file is `ai-models.yaml` beside the settings file; `PLEXORA_AI_MODELS`
names another (a repository can pin its own through its MCP config's `env`).
A malformed file never stops a session: what cannot be read is reported in
`problems` and ignored.
"""

from __future__ import annotations

import os
from pathlib import Path

FILENAME = "ai-models.yaml"
ENV_PATH = "PLEXORA_AI_MODELS"
#: A module's catch-all key: every task of the module not named on its own.
DEFAULT = "default"
#: Said wherever the mapping is shown: a short name is the client's, and its
#: meaning moves with the client's version (2026-10-03: one Claude Code
#: release's `opus` was another model than the next one's).
ALIAS_NOTE = ("A short model name (an alias) means whatever the agent's client maps it to, "
              "and that changes between client versions. Write the full model id, or pin "
              "the alias in the client's settings (Claude Code reads "
              "ANTHROPIC_DEFAULT_<ALIAS>_MODEL).")

_cache: dict = {}


def path() -> Path:
    """Where the mapping is read from (it need not exist)."""
    override = os.environ.get(ENV_PATH)
    if override:
        return Path(override).expanduser()
    from plexora import paths

    return paths.settings_path().parent / FILENAME


def _modules() -> dict:
    """{module: [task name, ...]} in registry order."""
    from plexora.ai import tasks

    out: dict = {}
    for task in tasks.tasks().values():
        out.setdefault(task.module, []).append(task.name)
    return out


def _parse(text: str) -> tuple[dict, list]:
    import yaml

    problems: list = []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {}, [f"not readable as YAML: {str(exc).splitlines()[0]}"]
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, ["the file is not a mapping of module to tasks"]
    known = _modules()
    mapping: dict = {}
    for module, entries in raw.items():
        if module not in known:
            problems.append(f"no AI module named {module!r} (modules: {', '.join(known)})")
            continue
        if entries is None:
            continue
        if not isinstance(entries, dict):
            problems.append(f"{module}: expected task: model lines")
            continue
        for task, model in entries.items():
            if task != DEFAULT and task not in known[module]:
                problems.append(f"{module}: no task named {task!r} "
                                f"(tasks: {', '.join([DEFAULT, *known[module]])})")
                continue
            if model is None or model == "":
                continue
            if not isinstance(model, (str, int, float)) or isinstance(model, bool):
                problems.append(f"{module}.{task}: a model is a name, not {type(model).__name__}")
                continue
            mapping.setdefault(module, {})[task] = str(model).strip()
    return mapping, problems


def load() -> dict:
    """{path, exists, mapping: {module: {task|default: model}}, problems}.
    Re-read whenever the file changes, so an edit applies to the next call."""
    where = path()
    try:
        stat = where.stat()
    except OSError:
        return {"path": str(where), "exists": False, "mapping": {}, "problems": []}
    stamp = (str(where), stat.st_mtime_ns, stat.st_size)
    if _cache.get("stamp") != stamp:
        try:
            text = where.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            mapping, problems = {}, [f"not readable: {exc}"]
        else:
            mapping, problems = _parse(text)
        _cache.update(stamp=stamp, mapping=mapping, problems=problems)
    return {"path": str(where), "exists": True, "mapping": _cache["mapping"],
            "problems": list(_cache["problems"])}


def model_for(task_id: str | None) -> str | None:
    """The model mapped to `task_id` (`module.task`): the task's own line, else
    its module's `default`, else None -- the agent's choice."""
    if not task_id or "." not in task_id:
        return None
    module, name = task_id.split(".", 1)
    entries = load()["mapping"].get(module) or {}
    return entries.get(name) or entries.get(DEFAULT) or None


def mapped(module: str) -> bool:
    """Whether the file names a model for any of `module`'s tasks."""
    return bool(load()["mapping"].get(module))


def groups(module: str) -> list[dict]:
    """`module`'s tasks grouped by the model that answers them, in registry
    order of each group's first task: [{model, tasks: [task id, ...]}]. Tasks
    the file leaves to the agent form one group with model None. A task no
    session packet is (`mcp: false`: the note interpreter, which only
    Plexora's harness calls) is in no group: a worker for it would only
    ask for packets that never come."""
    from plexora.ai import tasks

    registry = tasks.tasks()
    out: list = []
    by_model: dict = {}
    for name in _modules().get(module, []):
        task_id = f"{module}.{name}"
        if not registry[task_id].mcp:
            continue
        model = model_for(task_id)
        if model not in by_model:
            by_model[model] = {"model": model, "tasks": []}
            out.append(by_model[model])
        by_model[model]["tasks"].append(task_id)
    return out


def describe() -> dict:
    """What `server_info` and `plexora ai models` show: the file, and per
    module each task's model (None: the agent chooses)."""
    found = load()
    resolved = {module: {f"{module}.{name}": model_for(f"{module}.{name}") for name in names}
                for module, names in _modules().items()}
    return {"path": found["path"], "exists": found["exists"], "env": ENV_PATH,
            "tasks": resolved, "problems": found["problems"], "note": ALIAS_NOTE}


def template() -> str:
    """A starting file listing every task, each blank (the agent's choice)."""
    from plexora.ai import tasks

    lines = [
        "# Which model answers each Plexora AI task when an agent drives Plexora over MCP.",
        "#",
        "# Values are passed to your agent as they are: write names your agent's client",
        "# understands. Leave a task blank and your agent chooses; `default` covers every",
        "# task of its module not named on its own line.",
        "#",
        "# A short name (an alias) means whatever your client maps it to, and that changes",
        "# between client versions: prefer the full model id, or pin the alias in the",
        "# client's own settings.",
        "#",
        "# Runs Plexora AI makes itself (ai_run_session, the app's Gate and QC buttons) are",
        "# routed by the Plexora AI admin page instead, not by this file.",
        f"# Another file can be used instead: set {ENV_PATH}=<path>.",
    ]
    current = None
    for task in tasks.tasks().values():
        if task.module != current:
            current = task.module
            lines += ["", f"{task.module}:", f"  {DEFAULT}:"]
        lines.append(f"  {task.name}:".ljust(26) + f"# {task.label}: {task.blurb}")
    return "\n".join(lines) + "\n"


def write_template(*, force: bool = False) -> Path:
    """Write `template()` at `path()`; refuses to overwrite unless `force`."""
    where = path()
    if where.exists() and not force:
        raise FileExistsError(str(where))
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(template(), encoding="utf-8")
    return where
