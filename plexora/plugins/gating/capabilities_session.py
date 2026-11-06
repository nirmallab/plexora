"""The gating session as tools: start it, fetch a decision, answer it, finish.

    gating_session_start  -> {session_id, job_id}         (the bulk pass runs as a job)
                             or `needs_setup` + an `expression_setup` packet, when which
                             matrix to gate cannot be settled from the values
                             (`setup: ask`); or `needs_setup` + `setup`, waiting on the
                             viewer's set-up form (`setup: wait`); or refused, saying what
                             is missing (a column not found; `setup: fail`, the matrix too)
    gating_next           -> one decision packet (+ <= 2 images), or a status
    gating_answer         -> the outcome, and the next packet inline
    gating_session_status -> each marker's gate, state and why; `detail: full` every
                             unit's fields, the budget, QC; `guide` the reading guide
    gating_session_finish -> close | commit (propose mode) | cancel | rollback

The loop an agent runs is `gating_next`, then `gating_answer` until the answer
says `done`. Packets are self-contained, so a new conversation can pick a
session up with `gating_next(session_id)`; nothing depends on what an earlier
conversation saw.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from plexora import _work

from plexora.agent import presets
from plexora.agent.errors import AgentError
from plexora.agent.limits import MAX_LIST
from plexora.agent.receipts import make_receipt
from plexora.agent.registry import Capability, tool_name_of
from plexora.agent.schemas import AgentModel
from plexora.agent.sessions import budget as budgets
from plexora.agent.sessions import mirror as mirroring
from plexora.agent.sessions import tools as session_tools
from plexora.plugins.gating import PLUGIN
from plexora.plugins.gating.server.autogate import schemas

OWNER = "gating"
TAGS = ("gate", "gating", "threshold", "auto", "automatic", "session", "workflow",
        "phenotype", "marker")
STATE = "plugin_store:gating"


def _allowance(key, description):
    low, high = budgets.UNIT_BOUNDS[key]
    return Field(budgets.UNIT_DEFAULT[key], ge=low, le=high, description=description)


class Budget(AgentModel):
    packets: int | None = _allowance("packets", "Looks per marker (first look, beside a "
                                                "reference, candidates); confirmations "
                                                "are free.")
    images: int | None = _allowance("images", "Images per marker, over its looks.")
    pixels: int | None = _allowance("pixels", "Image pixels per marker, over its looks.")
    chars: int | None = _allowance("chars", "Packet JSON characters per marker, over its "
                                            "looks.")


_FIELD_UM = float(presets.PRESETS["gating_context"]["field_um"])
_FIELD_UM_BOUNDS = presets.PRESETS["gating_context"]["field_um_bounds"]


def _limit_default(name):
    """A limit option's default: the environment a command line set
    (`schemas.LIMIT_ENV`), else `schemas.LIMIT_DEFAULTS`. An unreadable
    value falls back to the default rather than failing every session."""
    import os

    raw = os.environ.get(schemas.LIMIT_ENV[name])
    default = schemas.LIMIT_DEFAULTS[name]
    if raw is None or raw == "":
        return default
    if name == "on_limit":
        return raw if raw in schemas.LIMIT_POLICIES else default
    try:
        return max(0, min(10, int(raw)))
    except ValueError:
        return default


class Biology(AgentModel):
    """The sample's biology as the user stated it (`autogate/biology.py`)."""
    tissue: str | None = Field(None, max_length=120, description="The tissue or organ, in "
                               "the user's words ('skin', 'lymph node').")
    disease: str | None = Field(None, max_length=120, description="The disease or sample "
                                "type, in the user's words ('melanoma').")
    notes: str | None = Field(None, max_length=400, description="Anything else the user "
                              "said about the sample.")
    #: Kept beside the interpretation when tissue/disease/notes were read from
    #: a note (`plexora.ai.context`).
    original: str | None = Field(None, max_length=1000, description="The user's note as "
                                 "written, when the fields above were read from it.")
    #: Populations of interest, corrected terms, ambiguities, scope.
    interpretation: dict | None = Field(None, description="The Context Interpreter's reading "
                                        "of `original`.")


class ExpressionChoice(AgentModel):
    """Which matrix to gate on, settled before the start."""
    features_layer: str = Field(description="`X` or `layer:<name>`, as "
                                            "inspect_expression_sources lists them.")
    features_log: bool = Field(description="Apply log1p as the values are read; never to "
                                           "values already logged.")
    #: user: `plexora ai run --matrix/--log1p`; inferred:
    #: `plexora.ai.harness.expression`.
    basis: Literal["user", "inferred"] = Field(
        "user", description="user: the user named it (applied over an earlier confirmed "
                            "choice); inferred: a model read it from the values (applied only "
                            "where nothing is confirmed).")
    why: str | None = Field(None, max_length=400, description="Why, in a sentence.")


# What the model reads when it loads `gating_session_start` is these
# descriptions (every door: MCP, the bridge, Biocognia Intelligence's guest
# catalogue): a sentence each that says what a value does. Why a default is
# what it is -- the benchmark behind it, the run that changed it -- is in the
# comment above the field, which no model pays for.
class SessionOptions(AgentModel):
    scope: Literal["project", "dataset"] = Field(
        "project", description="project: one image; dataset: every image of a dataset, the "
                               "reference image first and gates carried to the rest.")
    project: str | None = Field(None, description="With scope=project.")
    dataset: str | None = Field(None, description="With scope=dataset: name or id.")
    markers: list[str] | None = Field(None, description="Default: every marker except "
                                      "structural channels (DNA and the like).")
    #: apply: each write receipted and undoable; the empty gate of a failed or
    #: all-negative marker is written too.
    mode: Literal["apply", "propose"] = Field(
        "apply", description="apply: gates are written as they are decided; propose: nothing "
                             "is written until gating_session_finish(action=commit).")
    overwrite_manual: bool = Field(False, description="Also re-gate markers the user set by "
                                   "hand (never locked or approved ones).")
    #: There is no numbers-only tier: every gate is judged on the image's pixels.
    max_tier: int = Field(schemas.ENGINE["max_tier_default"], ge=2, le=len(schemas.TIERS),
                          description="2: one look per marker; 3: + reference channels; 4: + "
                                      "candidate refinement; 5: + questions for the user.")
    #: Without it a partial match refuses to start; when no marker matches,
    #: nothing starts either way.
    force: bool = Field(False, description="Start when only some markers match an image "
                        "channel: the rest are skipped (skipped_no_channel).")
    #: The set-up: a required column (cell id, coordinates, image id) not
    #: found or never confirmed, or an expression matrix its values cannot
    #: settle. fail is for a run with nobody to ask (`plexora ai run`); wait
    #: for Plexora AI started from the viewer (every column and the matrix).
    setup: Literal["ask", "wait", "fail"] = Field(
        "ask", description="When the gating set-up is unfinished (a column, the matrix). ask: "
                           "a missing column refuses; the matrix comes as an expression_setup "
                           "packet to put to the user, never guessed. fail: nothing starts; "
                           "the error says what to supply. wait: the session waits in "
                           "needs_setup until the user finishes the viewer's set-up form.")
    expression: ExpressionChoice | None = Field(
        None, description="The matrix to gate on, when the user named it or a model read it "
                          "from the values; checked against the project's matrices.")
    audit_sheet: bool = Field(True, description="Show automatically accepted markers on a "
                                                "batched audit sheet (~70 tokens a marker).")
    reference_image: str | None = Field(None, description="Dataset scope: the image gated "
                                        "first (default: the one with the most cells).")
    mirror: bool = Field(False, description="Show each decision in an open viewer too.")
    view_id: str | None = None
    mirror_delay_ms: int = Field(mirroring.DEFAULT_DELAY_MS, ge=0, le=5000)
    image_format: Literal["webp", "png"] = "webp"
    #: Sized from each image's own pixel size (or the session's estimate of it).
    field_um: float = Field(_FIELD_UM, ge=_FIELD_UM_BOUNDS[0], le=_FIELD_UM_BOUNDS[1],
                            description="The context sheet's tissue fields, microns a side.")
    reading: Literal["once", "every_packet"] = Field(
        "once", description="once: the reading guide comes with the start (and the status, "
                            "asked) and packets name its entries; every_packet: each packet "
                            "repeats the texts it needs.")
    #: delta: `reading_guide.as_in`, and briefs carry no repeated text.
    evidence: Literal["delta", "full"] = Field(
        "delta", description="delta: evidence this conversation was already sent comes as "
                             "{as_in: packet_id}; full: every packet whole.")
    #: trim: also a whole-image check shows only the slide scale.
    sheets: Literal["trim", "full"] = Field(
        "trim", description="trim: context-sheet rows this conversation was already shown "
                            "are left out; full: every sheet whole.")
    budget: Budget = Field(default_factory=Budget, description="What each marker may spend.")
    agent: str = Field("agent", max_length=80, description="Who answers (your model's name); "
                       "earlier answers are reused only from the same agent.")
    #: Same data, partners, code and seed: a rerun then reaches the same gates.
    reuse_answers: bool = Field(True, description="Answer a packet identical to one this agent "
                                "answered before with that answer. False asks afresh.")
    #: ask: the user is asked in the viewer and you are told; extend is for
    #: automated runs. A marker is never accepted because it ran out. Default
    #: from PLEXORA_GATING_ON_LIMIT (`plexora mcp serve --gating-on-limit`).
    on_limit: Literal[schemas.LIMIT_POLICIES] = Field(
        default_factory=lambda: _limit_default("on_limit"),
        description="A marker out of looks while the evidence says go on: ask (the user; "
                    "the marker waits), extend (another allowance), stop (review).")
    #: Default from PLEXORA_GATING_MAX_EXTENSIONS.
    max_extensions: int = Field(
        default_factory=lambda: _limit_default("max_extensions"), ge=0, le=10,
        description="Extra allowances one marker may get before review.")
    #: The gates still apply to every cell; no effect on an image QC has not
    #: been run on.
    qc: Literal["strict", "exclude", "off"] = Field(
        "strict", description="QC failures left out of every fit and picture. strict: cells "
                              "QC called exclude or warn, and per marker the cells QC flagged "
                              "unreliable; exclude: warn calls kept; off: QC ignored (only "
                              "when the user asks).")
    #: A prior for references, co-expression, spatial reading and confidence.
    biology: Biology | None = Field(None, description="The tissue and disease the user "
                                    "named: a prior, never a rule. Left out: from the "
                                    "project's metadata or inferred from the panel, marked so.")
    #: Weak: no robust partner curve, a poorly separated mixture. Children:
    #: markers whose positives are a subset of its own; it is then scored with
    #: where their positives sit, once, never after a look.
    defer_parents: bool = Field(True, description="A weakly separated marker waits for its "
                                "children in the panel's hierarchy.")
    known_guide: str | None = Field(None, description="The `guide_version` of a reading guide "
                                    "you hold: it is not sent again.")
    #: vision: markers in rows, five brightnesses in columns, code picks the
    #: column, for the markers no earlier check settled; a choice outlives the
    #: session. rule: black under the background, the bright cells' structure
    #: near the top. off with v3 is the setting v3 was measured with.
    display_check: Literal[schemas.DISPLAY_CHECKS] = Field(
        schemas.DISPLAY_CHECK_DEFAULT[schemas.DEFAULT_ENGINE],
        description="Auto contrast for every look: vision (a contrast grid to the model, "
                    "once per image), rule (the calibration's window), off. Default: off "
                    "with engines v3 and v4, vision with v1.")
    #: contrast: the set-up, the calibration and the contrast check; each
    #: marker closes `display_set` once its image's brightness is settled.
    purpose: Literal[schemas.PURPOSES] = Field(
        "gating", description="gating: decide the gates; contrast: auto contrast only "
                              "(contrast_session_start).")
    #: The 2 x 2 block: morphology, the context with the judged cell outlined,
    #: one expected and one not-expected partner (reading guide `collage_t2m`).
    #: Off, the looks are the two-panel cells.
    tiles: bool = Field(False, description="Draw each look's cells as 2 x 2 blocks with two "
                        "partner channels.")
    #: Three panels: the DNA alone, the marker alone from the display's black
    #: to the gate at mid-grey, the two combined (`collage_split`); a dim
    #: marker's looks stay morphology | context; `tiles` wins where both apply.
    split_views: Literal["off", "bright_rechecks"] = Field(
        "off", description="bright_rechecks: a recheck of a bright marker's gate draws each "
                           "cell as DNA | marker | both (unmeasured).")
    #: None in any region closes the marker with no positives (gate at the
    #: maximum); a technical check's failed-stain verdict stands only when the
    #: regions agree. Off by default: on 12 held-out images 9 of its 17
    #: closes were real populations of 0.5-8 % (bench GATING_GENERALISE).
    absence_check: bool = Field(False, description="Before a marker's first look, ask whether "
                                "six unmarked regions show any positive cell.")
    #: Rows wider than two background sds.
    split_mixed: bool = Field(True, description="Split a candidate row judged mixed into finer "
                              "ones and look again.")
    #: Opposite ways end in review. False: the gate stays where the rows put
    #: it, at low confidence, the disagreement noted -- the rows judged those
    #: very cells.
    whole_image_moves: bool = Field(False, description="A whole-image check calling the gate "
                                    "too low or too high sends it back to the candidates.")
    #: The first row passes even when judged mixed (two views agree); the
    #: rows still place the gate. False: it stays where the rows put it, at
    #: low confidence.
    whole_image_reopen: bool = Field(True, description="A whole-image check calling the gate "
                                     "too low gives one more look at the candidates above.")
    #: An edge band or a fold reads as one close up; a cluster of real cells
    #: still as cells.
    absence_confirm: bool = Field(True, description="A lone positive region of an absence "
                                  "picture is shown again close up before it counts.")
    #: layer: the gate's call unless the classifier is confident the other
    #: way, taking positives away more readily than adding them, and only where
    #: the marker's signal is poor. check: the flagged cells are shown beside
    #: cells it agrees on; the gate moves past each row the look confirms
    #: (unmeasured).
    classifier: Literal[schemas.CLASSIFIER_MODES] = Field(
        "layer", description="The Single Cell Classifier beside each gate. layer: trained at "
                             "each written gate, kept beside the threshold (which decides) "
                             "until the user adopts it; check: also show the cells it "
                             "disagrees on in a look; off.")
    #: auto: fibrous stains (smooth muscle, vessel walls: the panel's
    #: morphology) word, the rest mixed. word: one word a row, flagged cells
    #: must read as their own stain; mixed: flagged cells read as mixed count
    #: as stained; count: the cells in each row with their own stain.
    check_reading: Literal[schemas.CHECK_READINGS] = Field(
        "auto", description="How a classifier check's look is read (count: unmeasured).")
    #: v3: the lattice's candidate gates are competing hypotheses; blind
    #: panels, numbered and shuffled with no value or gate shown; looks again
    #: only where the leader has not beaten a rival; closes for review with a
    #: reason when it cannot. v4: v3, with a dataset's aligned images gated as
    #: groups. v1: then candidates beyond the gate. Named by none, the scope's
    #: (`schemas.DEFAULT_ENGINE_BY_SCOPE`).
    engine: Literal[schemas.ENGINES] = Field(
        schemas.DEFAULT_ENGINE,
        description="v3: blind panels of single cells labelled one at a time, code picks the "
                    "candidate gate the labels support; v4: v3, a dataset's aligned images "
                    "decided together (the default for scope=dataset); v1: rows below, at and "
                    "above the gate. classic (v1), candidates and v2 (v3) are accepted.")
    #: Folds (also ones that come or go between imaging cycles), tears,
    #: debris, saturation, lost nuclei; no model calls; marked automatic and
    #: unreviewed. Needs the QC plugin.
    basic_qc: bool = Field(
        schemas.BASIC_QC_DEFAULT[schemas.DEFAULT_ENGINE],
        description="On an image with no QC result, run QC's model-free checks first "
                    "(run_basic_qc) and leave out what they find. Default: on with v3 and v4, "
                    "off with v1.")
    panel_looks: int = Field(3, ge=1, le=6, description="Engines v3 and v4: the most cell "
                             "panels a marker is shown before review.")
    #: The pattern, dim cells seen stained, bright cells seen to be debris:
    #: starts the gate no higher than the dim stained cells, keeps the fields
    #: from the debris, caps confidence on a doubt, gives a clean split with a
    #: finding a look.
    observations: Literal[schemas.OBSERVATION_MODES] = Field(
        "off", description="Experimental. use: the contrast check's observations are evidence "
                           "in every look.")
    #: Weak: no valley, estimators that disagree. The first look starts at the
    #: dim cells the contrast check saw stained (else the 15% rung), the
    #: candidates are rungs by share of cells, cells below the gate seen mixed
    #: send it down, and the distribution's and spatial whole-image checks no
    #: longer fail a gate the looks placed.
    vision_first: bool = Field(False, description="Experimental: the looks place a weak "
                               "marker's gate. Pair with observations=use.")

    @field_validator("engine", mode="before")
    @classmethod
    def _engine_first_names(cls, value):
        return schemas.engine_name(value)

    @model_validator(mode="before")
    @classmethod
    def _defaults_by_engine(cls, data):
        """A session that names no engine gets its scope's
        (`schemas.DEFAULT_ENGINE_BY_SCOPE`); one that names no contrast check,
        or no basic QC, gets its engine's (`schemas.DISPLAY_CHECK_DEFAULT`,
        `schemas.BASIC_QC_DEFAULT`)."""
        if not isinstance(data, dict):
            return data
        if not data.get("engine"):
            data = {**data, "engine": schemas.DEFAULT_ENGINE_BY_SCOPE.get(
                data.get("scope") or "project", schemas.DEFAULT_ENGINE)}
        engine = schemas.engine_name(data["engine"])
        for key, table in (("display_check", schemas.DISPLAY_CHECK_DEFAULT),
                           ("basic_qc", schemas.BASIC_QC_DEFAULT)):
            if data.get(key) is None and engine in table:
                data = {**data, key: table[engine]}
        return data

    #: The markers left are reordered and rescored without the failed gates;
    #: accepted gates only a failed one placed go to review; what only biology
    #: can settle (a population that may be absent, an image whose looks keep
    #: naming the same artifact) goes to the planning task in a `replan` packet.
    replan: bool = Field(True, description="Re-plan when markers fail. False keeps the start's "
                         "plan.")
    seed: int = 0
    #: plexora/ai/delegation.py; workers on a cheaper or the same model.
    delegate: bool = Field(True, description="Return the `delegate` block: how to hand the "
                           "packet loop to worker conversations.")
    #: A marker never goes out beside one whose gate it reads, so the gates
    #: are the one-at-a-time run's. Default: as many as the panel's
    #: dependencies keep busy (1 for a few markers, or markers that all read
    #: each other).
    lanes: int | None = Field(None, ge=1, le=8, description="Readers answering at once, each "
                              "its own `reader` passing `parallel`.")


