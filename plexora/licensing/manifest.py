"""Every entitlement Plexora knows about, and the product manifest built from it.

Data, like `telemetry/schema.py`, so that the question "what does Paid unlock?"
has one answer a person can read. A capability or plugin that names an
entitlement not declared here fails `tests/test_licensing_manifest.py`, which
is what stops a typo from quietly making something free (or quietly making it
impossible to unlock).

Free is everything Plexora does without a licence, and has no entitlements
because it needs none: a Free requirement is `None`, never a grant. The root
`ai` covers every AI path below it; finer grants exist so that a licence can
be narrower without a schema change, not because any licence is sold that way
today. Which grants a plan carries is the BioCognia platform's to say (plans
and prices are edited only there); what the roots ARE is said here.

Add-ons are roots sold separately from the Paid default. `mcp` is the first:
it lets an outside coding agent (Claude Code, Codex, Cursor) reach the Paid
capabilities over MCP with its own model. A Paid capability called over MCP
needs its own entitlement *and* `mcp`; Free tools never look at either, on any
path. Tiers ("application only", "AI harness", "MCP", "AI harness + MCP") are
grant sets the platform's plans name; no code here names them.

`product_manifest()` is this module as the platform reads it -- the BioCognia
product manifest (`biocognia.product`): the roots with their labels and
sentences, the free tier, the AI modules the `ai` root opens, the trial
length. This file is its one editable source (invariant 15):
`tools/bioc_sync.py` uploads it and `--check` fails when the platform's copy
differs.
"""

from __future__ import annotations

from biocognia.entitlements import ancestors, any_satisfies, valid

#: entitlement -> (label shown to a person, one sentence on what it unlocks)
ENTITLEMENTS: dict[str, tuple[str, str]] = {
    "ai": (
        "Plexora AI",
        "AI agents that act on your data: guided gating sessions, evidence "
        "and analysis tools for automatic gating, and whatever AI features "
        "come next."),
    "ai:gating": (
        "AI gating",
        "An AI agent that proposes, checks and reports gates for you."),
    "ai:gating:session": (
        "AI gating sessions",
        "Guided gating sessions an agent runs marker by marker, with a panel "
        "context, validation regions and a session report."),
    "ai:gating:analytics": (
        "AI gating analysis",
        "The comparisons, marker profiles, cell samples and QC an agent "
        "gathers as evidence before proposing a gate."),
    "ai:evidence": (
        "AI evidence in the viewer",
        "An agent showing the evidence behind its proposal on the canvas."),
    "ai:chat": (
        "Plexora AI chat",
        "An assistant inside Plexora that answers questions about your "
        "projects by calling Plexora's tools, with your approval before "
        "anything that cannot be undone."),
    "ai:qc": (
        "AI quality control",
        "An AI agent that finds imaging artifacts and flags cells for you."),
    "ai:qc:session": (
        "AI QC sessions",
        "Guided quality-control sessions an agent runs channel by channel: "
        "artifact regions written as ROIs, cells flagged, and a QC report."),
    "ai:qc:analytics": (
        "AI QC analysis",
        "The image scans, channel profiles and evidence sheets an agent "
        "gathers before judging an artifact, including the example sheets "
        "sampled across a QC check's scores."),
    "mcp": (
        "External MCP access",
        "Using Plexora's Paid features from an outside coding agent such as "
        "Claude Code, Codex or Cursor over the Model Context Protocol, with "
        "that agent's own model."),
}

#: A third-party or future first-party plugin that is sold as a unit names
#: itself under this root (`plugin:<name>`). Declared here as a grammar rather
#: than a list, because a plugin that does not exist yet cannot be listed.
PLUGIN_ROOT = "plugin"

#: Roots sold separately from the Paid default (`ai`): an administrator adds
#: them to a licence or a seat.
ADD_ONS: tuple[str, ...] = ("mcp",)

#: The top-level entitlements, in the order a person is shown them.
ROOTS: tuple[str, ...] = tuple(name for name in ENTITLEMENTS if ":" not in name)


def known(entitlement) -> bool:
    """Whether `entitlement` is declared here, or is a `plugin:<name>` path."""
    if not valid(entitlement):
        return False
    if entitlement in ENTITLEMENTS:
        return True
    return entitlement.startswith(PLUGIN_ROOT + ":")


def label(entitlement) -> str:
    """The name to show a person for `entitlement`.

    Falls back to the nearest declared ancestor, then to the string itself:
    the modal that explains a locked feature must always have something to say.
    """
    for candidate in (entitlement, *ancestors(str(entitlement or ""))):
        if candidate in ENTITLEMENTS:
            return ENTITLEMENTS[candidate][0]
    if isinstance(entitlement, str) and entitlement.startswith(PLUGIN_ROOT + ":"):
        return entitlement.split(":", 1)[1].replace("_", " ").title()
    return str(entitlement)


def summary(entitlement) -> str:
    """One sentence on what `entitlement` unlocks, or the root's sentence."""
    for candidate in (entitlement, *ancestors(str(entitlement or ""))):
        if candidate in ENTITLEMENTS:
            return ENTITLEMENTS[candidate][1]
    return "A Paid Plexora feature."


def plan_for(entitlement) -> str:
    """Which of Plexora's two plans unlocks `entitlement`. There is one answer
    while there are two, and the function exists so that callers never
    hard-code it."""
    return "paid"


def unlocks(entitlements, *, paid: bool) -> list[dict]:
    """One row per root: what it is called, what it does and whether this
    licence grants it. Settings, `plexora license status` and `server_info`
    show these, so an "Included / Not included" list has one source."""
    grants = tuple(entitlements or ()) if paid else ()
    return [{"entitlement": root, "label": ENTITLEMENTS[root][0], "summary": ENTITLEMENTS[root][1],
             "granted": any_satisfies(grants, root)}
            for root in ROOTS]


# -- the product manifest -----------------------------------------------------------

#: The AI modules each root opens: what the platform's AI token carries as
#: `mods`, and so which `plexora.<module>.<task>` calls the gateway accepts for
#: a certificate granting that root. The modules are `plexora/ai/tasks.yaml`'s;
#: `tests/test_licensing_manifest.py` holds the two together.
AI_MODULES: dict[str, tuple[str, ...]] = {
    "ai": ("gating", "qc", "chat"),
}

#: How long a trial lasts. A trial is Paid with `trial: true`, not a third plan.
TRIAL_DAYS = 30

FREE_DESCRIPTION = ("Everything Plexora does without a licence: every image, table and "
                    "modality, every tool's manual features, remote and HPC viewing, "
                    "notebooks and export.")


def product_manifest() -> dict:
    """Plexora as the BioCognia platform knows it. Deterministic: the sync tool
    uploads exactly this, and the platform's copy is compared with it."""
    roots = {root: {"label": ENTITLEMENTS[root][0], "sentence": ENTITLEMENTS[root][1]}
             for root in ROOTS}
    # `plugin:<name>` is a grammar, so its root is declared for the platform's
    # plans to name and nothing else; it is never shown as a row of its own.
    roots[PLUGIN_ROOT] = {
        "label": "Plugins sold separately",
        "sentence": "A plugin sold as a unit, named under plugin:<name>."}
    return {
        "schema": 1,
        "id": "plexora",
        "name": "Plexora",
        "env_prefix": "PLEXORA",
        "cli": "plexora",
        "free": {"label": "Free", "description": FREE_DESCRIPTION, "limits": {}},
        "entitlements": roots,
        "limits": {},
        "ai": {"modules": {root: list(modules) for root, modules in AI_MODULES.items()}},
        "trial_days": TRIAL_DAYS,
    }
