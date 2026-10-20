"""The gating session as tools: start it, fetch a decision, answer it, finish.

    gating_session_start  -> {session_id, job_id}         (the bulk pass runs as a job)
                             or `needs_setup` + an `expression_setup` packet, when which
                             matrix to gate cannot be settled from the values
                             (`setup: ask`); or `needs_setup` + `setup`, waiting on the
                             viewer's set-up form (`setup: wait`); or refused, saying what
                             is missing (a column not found; `setup: fail`, the matrix too)
    gating_next           -> one decision packet (+ <= 2 images), or a status
    gating_answer         -> the outcome, and the next packet inline
    gating_session_status -> every unit's state, the budget, the questions
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

from pydantic import Field

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
                               "the user's words: 'skin', 'lymph node', 'colon'.")
    disease: str | None = Field(None, max_length=120, description="The disease or sample "
                                "type, in the user's words: 'melanoma', 'adenocarcinoma'.")
    notes: str | None = Field(None, max_length=400, description="Anything else the user "
                              "said about the sample's biology.")
    original: str | None = Field(None, max_length=1000, description="The user's note exactly "
                                 "as written, when tissue/disease/notes were interpreted from "
                                 "it (`plexora.ai.context`); kept beside the interpretation.")
    interpretation: dict | None = Field(None, description="The Context Interpreter's settled "
                                        "reading of `original` (populations of interest, "
                                        "corrected terms, ambiguities, scope).")


class ExpressionChoice(AgentModel):
    """Which matrix to gate on, settled before the start (`SessionOptions.expression`)."""
    features_layer: str = Field(description="`X` or `layer:<name>`, as "
                                            "inspect_expression_sources lists them.")
    features_log: bool = Field(description="Apply log1p as the values are read. Never for a "
                                           "matrix that is already log-transformed.")
    basis: Literal["user", "inferred"] = Field(
        "user", description="user: the user named it (`plexora ai run --matrix/--log1p`); "
                            "it is applied even over an earlier confirmed choice. inferred: "
                            "a model read it from the values (`plexora.ai.harness.expression`); "
                            "applied only where nothing is confirmed.")
    why: str | None = Field(None, max_length=400, description="Why, in a sentence.")


class SessionOptions(AgentModel):
    scope: Literal["project", "dataset"] = Field(
        "project", description="project: one image; dataset: every image of a dataset, the "
                               "reference image first and gates carried to the rest.")
    project: str | None = Field(None, description="With scope=project.")
    dataset: str | None = Field(None, description="With scope=dataset: name or id.")
    markers: list[str] | None = Field(None, description="Default: every marker except "
                                      "structural channels (DNA and the like).")
    mode: Literal["apply", "propose"] = Field(
        "apply", description="apply: accepted gates -- and the empty gate of a failed or "
                             "all-negative marker -- are written as they are decided (each "
                             "receipted and undoable); propose: nothing is written until "
                             "gating_session_finish(action=commit).")
    overwrite_manual: bool = Field(False, description="Also re-gate markers the user set by "
                                   "hand (never locked or approved ones).")
    max_tier: int = Field(schemas.ENGINE["max_tier_default"], ge=2, le=len(schemas.TIERS),
                          description="2: one look per marker; 3: + reference channels; 4: + "
                                      "candidate refinement; 5: + questions for the user. "
                                      "There is no numbers-only tier: every gate is judged "
                                      "on the image's pixels.")
    force: bool = Field(False, description="Start even when only some markers match an image "
                        "channel: only those are gated, and the rest are skipped "
                        "(skipped_no_channel) without a gate. Without it a partial match "
                        "refuses to start; when no marker matches, nothing starts either way.")
    setup: Literal["ask", "wait", "fail"] = Field(
        "ask", description="What the start does when the project's gating set-up is not "
                           "finished: a required column (cell id, coordinates, image id) not "
                           "found or never confirmed, or an expression matrix its values "
                           "cannot settle. ask: a column not found refuses the start, and the "
                           "matrix comes to you as an expression_setup packet (put it to the "
                           "user; never guess); fail: nothing starts, and the error says what "
                           "is missing and how to supply it (a run with nobody to ask: "
                           "`plexora ai run`); wait: the session waits in needs_setup, nothing "
                           "profiled, until the user finishes the viewer's set-up form -- every "
                           "column and the matrix (Plexora AI started from the viewer).")
    expression: ExpressionChoice | None = Field(
        None, description="The matrix to gate on, when the user named it or a model read it "
                          "from the values before the start; checked against the matrices the "
                          "project has, and never log1p on values already logged.")
    audit_sheet: bool = Field(True, description="Show automatically accepted markers on a "
                                                "batched audit sheet (~70 tokens a marker).")
    reference_image: str | None = Field(None, description="Dataset scope: the image gated "
                                        "first (default: the one with the most cells).")
    mirror: bool = Field(False, description="Show each decision in an open viewer too.")
    view_id: str | None = None
    mirror_delay_ms: int = Field(mirroring.DEFAULT_DELAY_MS, ge=0, le=5000)
    image_format: Literal["webp", "png"] = "webp"
    field_um: float = Field(_FIELD_UM, ge=_FIELD_UM_BOUNDS[0], le=_FIELD_UM_BOUNDS[1],
                            description="The context sheet's tissue fields, microns a side: "
                                        "sized from each image's own pixel size (or the "
                                        "session's estimate of it).")
    reading: Literal["once", "every_packet"] = Field(
        "once", description="once: the reading guide comes with the session's start and "
                            "status, and packets name the entries they use; every_packet: "
                            "each packet repeats the texts it needs.")
    evidence: Literal["delta", "full"] = Field(
        "delta", description="delta: an evidence value this conversation was already sent "
                             "comes as {as_in: packet_id} (reading_guide.as_in), and briefs "
                             "carry no repeated text; full: every packet whole.")
    sheets: Literal["trim", "full"] = Field(
        "trim", description="trim: a context-sheet row this conversation was already shown "
                            "with the same gate and fields is left out, and a whole-image "
                            "check shows only the slide scale; full: every sheet whole.")
    budget: Budget = Field(default_factory=Budget, description="What each marker may spend.")
    agent: str = Field("agent", max_length=80, description="Who answers (the model, say). "
                       "Earlier answers to identical packets are reused only from the same "
                       "agent.")
    reuse_answers: bool = Field(True, description="Answer a packet identical to one this agent "
                                "answered before (same data, partners, code and seed) with that "
                                "answer, without asking: a rerun then reaches the same gates. "
                                "False asks everything afresh.")
    on_limit: Literal[schemas.LIMIT_POLICIES] = Field(
        default_factory=lambda: _limit_default("on_limit"),
        description="When a marker reaches its allowance of looks or candidate rounds while "
                    "the evidence still says to go on: ask (the user is asked in the viewer "
                    "and you are told; the marker waits, the rest goes on), extend (another "
                    "allowance without asking: automated runs), stop (flag it for review). "
                    "A marker is never accepted because it ran out. Default from "
                    "PLEXORA_GATING_ON_LIMIT (set by `plexora mcp serve --gating-on-limit`).")
    max_extensions: int = Field(
        default_factory=lambda: _limit_default("max_extensions"), ge=0, le=10,
        description="How many extra allowances one marker may get before it is flagged for "
                    "review. Default from PLEXORA_GATING_MAX_EXTENSIONS.")
    qc: Literal["strict", "exclude", "off"] = Field(
        "strict", description="Which QC failures the session leaves out of every fit, sample, "
                              "collage and field (the gates still apply to every cell). strict: "
                              "cells QC called exclude or warn, and per marker the cells QC "
                              "flagged that marker unreliable in; exclude: warn calls kept; "
                              "off: QC ignored (only when the user asks, or QC is wrong). No "
                              "effect on an image QC has not been run on.")
    biology: Biology | None = Field(None, description="The tissue and disease the user "
                                    "named for this sample (\"a melanoma from the skin\": "
                                    "tissue skin, disease melanoma). A prior for references, "
                                    "co-expression, spatial reading and confidence -- never "
                                    "a rule. Left out, it is taken from the project's "
                                    "metadata or inferred from the panel, and marked so.")
    defer_parents: bool = Field(True, description="A marker whose own distribution is weak "
                                "(no robust partner curve, a poorly separated mixture) waits "
                                "for its children in the panel's hierarchy -- markers whose "
                                "positives are a subset of its own -- and is then scored with "
                                "where their positives sit (once, never after a look).")
    known_guide: str | None = Field(None, description="The `guide_version` of a reading guide "
                                    "you already hold (from an earlier session of this "
                                    "build): it is then not sent again.")
    display_check: Literal[schemas.DISPLAY_CHECKS] = Field(
        "vision", description="Auto contrast -- how bright each marker is drawn in every look: "
                              "`vision` (the default) puts a contrast grid to the model after "
                              "the panel context (markers in rows, five brightnesses in "
                              "columns; code picks the column) for the markers no earlier check "
                              "settled -- once per image, a choice outlives the session, so an "
                              "image an auto-contrast run already did is not asked again; "
                              "`rule` keeps the calibration's own window (black under the "
                              "background, the bright cells' structure near the top) and shows "
                              "it; `off` leaves the step out.")
    purpose: Literal[schemas.PURPOSES] = Field(
        "gating", description="gating: decide the markers' gates. contrast: auto contrast on its "
                              "own (contrast_session_start) -- the set-up, the calibration and "
                              "the contrast check; no marker is profiled or gated, and each "
                              "closes `display_set` once its image's brightness is settled.")
    tiles: bool = Field(False, description="Draw every look's cells as a 2 x 2 block: "
                        "morphology, the context with the judged cell outlined, and one "
                        "expected and one not-expected partner channel from the panel "
                        "context (reading guide `collage_t2m`). Off, the looks are the "
                        "two-panel cells.")
    absence_check: bool = Field(False, description="Before a marker's first look, show six "
                                "regions of the tissue with nothing marked and ask whether "
                                "any cell is clearly positive by its own stain: none in any "
                                "region closes the marker with no positives (gate at the "
                                "maximum), and a technical check's failed-stain verdict "
                                "stands only when the regions agree. Off by default: on 12 "
                                "held-out images 9 of its 17 closes were real populations of "
                                "0.5-8 % (bench GATING_GENERALISE). False: the first look "
                                "comes straight away.")
    split_mixed: bool = Field(True, description="A look at candidates that stops at a row "
                              "judged mixed and wider than two background sds gets that row "
                              "split into finer candidates and looked at again, instead of "
                              "the gate stopping at its edge.")
    whole_image_moves: bool = Field(False, description="After a look at candidates, a "
                                    "whole-image check that calls the gate too low or too "
                                    "high sends it back to the candidates (opposite ways end "
                                    "in review). False: the gate stays where the rows put it, "
                                    "at low confidence, the disagreement noted -- the rows "
                                    "judged those very cells.")
    whole_image_reopen: bool = Field(True, description="After a look at candidates, a "
                                     "whole-image check that calls the gate too low sends it "
                                     "up for one more look at the candidates above, where the "
                                     "first row passes even when judged mixed (two views "
                                     "agree); the rows still place the gate. False: it stays "
                                     "where the rows put it, at low confidence.")
    absence_confirm: bool = Field(True, description="When exactly one region of an absence "
                                  "picture shows clear positives and the others none or few, "
                                  "that region is shown again close up and in its "
                                  "surroundings before it keeps the marker open: an edge band "
                                  "or a fold reads as one there, a cluster of real cells "
                                  "still as cells.")
    classifier: Literal[schemas.CLASSIFIER_MODES] = Field(
        "layer", description="The Single Cell Classifier beside each gate. layer: after a "
                             "gate is written, the classifier is trained at it and calls "
                             "every cell -- the gate's call unless it is confident the other "
                             "way, taking positives away more readily than adding them, and "
                             "only taking them away where the marker's signal is poor; kept "
                             "beside the threshold (which still decides the marker) until "
                             "the user adopts it in the Thresholding panel. check: that, and "
                             "where the classifier trained at a gate disagrees with it beside "
                             "it, the cells it flags are shown in a look at the candidates on "
                             "that side, beside cells it agrees on; the gate moves past each "
                             "row the look confirms (unmeasured). off: neither.")
    check_reading: Literal[schemas.CHECK_READINGS] = Field(
        "auto", description="How a classifier check's look is read. auto: word for a marker "
                            "whose stain is fibrous (smooth muscle, vessel walls: the panel's "
                            "morphology), mixed for the rest. word: one word per row; "
                            "the cells the classifier flags must read as their own stain. "
                            "mixed: the same words, and flagged cells read as mixed count "
                            "as stained. count: the reader counts the cells in each row "
                            "that carry their own stain (unmeasured).")
    replan: bool = Field(True, description="Re-plan when markers fail: the markers left are "
                         "reordered and rescored without the failed gates, accepted gates that "
                         "only a failed one placed go to review, and what only biology can "
                         "settle (a population that may be absent, an image whose looks keep "
                         "naming the same artifact) is put to the planning task in a `replan` "
                         "packet. False keeps the plan made at the start.")
    seed: int = 0
    delegate: bool = Field(True, description="Return the `delegate` block: how to hand the "
                           "packet loop to fresh worker conversations on a cheaper or the "
                           "same model (plexora/ai/delegation.py).")


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
    from plexora.plugins.gating.server.autogate import context

    first = call.session.data(images[0])
    panel = context.for_project(first)
    available = list(dict.fromkeys(m for p in images for m in call.session.data(p).table.markers))
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
        present = set(ds.table.markers)
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
    from plexora.plugins.gating.server.autogate import engine as engines

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
        markers = call.session.data(project).table.markers
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
    return {"order": order, "skipped_markers": plan["skipped"], "units": units,
            "panel_hash": panel["panel_hash"], "biology": biology_record,
            "panel_pending": bool(unresolved), "unresolved": unresolved,
            "channel_match": {k: match[k] for k in ("state", "n_markers", "n_matched",
                                                    "unmatched")},
            "pixel": pixel, "display": display, "qc_exclusion": qc_blocks,
            "qc_missing": _qc_missing(qc_blocks)}


def _unplanned_fields() -> dict:
    """A session waiting on a column it cannot plan without: planned at
    `_settle_setup`."""
    return {"order": [], "skipped_markers": [], "units": {}, "panel_hash": None,
            "biology": None, "panel_pending": False, "unresolved": [], "channel_match": None,
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


def prepare(call, project, choice=None, *, setup="wait") -> dict:
    """The deterministic half of a start, with no session: what a driver
    collects before it calls any model (`plexora.ai.harness.decision`).

    Settles the matrix where the values settle it (`_expression_check`,
    applied and confirmed) or where the caller's `choice` does
    (`_given_expression`), receipted under `call.operation_id`; then says
    what is still open. {"needs", "missing", "expression", "applied",
    "receipts"}: `needs` is what a session started with `setup` would wait
    on or refuse for -- wait: everything the set-up form would ask; fail and
    ask: a column not found, and the matrix when it is unsettled -- or None.
    `missing`: the columns not found. Nothing else is written."""
    images = [project]
    call.session.project(project)
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
            if requires.applies_to(record) and not requires.missing_from(record):
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
            sizes = {name: call.session.data(name).table.geometry().height for name in images}
            reference = max(images, key=lambda n: (sizes[n], -images.index(n)))
        images = [reference] + [n for n in images if n != reference]
    if len(images) > 1:
        # The reference image stays held while the bulk pass walks the rest;
        # an evicted table would refit every marker for every packet.
        call.session.table_limit = max(call.session.table_limit, 4)
    # A column the set-up form would have to ask for (no cell id, no
    # coordinates): nothing can be planned without it, and only the user can
    # name it -- so only a session that waits for the form starts without it.
    gaps = setup_needs(call.session, images, confirmations=False)
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
    needs = setup_needs(call.session, images) if inp.setup == "wait" else None
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
    order = record["order"]
    st = engines.store()
    st.create(record)
    st.sweep()
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
            "n_units": len(record["units"]), "skipped_markers": record["skipped_markers"],
            "unresolved_markers": unresolved, "mode": inp.mode,
            "mirror": {k: record["mirror"].get(k) for k in ("status", "view_id", "last_error",
                                                            "hint") if record["mirror"].get(k)},
            "receipt": receipt.model_dump(mode="json"),
            "biology": _biology_brief(record["biology"]),
            "resource": session_uri(session_id), "expression": expression,
            "pixel": _pixel_brief(record["pixel"]), "state": record["state"],
            "channels": _channels_brief(record["channel_match"]),
            "qc_exclusion": record["qc_exclusion"],
            **_qc_warning(record["qc_exclusion"]),
            **_unconfirmed_note(call.session, images, inp.setup),
            **_guide(inp.reading, inp.known_guide),
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
                                f"{tool_name_of('gating.session_status')}(session_id); call "
                                "that yourself, without known_guide, if you answer packets")
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


def _qc_warning(blocks) -> dict:
    """A warning in the start output for `_qc_missing` images -- never a
    silent no-op: artifacts there (an edge band, a fold) reach every marker's
    fit, sample and look."""
    missing = _qc_missing(blocks)
    if not missing:
        return {}
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


def _guide(reading, known=None):
    """The reading guide, once per session (`packets.reading_guide`): a
    packet names the entries it relies on (and its answer schema) instead of
    repeating them. Byte-stable for a build, so a client caches it; a caller
    that already holds this `guide_version` (`known_guide`) is not sent it."""
    from plexora.plugins.gating.server.autogate import packets

    if reading != "once":
        return {}
    version = packets.guide_version()
    if known == version:
        return {"guide_version": version, "reading_guide": "unchanged (you hold it)"}
    return {"guide_version": version, "reading_guide": packets.reading_guide(),
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
    return {"delegate": delegation.block("gating_worker", first_task=first, session_id=session_id,
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
                                  ("chosen", e.get("chosen"))) if v}
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
    no tab open, no server attached."""
    mirror = {"enabled": inp.mirror, "view_id": inp.view_id, "status": "off",
              "last_error": None}
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
    """What an agent needs of the viewer's mirror: whether it worked."""
    if mirror.get("status") == "ok" and not mirror.get("last_error"):
        return {k: mirror[k] for k in ("status", "sent", "resent") if k in mirror}
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
        time.sleep(0.1 if status == "busy" else 0.5)


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
    return {k: mirror.get(k) for k in ("status", "view_id", "last_error", "sent")}


