"""One decision packet per kind: the question, the numbers, at most two images.

Every packet is stored self-contained, and it carries numbers only in JSON:
images show pixels and cell positions, never a value the agent would have to
read off a picture. Each builder returns `(packet, images)` with
`images = [(bytes, format, (width, height))]` and the packet's `_image_meta`
aligned with them, or None when it closed the unit instead.

What is SENT is shorter (`as_sent`, `SessionOptions.evidence = delta`): an
evidence value this reader was already sent goes as `{"as_in": packet_id}`,
and a context-sheet row it was already shown with the same inputs is left
out (`sheets = trim`). A reader is a conversation: `gating_session_status`
without the current `known_guide` starts a new one (`new_reader`), so a
pointer only ever names a packet the reader holds. Stored packets and memo
keys never change, so neither do the gates.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import get_args

from plexora.agent.errors import AgentError
from plexora.plugins.gating.server.autogate import answers, schemas, tableops, views
from plexora.plugins.gating.server.autogate.engine import ENGINE, seeded_order

#: At most this many images in one packet (`Engine.issue` refuses more).
MAX_IMAGES = 2

#: How to read the context sheet (`sheet.py`), the second picture of a look.
SCALES_READING = (
    "the context sheet shows the marker at three scales. Top: three fields of the tissue "
    "(borderline, clearly positive, clearly negative; `sheet.field` says their size, sized "
    "from the image's own pixel size; blue nuclei, yellow marker, magenta outlines = cells "
    "the gate calls positive). Bottom: the whole image's stain, the whole image's positive "
    "cells, and the marker against one gated partner (x = this marker, y = the partner, "
    "both gates drawn, as on a flow plot; `sheet.plot.why` says why that partner) or its "
    "distribution. Read coarse to fine: is the pattern across the tissue right for this "
    "marker, do the fields show the architecture it should (glands, vessels, lymphoid "
    "aggregates), then are the cells at the gate called correctly. A marker with an obvious "
    "tissue pattern is judged at field scale first")

#: What the partner plot should look like, per relation (`bivariate.RELATIONS`).
BIVARIATE_READING = {
    "subset": "a subset marker: its positives (right of the vertical gate) should also be "
              "partner-positive (above the horizontal gate); cells right and below are "
              "suspect",
    "coexpressed": "a co-expressed pair: positives of one should be positives of the other, "
                   "on the diagonal; the off-diagonal quadrants should be thin",
    "exclusive": "an exclusive pair: the upper-right quadrant should be nearly empty; a "
                 "cloud there means one gate is too low (or spill-over between neighbours)",
    "independent": "no relation is expected; no quadrant should be empty by rule",
}

#: Every fixed text a packet relies on, sent once per session (with the
#: start and the status) instead of in every packet; a packet names the
#: entries it uses in `guide` and carries only what is specific to it.
READING_GUIDE = {
    "collage_t2": ("the collage, panels per cell: top-left nuclear, top-right marker (white), "
                   "bottom-left merge (blue nuclei, yellow marker, magenta = this cell's "
                   "outline), bottom-right the marker on a log scale whose mid-grey IS the "
                   "gate; caption = value and call (+/-). Judge the rows nearest the gate "
                   "most"),
    "collage_t3": ("the collage's bottom-right panel = the reference channel (white); a subset "
                   "or co-expressed marker's positives should be reference-bright, an "
                   "exclusive one's should be reference-dark; the fields add the reference "
                   "in cyan"),
    "collage_flips": ("rows run from the current gate outward: row i1 is the cells between "
                      "the current gate and candidate c1, row i2 between c1 and c2, and so "
                      "on. Judge every row on its own"),
    "candidates": ("candidates are points of the marker's lattice, fixed before any look: the "
                   "Auto gate (`gmm`), the other estimators (`kde`, `otsu`, `gmm2`), each "
                   "partner's negative control (`ctrl:<partner>`: the marker's p99 among cells "
                   "the partner says are negative for it), each within-partner fit "
                   "(`within:<partner>`), the scored candidates (`score`, `bio:<partner>`, "
                   "`ceiling`, `onset`; see `scored`), fixed steps (`up:`/`down:`) and the "
                   "band's edges (`edge:`). A look never runs past a control, a within-partner "
                   "fit or a partner curve (`bio:`). The sheet's plot draws every candidate as "
                   "a dashed line at its id"),
    "scored": ("`scored` is how the starting gate was chosen, before any look "
               "(`autogate/scoring.py`). Candidates: `gmm` (the Auto mixture gate), `onset` "
               "(where background stops explaining the cells -- the lowest defensible gate), "
               "`ceiling` (the background peak plus six core sds: past it no background cell "
               "should be), `bio:<partner>` (where the share of partner-positive cells along "
               "this marker has done most of its rise: below it the cells are background or "
               "another lineage's spill, above it they are as partner-positive as real "
               "positives get; `robust: false` when it only follows the partner's own gate), "
               "`anti:<partner>` (where an exclusive partner's share has finished falling) "
               "and `sens:<partner>` (this marker's 5th percentile among a subset partner's "
               "positives: a gate above it drops real positives), and `score` (the "
               "proposal). Each carries `components` in [0, 1]: `distribution` (how surely "
               "cells there are above background), `coexpression`, `anti`, `retained` (of "
               "the cells in excess over background, the share kept), `region` (agreement "
               "with the same estimate in each tissue tile), `morphology` (cells just above "
               "are not larger or smaller than those just below). The numbers chose where "
               "to look; the cells decide. Judge the collage, and say which way the gate is "
               "wrong if it is -- the candidates are where it can go"),
    "biology": ("`biology` is the sample's tissue and disease and who said so (`source`: "
                "user, metadata, or inferred from the panel -- inferred is the weakest). "
                "`expect` says what this marker and its references also mark here, "
                "`structures` the tissue structures the marker belongs to, `ambiguity` when "
                "more than one could explain its positives. Read the pictures in that frame: "
                "a pattern unusual elsewhere may be expected here (keratin along the "
                "epidermis, SOX9 in hair follicles). It is a prior: it never moves the gate "
                "by itself, and never makes a population exist. Answer `biology`: consistent, "
                "conflicts, ambiguous (two structures could explain what you see) or "
                "not_judged"),
    "hierarchy": ("`hierarchy` is where the marker sits in the panel's tree "
                  "(`autogate/hierarchy.py`): its `stage` (broad, lineage, subtype, state, "
                  "unplaced), the markers it is a subset of (`parents`) and the ones that are "
                  "subsets of it (`children`). `used` are the gated markers its evidence "
                  "leans on, each with the `grade` its own gate earned (high, moderate, low) "
                  "and a `weight` (how relevant the relation is times how reliable the gate "
                  "is; `derived` when the tree implies the relation rather than states it). "
                  "`avoided` are related markers that were NOT used and why -- a failed gate "
                  "or one under review never is. `stood_on_failed` names a reference that "
                  "failed after this marker leaned on it: weigh that evidence less. The "
                  "pictures show only the marker and its chosen references, so judge the "
                  "relation from them, not from the rest of the panel"),
    "continuous": ("a continuously expressed marker (no valley between negative and "
                   "positive cells): there is still background, and a point where real "
                   "expression rises out of it. Judge THAT point: do the cells just above the "
                   "gate carry real, correctly localised stain that the cells just below do "
                   "not? `too_low` when the cells above still look like background, "
                   "`too_high` when cells below already stain convincingly. `not_binary` is "
                   "for a channel where no such point exists even here: the onset is then "
                   "written and tagged for manual review"),
    "sheet": SCALES_READING,
    "sheet_candidates": ("in a look at candidates the sheet's top row is one tissue field per "
                         "candidate: the cells between that candidate and the threshold "
                         "before it, magenta = positive at that candidate. A candidate is "
                         "right when the cells it drops (or keeps) look like the negatives "
                         "(or positives) around them in the tissue"),
    "partners": ("`partners` (first in the evidence) are the whole-image numbers against each "
                 "partner gated so far, with the negative control each gives (the marker's "
                 "p99 among cells the partner says are negative for it). The sheet plots the "
                 "partner that contradicts this gate most; a request naming a partner plots "
                 "that one"),
    "within_partner": ("a subset marker whose stain is real inside a partner's positives and "
                       "noise outside them (another lineage's spill, an off-target stain) is "
                       "answered `within_partner`, with `within` = that partner (a `subset` "
                       "partner of `partners` gated at moderate or better): the gate is "
                       "refitted among the partner's positives and shown again. The gate "
                       "written is the plain gate at that threshold, with the condition "
                       "recorded beside it; confidence is at most moderate"),
    "no_positives": "`no_positives` when no cell anywhere is really positive (checked on the "
                    "whole image next)",
    "requests": ("`request` asks for the evidence that would settle a look: `bivariate` naming "
                 "a partner the sheet did not plot, or `reference_channel` for a partner's "
                 "channel beside the cells. It is served when that partner is gated"),
    "as_in": ("a value given as {\"as_in\": \"pk_nnnn\"} is unchanged since that packet, "
              "which you read earlier in this conversation: read it from there. A context "
              "sheet whose `sheet.rows` lacks a row leaves out one you were shown with the "
              "same gate and fields; with no image at all, the sheet is the one in `as_in`. "
              "If you do not hold that packet, call gating_next(session_id, rerender=true) "
              "for this one in full"),
    "pixel_setup": ("the snapshots show nuclei (blue) and cell outlines at three cell "
                    "densities, with a scale bar and a ten-micron ring (yellow) drawn at the "
                    "estimate. Most nuclei are five to ten microns across, a lymphocyte's "
                    "about six; a ring clearly smaller than a typical nucleus means the "
                    "estimate is too small (microns per pixel should go up), clearly larger "
                    "that it is too large. The answer sizes this session's pictures only, "
                    "unless the user stated the value"),
    **{f"plot:{relation}": text for relation, text in BIVARIATE_READING.items()},
}


#: [cal] evidence numbers are sent to this many significant figures: every
#: number a packet carries is a count, a share or a gate an agent judges, never
#: one it types back (answers name candidates by id).
SIGNIFICANT = 4


def reading_guide() -> dict:
    """The session's reading guide (`gating_session_start`, `_status`): the
    fixed texts, the compartment readings and every answer schema, in a fixed
    order -- the same bytes for every session of a build, so an agent's
    client caches it once (`guide_version` says when it changed)."""
    guide = dict(READING_GUIDE)
    for name, policy in sorted(schemas.COMPARTMENT_POLICY.items()):
        guide[f"compartment:{name}"] = policy["reading"]
    full = {kind: answers.schema_for(kind) for kind in answers.KINDS}
    # Fields every answer takes (notes, flags, request, biology, ask_user) are
    # spelled once: repeated in each of the eleven schemas they were half
    # the guide (~20k characters on the live lsp11385 run).
    common = {name: prop for name, prop in full[answers.KINDS[0]]["properties"].items()
              if all(s["properties"].get(name) == prop for s in full.values())}
    guide["answer_common"] = common
    guide["answer_schemas"] = {
        kind: {**schema, "properties": {k: v for k, v in schema["properties"].items()
                                        if k not in common},
               "common": "reading_guide.answer_common"}
        for kind, schema in full.items()}
    guide["answer_with"] = (f"{_tool_name('gating.answer')} {{session_id, packet_id, answer: "
                            "{kind, ...}}}; `answer_schemas[kind]` lists the fields, plus "
                            "the optional `answer_common` fields every kind takes")
    return guide


def guide_version() -> str:
    """A short hash of `reading_guide()`: an agent that holds this version
    passes it back (`known_guide`) and is not sent the guide again."""
    import hashlib
    import json

    blob = json.dumps(reading_guide(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _tool_name(capability):
    from plexora.agent import registry

    return registry.tool_name_of(capability)


def _round(value):
    if isinstance(value, bool) or not isinstance(value, float):
        return value
    if value == 0 or value != value:
        return value
    return float(f"{value:.{SIGNIFICANT}g}")


#: Gate values keep their precision: the viewer's mirror sets the slider from
#: them, and there are only a few in any packet.
EXACT_KEYS = ("low", "high", "current", "final", "gmm", "partner_gate", "plain_low")


def _lean_value(value, exact=False):
    """Numbers to `SIGNIFICANT` figures (gate values exact); None, empty
    lists and empty dicts dropped (an absent key says the same, for fewer
    tokens)."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            item = _lean_value(item, exact=key in EXACT_KEYS)
            if item is None or item == [] or item == {}:
                continue
            out[key] = item
        return out
    if isinstance(value, list):
        return [_lean_value(v, exact) for v in value]
    return value if exact else _round(value)