def session_qc(handler):
    """Run a session handler under the QC mode its session was started with
    (`SessionOptions.qc`): the bulk job, packets and answers all estimate on
    the same cells."""
    import functools

    @functools.wraps(handler)
    def wrapped(call, inp):
        from plexora.agent import cell_exclusions
        from plexora.plugins.gating.server.autogate import engine as engines

        chosen = None
        session_id = getattr(inp, "session_id", None)
        if session_id:
            try:
                chosen = (engines.store().load(session_id).get("options") or {}).get("qc")
            except Exception:
                chosen = None
        with cell_exclusions.mode(chosen if chosen in cell_exclusions.MODES else None):
            return handler(call, inp)

    return wrapped


def option_defaults() -> dict:
    """Every session option at its default (what an older session's stored
    options are completed with)."""
    return SessionOptions.model_construct().model_dump(mode="json")


def _cap_by_name(name):
    from plexora.agent import registry

    return registry.get(name)


def _structural(marker) -> bool:
    """A nuclear stain or a control channel (`vocabulary.structural` and the
    vocabulary's DNA / Autofluorescence entries)."""
    from plexora.ai import vocabulary

    return vocabulary.canonical(marker) in (vocabulary.NUCLEAR, vocabulary.AUTOFLUORESCENCE)


def _markers_for(call, images, wanted):
    from plexora.plugins.gating.server.autogate import context, views

    first = call.session.data(images[0])
    panel = context.for_project(first)
    available = list(dict.fromkeys(m for p in images
                                   for m in views.markers(call.session.data(p))))
    if wanted:
        unknown = [m for m in wanted if m not in available]
        if unknown:
            raise AgentError("invalid_input", f"not markers of this scope: {unknown}",
                             detail={"markers": available[:MAX_LIST]})
        # Even named explicitly, a nuclear counterstain or an autofluorescence
        # / blank channel is not a phenotype: it is never gated.
        chosen = [m for m in wanted if not _structural(m)]
    else:
        chosen = [m for m in available
                  if (panel["entries"].get(m) or {}).get("role") != "context"]
    skipped = [m for m in available if m not in chosen]
    order = [m for m in panel["order"] if m in chosen] + \
        [m for m in chosen if m not in panel["order"]]
    return order, skipped, panel


def channel_match(call, images, markers) -> dict:
    """Which markers each image has a channel for (`views.image_channel`, the
    matcher the bulk pass and every look use). Automatic gating judges every
    gate on the pixels, so a marker with no channel cannot be gated at all.

    {"matched": {image: [marker]}, "unmatched": {image: [marker]},
     "n_markers", "n_matched" (markers matched in at least one image),
     "state": "full" | "partial" | "none", "channels": {image: [first names]}}"""
    from plexora.plugins.gating.server.autogate import views

    matched, unmatched, channels = {}, {}, {}
    for project in images:
        ds = call.session.data(project)
        present = set(views.markers(ds))
        names = [] if ds.project.image.is_blank else views.channel_names(ds)
        channels[project] = names[:MAX_LIST]
        hit = [m for m in markers if m in present and names
               and views.image_channel(ds, m) is not None]
        matched[project] = hit
        unmatched[project] = [m for m in markers if m in present and m not in hit]
    pairs = sum(len(v) for v in matched.values())
    missing = sum(len(v) for v in unmatched.values())
    n_matched = len({m for v in matched.values() for m in v})
    state = "none" if not pairs else ("partial" if missing else "full")
    return {"matched": matched, "unmatched": unmatched, "n_markers": len(markers),
            "n_matched": n_matched, "state": state, "channels": channels}


def _require_channels(match, force):
    """Refuse a start the pixels cannot carry: no marker with a channel ever;
    some without one unless `force`."""
    unmatched = sorted({m for v in match["unmatched"].values() for m in v})
    if match["state"] == "none":
        raise AgentError(
            "precondition_missing",
            f"none of the {match['n_markers']} markers matches a channel of the image, so "
            "none can be gated: automatic gating judges every gate on the pixels. Name the "
            "image's channels after the markers (Edit project, channel names), then start "
            "again.",
            detail={"unmatched": unmatched[:MAX_LIST], "channels": match["channels"],
                    "hint": "rename the image's channels to the table's marker names"})
    if match["state"] == "partial" and not force:
        raise AgentError(
            "precondition_missing",
            f"only {match['n_matched']} of the {match['n_markers']} markers match a channel "
            f"of the image; {len(unmatched)} do not ({', '.join(unmatched[:12])}"
            f"{', ...' if len(unmatched) > 12 else ''}). Start again with force=true "
            "(`plexora ai run gating --force`) to gate only the markers that match, or name "
            "the image's channels after the markers first.",
            detail={"matched": match["matched"], "unmatched": unmatched[:MAX_LIST],
                    "channels": match["channels"], "hint": "force"})


# -- the set-up a session needs before it plans anything ----------------------


def setup_images(session, images, purpose="gating") -> list:
    """The images whose set-up a session needs: every one, except that an
    auto-contrast run reads an image with no cell table by its pixels alone
    (its cells found in the image, `image_cells`), so there is no column for
    the set-up form to ask about."""
    if purpose != "contrast":
        return list(images)
    return [name for name in images if session.project(name).has_table]


def setup_needs(session, images, *, confirmations=True) -> dict | None:
    """What the viewer's set-up form would still ask before gating can run on
    `images`, required inputs only: those not found in the project
    (`Requires.missing_from`) and, with `confirmations`, those found but never
    confirmed (`unconfirmed_from`: the columns the predictor picked, and the
    expression matrix). None when there is nothing.

    {"tool", "project", "projects", "keys", "labels", "missing",
     "unconfirmed", "requirements"}: the shape the agent panel's `needs_setup`
    handler opens the form with."""
    requires = PLUGIN.requires
    missing, unconfirmed, projects, rows = [], [], [], {}
    for name in images:
        record = session.project(name)
        gone = [r for r in requires.missing_from(record) if not r.optional]
        unsure = ([r for r in requires.unconfirmed_from(record) if not r.optional]
                  if confirmations else [])
        if not gone and not unsure:
            continue
        projects.append(name)
        for row, into in [(r, missing) for r in gone] + [(r, unconfirmed) for r in unsure]:
            if row.key not in into:
                into.append(row.key)
            rows.setdefault(row.key, row)
    if not projects:
        return None
    keys = list(dict.fromkeys(missing + unconfirmed))
    return {"tool": OWNER, "project": projects[0], "projects": projects, "keys": keys,
            "labels": [rows[k].label for k in keys], "missing": missing,
            "unconfirmed": unconfirmed, "requirements": [rows[k].describe() for k in keys]}


def _words(labels) -> str:
    labels = [str(label) for label in labels]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]


def _setup_message(needs) -> str:
    """The set-up a session waits on, in the user's words."""
    where = needs["project"] if len(needs["projects"]) == 1 else f"{len(needs['projects'])} images"
    missing = [needs["labels"][needs["keys"].index(k)] for k in needs["missing"]]
    unsure = [needs["labels"][needs["keys"].index(k)] for k in needs["keys"]
              if k not in needs["missing"]]
    parts = []
    if missing:
        parts.append(f"{_words(missing)} {'is' if len(missing) == 1 else 'are'} not set")
    if unsure:
        parts.append(f"{_words(unsure)} {'has' if len(unsure) == 1 else 'have'} not been "
                     "confirmed")
    return (f"Automatic gating is waiting for the set-up of {where}: {' and '.join(parts)}. "
            "Finish the set-up form in the viewer (Thresholding opens it).")


def _setup_refusal(gaps, setup):
    """A start refused for a column the project does not have: what is
    missing, and where it is set."""
    where = gaps["project"] if len(gaps["projects"]) == 1 else f"{len(gaps['projects'])} images"
    missing = [gaps["labels"][gaps["keys"].index(k)] for k in gaps["missing"]]
    how = ("open the project in the viewer and answer the set-up form (Thresholding opens it), "
           "or set the column in Edit project, then start again")
    if setup == "ask":
        how = "ask the user to " + how
    return AgentError(
        "precondition_missing",
        f"automatic gating cannot start on {where}: {_words(missing)} "
        f"{'is' if len(missing) == 1 else 'are'} not set, and only the user can say which "
        f"column holds {'it' if len(missing) == 1 else 'them'}. To go on, {how}.",
        detail={"setup": gaps, "hint": "set-up form"})


def _matrix_refusal(expression):
    """A start refused because nobody confirmed which values to gate and
    they could not settle it: why, the matrices there are, and how to name one."""
    projects = expression.get("projects") or []
    options = expression.get("options") or []
    recommendation = expression.get("recommendation") or {}
    listed = ", ".join(f"{o['value']} ({str(o.get('kind') or 'unknown').replace('_', ' ')})"
                       for o in options) or "X"
    return AgentError(
        "precondition_missing",
        f"which expression values to gate on {', '.join(projects) or 'this image'} is not "
        f"confirmed, and could not be worked out: {recommendation.get('why') or 'unclear'}. "
        f"The matrices: {listed}. Name one: `plexora ai run gating <project> --matrix "
        "<X|layer:NAME> --log1p <on|off>` (log1p on for raw intensities, off for values "
        "already logged), or confirm it in the viewer's set-up form.",
        detail={"expression": expression, "hint": "--matrix / --log1p"})


def _unconfirmed_note(session, images, setup) -> dict:
    """A session that could not wait went on with columns nobody confirmed:
    said in the start output, so the caller can tell the user which."""
    if setup == "wait":
        return {}
    needs = setup_needs(session, images)
    keys = [k for k in (needs or {}).get("unconfirmed", ()) if k != "features"]
    if not keys:
        return {}
    labels = [needs["labels"][needs["keys"].index(k)] for k in keys]
    return {"unconfirmed_setup": {
        "keys": keys, "labels": labels,
        "note": (f"{_words(labels)} {'was' if len(labels) == 1 else 'were'} found by the "
                 "column predictor and never confirmed; this session uses "
                 f"{'it' if len(labels) == 1 else 'them'} as found. Tell the user, and to "
                 "check the set-up form (Thresholding opens it) if gates land on the wrong "
                 "cells.")}}


def _given_expression(call, images, choice):
    """Apply the matrix the caller settled (`SessionOptions.expression`) to
    each image: a user's choice everywhere, a model's only where nothing is
    confirmed. Checked like an answer to an expression_setup packet: a matrix
    the image has, and never log1p on values already logged (or below zero).

    Returns ([{project, choice, why, basis}], receipt ids)."""
    from plexora.agent import registry

    if choice is None:
        return [], []
    applied, receipts = [], []
    for index, project in enumerate(images):
        found = registry.invoke(call.session, "inspect_expression_sources",
                                {"project": project}, policy=call.policy,
                                audit=call.audit, link=call.link)
        if not found["ok"]:
            raise AgentError("precondition_missing", f"{project}: "
                             f"{found['error']['message']}")
        result = found["result"]
        inferred = choice.basis == "inferred"
        if inferred and result["current"]["confirmed"]:
            now = result["current"]
            if (now["features_layer"], bool(now["features_log"])) == (choice.features_layer,
                                                                      choice.features_log):
                # Applied before the start (a driver's set-up, `prepare`):
                # kept on the record, not done again.
                applied.append({"project": project, "basis": "inferred",
                                "choice": {"features_layer": choice.features_layer,
                                           "features_log": choice.features_log},
                                "why": choice.why or "read from the values by a model"})
            continue
        kinds = {o["value"]: o.get("kind") for o in result["options"]}
        kind = kinds.get(choice.features_layer)
        refusal = None
        if choice.features_layer not in kinds:
            refusal = AgentError("invalid_input", f"{choice.features_layer!r} is not a matrix "
                                 f"of {project!r}", detail={"allowed": list(kinds)})
        elif choice.features_log and kind in ("log_like", "scaled"):
            refusal = AgentError("invalid_input", f"{project}: {choice.features_layer} "
                                 + ("already looks log-transformed; log1p on it would "
                                    "transform it twice" if kind == "log_like" else
                                    "has values below zero (scaled or centred); log1p is not "
                                    "defined there"),
                                 detail={"hint": "log1p off"})
        if refusal is not None:
            if inferred:
                continue          # a model's reading the values contradict: not taken
            raise refusal
        done = registry.invoke(call.session, "set_expression_source",
                               {"project": project, "features_layer": choice.features_layer,
                                "features_log": choice.features_log, "confirm": True},
                               policy=call.policy, audit=call.audit, link=call.link,
                               notify=call.notify,
                               operation_id=f"{call.operation_id}.given{index + 1}")
        if not done["ok"]:
            raise AgentError(done["error"]["code"], f"{project}: {done['error']['message']}")
        receipt = (done["result"] or {}).get("receipt") or {}
        if receipt.get("changed") and receipt.get("operation_id"):
            receipts.append(receipt["operation_id"])
        applied.append({"project": project, "basis": choice.basis,
                        "choice": {"features_layer": choice.features_layer,
                                   "features_log": choice.features_log},
                        "why": choice.why or ("named by the user" if choice.basis == "user"
                                              else "read from the values by a model")})
    return applied, receipts


