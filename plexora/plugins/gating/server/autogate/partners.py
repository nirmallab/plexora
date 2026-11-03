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
            supersets of its supersets; the most specific first;
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

    def rank(table, specific):
        def key(name):
            conf, steps, _r, proposed = table[name]
            broader = specific and any(name in ups(other) for other in table if other != name)
            return (proposed, name not in gated, broader, -conf, steps,
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
    group = _group(entries, marker, rel, present, gated, told)
    out = {"co": [], "anti": []}
    for kind, table, specific in (("co", co, True), ("anti", anti, False)):
        for name in rank(table, specific):
            if group and name in group[0]:
                continue
            out[kind].append({"marker": name, "kind": kind, "relation": table[name][2],
                              "confidence": words[table[name][0]], "steps": table[name][1],
                              **({"proposed": True} if table[name][3] else {})})
    if group:
        members, conf, proposed = group
        # A proposed group stands after every stated expected partner.
        at = sum(1 for c in out["co"] if not c.get("proposed")) if proposed else 0
        out["co"].insert(at, {"marker": " or ".join(members), "members": members, "kind": "co",
                              "relation": "subset_any", "confidence": words[conf], "steps": 1,
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
            any((marker, b) in told for b in members))
