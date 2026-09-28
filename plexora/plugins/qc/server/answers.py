"""What an agent may say back to a QC packet: typed, bounded, closed.

The agent never types a coordinate or a threshold. It says whether a channel
looks clean, whether an outlined region is an artifact and of what class,
picks among outlines or cutoffs the server proposed, or names grid squares --
every one a closed vocabulary here, so an answer the engine cannot act on is
refused at validation rather than half-applied. One model per packet kind,
discriminated by `kind`; the four cell modules share one model.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union, get_args

from pydantic import Field

from plexora.agent.schemas import AgentModel
from plexora.plugins.qc.server import schemas

#: The words an answer may use besides ids: a problem no outline covers, the
#: outline already drawn, and "no outline fits" (a grid next).
ELSEWHERE = "elsewhere"
CURRENT = "current"
NONE_FITS = "none_fits"
CANNOT_TELL = "cannot_tell"

ArtifactClass = Literal[schemas.ARTIFACT_CLASSES]
Confidence = Literal[tuple(schemas.AI_CONFIDENCE)]
_CONFIDENCE = ("How sure the judgment is: " + ", ".join(f"`{w}`" for w in schemas.AI_CONFIDENCE)
               + ". Say `unsure` rather than guess.")


class _Base(AgentModel):
    notes: str = Field("", max_length=300, description="One or two sentences, for the record.")


class ChannelVerdict(AgentModel):
    verdict: Literal["clean", "suspicious", "uncertain"] = Field(
        description="clean: nothing technical wrong in this channel at this scale; "
                    "suspicious: something is (name where); uncertain: a closer look "
                    "is needed to tell.")
    class_hint: ArtifactClass | None = Field(None, description="What it looks like, when "
                                             "suspicious.")
    where: list[str] = Field(default_factory=list, max_length=8,
                             description="Candidate labels on this tile (c1, c2, ...) that "
                                         "worry you, or `elsewhere` for a problem no "
                                         "outline covers.")


class ChannelAuditAnswer(_Base):
    kind: Literal["channel_audit"] = "channel_audit"
    verdicts: dict[str, ChannelVerdict] = Field(description="One verdict per channel row, "
                                                "keyed by channel name.")


class ArtifactConfirmAnswer(_Base):
    kind: Literal["artifact_confirm"] = "artifact_confirm"
    verdict: Literal["artifact", "not_artifact", "need_more_evidence", "cannot_tell"] = Field(
        description="artifact: a technical problem, not biology; not_artifact: real tissue "
                    "or signal; need_more_evidence: a closer look would settle it.")
    artifact_class: ArtifactClass | None = Field(None, description="With `artifact`.")
    severity: Literal["minor", "moderate", "severe"] | None = Field(
        None, description="minor: cells there are still readable; moderate: some markers "
                          "unreliable; severe: nothing there can be trusted.")
    boundary: Literal["covers", "too_small", "too_large", "wrong_place", "cannot_tell"] = Field(
        "covers", description="Does the magenta outline cover the artifact?")
    scope: Literal[schemas.SCOPES] | None = Field(None, description="Which channels it "
                                                  "affects, when you can tell.")
    exclude_recommended: bool | None = Field(None, description="Should cells in it be "
                                             "excluded (false: warn only)?")
    confidence: Confidence = Field("fairly_sure", description=_CONFIDENCE)


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
    confidence: Confidence = Field("fairly_sure", description=_CONFIDENCE)


SideVerdict = Literal["accept", "too_aggressive", "too_lenient", "not_artifact", "cannot_tell"]
RowRead = Literal["mostly_artifact", "mostly_real", "mixed", "cannot_tell"]


class CellCutoffAnswer(_Base):
    kind: Literal["cell_intensity", "cell_area", "cycle_stability", "channel_outlier"]
    low: SideVerdict = Field("accept", description="The low-side cutoff: accept; "
                             "too_aggressive (it flags real cells: move it out); too_lenient "
                             "(artifacts pass it: move it in); not_artifact (these extremes "
                             "are biology: warn only, never exclude).")
    high: SideVerdict = Field("accept", description="The high-side cutoff, the same words.")
    rows: dict[str, RowRead] = Field(default_factory=dict,
                                     description="Per collage row (its label), what the cells "
                                                 "in it are.")
    pattern: Literal["tissue_loss", "registration", "focal_debris", "none"] | None = Field(
        None, description="cycle_stability only: what the lost cells look like.")
    confidence: Confidence = Field("fairly_sure", description=_CONFIDENCE)


class FinalConcern(AgentModel):
    target: str = Field(max_length=40, description="A region label (r1, r2...) or a cell "
                                                   "module's name.")
    issue: Literal["over_excluded", "under_excluded", "wrong_class", "boundary", "scope"]
    note: str = Field("", max_length=200)


class FinalReviewAnswer(_Base):
    kind: Literal["final_qc_review"] = "final_qc_review"
    verdict: Literal["consistent", "inconsistent", "cannot_tell"]
    concerns: list[FinalConcern] = Field(default_factory=list, max_length=8)
    recommend_manual_review: bool = False


MODELS = (ChannelAuditAnswer, ArtifactConfirmAnswer, ArtifactScopeAnswer,
          ArtifactLocalizeAnswer, ArtifactGridAnswer, CellCutoffAnswer, FinalReviewAnswer)

Answer = Annotated[Union[MODELS], Field(discriminator="kind")]


def _kinds(model):
    annotation = model.model_fields["kind"].annotation
    return get_args(annotation) or (model.model_fields["kind"].default,)


BY_KIND = {kind: model for model in MODELS for kind in _kinds(model)}
KINDS = tuple(BY_KIND)


def _short(prop, defs, depth=0):
    """A property's schema, compact but complete: nested models inlined."""
    if "$ref" in prop:
        target = defs.get(prop["$ref"].rsplit("/", 1)[-1], {})
        prop = {**target, **{k: v for k, v in prop.items() if k != "$ref"}}
    out = {k: prop[k] for k in ("type", "enum", "description", "const") if k in prop}
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
