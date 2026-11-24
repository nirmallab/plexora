"""Low quality staining (QC's stain check) in automatic gating: evidence,
never a rule.

The stain check reads one picture per marker -- the whole tissue, two
fields, a few cells, and co-expression with a partner -- gate-free, and
says `real`, `weak`, `failed` or `cannot_tell`. Gating's own whole-image
check (`qc_confirm`) sees another picture (the cells above a candidate
gate) and errs on other markers. Benched on the owner's labels of 209
channels on seven images (docs/internal/bench/runs/2026-11-23-qc-aware):
the stain check caught 8 of 9 failed stains but called 5 real and 5 weak
stains failed; gating's check alone caught 3 to 4 of 8 on lsp35766_p193_2
and failed CD66b, CD11c, KLF2 and Rb, all real. Requiring both to say so
kept every true failure and removed the false ones but KLF2 twice in 24.

So (`decision` in this module's terms):

- a stain read `failed` sends the marker to gating's whole-image check,
  never to an accept without a look, and only both together close it
  `technically_failed`; read real by gating's check, the marker is gated at
  low confidence and listed;
- a stain read `weak`, or one a reader called failed that did not settle
  failed (`contested`), is gated with a look, capped (moderate, low) and
  listed;
- a stain that did not read `failed` is never closed `technically_failed`:
  gating's check saying so sends it to a person (`blocks_failure`).

Gating's readers are never told the stain verdict: the two checks are only
worth combining while neither has seen the other.

The verdict comes from QC by registry name (`qc.stain_get`), or -- one
image's session, none stored -- gating has the stain read itself with QC's
own picture and question (`qc.stain_packet`), its readings stored where QC
reuses them (`qc.stain_put`). No QC installed, or `stain_check: off`: today's
behaviour.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: `stain_check`: auto -- reuse a stored verdict, else (one image) read it;
#: reuse -- never read; off -- the stain check is not used.
from plexora.plugins.gating.server.autogate.schemas import (  # noqa: E402
    STAIN_CHECK_DEFAULT as DEFAULT, STAIN_CHECK_MODES as MODES)

#: The highest confidence a gate keeps under each stain category.
CAPS = {"weak": "moderate", "contested": "low", "failed": "low"}

#: What the review list and a closed marker's reason say of each category.
SAID = {"weak": "the stain check read the stain weak",
        "contested": "a stain reader called the stain failed, the others did not",
        "failed": "the stain check read the stain failed (two readers)",
        "real": "the stain check read the stain real",
        "cannot_tell": "the stain check could not tell"}


def _available(name) -> bool:
    from plexora.agent import registry

    return any(c.name == name for c in registry.all_capabilities())


def _invoke(call, name, args, suffix):
    """A QC capability's result, or None (QC not installed, or it refused)."""
    from plexora.agent import registry

    if not _available(name):
        return None
    try:
        out = registry.invoke(call.session, name, args, policy=call.policy, audit=call.audit,
                              link=call.link, operation_id=f"{call.operation_id}.{suffix}")
    except Exception:  # noqa: BLE001 -- the stain check is evidence, never required
        log.warning("%s failed", name, exc_info=True)
        return None
    if not out.get("ok"):
        log.info("%s refused: %s", name, (out.get("error") or {}).get("message"))
        return None
    return out.get("result")


def sample_words(biology) -> str | None:
    """The session's biology as the stain reader is shown it ('large
    intestine; colorectal cancer'), or None."""
    said = (biology or {}).get("said") or {}
    return "; ".join(str(said[k]) for k in ("tissue", "disease", "notes") if said.get(k)) or None


def fetch(call, project, options) -> dict | None:
    """The image's stored verdicts (`qc.stain_get`): {available, checked,
    channels}, or None when the stain check is off or QC is not installed."""
    if options["stain_check"] == "off":
        return None
    return _invoke(call, "qc.stain_get", {"project": project}, f"stain.{project}")


