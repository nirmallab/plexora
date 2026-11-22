"""Multi-channel bright cells: one cell bright in markers of lineages no
real cell combines.

Some objects glow in every channel -- an autofluorescent, plump stromal cell
with a cytoplasmic haze, a dying cell, a fleck of debris the mask took for a
cell. In a marker's own histogram such a cell is simply bright, so it sits in
the top of CDX2, CD4, FOXP3, PDPN, CD11b and CD11c at once and no gate can
leave it out. Taken one marker at a time it is a positive; taken together it
is not a cell type.

The call (a CELL reason, `schemas.MULTI_BRIGHT_REASON`):

1. **Lit.** A marker is lit in a cell when the cell's logged value is above
   that marker's `cells.multi_bright_quantile` among the tissue's cells
   (the cells outside the Background ROI, when there is one).
2. **Lineages.** Each panel marker the vocabulary resolves to a graph family
   (`biocognia_markers`) is a lineage marker; a marker of no family (Ki67,
   PD-1, HLA-DR, CD103) is a state and never counts. Two lineage markers are
   *exclusive* when no cell is expected to carry both: their families sit in
   subtrees the graph's family-level exclusions keep apart (epithelial x
   immune, T cell x myeloid, endothelial x immune), and no `also_on`
   exception lets either marker onto the other's family (CD31 on immune
   cells, CD4 on macrophages, vimentin on almost everything). A rare
   exception weakens an exclusion by a grade, as the vocabulary reads it;
   only exclusions of `MULTI_BRIGHT["min_confidence"]` or stronger count. A
   marker-to-marker relation the vocabulary states wins: a stated subset or
   co-expression is compatible, a stated exclusion (CD8 x FOXP3) exclusive.
3. **Flag.** A cell is flagged when its lit lineage markers hold at least
   `cells.multi_bright_lineages` markers that are pairwise exclusive (the
   largest such set, a clique of the exclusion graph): that many lineages no
   cell combines. A T cell bright in CD3, CD8, CD69 and TCF1, a macrophage in
   CD163, CD206, IBA1 and HLA-DR, an epithelial cell in E-cadherin,
   beta-catenin and Na/K-ATPase is one lineage, however many markers.

**Skipped, never guessed.** The check runs only on what it needs: a cell
table with expression values, and panel markers that resolve to a lineage in
the graph (a channel the panel lacks is absent, never imputed). When the
panel's lineage markers span fewer than `cells.multi_bright_lineages`
mutually exclusive lineages, no cell could ever be called: the check is
skipped for the project, no cell gets the reason, and the record says why
("the panel covers 2 exclusive lineages; needs 3"). There is no count-only
fallback: counting lit markers without lineages flags real cells that carry
many markers of one lineage.

A pure function of the table's values, the vocabulary and the strictness
table: no image, no packet, no model call. Under another preset it is one
`calls.derive` away.
"""

from __future__ import annotations

import functools

import numpy as np

from plexora.plugins.qc.server import schemas

#: Bumped when what a call means changes (stored in the evidence).
VERSION = "1"


@functools.lru_cache(maxsize=1)
def _graph():
    from biocognia_markers import graph as _g

    return _g.load(overlays=False)


def _rank(word):
    """The graph's confidence words, 0 strongest."""
    order = ("strong", "moderate", "weak")
    return order.index(word) if word in order else 1


def _carriers(graph, mid, family):
    """{family: rank}: the families a marker's positive cells may
    be in: its own family, and each `also_on` family it holds everywhere (no
    tissue or context scope). A common exception carries the marker as its own
    family does (rank 0); a rare or weak one only weakens an exclusion by a
    grade (rank 1)."""
    from biocognia_markers import graph as _g

    from plexora.ai.vocabulary import counts

    out = {family: 0}
    for edge in graph.edges_of(mid):
        if edge.get("type") != "also_on" or edge.get("from") != mid \
                or not _g.is_family(edge.get("to")) or not graph.applies(edge) \
                or not counts(edge):
            continue
        slug = _g.family_slug(edge["to"])
        rare = edge.get("frequency") == "rare" or edge.get("confidence") == "weak"
        out[slug] = min(out.get(slug, 1), 1 if rare else 0)
    return out


