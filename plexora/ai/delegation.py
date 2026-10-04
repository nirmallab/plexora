"""Who runs what: the work Plexora's AI features hand to worker conversations,
and the model tier each piece needs.

Plexora decides when work is delegated, to which tier, with which tools and
how much of it one worker does; the client decides which model a tier is.
Nothing here names a vendor or a model. A tier is a description of the work,
and its model is the user's mapping (`plexora ai tiers routine=<model>`,
or `PLEXORA_MODEL_ROUTINE`); unset, the tier's words say which of the
client's models to pick.

Finer than a tier, the user's task -> model file (`models_config`) may name
a model per AI task. A module it maps is handed out as one worker group per
model (`block()["workers"]`): each worker is scoped to its tasks (`tasks` on
`*_next`), so the packets of one marker or channel may pass between workers
of different models, and a worker told `other_tasks` stops and says whose
they are.

Why delegate at all: a conversation re-reads everything before each call, so
one that answers n decision packets pays ~n² in re-read context (live run
lsp11385, 2026-10-01: 91 packets, 29.6M cache-read tokens). A coordinator that
starts the work, hands it out a few units at a time, and keeps only each
worker's returned lines pays ~n.

A role is used the same way by every feature:

- the server returns `delegate` (`block`) where the work starts and in its
  status, with the role's tier, the model if the user mapped one, the worker
  skill, the tools, the units per worker and a short `brief`;
- the coordinator launches a fresh worker per `units_per_worker` units with
  `brief` as its whole prompt, on that model, given only those tools, and
  keeps only the lines it returns;
- a client with agent files gets one generated per role (`agent_file`,
  `plexora ai setup claude --install-skills`); any other client uses the brief
  as it is.
"""

from __future__ import annotations

import os

#: The tiers, in words a coordinator on any client can act on.
TIERS = {
    "routine": ("tool relay and bookkeeping whose result is already decided: listing, "
                "counting, checking a state, formatting, writing up. Your client's cheapest "
                "model that calls tools reliably."),
    "judgement": ("reading pictures, placing or confirming a gate, judging an artifact -- "
                  "work where a wrong call changes a result. Your client's most capable "
                  "model."),
}

#: The work handed to workers. `tools` are capability names (or the server's
#: own tools), resolved to tool names when served; `quota` names the
#: constant that sets the units per worker (`skills.constants`).
ROLES = {
    "gating_worker": {
        "tier": "judgement",
        "skill": "gate-packets",
        "description": ("Answers a Plexora gating session's decision packets for a few "
                        "markers, then returns one line per marker."),
        "tools": ("read_skill", "gating.session_status", "gating.next", "gating.answer"),
        "quota": ("ENGINE", "markers_per_worker"),
        "units": "markers",
        "module": "gating",
        "next": "gating.next",
        "answer": "gating.answer",
        # Whose packets come first once the markers are profiled (the T1/T2 looks).
        "first_task": "gating.image_inspection",
    },
    "qc_worker": {
        "tier": "judgement",
        "skill": "qc-packets",
        "description": ("Answers a Plexora QC session's decision packets for the tasks its "
                        "brief names, then returns one line per packet."),
        "tools": ("read_skill", "qc.session_status", "qc.next", "qc.answer"),
        "quota": ("QC_ENGINE", "packets_per_worker"),
        "units": "packets",
        "module": "qc",
        "next": "qc.next",
        "answer": "qc.answer",
        "first_task": "qc.planning",
    },
}

#: How a coordinator waits for a worker: each wake-up, message or status poll
#: is a turn that re-reads its whole context (2026-10-03 benchmark: a coordinator
#: polled and messaged its workers while they ran).
WAIT = ("wait for the lines it returns; do not schedule wake-ups, message it or poll the "
        "session's status while it runs (launch it in the foreground: an agent tool with a "
        "run_in_background option starts it in the background unless that is false).")

#: A worker's `launch` in a block with `workers`: at once, or when another
#: worker returns `other_tasks` naming one of its tasks.
LAUNCH = ("now", "on_demand")

