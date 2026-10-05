"""What an agent may say back to a QC packet: typed, bounded, closed.

The agent never types a coordinate or a threshold. It says whether a channel
looks clean, whether an outlined region is an artifact and of what class,
picks among outlines or cutoffs the server proposed, or names grid squares --
every one a closed vocabulary here, so an answer the engine cannot act on is
refused at validation rather than half-applied. One model per packet kind,
discriminated by `kind`; the four cell modules share one model. A packet may
carry several units of one kind -- candidates to confirm, cell modules --
and is then answered per unit (`verdicts` by candidate label, `modules` by
module name) with the same fields a single unit's answer has.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union, get_args

from pydantic import Field, model_validator

from plexora.agent.schemas import AgentModel, clipped
from plexora.plugins.qc.server import schemas

#: The words an answer may use besides ids: a problem no outline covers, the
#: outline already drawn, and "no outline fits" (a grid next).
ELSEWHERE = "elsewhere"
CURRENT = "current"
NONE_FITS = "none_fits"
CANNOT_TELL = "cannot_tell"

ArtifactClass = Literal[schemas.AGENT_CLASSES]
Confidence = Literal[tuple(schemas.AI_CONFIDENCE)]
_CONFIDENCE = ("How sure the judgment is: " + ", ".join(f"`{w}`" for w in schemas.AI_CONFIDENCE)
               + ". Say `unsure` rather than guess.")

#: What `notes` are for: kept with the decision and shown to the user as the
#: finding's one-line explanation when they hover it in the viewer.
_NOTES = ("One or two plain sentences on what you saw, kept with the decision and shown "
          "to the user as the finding's explanation in the viewer. At most 300 characters; "
          "a longer note is cut.")


class _Base(AgentModel):
    notes: Annotated[str, clipped(300)] = Field("", max_length=300, description=_NOTES)


class ChannelVerdict(AgentModel):
    verdict: Literal["clean", "suspicious", "uncertain"] = Field(
        description="clean: nothing technical wrong in this channel at this scale; "
                    "suspicious: something is (name where); uncertain: a closer look "
                    "is needed to tell.")
    class_hint: ArtifactClass | None = Field(None, description="What it looks like, when "
                                             "suspicious.")
    where: list[str] = Field(default_factory=list, max_length=8,
                             description="Candidate labels on this tile (c1, c2, ...) that "
                                         "worry you (or, when uncertain, that you are "
                                         "unsure of), or `elsewhere` for a problem no "
                                         "outline covers.")


class ChannelAuditAnswer(_Base):
    kind: Literal["channel_audit"] = "channel_audit"
    verdicts: dict[str, ChannelVerdict] = Field(description="One verdict per channel row, "
                                                "keyed by channel name.")


ConfirmVerdictWord = Literal["artifact", "not_artifact", "need_more_evidence", "cannot_tell"]
_VERDICT = ("artifact: a technical problem, not biology; not_artifact: real tissue or signal; "
            "need_more_evidence: a closer look would settle it.")


class ConfirmVerdict(AgentModel):
    """One candidate's judgment: the whole answer of a single-candidate
    packet, or one entry of a batched packet's `verdicts`."""

    verdict: ConfirmVerdictWord = Field(description=_VERDICT)
    artifact_class: ArtifactClass | None = Field(
        None, description="With `artifact`. autofluorescence only when an autofluorescence "
                          "(blank, unstained) channel shows it or the same structures are "
                          "bright in two or more markers; one marker's diffuse off-target "
                          "signal is excessive_background (autofluorescence without that "
                          "evidence is recorded as excessive_background).")
    severity: Literal["minor", "moderate", "severe"] | None = Field(
        None, description="minor: cells there are still readable; moderate: some markers "
                          "unreliable; severe: nothing there can be trusted.")
    boundary: Literal["covers", "too_small", "too_large", "wrong_place", "cannot_tell"] = Field(
        "covers", description="Does the whole artifact lie inside the magenta outline? It "
                              "is where Plexora traces the artifact's own pixels, so it may "
                              "be larger than the artifact.")
    scope: Literal[schemas.SCOPES] | None = Field(None, description="Which channels it "
                                                  "affects, when you can tell.")
    exclude_recommended: bool | None = Field(None, description="Should cells in it be "
                                             "excluded (false: warn only)?")
    confidence: Confidence = Field("fairly_sure", description=_CONFIDENCE)
    notes: Annotated[str, clipped(300)] = Field("", max_length=300, description=_NOTES)