def _mirror(call, session_id, packet, *, replay=False):
    """Show the packet in the session's viewer; returns what the agent is
    told about it (`packet["mirror"]`), and records it on the session.

    `replay` is the tab asking to watch again (`attach_viewer`): it skips a
    packet the harness has just shown, and a viewer detached meanwhile."""
    from plexora.agent.sessions import mirror as session_mirror
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import mirror_script

    packet_id = packet.get("packet_id")
    with session_mirror.send_lock(session_id):
        if replay:
            st = engines.store()
            current = st.load(session_id).get("mirror") or {}
            if st.control(session_id).get("viewer_detached") or (
                    current.get("status") == "ok" and current.get("packet_id") == packet_id
                    and time.time() - float(current.get("sent_at") or 0) < 30):
                return _mirror_brief(current)
        try:
            result = mirror_script.run(call, session_id, packet)
        except Exception as exc:  # mirroring never breaks a session
            result = {"status": "degraded", "sent": 0, "errors": [{"message": str(exc)}]}
        with engines.engine_for(call, session_id) as engine:
            mirror = engine.record.setdefault("mirror", {})
            mirror["status"] = result.get("status")
            mirror["last_error"] = (result.get("errors") or [None])[0]
            mirror["view_id"] = result.get("view_id") or mirror.get("view_id")
            mirror["sent"] = int(result.get("sent") or 0)
            mirror["packet_id"] = packet_id
            mirror["sent_at"] = time.time()
            return _mirror_brief(mirror)


