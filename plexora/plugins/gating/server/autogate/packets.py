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
    "rows": ("one word per collage row, about the cells around the nucleus the four magenta "
             "ticks mark: `own_stain` -- stain touching that nucleus with no other nucleus "
             "inside it (a ring or halo around it, a fill inside it, cytoplasm hugging it on "
             "one or more sides: a spindle cell's stain runs along its long nucleus); "
             "`neighbour_only` -- stain in the tile, but around ANOTHER nucleus, or apart "
             "from this one by a dark gap; `unstained` -- nothing touches the nucleus; "
             "`mixed` -- the row holds both kinds about evenly; `cannot_tell`. A row is what "
             "most of its cells are. Judge the shape, not the brightness: the morphology "
             "panel brightens a dim cell's own stain on purpose, and a marker several cell "
             "types carry can be far dimmer on some (CD4 on monocytes and macrophages): "
             "their faint own stain is `own_stain`. Plexora places the gate from the words"),
    "collage_t2": ("each cell is two panels side by side, blue nuclei and the marker in "
                   "yellow. Left, morphology: the marker windowed on this cell's own "
                   "brightest pixels (a cell with no stain stays dark; a brighter neighbour "
                   "saturates). Right, context: the marker at one fixed window for the whole "
                   "marker, cell outlines thin and grey. Four magenta ticks mark the judged "
                   "cell's nucleus. Caption = the cell's place in its row. Rows: just below, "
                   "at and just above the gate; judge the rows nearest the gate most"),
    "collage_t3": ("as the first look, with a third panel per cell: the reference channel "
                   "alone (white); a subset or co-expressed marker's positives should be "
                   "reference-bright, an exclusive one's reference-dark; the fields add the "
                   "reference in cyan"),
    "collage_t2m": ("each cell is a 2 x 2 block. Top left, morphology: blue nuclei, the marker "
                    "in yellow windowed on this cell's own brightest pixels (a cell with no stain "
                    "stays dark), four magenta ticks around its nucleus. Top right, context: the "
                    "marker at one fixed window for the whole marker, the judged cell's outline "
                    "white, its neighbours' thin grey. Bottom left, the expected partner "
                    "(`partner_panels.expected`) alone in grey, bottom right the not-expected one "
                    "(`partner_panels.not_expected`): each at its own gate's 40 % grey, the judged "
                    "cell outlined in magenta, black when there is none. An expected partner lit "
                    "inside the outline supports the cell's own stain; a not-expected partner lit "
                    "inside it says the cell is another type, so marker light on it is likely a "
                    "neighbour's. Partners glow too: read them inside the outline. Caption = "
                    "the cell's place in its row. Rows: just below, at and just above the "
                    "gate; judge the rows nearest the gate most"),
    "collage_flips_m": ("each cell is the 2 x 2 block of `collage_t2m`. Rows run from the "
                        "current gate outward: row i1 is the cells between the current gate and "
                        "candidate c1, row i2 between c1 and c2, and so on. Judge every row on "
                        "its own"),
    "collage_flips": ("each cell is two panels, morphology | context, as in the first look, "
                      "the judged nucleus between four magenta ticks. Rows run from the "
                      "current gate outward: row i1 is the cells between the current gate "
                      "and candidate c1, row i2 between c1 and c2, and so on. Judge every row "
                      "on its own"),
    "sheet": SCALES_READING,
    "absence": ("an absence check: up to six regions of one marker, each about 200 um "
                "across, the marker in yellow on the session's display, nuclei dim blue, no "
                "cell marked and no threshold anywhere. The regions are where real positives "
                "would most likely show (the most bright cells, crowded tissue) and where a "
                "dim stain shows (cells near the threshold). Per region one word for what "
                "you see: `clear` -- several cells carry the stain as their own (a ring, a "
                "fill, cytoplasm hugging the nucleus), clearly above the faint floor the "
                "unstained cells beside them show; `few` -- one or two such cells; `none` -- "
                "only that floor, light at the edge a cell shares with a bright neighbour, "
                "debris, specks or a smear, a band of stain along the tissue's edge, or a "
                "stain lighting every cell of the field alike; `cannot_tell`. Nothing is "
                "gated from these "
                "words: a marker no region shows a clear positive for, in this picture and a "
                "second of other regions, is closed with no positives, as a person would"),
    "panel": ("one word per numbered cell (its caption), about the cell around the nucleus "
              "the four magenta ticks mark: `own_stain` -- the marker's stain belongs to it, "
              "touching that nucleus with no other nucleus inside it (a ring or halo around "
              "it, a fill inside it, cytoplasm hugging it); `not_own_stain` -- nothing "
              "touches the nucleus, or the stain is a neighbour's or apart by a dark gap; "
              "`uncertain` -- the picture cannot say; `artifact` -- debris, a fold, an edge "
              "or a smear across cells. The cells come from across the marker's range in "
              "random order, so a row means nothing; judge every cell on its own, by shape, "
              "not brightness. In a crowded field a ring may be partial: stain on the "
              "sides that touch no other cell, or all round it inside a cluster of stained "
              "cells, is `own_stain`; light only on the side facing one brighter neighbour, "
              "the rest of its outline dark, is that neighbour's (`not_own_stain`). A "
              "marker several cell types carry can be far dimmer on some "
              "(`context.lineage`; CD4: bright on helper T cells, dim on monocytes and "
              "macrophages): a faint stain that is the cell's own is `own_stain`, judged "
              "against the unstained cells, not the brightest. Say `uncertain` rather than "
              "guess. Plexora chooses the gate from the words"),
    "panel_fibrous": ("a fibrous stain (smooth muscle, vessel walls, fibroblasts, "
                      "pericytes): shape does not decide -- a spindle cell inside a band and "
                      "a round or oval cell beside a vessel are both `own_stain` when the "
                      "band hugs that cell's outline on one or more sides; `not_own_stain` "
                      "when the band passes it with a dark gap, or only crosses its outline "
                      "without following it"),
    "collage_panel": ("each cell is two panels side by side, blue nuclei and the marker in "
                      "yellow. Left, morphology: the marker windowed on this cell's own "
                      "brightest pixels (a cell with no stain stays dark; a brighter "
                      "neighbour saturates). Right, context: the marker at one fixed window "
                      "for the whole marker, outlines thin and grey"),
    "collage_panel_m": ("each cell is a 2 x 2 block. Top: morphology (the marker windowed on "
                        "this cell's own brightest pixels) and context (one fixed window for "
                        "the marker, the judged cell outlined white). Bottom: the expected "
                        "partner (`partner_panels.expected`) and the not-expected one "
                        "(`partner_panels.not_expected`) alone in grey, the judged cell "
                        "outlined magenta, black when there is none. The partners are context: "
                        "an expected partner lit inside the outline is consistent with the "
                        "lineage, a not-expected one with another lineage, and neither decides "
                        "whether the marker's stain is this cell's own -- a marker several cell "
                        "types carry is own stain on a cell without the expected partner too. "
                        "Judge the marker tiles"),
    "sheet_candidates": ("in a look at candidates the sheet's top row is one tissue field per "
                         "candidate: the cells between that candidate and the threshold "
                         "before it, magenta = positive at that candidate. A candidate is "
                         "right when the cells it drops (or keeps) look like the negatives "
                         "(or positives) around them in the tissue"),
    "partners": ("`partners` are the whole-image numbers against each partner gated so "
                 "far: `grade` how reliable its gate is; `orphan_fraction` the share of this "
                 "marker's positives that a subset or co-expressed partner calls negative; "
                 "`double_positive_fraction` the share an exclusive partner also calls "
                 "positive. The sheet plots the partner that contradicts this gate most; a "
                 "request naming a partner plots that one"),
    "within_partner": ("a subset marker whose stain is real inside a partner's positives and "
                       "noise outside them (another lineage's spill, an off-target stain) is "
                       "answered with `within` = that partner (one of `allowed.within`): the "
                       "gate is refitted among the partner's positives and shown again. The "
                       "gate written is the plain gate at that threshold, with the condition "
                       "recorded beside it; confidence is at most moderate"),
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
    "display_setup": ("each row is one marker, each column (c1 to c5, darker to brighter) one "
                      "way of drawing it: nuclei blue, the marker yellow, pixels at full white "
                      "pure red. A block is four cells: a background cell top left, a dim cell "
                      "top right, two of the brightest below. A good background is a faint "
                      "dark-grey floor you can tell from black; a good bright cell shows the "
                      "row's pattern with detail inside it (a ring, a speckled nucleus, "
                      "granules), not a flat or red blob. The row's `pattern` says which "
                      "structure that marker has. Judge each row on its own"),
    **{f"plot:{relation}": text for relation, text in BIVARIATE_READING.items()},
}