def lean(packet, options) -> dict:
    """A packet as sent: compact numbers, no empty fields, and -- when the
    session reads its guide once -- the answer schema by reference. What is
    stored is what is sent, so a packet fetched again is the same bytes."""
    packet["evidence"] = _lean_value(packet.get("evidence") or {})
    if options["reading"] == "once":
        packet["answer_schema"] = {"see": f"reading_guide.answer_schemas.{packet['kind']}"}
        packet.pop("answer_with", None)
    progress = packet.get("progress") or {}
    if progress:
        packet["progress"] = {k: progress.get(k) for k in ("units_done", "units_total")}
    budget = packet.get("budget") or {}
    if budget.get("this_packet"):
        packet["budget"] = {"this_packet": budget["this_packet"]}
    packet["images"] = [{k: v for k, v in image.items() if k != "estimated_vision_tokens"}
                        for image in packet.get("images") or []]
    return packet


# -- what is sent: each brief once per reader ------------------------------------------

#: [cal] an evidence value at least this long (compact JSON) that the reader
#: was already sent goes as `{"as_in": packet_id}`; a shorter one costs less
#: than the pointer saves.
AS_IN_MIN_CHARS = 60


def _fingerprint(value):
    """(sha1 prefix, length) of a value's compact JSON."""
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12], len(blob)


def briefed(record) -> dict:
    """{epoch, seen: {"<path>:<fingerprint>": first packet_id}}: what the
    session's current reader was sent."""
    return record.setdefault("briefed", {"epoch": 0, "seen": {}})


def new_reader(record) -> int:
    """A new reader (a fresh conversation): nothing it was sent before holds,
    so nothing may be pointed at. Returns the new epoch."""
    epoch = int((record.get("briefed") or {}).get("epoch", 0)) + 1
    record["briefed"] = {"epoch": epoch, "seen": {}}
    return epoch


def _pointer(seen, path, value, packet_id):
    fp, size = _fingerprint(value)
    if size < AS_IN_MIN_CHARS:
        return None
    key = f"{path}:{fp}"
    first = seen.get(key)
    if first is None or first == packet_id:
        seen[key] = packet_id
        return None
    return {"as_in": first}


