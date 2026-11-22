"""The partners a multi-view look shows beside a marker (`SessionOptions.tiles`).

A look drawn as tiles (`collage` layout `t2m`) shows, beside the marker's own
morphology and outlined context, one partner the marker is expected in (`co`)
and one it is not (`anti`). `partner_plan` picks them from the panel context's
vocabulary relations alone: no image is read here.

A marker carried by several lineages (`subset_any`: TIM-3 on T, NK or
myeloid cells) is expected in any of them, so its `co` is the group, drawn
as one tile (`members`) -- unless the panel lacks one of the lineages
(`subset_any_missing`, or a member without a channel), when positives
outside the members are expected and the plan falls back to the pairs.
"""

from __future__ import annotations

#: The vocabulary's confidence words, ranked.
_CONFIDENCE = {"high": 3, "moderate": 2, "low": 1}


def partner_plan(entries, marker, present, abundance, n, gated=(), dropped=()):
    """[{marker, kind: co|anti, relation, confidence, steps}]: up to `n`
    channels expected in the same cell and `n` not expected, from the panel
    context's vocabulary relations (`entries[m]["partners"]`, mapped to this
    panel's names):
      co    the marker's `subset_any` group first, as one entry whose
            `members` are drawn together (`marker` names them joined by
            " or "); then its own subset / coexpressed partners (a relation
            another marker declares about this one is not reversed), then the
            supersets of its supersets; the most specific first. A broad
            lineage marker with none of these (CD3, PanCK) gets the lineage
            markers that declare themselves inside it instead (`contains`:
            "CD4 or CD8a" for CD3), never a state; failing that, up to
            `_MAX_SUPPORTS` lineage markers whose positives sit inside its
            own (`supports`: CD163 for IBA1) -- a tile whose light supports
            a cell's own stain and whose dark says nothing;
      anti  exclusive partners either way, those of the marker's supersets,
            and the subsets of a marker it excludes.
    A derived relation is as confident as its weakest step, and never one a
    partner review dropped for this sample (`dropped`: `context.dropped_pairs`).
    A partner the scientist gated comes first (their gate vouches for the
    channel), then confidence, then the partner more cells are positive for
    (`abundance`). One partner per vocabulary entry: two channels read as the
    same marker say one thing twice."""
    # `told`: the pairs a planner proposed for the tiles only (`proposed`).
    # Each ranks after every stated partner of its kind, and anything derived
    # through one is proposed too.
    rel, told = {}, set()
    for name, entry in entries.items():
        for p in (entry or {}).get("partners") or []:
            rel[(name, p["marker"])] = (p["relation"], _CONFIDENCE.get(p.get("confidence"), 1))
            if p.get("proposed"):
                told.add((name, p["marker"]))

    def ups(a):
        return {b: (conf, (a, b) in told) for (x, b), (r, conf) in rel.items()
                if x == a and r == "subset"}

    def downs(a):
        return {x: (conf, (x, a) in told) for (x, b), (r, conf) in rel.items()
                if b == a and r == "subset"}

    def excl(a):
        out = {}
        for (x, b), (r, conf) in rel.items():
            if r == "exclusive" and a in (x, b):
                other = b if x == a else x
                mine, old = (conf, (x, b) in told), out.get(other)
                # A stated exclusion over a proposed one, then the surer.
                if old is None or (mine[1], -mine[0]) < (old[1], -old[0]):
                    out[other] = mine
        return out

    co = {}
    for (x, b), (r, conf) in rel.items():
        if x == marker and r in ("subset", "coexpressed"):
            co[b] = (conf, 1, r, (x, b) in told)
    supers = dict(ups(marker))
    for b, (conf, said) in list(supers.items()):
        for b2, (conf2, said2) in ups(b).items():
            if b2 != marker:
                supers.setdefault(b2, (min(conf, conf2), said or said2))
                co.setdefault(b2, (min(conf, conf2), 2, "subset", said or said2))
    anti = {b: (conf, 1, "exclusive", said) for b, (conf, said) in excl(marker).items()}
    for s_, (conf, said) in supers.items():
        for b, (conf2, said2) in excl(s_).items():
            anti.setdefault(b, (min(conf, conf2), 2, "exclusive", said or said2))
    for b, (conf, steps, _r, said) in list(anti.items()):
        for sub, (conf2, said2) in downs(b).items():
            anti.setdefault(sub, (min(conf, conf2), steps + 1, "exclusive", said or said2))
    for table, relations in ((co, ("subset", "coexpressed")), (anti, ("exclusive",))):
        for name in [k for k in table if k == marker or k not in present
                     or any((marker, k, r) in dropped for r in relations)]:
            del table[name]
    for name in list(anti):
        if name in co:
            del anti[name]

    group = _group(entries, marker, rel, present, gated, told)
    if group is None and not co:
        # A broad lineage marker (CD3, PanCK) is no other marker's subset, so
        # it has no expected partner of its own; the lineages beneath it on
        # this panel (CD4 or CD8 inside CD3, ECAD beside PanCK) are where its
        # positives are, drawn as one tile. Never a state beneath it (FOXP3,
        # PD-1): only some of its cells carry one.
        group = _beneath(entries, marker, rel, present, gated, told, dropped)
        if group is None:
            # No lineage beneath it carries its cells (IBA1: only CD163, which
            # many IBA1+ macrophages lack): the markers whose positives sit
            # inside its positives, as support only (`_supports`).
            group = _supports(entries, marker, rel, present, gated, told, dropped)
        if group and len(group[0]) == 1:
            (only,), conf, proposed, relation = group
            co[only] = (conf, 1, relation, proposed)
            group = None
    # The expected tile shows the cells the marker's positives belong to. A
    # not-expected partner that lives in those same cells -- a sibling, as
    # CD8 is beside CD4 inside CD3 -- is the only tile that tells the two
    # apart; one the expected tile already rules out (CD20: its cells are
    # CD3-negative) says the same thing twice. Siblings rank first among the
    # not-expected, before a gated partner: a gate vouches for a channel, a
    # sibling is what the reader cannot judge without. Only the expected
    # partner the tile shows counts (`shown`, set below once `co` is ranked):
    # every cell is inside a broad partner such as CD45 or b2m.
    shown = set()

    def sibling(name):
        return any((name, b) in rel and rel[(name, b)][0] in ("subset", "coexpressed") for b in shown)

    def rank(table, specific):
        def key(name):
            conf, steps, _r, proposed = table[name]
            broader = specific and any(name in ups(other) for other in table if other != name)
            # Of two siblings, the broader: FOXP3 is inside CD4, so CD4 tells more cells apart.
            narrower = not specific and sibling(name) and any(
                name in downs(other) for other in table if other != name and sibling(other))
            return (proposed, specific or not sibling(name), narrower, name not in gated, broader, -conf, steps,
                    -(abundance.get(name) or 0.0), name)
        return sorted(table, key=key)

    def dedupe(table):
        seen = {}
        for name in sorted(table, key=lambda k: (
                table[k][3], (entries.get(k) or {}).get("canonical") != k, -table[k][0],
                table[k][1], -(abundance.get(k) or 0.0), k)):
            seen.setdefault((entries.get(name) or {}).get("canonical") or name, name)
        for name in [k for k in table if k not in seen.values()]:
            del table[name]
    dedupe(co)
    dedupe(anti)

    words = {v: k for k, v in _CONFIDENCE.items()}
    # The expected tile: the subset_any group when one leads, else the first expected partner.
    lead = [b for b in rank(co, True) if not (group and b in group[0])]
    if group and not group[2]:
        shown.update(group[0])
    elif lead and not co[lead[0]][3]:
        shown.add(lead[0])
    elif group:
        shown.update(group[0])
    out = {"co": [], "anti": []}
    for kind, table, specific in (("co", co, True), ("anti", anti, False)):
        for name in rank(table, specific):
            if group and name in group[0]:
                continue
            out[kind].append({"marker": name, "kind": kind, "relation": table[name][2],
                              "confidence": words[table[name][0]], "steps": table[name][1],
                              **({"proposed": True} if table[name][3] else {})})
    if group:
        members, conf, proposed, relation = group
        # A proposed group stands after every stated expected partner.
        at = sum(1 for c in out["co"] if not c.get("proposed")) if proposed else 0
        out["co"].insert(at, {"marker": " or ".join(members), "members": members, "kind": "co",
                              "relation": relation, "confidence": words[conf], "steps": 1,
                              **({"proposed": True} if proposed else {})})
    return out["co"][:n] + out["anti"][:n]