def _plan(call, inp, images) -> dict:
    """The markers a session gates and the image channels they match; refused
    when the pixels cannot carry them (`_require_channels`)."""
    order, skipped, panel = _markers_for(call, images, inp.markers)
    if not order:
        raise AgentError("invalid_input", "no markers to gate")
    match = channel_match(call, images, order)
    _require_channels(match, inp.force)
    return {"order": order, "skipped": skipped, "panel": panel, "match": match}


def _plan_fields(call, inp, images, plan) -> dict:
    """The planned part of a session record (`_plan`, then the pixel size,
    QC, biology and one unit per image and marker), plus `unresolved`."""
    from plexora.plugins.gating.server.autogate import context
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import views

    order, panel, match = plan["order"], plan["panel"], plan["match"]
    pixel = _pixel_check(call, images, inp)
    display = _display_plan(images, inp)
    qc_blocks = _qc_check(call, images)
    biology_record = _biology_for(call, images, inp, panel)
    if biology_record and biology_record.get("contexts"):
        from plexora.plugins.gating.server.autogate import biology as biomod

        overlaid = biomod.overlay(panel, biology_record)["order"]
        order = [m for m in overlaid if m in order] + [m for m in order if m not in overlaid]
    units = {}
    for project in images:
        markers = views.markers(call.session.data(project))
        seen = set(match["matched"].get(project) or ())
        for marker in order:
            unit = {"project": project, "marker": marker,
                    "state": "pending" if marker in seen
                    else "skipped_no_marker" if marker not in markers else "skipped_no_channel"}
            if unit["state"] == "skipped_no_channel":
                unit["reason"] = ("no image channel matches this marker: automatic gating "
                                  "judges every gate on the pixels")
            units[engines.unit_key(project, marker)] = unit
    unresolved = [m for m in panel["unresolved"] if m in order]
    # The planner reviews every marker's partners (`context.review_markers`,
    # each batch twice): it fills empty partner slots whatever the context,
    # and changes a stated relation only for a tissue or disease named or
    # inferred. Once per panel and context; a later session reads what was
    # settled. An auto-contrast session gates nothing, so asks nothing.
    reviewed = context.review_markers(panel, biology_record, order) \
        if inp.purpose == "gating" else []
    queue = context.review_queue(reviewed) \
        if reviewed and not context.review_done(panel, biology_record, reviewed) else []
    settled = context.review_for(panel, biology_record) if reviewed and not queue else {}
    return {"order": order, "skipped_markers": plan["skipped"], "units": units,
            "panel_hash": panel["panel_hash"], "biology": biology_record,
            "panel_pending": bool(unresolved), "unresolved": unresolved,
            "review_queue": queue, "review_pending": bool(queue),
            "review_batches": len({q["batch"] for q in queue}),
            "partner_review": context.review_brief(settled) if settled.get("done") else None,
            "channel_match": {k: match[k] for k in ("state", "n_markers", "n_matched",
                                                    "unmatched")},
            "pixel": pixel, "display": display, "qc_exclusion": qc_blocks,
            "qc_missing": _qc_missing(qc_blocks)}


def _unplanned_fields() -> dict:
    """A session waiting on a column it cannot plan without: planned at
    `_settle_setup`."""
    return {"order": [], "skipped_markers": [], "units": {}, "panel_hash": None,
            "biology": None, "panel_pending": False, "unresolved": [],
            "review_queue": [], "review_pending": False,
            "review_batches": 0, "partner_review": None, "channel_match": None,
            "pixel": {}, "display": {}, "qc_exclusion": {}, "qc_missing": []}


def _channels_brief(match) -> dict:
    if not match:
        return {"state": "unplanned"}
    return ({k: match[k] for k in ("state", "n_markers", "n_matched")}
            | {"unmatched": sorted({m for v in match["unmatched"].values() for m in v})})


def _fingerprints(session, images) -> dict:
    """{image: "<matrix>|<log1p 0/1>"}: which values each image is read from
    (`Table.expression_fingerprint`), as the session started on them."""
    out = {}
    for name in images:
        record = session.project(name)
        out[name] = f"{record.feature_source}|{int(bool(record.log_transformed))}"
    return out


def prepare(call, project, choice=None, *, setup="wait", purpose="gating") -> dict:
    """The deterministic half of a start, with no session: what a driver
    collects before it calls any model (`plexora.ai.harness.decision`).

    Settles the matrix where the values settle it (`_expression_check`,
    applied and confirmed) or where the caller's `choice` does
    (`_given_expression`), receipted under `call.operation_id`; then says
    what is still open. {"needs", "missing", "expression", "applied",
    "receipts"}: `needs` is what a session started with `setup` would wait
    on or refuse for -- wait: everything the set-up form would ask; fail and
    ask: a column not found, and the matrix when it is unsettled -- or None.
    `missing`: the columns not found. Nothing else is written. An
    auto-contrast run (`purpose`) on an image with no cell table has nothing
    to set up (`setup_images`)."""
    call.session.project(project)
    images = setup_images(call.session, [project], purpose)
    if not images:
        return {"needs": None, "missing": None, "expression": {"status": "confirmed"},
                "applied": [], "receipts": []}
    if isinstance(choice, dict):
        choice = ExpressionChoice(**choice)
    gaps = setup_needs(call.session, images, confirmations=False)
    given, given_receipts = _given_expression(call, images, choice)
    expression, receipts = _expression_check(call, images)
    pending = expression.get("status") == "pending"
    if setup == "wait":
        needs = setup_needs(call.session, images)
    else:
        needs = gaps
        if pending:
            matrix = setup_needs(call.session, images) or {}
            if "features" in (matrix.get("keys") or ()):
                needs = needs or {**matrix, "keys": [], "labels": [], "missing": [],
                                  "unconfirmed": [], "requirements": []}
                at = matrix["keys"].index("features")
                needs = {**needs, "keys": needs["keys"] + ["features"],
                         "labels": needs["labels"] + [matrix["labels"][at]],
                         "unconfirmed": needs["unconfirmed"] + ["features"],
                         "requirements": needs["requirements"] + [matrix["requirements"][at]]}
    return {"needs": needs, "missing": gaps, "expression": expression,
            "applied": given + list(expression.get("applied") or []),
            "receipts": list(given_receipts) + list(receipts)}


def start(call, inp):
    from plexora.agent.sessions.store import new_session_id
    from plexora.plugins.gating.server.autogate import engine as engines

    if inp.scope == "project":
        if not inp.project:
            raise AgentError("invalid_input", "scope=project needs `project`")
        call.session.project(inp.project)
        images = [inp.project]
        reference = inp.project
        dataset_name = None
    else:
        if not inp.dataset:
            raise AgentError("invalid_input", "scope=dataset needs `dataset`")
        from plexora import datasets

        try:
            cohort = datasets.dataset(inp.dataset)
        except KeyError as exc:
            raise AgentError("invalid_input", str(exc.args[0]),
                             detail={"hint": "list_datasets"}) from None
        requires = PLUGIN.requires
        images = []
        for name in cohort.projects:
            record = call.session.project(name)
            if not requires.applies_to(record):
                continue
            if not requires.missing_from(record) or (
                    not setup_images(call.session, [name], inp.purpose)
                    and not record.image.is_blank):
                images.append(name)
        if not images:
            raise AgentError("precondition_missing", f"{cohort.name!r} has no gateable images")
        dataset_name = cohort.name
        if inp.reference_image:
            if inp.reference_image not in images:
                raise AgentError("invalid_input", f"{inp.reference_image!r} is not a gateable "
                                 "image of this dataset", detail={"images": images})
            reference = inp.reference_image
        else:
            # An image with no table (an auto-contrast run's) counts no cells.
            sizes = {name: call.session.data(name).table.geometry().height
                     if call.session.project(name).has_table else 0 for name in images}
            reference = max(images, key=lambda n: (sizes[n], -images.index(n)))
        images = [reference] + [n for n in images if n != reference]
    if len(images) > 1:
        # The reference image stays held while the bulk pass walks the rest;
        # an evicted table would refit every marker for every packet.
        call.session.table_limit = max(call.session.table_limit, 4)
    # A column the set-up form would have to ask for (no cell id, no
    # coordinates): nothing can be planned without it, and only the user can
    # name it -- so only a session that waits for the form starts without it.
    asks = setup_images(call.session, images, inp.purpose)
    gaps = setup_needs(call.session, asks, confirmations=False)
    if gaps and inp.setup != "wait":
        raise _setup_refusal(gaps, inp.setup)
    # Before anything is written (the expression check can set a matrix): a
    # marker with no image channel has no pixels to judge its gate on.
    plan = None if gaps else _plan(call, inp, images)
    given, given_receipts = _given_expression(call, images, inp.expression)
    expression, expression_receipts = _expression_check(call, images)
    if given:
        expression = {**expression, "applied": given + list(expression.get("applied") or [])}
        if expression["status"] == "confirmed":
            expression["status"] = "applied"
    if inp.setup == "fail" and expression.get("status") == "pending":
        raise _matrix_refusal(expression)
    # Everything the form would still ask -- columns found but never
    # confirmed, and a matrix the values did not settle -- for a session that
    # waits on it. One that cannot wait goes on with what was found.
    needs = setup_needs(call.session, asks) if inp.setup == "wait" else None
    fields = _plan_fields(call, inp, images, plan) if plan else _unplanned_fields()
    unresolved = fields.pop("unresolved")
    session_id = new_session_id()
    record = {
        "session_id": session_id, "kind": "gating", "created_at": _now(),
        "principal": getattr(call.policy, "principal", None) or "agent",
        "options": inp.model_dump(mode="json"), "scope": inp.scope,
        "dataset": dataset_name, "state": "created", "operation_id": call.operation_id,
        "images": images, "reference_image": reference, **fields,
        "used": {"packets": 0, "images": 0, "pixels": 0, "chars": 0},
        "receipts": list(given_receipts) + list(expression_receipts), "questions": [],
        "packet_seq": 0, "write_seq": 0, "mirror": _mirror_at_start(call, inp),
        "expression": expression, "classifier": {"schema": 1},
    }
    if needs:
        record["state"] = "needs_setup"
        record["setup_wait"] = needs
    elif expression.get("status") == "pending":
        record["state"] = "needs_setup"
    else:
        record["expression_fingerprints"] = _fingerprints(call.session, images)
    if inp.mode == "apply":
        # The gates as they stand, kept before the run writes over them: the
        # user's hand edits since the last version (`model.save_version`
        # keeps nothing new when the latest version holds them already).
        _save_versions(call, images, session_id=None)
    order = record["order"]
    st = engines.store()
    st.create(record)
    st.sweep()
    if record["units"]:
        # As many readers as the panel's dependencies keep busy (`lanes_for`).
        with engines.engine_for(call, session_id, st=st) as engine:
            record["useful_lanes"] = engine.record["useful_lanes"] = engine.useful_lanes()
    receipt = make_receipt(call, changed=False, before=None,
                           after={"session_id": session_id, "images": images,
                                  "markers": order},
                           persistent_state=STATE, reversible=False,
                           extra={"gating_session": session_id, "note": "the session's "
                                  "writes are receipted as <operation_id>.<nnn>"})
    packet = images_ = None
    if record["state"] == "needs_setup" and needs:
        # Nothing is planned or profiled until the user has finished the
        # viewer's set-up form: every number would be of the wrong columns or
        # values, and an answer given while the run went on would switch them
        # under it. The panel opens the form (`needs_setup`); no packet is
        # issued, so no model answers it in the user's place.
        job = {"job_id": None}
        with engines.engine_for(call, session_id) as engine:
            phase = engines.phase_for(engine.record)
            progress = engine.progress()
        _announce(call, record, session_id, "started", phase=phase, progress=progress,
                  order=order, images=images, mode=inp.mode,
                  view_id=record["mirror"].get("view_id"),
                  viewer_attached=_mirroring(record["mirror"]))
        _announce(call, record, session_id, "needs_setup", phase="planning",
                  view_id=record["mirror"].get("view_id"), needs=needs,
                  message=_setup_message(needs))
    elif record["state"] == "needs_setup":
        # Nothing is profiled until the matrix is settled: every number the
        # bulk pass computes would be of the wrong values.
        job = {"job_id": None}
        with engines.engine_for(call, session_id) as engine:
            packet, images_, _status = engine.issue()
            phase = engines.phase_for(engine.record)
            progress = engine.progress()
        _announce(call, record, session_id, "started", phase=phase, progress=progress,
                  order=order, images=images, mode=inp.mode,
                  view_id=record["mirror"].get("view_id"),
                  viewer_attached=_mirroring(record["mirror"]))
        _announce(call, record, session_id, "needs_setup", phase="planning",
                  view_id=record["mirror"].get("view_id"),
                  needs=_setup_needs(images, expression))
    else:
        job = _submit_bulk(call, session_id)
        with engines.engine_for(call, session_id) as engine:
            engine.record["bulk_job_id"] = job["job_id"]
            phase = engines.phase_for(engine.record)
            progress = engine.progress()
        _announce(call, record, session_id, "started", phase=phase, progress=progress,
                  order=order, images=images, mode=inp.mode,
                  view_id=record["mirror"].get("view_id"),
                  viewer_attached=_mirroring(record["mirror"]))
    out = {"session_id": session_id, "job_id": job["job_id"], "scope": inp.scope,
            "images": images, "reference_image": reference, "order": order,
            "n_units": len(record["units"]), "lanes": engines.lanes_for(record),
            "skipped_markers": record["skipped_markers"],
            "unresolved_markers": unresolved, "mode": inp.mode,
            **({"partner_review": {"markers": len({m for q in record["review_queue"]
                                                   for m in q["markers"]}),
                                   "packets": len(record["review_queue"])}}
               if record.get("review_queue") else {}),
            "mirror": {k: record["mirror"].get(k) for k in ("status", "view_id", "last_error",
                                                            "hint", "follow")
                       if record["mirror"].get(k)},
            "receipt": receipt.model_dump(mode="json"),
            "biology": _biology_brief(record["biology"]),
            "resource": session_uri(session_id), "expression": expression,
            "pixel": _pixel_brief(record["pixel"]), "state": record["state"],
            "channels": _channels_brief(record["channel_match"]),
            "qc_exclusion": record["qc_exclusion"],
            **_qc_warning(record["qc_exclusion"], basic=_basic_qc_on(inp.basic_qc)),
            **_unconfirmed_note(call.session, images, inp.setup),
            **_guide(inp.reading, inp.known_guide, inp.engine),
            **_delegate_block(record, session_id, inp.model_dump(mode="json"), packet),
            "next": f"{tool_name_of('gating.next')}(session_id) -- packets start as soon as "
                    "the first markers are profiled; answer each with "
                    f"{tool_name_of('gating.answer')}"}
    if needs:
        out["setup"] = needs
        out["next"] = (f"{_setup_message(needs)} {tool_name_of('gating.next')}(session_id) "
                       "answers needs_setup until they have; the markers are planned once "
                       "they have.")
    if packet is None and inp.delegate and "reading_guide" in out and \
            isinstance(out["reading_guide"], dict):
        # A coordinator that hands the packets to workers never reads the
        # guide (~20k characters); each worker fetches its own.
        out["reading_guide"] = ("not sent: the workers of `delegate` fetch it with "
                                f"{tool_name_of('gating.session_status')}(session_id, "
                                "guide=true); call that yourself if you answer packets")
        out.pop("reading_note", None)
    if packet is not None:
        if _mirroring(record["mirror"]):
            # The tab puts the same question to the user (its requirements
            # prompt), so either of them can answer it.
            out["mirror"] = _mirror(call, session_id, packet)
        out["packet"] = packet
        out["_images"] = [{"data": data, "format": fmt} for data, fmt in images_ or []]
        out["next"] = ("answer the expression_setup packet with the user's choice "
                       f"({tool_name_of('gating.answer')}), or let them confirm the "
                       "expression source in the viewer; then "
                       f"{tool_name_of('gating.next')}(session_id)")
    return out