def resolve_as_in(packet, held) -> dict:
    """A scripted reader's view (the bench, the tests): `packet` with every
    `{as_in: packet_id}` replaced from `held` ({packet_id: packet already
    resolved}), and recorded there. A real agent reads them from its own
    conversation."""
    evidence = packet.get("evidence")

    def lookup(pointer, key, sub=None):
        earlier = (held[pointer["as_in"]].get("evidence") or {}).get(key)
        return copy.deepcopy(earlier if sub is None else (earlier or {}).get(sub))

    filled = []
    if isinstance(evidence, dict):
        for key, value in list(evidence.items()):
            if isinstance(value, dict) and "as_in" in value:
                evidence[key] = lookup(value, key)
                filled.append(key)
            elif isinstance(value, dict):
                for sub, item in list(value.items()):
                    if isinstance(item, dict) and set(item) == {"as_in"}:
                        value[sub] = lookup(item, key, sub)
                        filled.append(f"{key}.{sub}")
    if filled:
        packet["_as_in"] = filled
    if packet.get("packet_id"):
        held[packet["packet_id"]] = packet
    return packet


def sends_delta(options) -> bool:
    """Whether a session sends each brief once (`evidence: delta`); a session
    that repeats its reading in every packet sends every packet whole."""
    return options["evidence"] == "delta" and options["reading"] != "every_packet"


def as_sent(packet, record, options, *, full=False) -> dict:
    """The packet as the agent reads it, from the stored one (left as it is).

    `evidence: delta` (the default): evidence values -- each key, and each
    key of a dict-valued one -- already sent to this reader go as
    `{"as_in": packet_id}` (`full` sends them all: a packet served again);
    the question loses its biology prefix (in `evidence.biology`), candidates
    their score `components`, the whole-image checks their passing rows; the
    budget and the viewer's narration stay with the status tool and the
    viewer. `evidence: full` sends the stored packet. `options` are the
    session's, completed (`Engine.options`)."""
    seen = briefed(record)["seen"]
    packet_id = packet.get("packet_id")
    for row, fp in (packet.get("_sheet_rows") or {}).items():
        seen.setdefault(f"sheet.{row}:{fp}", packet_id)
    out = {k: v for k, v in packet.items() if not k.startswith("_")}
    if not sends_delta(options):
        return out
    out = copy.deepcopy(out)
    out.pop("budget", None)
    out.pop("narration", None)
    evidence = out.get("evidence") or {}
    contexts = (evidence.get("biology") or {}).get("contexts")
    question = out.get("question")
    if contexts and isinstance(question, str) and \
            question.startswith(f"[{', '.join(contexts)};"):
        out["question"] = question.split("] ", 1)[-1]
    for cand in evidence.get("candidates") or []:
        if isinstance(cand, dict):
            cand.pop("components", None)
    checks = evidence.get("checks")
    if isinstance(checks, list):
        kept = [c for c in checks if not (isinstance(c, dict) and c.get("ok") is True)]
        evidence["checks"] = kept
        if len(kept) < len(checks):
            evidence["checks_passed"] = len(checks) - len(kept)
    if full:
        return out
    for key, value in list(evidence.items()):
        if isinstance(value, dict) and "as_in" not in value:
            for sub, item in list(value.items()):
                pointer = _pointer(seen, f"{key}.{sub}", item, packet_id)
                if pointer:
                    value[sub] = pointer
        else:
            pointer = _pointer(seen, key, value, packet_id)
            if pointer:
                evidence[key] = pointer
    return out


def _reading(engine, keys, specific=()):
    """A packet's reading: the entries of the guide it relies on, by key
    (with the marker's compartment and the plotted relation among them),
    plus what is specific to it -- or every text in full, when the session
    asked for `reading="every_packet"`."""
    guide = reading_guide()
    specific = [s.strip("; ") for s in specific if s]
    keys = [k for k in dict.fromkeys(keys) if k in guide]
    if engine.options["reading"] == "every_packet":
        return {"how_to_read": "; ".join([guide[k] for k in keys] + specific)}
    out = {"guide": keys}
    if specific:
        out["how_to_read"] = "; ".join(specific)
    return out


def profile_digest(summary) -> dict:
    """What a look needs of a marker's distribution; the rest is a profile
    call away (`gating_profile`)."""
    summary = summary or {}
    return {k: summary.get(k) for k in ("class", "separation_d", "positive_fraction",
                                        "n_positive", "n_cells", "signal_to_background")
            if summary.get(k) is not None}


def _flags(unit):
    """The flags that route a marker (hard ones); soft flags only shaped the
    confidence and are in the profile."""
    return schemas.hard(unit.get("flags"))


def _allowed(model, field):
    """The values a packet offers for an answer field: its Literal's."""
    annotation = model.model_fields[field].annotation
    return list(get_args(annotation))


def _fmt(engine):
    return engine.options["image_format"]


def _seed(engine):
    return int(engine.options["seed"])


def _image(rendered, role, caption):
    return ((rendered["image"], rendered["format"], tuple(rendered["manifest"]["size"])),
            {"role": role, "caption": caption,
             "artifact_id": (rendered.get("artifact") or {}).get("id")})


def _context_brief(unit):
    ctx = unit.get("context") or {}
    return {k: ctx.get(k) for k in ("canonical", "role", "compartment", "lineage", "binary",
                                    "caveats", "source") if ctx.get(k) is not None}


def _partner_numbers(engine, unit, ds, low, *, full=False):
    """Bivariate numbers against every partner gated so far -- by this run, or
    by the user in a gate the run kept (`Engine.references_ready`) -- with the
    FACS negative control each one gives (`bivariate.negative_control`)."""
    out = []
    for ref in engine.references_ready(unit):
        try:
            result = tableops.local_or_node(ds, "gating.autogate.bivariate", {
                "a": unit["marker"], "gate_a": low, "b": ref["marker"],
                "gate_b": ref["gate"], "relation": ref["relation"], "with_grid": False})
        except Exception:
            continue
        entry = {"partner": ref["marker"], "relation": ref["relation"],
                 "partner_confidence": ref.get("confidence"),
                 "quadrants": result["quadrants"],
                 "frac_marker_in_partner": result["frac_a_in_b"],
                 "orphan_fraction": result["orphan_fraction"],
                 "double_positive_fraction": result["double_positive_fraction"],
                 "adjacent_orphan_share": result["adjacent_orphan_share"],
                 "contradiction": result["contradiction"]}
        if ref["relation"] == "subset":
            # One minus the orphan fraction: the same number twice.
            entry.pop("frac_marker_in_partner")
        try:
            control = tableops.local_or_node(ds, "gating.autogate.control", {
                "a": unit["marker"], "gate_a": low, "b": ref["marker"],
                "gate_b": ref["gate"], "relation": ref["relation"]}).get("control")
        except Exception:
            control = None
        if control:
            entry["control"] = control if full else {k: control.get(k) for k in (
                "population", "n", "p99", "fraction_above_current")}
        out.append(entry)
    return out


def within_eligible(partners) -> list:
    """The partners a `within_partner` answer may name: `subset` partners
    gated at moderate confidence or better."""
    return [p["partner"] for p in partners or ()
            if p.get("relation") == "subset"
            and p.get("partner_confidence") in ("high", "moderate")]