#: [cal] evidence numbers are sent to this many significant figures: every
#: number a packet carries is a count, a share or a gate an agent judges, never
#: one it types back (answers name candidates by id).
SIGNIFICANT = 4


#: Guide entries and answer kinds only one engine's looks use
#: (`SessionOptions.engine`): a session is sent its own engine's, and a
#: packet that needs another's (a v1 look inside a v2 session,
#: for a marker with one candidate) carries those texts and its answer
#: schema itself (`_reading`, `lean`).
ENGINE_ONLY = {
    "v1": {"entries": ("rows", "collage_t2", "collage_t3", "collage_t2m",
                            "collage_flips_m", "collage_flips", "sheet_candidates",
                            "within_partner", "requests"),
                "kinds": ("t2_confirm", "t3_biological", "t4_candidates")},
    "v2": {"entries": ("panel", "panel_fibrous", "collage_panel", "collage_panel_m"),
                   "kinds": ("cell_panel",)},
}


def reading_guide(engine=schemas.DEFAULT_ENGINE) -> dict:
    """The session's reading guide (`gating_session_start`, `_status`): the
    fixed texts, the compartment readings and every answer schema, in a fixed
    order -- the same bytes for every session of a build and engine, so an
    agent's client caches it once (`guide_version` says when it changed).
    Another engine's looks are left out (`ENGINE_ONLY`)."""
    engine = schemas.engine_name(engine)
    guide = _full_guide()
    for other, parts in ENGINE_ONLY.items():
        if other == engine:
            continue
        for key in parts["entries"]:
            guide.pop(key, None)
        guide["answer_schemas"] = {k: v for k, v in guide["answer_schemas"].items()
                                   if k not in parts["kinds"]}
    return guide


def _full_guide() -> dict:
    guide = dict(READING_GUIDE)
    for name, policy in sorted(schemas.COMPARTMENT_POLICY.items()):
        guide[f"compartment:{name}"] = policy["reading"]
    full = {kind: answers.schema_for(kind) for kind in answers.KINDS}
    # Fields every answer takes (notes, flags, request, ask_user) are
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


def guide_version(engine=schemas.DEFAULT_ENGINE) -> str:
    """A short hash of `reading_guide(engine)`: an agent that holds this
    version passes it back (`known_guide`) and is not sent the guide again."""
    import hashlib
    import json

    blob = json.dumps(reading_guide(engine), sort_keys=True, separators=(",", ":"))
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
        if any(packet["kind"] in parts["kinds"] for name, parts in ENGINE_ONLY.items()
               if name != schemas.engine_name(options["engine"])):
            # Not in this session's guide: the schema comes with the packet.
            packet["answer_schema"] = answers.schema_for(packet["kind"])
        else:
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
    the whole-image checks lose their passing rows; the
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
    guide = reading_guide(engine.options["engine"])
    full = _full_guide()
    specific = [s.strip("; ") for s in specific if s]
    # An entry this session's guide leaves out (another engine's look) is
    # sent with the packet.
    specific = [full[k] for k in dict.fromkeys(keys) if k not in guide and k in full] \
        + specific
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