class ArtifactConfirmAnswer(ConfirmVerdict):
    """A single candidate's packet: its fields at the top level (`verdict`,
    `artifact_class`, ...). A packet of several candidates (one sheet row
    each, labelled): `verdicts`, keyed by label, each entry those fields."""

    kind: Literal["artifact_confirm"] = "artifact_confirm"
    verdict: ConfirmVerdictWord | None = Field(
        None, description=_VERDICT + " (a single-candidate packet)")
    verdicts: dict[str, ConfirmVerdict] | None = Field(
        None, description="A packet of several candidates: one judgment per candidate "
                          "label (the sheet's row labels, e.g. c3), each with the fields "
                          "above.")

    @model_validator(mode="after")
    def _one_form(self):
        if (self.verdict is None) == (self.verdicts is None):
            raise ValueError("give `verdict` (one candidate) or `verdicts` keyed by "
                             "candidate label (several), not both and not neither")
        return self

    def judgments(self, labels):
        """{label: ConfirmVerdict} for the packet's candidate labels."""
        if self.verdicts is not None:
            return dict(self.verdicts)
        if len(labels) != 1:
            return {}
        return {labels[0]: ConfirmVerdict(**self.model_dump(
            include=set(ConfirmVerdict.model_fields)))}


class ArtifactScopeAnswer(_Base):
    kind: Literal["artifact_scope"] = "artifact_scope"
    chosen: str = Field(description="The id of the scope option that fits, or `cannot_tell`.")
    confidence: Confidence = Field("fairly_sure", description=_CONFIDENCE)


class ArtifactLocalizeAnswer(_Base):
    kind: Literal["artifact_localize"] = "artifact_localize"
    chosen: str = Field(description="The letter of the outline that covers the artifact "
                                    "(A..E), `current`, or `none_fits` for a grid.")
    confidence: Confidence = Field("fairly_sure", description=_CONFIDENCE)


class ArtifactGridAnswer(_Base):
    kind: Literal["artifact_grid"] = "artifact_grid"
    cells: list[str] = Field(default_factory=list, max_length=256,
                             description="The grid squares the artifact covers (A1, B3, ...).")
    refine: bool = Field(False, description="Ask for a finer grid inside these squares.")
    artifact_class: ArtifactClass | None = Field(
        None, description="Only when a closer look shows another artifact than the one it "
                          "was raised as (a fold, not debris, say).")
    severity: Literal["minor", "moderate", "severe"] | None = Field(
        None, description="When you can tell: minor, moderate or severe.")
    confidence: Confidence = Field("fairly_sure", description=_CONFIDENCE)


SideVerdict = Literal["accept", "too_aggressive", "too_lenient", "not_artifact", "cannot_tell"]
RowRead = Literal["mostly_artifact", "mostly_real", "mixed", "cannot_tell"]


class CellModuleVerdict(AgentModel):
    """One cell module's judgment: the whole answer of a single module's
    packet, or one entry of a combined packet's `modules`."""

    low: SideVerdict = Field("accept", description="The low-side cutoff: accept; "
                             "too_aggressive (it flags real cells: move it out); too_lenient "
                             "(artifacts pass it: move it in); not_artifact (these extremes "
                             "are biology: warn only, never exclude). A side not drawn "
                             "is kept as proposed whatever it says.")
    high: SideVerdict = Field("accept", description="The high-side cutoff, the same words.")
    rows: dict[str, RowRead] = Field(default_factory=dict,
                                     description="Per collage row (its label), what the cells "
                                                 "in it are.")
    confidence: Confidence = Field("fairly_sure", description=_CONFIDENCE)


class CellCutoffAnswer(CellModuleVerdict):
    kind: Literal["cell_segmentation"]
    notes: Annotated[str, clipped(300)] = Field("", max_length=300, description=_NOTES)


class CellModulesAnswer(_Base):
    kind: Literal["cell_modules"] = "cell_modules"
    modules: dict[str, CellModuleVerdict] = Field(
        description="One judgment per module of the packet, keyed by module name "
                    "(seg_under, seg_over, seg_size, seg_shape), "
                    "each with low/high/rows/confidence as a single module's "
                    "answer.")


StratumVerdict = Literal[schemas.SCORE_VERDICTS]


