"""Every entitlement Plexora knows about, and what each plan grants.

Data, like `telemetry/schema.py`, so that the question "what does Paid unlock?"
has one answer a person can read. A capability or plugin that names an
entitlement not declared here fails `tests/test_licensing_manifest.py`, which
is what stops a typo from quietly making something free (or quietly making it
impossible to unlock).

Two plans and only two. Free is everything Plexora does without a licence, and
has no entitlements because it needs none: a Free requirement is `None`, never
a grant. Paid carries the single root `ai`, which covers every AI path below
it; finer grants exist so that a future licence can be narrower without a
schema change, not because any licence is sold that way today.
"""

from __future__ import annotations

from plexora.licensing.entitlements import ancestors, valid

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
        "gathers before judging an artifact."),
}

#: A third-party or future first-party plugin that is sold as a unit names
#: itself under this root (`plugin:<name>`). Declared here as a grammar rather
#: than a list, because a plugin that does not exist yet cannot be listed.
PLUGIN_ROOT = "plugin"

PLANS = ("free", "paid")

#: What a certificate for each plan carries when the server issues it. Kept in
#: step with the licensing Worker's default by the cross-language vectors.
PLAN_ENTITLEMENTS: dict[str, tuple[str, ...]] = {
    "free": (),
    "paid": ("ai",),
}


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
    """Which plan unlocks `entitlement`. There is one answer while there are
    two plans, and the function exists so that callers never hard-code it."""
    return "paid"