def _partner_line(numbers) -> list:
    """What a look is told of each partner: one line -- the relation, how
    reliable the partner's gate is, and the one share that says whether this
    gate keeps to the relation. The rest of `_partner_numbers` is code's (the
    sheet's plot, `regression`'s purity check)."""
    out = []
    for n in numbers or ():
        entry = {"partner": n["partner"], "relation": n["relation"],
                 "grade": n.get("partner_confidence")}
        for key in ("orphan_fraction", "double_positive_fraction"):
            if n.get(key) is not None:
                entry[key] = round(float(n[key]), 3)
        out.append(entry)
    return out


def within_eligible(partners) -> list:
    """The partners a `within` answer may name: `subset` partners gated at
    moderate confidence or better, that the data agree with -- no more than
    `bivariate.WITHIN["max_orphan"]` of the marker's positives at the gate
    negative for the partner. A partner most of the positives sit outside of
    is a subset in the vocabulary, not in this image (or its gate is wrong),
    and refitting among its positives drops real cells."""
    from plexora.plugins.gating.server.autogate import bivariate

    limit = bivariate.WITHIN["max_orphan"]
    return [p["partner"] for p in partners or ()
            if p.get("relation") == "subset"
            and (p.get("grade") or p.get("partner_confidence")) in ("high", "moderate")
            and (p.get("orphan_fraction") is None or p["orphan_fraction"] <= limit)]


def _row_spans(rows) -> dict:
    """{row key: [lowest, highest]} of the values a look's rows show: what
    the look judged, by value (`transitions._judge_rows`)."""
    out = {}
    for row in rows:
        values = [float(c["value"]) for c in row.get("cells") or ()
                  if c.get("value") is not None]
        if values:
            out[row["key"]] = [min(values), max(values)]
    return out


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
           candidate_fields=None, title=None, numbers=None, rows=None, plot=None):
    """The context sheet for a unit (the second picture of a look, the only
    one of a check); its artifact joins the unit's. With `sheets: trim`, a
    row this reader was already shown with the same inputs is left out
    (`_sheet_rows`); with none left, no sheet is drawn (`skipped`). `plot`
    names the partner to plot when the caller knows it."""
    from plexora.plugins.gating.server.autogate import sheet

    refs = engine.references_ready(unit)
    chosen = [r for r in refs if r["marker"] == plot]
    if plot and not chosen:
        # Not one of this marker's references (an exclusion the tree derives
        # across lineages): its gate from the image's ledger.
        entry = engine.ledger(unit["project"], without=unit).get(plot) or {}
        if entry.get("gate") is not None and entry.get("grade") != "failed":
            chosen = [{"marker": plot, "relation": "exclusive", "gate": entry["gate"]}]
    partner = ({**chosen[0], "why": "the lineage that should exclude this marker"} if chosen
               else plot_partner(unit, refs, numbers))
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


def _multiview(engine, unit, ds, reference=None):
    """(layout, partners, evidence) for a look's collage with tiles on
    (`SessionOptions.tiles`): the 2 x 2 multi-view layout `t2m` and the
    partner channels its `co` / `anti` panels show -- the reference, when a
    look asked for one, in the panel its relation belongs to. (None, None,
    {}) without tiles: the look is today's."""
    if not engine.tiles_on():
        return None, None, {}
    partners = dict(engine.partner_panels(unit) or {})
    partners.setdefault("markers", {})
    partners.setdefault("gates", {})
    if reference:
        kind = "anti" if reference["relation"] == "exclusive" else "co"
        channel = views.image_channel(ds, reference["marker"])
        if channel:
            partners[kind] = channel
            partners["markers"][kind] = reference["marker"]
            partners["gates"][channel] = float(reference["gate"])
    names = partners["markers"]
    return "t2m", partners, {"partner_panels": {"expected": names.get("co"),
                                                "not_expected": names.get("anti")}}