class ContrastOptions(AgentModel):
    """Auto contrast on its own (`contrast_session_start`)."""
    scope: Literal["project", "dataset"] = Field(
        "project", description="project: one image; dataset: every image of a dataset.")
    project: str | None = Field(None, description="With scope=project.")
    dataset: str | None = Field(None, description="With scope=dataset: name or id.")
    markers: list[str] | None = Field(None, description="Default: every marker except "
                                      "structural channels (DNA and the like).")
    check: Literal["vision", "rule"] = Field(
        "vision", description="vision: the model judges a contrast grid per batch of markers "
                              "(code picks the column); rule: the calibration's own window, "
                              "no model.")
    redo: bool = Field(False, description="Ask again about markers an earlier run settled "
                                          "(their choices are taken off first). Off, a marker "
                                          "already set is skipped -- and an image whose "
                                          "markers all are asks nothing.")
    mirror: bool = Field(False, description="Show it in an open viewer: each marker in turn "
                                            "in the second channel slot, its slider dragged "
                                            "to the chosen window.")
    view_id: str | None = None
    mirror_delay_ms: int = Field(mirroring.DEFAULT_DELAY_MS, ge=0, le=5000)
    setup: Literal["ask", "wait", "fail"] = Field(
        "ask", description="As gating_session_start's: what happens when the project's set-up "
                           "(columns, expression matrix) is not finished.")
    expression: ExpressionChoice | None = None
    image_format: Literal["webp", "png"] = "webp"
    reading: Literal["once", "every_packet"] = "once"
    known_guide: str | None = None
    agent: str = Field("agent", max_length=80, description="Who answers (the model, say).")
    reuse_answers: bool = True


def contrast_start(call, inp):
    """Auto contrast as its own module: a session of the gating engine with
    `purpose: contrast` -- the set-up a contrast check needs (matrix, pixel
    size, the panel's compartments), the calibration, and the check itself;
    no marker profiled, nothing gated. Its packets come through gating_next
    and gating_answer like any session's."""
    from plexora.agent.evidence import calibration

    if inp.redo:
        images = [inp.project] if inp.scope == "project" and inp.project else []
        if inp.scope == "dataset" and inp.dataset:
            from plexora import datasets

            try:
                images = list(datasets.dataset(inp.dataset).projects)
            except KeyError as exc:
                raise AgentError("invalid_input", str(exc.args[0]),
                                 detail={"hint": "list_datasets"}) from None
        for project in images:
            channels = None
            if inp.markers:
                from plexora.plugins.gating.server.autogate import views

                ds = call.session.data(project)
                channels = [c for c in (views.image_channel(ds, m) for m in inp.markers) if c]
            calibration.forget_choices(project, channels)
    options = SessionOptions(
        scope=inp.scope, project=inp.project, dataset=inp.dataset, markers=inp.markers,
        purpose="contrast", display_check=inp.check, mode="propose", force=True,
        audit_sheet=False, replan=False, delegate=False, mirror=inp.mirror,
        view_id=inp.view_id, mirror_delay_ms=inp.mirror_delay_ms, setup=inp.setup,
        expression=inp.expression, image_format=inp.image_format, reading=inp.reading,
        known_guide=inp.known_guide, agent=inp.agent, reuse_answers=inp.reuse_answers)
    out = start(call, options)
    out["purpose"] = "contrast"
    if "packet" not in out and not out.get("setup"):
        out["next"] = (f"{tool_name_of('gating.next')}(session_id) -- the set-up packets "
                       "(the panel's compartments, the pixel size) and then one contrast grid "
                       "per batch of markers; answer each with "
                       f"{tool_name_of('gating.answer')}. The session is done when every "
                       "marker reads `display_set`; close it with "
                       f"{tool_name_of('gating.session_finish')}.")
    return out


def _biology_for(call, images, inp, panel):
    """The session's biology record (`autogate.biology`): the user's words
    when given, else the reference image's metadata, else what the panel's
    markers imply -- each marked with its source."""
    from plexora.plugins.gating.server.autogate import biology

    if inp.biology is not None and any((inp.biology.tissue, inp.biology.disease,
                                        inp.biology.notes, inp.biology.original)):
        return biology.resolve(inp.biology.tissue, inp.biology.disease, inp.biology.notes,
                               source="user", original=inp.biology.original,
                               interpretation=inp.biology.interpretation)
    return biology.infer(panel, biology.project_metadata(call, images[0]))


def _biology_brief(record):
    if not record:
        return {"source": None, "note": "no tissue or disease context: every marker is read "
                                        "generically. If the user named the tissue or "
                                        "disease, start again with `biology`."}
    out = {k: record.get(k) for k in ("source", "said", "original", "contexts", "unmatched",
                                      "basis") if record.get(k)}
    if record.get("source") == "inferred":
        out["note"] = ("inferred from the panel's markers alone, not stated: confirm it with "
                       "the user, and do not report it as the sample's diagnosis")
    elif record.get("unmatched"):
        out["note"] = ("not in the shipped tissue and disease knowledge: passed to every look "
                       "in the user's words, with no relations of its own")
    return out


def _qc_check(call, images):
    """Per image, which QC failures every estimate of this session leaves out
    (`SessionOptions.qc`): counts only, never ids."""
    from plexora.agent import cell_exclusions

    out = {}
    for name in images:
        block = cell_exclusions.describe(call.session.data(name))
        out[name] = {k: block[k] for k in ("mode", "applied", "reason", "n_left_out",
                                           "n_cells", "fraction", "result_id", "origin",
                                           "n_exclude", "n_warn", "n_marker", "warning")
                     if k in block}
    return out


def _qc_missing(blocks) -> list:
    """The images whose QC mode leaves cells out, but that have no QC result
    to leave out: strict (or exclude) there does nothing."""
    return sorted(name for name, block in (blocks or {}).items()
                  if block.get("mode") not in (None, "off") and not block.get("applied")
                  and block.get("reason") == "no QC result for this image")


def _basic_qc_on(chosen) -> bool:
    """Whether basic QC runs: the session asked for it and QC is installed."""
    from plexora.agent import registry

    return bool(chosen) and any(c.name == "qc.basic_run" for c in registry.all_capabilities())


#: What the agent tells the user when basic QC runs (`_qc_warning`).
BASIC_QC_MESSAGE = (
    "{images} {verb} no QC result. Before gating, Plexora runs basic QC on {them}: QC's "
    "model-free checks (tissue folds -- also ones that come or go between imaging cycles "
    "-- tears, debris, saturated pixels, and lost nuclei where the image has a DNA channel "
    "per cycle), with no model calls, and leaves out the cells in the artifacts it is sure "
    "of; weaker ones are held back. Tell the user it is running and that nobody reviews it: "
    "recommend the full QC from the QC panel, which reviews these regions and the held-back "
    "ones, and checks focus, registration and segmentation too.")


def _qc_warning(blocks, *, basic=False) -> dict:
    """A note in the start output for `_qc_missing` images -- never a silent
    no-op: artifacts there (an edge band, a fold) reach every marker's fit,
    sample and look. With `basic`, the bulk pass runs basic QC on them first
    (`bulk._basic_qc`), and the note says so."""
    missing = _qc_missing(blocks)
    if not missing:
        return {}
    if basic:
        return {"basic_qc": {"images": missing, "message": BASIC_QC_MESSAGE.format(
            images=", ".join(missing), verb="has" if len(missing) == 1 else "have",
            them="it" if len(missing) == 1 else "them")}}
    return {"qc_warning": {
        "images": missing,
        "message": (f"{', '.join(missing)} {'has' if len(missing) == 1 else 'have'} no QC "
                    "result, so the QC mode leaves nothing out: an artifact there (an edge "
                    "band, a fold, autofluorescence) reaches every marker. "
                    "Tell the user; run QC first if they want it ("
                    + tool_name_of("qc.session_start") + "), or go on and let re-planning "
                    "flag an image whose looks keep naming the same artifact.")}}


def _expression_check(call, images):
    """Settle which matrix each image is gated on, before anything is
    profiled: a confirmed source is used as it is; a certain recommendation is
    applied (receipted, undoable); anything else waits on the user.

    Returns (expression record, receipt ids)."""
    from plexora.agent import registry

    applied, pending, found = [], [], {}
    receipts = []
    for index, project in enumerate(images):
        answer_ = registry.invoke(call.session, "inspect_expression_sources",
                                  {"project": project}, policy=call.policy,
                                  audit=call.audit, link=call.link)
        if not answer_["ok"]:
            continue          # nothing to read here (a node-bound table): used as it is
        result = answer_["result"]
        if result["current"]["confirmed"]:
            continue
        recommendation = result["recommendation"]
        choice = recommendation.get("choice")
        if recommendation["confidence"] == "certain" and choice:
            done = registry.invoke(call.session, "set_expression_source",
                                   {"project": project, **choice, "confirm": True},
                                   policy=call.policy, audit=call.audit, link=call.link,
                                   notify=call.notify,
                                   operation_id=f"{call.operation_id}.expr{index + 1}")
            if done["ok"]:
                receipt = (done["result"] or {}).get("receipt") or {}
                if receipt.get("changed") and receipt.get("operation_id"):
                    receipts.append(receipt["operation_id"])
                applied.append({"project": project, "choice": choice,
                                "why": recommendation["why"]})
                continue
        pending.append(project)
        found[project] = result
    if pending:
        first = found[pending[0]]
        common = [o["value"] for o in first["options"]
                  if all(o["value"] in [x["value"] for x in found[p]["options"]]
                         for p in pending)]
        return {"status": "pending", "projects": pending, "applied": applied,
                "source_kind": first["source_kind"], "current": first["current"],
                "options": [o for o in first["options"] if o["value"] in common],
                "recommendation": first["recommendation"], "rule": first["rule"]}, receipts
    if applied:
        return {"status": "applied", "applied": applied}, receipts
    return {"status": "confirmed"}, receipts


def _guide(reading, known=None, engine=schemas.DEFAULT_ENGINE):
    """The reading guide, once per session (`packets.reading_guide`): a
    packet names the entries it relies on (and its answer schema) instead of
    repeating them. Byte-stable for a build, so a client caches it; a caller
    that already holds this `guide_version` (`known_guide`) is not sent it."""
    from plexora.plugins.gating.server.autogate import packets

    if reading != "once":
        return {}
    version = packets.guide_version(engine)
    if known == version:
        return {"guide_version": version, "reading_guide": "unchanged (you hold it)"}
    return {"guide_version": version, "reading_guide": packets.reading_guide(engine),
            "reading_note": "every packet's `evidence.guide` names the entries of this guide "
                            "it relies on, and `answer_schema.see` its answer schema; keep it "
                            "for the whole session (pass `guide_version` back as "
                            "`known_guide` to skip it next time)"}


def _delegate_block(record, session_id, options, packet=None):
    """`delegate` (`plexora.ai.delegation`): how to hand the packet loop to
    worker conversations, a few markers each. Not for a finished session, or
    one started with `delegate: false`. `packet`, when one is in hand (a
    set-up packet), names whose worker starts."""
    from plexora.ai import delegation, tasks

    if not options["delegate"] or record.get("state") in schemas.FINISHED_STATES:
        return {}
    contexts = (record.get("biology") or {}).get("contexts") or []
    first = tasks.task_for("gating", packet.get("kind", "")) if isinstance(packet, dict) else None
    if first is None and schemas.panel_engine(options["engine"]):
        # v3's and v4's looks are all cell panels: their worker starts, not the one
        # for v1's first looks (`delegation.ROLES` `first_task`).
        first = tasks.task_for("gating", "cell_panel")
    from plexora.plugins.gating.server.autogate import engine as engines

    return {"delegate": delegation.block("gating_worker", first_task=first, session_id=session_id,
                                         lanes=engines.lanes_for(record),
                                         biology=", ".join(contexts) or None)}


def _pixel_brief(pixel):
    pixel = pixel or {}
    out = {"status": pixel.get("status")}
    if pixel.get("projects"):
        out["projects"] = {p: {k: e.get(k) for k in ("status", "value", "basis") if e.get(k)}
                           for p, e in pixel["projects"].items()}
    return out


def _display_plan(images, inp):
    """The session's contrast check (`display_setup`): pending per image when
    the model is to look (`display_check: vision`), else `rule` -- the
    calibration's windows stand, and the viewer is shown them all once."""
    check = getattr(inp, "display_check", "vision")
    if check == "off":
        return {"status": "off", "projects": {p: {"status": "off"} for p in images}}
    if check == "rule":
        return {"status": "rule",
                "projects": {p: {"status": "rule", "show_all": True} for p in images}}
    return {"status": "pending", "projects": {p: {"status": "pending"} for p in images}}


def _display_brief(display):
    display = display or {}
    out = {"status": display.get("status")}
    if display.get("projects"):
        out["projects"] = {
            p: {k: v for k, v in (("status", e.get("status")), ("reason", e.get("reason")),
                                  ("chosen", e.get("chosen")),
                                  ("observed", e.get("observed"))) if v}
            for p, e in display["projects"].items()}
    return out


def _pixel_check(call, images, inp):
    """Which images state no pixel size. Their pictures need one -- fields in
    microns, crops, scale bars -- so a `pixel_setup` packet estimates it first
    (`pixel_estimate`)."""
    from plexora.server.utils import pixel_scale

    missing = [p for p in images if not pixel_scale.pixel_size(call.session.project(p))]
    if not missing:
        return {"status": "calibrated"}
    return {"status": "pending", "projects": {p: {"status": "pending"} for p in missing}}


def _setup_needs(images, expression):
    from plexora.api.plugin import requirement

    return {"tool": OWNER, "project": (expression.get("projects") or images)[0],
            "projects": expression.get("projects") or images, "keys": ["features"],
            "requirements": [requirement("features").describe()]}


def _settle_expression(call, session_id, st):
    """A session waiting on the expression source moves on once it is settled
    -- by the agent's answer, or by the user in the viewer's modal."""
    from plexora.plugins.gating.server.autogate import engine as engines

    with engines.engine_for(call, session_id, st=st) as engine:
        record = engine.record
        if record["state"] != "needs_setup":
            return False
        expression = record.setdefault("expression", {})
        if expression.get("status") == "pending":
            projects = expression.get("projects") or record["images"]
            records = [call.session.project(p) for p in projects]
            if not all("features" in set(r.confirmed) for r in records):
                return False
            for name in projects:
                call.session.invalidate(name)
            expression.update(status="applied_by_user",
                              choice={"features_layer": records[0].feature_source,
                                      "features_log": bool(records[0].log_transformed)},
                              why="confirmed by the user in the viewer")
        engine.release_all(schemas.USER_SETUP_KINDS)
        record["state"] = "created"
        record["bulk_job_id"] = None
        record["expression_fingerprints"] = _fingerprints(call.session, record["images"])
        progress = engine.progress()
    _announce(call, record, session_id, "phase", phase="analyzing", progress=progress)
    return True


