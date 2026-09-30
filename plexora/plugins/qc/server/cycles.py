"""Which channels were imaged together: the cycle structure QC needs.

Plexora holds channels as a flat list; cyclic imaging (CyCIF, CODEX, IBEX)
repeats a nuclear stain every cycle, and several artifacts are properties of a
cycle -- tissue lost after cycle 5, cycle 3 shifted against cycle 1. So the
cycles are inferred here, first rule that fits wins:

1. the user's override (`set_qc_cycles`): groups of channel names, or a period;
2. repeated nuclear channels at equal gaps (DNA_1 ... DNA_n every k channels);
3. a cycle suffix on most channel names (`CD3_c2`, `Ki67_cycle3`, `r4`);
4. none: cross-cycle checks report themselves unavailable, with the hint.
"""

from __future__ import annotations

import re
from collections import Counter

_CYCLE_SUFFIX = re.compile(r"[_\-\s.](?:c|cyc|cycle|r|round)(\d+)$", re.I)


def is_nuclear(name) -> bool:
    from plexora.agent import presets

    return presets.is_nuclear_name(name)


def infer(names, *, override=None) -> dict:
    """{cycles: [{index, channels, nuclear}], of_channel: {name: index},
    method, confidence, evidence}."""
    names = list(names)
    if override:
        return _from_override(names, override)
    nuclear = [i for i, name in enumerate(names) if is_nuclear(name)]
    by_repeat = _from_repeats(names, nuclear)
    by_suffix = _from_suffix(names)
    if by_repeat and by_suffix and _same_groups(by_repeat, by_suffix):
        by_repeat["confidence"] = 0.95
        by_repeat["evidence"]["agrees_with"] = "name suffix"
        return by_repeat
    if by_repeat:
        return by_repeat
    if by_suffix:
        return by_suffix
    return {"cycles": [], "of_channel": {}, "method": "none", "confidence": 0.0,
            "evidence": {"nuclear_channels": [names[i] for i in nuclear],
                         "hint": "set_qc_cycles gives the cycle groups when the names "
                                 "do not say"}}


def _result(names, groups, method, confidence, evidence):
    cycles = []
    of_channel = {}
    for index, group in enumerate(groups, start=1):
        members = [names[i] for i in group]
        nuclear = next((n for n in members if is_nuclear(n)), None)
        cycles.append({"index": index, "channels": members, "nuclear": nuclear})
        for name in members:
            of_channel[name] = index
    return {"cycles": cycles, "of_channel": of_channel, "method": method,
            "confidence": float(confidence), "evidence": evidence}


def _from_override(names, override):
    if "cycles" in override:
        groups = []
        known = {n: i for i, n in enumerate(names)}
        for group in override["cycles"]:
            groups.append([known[n] for n in group if n in known])
        groups = [g for g in groups if g]
        return _result(names, groups, "user", 1.0, {"override": "groups"})
    period = int(override.get("period") or 0)
    if period <= 0:
        return infer(names)
    groups = [list(range(start, min(start + period, len(names))))
              for start in range(0, len(names), period)]
    return _result(names, groups, "user", 1.0, {"override": "period", "period": period})


def _from_repeats(names, nuclear):
    if len(nuclear) < 2:
        return None
    gaps = [b - a for a, b in zip(nuclear, nuclear[1:])]
    if min(gaps) < 2 and len(set(gaps)) > 1:
        return None
    mode, count = Counter(gaps).most_common(1)[0]
    if mode < 1:
        return None
    confidence = 0.9 if count == len(gaps) else (0.75 if count >= len(gaps) - 1 else 0.0)
    if confidence == 0.0:
        return None
    # Each cycle starts at its nuclear channel; channels before the first
    # nuclear one join the first cycle.
    starts = list(nuclear)
    groups = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(names)
        groups.append(list(range(0 if index == 0 else start, end)))
    return _result(names, groups, "nuclear_repeats", confidence,
                   {"nuclear_channels": [names[i] for i in nuclear], "gap": mode})


def _from_suffix(names):
    found = {}
    for index, name in enumerate(names):
        match = _CYCLE_SUFFIX.search(str(name))
        if match:
            found[index] = int(match.group(1))
    if len(found) < 0.8 * len(names) or len(set(found.values())) < 2:
        return None
    groups = {}
    for index, name in enumerate(names):
        cycle = found.get(index)
        if cycle is None:
            continue
        groups.setdefault(cycle, []).append(index)
    ordered = [groups[k] for k in sorted(groups)]
    return _result(names, ordered, "name_suffix", 0.7,
                   {"cycles_named": sorted(groups)})


def _same_groups(a, b):
    return [c["channels"] for c in a["cycles"]] == [c["channels"] for c in b["cycles"]]