def _compartment_key(unit):
    """The guide entry for the marker's compartment
    (`schemas.COMPARTMENT_POLICY`), or None."""
    compartment = (unit.get("context") or {}).get("compartment")
    return f"compartment:{compartment}" if compartment in schemas.COMPARTMENT_POLICY \
        else None


def _requested_partner(unit):
    """The partner the last request named (a `bivariate` plot or a
    `reference_channel` beside the cells: either way, the one the agent
    wants to see), if any."""
    for request in reversed(unit.get("requests") or []):
        if request.get("marker"):
            return request["marker"]
    return None


def plot_partner(unit, refs, numbers=None):
    """The partner the sheet plots, with why: the condition's partner in a
    conditional look; else the one a `bivariate` request named; else the one
    whose numbers contradict this gate most (the informative plot), ties by
    the vocabulary's order; else the first gated. None without references."""
    if not refs:
        return None
    by_marker = {r["marker"]: r for r in refs}
    condition = unit.get("condition")
    if condition and condition.get("within") in by_marker:
        return {**by_marker[condition["within"]], "why": "the partner the gate is conditional on"}
    wanted = _requested_partner(unit)
    if wanted in by_marker:
        return {**by_marker[wanted], "why": "named by a request"}
    scored = [(n.get("contradiction") or 0.0, n["partner"]) for n in numbers or ()
              if n["partner"] in by_marker]
    if scored:
        best = max(value for value, _m in scored)
        if best > 0:
            marker = next(m for value, m in scored if value == best)
            return {**by_marker[marker],
                    "why": f"the partner that contradicts this gate most "
                           f"(contradiction {best:.2f})"}
    if numbers is None:
        return {**refs[0], "why": "the first gated partner (this check compares none)"}
    return {**refs[0], "why": "the first gated partner; none contradicts this gate"}


def _sheet(engine, unit, ds, channel, low, *, references=(), candidates=None,
           candidate_fields=None, title=None, numbers=None, rows=None):
    """The context sheet for a unit (the second picture of a look, the only
    one of a check); its artifact joins the unit's. With `sheets: trim`, a
    row this reader was already shown with the same inputs is left out
    (`_sheet_rows`); with none left, no sheet is drawn (`skipped`)."""
    from plexora.plugins.gating.server.autogate import sheet

    refs = engine.references_ready(unit)
    partner = plot_partner(unit, refs, numbers)
    condition = unit.get("condition")
    within = ({"marker": condition["within"], "gate": condition["partner_gate"]}
              if condition else None)
    pixel = engine.pixel_for(ds.name)
    inputs = {"project": ds.name, "marker": unit["marker"], "channel": channel,
              "low": float(low), "high": unit.get("high"), "within": within}
    fps = {"fields": _fingerprint({**inputs, "references": list(references),
                                   "chain": candidate_fields, "seed": _seed(engine),
                                   "field_um": engine.field_um(),
                                   "pixel": (pixel or {}).get("value")})[0],
           "slide": _fingerprint({**inputs, "partner": {k: (partner or {}).get(k) for k in
                                                        ("marker", "gate", "relation")}})[0]}
    drawn, left_out = list(rows or sheet.ROWS), {}
    if engine.options["sheets"] == "trim":
        seen = briefed(engine.record)["seen"]
        for row in list(drawn):
            first = seen.get(f"sheet.{row}:{fps[row]}")
            if first:
                drawn.remove(row)
                left_out[row] = first
    if not drawn:
        return {"skipped": True, "left_out": left_out, "manifest": {},
                "epoch": briefed(engine.record)["epoch"]}
    rendered = sheet.render_context_sheet(
        engine.call.session, ds, marker=unit["marker"], channel=channel, low=low,
        high=unit.get("high"), references=references, partner=partner,
        candidates=candidates, candidate_fields=candidate_fields, seed=_seed(engine),
        fmt=_fmt(engine), field_um=engine.field_um(), pixel=pixel,
        positives_within=within, rows=tuple(drawn),
        title=title or f"{unit['marker']} · gate {_compact(low)} · three scales")
    rendered["row_fps"] = {row: fps[row] for row in drawn}
    rendered["left_out"] = left_out
    rendered["epoch"] = briefed(engine.record)["epoch"]
    if rendered.get("artifact"):
        unit.setdefault("artifacts", []).append(rendered["artifact"]["id"])
    return rendered


def _compact(value):
    from plexora.agent.evidence import collage

    return collage.compact_number(value)


def _sheet_keys(rendered, unit):
    """The guide entries this sheet needs: the marker's compartment and the
    plotted partner's relation."""
    plot = rendered["manifest"].get("plot") or {}
    keys = [_compartment_key(unit)]
    if plot.get("kind") == "density" and plot.get("relation") in BIVARIATE_READING:
        keys.append(f"plot:{plot['relation']}")
    return [k for k in keys if k]


def _sheet_evidence(rendered):
    left_out = rendered.get("left_out") or {}
    if rendered.get("skipped"):
        first = left_out.get("slide") or left_out.get("fields")
        return {"fields": {"as_in": left_out.get("fields", first)},
                "sheet": {"as_in": first, "rows": []}}
    manifest = rendered["manifest"]
    fields = [{k: f[k] for k in ("field_id", "class", "cells", "positives", "candidate", "low")
               if k in f} for f in manifest.get("fields") or []]
    plot = {k: v for k, v in (manifest.get("plot") or {}).items()
            if k not in ("quadrants", "contradiction")}     # both are in `partners`
    field = manifest.get("field") or {}
    out = {"fields": fields,
           "sheet": {"field": {k: field.get(k) for k in ("label", "um_per_px")},
                     "overview": manifest.get("overview"), "plot": plot,
                     "classes_without_field": manifest.get("classes_without_field")}}
    if left_out:
        out["sheet"]["rows"] = list(manifest.get("rows") or ())
        if "fields" in left_out:
            out["fields"] = {"as_in": left_out["fields"]}
        if "slide" in left_out:
            out["sheet"].update(overview={"as_in": left_out["slide"]},
                                plot={"as_in": left_out["slide"]})
    return out


def _sample(engine, ds, unit, low):
    condition = unit.get("condition")
    payload = {"marker": unit["marker"], "low": low, "high": unit.get("high"),
               "seed": _seed(engine)}
    if condition:
        payload["within"] = {"marker": condition["within"], "gate": condition["partner_gate"]}
    return tableops.local_or_node(ds, "gating.autogate.sample", payload)


def _unit_channel(engine, unit):
    ds = engine.call.session.data(unit["project"])
    channel = views.image_channel(ds, unit["marker"])
    if channel is None:
        raise AgentError("precondition_missing", f"{unit['marker']!r} has no image channel")
    return ds, channel


# -- T2 -----------------------------------------------------------------------------


def _images(packet, pairs):
    images = []
    for rendered, role, caption in pairs:
        if rendered.get("left_out"):
            # Which reader the rows were left out for: served again to
            # another, the packet is drawn whole (`next_packet`).
            packet["_sheet_left_out"] = {"epoch": rendered.get("epoch"),
                                         "rows": rendered["left_out"]}
        if rendered.get("skipped"):
            continue          # every row was shown to this reader already
        if rendered.get("row_fps"):
            # Registered as seen only when the packet is sent (`as_sent`).
            packet["_sheet_rows"] = rendered["row_fps"]
        image, meta = _image(rendered, role, caption)
        images.append(image)
        packet["_image_meta"].append(meta)
    return images