def panel(markers) -> dict:
    """What the vocabulary and the graph say of each panel marker:
    {lineage: {marker: {canonical, id, family, family_words}}, states: [marker],
    skipped: {marker: why}, usable: [marker], exclusive: {(a, b): grade},
    max_lineages: the most pairwise-exclusive lineage markers the panel has,
    widest: their families}. A marker the graph gives no family (a learned
    one, a state) takes no part."""
    from plexora.ai import vocabulary

    rules = schemas.MULTI_BRIGHT
    graph = _graph()
    housekeeping = {vocabulary.fold(h) for h in rules["housekeeping"]}
    lineage, states, skipped, usable = {}, [], {}, []
    for marker in markers:
        if vocabulary.structural(marker):
            skipped[marker] = f"a {vocabulary.structural(marker)} channel"
            continue
        name = vocabulary.canonical(marker)
        found = vocabulary.load()["entries"].get(name) if name else None
        if found is not None and found.get("role") == "context":
            skipped[marker] = "a structural marker (role context)"
            continue
        if vocabulary.fold(marker) in housekeeping or (name and vocabulary.fold(name)
                                                        in housekeeping):
            skipped[marker] = "a housekeeping marker"
            continue
        usable.append(marker)
        mid = (found or {}).get("id")
        family = graph.family_of(mid) if mid else None
        if family and family in graph.families:
            lineage[marker] = {"canonical": name, "id": mid, "family": family,
                               "family_words": (graph.families[family] or {}).get("name")
                               or family}
        else:
            states.append(marker)
    exclusive = _exclusive_pairs(lineage, graph)
    names = list(lineage)
    adjacency = {m: {b for (a, b) in exclusive if a == m} for m in names}
    best = _max_clique(names, adjacency)
    return {"lineage": lineage, "states": states, "skipped": skipped, "usable": usable,
            "exclusive": exclusive, "max_lineages": len(best),
            "widest": [lineage[m]["family"] for m in best]}


def panel_for(markers: tuple) -> dict:
    """`panel` of a tuple of markers, kept per vocabulary version: the panel
    of an image is read again on every derive, the graph rarely changes."""
    from plexora.ai import vocabulary

    return _panel_cached(tuple(markers), vocabulary.version())


@functools.lru_cache(maxsize=8)
def _panel_cached(markers, _version):
    return panel(list(markers))


def _exclusive_pairs(lineage, graph) -> dict:
    """{(a, b): "strong" | "moderate"} over the lineage markers, both ways."""
    from plexora.ai import vocabulary

    floor = _rank(schemas.MULTI_BRIGHT["min_confidence"])
    excl = {}

    def excluded(slug, rank):
        key = (slug, rank)
        if key not in excl:
            word = ("strong", "moderate", "weak")[rank]
            excl[key] = graph.exclusive_of(slug, min_confidence=word)
        return excl[key]

    entries = vocabulary.load()["entries"]
    stated = {}
    for marker, info in lineage.items():
        for partner in (entries.get(info["canonical"]) or {}).get("partners") or ():
            stated[(info["canonical"], partner["marker"])] = (
                partner["relation"], partner.get("confidence") or vocabulary.DEFAULT_CONFIDENCE)
    carriers = {m: _carriers(graph, info["id"], info["family"]) for m, info in lineage.items()}
    out = {}
    names = list(lineage)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ca, cb = lineage[a]["canonical"], lineage[b]["canonical"]
            if ca == cb:
                continue
            said = [stated.get((ca, cb)), stated.get((cb, ca))]
            said = [s for s in said if s]
            if any(r in ("subset", "coexpressed", "subset_any") for r, _c in said):
                continue
            grade = None
            for relation, confidence in said:
                if relation == "exclusive" and confidence in ("high", "moderate"):
                    grade = "strong" if confidence == "high" else "moderate"
            if grade is None:
                grade = _family_exclusion(carriers[a], carriers[b], graph, excluded, floor)
            if grade is not None:
                out[(a, b)] = out[(b, a)] = grade
    return out