def _settle_setup(call, session_id, st):
    """A session waiting on the viewer's set-up form (`setup: wait`) is
    planned once the user has finished it -- from the tables as they are
    then, since the answers may have changed which column is what -- and the
    bulk pass goes on. A plan the pixels cannot carry (no marker matches a
    channel) stops it, saying why. A session waiting on the matrix alone
    (`setup: ask`) settles as before (`_settle_expression`)."""
    from plexora.plugins.gating.server.autogate import engine as engines

    with engines.engine_for(call, session_id, st=st) as engine:
        record = engine.record
        if record["state"] != "needs_setup":
            return False
        if not record.get("setup_wait"):
            waiting = None
        else:
            waiting = list(record["images"])
            options = dict(engine.options)
    if waiting is None:
        return _settle_expression(call, session_id, st)
    if setup_needs(call.session, waiting):
        return False
    for name in waiting:
        call.session.invalidate(name)
    inp = SessionOptions(**options)
    failure = None
    try:
        plan = _plan(call, inp, waiting)
        expression, receipts = _expression_check(call, waiting)
        fields = _plan_fields(call, inp, waiting, plan)
    except AgentError as exc:
        failure = exc
    with engines.engine_for(call, session_id, st=st) as engine:
        record = engine.record
        if record["state"] != "needs_setup":
            return False
        if failure is None:
            fields.pop("unresolved")
            record.update(fields)
            record["receipts"] = list(receipts) + list(record.get("receipts") or [])
            records = [call.session.project(p) for p in waiting]
            before = record.get("expression") or {}
            asked = before.get("status") == "pending"
            record["expression"] = {
                **before, **expression,
                **({"status": "applied_by_user"} if asked else {}),
                "choice": {"features_layer": records[0].feature_source,
                           "features_log": bool(records[0].log_transformed)},
                "why": ("confirmed by the user in the viewer's set-up form" if asked
                        else before.get("why") or expression.get("why")
                        or "settled before the set-up form was finished")}
            record["setup_done"] = {"at": _now(), "keys": record["setup_wait"]["keys"]}
            record.pop("setup_wait", None)
            record["state"] = "created"
            record["bulk_job_id"] = None
            record["expression_fingerprints"] = _fingerprints(call.session, waiting)
            record["useful_lanes"] = engine.useful_lanes()
            progress = engine.progress()
    if failure is not None:
        _stop(call, st, session_id, "setup_failed",
              f"The set-up is finished, but the run cannot go on: {failure.message}")
        return False
    _announce(call, record, session_id, "phase", phase="analyzing", progress=progress,
              order=record["order"])
    return True


def _stop(call, st, session_id, by, reason):
    """Stop a session for a reason of its own (not the user's Stop): its
    bulk pass at its next unit, its driver at its next call -- which is told
    `reason` -- and the tabs now."""
    from plexora.agent import jobs
    from plexora.agent.audit import now_iso
    from plexora.plugins.gating.server.autogate import engine as engines

    st.set_control(session_id, stopped=True, stopped_by=by, stopped_at=now_iso(),
                   paused=False, stop_reason=reason)
    record = st.load(session_id)
    if record.get("state") == "bulk_running" and record.get("bulk_job_id"):
        jobs.store().cancel(record["bulk_job_id"])
    _announce(call, record, session_id, "finished", reason=by, message=reason,
              state=record.get("state"), summary=engines.summary_of(record),
              phase="summarizing")


def _matrix_moved(call, st, session_id):
    """Stop a session whose images are no longer read from the values it
    started on: the matrix or log1p switched (the set-up form, Edit project,
    another agent) while it ran. Every profile and gate so far is in the old
    values' units, and going on would mix the two. Returns what `gating_next`
    answers, or None when nothing moved."""
    try:
        record = st.load(session_id)
    except Exception:              # noqa: BLE001 -- an unknown session is next_packet's to report
        return None
    then = record.get("expression_fingerprints")
    if not then or record.get("state") in schemas.FINISHED_STATES:
        return None
    now = _fingerprints(call.session, list(then))
    moved = {p: (then[p], now[p]) for p in then if now.get(p) != then[p]}
    if not moved:
        return None

    def read(value):
        layer, log = value.rsplit("|", 1)
        return f"{layer} with log1p {'on' if log == '1' else 'off'}"

    # Counted as the panel's summary counts them (`engine.summary_of`).
    written = sum(1 for u in (record.get("units") or {}).values() if u.get("receipts"))
    project, (before, after) = next(iter(moved.items()))
    reason = (f"The expression values of {project} changed while automatic gating ran "
              f"(from {read(before)} to {read(after)}), so it stopped: what it measured so far "
              "is in the old values' units."
              + (f" The {written} gate{'s' if written != 1 else ''} it wrote are on the old "
                 "values: clear them (the eraser beside each marker, or Clear all gates) "
                 "before gating again." if written else "")
              + " Start it again to gate on the new values.")
    _stop(call, st, session_id, "matrix_changed", reason)
    return _halted(call, st, session_id)


def session_uri(session_id):
    """The MCP resource a session is readable at (`plexora.mcp.resources_gating`)."""
    return f"plexora://gating/session/{session_id}"


def _mirror_at_start(call, inp):
    """The session's mirror, probed once: `pending` when a tab can be driven,
    `off` (and why) when none can -- a viewer that predates agent control,
    no tab open, no server attached. A dataset's session gates its images
    side by side, so it follows the tab (`follow: tab`): only the image the
    tab shows is mirrored, and the tab is never sent to another; a
    project's session (`follow` None) takes the tab to its image."""
    mirror = {"enabled": inp.mirror, "view_id": inp.view_id, "status": "off",
              "last_error": None, "follow": "tab" if inp.scope == "dataset" else None}
    if not inp.mirror:
        return mirror
    from plexora.agent import viewer

    try:
        view = viewer.resolve_view(viewer.require(call.link), inp.view_id)
    except AgentError as exc:
        mirror["last_error"] = {"code": exc.code, "message": exc.message}
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        mirror["hint"] = ((detail.get("hint") or viewer.NOT_AVAILABLE_HINT)
                          + f"; then {tool_name_of('gating.session_status')}(session_id, "
                            "reattach_viewer=true)")
        return mirror
    mirror.update(status="pending", view_id=view.get("view_id") or inp.view_id)
    return mirror


def _now():
    from plexora.agent.audit import now_iso

    return now_iso()


def _submit_bulk(call, session_id):
    from plexora.agent import jobs

    capability = _cap_by_name("gating.session_bulk")
    child = dataclasses.replace(call, capability=capability,
                                operation_id=f"{call.operation_id}.bulk",
                                arguments={"session_id": session_id}, receipted=False,
                                extras=dict(call.extras))
    return jobs.submit(child, BulkInput(session_id=session_id))


def _announce(call, record, session_id, event, /, **payload):
    """Tell every tab open on one of the session's images what the session is
    doing (`schemas.SESSION_EVENTS`; core's agent panel listens)."""
    from plexora.plugins.gating.server.autogate import events

    images = record.get("images") if isinstance(record, dict) else [record]
    events.announce(call.notify, images, session_id, event, **payload)


class BulkInput(AgentModel):
    session_id: str


def bulk(call, inp):
    from plexora.plugins.gating.server.autogate import bulk as bulk_pass

    return bulk_pass.run(call, inp)


class JobInput(AgentModel):
    session_id: str


def session_classifier(call, inp):
    from plexora.plugins.gating.server.autogate import cellcalls

    return cellcalls.run_job(call, inp.session_id)


# -- next / answer ------------------------------------------------------------


#: The most packets of one session out at once (`gating_next(parallel=...)`).
MAX_PARALLEL = 8

_READER = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_.:-]+$",
                description="Who is answering, when several conversations answer one "
                            "session at once (one id per conversation). Omit when you are "
                            "the only one.")
_PARALLEL = Field(1, ge=1, le=MAX_PARALLEL,
                  description="The most packets of this session out at once, across every "
                              "reader. Above 1, markers that do not depend on each other "
                              "are handed out side by side; a marker still waits for the "
                              "partners it is judged beside.")


_TASKS = session_tools.TASKS


class NextInput(AgentModel):
    session_id: str
    wait_s: float = Field(10.0, ge=0, le=30, description="How long to wait for the bulk "
                                                         "pass when nothing is ready yet.")
    rerender: bool = Field(False, description="Draw the outstanding packet's images again "
                           "(same packet, same charge), in full: after a renderer change, "
                           "or when you no longer hold a packet an `as_in` names.")
    reader: str | None = _READER
    parallel: int = _PARALLEL
    tasks: list[str] | None = _TASKS


def _slim_mirror(mirror):
    """What an agent needs of the viewer's mirror: whether it worked, and
    whether the tab is watching at all (`detached`)."""
    if mirror.get("status") in ("ok", "queued") and not mirror.get("last_error"):
        return {k: mirror[k] for k in ("status", "sent", "resent", "detached") if k in mirror}
    return mirror


def _slim_progress(progress):
    return {"units_done": progress.get("units_done"), "units_total": progress.get("units_total"),
            **({"waiting_for_user": progress["waiting_for_user"]}
               if progress.get("waiting_for_user") else {})}


def _packet_result(packet, images):
    state = "needs_setup" if packet.get("kind") in schemas.USER_SETUP_KINDS else "decision"
    return {"state": state, "packet": packet,
            "_images": [{"data": data, "format": fmt} for data, fmt in images]}


def _halted(call, st, session_id):
    """`stopped` (the user stopped the session in the viewer) or `paused`,
    or None when the session may go on."""
    control = st.control(session_id)
    if control.get("stopped"):
        from plexora.agent import jobs

        try:
            record = st.load(session_id)
        except Exception:
            record = {}
        if record.get("state") == "bulk_running" and record.get("bulk_job_id"):
            jobs.store().cancel(record["bulk_job_id"])
        finish = tool_name_of("gating.session_finish")
        # The session's own stop (a matrix switched under it, a set-up the
        # pixels cannot carry) says why; the user's says it was theirs.
        reason = control.get("stop_reason")
        return {"state": "stopped", "by": control.get("stopped_by"),
                "note": reason or "the user stopped this session in the viewer",
                **({"reason": reason} if reason else {}),
                "next": f"{finish}(session_id, action='close') keeps the gates written so "
                        f"far; {finish}(session_id, action='rollback') undoes them. Then stop."}
    if control.get("paused"):
        return {"state": "paused", "by": control.get("paused_by"), "retry_after_s": 10,
                "note": "the user paused this session in the viewer"}
    return None


#: The last phase each session announced from this process (so a wait loop
#: says "analyzing" once, not every half second).
_LAST_PHASE: dict = {}


def _phase(call, record, session_id, phase, /, **payload):
    if _LAST_PHASE.get(session_id) == phase and not payload:
        return
    _LAST_PHASE[session_id] = phase
    _announce(call, record, session_id, "phase", phase=phase, **payload)


#: Per dataset session: (each image's state, marker counts, when sent) of the
#: last `dataset_progress` event from this process.
_LAST_DATASET_PROGRESS: dict = {}
#: Marker counts alone move the switcher's badges at most this often; an
#: image changing state (starts gating, finishes, needs review) always goes.
DATASET_PROGRESS_THROTTLE_S = 1.0


def _dataset_progress(call, record, session_id, progress, *, last=False):
    """Tell every tab of a dataset session where each image is
    (`Engine.dataset_progress`), when that changed."""
    import time

    dataset = (progress or {}).get("dataset_progress")
    if not dataset:
        return
    states = tuple(sorted((p, i["state"]) for p, i in dataset["images"].items()))
    counts = tuple(sorted(dataset["counts"].items()))
    now = time.monotonic()
    before = _LAST_DATASET_PROGRESS.get(session_id)
    if before is not None and not last:
        if before[0] == states and before[1] == counts:
            return
        if before[0] == states and now - before[2] < DATASET_PROGRESS_THROTTLE_S:
            return
    _LAST_DATASET_PROGRESS[session_id] = (states, counts, now)
    try:
        _announce(call, record, session_id, "dataset_progress", dataset_progress=dataset,
                  done=bool(last))
    except Exception:  # the switcher's badges are never a reason to fail a call
        pass


def _mirroring(mirror) -> bool:
    return bool(mirror.get("enabled")) and mirror.get("status") != "off"


def _busy(progress, outstanding):
    """What `gating_next` says to a reader while every decision it could be
    given waits on a packet another reader holds (`parallel` above 1)."""
    return {"state": "busy", "progress": progress, "retry_after_s": 1,
            "outstanding": outstanding,
            "note": "every marker left waits on a packet another reader is answering "
                    "(a partner it is judged beside, or an audit strip)",
            "next": f"call {tool_name_of('gating.next')} again; an answer frees the markers "
                    "that wait on it"}


def next_packet(call, inp):
    from plexora.agent.sessions.engine import DEFAULT_READER
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import packets

    st = engines.store()
    halted = _halted(call, st, inp.session_id)
    if halted:
        return halted
    st.claim(inp.session_id)
    _settle_setup(call, inp.session_id, st)
    halted = _halted(call, st, inp.session_id) or _matrix_moved(call, st, inp.session_id)
    if halted:
        return halted
    waiting = _waiting_on_setup(st, inp.session_id)
    if waiting:
        return waiting
    _resume_bulk(call, inp.session_id, st)
    deadline = time.monotonic() + float(inp.wait_s)
    needs = None
    while True:
        with engines.engine_for(call, inp.session_id, st=st) as engine:
            record = engine.record
            if record["state"] in schemas.FINISHED_STATES:
                return {"state": record["state"], "progress": engine.progress()}
            reader = inp.reader or DEFAULT_READER
            held = engine.outstanding(reader)
            outstanding = held[0] if held else None
            mirror = dict(record.get("mirror") or {})
            # "Continue in background": the session goes on, the tab is left be.
            detached = bool(st.control(inp.session_id).get("viewer_detached"))
            options = dict(engine.options)
            if outstanding:
                packet, images = st.read_packet(inp.session_id, outstanding)
                # A sheet trimmed for an earlier reader is drawn again whole.
                trimmed = packet.get("_sheet_left_out") or {}
                stale = bool(trimmed) and trimmed["epoch"] != packets.briefed(record)["epoch"]
                if inp.rerender or stale or len(images) < len(packet.get("images") or []):
                    # Drawn again, it is sent whole to a new reader: whatever
                    # was sent before may be gone (`packets.as_in`).
                    packets.new_reader(record)
                    packet, images = engine.rerender(outstanding)
                    fresh = packet is not None
                else:
                    fresh = False
                if packet is not None:
                    status = "again"
            if not outstanding or packet is None:
                packet, images, status = engine.issue(reader=reader, parallel=inp.parallel,
                                                      tasks=inp.tasks)
                fresh = status == "packet"
                needs = engine.needs
                if fresh:
                    from plexora.plugins.gating.server.autogate import cellcalls

                    # The classifier's features are read while the look is
                    # answered, so its layer is ready with the gate.
                    cellcalls.after_issue(engine, packet)
            asking = []
            if status == "wait_user":
                # `images` holds the waiting units here (`Engine.issue`).
                asking = [dict(u["limit_request"]) for u in images]
                for unit in images:
                    unit["limit_request"]["announced"] = True
            progress = engine.progress()
            state = record["state"]
            n_out = len(record["outstanding"])
            # Briefings (`packets.briefed`) are kept for one reader, so a lane
            # of a parallel session is sent its packets whole.
            several = reader != DEFAULT_READER or inp.parallel > 1
            sent = (packets.as_sent(packet, record, options, full=not fresh or several)
                    if status in ("packet", "again") else None)
            snapshot = {"images": record["images"], "state": state,
                        "outstanding_kind": record.get("outstanding_kind"),
                        "panel_pending": record.get("panel_pending"),
                        "units": record["units"]}
        if status in ("packet", "again"):
            kind = packet.get("kind")
            mirrors = _mirroring(mirror) and not detached
            if fresh:
                refs = packet.get("units") or []
                phase = engines.phase_for(snapshot, mirroring=mirrors)
                _LAST_PHASE[inp.session_id] = phase
                _announce(call, snapshot, inp.session_id, "issued",
                          packet_id=packet.get("packet_id"), kind=kind,
                          marker=refs[0]["marker"] if len(refs) == 1 else None,
                          markers=[r["marker"] for r in refs],
                          project=refs[0]["project"] if refs else None,
                          subject=_subject(packet), phase=phase, progress=progress,
                          narration=packet.get("narration"))
                _dataset_progress(call, snapshot, inp.session_id, progress)
            if mirrors and (fresh or mirror.get("status") in ("pending", "degraded")):
                sent["mirror"] = _mirror(call, inp.session_id, packet)
            elif mirror.get("enabled"):
                sent["mirror"] = {**_mirror_brief(mirror),
                                  **({"detached": True} if detached else {}),
                                  **({"resent": False} if status == "again" else {})}
            if "mirror" in sent and packets.sends_delta(options):
                sent["mirror"] = _slim_mirror(sent["mirror"])
            if fresh and mirrors and kind in schemas.LOOK_KINDS:
                _phase(call, snapshot, inp.session_id, "thinking")
            return _packet_result(sent, images)
        if status == "wait_user":
            for request in asking:
                if not request.pop("announced", False):
                    _announce(call, snapshot, inp.session_id, "limit_reached",
                              **_limit_brief(request), phase="waiting")
            return _waiting_for_user(asking, progress)
        if status == "other_tasks":
            from plexora.ai import delegation

            return delegation.other_tasks(needs, progress)
        if status == "wait":
            _phase(call, snapshot, inp.session_id, "analyzing")
        if status == "busy" and time.monotonic() >= deadline:
            return _busy(progress, n_out)
        if status == "done":
            if state != "bulk_running":
                _phase(call, snapshot, inp.session_id, "summarizing")
                if options["purpose"] == "contrast":
                    # An auto-contrast run decides no gate: nothing to report.
                    return {"state": "decided", "progress": progress,
                            "next": f"{tool_name_of('gating.session_finish')}(session_id, "
                                    "action='close')"}
                return {"state": "decided", "progress": progress,
                        "next": f"{tool_name_of('gating.session_finish')}(session_id, "
                                f"action='close'), then {tool_name_of('gating.report')}"}
        if time.monotonic() >= deadline:
            return {"state": "bulk_running", "progress": progress,
                    "job_id": engine.record.get("bulk_job_id"),
                    "next": f"call {tool_name_of('gating.next')} again; the deterministic "
                            "pass is still profiling the next marker"}
        # A reader waiting on another's answer looks again soon: answers come
        # in seconds, the bulk pass in tens of them.
        _work.wait(0.1 if status == "busy" else 0.5)


