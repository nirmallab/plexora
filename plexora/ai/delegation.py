"""Who runs what: the work Plexora's AI features hand to worker conversations,
and the model tier each piece needs.

Plexora decides when work is delegated, to which tier, with which tools and
how much of it one worker does; the client decides which model a tier is.
Nothing here names a vendor or a model. A tier is a description of the work,
and its model is the user's mapping (`plexora ai tiers set routine=<model>`,
or `PLEXORA_MODEL_ROUTINE`); unset, the tier's words say which of the
client's models to pick.

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
    },
}

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


def brief(role: str, **context) -> str:
    """The worker's whole prompt: who it is, the skill to read, the context it
    cannot get from its tools, and what to return. Short on purpose -- the
    coordinator writes it once per worker."""
    spec = ROLES[role]
    n = units_per_worker(role)
    lines = [f"You are a Plexora worker ({role}). Read the skill `{spec['skill']}` with "
             f"{_tool('read_skill')} and follow it exactly.",
             f"Do at most {n} {spec['units']}, then stop and return only the lines the skill "
             "asks for."]
    for key, value in context.items():
        if value not in (None, "", [], {}):
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def block(role: str, **context) -> dict:
    """What a tool result carries so any client can hand the work out."""
    spec = ROLES[role]
    tier = spec["tier"]
    model = model_for(tier)
    return {"role": role, "tier": tier, "model": model,
            **({} if model else {"pick": TIERS[tier]}),
            "skill": spec["skill"], "tools": tools(role),
            "units_per_worker": units_per_worker(role), "agent": agent_name(role),
            "brief": brief(role, **context),
            "how": ("launch a fresh worker conversation with `brief` as its whole prompt, on "
                    "`model` (or the model `pick` describes), given only `tools` (`agent` is "
                    "that worker where your client installs agent files); keep only the "
                    "lines it returns and launch the next until the work is done. Without "
                    "workers, follow the skill yourself")}


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