def _panel_words(layout, partners):
    if layout != "t2m":
        return None
    names = (partners or {}).get("markers") or {}
    return (f"morphology | outlined context / expected {names.get('co') or 'none'} | "
            f"not expected {names.get('anti') or 'none'}")


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
    layout, partner_panels, multiview = _multiview(engine, unit, ds)
    main = collage.render_collage(
        engine.call.session, ds, layout=layout or "t2", rows=rows, marker=channel, gate=low,
        high=unit.get("high"), fmt=_fmt(engine), to_log=to_log,
        pixel=engine.pixel_for(ds.name), partners=partner_panels,
        title=f"{unit['marker']} · gate {collage.compact_number(low)}{within_note} · rows "
              f"below/at/above (each cell: "
              f"{_panel_words(layout, partner_panels) or 'morphology | context'})")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    numbers = _partner_numbers(engine, unit, ds, low)
    context_sheet = _sheet(engine, unit, ds, channel, low, numbers=numbers)
    partners = _partner_line(numbers)
    compartment = (unit.get("context") or {}).get("compartment")
    stain = f"{compartment} {unit['marker']}" if compartment else unit["marker"]
    if condition:
        question = (f"{unit['marker']} ({ds.name}) gated only within {condition['within']}+ "
                    f"cells, at {collage.compact_number(low)}; the collage shows "
                    f"{condition['within']}+ cells only. For each row, do the cells carry "
                    f"their OWN {stain} stain, around the nucleus the ticks mark? One word "
                    "per row, and the pattern of the stained cells.")
    else:
        question = (f"{unit['marker']} ({ds.name}): for each row of the collage, do the cells "
                    f"carry their OWN {stain} stain, around the nucleus the ticks mark? One "
                    "word per row, and the pattern of the stained cells.")
    within = within_eligible(partners) if not condition else []
    guide = ["rows", "collage_t2m" if layout else "collage_t2", "sheet", "partners",
             "requests"] + (["within_partner"] if within else [])
    packet = {
        "question": question,
        "evidence": {
            "partners": partners,
            "marker": unit["marker"], "project": ds.name,
            "candidate": {"low": low, "high": unit.get("high")},
            **({"condition": _condition_brief(unit)} if condition else {}),
            "within_allowed": within,
            "profile": {"class": (unit.get("summary") or {}).get("class")},
            "rows": {r["key"]: r["label"] for r in rows},
            **multiview,
            "context": {"compartment": compartment}, "flags": _flags(unit),
            **_sheet_evidence(context_sheet),
            **_reading(engine, guide + _sheet_keys(context_sheet, unit)),
        },
        "allowed": {"rows": [r["key"] for r in rows], "verdicts": list(schemas.ROW_VERDICTS),
                    "patterns": list(schemas.PATTERNS), "within": within},
        "_image_meta": [],
        "_row_spans": _row_spans(rows),
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
    numbers = _partner_numbers(engine, unit, ds, low)
    refs = unit.get("reference_gates") or []
    wanted = _requested_partner(unit)
    if wanted:
        refs = sorted(refs, key=lambda r: r["marker"] != wanted)
    ref_channels = [views.image_channel(ds, r["marker"]) for r in refs]
    ref_channels = [c for c in ref_channels if c]
    sample = _sample(engine, ds, unit, low)
    rows = views.t2_rows(sample, low, unit.get("high"))
    to_log = sample.get("fit_space") == "log1p"
    layout, partner_panels, multiview = _multiview(engine, unit, ds,
                                                   reference=refs[0] if refs else None)
    main = collage.render_collage(
        engine.call.session, ds, layout=layout or "t3", rows=rows, marker=channel, gate=low,
        high=unit.get("high"), references=() if layout else ref_channels, fmt=_fmt(engine),
        to_log=to_log, pixel=engine.pixel_for(ds.name),
        partners=partner_panels,
        title=f"{unit['marker']} with {', '.join(r['marker'] for r in refs) or 'no reference'}"
              f" · gate {collage.compact_number(low)} (each cell: "
              f"{_panel_words(layout, partner_panels) or 'morphology | context | reference'})")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    second = _sheet(engine, unit, ds, channel, low, references=ref_channels[:1],
                    numbers=numbers)
    partners = _partner_line(numbers)
    compartment = (unit.get("context") or {}).get("compartment")
    stain = f"{compartment} {unit['marker']}" if compartment else unit["marker"]
    condition = unit.get("condition")
    within = within_eligible(partners) if not condition else []
    guide = ["rows"] + (["collage_t2m"] if layout else ["collage_t2", "collage_t3"]) + \
        ["sheet", "partners"] + (["within_partner"] if within else [])
    packet = {
        "question": (f"{unit['marker']} ({ds.name}) beside its reference "
                     f"{', '.join(r['marker'] for r in refs) or '(none gated yet)'}: for each "
                     f"row, do the cells carry their OWN {stain} stain, around the nucleus the "
                     "ticks mark? "
                     + ("The partner panels hold the reference. " if layout else
                        "The third panel of each cell is the reference. ")
                     + "One word per row, and the pattern of the stained cells."),
        "evidence": {
            "partners": partners,
            "marker": unit["marker"], "project": ds.name,
            "candidate": {"low": low, "high": unit.get("high")},
            **({"condition": _condition_brief(unit)} if condition else {}),
            "within_allowed": within,
            "profile": {"class": (unit.get("summary") or {}).get("class")},
            "rows": {r["key"]: r["label"] for r in rows},
            "context": {"compartment": compartment},
            "references": [r["marker"] for r in refs],
            **multiview,
            **_sheet_evidence(second),
            **_reading(engine, guide + _sheet_keys(second, unit)),
        },
        "allowed": {"rows": [r["key"] for r in rows], "verdicts": list(schemas.ROW_VERDICTS),
                    "patterns": list(schemas.PATTERNS), "within": within},
        "_image_meta": [],
        "_row_spans": _row_spans(rows),
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
    numbers = _partner_numbers(engine, unit, ds, current)
    lat = engine.lattice_for(unit)
    points = latmod.chain(lat, current, direction)
    if not points:
        engine.close(unit, "manual_review_recommended",
                     f"no admissible threshold {'above' if direction == 'up' else 'below'} "
                     "the current gate (the marker's lattice ends here)")
        return None
    from plexora.plugins.gating.server.autogate import cellcalls

    reach = latmod.chain(lat, current, direction, max_points=cellcalls.CHECK_REACH)
    check = _pending_check(unit, current, direction, reach)
    if check is not None:
        # The check's own points (`cellcalls.check_points`: none a sliver,
        # any along the walk to its anchor).
        ends = [round(float(r["point"]), 9) for r in check["rows"]]
        points = [p for p in reach if round(float(p["low"]), 9) in ends]
    ids = [f"c{index + 1}" for index in range(len(points))]
    unit["candidates"] = {cid: p["low"] for cid, p in zip(ids, points)}
    unit["candidate_steps"] = {cid: p["id"] for cid, p in zip(ids, points)}
    unit["chain"] = [p["id"] for p in points]
    unit["candidates_reach_edge"] = any(s.startswith("edge:") for s in points[-1]["sources"])
    thresholds = sorted([current] + [p["low"] for p in points])
    condition = unit.get("condition")
    # As many cells an interval as a row draws: `delta_cells` spreads its
    # picks over the interval's values, so a larger sample cut to the row
    # would leave out its brightest cells.
    payload = {"marker": unit["marker"], "candidates": thresholds, "high": unit.get("high"),
               "seed": _seed(engine), "n_per_interval": views._per_row("flips")}
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
    rows = views.flip_rows(ordered, labels=names) if check is None \
        else _check_rows(ds, unit, check, names)
    layout, partner_panels, multiview = _multiview(engine, unit, ds)
    main = collage.render_collage(
        engine.call.session, ds, layout=layout or "flips", rows=rows, marker=channel,
        gate=current, high=unit.get("high"), fmt=_fmt(engine),
        pixel=engine.pixel_for(ds.name), partners=partner_panels,
        title=f"{unit['marker']} · rows nearest the gate first: the cells that change call "
              "between neighbouring thresholds (each cell: "
              f"{_panel_words(layout, partner_panels) or 'morphology | context'})")
    unit.setdefault("artifacts", []).extend(a["id"] for a in (main.get("artifact"),) if a)
    listing = [{"id": cid, "low": p["low"],
                "n_positive": at.get(p["low"], p["n_positive"]) if condition
                else p["n_positive"]}
               for cid, p in zip(ids, points)]
    chain_view = candidate_chain([{"id": c["id"], "low": c["low"]} for c in listing],
                                 current, direction)
    context_sheet = _sheet(engine, unit, ds, channel, current,
                           candidates=[{"id": c["id"], "low": c["low"]} for c in listing],
                           candidate_fields=chain_view, numbers=numbers)
    rows_ev = [{"row": f"i{index + 1}", "from": iv["from"], "to": iv["to"],
                "n_flip": iv["n_flip"]}
               for index, iv in enumerate(intervals)]
    if check is not None:
        rows_ev = [{"row": row["key"], "from": iv["from"], "to": iv["to"],
                    "n_shown": len(row["cells"])}
                   for iv, pair in zip(intervals, _pairs(rows)) for row in pair]
    compartment = (unit.get("context") or {}).get("compartment")
    stain = f"{compartment} {unit['marker']}" if compartment else unit["marker"]
    t4_guide = ["rows", "collage_t2m", "collage_flips_m" if layout else "collage_flips",
                "sheet_candidates", "sheet", "partners"]
    if not layout:
        t4_guide.remove("collage_t2m")
    question = (f"{unit['marker']} ({ds.name}): each row holds the cells that change "
                "call between neighbouring thresholds, nearest the current gate first. "
                f"For EVERY row, do those cells carry their OWN {stain} stain, around "
                "the nucleus the ticks mark? One word per row.")
    if check is not None:
        question = (f"{unit['marker']} ({ds.name}): the rows come in pairs (i1a and i1b, "
                    "i2a and i2b, ...), nearest the current gate first; both rows of a pair "
                    "hold cells from the same range of values, picked in two different ways. "
                    f"Judge EVERY row on its own: do its cells carry their OWN {stain} stain, "
                    "around the nucleus the ticks mark? One word per row.")
        if check.get("reading") == "count":
            question = (f"{unit['marker']} ({ds.name}): the rows come in pairs (i1a and i1b, "
                        "i2a and i2b, ...), nearest the current gate first; both rows of a "
                        "pair hold cells from the same range of values, picked in two "
                        "different ways. For EVERY row on its own, count its cells that "
                        f"carry their OWN {stain} stain around the nucleus the ticks mark "
                        "(a ring or halo around it, a fill inside it, cytoplasm hugging it); "
                        "stain around another nucleus does not count. Answer `counts`, one "
                        "number per row (or `cannot_tell`).")
    packet = {
        "question": question,
        "evidence": {"partners": _partner_line(numbers),
                     "marker": unit["marker"], "project": ds.name, "current": current,
                     "direction": direction, "candidates": listing, "intervals": rows_ev,
                     **multiview,
                     **({"condition": _condition_brief(unit)} if condition else {}),
                     "round": int(unit.get("rounds", 0)) + 1,
                     **_sheet_evidence(context_sheet),
                     **_reading(engine, t4_guide + _sheet_keys(context_sheet, unit))},
        "allowed": {"intervals": [r["row"] for r in rows_ev],
                    "verdicts": list(schemas.ROW_VERDICTS)},
        "_image_meta": [],
    }
    if check is not None:
        packet["evidence"]["pairs"] = len(rows) // 2
        # Its own AI task (`tasks.yaml` gating.classifier_check), for a
        # harness that routes by the packet.
        packet["evidence"]["check"] = "classifier"
        if check.get("reading") == "count":
            packet["evidence"]["reading"] = "count"
            # Each row's most: its cell count.
            packet["allowed"] = {"intervals": packet["allowed"]["intervals"],
                                 "counts": {r["key"]: len(r["cells"]) for r in rows}}
    images = _images(packet, (
        (main, "flips_collage", "cells between neighbouring thresholds, nearest first"),
        (context_sheet, "context_sheet", "each candidate in the tissue, candidates on the "
                                         "plot")))
    unit["last_manifest"] = main["manifest"]
    return packet, images


def _pending_check(unit, current, direction, points):
    """The classifier check this look is for (`cellcalls.check`), when the
    unit has one not yet shown and its rows end at points of this chain; a
    check whose points moved (the lattice was split since) is dropped."""
    from plexora.plugins.gating.server.autogate import cellcalls

    check = cellcalls.pending({**unit, "direction": direction, "candidate": current})
    if check is None:
        return None
    ends = [round(float(r["point"]), 9) for r in check["rows"]]
    lows = [round(float(p["low"]), 9) for p in points]
    if [e for e in lows if e in ends] != ends:
        check["asked"] = True
        check["dropped"] = "the candidates changed before the look"
        return None
    return check


def _check_rows(ds, unit, check, names):
    """Two collage rows per interval, `i<k>a` and `i<k>b`: the cells the
    classifier flags and the cells it agrees on, in the order `check_rows`
    drew (which is which is never in the packet)."""
    from plexora.plugins.gating.server.autogate import cells as cellmod
    from plexora.plugins.gating.server.autogate import profile as profmod
    from plexora.plugins.gating.server.autogate import sampler

    c = cellmod.cells(ds)
    v = cellmod.values(ds, unit["marker"])
    col = profmod.column(ds, unit["marker"])
    rows = []
    for index, row in enumerate(check["rows"]):
        halves = {row["flagged_is"]: row["flagged"],
                  "b" if row["flagged_is"] == "a" else "a": row["control"]}
        for tag in ("a", "b"):
            key = f"i{index + 1}{tag}"
            cells = [sampler._record(c, v, col, int(r), "flip", "flips", None)
                     for r in halves[tag]]
            rows.append({"key": key,
                         "label": f"{key}: {names.get(index) or ''} ({len(cells)} cells)",
                         "cells": views.indexed(cells)})
    return rows


def _pairs(rows):
    return [rows[i:i + 2] for i in range(0, len(rows), 2)]


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
    c = unit.get("lineage_contamination") if unit.get("lineage_check") else None
    # The lineage suspect's check (`replan.check_whole_image`) draws the
    # excluding lineage's channel in the fields, in cyan, and plots it: the
    # question is whether any cell outside that lineage carries the stain.
    partner_channel = views.image_channel(ds, c["partner"]) if c else None
    view = _sheet(engine, unit, ds, channel, low,
                  references=[partner_channel] if partner_channel else (),
                  plot=c["partner"] if c else None,
                  title=f"{unit['marker']}: yellow = stain, blue = nuclei, "
                        + (f"cyan = {c['partner']}, " if partner_channel else "")
                        + f"magenta = cells above {collage.compact_number(low)}")
    verdict = unit.get("qc_verdict")
    qc_said = (f" Image QC reviewed this channel and called it {verdict}."
               if verdict in ("usable", "failed") else "")
    if c:
        qc_said += (f" Its brightest cells are {c['partner']}+ cells, which should not carry "
                    f"{unit['marker']}: stain only on those cells (their light in this "
                    "channel) is no population of its own; real signal means other cells "
                    "carry it too."
                    + (f" In the fields, cyan is {c['partner']}: judge the yellow cells that "
                       "are not cyan." if partner_channel else ""))
    packet = {
        "question": (f"{unit['marker']} ({ds.name}) needs a whole-image check: "
                     f"{', '.join(unit.get('qc_reason') or unit.get('flags') or [])}.{qc_said} "
                     "Is there real, cell-shaped staining in some cells (`real_signal`), did "
                     "the stain work but no cell in this image is positive "
                     "(`no_positive_population`: the gate is put at the maximum), or is the "
                     "channel technically failed (`technical_failure`: flat, saturated, "
                     "background only, artifact; also gated at the maximum)?"),
        "evidence": {"marker": unit["marker"], "project": ds.name, "flags": unit.get("flags"),
                     "image_qc": unit.get("image_qc"),
                     "profile": profile_digest(unit.get("summary")),
                     "context": _context_brief(unit), **_sheet_evidence(view),
                     **_reading(engine, ["sheet"] + _sheet_keys(view, unit))},
        "allowed": _allowed(answers.QCAnswer, "verdict"),
        "_image_meta": [],
    }
    return packet, _images(packet, ((view, "context_sheet",
                                     "the whole image and three fields"),))


def _absence_skipped(engine, unit, why, regions=()):
    """No absence picture could be drawn: what was decided without it
    stands -- a technical check's verdict closes the marker, anything else
    goes on to its first look. None (the unit settled without a packet)."""
    unit["absence_done"] = True
    unit["absence"] = {"skipped": why, "regions": list(regions)}
    after = unit.pop("absence_after", None)
    if after:
        engine.close(unit, after, unit.pop("absence_reason", None) or why,
                     **({"confidence": "failed_qc"} if after == "technically_failed" else {}))
    else:
        unit["state"] = "awaiting_t2"
    return None


def absence_check(engine, units):
    """Four regions of the tissue, nothing marked: is any cell clearly
    positive by its own stain (`absence`)? Before the marker's first look,
    or after a technical check called the stain failed or empty."""
    from plexora.plugins.gating.server.autogate import absence

    unit = units[0]
    try:
        ds, channel = _unit_channel(engine, unit)
    except AgentError:
        return _absence_skipped(engine, unit, "no image channel to draw")
    earlier = unit.get("absence_rounds") or []
    lone = unit.get("absence_confirming")
    if lone and earlier:
        return _absence_close(engine, unit, ds, channel, lone, earlier)
    view = absence.render(engine.call.session, ds, marker=unit["marker"], channel=channel,
                          unit=unit, seed=_seed(engine), fmt=_fmt(engine),
                          pixel=engine.pixel_for(ds.name),
                          avoid=[b for r in earlier for b in absence.boxes(r["regions"])])
    regions = [r["region"] for r in view["fields"]]
    if len(regions) < absence.MIN_JUDGED:
        return _absence_skipped(engine, unit, "fewer regions than the check needs",
                                regions=view["fields"])
    compartment = (unit.get("context") or {}).get("compartment")
    stain = f"{compartment} {unit['marker']}" if compartment else unit["marker"]
    packet = {
        "question": (f"{unit['marker']} ({ds.name}): {len(regions)} "
                     + ("other " if earlier else "") + "regions of the tissue, "
                     f"the {stain} stain in yellow, nuclei dim blue, NO cell marked. For "
                     "EVERY region: does it show cells clearly positive by their OWN stain "
                     "-- brighter than the faint floor the unstained cells there show, where "
                     "the stain belongs on the cell -- (`clear`: several; `few`: one or two; "
                     "`none`: only the floor, glow beside a bright neighbour, debris or "
                     "smears; `cannot_tell`)? Judge the cells, not the brightness of the "
                     "picture."),
        "evidence": {"marker": unit["marker"], "project": ds.name,
                     "round": len(earlier) + 1,
                     "regions": [{k: r[k] for k in ("region", "class", "cells") if k in r}
                                 for r in view["fields"]],
                     "field": view["manifest"]["field"].get("label"),
                     "context": _context_brief(unit),
                     **_reading(engine, ["absence"]
                                + [k for k in (_compartment_key(unit),) if k])},
        "allowed": {"regions": regions, "words": list(absence.REAL_WORDS)},
        "_image_meta": [],
    }
    unit["absence_regions"] = view["fields"]
    return packet, _images(packet, ((view, "absence_regions",
                                     f"{len(regions)} regions, nothing marked"),))


def _absence_close(engine, unit, ds, channel, lone, earlier):
    """The one region of the last absence picture that showed clear
    positives, seen again close up and in its surroundings
    (`absence.render_close`): cells, or an edge band, a fold, debris?"""
    from plexora.plugins.gating.server.autogate import absence

    region = next((r for r in earlier[-1].get("regions") or () if r.get("region") == lone),
                  None)
    if region is None:
        unit.pop("absence_confirming", None)
        return _absence_skipped(engine, unit, "the region to look at again is not on record")
    view = absence.render_close(engine.call.session, ds, marker=unit["marker"],
                                channel=channel, region=region, fmt=_fmt(engine),
                                pixel=engine.pixel_for(ds.name))
    compartment = (unit.get("context") or {}).get("compartment")
    stain = f"{compartment} {unit['marker']}" if compartment else unit["marker"]
    packet = {
        "question": (f"{unit['marker']} ({ds.name}): in the last picture only region {lone} "
                     "showed cells clearly positive. Here it is again as r1: close up (left) "
                     f"and in its surroundings (right, the white box), the {stain} stain in "
                     "yellow, nuclei dim blue, NO cell marked. Does r1 show cells clearly "
                     "positive by their OWN stain -- each stained where the stain belongs on "
                     "that cell, brighter than the floor the unstained cells show -- "
                     "(`clear`: several; `few`: one or two; `none`: no such cell -- a band "
                     "along the tissue edge, a fold, debris or smears, glow beside a bright "
                     "neighbour, or a stain lighting every cell of the field alike; "
                     "`cannot_tell`)? Judge the cells, not the brightness of the picture."),
        "evidence": {"marker": unit["marker"], "project": ds.name,
                     "round": len(earlier), "close_look": True,
                     "regions": [{"region": "r1", **{k: region[k] for k in ("class", "cells")
                                                     if k in region}}],
                     "context": _context_brief(unit),
                     **_reading(engine, ["absence"]
                                + [k for k in (_compartment_key(unit),) if k])},
        "allowed": {"regions": ["r1"], "words": list(absence.REAL_WORDS)},
        "_image_meta": [],
    }
    return packet, _images(packet, ((view, "absence_close",
                                     "one region close up and in its surroundings"),))


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
    allowed = _allowed(answers.ConfirmAnswer, "verdict")
    spatial = ""
    if regression.get("spatial_only"):
        # Where the positives are (an edge, one region) says nothing about
        # which way the threshold is off: the look may keep or reject the
        # gate, never move it to chase the artifact (lsp11385_2, SMA).
        allowed = [v for v in allowed if v not in ("too_low", "too_high")]
        spatial = (" Only checks of WHERE the positives are failed: say whether the map "
                   "holds or an artifact drives it; the gate is not moved from here.")
    packet = {
        "question": (f"{unit['marker']} ({ds.name}): the chosen gate "
                     f"{collage.compact_number(low)} failed whole-image checks "
                     f"({', '.join(regression.get('failed') or [])}). Does the positive map "
                     f"still look right across the tissue?{spatial}"),
        "evidence": {"marker": unit["marker"], "final": low, "gmm": unit.get("gmm"),
                     "checks": regression.get("checks"), "fraction": regression.get("fraction"),
                     **({"condition": _condition_brief(unit)} if unit.get("condition")
                        else {}),
                     "context": _context_brief(unit), **_sheet_evidence(view),
                     **_reading(engine, ["sheet"] + _sheet_keys(view, unit))},
        "allowed": allowed,
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


def _structure(engine, ds, unit):
    """(words, short) for what a marker's bright cells should show
    (`schemas.STRUCTURE_WORDS`), by its compartment: the unit's context, else
    the panel's."""
    compartment = (unit.get("context") or {}).get("compartment")
    if compartment is None:
        try:
            from plexora.plugins.gating.server.autogate import context

            entry = (context.for_project(ds, create=False).get("entries") or {}).get(
                unit["marker"]) or {}
            compartment = entry.get("compartment")
        except Exception:
            compartment = None
    return (schemas.STRUCTURE_WORDS.get(compartment, schemas.STRUCTURE_DEFAULT),
            schemas.STRUCTURE_SHORT.get(compartment, ""))


def display_setup(engine, units):
    """The contrast check for the next image whose calibration is ready: a
    grid of at most `display_batch` of its markers (`display_grid`), each at
    the calibration's brightness ladder. Markers the calibration settles on
    its own (no bright cells to show, an overview window, or a choice an
    earlier session made) are not asked; a dim or floored one is. None, with
    the image's state moved on, when nothing is left to ask or no grid can be
    drawn."""
    from plexora.agent.evidence import calibration, display_grid

    display = engine.record.setdefault("display", {})
    projects = display.setdefault("projects", {})
    due, _waiting = engine.display_due()
    if not due:
        engine.settle_display()
        return None
    project = due[0]
    entry = projects.setdefault(project, {"status": "pending"})
    session = engine.call.session
    try:
        record, _changed = calibration.calibrate(session, project)
    except AgentError as exc:
        entry.update(status="unavailable", reason=exc.message)
        engine.settle_display()
        engine.log(event="display_setup_unavailable", project=project, reason=exc.message)
        return None
    ds = session.data(project)
    asked = set(entry.get("asked") or ())
    ruled = list(entry.get("rule") or ())
    # A batch issued and not answered yet (`out`) is drawn again as it was:
    # the same rows, when the packet is re-rendered or served again.
    out = list(entry.get("out") or ())
    rows = []
    for unit in engine.units_of(project):
        if str(unit["state"]).startswith("skipped"):
            continue
        channel = views.image_channel(ds, unit["marker"])
        if channel is None or channel in ruled:
            continue
        if out and channel not in out:
            continue
        if not out and channel in asked:
            continue
        if not out and not calibration.needs_choice(record, channel):
            # Settled without a look -- by the rule, or by an earlier check
            # (an auto-contrast run): not asked, and not played on the viewer
            # again; the viewer's channels nobody set take it all the same.
            ruled.append(channel)
            continue
        words, short = _structure(engine, ds, unit)
        rows.append({"marker": unit["marker"], "channel": channel, "pattern": words,
                     "short_pattern": short})
        if len(rows) >= ENGINE["display_batch"]:
            break
    entry["rule"] = ruled
    if not rows:
        entry["status"] = "applied"
        entry.setdefault("unshown", [])
        engine.settle_display()
        return None
    try:
        rendered = display_grid.render_display_grid(session, ds, record, rows, fmt=_fmt(engine))
    except Exception as exc:  # a check that cannot be drawn leaves the rule standing
        entry.update(status="unavailable", reason=f"no grid: {exc}")
        engine.settle_display()
        engine.log(event="display_setup_unavailable", project=project, reason=str(exc))
        return None
    entry["out"] = [r["channel"] for r in rows]
    levels = entry.setdefault("levels", {})
    for row in rendered["manifest"]["rows"]:
        levels[row["channel"]] = row["levels"]
    markers = [r["marker"] for r in rows]
    packet = {
        "question": (f"How bright should {len(rows)} markers of {project} be drawn? For EVERY "
                     "row give `background_ok` (columns where the background tile is a faint "
                     "dark-grey floor: not black, not stained-looking) and `structure_ok` "
                     "(columns where the bright cells show the row's `pattern` with detail: "
                     "not a flat or red blob, not too dim). Empty lists are fine; code picks "
                     "the column."),
        "evidence": {"project": project,
                     "rows": [{"marker": r["marker"], "pattern": r["pattern"]} for r in rows],
                     **_reading(engine, ["display_setup"])},
        "allowed": {"rows": markers, "columns": list(calibration.COLUMN_IDS)},
        "_image_meta": [],
    }
    # Which channel each row draws, for the answer (kept off what is sent).
    entry.setdefault("rows", {}).update({r["marker"]: r["channel"] for r in rows})
    return packet, _images(packet, ((rendered, "display_grid",
                                     "markers in rows, five brightnesses in columns"),))


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
              "the gate (morphology | context)")
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
                     "role, compartment, lineage, whether it is binary, its morphology (cell, "
                     "or fibrous: fibres, processes or vessel walls running between cells), "
                     "and partners -- only markers of this panel, only what you are "
                     "confident of. Skip any you do not know; they are gated on their own "
                     "data."),
        "evidence": {"unresolved": unresolved,
                     "panel": [{"marker": m, "canonical": panel["entries"][m].get("canonical"),
                                "role": panel["entries"][m].get("role")}
                               for m in panel["markers"]],
                     "roles": list(context.ROLES),
                     "morphologies": list(context.MORPHOLOGIES),
                     "rule": "your entries order the markers and pick references; they never "
                             "move a gate, and the vocabulary outranks them"},
        "allowed": ["entries"],
        "_image_meta": [],
    }
    return packet, []


def replan_packet(engine, units):
    """The planning task's look at what failed (`replan.packet`). Text only:
    a picture of each suspect marker's brightest positives beside the
    excluding partner was tried and made the call worse -- the tiles of a
    population that is not there read as saturated haze, and the planner
    kept a PD1 it had called absent from the numbers six times of six
    (2026-10-11; patch `docs/internal/bench/runs/2026-10-11-replan/`)."""
    from plexora.plugins.gating.server.autogate import replan

    return replan.packet(engine, units)


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
            "absence_check": absence_check,
            "regression_confirm": regression_confirm, "t1_strip": t1_strip,
            "panel_context": panel_context, "replan": replan_packet,
            "transfer_check": transfer_check,
            "expression_setup": expression_setup, "pixel_setup": pixel_setup,
            "display_setup": display_setup}


def cell_panel(engine, units):
    """A blind panel of single cells (`hypotheses.packet`)."""
    from plexora.plugins.gating.server.autogate import hypotheses

    return hypotheses.packet(engine, units)


BUILDERS["cell_panel"] = cell_panel


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
                           references=" and ".join(references) or partner,
                           n=len(units) or len(evidence.get("rows") or ()))


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