def _scored_brief(unit, *, top=8):
    """The unit's scored candidates (`Engine.propose`), as a look shows them:
    where the start came from and the candidates by threshold, with scores."""
    scored = unit.get("scoring")
    if not scored or not scored.get("candidates"):
        return None
    start = unit.get("start") or {}
    cands = sorted(scored["candidates"], key=lambda c: -(c.get("score") or 0))[:top]
    return {"start": start.get("source"), "start_low": start.get("low"),
            "basis": scored.get("basis"), "leading": scored.get("leading"),
            "gmm": unit.get("gmm"), "continuous": bool(unit.get("continuous")),
            "background": scored.get("background"), "region": scored.get("region"),
            "notes": scored.get("notes") or [],
            "candidates": sorted(cands, key=lambda c: c["low"])}


def _hierarchy_brief(unit):
    """The unit's place in the panel's tree and the evidence it leans on
    (`Engine.plan_evidence`), as a look shows them."""
    plan = unit.get("evidence_plan")
    if not plan:
        return None
    out = {k: plan.get(k) for k in ("stage", "placed_under", "parents", "children", "used",
                                    "avoided") if plan.get(k)}
    if plan.get("not_gated_yet"):
        out["not_gated_yet"] = plan["not_gated_yet"]
    if unit.get("stood_on_failed"):
        out["stood_on_failed"] = list(unit["stood_on_failed"])
    if unit.get("deferred") == "resumed" and (unit.get("scoring") or {}).get(
            "leading") == "children":
        out["placed_by_children"] = True
    return out or None


def _biology_evidence(engine, unit, guide):
    """The sample's tissue and disease, for this marker and its references
    (`biology.brief`): the frame the look reads the pictures in."""
    from plexora.plugins.gating.server.autogate import biology

    record = engine.record.get("biology")
    if not record:
        return {}
    refs = [r["marker"] for r in unit.get("reference_gates") or []]
    brief = biology.brief(record, engine.panel_for(unit["project"]), unit["marker"], refs)
    if not brief:
        return {}
    if "biology" not in guide:
        guide.insert(1, "biology")
    return {"biology": brief}


def _hierarchy_evidence(unit, guide):
    brief = _hierarchy_brief(unit)
    if not brief:
        return {}
    if "hierarchy" not in guide:
        guide.insert(1, "hierarchy")
    return {"hierarchy": brief}


def _condition_brief(unit):
    condition = unit.get("condition")
    if not condition:
        return None
    return {k: condition.get(k) for k in ("within", "partner_gate", "relation", "method",
                                          "plain_low", "n_positive_within",
                                          "n_positive_outside", "separation_d")}