def _group(entries, marker, rel, present, gated, told=()):
    """(members, confidence, proposed) of the marker's `subset_any` group on
    this panel, or None: when it has none, when the vocabulary names a
    lineage this panel does not mark, or when a member has no image channel
    -- a group missing a lineage would draw that lineage's positives dark.
    Gated members first; one channel per vocabulary entry."""
    names = [b for (x, b), (r, _c) in rel.items() if x == marker and r == "subset_any"]
    if len(names) < 2 or (entries.get(marker) or {}).get("subset_any_missing") \
            or any(b not in present for b in names):
        return None
    seen, members = set(), []
    for name in sorted(names, key=lambda k: (k not in gated, k)):
        key = (entries.get(name) or {}).get("canonical") or name
        if key not in seen:
            seen.add(key)
            members.append(name)
    return (members, min(rel[(marker, b)][1] for b in members),
            any((marker, b) in told for b in members), "subset_any")


#: The roles whose markers name a lineage (`context.CARRIER_ROLES`).
_LINEAGE_ROLES = ("lineage_reliable", "lineage_other", "tumour_stromal")
#: The most lineages one `contains` tile draws together.
_MAX_BENEATH = 3


def _beneath(entries, marker, rel, present, gated, told, dropped=()):
    """(members, confidence, proposed, "contains") of the lineage markers
    that declare themselves inside `marker` on this panel (`subset`, or
    `coexpressed` read beneath it: CD8 and CD4 for CD3, ECAD for PanCK), or
    None. Only for a lineage marker, and only lineage markers beneath it: a
    state's carriers are some of the lineage's cells, never where its
    positives are. And only when one of them is `coexpressed` with it --
    most cells of each positive for the other -- the one statement that
    says the marker's own cells carry it: CD66b alone inside CD11b, or CD163
    inside IBA1, would draw most of the marker's positives dark. Gated
    first, then the surer, at most `_MAX_BENEATH`; one channel per
    vocabulary entry."""
    if (entries.get(marker) or {}).get("role") not in _LINEAGE_ROLES:
        return None
    found = {}
    for (x, b), (r, conf) in rel.items():
        if b != marker or x == marker or x not in present or r not in ("subset", "coexpressed"):
            continue
        if (entries.get(x) or {}).get("role") not in _LINEAGE_ROLES:
            continue
        if any((x, marker, d) in dropped or (marker, x, d) in dropped
               for d in ("subset", "coexpressed")):
            continue
        found[x] = (conf, (x, b) in told, r)
    if not any(f[2] == "coexpressed" for f in found.values()):
        return None
    seen, members = set(), []
    for name in sorted(found, key=lambda k: (k not in gated, found[k][1], -found[k][0], k)):
        key = (entries.get(name) or {}).get("canonical") or name
        if key not in seen:
            seen.add(key)
            members.append(name)
    members = members[:_MAX_BENEATH]
    return (members, min(found[m][0] for m in members), any(found[m][1] for m in members),
            "contains")