#: How soon a driver asks again while the user fills in the set-up form.
SETUP_RETRY_S = 3


def _waiting_on_setup(st, session_id):
    """What `gating_next` answers while a session waits on the viewer's
    set-up form (`setup: wait`): no packet -- the form is the user's to
    answer, never a model's -- and what it waits on. None otherwise."""
    try:
        record = st.load(session_id)
    except Exception:              # noqa: BLE001
        return None
    needs = record.get("setup_wait")
    if record.get("state") != "needs_setup" or not needs:
        return None
    return {"state": "needs_setup", "setup": needs, "retry_after_s": SETUP_RETRY_S,
            "note": _setup_message(needs),
            "next": f"call {tool_name_of('gating.next')} again after retry_after_s; the "
                    "session is planned and goes on once the user has finished the form"}


def _limit_brief(request):
    return {k: request.get(k) for k in ("project", "marker", "why", "words", "looks",
                                        "rounds", "extension", "max_extensions", "proposed")}


def _waiting_for_user(requests, progress):
    """What `gating_next` says while every open marker waits on a limit
    question. The viewer shows the user a Continue / Stop dialog; an agent
    driving without a viewer asks the user itself and passes the answer on."""
    status_tool = tool_name_of("gating.session_status")
    return {"state": "waiting_for_user", "progress": progress, "retry_after_s": 10,
            "requests": [_limit_brief(r) for r in requests],
            "note": "these markers reached their allowance while the evidence still says to "
                    "keep going; the user is asked in the viewer whether to continue",
            "next": f"ask the user if no viewer is open, then {status_tool}(session_id, "
                    "limits={marker: 'continue' | 'stop'}); otherwise call "
                    f"{tool_name_of('gating.next')} again after retry_after_s"}


def _subject(packet):
    """What a packet is about, in a few words (the panel's phase line)."""
    refs = packet.get("units") or []
    kind = packet.get("kind")
    if kind == "expression_setup":
        return "expression source"
    if kind == "panel_context":
        return "panel context"
    if kind == "partner_review":
        return "partner review"
    if kind == "replan":
        return "re-planning"
    if kind == "pixel_setup":
        return "pixel size"
    if kind == "display_setup":
        return "contrast"
    if len(refs) == 1:
        return refs[0]["marker"]
    return f"{len(refs)} markers" if refs else None


def _resume_bulk(call, session_id, st):
    """A session whose bulk pass is no longer running anywhere -- the MCP
    server that ran it was restarted -- gets it again. The pass skips every
    unit already profiled, so only the rest is redone."""
    from plexora.agent import jobs
    from plexora.plugins.gating.server.autogate import engine as engines

    with engines.engine_for(call, session_id, st=st) as engine:
        record = engine.record
        if st.control(session_id).get("stopped"):
            return None       # a stopped pass is not resubmitted
        if record["state"] != "bulk_running" and not (
                record["state"] == "created" and record.get("bulk_job_id") is None):
            return None
        job = jobs.store().get(record.get("bulk_job_id") or "") if record.get(
            "bulk_job_id") else None
        if job is not None and job.get("status") in ("queued", "running", "done"):
            return None
        job = _submit_bulk(call, session_id)
        record["bulk_job_id"] = job["job_id"]
        record.setdefault("bulk_resumed", []).append(job["job_id"])
        return job["job_id"]


def _mirror_brief(mirror):
    return {k: mirror.get(k) for k in ("status", "view_id", "last_error", "sent", "follow")}


def _tab_of(call, view_id):
    """The presenter's `where` for a session that follows the tab: what the
    tab mirrored into shows ({project, status, ...},
    `viewer_sessions.describe`), or None once it is gone."""
    def where():
        from plexora.agent import viewer

        try:
            return viewer.resolve_view(viewer.require(call.link), view_id)
        except AgentError:
            return None

    return where


def _mirror(call, session_id, packet, *, replay=False):
    """Show the packet in the session's viewer, off the driver's path: it is
    offered to the session's presenter (`session_mirror.Presenter`), which
    sends it when the tab is free -- the newest look first -- and records how
    it went (`_mirrored`). Returns what the agent is told now: the mirror
    `queued` (or, for a replay that is not needed, what is on record).

    `replay` is the tab asking to watch again (`attach_viewer`): it skips a
    packet the harness has just shown, and a viewer detached meanwhile.

    A session that follows the tab (`mirror.follow`) offers the look under
    its image, with the tab's `where`: the presenter sends it only while the
    tab shows that image. A look the tab was not on (`skipped`) is not
    recorded as shown, so the tab's attach after a switch replays it."""
    from plexora.agent.sessions import mirror as session_mirror
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import mirror_script

    packet_id = packet.get("packet_id")
    st = engines.store()
    current = st.load(session_id).get("mirror") or {}
    if replay:
        if st.control(session_id).get("viewer_detached") or (
                current.get("status") == "ok" and current.get("packet_id") == packet_id
                and time.time() - float(current.get("sent_at") or 0) < 30):
            return _mirror_brief(current)
    refs = packet.get("units") or []

    def show():
        started = time.perf_counter()
        try:
            result = mirror_script.run(call, session_id, packet)
        except Exception as exc:  # mirroring never breaks a session
            result = {"status": "degraded", "sent": 0, "errors": [{"message": str(exc)}]}
        if result.get("status") == "skipped":
            return session_mirror.SKIPPED
        return _mirrored(call, session_id, result, started, packet_id=packet_id,
                         show=bool(refs))

    follow = {}
    if current.get("follow") == "tab":
        images = mirror_script.packet_images(packet)
        # A set-up question waiting on several images is shown on any of them.
        follow = {"project": images[0] if len(images) == 1 else None,
                  "where": _tab_of(call, current.get("view_id"))}
    queued = session_mirror.presenter(session_id).offer(
        show, subject=engines.unit_key(refs[0]["project"], refs[0]["marker"])
        if len(refs) == 1 else None,
        # A set-up packet is a question the tab puts to the user too, and a
        # replay was asked for: neither waits out the look being played.
        wait=bool(refs) and not replay, key=None if replay else packet_id, **follow)
    return {"status": queued, "packet_id": packet_id}


def _mirrored(call, session_id, result, started, *, packet_id=None, show=False):
    """Record what the presenter sent on the session (`mirror`) and in its log
    (`mirrored`); returns how long the tab should keep the show before a newer
    look replaces it (`MIN_SHOW_S` for a look of a paced session)."""
    from plexora.agent.sessions import mirror as session_mirror
    from plexora.plugins.gating.server.autogate import engine as engines

    spent = round((time.perf_counter() - started) * 1e3, 1)
    with engines.engine_for(call, session_id) as engine:
        mirror = engine.record.setdefault("mirror", {})
        if packet_id is not None or result.get("sent") or result.get("status") != "ok":
            mirror["status"] = result.get("status")
            mirror["last_error"] = (result.get("errors") or [None])[0]
            mirror["view_id"] = result.get("view_id") or mirror.get("view_id")
        if packet_id is not None:
            mirror["sent"] = int(result.get("sent") or 0)
            mirror["packet_id"] = packet_id
            mirror["sent_at"] = time.time()
        engine.log(event="mirrored", packet_id=packet_id, status=result.get("status"),
                   sent=int(result.get("sent") or 0), ms={"mirror": spent},
                   dropped=session_mirror.presenter(session_id).take_dropped(),
                   **({"aborted": True} if result.get("aborted") else {}))
        paced = bool(engine.options["mirror_delay_ms"])
    if not (show and paced):
        return 0.0
    # As long as the tab says the look's moves take (`show_s`), so the next
    # look does not cut in half way through them.
    return min(session_mirror.MAX_SHOW_S,
               max(session_mirror.MIN_SHOW_S, float(result.get("show_s") or 0.0)))


def _mirror_display(call, session_id, project):
    """Show the mirrored tab the brightness a contrast check just settled,
    right after the answer (`mirror_script.run_display`): the tab plays it
    while the session moves on -- and a contrast run, which has no next
    packet to carry it, still plays it. Best effort, like every mirror; kept
    in order by the presenter, never replaced by a look."""
    from plexora.agent.sessions import mirror as session_mirror
    from plexora.plugins.gating.server.autogate import mirror_script

    def show():
        started = time.perf_counter()
        try:
            result = mirror_script.run_display(call, session_id, project)
        except Exception as exc:  # mirroring never breaks a session
            result = {"status": "degraded", "sent": 0, "errors": [{"message": str(exc)}]}
        # Skipped (the tab follows another image): what was settled is eased
        # in with this image's next look instead.
        if result.get("status") != "skipped" and (result.get("sent")
                                                  or result.get("status") != "ok"):
            _mirrored(call, session_id, result, started)
        return 0.0

    session_mirror.presenter(session_id).offer(show, keep=True)


class AnswerInput(AgentModel):
    session_id: str
    packet_id: str = Field(description="The packet being answered (pk_nnnn).")
    answer: dict[str, Any] = Field(description="The typed answer: `kind` plus the fields the "
                                   "packet's answer_schema lists.")
    include_next: bool = Field(True, description="Return the next packet with the outcome, "
                                                 "saving a gating_next call.")
    reader: str | None = _READER
    parallel: int = _PARALLEL
    tasks: list[str] | None = _TASKS
    model: str | None = session_tools.MODEL


def answer(call, inp):
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import packets

    st = engines.store()
    halted = _halted(call, st, inp.session_id)
    if halted:
        return halted
    st.claim(inp.session_id)
    with engines.engine_for(call, inp.session_id, st=st) as engine:
        receipts_before = list(engine.record.get("receipts") or [])
        states_before = {k: u["state"] for k, u in engine.record["units"].items()}
        kind = (engine.record["outstanding"].get(inp.packet_id) or {}).get("kind")
        reader, tasks = engine.scope_of(inp.packet_id, inp.reader, inp.tasks)
        try:
            outcome = engine.apply(inp.packet_id, inp.answer)
        except AgentError as exc:
            if exc.code == "invalid_input":
                exc.save = True
            raise
        if not outcome.get("already_applied"):
            engine.answered_by(inp.packet_id, inp.model)
        receipts = [r for r in engine.record.get("receipts") or []
                    if r not in set(receipts_before)]
        progress = engine.progress()
        record = engine.record
        delta = packets.sends_delta(engine.options)
        closed = [u for k, u in record["units"].items()
                  if u["state"] in schemas.TERMINAL_STATES
                  and states_before.get(k) not in schemas.TERMINAL_STATES]
        snapshot = {"images": record["images"], "state": record["state"],
                    "outstanding_kind": None, "units": record["units"]}
    if not outcome.get("already_applied"):
        refs = outcome.get("unit") or ""
        # One event per answer: the markers it closed ride on `answered`
        # (`closed`, each what a `unit_closed` event carried), not one event
        # -- one round trip to the viewer -- per marker.
        _announce(call, snapshot, inp.session_id, "answered", packet_id=inp.packet_id,
                  kind=kind, marker=refs.split("::", 1)[-1] if refs else None,
                  outcome_state=outcome.get("state"),
                  phase=engines.phase_for(snapshot), progress=progress,
                  closed=[{"marker": unit["marker"], "project": unit["project"],
                           "state": unit["state"], "confidence": unit.get("confidence"),
                           "low": unit.get("final") if unit.get("final") is not None
                           else unit.get("proposed"), "reason": unit.get("reason")}
                          for unit in closed])
        _dataset_progress(call, snapshot, inp.session_id, progress)
        if kind == "display_setup" and outcome.get("project"):
            # The viewer's channels nobody set take the chosen brightness
            # (viewerSidebar.syncGatingContrast); a mirrored tab is shown it
            # being set, now (`_mirror_display`).
            _announce(call, snapshot, inp.session_id, "display", project=outcome["project"],
                      revision=outcome.get("revision"),
                      chosen={m: c["chosen_by"] for m, c in
                              (outcome.get("chosen") or {}).items()})
            if _mirroring(record.get("mirror") or {}):
                _mirror_display(call, inp.session_id, outcome["project"])
    if delta:
        progress = _slim_progress(progress)
    # A refused answer (`reissue`: a partner gate changed while the packet was
    # out) applied nothing; the next packet is the same decision, drawn again.
    result = {"applied": not outcome.get("already_applied")
              and outcome.get("state") != "reissue",
              "outcome": outcome, "receipts": session_tools.receipts_summary(receipts),
              "progress": progress}
    if inp.include_next and not outcome.get("already_applied"):
        following = next_packet(call, NextInput(session_id=inp.session_id, wait_s=5.0,
                                                reader=reader, parallel=inp.parallel,
                                                tasks=tasks))
        images = following.pop("_images", None)
        result["next"] = following
        if images:
            result["_images"] = images
    return result


# -- status / finish ----------------------------------------------------------


class StatusInput(AgentModel):
    session_id: str | None = Field(None, description="Omit to list recent sessions.")
    reattach_viewer: bool = False
    replay_mirror: bool = Field(False, description="With reattach_viewer: show the packet "
                                "out in the viewer now, not at the next packet.")
    view_id: str | None = Field(None, description="With reattach_viewer: the tab to mirror "
                                "into (defaults to the one mirrored before).")
    project: str | None = Field(None, description="With reattach_viewer, in a dataset's "
                                "session: the image the tab shows (default: what it reports); "
                                "the packet replayed is that image's.")
    pause: bool | None = Field(None, description="Pause (true) or resume (false) the "
                                                 "session.")
    detail: Literal["brief", "full"] = Field(
        "brief", description="brief: state, progress, counts, and each marker's gate, state, "
                             "confidence and why it needs review; full: every unit's fields "
                             "(the candidate gates the reads leave, the classifier layer), "
                             "the budget spent, QC, biology, mirroring, the delegate block, "
                             "the vocabulary and the reading guide.")
    guide: bool = Field(False, description="Send the reading guide packets point into "
                        "(`evidence.guide`, `answer_schema.see`): once, before you answer "
                        "packets yourself.")
    known_guide: str | None = Field(None, description="The `guide_version` you hold: the "
                                    "reading guide is then not sent again.")
    limits: dict[str, Literal[schemas.LIMIT_DECISIONS]] | None = Field(
        None, description="Answers to the session's limit questions (`requests` of a "
                          "`waiting_for_user` result), by marker: `continue` grants another "
                          "allowance, `stop` flags the marker for manual review. Pass the "
                          "user's answer, not your own guess.")


