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

Artifact = Literal["image_quality", "segmentation", "saturation", "bleedthrough",
                   "autofluorescence", "tissue_fold", "uneven_staining", "edge"]
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
    confidence: float = Field(ge=0, le=1, description="Ordinal: 0.9 sure, 0.6 fairly sure, "
                                                      "0.3 guessing.")


class T3Answer(T2Answer):
    kind: Literal["t3_biological"] = "t3_biological"
    coexpression_consistent: bool | None = None
    exclusion_consistent: bool | None = None
    background_pattern: Literal["none", "diffuse", "edge", "nuclear_bleed",
                                "segmentation_boundary", "unknown"] = "none"


class T4Answer(_Base):
    kind: Literal["t4_candidates"] = "t4_candidates"
    chosen_candidate: str = Field(description="A candidate id (c1, c2, ...), `keep` (the "
                                  "current gate), or `none_separates`.")
    intervals: dict[str, Literal["mostly_positive", "mostly_negative", "mixed"]] = Field(
        default_factory=dict, description="Per interval row (i1, i2, ...): are the cells "
                                          "that flip there really positive?")
    confidence: float = Field(ge=0, le=1)


class ConfirmAnswer(_Base):
    kind: Literal["regression_confirm"] = "regression_confirm"
    verdict: Literal["holds", "too_low", "too_high", "artifact", "cannot_tell"]


class TransferAnswer(_Base):
    kind: Literal["transfer_check"] = "transfer_check"
    per_image: dict[str, Literal["holds", "too_low", "too_high", "cannot_tell"]]


class PanelEntry(AgentModel):
    marker: str
    role: Literal["context", "lineage_reliable", "lineage_other", "tumour_stromal", "state",
                  "signalling"]
    compartment: Literal["nuclear", "cytoplasmic", "membrane", "nuclear_cytoplasmic",
                         "extracellular"] | None = None
    lineage: str | None = Field(None, max_length=120)
    binary: bool = True
    partners: list[dict] = Field(default_factory=list, max_length=6)


class PanelContextAnswer(_Base):
    kind: Literal["panel_context"] = "panel_context"
    entries: list[PanelEntry] = Field(max_length=60)


Answer = Annotated[Union[T1StripAnswer, QCAnswer, T2Answer, T3Answer, T4Answer,
                         ConfirmAnswer, TransferAnswer, PanelContextAnswer],
                   Field(discriminator="kind")]

KINDS = ("t1_strip", "qc_confirm", "t2_confirm", "t3_biological", "t4_candidates",
         "regression_confirm", "transfer_check", "panel_context")


def schema_for(kind) -> dict:
    """The JSON schema of one kind's answer (what a packet tells the agent)."""
    model = {"t1_strip": T1StripAnswer, "qc_confirm": QCAnswer, "t2_confirm": T2Answer,
             "t3_biological": T3Answer, "t4_candidates": T4Answer,
             "regression_confirm": ConfirmAnswer, "transfer_check": TransferAnswer,
             "panel_context": PanelContextAnswer}[kind]
    schema = model.model_json_schema()
    return {"kind": kind, "required": schema.get("required", []),
            "properties": {k: _short(v) for k, v in schema.get("properties", {}).items()}}


def _short(prop):
    out = {k: prop[k] for k in ("type", "enum", "description", "const") if k in prop}
    if "anyOf" in prop:
        out["anyOf"] = [{k: p[k] for k in ("type", "enum", "const") if k in p}
                        for p in prop["anyOf"]]
    if "$ref" in prop:
        out["ref"] = prop["$ref"].rsplit("/", 1)[-1]
    if "additionalProperties" in prop and isinstance(prop["additionalProperties"], dict):
        out["values"] = {k: prop["additionalProperties"][k]
                         for k in ("enum", "type") if k in prop["additionalProperties"]}
    return out
