"""What an agent may say back to a gating packet: typed, bounded, closed.

The agent never types a threshold, and never says which way a gate should
move: it answers one closed question per collage row about the cells around
the marked nucleus (`schemas.ROW_VERDICTS`), and code turns the words into a
direction (`verdicts`). Every field is a closed vocabulary here, so an answer
the engine cannot act on is refused at validation rather than half-applied.
One model per packet kind, discriminated by `kind`.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field

from plexora.agent.schemas import AgentModel, clipped
from plexora.ai import vocabulary
from plexora.plugins.gating.server.autogate import context, pixel_estimate, schemas

Artifact = Literal[schemas.ARTIFACTS]

_PARTNERS = (f"[{{marker: a marker of this panel, relation: {' | '.join(vocabulary.RELATIONS)}, "
             f"confidence: {' | '.join(vocabulary.CONFIDENCE)}}}]")
_CONFIDENCE = ("How sure the judgment is: " + ", ".join(f"`{w}`" for w in schemas.AI_CONFIDENCE)
               + ". Say `unsure` rather than guess.")
Confidence = Literal[tuple(schemas.AI_CONFIDENCE)]
RowVerdict = Literal[schemas.ROW_VERDICTS]
Pattern = Literal[schemas.PATTERNS]
_ROWS = ("One word per row about the cells around the marked nucleus (the four magenta "
         "ticks): `own_stain` (stain touching the nucleus with no other nucleus in it: a ring "
         "or halo around it, a fill inside it, cytoplasm hugging it), `neighbour_only` "
         "(stain in the tile, but around another nucleus or apart from this one), "
         "`unstained` (nothing touches the nucleus), `mixed` (both kinds, about evenly), "
         "`cannot_tell`. A row is what most of its cells are.")


class Request(AgentModel):
    kind: Literal["reference_channel", "bivariate"]
    marker: str | None = Field(None, description="The reference or partner marker.")
    reason: Annotated[str, clipped(200)] = Field("", max_length=200)


class AskUser(AgentModel):
    question: Annotated[str, clipped(300)] = Field(max_length=300)
    options: list[str] = Field(default_factory=list, max_length=4)
    why: Annotated[str, clipped(200)] = Field("", max_length=200)


class _Base(AgentModel):
    notes: Annotated[str, clipped(300)] = Field(
        "", max_length=300, description="One or two sentences, for the record (at most 300 "
                                        "characters; a longer note is cut).")
    artifact_flags: list[Artifact] = Field(default_factory=list, description="Anything "
                                           "technical that affects the cells shown.")
    request: Request | None = None
    ask_user: AskUser | None = Field(None, description="Only for what the data and the "
                                     "panel cannot settle: binary vs continuous, expected "
                                     "prevalence, which partner to trust.")


class T1StripAnswer(_Base):
    kind: Literal["t1_strip"] = "t1_strip"
    verdicts: dict[str, Literal["ok", "suspicious", "cannot_tell"]] = Field(
        description="Per marker row: do the cells at the gate look split correctly?")


class QCAnswer(_Base):
    kind: Literal["qc_confirm"] = "qc_confirm"
    verdict: Literal["real_signal", "technical_failure", "no_positive_population",
                     "cannot_tell"] = Field(
        description="real_signal: the stain is real and some cells carry it; "
                    "technical_failure: flat, saturated, background or artifact only; "
                    "no_positive_population: the stain worked and no cell in this image is "
                    "positive (the gate is put at the maximum).")


class T2Answer(_Base):
    kind: Literal["t2_confirm"] = "t2_confirm"
    rows: dict[Literal["below", "near", "above"], RowVerdict] = Field(
        description=_ROWS + " Rows: just below, at and just above the gate (`near` may be "
                            "empty).")
    pattern: Pattern = Field(
        description="What the stained cells look like, in the collage or the sheet's "
                    "fields; `none` only when no cell anywhere carries the stain.")
    within: str | None = Field(
        None, description="Only when the stain is real inside the positives of a subset "
                          "partner (one of `allowed.within`) and noise outside them: that "
                          "partner. The gate is then fitted among its positives and shown "
                          "again.")
    confidence: Confidence = Field(description=_CONFIDENCE)


class T3Answer(T2Answer):
    kind: Literal["t3_biological"] = "t3_biological"


class T4Answer(_Base):
    kind: Literal["t4_candidates"] = "t4_candidates"
    intervals: dict[str, RowVerdict] = Field(
        description="EVERY interval row (i1 nearest the current gate, i2, ...), about the "
                    "cells that change call there. " + _ROWS + " The gate is placed from "
                    "these words.")
    confidence: Confidence = Field(description=_CONFIDENCE)


class ConfirmAnswer(_Base):
    kind: Literal["regression_confirm"] = "regression_confirm"
    verdict: Literal["holds", "too_low", "too_high", "artifact", "cannot_tell"]


class TransferAnswer(_Base):
    kind: Literal["transfer_check"] = "transfer_check"
    per_image: dict[str, Literal["holds", "too_low", "too_high", "cannot_tell"]]


class ExpressionSetupAnswer(_Base):
    kind: Literal["expression_setup"] = "expression_setup"
    features_layer: str = Field(description="One of the packet's `allowed` values: `X` or "
                                            "`layer:<name>`.")
    features_log: bool = Field(description="Apply log1p as the values are read. Never for a "
                                           "matrix that is already log-transformed.")


class PixelSetupAnswer(_Base):
    kind: Literal["pixel_setup"] = "pixel_setup"
    basis: Literal[schemas.PIXEL_BASES] = Field(
        description="estimate_confirmed: the nuclei are the size the bar and ring imply; "
                    "adjusted: they are clearly not, and `microns_per_pixel` is your "
                    "correction; user_stated: the user gave the pixel size (it is then "
                    "written to the project).")
    microns_per_pixel: float | None = Field(
        None, ge=pixel_estimate.BOUNDS[0], le=pixel_estimate.BOUNDS[1],
        description="Needed for adjusted and user_stated; omit to keep the estimate.")


class PanelEntry(AgentModel):
    marker: str
    role: Literal[vocabulary.ROLES]
    compartment: Literal[vocabulary.COMPARTMENTS] | None = None
    lineage: Annotated[str | None, clipped(120)] = Field(None, max_length=120)
    binary: bool = True
    partners: list[dict] = Field(default_factory=list, max_length=context.MAX_PARTNERS,
                                 description=_PARTNERS)


class PanelContextAnswer(_Base):
    kind: Literal["panel_context"] = "panel_context"
    entries: list[PanelEntry] = Field(max_length=context.MAX_ENTRIES)


MODELS = (T1StripAnswer, QCAnswer, T2Answer, T3Answer, T4Answer, ConfirmAnswer,
          TransferAnswer, PanelContextAnswer, ExpressionSetupAnswer, PixelSetupAnswer)

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