#: The relation of a supporting tile (`_supports`), and the most markers one
#: draws together.
SUPPORTS = "supports"
_MAX_SUPPORTS = 2
#: A marker expected on at least this share of cells (its `expected_fraction`'s
#: lower bound: HLA-A on every nucleated cell) gets no supporting tile: a
#: lineage inside it says nothing about whose stain a cell carries.
_UBIQUITOUS = 0.5
#: Roles that never get a supporting tile: a context channel (a membrane or
#: tubulin stain on most cells) is no population.
_NO_SUPPORT_ROLES = ("context",)


def _supports(entries, marker, rel, present, gated, told, dropped=()):
    """(members, confidence, proposed, "supports") of the panel's lineage
    markers whose positives sit inside `marker`'s, or None: those declaring
    themselves its `subset` (CD163 inside IBA1, CD66b inside CD11b, CD19 and
    CD11c inside HLA-DR), and those whose `subset_any` carriers on this panel
    all lie within it (the marker itself, or a subset of it), with none of
    the group's lineages missing from the panel. Only for a marker with no
    expected partner and no `contains` tile, and only on stated relations:
    a pair a planner proposed for another marker's tiles (`told`: CD11b
    inside IBA1, "monocytes and macrophages carrying CD11b") was said about
    that marker's cells, not this one's.

    Support only. Unlike `contains`, it does not say where the marker's
    positives are -- many IBA1+ macrophages are CD163-negative -- so a reader
    is told that the tile lit inside a cell's outline supports its own stain
    and that a dark tile says nothing. It never counts against a cell, never
    enters a gate's purity or contradiction numbers (those read the panel
    context's relations, to which this adds none), and never moves a gate.
    At most `_MAX_SUPPORTS`: gated first, then the broadest (one another
    candidate sits inside), the surer, the one more markers sit inside; one
    channel per vocabulary entry."""
    entry = entries.get(marker) or {}
    if entry.get("role") in _NO_SUPPORT_ROLES:
        return None
    fraction = entry.get("expected_fraction")
    if isinstance(fraction, (list, tuple)) and fraction and fraction[0] is not None \
            and float(fraction[0]) >= _UBIQUITOUS:
        return None

    def within(x):
        return x == marker or (rel.get((x, marker)) or ("",))[0] in ("subset", "coexpressed")

    found = {}
    for (x, b), (r, conf) in rel.items():
        if b != marker or x == marker or x not in present or (x, b) in told \
                or (entries.get(x) or {}).get("role") not in _LINEAGE_ROLES:
            continue
        if any((x, marker, d) in dropped or (marker, x, d) in dropped
               for d in ("subset", "subset_any")):
            continue
        if r == "subset":
            found[x] = max(found.get(x, 0), conf)
        elif r == "subset_any":
            carriers = [c for (y, c), (r2, _c2) in rel.items() if y == x and r2 == "subset_any"]
            if (entries.get(x) or {}).get("subset_any_missing") \
                    or not all(c in present or c == marker for c in carriers) \
                    or not all(within(c) for c in carriers):
                continue
            found[x] = max(found.get(x, 0), min(rel[(x, c)][1] for c in carriers))
    if not found:
        return None

    def broader(name):
        return any((rel.get((other, name)) or ("",))[0] == "subset" for other in found
                   if other != name)

    def breadth(name):
        return sum(1 for (_y, b), (r, _c) in rel.items() if b == name and r == "subset")

    seen, members = set(), []
    for name in sorted(found, key=lambda k: (k not in gated, not broader(k), -found[k],
                                             -breadth(k), k)):
        key = (entries.get(name) or {}).get("canonical") or name
        if key not in seen:
            seen.add(key)
            members.append(name)
    members = members[:_MAX_SUPPORTS]
    return members, min(found[m] for m in members), False, SUPPORTS