def _family_exclusion(ca, cb, graph, excluded, floor):
    """The grade at which no family carrying `a` can hold `b`, or None.

    Common carriers (rank 0) are where the marker's positives are: every pair
    of them must be exclusive, and a family within the other is the same
    lineage. A rare carrier (rank 1) is read as the vocabulary reads it: when
    it crosses into the other marker's lineage it weakens the exclusion by a
    grade (DC x T cell, strong, holds for CD11c x CD3 at moderate: CD11c is on
    a few CD8 T cells); one elsewhere says nothing about this pair."""

    def related(x, y):
        return x in graph.ancestors(y) or y in graph.ancestors(x)

    common_a = [f for f, r in ca.items() if r == 0]
    common_b = [f for f, r in cb.items() if r == 0]
    worst = 0
    for fa in common_a:
        for fb in common_b:
            if related(fa, fb):
                return None
            at = next((r for r in range(floor + 1) if fb in excluded(fa, r)), None)
            if at is None:
                return None
            worst = max(worst, at)
    crossed = any(related(f, fb) for f, r in ca.items() if r for fb in common_b) \
        or any(related(f, fa) for f, r in cb.items() if r for fa in common_a)
    if crossed:
        worst += 1
    if worst > floor:
        return None
    return ("strong", "moderate", "weak")[worst]


def _max_clique(nodes, adjacency):
    """The largest set of `nodes` pairwise adjacent (small graphs: a cell's lit
    lineage markers)."""
    best = []

    def grow(chosen, pool):
        nonlocal best
        if not pool:
            if len(chosen) > len(best):
                best = list(chosen)
            return
        for node in sorted(pool):   # sorted: the same clique reported every run
            if len(chosen) + len(pool) <= len(best):
                return
            grow([*chosen, node], pool & adjacency.get(node, set()))
            pool = pool - {node}

    grow([], set(nodes))
    return best


def cutoffs(values, tissue, quantile) -> dict:
    """{marker: the value above which a cell is lit}: the marker's `quantile`
    of its finite values in `tissue` (row mask, or None: every cell)."""
    out = {}
    for marker, column in values.items():
        ref = column if tissue is None else column[tissue]
        ref = ref[np.isfinite(ref)]
        out[marker] = float(np.quantile(ref, quantile)) if ref.size else float("inf")
    return out