def record_limit_answers(st, session_id, record, answers) -> dict:
    """Merge `{marker or project::marker: continue | stop}` into the
    session's control file (the engine reads them on its next call); returns
    the answers recorded, by unit key. Unknown markers are refused."""
    from plexora.plugins.gating.server.autogate.engine import unit_key

    keys = {}
    for name, decision in (answers or {}).items():
        if name in record["units"]:
            keys[name] = decision
            continue
        found = [unit_key(p, name) for p in record["images"]
                 if unit_key(p, name) in record["units"]]
        if not found:
            raise AgentError("invalid_input", f"{name!r} is not a marker of this session",
                             detail={"markers": sorted({u["marker"] for u in
                                                        record["units"].values()})})
        for key in found:
            keys[key] = decision
    control = st.control(session_id)
    st.set_control(session_id, limit_answers={**(control.get("limit_answers") or {}), **keys})
    return keys


def _unit_row(unit):
    row = {k: unit.get(k) for k in ("project", "marker", "state", "confidence", "final",
                                    "proposed", "gmm", "tier", "class", "reason")
           if unit.get(k) is not None}
    layer = unit.get("classifier")
    if layer:
        compare = layer.get("compare") or {}
        row["classifier"] = {k: v for k, v in (
            ("status", layer.get("status")), ("why", layer.get("why")),
            ("removed", compare.get("n_removed")), ("added", compare.get("n_added")),
            ("grade", (layer.get("snr") or {}).get("grade"))) if v is not None}
    if unit.get("classifier_check"):
        row["classifier_check"] = unit["classifier_check"]["direction"]
    return row


def _classifier_brief(record):
    """What the session's classifier did, for status: the mode, counts, and
    how to look at a marker's layer."""
    mode = (record.get("options") or {}).get("classifier", "off")
    units = (record.get("units") or {}).values()
    if mode == "off":
        return {"mode": "off"}
    count = {}
    for unit in units:
        status = (unit.get("classifier") or {}).get("status")
        if status:
            count[status] = count.get(status, 0) + 1
    state = record.get("classifier") or {}
    return {"mode": mode, "layers": count,
            "checked": sum(1 for u in units if u.get("classifier_check")),
            "reading_ahead": len(state.get("queue") or []),
            "next": (f"{tool_name_of('gating.classifier_state')}(project, marker) for a "
                     "marker's layer; the threshold decides until the user adopts it")}


def _bulk_status(record):
    """The bulk job's own status beside the session's (read-only): a session
    left `bulk_running` by a restarted server says so rather than looking busy."""
    from plexora.agent import jobs

    job_id = record.get("bulk_job_id")
    job = jobs.store().get(job_id) if job_id else None
    out = {"job_id": job_id, "status": (job or {}).get("status"),
           "resumed": list(record.get("bulk_resumed") or [])}
    if record["state"] == "bulk_running" and out["status"] not in ("queued", "running"):
        out["note"] = ("the bulk pass is not running anywhere (the server that ran it "
                       f"restarted); the next {tool_name_of('gating.next')} resumes it")
    return out


def _why(unit):
    """Why a marker closed where it did, for a brief status: its note up to
    the candidates the reads leave (`hypotheses.PLATEAU_LEAD`, which the full
    status keeps). None for an accepted marker or one still open."""
    from plexora.plugins.gating.server.autogate.hypotheses import PLATEAU_LEAD

    reason = unit.get("reason")
    if not reason or unit.get("state") == "accepted" \
            or unit.get("state") not in schemas.TERMINAL_STATES:
        return None
    return str(reason).split(PLATEAU_LEAD, 1)[0]


#: The counts a brief status always gives; the rest only when not zero.
BRIEF_COUNTS = ("accepted", "review", "written")


def _gate_groups(record):
    """A brief status's markers, grouped by state and confidence, each group
    `{marker: gate}` -- under `why` by the reason it closed with, when it has
    one: the gate written or proposed, else the best one the evidence reached
    (`proposed`), None for none; a dataset's markers as `image::marker`.
    Accepted markers first, then those left for review, the commonest reason
    first. At most MAX_LIST; returns (groups, how many were left out)."""
    from plexora.plugins.gating.server.autogate.engine import unit_key

    many = len(record.get("images") or []) > 1
    states = dict.fromkeys(schemas.ACCEPTED_STATES + schemas.REVIEW_STATES
                           + schemas.TERMINAL_STATES)
    rank = {state: i for i, state in enumerate(states)}
    sure = {level: i for i, level in enumerate(schemas.CONFIDENCE)}
    units = list((record.get("units") or {}).values())
    groups: dict = {}
    for unit in units[:MAX_LIST]:
        name = unit_key(unit["project"], unit["marker"]) if many else unit["marker"]
        gate = unit.get("final") if unit.get("final") is not None else unit.get("proposed")
        key = (unit.get("state"), unit.get("confidence"))
        groups.setdefault(key, {}).setdefault(_why(unit), {})[name] = gate
    out = []
    for (state, confidence), by_why in sorted(
            groups.items(), key=lambda item: (rank.get(item[0][0], len(rank)),
                                              sure.get(item[0][1], len(sure)))):
        group = {"state": state, **({"confidence": confidence} if confidence else {})}
        plain = by_why.pop(None, None)
        if plain:
            group["markers"] = plain
        if by_why:
            group["why"] = dict(sorted(by_why.items(), key=lambda item: (-len(item[1]), item[0])))
        out.append(group)
    return out, max(0, len(units) - MAX_LIST)


def _brief(engine, st, session_id, inp):
    """`gating_session_status` by default: what a person asks of a session --
    its state, progress and counts, and each marker's gate, state, confidence
    and why it needs review -- in a few thousand characters for a whole
    panel. Whatever waits on someone (the set-up form, a limit question, an
    open question, a pause, a commit) is added when there is one; `detail:
    full` has the rest."""
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import transfer

    record = engine.record
    progress = engine.progress()
    summary = engines.summary_of(record)
    groups, omitted = _gate_groups(record)
    out = {"session_id": session_id, "state": record["state"],
           "mode": record["options"]["mode"], "scope": record.get("scope"),
           "images": record["images"],
           "progress": {"units_done": progress["units_done"],
                        "units_total": progress["units_total"],
                        **{k: progress[k] for k in ("not_profiled", "waiting_for_user")
                           if progress.get(k)}},
           "counts": {k: v for k, v in summary.items()
                      if k in BRIEF_COUNTS or (v and k not in ("by_state", "units_done",
                                                               "units_total"))},
           "gates": groups, **({"omitted": omitted} if omitted else {})}
    if out["mode"] == "propose" and not summary.get("written") \
            and record["state"] != "rolled_back":
        out["commit"] = (f"nothing is written until {tool_name_of('gating.session_finish')}"
                         "(session_id, action='commit')")
    if record.get("setup_wait"):
        out["setup"] = record["setup_wait"]
    bulk = _bulk_status(record)
    if bulk.get("note"):
        out["bulk"] = bulk
    requests = [_limit_brief(u["limit_request"]) for u in engine.waiting_for_user()]
    if requests:
        out["limit_requests"] = requests
    if record.get("questions"):
        out["questions"] = record["questions"]
    control = st.control(session_id)
    held = {k: control.get(k) for k in ("paused", "paused_by", "stopped", "stopped_by")
            if control.get(k)}
    if held:
        out["control"] = held
    if record.get("outstanding_packet"):
        out["outstanding_packet"] = record["outstanding_packet"]
    if len(record.get("outstanding") or {}) > 1:
        out["outstanding_packets"] = list(record["outstanding"])
    if record.get("scope") == "dataset":
        from plexora.plugins.gating.server.autogate import groups as group_mod

        dataset = transfer.dataset_summary(engine)
        out["reference_image"] = dataset["reference_image"]
        out["strategy"] = {marker: row.get("strategy")
                           for marker, row in dataset["markers"].items()}
        if record.get("groups"):
            out["groups"] = group_mod.brief(engine)
        out["dataset_progress"] = progress["dataset_progress"]["counts"]
    if inp.reattach_viewer:
        out["mirror"] = record.get("mirror")
    if record["state"] not in schemas.FINISHED_STATES \
            and record["options"].get("reading") == "once":
        out["guide"] = "not sent: guide=true sends the reading guide packets point into"
    out["more"] = ("detail='full' adds every unit's fields (the candidate gates the reads "
                   "leave), QC, biology, the classifier, the budget spent and mirroring")
    return out


def status(call, inp):
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import packets
    from plexora.plugins.gating.server.autogate import transfer

    st = engines.store()
    if inp.session_id is None:
        return {"sessions": [{k: r.get(k) for k in ("session_id", "created_at", "state",
                                                     "scope", "images", "dataset")}
                             for r in st.list()]}
    if inp.pause is not None:
        st.set_control(inp.session_id, paused=bool(inp.pause),
                       paused_by="agent" if inp.pause else None)
        _announce(call, st.load(inp.session_id), inp.session_id, "control",
                  paused=bool(inp.pause), paused_by="agent" if inp.pause else None)
    if inp.limits:
        answered = record_limit_answers(st, inp.session_id, st.load(inp.session_id), inp.limits)
        _announce(call, st.load(inp.session_id), inp.session_id, "limit_answered",
                  answers={k.split("::", 1)[-1]: v for k, v in answered.items()},
                  by="agent")
    # The reading guide (~20k characters) goes to a caller that asks for it --
    # one about to answer packets (`guide`), or the whole status -- never to
    # one asking how the session stands.
    send_guide = inp.guide or inp.detail == "full"
    with engines.engine_for(call, inp.session_id, st=st) as engine:
        record = engine.record
        reader = None
        if send_guide and engine.options["reading"] == "once" \
                and inp.known_guide != packets.guide_version(engine.options["engine"]):
            # A caller sent the guide is a new conversation: nothing sent
            # before may be pointed at (`packets.as_in`).
            reader = packets.new_reader(record)
        follow, view_id = False, None
        if inp.reattach_viewer:
            mirror_ = record.setdefault("mirror", {})
            mirror_.update(status="pending", enabled=True,
                           view_id=inp.view_id or mirror_.get("view_id"))
            # A dataset's session recorded before the mirror followed the tab
            # follows it from now on.
            mirror_.setdefault("follow", "tab" if record.get("scope") == "dataset" else None)
            follow, view_id = mirror_["follow"] == "tab", mirror_["view_id"]
        replay = None
        if inp.reattach_viewer and inp.replay_mirror:
            # Following the tab: the packets out, newest first, for the one of
            # the image it shows (`_replay_for`).
            replay = engine.outstanding()[::-1] if follow else record.get("outstanding_packet")
        guide = _guide(record["options"]["reading"], inp.known_guide,
                       engine.options["engine"]) if send_guide else {}
        if inp.detail == "brief":
            out = _brief(engine, st, inp.session_id, inp)
            if guide:
                out.pop("guide", None)
                out.update(guide)
            if reader is not None:
                out["reader"] = reader
        else:
            out = _full_status(engine, st, inp, guide, reader)
            if record.get("scope") == "dataset":
                out["dataset"] = transfer.dataset_summary(engine)
    if replay and not st.control(inp.session_id).get("viewer_detached"):
        if follow:
            shows = inp.project or (_tab_of(call, view_id)() or {}).get("project")
            packet = _replay_for(st, inp.session_id, replay, shows)
        else:
            try:
                packet, _ = st.read_packet(inp.session_id, replay)
            except AgentError:
                packet = None     # answered meanwhile: the next packet is mirrored
        if packet is not None:
            out["mirror"] = _mirror(call, inp.session_id, packet, replay=True)
    if inp.reattach_viewer and follow:
        from plexora.agent.sessions import mirror as session_mirror

        # The tab may show another image than before: the looks waiting for
        # it are weighed again at once.
        session_mirror.presenter(inp.session_id).poke()
    return out


def _replay_for(st, session_id, packet_ids, project):
    """The packet of `packet_ids` (newest first) the tab, following it, is
    shown again on attaching: the newest of the image it shows (`project`) --
    a look's `units[0].project`, or an image a set-up question waits on --
    or None (none out for that image, or answered meanwhile)."""
    from plexora.plugins.gating.server.autogate import mirror_script

    if not project:
        return None
    for packet_id in packet_ids:
        try:
            packet, _ = st.read_packet(session_id, packet_id)
        except AgentError:
            continue
        if project in mirror_script.packet_images(packet):
            return packet
    return None


def _full_status(engine, st, inp, guide, reader):
    """`gating_session_status(detail: full)`: every unit's row and everything
    the session holds -- what the status was before it had a brief."""
    from plexora.plugins.gating.server.autogate import engine as engines

    record = engine.record
    units = [_unit_row(u) for u in record["units"].values()]
    out = {"session_id": inp.session_id, "state": record["state"],
           "scope": record.get("scope"), "mode": record["options"]["mode"],
           "images": record["images"], "reference_image": record.get("reference_image"),
           "progress": engine.progress(), "bulk": _bulk_status(record),
           "n_units": len(units), "channels": _channels_brief(record.get("channel_match")),
           "units": units[:MAX_LIST],
           "truncated": len(units) > MAX_LIST, "used": record.get("used"),
           "estimated_vision_tokens": budgets.vision_tokens(
               (record.get("used") or {}).get("pixels", 0)),
           "vocabulary": {"terminal_states": list(schemas.TERMINAL_STATES),
                          "accepted_states": list(schemas.ACCEPTED_STATES),
                          "written_states": list(schemas.WRITTEN_STATES),
                          "confidence": list(schemas.CONFIDENCE)},
           "expression": record.get("expression"),
           **({"setup": record["setup_wait"]} if record.get("setup_wait") else {}),
           "pixel": _pixel_brief(record.get("pixel")),
           "display": _display_brief(record.get("display")),
           **({"basic_qc": record["basic_qc"]} if record.get("basic_qc") else {}),
           "summary": engines.summary_of(record),
           **guide,
           "questions": record.get("questions") or [], "mirror": record.get("mirror"),
           "control": st.control(inp.session_id),
           "outstanding_packet": record.get("outstanding_packet"),
           "outstanding_packets": list(record.get("outstanding") or {}),
           "outstanding_peak": int(record.get("outstanding_peak") or 0),
           "reissued": int(record.get("reissued") or 0),
           "receipts": len(record.get("receipts") or []),
           "replayed": len(record.get("replayed") or []),
           "limit_requests": [_limit_brief(u["limit_request"])
                              for u in engine.waiting_for_user()],
           "biology": _biology_brief(record.get("biology")),
           "classifier": _classifier_brief(record),
           **({"reader": reader} if reader is not None else {}),
           **_delegate_block(record, inp.session_id, engine.options)}
    models = engine.models_used()
    if models:
        out["models"] = models
    if record.get("unconfirmed_context"):
        # What a reader answered about the panel: used by this session,
        # learned for later panels only once a person confirms it
        # (`set_panel_context` with source user).
        out["unconfirmed_context"] = list(record["unconfirmed_context"])
    if record.get("partner_review"):
        # The planner's review of the panel's partners: only the changes both
        # draws were sure of were made; `tiles_only` drew empty slots alone.
        out["partner_review"] = record["partner_review"]
    return out


class FinishInput(AgentModel):
    session_id: str
    action: Literal["close", "commit", "cancel", "rollback"] = Field(
        "close", description="close: end the session (writes stay; a marker not yet "
                             "settled is written at the best gate it reached, for review; "
                             "a session the user stopped in the viewer is cancelled); "
                             "commit: write a propose-mode session's accepted gates and "
                             "empty gates; "
                             "cancel: stop the bulk pass (writes stay); rollback: undo every "
                             "write the session made, newest first.")


def _save_versions(call, images, *, session_id=None, by=None):
    """`model.save_version` for each image, best effort: keeping a version
    never stops a session."""
    from plexora.plugins.gating.server import model as gating_model

    for name in images:
        try:
            gating_model.save_version(call.session.data(name), by, session_id=session_id)
        except Exception:
            continue


def _close_unsettled(engine):
    """No marker is left ungated by a close or a commit: one still waiting on
    a look, a check or the user is written (or, in propose mode, proposed) at
    the best gate it reached, for review (`Engine.close`). A Stop is a cancel
    and writes nothing: it may be for values that moved (`_matrix_moved`).
    An auto-contrast session never gates, so it is left as it is."""
    if engine.options["purpose"] == "contrast":
        return
    for unit in list(engine.record["units"].values()):
        if unit["state"] not in schemas.TERMINAL_STATES:
            engine.close(unit, "manual_review_recommended",
                         "the session was closed before this marker was settled; written "
                         "at the best gate it reached, for manual review",
                         unlooked="session_closed")