def _mirror_display(call, session_id, project):
    """Show the mirrored tab the brightness a contrast check just settled,
    right after the answer (`mirror_script.run_display`): the tab plays it
    while the session moves on -- and a contrast run, which has no next
    packet to carry it, still plays it. Best effort, like every mirror."""
    from plexora.agent.sessions import mirror as session_mirror
    from plexora.plugins.gating.server.autogate import engine as engines
    from plexora.plugins.gating.server.autogate import mirror_script

    with session_mirror.send_lock(session_id):
        try:
            result = mirror_script.run_display(call, session_id, project)
        except Exception as exc:  # mirroring never breaks a session
            result = {"status": "degraded", "sent": 0, "errors": [{"message": str(exc)}]}
        if not result.get("sent") and result.get("status") == "ok":
            return
        with engines.engine_for(call, session_id) as engine:
            mirror = engine.record.setdefault("mirror", {})
            mirror["status"] = result.get("status")
            mirror["last_error"] = (result.get("errors") or [None])[0]
            mirror["view_id"] = result.get("view_id") or mirror.get("view_id")


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
    pause: bool | None = Field(None, description="Pause (true) or resume (false) the "
                                                 "session.")
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
    with engines.engine_for(call, inp.session_id, st=st) as engine:
        record = engine.record
        reader = None
        if engine.options["reading"] == "once" and inp.known_guide != packets.guide_version():
            # A caller without the guide is a new conversation: nothing sent
            # before may be pointed at (`packets.as_in`).
            reader = packets.new_reader(record)
        if inp.reattach_viewer:
            mirror_ = record.setdefault("mirror", {})
            mirror_.update(status="pending", enabled=True,
                           view_id=inp.view_id or mirror_.get("view_id"))
        replay = record.get("outstanding_packet") \
            if inp.reattach_viewer and inp.replay_mirror else None
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
               "summary": engines.summary_of(record),
               **_guide(record["options"]["reading"], inp.known_guide),
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
        if record.get("scope") == "dataset":
            out["dataset"] = transfer.dataset_summary(engine)
        models = engine.models_used()
        if models:
            out["models"] = models
    if replay and not st.control(inp.session_id).get("viewer_detached"):
        try:
            packet, _ = st.read_packet(inp.session_id, replay)
        except AgentError:
            packet = None     # answered meanwhile: the next packet is mirrored
        if packet is not None:
            out["mirror"] = _mirror(call, inp.session_id, packet, replay=True)
    return out


