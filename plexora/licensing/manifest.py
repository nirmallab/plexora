"""Every entitlement Plexora knows about, and the product manifest built from it.

Data, like `telemetry/schema.py`, so that the question "what does Pro unlock?"
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

Four roots. `ai` is every AI path. `volume` ("Plexora 3D") is working with
z-stacks and time series beyond looking at them: Surface Studio
(plexora/server/surface), measuring and tracking 3-D cells, Cellpose over a
stack, Stack QC and 3-D analysis. Viewing a stack -- planes, projections,
sections, the volume view, a 3-D mask drawn over it -- is Free.
`transcriptomics` is the spatial-transcriptomics modality as a whole: without
it a Xenium or Visium run is not even offered at import
(`import_proposal._licensed`), and the transcripts and Visium HD plugins
(`transcriptomics:xenium`, `transcriptomics:visium`) are not on the page. A
sample imported from such a run still opens on Free -- image, cell table,
gating, ROIs, QC, Cell Explorer -- and its transcript or bin layer keeps its
card and its built tiles, which core's layer route still serves; the gene
section is not mounted, and the plugin scripts that draw the points and bins
in the viewer are not loaded with it. Nothing is deleted. `mcp` is off by
default (below).

First-party plugins licensed as a whole are named `plugin:<name>` and listed in
`PLUGINS` (Video Studio, `plugin:video_studio`). They are part of Pro: the Pro
default is every root that is not off by default, plus every plugin in
`PLUGINS`. There are two plans, Free and Pro; a trial is Pro with
`trial: true`, not a plan of its own.

Two principles hold for every root:

- Not granted means not shown. A tool, button, menu row or card action whose
  entitlement this licence does not carry is absent from the page -- not
  greyed, not badged, not explained (`plugins.unlocked`, the `licensed()`
  template global, `PlexoraPro.allows` in the client). The server still
  refuses a direct call, with the `license` block (`guards`).
- What a Pro feature produced stays readable on any plan. A surface, a 3-D
  object table, a region carried through planes, an imported transcript or
  bin layer is drawn, exported, measured and deleted on Free, like everything
  else; only making more of them is Pro.

Some roots are off by default (`OFF_BY_DEFAULT`): not part of Pro or its
trial, never sold, and turned on for a licence by an administrator, on the
BioCognia platform's admin licence page (a switch per entitlement). `mcp`,
external MCP access, is the one today: it lets an outside coding agent (Claude
Code, Codex, Cursor) reach the Pro capabilities over MCP with its own model. A
Pro capability called over MCP needs its own entitlement *and* `mcp`; Free
tools never look at either, on any path. A `plugin:<name>` that `PLUGINS` does
not list is a third-party plugin (`third_party()`): it is not part of Pro
either, and comes from its maker, who licenses it on their own terms (LICENSE,
section 7). Tiers ("application only", "AI harness", "MCP", "AI harness +
MCP") are grant sets the platform's licences carry; no code here names them.

`product_manifest()` is this module as the platform reads it -- the BioCognia
product manifest (`biocognia.product`): the roots with their labels and
sentences, the first-party plugins, the free tier, the AI modules the `ai` root opens, the trial
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
    "volume": (
        "Plexora 3D",
        "Working with z-stacks and time series beyond looking at them: "
        "surfaces, 3-D cells measured and tracked, segmentation over a stack, "
        "Stack QC and 3-D analysis."),
    "volume:surfaces": (
        "Surface Studio",
        "Surfaces in the 3-D view: isosurfaces of a channel with live "
        "thresholding, a surface for every cell of a 3-D mask, their mesh "
        "measurements and spatial neighbours, and mesh refinement."),
    "volume:objects": (
        "Cells in 3-D",
        "Measuring the objects of a 3-D mask, tracking them across time "
        "points, reviewing them plane by plane, and regions made from cells "
        "or carried through planes."),
    "volume:segmentation": (
        "3-D segmentation",
        "Segmenting cells over a whole z-stack with Cellpose in 3-D."),
    "volume:analysis": (
        "3-D analysis",
        "Stack QC across planes, 3-D colocalisation and finding regions of "
        "interest in a volume."),
    "transcriptomics": (
        "Spatial transcriptomics",
        "Importing Xenium, Visium and Visium HD runs: transcript and bin "
        "layers, gene lists and the 10x conversion."),
    "transcriptomics:xenium": (
        "Xenium",
        "Xenium runs: the transcript layer, gene lists and gene groups."),
    "transcriptomics:visium": (
        "Visium",
        "Visium and Visium HD runs: spot and bin layers, gene expression "
        "per bin and the 10x conversion."),
    "mcp": (
        "External MCP access",
        "Using Plexora's Pro features from an outside coding agent such as "
        "Claude Code, Codex or Cursor over the Model Context Protocol, with "
        "that agent's own model."),
}

#: A plugin licensed as a whole names itself under this root (`plugin:<name>`),
#: first-party or third-party. Declared here as a grammar rather than a list,
#: because a plugin that does not exist yet cannot be listed.
PLUGIN_ROOT = "plugin"

#: The first-party plugins licensed as a whole: name -> (label, sentence),
#: granted as `plugin:<name>`. Each is part of Pro (`pro_default()`). The
#: product manifest lists them, so the platform's licence page shows a switch
#: for each; a first-party capability naming a `plugin:<name>` missing here
#: fails `tests/test_licensing_manifest.py`.
PLUGINS: dict[str, tuple[str, str]] = {
    "video_studio": (
        "Video Studio",
        "Scientific videos made from the viewer, frame-exact and reproducible: "
        "z-stack rotations, plane sweeps, time-lapses, zooms and channel "
        "reveals, refined as camera paths on a timeline and exported."),
}

#: Roots that are off by default: not part of Pro or its trial, and never sold.
#: An administrator turns one on for a licence on the BioCognia platform.
OFF_BY_DEFAULT: tuple[str, ...] = ("mcp",)

#: The top-level entitlements, in the order a person is shown them.
ROOTS: tuple[str, ...] = tuple(name for name in ENTITLEMENTS if ":" not in name)


def pro_default() -> tuple[str, ...]:
    """Every root that is not off by default, then every first-party plugin
    (`plugin:<name>`): what the platform's `plexora.pro` plan is expected to
    carry, a trial included. The platform stays the source of truth for plans;
    `tools/bioc_sync.py --check` fails when it disagrees."""
    roots = tuple(root for root in ROOTS if root not in OFF_BY_DEFAULT)
    return roots + tuple(f"{PLUGIN_ROOT}:{name}" for name in PLUGINS)


def third_party(entitlement) -> bool:
    """Whether `entitlement` is a `plugin:<name>` that `PLUGINS` does not list:
    a plugin from another maker, who licenses it (LICENSE, section 7)."""
    text = str(entitlement or "")
    if not text.startswith(PLUGIN_ROOT + ":"):
        return False
    return text.split(":", 2)[1] not in PLUGINS


def off_by_default(entitlement) -> bool:
    """Whether `entitlement` is outside the Pro default, so that neither Pro
    nor its trial unlocks it and a refusal must not call it part of Pro: a
    root that is off by default (`mcp`, which an administrator turns on for a
    licence) or a third-party plugin (`third_party()`, which comes from its
    maker). Plexora sells neither, so no refusal of it offers a price or a
    trial."""
    return not any_satisfies(pro_default(), str(entitlement or ""))


def _joined(words) -> str:
    """"A", "A and B", "A, B and C"."""
    words = list(words)
    if len(words) < 2:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def pro_words() -> dict[str, str]:
    """What Pro adds, in words a person reads, built from `pro_default()` so a
    sentence that uses it cannot go stale when a root or plugin is added.

    `included` names the Pro default ("Plexora AI, Plexora 3D, Spatial
    transcriptomics and Video Studio"); `off_by_default` is a sentence on the
    roots that are off by default ("External MCP access is off by default; an
    administrator can turn it on."), or "" when there are none. Settings'
    licence lead (`settings_routes.settings_page`) renders both, and
    `plexora license status` on Free says the same."""
    off = [ENTITLEMENTS[root][0] for root in OFF_BY_DEFAULT]
    sentence = ""
    if off:
        verb, them = ("are", "them") if len(off) > 1 else ("is", "it")
        sentence = f"{_joined(off)} {verb} off by default; an administrator can turn {them} on."
    return {"included": _joined(label(name) for name in pro_default()), "off_by_default": sentence}


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
        name = entitlement.split(":", 1)[1]
        return PLUGINS[name][0] if name in PLUGINS else name.replace("_", " ").title()
    return str(entitlement)


def summary(entitlement) -> str:
    """One sentence on what `entitlement` unlocks, or the root's sentence."""
    for candidate in (entitlement, *ancestors(str(entitlement or ""))):
        if candidate in ENTITLEMENTS:
            return ENTITLEMENTS[candidate][1]
    if isinstance(entitlement, str) and entitlement.startswith(PLUGIN_ROOT + ":"):
        name = entitlement.split(":", 1)[1]
        if name in PLUGINS:
            return PLUGINS[name][1]
    if third_party(entitlement):
        return "A plugin from its maker, not part of Plexora Pro."
    return ("A Plexora feature that is off by default, not part of Plexora Pro."
            if off_by_default(entitlement) else "A Plexora Pro feature.")


def plan_for(entitlement) -> str:
    """Which of Plexora's two plans unlocks `entitlement`: `pro`. There is one
    answer while there are two, and the function exists so that callers never
    hard-code it."""
    return "pro"


def unlocks(entitlements, *, paid: bool) -> list[dict]:
    """One row per root, then one per first-party plugin: what it is called,
    what it does and whether this licence grants it. Settings,
    `plexora license status` and `server_info` show these, so an "Included /
    Not included" list has one source. `paid` is `describe()`'s boolean --
    whether a licence is held -- not a plan name."""
    grants = tuple(entitlements or ()) if paid else ()
    rows = [(root, *ENTITLEMENTS[root]) for root in ROOTS]
    rows += [(f"{PLUGIN_ROOT}:{name}", *PLUGINS[name]) for name in PLUGINS]
    return [{"entitlement": name, "label": label, "summary": sentence, "granted": any_satisfies(grants, name)}
            for name, label, sentence in rows]


# -- the product manifest -----------------------------------------------------------

#: The AI modules each root opens: what the platform's AI token carries as
#: `mods`, and so which `plexora.<module>.<task>` calls the gateway accepts for
#: a certificate granting that root. The modules are `plexora/ai/tasks.yaml`'s;
#: `tests/test_licensing_manifest.py` holds the two together.
AI_MODULES: dict[str, tuple[str, ...]] = {
    "ai": ("gating", "qc", "chat"),
}

#: How long a trial lasts. A trial is Pro with `trial: true`, not a third plan.
TRIAL_DAYS = 30

FREE_DESCRIPTION = ("Everything Plexora does without a licence: 2-D and 3-D multiplexed "
                    "images and their tables -- z-stacks, time series, projections, "
                    "sections and the volume view -- every basic tool's manual features, "
                    "remote and HPC viewing, notebooks and export.")


def product_manifest() -> dict:
    """Plexora as the BioCognia platform knows it. Deterministic: the sync tool
    uploads exactly this, and the platform's copy is compared with it."""
    roots = {root: {"label": ENTITLEMENTS[root][0], "sentence": ENTITLEMENTS[root][1]}
             for root in ROOTS}
    # `plugin:<name>` is a grammar, so its root is declared for the platform's
    # plans to name and nothing else; it is never shown as a row of its own.
    roots[PLUGIN_ROOT] = {
        "label": "Plugins",
        "sentence": "A plugin licensed as a whole, named under plugin:<name>."}
    return {
        "schema": 1,
        "id": "plexora",
        "name": "Plexora",
        "env_prefix": "PLEXORA",
        "cli": "plexora",
        "free": {"label": "Free", "description": FREE_DESCRIPTION, "limits": {}},
        "entitlements": roots,
        "plugins": {name: {"label": label, "sentence": sentence} for name, (label, sentence) in PLUGINS.items()},
        "limits": {},
        "ai": {"modules": {root: list(modules) for root, modules in AI_MODULES.items()}},
        "trial_days": TRIAL_DAYS,
    }
