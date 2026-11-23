"""The user's task -> model mapping for agents that drive Plexora over MCP.

A run Plexora makes itself (`ai_run_session`) is served by the gateway, which
an administrator routes per task. A session an agent drives over MCP is
answered by the agent's own models, which Plexora cannot choose -- but the
user can, in one hand-edited file:

    gating:
      default: <model>             # any gating task not named below
      threshold_evaluation: {model: <model>, effort: high}
    qc:
      artifact_inspection: <model>

The task names are the gateway's (`tasks.yaml`), so one vocabulary serves
both. A value is passed to the agent as it is -- a name *its* client
understands; Plexora never names a model. A task left out (and no module
`default`) is the agent's choice, as it is with no file at all.

A line may also carry the reasoning effort the worker runs at
(`{model: <model>, effort: <level>}`, levels `EFFORTS`), so a session an agent
drives can think as hard per task as the gateway's assignment does for a run
Plexora makes itself. The effort is passed to the agent with the model; a
task with none is run at the agent's own setting. A line naming an effort but
no model leaves the model to the agent.

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
#: The reasoning efforts a line may name (the clients' scale: Claude Code's
#: `--effort` and its Agent tool's `effort`).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
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


def _parse(text: str) -> tuple[dict, dict, list]:
    import yaml

    problems: list = []
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {}, {}, [f"not readable as YAML: {str(exc).splitlines()[0]}"]
    if raw is None:
        return {}, {}, []
    if not isinstance(raw, dict):
        return {}, {}, ["the file is not a mapping of module to tasks"]
    known = _modules()
    mapping: dict = {}
    efforts: dict = {}
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
            effort = None
            if isinstance(model, dict):
                unknown = sorted(set(model) - {"model", "effort"})
                if unknown:
                    problems.append(f"{module}.{task}: a line names `model` and `effort`, "
                                    f"not {', '.join(map(str, unknown))}")
                    continue
                effort = model.get("effort")
                if effort not in (None, "") and str(effort).strip().lower() not in EFFORTS:
                    problems.append(f"{module}.{task}: effort is one of {', '.join(EFFORTS)}, "
                                    f"not {effort!r}")
                    continue
                if effort not in (None, ""):
                    efforts.setdefault(module, {})[task] = str(effort).strip().lower()
                model = model.get("model")
            if model is None or model == "":
                continue
            if not isinstance(model, (str, int, float)) or isinstance(model, bool):
                problems.append(f"{module}.{task}: a model is a name, not {type(model).__name__}")
                efforts.get(module, {}).pop(task, None)
                continue
            mapping.setdefault(module, {})[task] = str(model).strip()
    return mapping, efforts, problems


def load() -> dict:
    """{path, exists, mapping: {module: {task|default: model}}, efforts:
    {module: {task|default: effort}}, problems}. Re-read whenever the file
    changes, so an edit applies to the next call."""
    where = path()
    try:
        stat = where.stat()
    except OSError:
        return {"path": str(where), "exists": False, "mapping": {}, "efforts": {}, "problems": []}
    stamp = (str(where), stat.st_mtime_ns, stat.st_size)
    if _cache.get("stamp") != stamp:
        try:
            text = where.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            mapping, efforts, problems = {}, {}, [f"not readable: {exc}"]
        else:
            mapping, efforts, problems = _parse(text)
        _cache.update(stamp=stamp, mapping=mapping, efforts=efforts, problems=problems)
    return {"path": str(where), "exists": True, "mapping": _cache["mapping"],
            "efforts": _cache["efforts"], "problems": list(_cache["problems"])}


def _reads_for(task_id: str) -> str | None:
    """The task whose packets `task_id` reads again (`Task.reader_of`), as
    `module.task`, or None."""
    from plexora.ai import tasks

    task = tasks.tasks().get(task_id)
    return f"{task.module}.{task.reader_of}" if task is not None and task.reader_of else None


def model_for(task_id: str | None) -> str | None:
    """The model mapped to `task_id` (`module.task`): the task's own line, else
    -- for a second reader (`Task.reader_of`) -- the model of the task it
    reads again, else its module's `default`, else None -- the agent's
    choice."""
    if not task_id or "." not in task_id:
        return None
    module, name = task_id.split(".", 1)
    entries = load()["mapping"].get(module) or {}
    if entries.get(name):
        return entries[name]
    first = _reads_for(task_id)
    return model_for(first) if first else (entries.get(DEFAULT) or None)


def effort_for(task_id: str | None) -> str | None:
    """The effort mapped to `task_id`: the task's own line, else that of the
    task a second reader reads again, else its module's `default`, else None
    -- the agent's own setting."""
    if not task_id or "." not in task_id:
        return None
    module, name = task_id.split(".", 1)
    entries = load()["efforts"].get(module) or {}
    if entries.get(name):
        return entries[name]
    first = _reads_for(task_id)
    return effort_for(first) if first else (entries.get(DEFAULT) or None)


def mapped(module: str) -> bool:
    """Whether the file names a model or an effort for any of `module`'s tasks."""
    found = load()
    return bool(found["mapping"].get(module) or found["efforts"].get(module))


def groups(module: str) -> list[dict]:
    """`module`'s tasks grouped by the model that answers them and the effort
    it runs at, in registry order of each group's first task: [{model, tasks:
    [task id, ...]}], with `effort` on a group that has one. Tasks the file
    leaves to the agent form one group with model None. A task no
    session packet is (`mcp: false`: the note interpreter, which only
    Plexora's harness calls) is in no group: a worker for it would only
    ask for packets that never come. A second reader (`Task.reader_of`) is
    a group of its own whatever its model: a worker that read a picture
    once would not read it again blind."""
    from plexora.ai import tasks

    registry = tasks.tasks()
    out: list = []
    by_model: dict = {}
    for name in _modules().get(module, []):
        task_id = f"{module}.{name}"
        if not registry[task_id].mcp:
            continue
        model, effort = model_for(task_id), effort_for(task_id)
        key = (model, effort, task_id if registry[task_id].reader_of else None)
        if key not in by_model:
            by_model[key] = {"model": model, **({"effort": effort} if effort else {}), "tasks": []}
            out.append(by_model[key])
        by_model[key]["tasks"].append(task_id)
    return out


def describe() -> dict:
    """What `server_info` and `plexora ai models` show: the file, and per
    module each task's model (None: the agent chooses)."""
    found = load()
    resolved = {module: {f"{module}.{name}": model_for(f"{module}.{name}") for name in names}
                for module, names in _modules().items()}
    efforts = {module: {f"{module}.{name}": effort_for(f"{module}.{name}") for name in names}
               for module, names in _modules().items()}
    return {"path": found["path"], "exists": found["exists"], "env": ENV_PATH,
            "tasks": resolved, "efforts": efforts, "problems": found["problems"],
            "note": ALIAS_NOTE}


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
        "# A line may also set the reasoning effort the worker runs at:",
        f"#   threshold_evaluation: {{model: <model>, effort: high}}   ({', '.join(EFFORTS)})",
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