def prepare(call, project, marker, options, found) -> dict | None:
    """`unit["stain"]` when the marker is profiled: a stored verdict, a
    picture waiting for its reading (`auto`, when the pass may read), or
    None -- the stain check says nothing of this marker."""
    if not found or not found.get("available") or marker not in (found.get("checked") or ()):
        return None
    hit = (found.get("channels") or {}).get(marker)
    if hit:
        return {"verdict": hit["verdict"], "contested": bool(hit.get("contested")),
                "readings": hit.get("readings") or [], "source": hit.get("source"),
                "fresh": hit.get("fresh"), "picture": hit.get("picture"), "reused": True}
    if options["stain_check"] != "auto" or not options["_stain_reads"]:
        return None
    packet = _invoke(call, "qc.stain_packet",
                     {"project": project, "channel": marker,
                      **({"sample": options["_stain_sample"]}
                         if options.get("_stain_sample") else {})},
                     f"stain.{project}.{marker}")
    if not packet or not packet.get("picture"):
        return None
    return {"verdict": None, "contested": False, "readings": [], "source": "gating", "read": 1,
            "picture": packet["picture"], "partner": packet.get("partner"),
            "packet": {k: packet.get(k) for k in ("question", "evidence", "allowed",
                                                  "reading")}}


def due(unit) -> bool:
    """Whether the unit waits for a reading of its stain picture."""
    stain = unit.get("stain") or {}
    return stain.get("verdict") is None and bool(stain.get("packet"))


def category(unit) -> str | None:
    """real, weak, failed, contested, cannot_tell -- or None (not read)."""
    stain = unit.get("stain") or {}
    verdict = stain.get("verdict")
    if verdict is None:
        return None
    return "contested" if stain.get("contested") else verdict


def flagged(unit) -> bool:
    return category(unit) in CAPS


def blocks_failure(unit) -> bool:
    """A stain the stain check did not read `failed` is never closed
    `technically_failed` on gating's word alone."""
    found = category(unit)
    return found is not None and found != "failed"


def cap(unit) -> str | None:
    return CAPS.get(category(unit))


def said(unit) -> str | None:
    found = category(unit)
    if found is None:
        return None
    verdicts = [r.get("verdict") for r in (unit.get("stain") or {}).get("readings") or ()]
    return SAID[found] + (f" (readings: {' / '.join(verdicts)})" if len(verdicts) > 1 else "")


def defer_failure(engine, unit, close) -> bool:
    """Before a stain is closed `technically_failed` with no stain verdict
    (a dataset, whose bulk pass reads none): with `auto`, have the stain
    read first and make the close once it settles (`stain_then`,
    `transitions.apply_stain`), so the two checks are both heard. True when
    the close waits."""
    stain = unit.get("stain") or {}
    if engine.options["stain_check"] != "auto" or category(unit) is not None \
            or stain.get("lost") or unit.get("stain_then") is not None:
        return False
    found = (engine.record.get("stain") or {}).get(unit["project"]) or {}
    if found.get("status") != "available":
        return False
    sample = sample_words(engine.record.get("biology"))
    packet = _invoke(engine.call, "qc.stain_packet",
                     {"project": unit["project"], "channel": unit["marker"],
                      **({"sample": sample} if sample else {})},
                     f"stain.{unit['project']}.{unit['marker']}.before_failure")
    if not packet or not packet.get("picture"):
        return False
    unit["stain"] = {"verdict": None, "contested": False, "readings": [], "source": "gating",
                     "read": 1, "picture": packet["picture"], "partner": packet.get("partner"),
                     "packet": {k: packet.get(k) for k in ("question", "evidence", "allowed",
                                                           "reading")}}
    unit["stain_then"] = close
    unit["state"] = "awaiting_stain"
    engine.log(event="stain_before_failure", unit=f"{unit['project']}::{unit['marker']}")
    return True


def put(engine, unit, answer) -> bool:
    """Store one reading (`qc.stain_put`) and settle: True when the verdict
    stands, False while another reader is due. A reading QC cannot store
    leaves the stain unread (None): the marker goes on without it."""
    stain = unit["stain"]
    read = int(stain.get("read") or 1)
    reading = {k: getattr(answer, k, None) for k in ("verdict", "structure", "expectation",
                                                      "observed", "why")}
    result = _invoke(engine.call, "qc.stain_put", {
        "project": unit["project"], "channel": unit["marker"],
        "attempt": f"gating:{engine.id}", "read": read,
        "answer": {k: v for k, v in reading.items() if v is not None},
        "picture": stain.get("picture"), "partner": stain.get("partner"),
        "source": "gating"}, f"stain.{unit['marker']}.{read}")
    if result is None:
        stain.update(verdict=None, packet=None, lost=True)
        return True
    stain["readings"] = result.get("readings") or []
    if result.get("verdict") is None:
        stain["read"] = int(result.get("next_read") or read + 1)
        return False
    stain.update(verdict=result["verdict"], contested=bool(result.get("contested")))
    stain.pop("packet", None)
    return True