def detect(values, *, quantile, min_lineages, tissue=None, info=None) -> tuple:
    """(flagged row mask, record). `values` {marker: logged values, row
    aligned}; `tissue` the rows the cutoffs are read in (None: every row);
    `info` a `panel` of the markers (computed when None). The record holds
    the method (graph, or skipped with `why`), the cutoffs, the markers and
    lineages lit in the flagged cells."""
    rules = schemas.MULTI_BRIGHT
    markers = list(values)
    n = int(next(iter(values.values())).shape[0]) if values else 0
    info = info or panel(markers)
    # Only the markers the panel measures that resolve to a lineage take part.
    taking_part = [m for m in info["lineage"] if m in values]
    min_lineages = int(min_lineages)
    record = {"version": VERSION, "quantile": float(quantile), "min_lineages": min_lineages,
              "space": "log", "n_flagged": 0,
              "lineage_markers": {m: info["lineage"][m]["family"] for m in taking_part},
              "not_counted": {**{m: "no lineage in the marker graph (a state)"
                                 for m in info["states"] if m in values},
                              **{m: why for m, why in info["skipped"].items() if m in values}},
              "panel_lineages": info["max_lineages"],
              "panel_widest": list(info["widest"])}
    flagged = np.zeros(n, dtype=bool)
    if tissue is not None and int(np.count_nonzero(tissue)) < rules["min_tissue_cells"]:
        tissue = None
    if not n:
        return flagged, {**record, "method": "skipped", "skipped": True,
                         "why": "the table has no cells"}
    if info["max_lineages"] < min_lineages:
        return flagged, {**record, "method": "skipped", "skipped": True,
                         "why": _short_why(info, min_lineages)}
    cut = cutoffs({m: values[m] for m in taking_part}, tissue, quantile)
    record["cutoffs"] = {m: round(v, 4) for m, v in cut.items()}
    lit = {m: np.nan_to_num(values[m], nan=-np.inf) > cut[m] for m in taking_part}
    flagged, extra = _by_lineage(lit, info, min_lineages, n)
    record.update(method="graph", skipped=False, **extra)
    record["n_flagged"] = int(flagged.sum())
    # The markers lit in the flagged cells, most often first: what the viewer
    # shows when the call is clicked.
    lit_counts = {m: int(np.count_nonzero(lit[m] & flagged)) for m in taking_part}
    record["lit_in_flagged"] = {m: c for m, c in sorted(lit_counts.items(),
                                                       key=lambda kv: -kv[1]) if c}
    # How much of each marker's own top it explains.
    record["share_of_lit"] = {m: round(lit_counts[m] / max(int(lit[m].sum()), 1), 4)
                              for m in record["lit_in_flagged"]}
    return flagged, record


def _by_lineage(lit, info, min_lineages, n):
    names = [m for m in info["lineage"] if m in lit]
    adjacency = {m: {b for (a, b) in info["exclusive"] if a == m and b in lit} for m in names}
    flagged = np.zeros(n, dtype=bool)
    if len(names) < min_lineages:
        return flagged, {"families": {}}
    matrix = np.column_stack([lit[m] for m in names])
    counted = matrix.sum(axis=1)
    rows = np.flatnonzero(counted >= min_lineages)
    families = {}
    if rows.size:
        packed = np.packbits(matrix[rows], axis=1)
        patterns, inverse = np.unique(packed, axis=0, return_inverse=True)
        inverse = np.asarray(inverse).reshape(-1)
        widths = np.zeros(patterns.shape[0], dtype=np.int64)
        cliques = []
        for k, pattern in enumerate(patterns):
            on = np.flatnonzero(np.unpackbits(pattern)[:len(names)])
            members = [names[i] for i in on]
            best = _max_clique(members, {m: adjacency[m] & set(members) for m in members})
            widths[k] = len(best)
            cliques.append(best)
        hit = widths[inverse] >= min_lineages
        flagged[rows[hit]] = True
        for k in np.flatnonzero(widths >= min_lineages):
            many = int(np.count_nonzero(inverse == k))
            for m in cliques[k]:
                family = info["lineage"][m]["family"]
                families[family] = families.get(family, 0) + many
    return flagged, {"families": dict(sorted(families.items(), key=lambda kv: -kv[1]))}


def _short_why(info, min_lineages):
    covers = info["max_lineages"]
    words = (f"the panel covers {covers} exclusive lineage{'s' if covers != 1 else ''}; "
             f"needs {min_lineages}")
    if not info["lineage"]:
        return words + " (no panel marker resolves to a lineage in the marker graph)"
    return words + f" ({', '.join(sorted(set(info['widest'])))})" if info["widest"] else words


def skipped_record(why, *, quantile, min_lineages) -> dict:
    """The record of a check that could not run (no table, no values)."""
    return {"version": VERSION, "quantile": float(quantile), "min_lineages": int(min_lineages),
            "method": "skipped", "skipped": True, "why": why, "n_flagged": 0}
