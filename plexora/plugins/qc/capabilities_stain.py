"""Low quality staining for whoever needs it outside a QC session.

Automatic gating reads a marker's stain verdict, or has the stain read when
none is stored, without the full QC workflow: the same picture, question and
answer as QC's own check (`server/stain.py`), every reading kept in the one
ledger (`server/stain_ledger.py`) so QC and gating reuse each other's. Gating
calls these by registry name, never by import (a QC build never pulls in
gating, and gating stays installable without QC).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from plexora.agent.errors import AgentError
from plexora.agent.schemas import AgentModel, ProjectInput

TAGS = ("qc", "stain", "staining", "low quality staining", "marker", "channel")


class StainGetInput(ProjectInput):
    channels: list[str] | None = Field(
        None, description="The marker channels to look up (default: every channel the stain "
                          "check reads).")


class StainPacketInput(ProjectInput):
    channel: str = Field(description="The marker channel whose stain picture is wanted.")
    picture: str | None = Field(None, description="A picture drawn before (its artifact id): "
                                "reused when still stored.")


class StainReading(AgentModel):
    verdict: Literal["real", "weak", "failed", "cannot_tell"]
    structure: Literal["yes", "no", "unclear"] = "unclear"
    expectation: str = Field("", max_length=300)
    observed: str = Field("", max_length=300)
    why: str = Field("", max_length=200)


class StainPutInput(ProjectInput):
    channel: str
    attempt: str = Field(description="The asker's attempt id: one session's readings of one "
                                     "channel, in order (`gating:<session id>`).")
    read: int = Field(ge=1, le=3, description="Which reading this is (1, 2 or 3).")
    answer: StainReading
    picture: str | None = None
    partner: str | None = None
    source: Literal["qc", "gating"] = "gating"


def _context(session, project):
    from plexora.plugins.qc.server import stain, stain_ledger

    names = [c.get("fullname") or c.get("name")
             for c in session.project(project).image.real_channels]
    nuclear = stain_ledger.nuclear_of(session, project)
    checked = stain.checked_channels(names, nuclear) if nuclear else []
    return names, nuclear, checked


def _partner(names, channel, nuclear):
    from plexora.plugins.qc.server import stain

    try:
        return stain.partner(names, channel, exclude=nuclear)
    except Exception:  # noqa: BLE001 -- a partner is a help, never required
        return None, None


def stain_get(call, inp):
    """Every stored stain verdict that can be reused now, per channel."""
    from plexora.plugins.qc.server import stain_ledger

    names, nuclear, checked = _context(call.session, inp.project)
    wanted = [c for c in (inp.channels or checked) if c in checked]
    partners = {c: _partner(names, c, nuclear)[0] for c in wanted}
    found = stain_ledger.lookup(call.session, inp.project, wanted, nuclear=nuclear,
                                partners=partners) if wanted else {}
    return {"project": inp.project, "available": bool(nuclear), "checked": checked,
            "channels": found,
            "note": None if nuclear else "no DNA channel is recognisable by name, so the "
                                         "stain pictures cannot be drawn"}


def stain_packet(call, inp):
    """One channel's stain picture (drawn and stored when needed) with the
    question and evidence its reader is shown. No model is asked."""
    from plexora.plugins.qc.server import stain

    names, nuclear, checked = _context(call.session, inp.project)
    if inp.channel not in checked:
        raise AgentError("invalid_input", f"{inp.channel!r} is not a marker channel the stain "
                         "check reads" + ("" if nuclear else " (no DNA channel is "
                                          "recognisable by name)"))
    partner, words = _partner(names, inp.channel, nuclear)
    parts = stain.packet_parts(call.session, inp.project, inp.channel, partner_name=partner,
                               partner_words=words, picture=inp.picture)
    parts.pop("png", None)
    return {**parts, "partner": partner, "partner_words": words, "reading": stain.READING}


def stain_put(call, inp):
    """Store one reading and settle the channel: {verdict, contested,
    readings, next_read}. `verdict` is None while another reading is due
    (`next_read`: give the same picture to a reader that never saw it)."""
    from plexora.plugins.qc.server import stain_ledger

    names, nuclear, checked = _context(call.session, inp.project)
    if inp.channel not in checked:
        raise AgentError("invalid_input", f"{inp.channel!r} is not a marker channel the stain "
                         "check reads")
    done = stain_ledger.readings(inp.project, inp.channel, inp.attempt)
    if any(int(r["read"]) == inp.read for r in done):
        raise AgentError("conflict", f"reading {inp.read} of {inp.channel!r} in "
                         f"{inp.attempt!r} is stored already")
    stain_ledger.append(inp.project, channel=inp.channel, attempt=inp.attempt, read=inp.read,
                        answer=inp.answer.model_dump(), picture=inp.picture,
                        partner=inp.partner,
                        keys=stain_ledger.keys(call.session, inp.project, inp.channel,
                                               partner=inp.partner, nuclear=nuclear),
                        source=inp.source)
    found = stain_ledger.settled(stain_ledger.readings(inp.project, inp.channel, inp.attempt))
    return {"project": inp.project, "channel": inp.channel, **found,
            "next_read": None if found["verdict"] is not None
            else len(found["readings"]) + 1}


def capabilities(free):
    return [
        free(name="qc.stain_get", tool_name="get_stain_verdicts",
             purpose="Low quality staining's stored verdicts that still hold for this image "
                     "(real, weak, failed or cannot_tell per marker channel, with every "
                     "reading and who asked for it): read by a QC session or by automatic "
                     "gating, on the same picture. A flag for review, never a removal.",
             permission="read", input_model=StainGetInput, handler=stain_get,
             egress="metadata", reads=("qc", "image"), tags=TAGS),
        free(name="qc.stain_packet", tool_name="get_stain_picture",
             purpose="One marker channel's stain-check picture (whole tissue, two fields, a "
                     "few cells, and co-expression with a partner from the marker graph), "
                     "drawn and stored when needed, with the question its reader answers. "
                     "No model is asked.",
             permission="read", input_model=StainPacketInput, handler=stain_packet,
             egress="rendered_pixels", reads=("qc", "image", "table"), tags=TAGS),
        free(name="qc.stain_put", tool_name="put_stain_reading",
             purpose="Store one reading of a stain-check picture and settle the channel's "
                     "verdict (a first failed is read again blind; best of three when two "
                     "differ). Kept where QC and automatic gating both reuse it.",
             permission="reversible_write", input_model=StainPutInput, handler=stain_put,
             writes=("qc",), persistent=True, reads=("qc", "image"), tags=TAGS),
    ]