#: A scoped worker's quota in packets per unit of its role's quota: a marker
#: takes about three looks (lsp11385: 91 packets for ~30 markers).
PACKETS_PER_UNIT = 3

#: Where the user's mapping lives in the settings file (`paths.settings_path`).
SETTINGS_KEY = "ai_tiers"
#: `PLEXORA_MODEL_ROUTINE`, `PLEXORA_MODEL_JUDGEMENT`: one per tier.
ENV_PREFIX = "PLEXORA_MODEL_"


def _settings() -> dict:
    from plexora import paths

    mapping = paths.read_settings().get(SETTINGS_KEY)
    return mapping if isinstance(mapping, dict) else {}


def model_for(tier: str):
    """The model the user mapped to `tier`, or None (the client picks by the
    tier's words). The environment wins over the settings file."""
    if tier not in TIERS:
        raise KeyError(f"no tier named {tier!r}; tiers: {', '.join(TIERS)}")
    value = os.environ.get(f"{ENV_PREFIX}{tier.upper()}") or _settings().get(tier)
    return str(value) if value else None


def set_models(pairs: dict) -> dict:
    """Record `{tier: model}` in the settings file (an empty model clears the
    tier); returns the whole mapping."""
    from plexora import paths

    unknown = sorted(set(pairs) - set(TIERS))
    if unknown:
        raise KeyError(f"no tier named {unknown[0]!r}; tiers: {', '.join(TIERS)}")
    settings = paths.read_settings()
    mapping = dict(settings.get(SETTINGS_KEY) or {})
    for tier, model in pairs.items():
        if model:
            mapping[tier] = str(model)
        else:
            mapping.pop(tier, None)
    settings[SETTINGS_KEY] = mapping
    paths.write_settings(settings)
    return mapping


def describe() -> dict:
    """The tiers, each with its words and the model mapped to it (`server_info`)."""
    return {tier: {"for": words, "model": model_for(tier)} for tier, words in TIERS.items()}


def _tool(name):
    from plexora.agent import registry
    from plexora.mcp import SERVER_TOOLS

    if name in SERVER_TOOLS:
        return name
    if registry.tool_name_of(name) == name:
        registry.discover()       # a command line, not a server: nothing registered yet
    return registry.tool_name_of(name)


def units_per_worker(role: str) -> int:
    from plexora.ai import skills

    group, key = ROLES[role]["quota"]
    return int(skills.constants()[group][key])


def tools(role: str) -> list:
    return [_tool(name) for name in ROLES[role]["tools"]]


