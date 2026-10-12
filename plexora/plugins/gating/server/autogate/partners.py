"""The partners a multi-view look shows beside a marker (`SessionOptions.tiles`).

A look drawn as tiles (`collage` layout `t2m`) shows, beside the marker's own
morphology and outlined context, one partner the marker is expected in (`co`)
and one it is not (`anti`). `partner_plan` picks them from the panel context's
vocabulary relations alone: no image is read here.
"""

from __future__ import annotations

#: The vocabulary's confidence words, ranked.
_CONFIDENCE = {"high": 3, "moderate": 2, "low": 1}


def partner_plan(entries, marker, present, abundance, n, gated=()):
    """[{marker, kind: co|anti, relation, confidence, steps}]: up to `n`
    channels expected in the same cell and `n` not expected, from the panel
    context's vocabulary relations (`entries[m]["partners"]`, mapped to this
    panel's names):
      co    the marker's own subset / coexpressed partners (a relation
            another marker declares about this one is not reversed), then the
            supersets of its supersets; the most specific first;
      anti  exclusive partners either way, those of the marker's supersets,
            and the subsets of a marker it excludes.
    A derived relation is as confident as its weakest step. A partner the
    scientist gated comes first (their gate vouches for the channel), then
    confidence, then the partner more cells are positive for (`abundance`).
    One partner per vocabulary entry: two channels read as the same marker
    say one thing twice."""
    rel = {}
    for name, entry in entries.items():
        for p in (entry or {}).get("partners") or []:
            rel[(name, p["marker"])] = (p["relation"], _CONFIDENCE.get(p.get("confidence"), 1))

    def ups(a):
        return {b: conf for (x, b), (r, conf) in rel.items() if x == a and r == "subset"}

    def downs(a):
        return {x: conf for (x, b), (r, conf) in rel.items() if b == a and r == "subset"}

    def excl(a):
        out = {}
        for (x, b), (r, conf) in rel.items():
            if r == "exclusive" and a in (x, b):
                other = b if x == a else x
                out[other] = max(out.get(other, 0), conf)
        return out

    co = {}
    for (x, b), (r, conf) in rel.items():
        if x == marker and r in ("subset", "coexpressed"):
            co[b] = (conf, 1, r)
    supers = dict(ups(marker))
    for b, conf in list(supers.items()):
        for b2, conf2 in ups(b).items():
            if b2 != marker:
                supers.setdefault(b2, min(conf, conf2))
                co.setdefault(b2, (min(conf, conf2), 2, "subset"))
    anti = {b: (conf, 1, "exclusive") for b, conf in excl(marker).items()}
    for s_, conf in supers.items():
        for b, conf2 in excl(s_).items():
            anti.setdefault(b, (min(conf, conf2), 2, "exclusive"))
    for b, (conf, steps, _r) in list(anti.items()):
        for sub, conf2 in downs(b).items():
            anti.setdefault(sub, (min(conf, conf2), steps + 1, "exclusive"))
    for table in (co, anti):
        for name in [k for k in table if k == marker or k not in present]:
            del table[name]
    for name in list(anti):
        if name in co:
            del anti[name]

    def rank(table, specific):
        def key(name):
            conf, steps, _r = table[name]
            broader = specific and any(name in ups(other) for other in table if other != name)
            return (name not in gated, broader, -conf, steps, -(abundance.get(name) or 0.0), name)
        return sorted(table, key=key)[:n]

    def dedupe(table):
        seen = {}
        for name in sorted(table, key=lambda k: (
                (entries.get(k) or {}).get("canonical") != k, -table[k][0], table[k][1],
                -(abundance.get(k) or 0.0), k)):
            seen.setdefault((entries.get(name) or {}).get("canonical") or name, name)
        for name in [k for k in table if k not in seen.values()]:
            del table[name]
    dedupe(co)
    dedupe(anti)

    words = {v: k for k, v in _CONFIDENCE.items()}
    return [{"marker": name, "kind": kind, "relation": table[name][2],
             "confidence": words[table[name][0]], "steps": table[name][1]}
            for kind, table, specific in (("co", co, True), ("anti", anti, False))
            for name in rank(table, specific)]