def expected_words(panels) -> str | None:
    """How a look names its expected tile: the partner, or "supporting X"
    for a supporting tile (`SUPPORTS`), whose dark says nothing."""
    names = (panels or {}).get("markers") or {}
    co = names.get("co")
    if co and ((panels or {}).get("relations") or {}).get("co") == SUPPORTS:
        return f"supporting {co}"
    return co


def tiles_of(panels):
    """[{marker, channel, panel: co|anti, relation, gate?, members?}]: the
    partner tiles `Engine.partner_panels` laid out for a look, one row a
    marker (a `subset_any` group is one tile, one row per member, each
    naming the group's `members`). [] for a look without tiles."""
    out = []
    panels = panels or {}
    names = panels.get("markers") or {}
    relations = panels.get("relations") or {}
    groups = panels.get("members") or {}
    gates = panels.get("gates") or {}
    for kind in ("co", "anti"):
        channels = panels.get(kind)
        if not names.get(kind) or not channels:
            continue
        members = groups.get(kind)
        channels = channels if isinstance(channels, list) else [channels]
        markers = members if members else [names[kind]]
        for marker, channel in zip(markers, channels):
            row = {"marker": marker, "channel": channel, "panel": kind,
                   "relation": relations.get(kind) or ("exclusive" if kind == "anti"
                                                       else "subset")}
            if gates.get(channel) is not None:
                row["gate"] = float(gates[channel])
            if members:
                row["members"] = list(members)
            out.append(row)
    return out