def finish(call, inp):
    from plexora.agent import jobs, registry
    from plexora.plugins.gating.server.autogate import engine as engines

    st = engines.store()
    control_ = st.control(inp.session_id)
    stopped = bool(control_.get("stopped"))
    detached = bool(control_.get("viewer_detached"))
    action = "cancel" if stopped and inp.action == "close" else inp.action
    with engines.engine_for(call, inp.session_id, st=st) as engine:
        record = engine.record
        out = {"session_id": inp.session_id, "action": action}
        from plexora.plugins.gating.server.autogate import cellcalls

        if action == "cancel":
            job_id = record.get("bulk_job_id")
            if job_id:
                out["job"] = jobs.store().cancel(job_id)
            cellcalls.on_cancel(engine)
            record["state"] = "cancelled"
        elif action == "commit":
            if record["options"]["mode"] != "propose":
                raise AgentError("invalid_input", "commit is for a propose-mode session")
            _close_unsettled(engine)              # proposed, then written below
            record["options"]["mode"] = "apply"
            written = []
            for unit in record["units"].values():
                if unit.get("proposed") is not None and unit["state"] in \
                        schemas.WRITTEN_STATES:
                    empty = unit["state"] in schemas.EMPTY_GATE_STATES
                    op = engine.write(unit, unit["proposed"],
                                      method=unit.get("method") or "ai_accepted",
                                      confidence=unit.get("confidence") or "low",
                                      state=unit["state"], tier=unit.get("tier") or "T2",
                                      empty=empty)
                    if op:
                        written.append(op)
            out["written"] = written
            # Each written gate's classifier layer, stored now (the session's
            # job: never a wait here).
            cellcalls.on_commit(engine)
            record["options"]["mode"] = "propose"
            record["state"] = "done"
        elif action == "rollback":
            undone, refused = [], []
            # Newest first; each undo moves its store's revision, which the
            # next-older receipt was not made against -- so the revision this
            # rollback's own previous undo left is vouched for (and nothing
            # anyone else changed in between).
            left = {}
            for operation_id in reversed(record.get("receipts") or []):
                line = call.audit.find(operation_id) or {}
                state = (line.get("receipt") or {}).get("persistent_state")
                arguments = {"operation_id": operation_id}
                if left.get(state):
                    arguments["expected_current_revision"] = left[state]
                answer_ = registry.invoke(call.session, "undo_operation", arguments,
                                          policy=call.policy, audit=call.audit, link=call.link,
                                          notify=call.notify,
                                          operation_id=f"{call.operation_id}.u{len(undone):03d}")
                if answer_["ok"]:
                    undone.append(operation_id)
                    receipt_ = (answer_["result"] or {}).get("receipt") or {}
                    if receipt_.get("revision_after") is not None:
                        left[state] = receipt_["revision_after"]
                else:
                    refused.append({"operation_id": operation_id,
                                    "reason": answer_["error"]["message"]})
            out.update(undone=undone, refused=refused)
            out["classifier_discarded"] = cellcalls.on_rollback(engine)
            record["state"] = "rolled_back"
        else:
            if record["state"] == "bulk_running":
                raise AgentError("conflict", "the bulk pass is still running; cancel it or "
                                 "wait for it", retryable=True)
            _close_unsettled(engine)
            record["state"] = "done"
        record["finished_at"] = _now()
        if action != "rollback" and any(u.get("receipts") for u in record["units"].values()):
            # What the run wrote, kept as a version: the next run or a slider
            # writes over the list, never over this.
            from plexora.plugins.gating.server import model as gating_model

            _save_versions(call, record["images"], session_id=inp.session_id,
                           by=gating_model.GATED_BY_AI)
        if action != "cancel":
            from plexora.plugins.gating.server import experience
            from plexora.plugins.gating.server.autogate import memo

            # What this session decided, for a person to learn from across
            # runs (`server/experience.py`); nothing reads it into a gate.
            experience.after_finish(record, action, memo.versions())
        engine.release_all()
        out["progress"] = engine.progress()
        out["units"] = [_unit_row(u) for u in record["units"].values()][:MAX_LIST]
        out["questions"] = record.get("questions") or []
        summary = engines.summary_of(record)
        out["summary"] = summary
        mirror = dict(record.get("mirror") or {})
        snapshot = {"images": record["images"]}
        state = record["state"]
    st.release(inp.session_id)
    reason = "stopped" if stopped else {"close": "closed", "commit": "committed",
                                        "cancel": "cancelled",
                                        "rollback": "rolled_back"}[action]
    _LAST_PHASE.pop(inp.session_id, None)
    _dataset_progress(call, snapshot, inp.session_id, out["progress"], last=True)
    _LAST_DATASET_PROGRESS.pop(inp.session_id, None)
    _announce(call, snapshot, inp.session_id, "finished", reason=reason, state=state,
              summary=summary, phase="summarizing")
    from plexora.agent.sessions import mirror as session_mirror

    if _mirroring(mirror) and not detached:
        # A detached viewer is the user's own: nothing is put back in it. Sent
        # by the presenter, after the command under way and nothing else.
        from plexora.plugins.gating.server.autogate import mirror_script

        def teardown():
            try:
                mirror_script.run_teardown(call, inp.session_id, reason)
            except Exception:  # the view is restored best effort
                pass
            return 0.0

        session_mirror.presenter(inp.session_id).close(teardown)
        out["teardown"] = {"status": "queued"}
    else:
        session_mirror.presenter(inp.session_id).close()
    receipt = make_receipt(call, changed=inp.action in ("commit", "rollback"),
                           before=None, after={k: out.get(k) for k in ("action", "written",
                                                                          "undone")},
                           persistent_state=STATE, reversible=False,
                           extra={"gating_session": inp.session_id})
    out["receipt"] = receipt.model_dump(mode="json")
    out["next"] = (f"{tool_name_of('gating.report')}(session_id) for the review report; "
                   f"{tool_name_of('gating.export')} for CSV")
    return out


# -- across a dataset ---------------------------------------------------------


class CompareInput(AgentModel):
    dataset: str
    markers: list[str] | None = None
    reference_image: str | None = None


def compare(call, inp):
    """Per marker and image: aligned vs own gate, drift class, strategy."""
    from plexora import datasets
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import reference as refmod
    from plexora.plugins.gating.server.autogate import tableops

    try:
        cohort = datasets.dataset(inp.dataset)
    except KeyError as exc:
        raise AgentError("invalid_input", str(exc.args[0])) from None
    images = [p for p in cohort.projects
              if PLUGIN.requires.applies_to(call.session.project(p))
              and not PLUGIN.requires.missing_from(call.session.project(p))]
    if len(images) < 2:
        raise AgentError("precondition_missing", "comparing gates needs at least two images")
    summaries = {}
    total = len(images)
    for index, project in enumerate(images, start=1):
        call.check_cancelled()
        ds = call.session.data(project)
        markers = [m for m in (inp.markers or ds.table.markers) if m in ds.table.markers]
        per = {}
        for marker in markers:
            profile = tableops.local_or_node(ds, "gating.autogate.profile",
                                             {"marker": marker, "with_cell_qc": False})
            gate = model.get_gate(ds, marker)
            per[marker] = {"summary": engines.compact_profile(profile),
                           "metrics": engines._metrics(profile),
                           "class": profile.get("distribution_class"),
                           "fit_space": profile.get("fit_space"),
                           "gate": gate["low"] if gate["thresholded"] else None,
                           "gmm": (profile.get("fit") or {}).get("gate_raw"),
                           "n": ds.table.geometry().height}
        summaries[project] = per
        call.progress(done=index, total=total, message=f"profiled {project}")
    reference_image = inp.reference_image or max(
        images, key=lambda p: (max((v["n"] for v in summaries[p].values()), default=0),
                               -images.index(p)))
    out = {}
    for marker in sorted({m for per in summaries.values() for m in per}):
        ref = summaries[reference_image].get(marker)
        if ref is None:
            continue
        ref_gate = ref["gate"] if ref["gate"] is not None else ref["gmm"]
        rows = []
        for project in images:
            item = summaries[project].get(marker)
            if item is None:
                continue
            if project == reference_image:
                rows.append({"project": project, "class": "reference", "gate": ref_gate})
                continue
            alignment = refmod.align(item["summary"], ref["summary"], item["fit_space"])
            predicted = refmod.predict(alignment, ref_gate, item["fit_space"]) \
                if alignment and ref_gate is not None else None
            klass = refmod.classify(alignment, item, ref)
            sd = (ref["metrics"] or {}).get("sd_bg") or 1.0
            shift = ((alignment["a"] + (alignment["b"] - 1.0)
                      * ((ref["metrics"] or {}).get("mu_bg") or 0.0)) / sd
                     if alignment else None)
            rows.append({"project": project, "class": klass, "shift_bg_sd": shift,
                         "aligned_gate": predicted, "own_gate": item["gate"],
                         "own_gmm": item["gmm"], "alignment": alignment})
        refined = refmod.dataset_classes(
            [{**r, "shift": r.get("shift_bg_sd")} for r in rows if r["class"] != "reference"])
        out[marker] = {"reference_gate": ref_gate, "images": rows,
                       "classes": [r["class"] for r in refined],
                       "strategy": refmod.strategy([r["class"] for r in refined])}
    return {"dataset": cohort.name, "reference_image": reference_image, "markers": out,
            "experimental_unit": "image", "n_images": len(images),
            "note": "classes are relative to the reference image; gates in each table's "
                    "own units"}


# -- export -------------------------------------------------------------------


class ExportInput(AgentModel):
    project: str | None = None
    dataset: str | None = None
    include_provenance: bool = True


def export(call, inp):
    from plexora.plugins.gating.server.autogate import report

    if bool(inp.project) == bool(inp.dataset):
        raise AgentError("invalid_input", "give exactly one of project or dataset")
    return report.export_gates(call, project=inp.project, dataset=inp.dataset,
                               include_provenance=inp.include_provenance)


class ReportInput(AgentModel):
    session_id: str
    format: Literal["html", "pdf", "both"] = "both"


def gating_report(call, inp):
    from plexora.plugins.gating.server.autogate import report

    return report.write_report(call, inp.session_id, fmt=inp.format)


def capabilities():
    def cap(**kwargs):
        kwargs.setdefault("tags", TAGS)
        return Capability(owner=OWNER, version="1", **kwargs)

    return [
        cap(name="gating.session_start", entitlement="ai:gating:session", tool_name="gating_session_start",
            purpose="Gate a whole image or dataset automatically. Starts a gating session: "
                    "every marker is profiled and settled deterministically where the "
                    "numbers suffice (a job), and the rest become decision packets for you, "
                    "one at a time, through gating_next / gating_answer.",
            permission="reversible_write", input_model=SessionOptions, handler=start,
            writes=("gates",), persistent=True, egress="metadata",
            reads=("table", "gates", "image")),
        cap(name="gating.contrast_start", entitlement="ai:gating:session",
            tool_name="contrast_session_start",
            purpose="Auto contrast on its own: set how bright every marker of an image or "
                    "dataset is drawn, without gating. The calibration sets each marker's "
                    "window from its negative and bright cells -- with no cell table, cells "
                    "found in the image (its mask, else its nuclear stain's nuclei) -- then a "
                    "contrast grid per batch of markers goes to you (gating_next / "
                    "gating_answer) and code picks the column. A marker an earlier run set "
                    "is skipped unless "
                    "`redo`. The choice is what every later picture -- and the viewer, for "
                    "channels nobody set -- draws with; gating sessions then skip the step.",
            permission="reversible_write", input_model=ContrastOptions, handler=contrast_start,
            writes=("display",), persistent=True, egress="metadata",
            reads=("table", "image"),
            tags=tuple(TAGS) + ("contrast", "display", "brightness", "window", "auto contrast")),
        cap(name="gating.session_bulk", entitlement="ai:gating:session", tool_name="gating_session_bulk",
            purpose="The deterministic pass of a gating session (started by "
                    "gating_session_start; call it yourself only to resume one).",
            permission="reversible_write", input_model=BulkInput, handler=session_qc(bulk),
            writes=("gates",), persistent=True, execution="job", egress="aggregates",
            reads=("table", "gates", "image")),
        cap(name="gating.session_classifier", entitlement="ai:gating:session",
            tool_name="gating_session_classifier",
            purpose="A gating session's classifier work: the pixel features read ahead "
                    "and the per-cell layers waiting on them (started by the session; "
                    "call it yourself only to finish one a stopped server left pending).",
            permission="reversible_write", input_model=JobInput,
            handler=session_qc(session_classifier), writes=("gates",), persistent=True,
            execution="job", egress="aggregates", reads=("table", "gates", "image", "mask")),
        cap(name="gating.next", entitlement="ai:gating:session", tool_name="gating_next",
            purpose="The session's next decision packet: one question, compact numbers, at "
                    "most two small images, and the answer schema. The same packet again "
                    "if it is still unanswered.",
            permission="reversible_write", input_model=NextInput, handler=session_qc(next_packet),
            writes=("gates",), persistent=True, visual_output=True,
            egress="rendered_pixels", reads=("table", "gates", "image", "mask")),
        cap(name="gating.answer", entitlement="ai:gating:session", tool_name="gating_answer",
            purpose="Answer the outstanding packet with a typed judgement; the server moves "
                    "the marker on (and writes its gate when it is decided) and returns the "
                    "next packet.",
            permission="reversible_write", input_model=AnswerInput, handler=session_qc(answer),
            writes=("gates",), persistent=True, visual_output=True,
            egress="rendered_pixels", reads=("table", "gates", "image", "mask")),
        cap(name="gating.session_status", entitlement="ai:gating:session", tool_name="gating_session_status",
            purpose="A gating session: each marker's gate, state, confidence and why it "
                    "needs review, its progress and what waits on the user (detail=full: "
                    "everything else it holds); or the recent sessions. Can pause or resume "
                    "it.",
            permission="read", input_model=StatusInput, handler=session_qc(status), egress="aggregates"),
        cap(name="gating.session_finish", entitlement="ai:gating:session", tool_name="gating_session_finish",
            purpose="Finish a gating session: close it, commit a propose-mode session's "
                    "gates, cancel its bulk pass, or roll back every gate it wrote.",
            permission="reversible_write", input_model=FinishInput, handler=session_qc(finish),
            writes=("gates",), persistent=True, egress="aggregates"),
        cap(name="gating.compare_images", entitlement="ai:gating:analytics", tool_name="compare_gates_across_images",
            purpose="One marker across a dataset's images: each image's intensities aligned "
                    "to a reference image's, the reference gate carried through, the drift "
                    "class (stable, drift, batch, image-specific, changed, failed) and the "
                    "strategy it implies. A job; the image is the unit.",
            permission="read", input_model=CompareInput, handler=compare, execution="job",
            requires=None, egress="aggregates", reads=("table", "gates"),
            tags=TAGS + ("dataset", "cohort", "batch", "drift", "compare")),
        cap(name="gating.export", tool_name="export_gates",
            purpose="Write a project's or dataset's gates (with provenance) to CSV files "
                    "under Plexora's data directory, for other tools.",
            permission="read", input_model=ExportInput, handler=export, egress="aggregates",
            reads=("gates",), tags=TAGS + ("export", "csv", "download")),
        cap(name="gating.report", entitlement="ai:gating:session", tool_name="gating_report",
            purpose="The review report of a gating session, HTML and/or PDF: per marker the "
                    "final gate, the GMM proposal, confidence, flags, the distribution and "
                    "the near-gate cells; for a dataset the spread across images.",
            permission="read", input_model=ReportInput, handler=session_qc(gating_report),
            egress="rendered_pixels", reads=("gates",),
            tags=TAGS + ("report", "pdf", "html", "review", "provenance")),
    ]
