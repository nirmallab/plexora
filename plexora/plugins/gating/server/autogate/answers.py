"""What an agent may say back to a gating packet: typed, bounded, closed.

The agent never types a threshold. It judges plausibility, says which way a
gate is wrong, picks among thresholds the server proposed, or names an
artifact -- and every one of those is a closed vocabulary here, so an answer
the engine cannot act on is refused at validation rather than half-applied.
One model per packet kind, discriminated by `kind`.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field

from plexora.agent.schemas import AgentModel
from plexora.ai import vocabulary
from plexora.plugins.gating.server.autogate import context, schemas

Artifact = Literal[schemas.ARTIFACTS]

_PARTNERS = (f"[{{marker: a marker of this panel, relation: {' | '.join(vocabulary.RELATIONS)}, "
             f"confidence: {' | '.join(vocabulary.CONFIDENCE)}}}]")
_CONFIDENCE = (f"Ordinal: {schemas.ENGINE['high_ai']:g} or more sure, "
               f"{schemas.ENGINE['moderate_ai']:g} fairly sure, below that guessing.")
RowVerdict = Literal["plausible", "implausible", "mixed", "cannot_tell"]


class Request(AgentModel):
    kind: Literal["reference_channel", "more_cells", "bivariate"]
    marker: str | None = Field(None, description="The reference or partner marker.")
    reason: str = Field("", max_length=200)


class AskUser(AgentModel):
    question: str = Field(max_length=300)
    options: list[str] = Field(default_factory=list, max_length=4)
    why: str = Field("", max_length=200)


class _Base(AgentModel):
    notes: str = Field("", max_length=300, description="One or two sentences, for the "
                                                       "record.")
    artifact_flags: list[Artifact] = Field(default_factory=list, description="Anything "
                                           "technical that affects the cells shown.")
    request: Request | None = None
    ask_user: AskUser | None = Field(None, description="Only for what the data and the "
                                     "panel cannot settle: binary vs continuous, expected "
                                     "prevalence, which partner to trust.")


class Plausibility(AgentModel):
    compartment: Literal["matches", "mismatch", "cannot_tell"] = Field(
        description="Is the stain where the panel context says it belongs?")
    pattern: Literal["membrane", "cytoplasmic", "nuclear", "diffuse", "punctate",
                     "cannot_tell"]
    positives_look_real: bool = Field(description="Do the cells above the gate carry real, "
                                      "cell-shaped staining (not background, bleed or "
                                      "autofluorescence)?")


class T1StripAnswer(_Base):
    kind: Literal["t1_strip"] = "t1_strip"
    verdicts: dict[str, Literal["ok", "suspicious", "cannot_tell"]] = Field(
        description="Per marker row: do the cells at the gate look split correctly?")


class QCAnswer(_Base):
    kind: Literal["qc_confirm"] = "qc_confirm"
    verdict: Literal["real_signal", "technical_failure", "cannot_tell"]


class T2Answer(_Base):
    kind: Literal["t2_confirm"] = "t2_confirm"
    plausibility: Plausibility
    rows: dict[Literal["below", "near", "above"], RowVerdict] = Field(
        default_factory=dict, description="Per row of the collage: are these cells called "
                                          "correctly? (`near` may be empty.)")
    direction: Literal["about_right", "too_low", "too_high", "cannot_tell", "not_binary"] = \
        Field(description="too_low: negatives are called positive (raise the gate); "
                          "too_high: real positives are missed (lower it).")
    magnitude: Literal["small", "medium", "large"] | None = None
    confidence: float = Field(ge=0, le=1, description=_CONFIDENCE)


class T3Answer(T2Answer):
    kind: Literal["t3_biological"] = "t3_biological"
    coexpression_consistent: bool | None = None
    exclusion_consistent: bool | None = None
    background_pattern: Literal["none", "diffuse", "edge", "nuclear_bleed",
                                "segmentation_boundary", "unknown"] = "none"


class T4Answer(_Base):
    kind: Literal["t4_candidates"] = "t4_candidates"
    chosen_candidate: str = Field(description="A candidate id (c1, c2, ...) or one of "
                                  + ", ".join(f"`{c}`" for c in schemas.T4_CHOICES)
                                  + " (the current gate was right; no row boundary separates "
                                    "the cells).")
    intervals: dict[str, Literal["mostly_positive", "mostly_negative", "mixed"]] = Field(
        default_factory=dict, description="Per interval row (i1, i2, ...): are the cells "
                                          "that flip there really positive?")
    confidence: float = Field(ge=0, le=1, description=_CONFIDENCE)


class ConfirmAnswer(_Base):
    kind: Literal["regression_confirm"] = "regression_confirm"
    verdict: Literal["holds", "too_low", "too_high", "artifact", "cannot_tell"]


class TransferAnswer(_Base):
    kind: Literal["transfer_check"] = "transfer_check"
    per_image: dict[str, Literal["holds", "too_low", "too_high", "cannot_tell"]]


class PanelEntry(AgentModel):
    marker: str
    role: Literal[vocabulary.ROLES]
    compartment: Literal[vocabulary.COMPARTMENTS] | None = None
    lineage: str | None = Field(None, max_length=120)
    binary: bool = True
    partners: list[dict] = Field(default_factory=list, max_length=context.MAX_PARTNERS,
                                 description=_PARTNERS)


class PanelContextAnswer(_Base):
    kind: Literal["panel_context"] = "panel_context"
    entries: list[PanelEntry] = Field(max_length=context.MAX_ENTRIES)


MODELS = (T1StripAnswer, QCAnswer, T2Answer, T3Answer, T4Answer, ConfirmAnswer,
          TransferAnswer, PanelContextAnswer)

Answer = Annotated[Union[MODELS], Field(discriminator="kind")]

#: {packet kind: its answer model}, from each model's own `kind`.
BY_KIND = {model.model_fields["kind"].default: model for model in MODELS}
KINDS = tuple(BY_KIND)


def schema_for(kind) -> dict:
    """The JSON schema of one kind's answer (what a packet tells the agent)."""
    schema = BY_KIND[kind].model_json_schema()
    defs = schema.get("$defs", {})
    return {"kind": kind, "required": schema.get("required", []),
            "properties": {k: _short(v, defs) for k, v in schema.get("properties", {}).items()}}


def _short(prop, defs, depth=0):
    """A property's schema, compact but complete: nested models are inlined
    (an agent cannot follow a `$ref`), with their own fields and enums."""
    if "$ref" in prop:
        target = defs.get(prop["$ref"].rsplit("/", 1)[-1], {})
        prop = {**target, **{k: v for k, v in prop.items() if k != "$ref"}}
    out = {k: prop[k] for k in ("type", "enum", "description", "const") if k in prop}
    if "anyOf" in prop:
        options = [_short(p, defs, depth + 1) for p in prop["anyOf"]
                   if p.get("type") != "null"]
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