def t2_confirm(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    ds, channel = _unit_channel(engine, unit)
    low = unit["candidate"]
    condition = unit.get("condition")
    sample = _sample(engine, ds, unit, low)
    rows = views.t2_rows(sample, low, unit.get("high"))
    to_log = sample.get("fit_space") == "log1p"
    within_note = f" · only {condition['within']}+ cells" if condition else ""
    main = collage.render_collage(
        engine.call.session, ds, layout="t2", rows=rows, marker=channel, gate=low,
        high=unit.get("high"), fmt=_fmt(engine), to_log=to_log,
        pixel=engine.pixel_for(ds.name),
        title=f"{unit['marker']} · gate {collage.compact_number(low)}{within_note} · rows "
              "below/at/above (panels: nuclear | marker; merge | gate-relative)")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    partners = _partner_numbers(engine, unit, ds, low)
    context_sheet = _sheet(engine, unit, ds, channel, low, numbers=partners)
    ctx = _context_brief(unit)
    compartment = ctx.get("compartment") or "the expected"
    if condition:
        question = (f"{unit['marker']} ({ds.name}) gated only within {condition['within']}+ "
                    f"cells, at {collage.compact_number(low)}: do the {condition['within']}+ "
                    f"cells above the gate carry real {compartment} staining, and is the gate "
                    "too low, about right or too high among them? The collage shows "
                    f"{condition['within']}+ cells only.")
    elif unit.get("continuous"):
        question = (f"{unit['marker']} ({ds.name}) is expressed on a continuum: is "
                    f"{collage.compact_number(low)} where real {compartment} expression "
                    "rises out of background -- do the cells just above it stain "
                    "convincingly and the cells just below it not? Too low, about right or "
                    "too high? Judge the rows nearest the gate most.")
    else:
        question = (f"{unit['marker']} ({ds.name}): do the cells above the gate carry real "
                    f"{compartment} staining, and is the gate too low, about right or too "
                    "high? Judge the rows nearest the gate most.")
    guide = ["collage_t2", "sheet", "partners", "no_positives", "requests"]
    scored = _scored_brief(unit)
    if scored:
        guide.insert(1, "scored")
    if unit.get("continuous"):
        guide.insert(0, "continuous")
    if within_eligible(partners) and not condition:
        guide.insert(3, "within_partner")
    packet = {
        "question": question,
        "evidence": {
            "partners": partners,
            "marker": unit["marker"], "project": ds.name,
            "candidate": {"low": low, "high": unit.get("high"), "source": unit.get("source")},
            **({"scored": scored} if scored else {}),
            **_hierarchy_evidence(unit, guide),
            **_biology_evidence(engine, unit, guide),
            **({"condition": _condition_brief(unit)} if condition else {}),
            "within_allowed": within_eligible(partners) if not condition else [],
            "profile": profile_digest(unit.get("summary")),
            "rows": {r["key"]: r["label"] for r in rows},
            "context": ctx, "flags": _flags(unit),
            **_sheet_evidence(context_sheet),
            **_reading(engine, guide + _sheet_keys(context_sheet, unit)),
        },
        "allowed": _allowed(answers.T2Answer, "direction"),
        "_image_meta": [],
    }
    images = _images(packet, (
        (main, "t2_collage", "cells below / at / above the gate"),
        (context_sheet, "context_sheet", "the tissue at three scales, and the partner plot")))
    unit["last_manifest"] = main["manifest"]
    return packet, images


# -- T3 -----------------------------------------------------------------------------


def t3_biological(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    ds, channel = _unit_channel(engine, unit)
    low = unit["candidate"]
    partners = _partner_numbers(engine, unit, ds, low)
    refs = unit.get("reference_gates") or []
    wanted = _requested_partner(unit)
    if wanted:
        refs = sorted(refs, key=lambda r: r["marker"] != wanted)
    ref_channels = [views.image_channel(ds, r["marker"]) for r in refs]
    ref_channels = [c for c in ref_channels if c]
    sample = _sample(engine, ds, unit, low)
    rows = views.t2_rows(sample, low, unit.get("high"))
    to_log = sample.get("fit_space") == "log1p"
    main = collage.render_collage(
        engine.call.session, ds, layout="t3", rows=rows, marker=channel, gate=low,
        high=unit.get("high"), references=ref_channels, fmt=_fmt(engine), to_log=to_log,
        pixel=engine.pixel_for(ds.name),
        title=f"{unit['marker']} with {', '.join(r['marker'] for r in refs) or 'no reference'}"
              f" · gate {collage.compact_number(low)} (panels: nuclear | marker; merge | "
              "reference)")
    numbers = []
    for ref in refs:
        result = tableops.local_or_node(ds, "gating.autogate.bivariate", {
            "a": unit["marker"], "gate_a": low, "b": ref["marker"], "gate_b": ref["gate"],
            "relation": ref["relation"]})
        brief = {k: result[k] for k in ("frac_b_in_a", "conditional_shift_fit")}
        brief["partner"] = ref["marker"]
        numbers.append(brief)
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    second = _sheet(engine, unit, ds, channel, low, references=ref_channels[:1],
                    numbers=partners)
    ctx = _context_brief(unit)
    compartment = ctx.get("compartment") or "the expected"
    condition = unit.get("condition")
    guide = ["collage_t3", "sheet", "partners", "no_positives"]
    if within_eligible(partners) and not condition:
        guide.insert(3, "within_partner")
    scored = _scored_brief(unit)
    if scored:
        guide.insert(1, "scored")
    if unit.get("continuous"):
        guide.insert(0, "continuous")
    packet = {
        "question": (f"{unit['marker']} ({ds.name}) beside its reference "
                     f"{', '.join(r['marker'] for r in refs) or '(none gated yet)'}: is the "
                     f"{compartment} staining real and in the right cells, is the relation "
                     "to the reference as expected, and which way (if any) is the gate "
                     "wrong?"),
        "evidence": {
            "partners": partners,
            "marker": unit["marker"], "candidate": {"low": low, "high": unit.get("high")},
            **({"scored": scored} if scored else {}),
            **_hierarchy_evidence(unit, guide),
            **_biology_evidence(engine, unit, guide),
            **({"condition": _condition_brief(unit)} if condition else {}),
            "within_allowed": within_eligible(partners) if not condition else [],
            "profile": profile_digest(unit.get("summary")), "context": ctx,
            "references": [r["marker"] for r in refs],
            "bivariate_extra": numbers,
            "previous_answer": unit.get("last_answer"),
            **_sheet_evidence(second),
            **_reading(engine, guide + _sheet_keys(second, unit)),
        },
        "allowed": _allowed(answers.T3Answer, "direction"),
        "_image_meta": [],
    }
    images = _images(packet, (
        (main, "t3_collage", "cells with the reference channel"),
        (second, "context_sheet", "the tissue at three scales, and the partner plot")))
    unit["last_manifest"] = main["manifest"]
    return packet, images


# -- T4 -----------------------------------------------------------------------------


def candidate_chain(candidates, start, direction):
    """[{id, low, prev}] nearest the current gate first: each candidate with
    the threshold before it on the way from `start` (what its field shows)."""
    ordered = sorted(candidates, key=lambda c: c["low"], reverse=direction != "up")
    chain, prev = [], float(start)
    for cand in ordered:
        chain.append({"id": cand["id"], "low": float(cand["low"]), "prev": prev})
        prev = float(cand["low"])
    return chain


def t4_candidates(engine, units):
    """The lattice points beyond the current gate, nearest first
    (`lattice.chain`), and the cells that flip between each and the one
    before it. The agent judges every row; the server places the gate
    (`lattice.place`)."""
    from plexora.agent.evidence import collage
    from plexora.plugins.gating.server.autogate import lattice as latmod

    unit = units[0]
    ds, channel = _unit_channel(engine, unit)
    direction = unit.get("direction")
    current = float(unit["candidate"])
    partners = _partner_numbers(engine, unit, ds, current)
    lat = engine.lattice_for(unit)
    points = latmod.chain(lat, current, direction)
    if not points:
        engine.close(unit, "manual_review_recommended",
                     f"no admissible threshold {'above' if direction == 'up' else 'below'} "
                     "the current gate (the marker's lattice ends here)")
        return None
    ids = [f"c{index + 1}" for index in range(len(points))]
    unit["candidates"] = {cid: p["low"] for cid, p in zip(ids, points)}
    unit["candidate_steps"] = {cid: p["id"] for cid, p in zip(ids, points)}
    unit["chain"] = [p["id"] for p in points]
    unit["candidates_reach_edge"] = any(s.startswith("edge:") for s in points[-1]["sources"])
    thresholds = sorted([current] + [p["low"] for p in points])
    condition = unit.get("condition")
    payload = {"marker": unit["marker"], "candidates": thresholds, "high": unit.get("high"),
               "seed": _seed(engine)}
    if condition:
        payload["within"] = {"marker": condition["within"], "gate": condition["partner_gate"]}
    delta = tableops.local_or_node(ds, "gating.autogate.delta", payload)
    at = dict(zip(delta["candidates"], delta.get("n_positive_at") or []))
    # Rows nearest the current gate first, whichever way the gate moves.
    intervals = list(delta["intervals"])
    if direction != "up":
        intervals = intervals[::-1]
    ordered = {**delta, "intervals": intervals}
    names = {index: (f"{collage.compact_number(iv['from'])} to "
                     f"{collage.compact_number(iv['to'])}")
             for index, iv in enumerate(intervals)}
    rows = views.flip_rows(ordered, labels=names)
    main = collage.render_collage(
        engine.call.session, ds, layout="flips", rows=rows, marker=channel,
        gate=current, high=unit.get("high"), fmt=_fmt(engine),
        pixel=engine.pixel_for(ds.name),
        title=f"{unit['marker']} · rows nearest the gate first: the cells that change call "
              "between neighbouring thresholds (panels: marker | merge)")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    scores = {c["id"]: c for c in (unit.get("scoring") or {}).get("candidates") or []}

    def _scored_point(p):
        found = next((scores[src] for src in p["sources"] if src in scores), None)
        return {"score": found.get("score"), "components": found.get("components")} \
            if found else {}

    listing = [{"id": cid, "low": p["low"], "point": p["id"],
                "n_positive": at.get(p["low"], p["n_positive"]) if condition
                else p["n_positive"],
                **({"also": p["sources"][1:]} if len(p["sources"]) > 1 else {}),
                **_scored_point(p)}
               for cid, p in zip(ids, points)]
    chain_view = candidate_chain([{"id": c["id"], "low": c["low"]} for c in listing],
                                 current, direction)
    context_sheet = _sheet(engine, unit, ds, channel, current,
                           candidates=[{"id": c["id"], "low": c["low"]} for c in listing],
                           candidate_fields=chain_view, numbers=partners)
    moving = "mostly_negative" if direction == "up" else "mostly_positive"
    rows_ev = [{"row": f"i{index + 1}", "from": iv["from"], "to": iv["to"],
                "n_flip": iv["n_flip"], "ends_at": ids[index]}
               for index, iv in enumerate(intervals)]
    t4_guide = ["collage_flips", "candidates", "scored", "sheet_candidates", "sheet",
                "partners"]
    packet = {
        "question": (f"{unit['marker']} ({ds.name}): the gate looked too "
                     f"{'low' if direction == 'up' else 'high'}. Each row holds the cells "
                     "that change call between neighbouring thresholds, nearest the gate "
                     "first. Judge EVERY row: are those cells really positive? The gate "
                     f"moves past each row you call `{moving}` and stops at the first you "
                     "do not."),
        "evidence": {"partners": partners,
                     "marker": unit["marker"], "current": current,
                     "direction": direction, "candidates": listing, "intervals": rows_ev,
                     **({"condition": _condition_brief(unit)} if condition else {}),
                     **_hierarchy_evidence(unit, t4_guide),
                     **_biology_evidence(engine, unit, t4_guide),
                     "lattice": {"fingerprint": lat["fingerprint"],
                                 "beyond": [b["id"] for b in lat.get("beyond") or []]},
                     "round": int(unit.get("rounds", 0)) + 1,
                     "profile": profile_digest(unit.get("summary")),
                     **_sheet_evidence(context_sheet),
                     **_reading(engine, t4_guide + _sheet_keys(context_sheet, unit))},
        "allowed": {"intervals": [r["row"] for r in rows_ev],
                    "verdicts": list(schemas.INTERVAL_VERDICTS),
                    "chosen_candidate": ids + list(schemas.T4_CHOICES)},
        "_image_meta": [],
    }
    images = _images(packet, (
        (main, "flips_collage", "cells between neighbouring thresholds, nearest first"),
        (context_sheet, "context_sheet", "each candidate in the tissue, candidates on the "
                                         "plot")))
    unit["last_manifest"] = main["manifest"]
    return packet, images


# -- confirmations -------------------------------------------------------------------


def qc_confirm(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    try:
        ds, channel = _unit_channel(engine, unit)
    except AgentError:
        engine.close(unit, "technically_failed",
                     "technical flags and no image channel to check them against",
                     confidence="failed_qc")
        return None
    low = unit["candidate"]
    if low is None:
        low = (unit.get("seen") or [None])[0]
    view = _sheet(engine, unit, ds, channel, low,
                  title=f"{unit['marker']}: yellow = stain, blue = nuclei, magenta = cells "
                        f"above {collage.compact_number(low)}")
    packet = {
        "question": (f"{unit['marker']} ({ds.name}) needs a whole-image check: "
                     f"{', '.join(unit.get('qc_reason') or unit.get('flags') or [])}. Is "
                     "there real, cell-shaped staining in some cells (`real_signal`), did "
                     "the stain work but no cell in this image is positive "
                     "(`no_positive_population`: the gate is put at the maximum), or is the "
                     "channel technically failed (`technical_failure`: flat, saturated, "
                     "background only, artifact; also gated at the maximum)?"),
        "evidence": {"marker": unit["marker"], "flags": unit.get("flags"),
                     "image_qc": unit.get("image_qc"),
                     "profile": profile_digest(unit.get("summary")),
                     "context": _context_brief(unit), **_sheet_evidence(view),
                     **_reading(engine, ["sheet"] + _sheet_keys(view, unit))},
        "allowed": _allowed(answers.QCAnswer, "verdict"),
        "_image_meta": [],
    }
    return packet, _images(packet, ((view, "context_sheet",
                                     "the whole image and three fields"),))


def regression_confirm(engine, units):
    from plexora.agent.evidence import collage

    unit = units[0]
    ds, channel = _unit_channel(engine, unit)
    low = unit["candidate"]
    # A whole-image question: with `sheets: trim` the fields row is left out.
    view = _sheet(engine, unit, ds, channel, low,
                  rows=("slide",) if engine.options["sheets"] == "trim" else None,
                  title=f"{unit['marker']} at {collage.compact_number(low)}: magenta = "
                        "positive cells")
    regression = unit.get("regression") or {}
    packet = {
        "question": (f"{unit['marker']} ({ds.name}): the chosen gate "
                     f"{collage.compact_number(low)} failed whole-image checks "
                     f"({', '.join(regression.get('failed') or [])}). Does the positive map "
                     "still look right across the tissue?"),
        "evidence": {"marker": unit["marker"], "final": low, "gmm": unit.get("gmm"),
                     "checks": regression.get("checks"), "fraction": regression.get("fraction"),
                     **({"condition": _condition_brief(unit)} if unit.get("condition")
                        else {}),
                     "context": _context_brief(unit), **_sheet_evidence(view),
                     **_reading(engine, ["sheet"] + _sheet_keys(view, unit))},
        "allowed": _allowed(answers.ConfirmAnswer, "verdict"),
        "_image_meta": [],
    }
    return packet, _images(packet, ((view, "context_sheet",
                                     "positives at the chosen gate, three scales"),))


# -- set-up: the pixel size ----------------------------------------------------------


def pixel_setup(engine, units):
    """What one pixel is worth, for an image that does not say: the estimate
    from the cells' size, and three snapshots drawn at it for the agent to
    check. None (and the set-up settled) when nothing is pending."""
    from plexora.plugins.gating.server.autogate import pixel_estimate

    pixel = engine.record.setdefault("pixel", {})
    projects = pixel.setdefault("projects", {})
    pending = [p for p in engine.record["images"]
               if (projects.get(p) or {}).get("status") == "pending"]
    if not pending:
        pixel["status"] = "applied"
        return None
    project = pending[0]
    entry = projects[project]
    session = engine.call.session
    ds = session.data(project)
    if "estimate" not in entry:
        try:
            entry["estimate"] = pixel_estimate.estimate_for(session, ds, seed=_seed(engine))
        except Exception as exc:  # an estimate is a help; its absence still asks
            entry["estimate"] = {"microns_per_pixel": None, "how": f"failed: {exc}"}
    found = entry["estimate"]
    try:
        rendered = pixel_estimate.render_snapshots(session, ds, found, seed=_seed(engine),
                                                   fmt=_fmt(engine))
    except Exception as exc:
        for name in pending:
            projects[name].update(status="unavailable", reason=f"no snapshots: {exc}")
        pixel["status"] = "unavailable"
        engine.log(event="pixel_setup_unavailable", reason=str(exc))
        return None
    mpp = found.get("microns_per_pixel")
    field_um = engine.field_um()
    if mpp:
        said = (f"From the segmented cells (median diameter "
                f"{found['median_diameter_px']:.1f} px, {found['n']} cells) and a typical "
                f"{found['prior']['cell_diameter_um']:g} µm cell, it is about {mpp:.3g} µm/px "
                f"(plausible {found['low']:.3g} to {found['high']:.3g}).")
    else:
        said = ("No estimate could be made from the cells; the snapshots carry a bar in image "
                "pixels. Give the value only if the user states it, or estimate it from the "
                "nuclei (most are five to ten microns across).")
    packet = {
        "question": (f"{project} states no pixel size, so the {field_um:g} µm tissue fields, "
                     f"the cell crops and the scale bars need one. {said} Do the nuclei look "
                     "the size the ring and bar imply? Answer `estimate_confirmed`, "
                     "`adjusted` with your `microns_per_pixel`, or `user_stated` with the "
                     "value the user gave. The deterministic pass runs meanwhile."),
        "evidence": {"project": project, "applies_to": pending,
                     "estimate": {k: found.get(k) for k in (
                         "microns_per_pixel", "low", "high", "n", "median_diameter_px",
                         "iqr_diameter_px", "prior", "how") if found.get(k) is not None},
                     "field_um": field_um,
                     "field_px_at_estimate": round(field_um / mpp, 1) if mpp else None,
                     "snapshots": [{"name": f["name"], "bounds": f["bounds"],
                                    "image_px_per_screen_px": f["image_px_per_screen_px"]}
                                   for f in rendered["manifest"]["fields"]],
                     **_reading(engine, ["pixel_setup"])},
        "allowed": list(schemas.PIXEL_BASES),
        "_image_meta": [],
    }
    return packet, _images(packet, ((rendered, "pixel_snapshots",
                                     "nuclei and outlines, bar and ring at the estimate"),))


def t1_strip(engine, units):
    """One sheet for the T1-accepted markers of one image (`strip_batch` of
    them at most), `strip_cells` cells a row."""
    from plexora.agent.evidence import collage

    project = units[0]["project"]
    ds = engine.call.session.data(project)
    rows = []
    listed = []
    for unit in units:
        channel = views.image_channel(ds, unit["marker"])
        if channel is None:
            unit["audited"] = False
            engine.finalize(unit, method="gmm")
            continue
        low = unit["candidate"]
        sample = _sample(engine, ds, unit, low)
        below = sorted([c for n in ("just_below", "borderline", "low_background")
                        for c in (sample["strata"].get(n) or []) if c["value"] <= low],
                       key=lambda c: -c["value"])[:ENGINE["strip_cells"] // 2]
        above = sorted([c for n in ("borderline", "just_above", "moderate_positive")
                        for c in (sample["strata"].get(n) or []) if c["value"] > low],
                       key=lambda c: c["value"])[:ENGINE["strip_cells"] // 2]
        cells = [dict(c, call="negative") for c in sorted(below, key=lambda c: c["value"])] + \
                [dict(c, call="positive") for c in above]
        rows.append({"key": unit["marker"], "label": unit["marker"], "marker": channel,
                     "gate": low, "cells": cells})
        listed.append(unit)
    if not listed:
        return None
    half = ENGINE["strip_cells"] // 2
    sheet = collage.render_collage(
        engine.call.session, ds, layout="strip", rows=rows, marker=rows[0]["marker"],
        gate=listed[0]["candidate"], fmt=_fmt(engine), pixel=engine.pixel_for(project),
        title=f"accepted automatically - per row: {half} cells just below, {half} just above "
              "the gate (marker | merge)")
    for unit in listed:
        unit.setdefault("artifacts", []).extend(
            a["id"] for a in (sheet.get("artifact"),) if a)
    packet = {
        "question": ("These markers were accepted from their distributions alone. For each "
                     f"row: do the {half} cells on the right look positive and the {half} on "
                     "the left negative? Answer per row with one of "
                     + ", ".join(f"`{v}`" for v in _strip_verdicts())
                     + " (`suspicious` sends it to a proper look)."),
        "evidence": {"markers": [{"marker": u["marker"], "gate": u["candidate"],
                                  "fraction": (u.get("summary") or {}).get("positive_fraction"),
                                  "t1_score": (u.get("t1") or {}).get("score"),
                                  "class": u.get("class")} for u in listed]},
        "allowed": _strip_verdicts(),
        "_image_meta": [],
    }
    image, meta = _image(sheet, "audit_sheet", "near-gate cells, one row per marker")
    packet["_image_meta"].append(meta)
    return packet, [image]


def _strip_verdicts():
    annotation = answers.T1StripAnswer.model_fields["verdicts"].annotation
    return list(get_args(get_args(annotation)[1]))


def panel_context(engine, units):
    from plexora.plugins.gating.server.autogate import context

    project = engine.record["images"][0]
    ds = engine.call.session.data(project)
    panel = context.for_project(ds)
    unresolved = panel["unresolved"]
    if not unresolved:
        engine.record["panel_pending"] = False
        return None
    packet = {
        "question": ("Plexora's vocabulary does not know these markers. For each, give its "
                     "role, compartment, lineage, whether it is binary, and partners -- only "
                     "markers of this panel, only what you are confident of. Skip any you "
                     "do not know; they are gated on their own data."),
        "evidence": {"unresolved": unresolved,
                     "panel": [{"marker": m, "canonical": panel["entries"][m].get("canonical"),
                                "role": panel["entries"][m].get("role")}
                               for m in panel["markers"]],
                     "roles": list(context.ROLES),
                     "rule": "your entries order the markers and pick references; they never "
                             "move a gate, and the vocabulary outranks them"},
        "allowed": ["entries"],
        "_image_meta": [],
    }
    return packet, []


def expression_setup(engine, units):
    """Which matrix to gate, when the values could not settle it: put to the
    user through the agent (and the viewer's requirements modal)."""
    expression = engine.record.get("expression") or {}
    if expression.get("status") != "pending":
        return None
    projects = expression.get("projects") or engine.record["images"]
    current = expression.get("current") or {}
    options = expression.get("options") or []
    where = projects[0] if len(projects) == 1 else f"{len(projects)} images"
    packet = {
        "question": (f"{where} reads its marker values from {current.get('features_layer')} "
                     f"with log1p {'on' if current.get('features_log') else 'off'}, and nobody "
                     "has confirmed that. Which matrix holds the intensities to gate, and "
                     "should log1p be applied as they are read? Put the options to the user; "
                     "do not guess."),
        "evidence": {"projects": projects, "source_kind": expression.get("source_kind"),
                     "current": current, "options": options,
                     "recommendation": expression.get("recommendation"),
                     "rule": expression.get("rule"), "ask_user_required": True},
        "allowed": [o["value"] for o in options],
        "_image_meta": [],
    }
    return packet, []


def transfer_check(engine, units):
    from plexora.plugins.gating.server.autogate import transfer

    return transfer.transfer_packet(engine, units)


BUILDERS = {"t2_confirm": t2_confirm, "t3_biological": t3_biological,
            "t4_candidates": t4_candidates, "qc_confirm": qc_confirm,
            "regression_confirm": regression_confirm, "t1_strip": t1_strip,
            "panel_context": panel_context, "transfer_check": transfer_check,
            "expression_setup": expression_setup, "pixel_setup": pixel_setup}


def narrate(packet) -> str:
    """The panel's line for a packet (`schemas.NARRATION`): what the agent is
    doing, for the user; built from the packet's evidence, never from its
    question."""
    kind = packet.get("kind")
    units = packet.get("units") or []
    evidence = packet.get("evidence") or {}
    marker = units[0]["marker"] if units else ""
    plot = (evidence.get("sheet") or {}).get("plot") or {}
    references = [r["marker"] if isinstance(r, dict) else str(r)
                  for r in evidence.get("references") or []]
    key = kind
    if kind == "t2_confirm":
        compartment = (evidence.get("context") or {}).get("compartment")
        if evidence.get("condition"):
            key = "t2_confirm:within"
        elif plot.get("partner"):
            key = ("t2_confirm:partner_exclusive" if plot.get("relation") == "exclusive"
                   else "t2_confirm:partner")
        elif (schemas.COMPARTMENT_POLICY.get(compartment) or {}).get("image_led"):
            key = "t2_confirm:tissue"
    elif kind == "t4_candidates":
        key = f"t4_candidates:{evidence.get('direction') or 'up'}"
    template = schemas.NARRATION.get(key) or schemas.NARRATION.get(kind) or ""
    partner = (evidence.get("condition") or {}).get("within") or plot.get("partner") or ""
    return template.format(marker=marker, partner=partner,
                           references=" and ".join(references) or partner, n=len(units))


def evidence_label(packet) -> str:
    """The thumbnail's caption in the panel (`schemas.EVIDENCE_LABELS`)."""
    images = packet.get("images") or []
    role = images[0].get("role") if images else None
    units = packet.get("units") or []
    evidence = packet.get("evidence") or {}
    references = [r["marker"] if isinstance(r, dict) else str(r)
                  for r in evidence.get("references") or []]
    template = schemas.EVIDENCE_LABELS.get(role) or ""
    return template.format(marker=units[0]["marker"] if units else "",
                           references=" and ".join(references))