class ScoreReviewAnswer(_Base):
    """What the places sampled across one check's score show."""

    kind: Literal["score_review"] = "score_review"
    strata: dict[str, StratumVerdict] = Field(
        description="One verdict per row of places shown, keyed by the row's stratum "
                    "(clear_good, borderline_below, borderline_above, strongly_abnormal, "
                    "clustered): artifact -- the tiles show the check's problem (blur, "
                    "misregistered nuclei, a mask drawn wrong); normal -- normal tissue or "
                    "signal, however the number reads; mixed -- some of each; cannot_tell. "
                    "Judge what the tiles show, not the scores in their captions.")
    threshold: Literal[schemas.THRESHOLD_VERDICTS] = Field(
        "accept", description="Where the bar sits: accept -- the rows beyond it are "
                              "artifacts and the borderline rows are what a bar should "
                              "split; too_lenient -- artifacts sit below the bar (just "
                              "below looks like the problem): it comes down one step; "
                              "too_aggressive -- normal tissue sits above it (just above "
                              "looks normal): it goes up one step; cannot_tell.")
    whole_tissue: Literal["artifact", "normal", "cannot_tell"] | None = Field(
        None, description="The whole-tissue row, when the packet's `global.possible` says "
                          "the problem may be everywhere: artifact when the whole "
                          "channel, cycle or mask shows it (it becomes one region over the "
                          "tissue); normal; cannot_tell.")
    artifact_class: ArtifactClass | None = Field(
        None, description="Only when the flagged places show another artifact than the "
                          "check's own class (blur that is really a fold, say).")
    severity: Literal["minor", "moderate", "severe"] | None = Field(
        None, description="How bad the flagged places are: minor -- cells still readable; "
                          "moderate -- some markers unreliable; severe -- nothing there can "
                          "be trusted.")
    confidence: Confidence = Field("fairly_sure", description=_CONFIDENCE)


class FinalConcern(AgentModel):
    target: str = Field(max_length=40, description="A region label (r1, r2...) or a cell "
                                                   "module's name.")
    issue: Literal["over_excluded", "under_excluded", "wrong_class", "boundary", "scope"]
    note: Annotated[str, clipped(200)] = Field("", max_length=200)


class FinalReviewAnswer(_Base):
    kind: Literal["final_qc_review"] = "final_qc_review"
    verdict: Literal["consistent", "inconsistent", "cannot_tell"]
    concerns: list[FinalConcern] = Field(default_factory=list, max_length=8)
    recommend_manual_review: bool = False


MODELS = (ChannelAuditAnswer, ArtifactConfirmAnswer, ArtifactScopeAnswer,
          ArtifactLocalizeAnswer, ArtifactGridAnswer, CellCutoffAnswer, CellModulesAnswer,
          ScoreReviewAnswer, FinalReviewAnswer)

Answer = Annotated[Union[MODELS], Field(discriminator="kind")]


def _kinds(model):
    annotation = model.model_fields["kind"].annotation
    return get_args(annotation) or (model.model_fields["kind"].default,)


BY_KIND = {kind: model for model in MODELS for kind in _kinds(model)}
KINDS = tuple(BY_KIND)


#: Kept verbatim from the pydantic schema: enough for the agent to know a
#: 9th item in an 8-item list is refused before it tries one (`max_length=`
#: on a list, in answers.py), and that a `notes` past its limit is cut.
_LENGTH_KEYS = ("maxLength", "minLength", "maxItems", "minItems")


def _short(prop, defs, depth=0):
    """A property's schema, compact but complete: nested models inlined."""
    if "$ref" in prop:
        target = defs.get(prop["$ref"].rsplit("/", 1)[-1], {})
        prop = {**target, **{k: v for k, v in prop.items() if k != "$ref"}}
    out = {k: prop[k] for k in ("type", "enum", "description", "const") + _LENGTH_KEYS
          if k in prop}
    if "anyOf" in prop:
        options = [_short(p, defs, depth + 1) for p in prop["anyOf"] if p.get("type") != "null"]
        if len(options) == 1:
            out.update({k: v for k, v in options[0].items() if k not in out})
            out["nullable"] = True
        else:
            out["anyOf"] = options
    if depth < 3 and isinstance(prop.get("properties"), dict):
        out["type"] = "object"
        out["properties"] = {k: _short(v, defs, depth + 1)
                             for k, v in prop["properties"].items()}
        if prop.get("required"):
            out["required"] = prop["required"]
    if depth < 3 and isinstance(prop.get("items"), dict):
        out["items"] = _short(prop["items"], defs, depth + 1)
    if "additionalProperties" in prop and isinstance(prop["additionalProperties"], dict):
        out["values"] = _short(prop["additionalProperties"], defs, depth + 1)
    return out


def schema_for(kind) -> dict:
    """The JSON schema of one kind's answer (what a packet tells the agent)."""
    schema = BY_KIND[kind].model_json_schema()
    defs = schema.get("$defs", {})
    properties = {k: _short(v, defs) for k, v in schema.get("properties", {}).items()}
    if "kind" in properties:
        properties["kind"] = {"const": kind}
    return {"kind": kind, "required": [r for r in schema.get("required", [])],
            "properties": properties}