def brief(role: str, *, tasks=None, reader=None, **context) -> str:
    """The worker's whole prompt: who it is, the skill to read, the context it
    cannot get from its tools, and what to return. Short on purpose -- the
    coordinator writes it once per worker. A worker scoped to `tasks` passes
    them, and its `reader`, on every call that takes them."""
    spec = ROLES[role]
    n = units_per_worker(role)
    lines = [f"You are a Plexora worker ({role}). Read the skill `{spec['skill']}` with "
             f"{_tool('read_skill')} and follow it exactly."]
    if tasks and spec["units"] != "packets":
        lines.append(f"Answer at most {n * PACKETS_PER_UNIT} packets, then stop and return only "
                     "the lines the skill asks for.")
    else:
        lines.append(f"Do at most {n} {spec['units']}, then stop and return only the lines the "
                     "skill asks for.")
    if tasks:
        lines.append(f"tasks: {', '.join(tasks)} -- pass `tasks` and `reader` on every "
                     f"{_tool(spec['next'])} and {_tool(spec['answer'])}; on `other_tasks`, stop "
                     "and return the line the skill gives for it")
        lines.append(f"reader: {reader}")
    for key, value in context.items():
        if value not in (None, "", [], {}):
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def block(role: str, *, first_task: str | None = None, **context) -> dict:
    """What a tool result carries so any client can hand the work out. When
    the user's file maps the role's module, `workers` lists one worker per
    model, each with its tasks and its own brief.

    Only the worker whose tasks hold what is ready first (`first_task`: the
    packet in hand, else the role's usual first) is `launch: now`; the rest
    are `on_demand`, launched when a worker returns `other_tasks` naming
    theirs. Launched all at once, a worker whose packets come last only
    polls and stops (2026-10-03 benchmark: an Opus worker started beside the
    first, found nothing and returned)."""
    from plexora.ai import models_config

    spec = ROLES[role]
    tier = spec["tier"]
    model = model_for(tier)
    out = {"role": role, "tier": tier, "model": model,
           **({} if model else {"pick": TIERS[tier]}),
           "skill": spec["skill"], "tools": tools(role),
           "units_per_worker": units_per_worker(role), "agent": agent_name(role),
           "brief": brief(role, **context),
           "how": ("launch a fresh worker conversation with `brief` as its whole prompt, on "
                   "`model` (or the model `pick` describes), given only `tools` (`agent` is "
                   "that worker where your client installs agent files), in the foreground: "
                   f"{WAIT} Keep only the lines it returns and launch the next until the work "
                   "is done. Without workers, follow the skill yourself")}
    if not models_config.mapped(spec["module"]):
        return out
    workers = []
    groups = models_config.groups(spec["module"])
    first = first_task or spec.get("first_task")
    starts = next((i for i, g in enumerate(groups) if first in g["tasks"]), 0)
    for n, group in enumerate(groups, start=1):
        reader = f"{spec['module']}-{n}"
        chosen = group["model"] or model
        workers.append({"model": chosen, **({} if chosen else {"pick": TIERS[tier]}),
                        "tasks": group["tasks"], "reader": reader,
                        "launch": LAUNCH[0] if n - 1 == starts else LAUNCH[1],
                        "brief": brief(role, tasks=group["tasks"], reader=reader, **context)})
    out.pop("brief")
    out["model"] = None
    out.pop("pick", None)
    out["workers"] = workers
    out["models_file"] = models_config.load()["path"]
    out["how"] = ("the user's models file assigns these tasks to models: launch the worker whose "
                  "`launch` is `now` -- a fresh conversation with its `brief` as the whole prompt, "
                  "on its `model` (or the model `pick` describes), given only `tools` -- in the "
                  f"foreground: {WAIT} A worker that returns `other_tasks <task>` stopped "
                  "because what is ready is another worker's: launch the worker whose `tasks` hold "
                  "it (an `on_demand` one is launched only then). Two may run at once when each "
                  "has work. Relaunch until the work is done. A client that cannot choose a "
                  "worker's model answers itself and says which tasks ran on another model")
    return out


def other_tasks(needs, progress) -> dict:
    """What `*_next` tells a worker scoped to some tasks when what is ready
    now is another task's (`needs`)."""
    from plexora.ai import models_config

    return {"state": "other_tasks", "progress": progress,
            "needs": {"task": needs, "model": models_config.model_for(needs)},
            "note": "what is ready now belongs to another task, answered by another worker",
            "next": f"stop, and return `other_tasks {needs}` as your last line"}


# -- client adapters -----------------------------------------------------------


def agent_name(role: str) -> str:
    return "plexora-" + role.replace("_", "-")


def agent_file(role: str, server_key: str = "plexora") -> str:
    """A client agent definition for `role` (the markdown-with-frontmatter
    format Claude Code reads from `.claude/agents/`). It carries no model: the
    coordinator passes the tier's model on each launch."""
    spec = ROLES[role]
    names = ", ".join(f"mcp__{server_key}__{name}" for name in tools(role))
    return (f"---\nname: {agent_name(role)}\ndescription: {spec['description']} Launched with "
            f"the `brief` of a Plexora `delegate` block.\ntools: {names}\n---\n\n"
            f"You are a Plexora worker ({role}). Your prompt is a brief from the coordinator. "
            f"Read the skill `{spec['skill']}` with `read_skill` and follow it exactly. Use "
            "only your tools. Reply with the lines the skill asks for and nothing else.\n")