class FinishInput(AgentModel):
    session_id: str
    action: Literal["close", "commit", "cancel", "rollback"] = Field(
        "close", description="close: end the session (writes stay; a session the user "
                             "stopped in the viewer is cancelled); commit: write a "
                             "propose-mode session's accepted gates and empty gates; "
                             "cancel: stop the bulk pass (writes stay); rollback: undo every "
                             "write the session made, newest first.")


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
            record["state"] = "done"
        record["finished_at"] = _now()
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
    _announce(call, snapshot, inp.session_id, "finished", reason=reason, state=state,
              summary=summary, phase="summarizing")
    if _mirroring(mirror) and not detached:
        # A detached viewer is the user's own: nothing is put back in it.
        from plexora.plugins.gating.server.autogate import mirror_script

        try:
            out["teardown"] = mirror_script.run_teardown(call, inp.session_id, reason)
        except Exception as exc:  # the view is restored best effort
            out["teardown"] = {"status": "degraded", "errors": [{"message": str(exc)}]}
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
                    "window from its negative and bright cells, then a contrast grid per "
                    "batch of markers goes to you (gating_next / gating_answer) and code "
                    "picks the column. A marker an earlier run set is skipped unless "
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
            purpose="A gating session's units (state, confidence, gate), what it has spent, "
                    "its open questions, mirroring; or the recent sessions. Can pause or "
                    "resume it.",
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
