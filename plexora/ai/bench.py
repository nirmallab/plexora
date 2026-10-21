"""`plexora ai bench gating`: how well automatic gating gates, and at what cost.

Arms, per marker of each image:

- `gmm`      the Auto button's gate (3-component mixture, top vs rest);
- `profile`  the profile's gate (split-aware pools), no look at all;
- `session`  a full gating session answered by a scripted agent -- `oracle`
             (answers from the truth: the pipeline's ceiling), `lazy` (always
             "looks fine": what the deterministic layer catches on its own), or
             `noisy:P` (wrong about the direction with probability P: whether
             the guards hold).

Truth is either a synthetic scene's phenotypes (`--synthetic`), an expert's
gates stored in the project's AnnData (`--truth uns:<table>`), or a reference
run's gates in a JSON file (`--truth json:<path>`, `{"markers": {name: {low,
state}}}`: the accepted ones are the truth -- how a cheaper model's run is
scored against a trusted one). The primary
endpoint is downstream classification agreement -- the share of cells whose
whole positive/negative code across the gated markers matches the truth --
because two gates in an empty valley are the same gate for every analysis
that follows, and no threshold is identifiable inside an overlap. Per-marker
F1, Cohen's kappa, MCC and positive-fraction error explain it; cost is packets,
characters, pixels (vision tokens ~ pixels/750) and seconds.

A real model is benchmarked by running it over MCP (the gate-image skill) and
then scoring its session: `--score-session <id>`.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path

import numpy as np

from plexora.agent.sessions import budget as budgets

ARMS = ("gmm", "profile", "session")

#: The scripted agents (`noisy` takes a probability: `noisy:0.3`).
AGENT_STYLES = ("oracle", "lazy", "noisy")

#: Where a project's expert gates are read from unless told otherwise.
TRUTH_TABLE = "gates"
TRUTH_DEFAULT = f"uns:{TRUTH_TABLE}"


def _oracle_confidence():
    """What the scripted agent says it is: the surest word the engine has."""
    from plexora.plugins.gating.server.autogate.schemas import AI_CONFIDENCE

    return max(AI_CONFIDENCE, key=AI_CONFIDENCE.get)


def interval_verdicts(values, truth, intervals, *, flip=None):
    """{row: word} for T4 rows from per-cell truth (`schemas.ROW_VERDICTS`):
    the share of truly positive cells among those between the row's
    thresholds -- `own_stain` when most are, `unstained` when few are, else
    `mixed`. `flip(row)` returning True inverts a clear word (a noisy
    agent)."""
    values = np.asarray(values, dtype=np.float32)
    truth = np.asarray(truth, dtype=bool)
    out = {}
    for row in intervals:
        lo, hi = sorted((float(row["from"]), float(row["to"])))
        inside = (values > np.float32(lo)) & (values <= np.float32(hi))
        share = float(truth[inside].mean()) if inside.any() else 0.5
        verdict = ("own_stain" if share >= 0.6 else
                   "unstained" if share <= 0.4 else "mixed")
        if flip is not None and verdict != "mixed" and flip(row["row"]):
            verdict = "unstained" if verdict == "own_stain" else "own_stain"
        out[row["row"]] = verdict
    return out


# -- scoring -----------------------------------------------------------------------


def classification(values, truth, low, high=None) -> dict:
    """Per-cell agreement of `low < v <= high` (float32) with the truth."""
    from plexora.agent import gate_rule

    values = np.asarray(values, dtype=np.float32)
    finite = np.isfinite(values)
    high = float(np.nanmax(values)) if high is None else float(high)
    called = gate_rule.passes(values, low, high)[finite]
    truth = np.asarray(truth, dtype=bool)[finite]
    tp = int((called & truth).sum())
    fp = int((called & ~truth).sum())
    fn = int((~called & truth).sum())
    tn = int((~called & ~truth).sum())
    n = tp + fp + fn + tn
    f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 1.0
    po = (tp + tn) / n if n else 1.0
    pe = (((tp + fp) * (tp + fn)) + ((fn + tn) * (fp + tn))) / (n * n) if n else 0.0
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    denominator = math.sqrt(float((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)))
    mcc = (tp * tn - fp * fn) / denominator if denominator else (1.0 if fp + fn == 0 else 0.0)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "f1": f1, "kappa": kappa, "mcc": mcc,
            "fraction": (tp + fp) / n if n else None,
            "fraction_truth": (tp + fn) / n if n else None,
            "fraction_error": abs((tp + fp) - (tp + fn)) / n if n else None}


def code_agreement(values, truth, gates) -> float | None:
    """Share of cells whose positive/negative code over `gates` matches truth."""
    from plexora.agent import gate_rule

    markers = [m for m in gates if gates[m] is not None]
    if not markers:
        return None
    n = len(next(iter(values.values())))
    ok = np.ones(n, dtype=bool)
    for marker in markers:
        v = np.asarray(values[marker], dtype=np.float32)
        ok &= gate_rule.passes(v, gates[marker], float(np.nanmax(v))) == \
            np.asarray(truth[marker], dtype=bool)
    return float(ok.mean())


# -- scripted agents -----------------------------------------------------------------


#: How a scripted agent words a direction it knows from the truth, row by row
#: (`verdicts.direction_from_rows` reads each back as that direction).
ROWS_FOR = {"too_low": {"below": "unstained", "near": "unstained", "above": "unstained"},
            "too_high": {"below": "own_stain", "near": "own_stain", "above": "own_stain"},
            "about_right": {"below": "unstained", "near": "mixed", "above": "own_stain"}}


def rows_for(direction, evidence) -> dict:
    """{rows, pattern} a scripted first look answers for `direction`: the rows
    the packet has, and the first pattern the marker's compartment allows."""
    from plexora.plugins.gating.server.autogate import schemas

    keys = list((evidence or {}).get("rows") or ("below", "near", "above"))
    words = ROWS_FOR[direction]
    compartment = ((evidence or {}).get("context") or {}).get("compartment")
    patterns = (schemas.COMPARTMENT_POLICY.get(compartment) or {}).get("patterns") \
        or ("membrane",)
    return {"rows": {k: words[k] for k in keys if k in words}, "pattern": patterns[0]}


class TruthAgent:
    """Answers packets from known per-cell truth (see the module docstring);
    `noisy:P` turns a word with probability P."""

    def __init__(self, values, truth, style=AGENT_STYLES[0], seed=0, ids=None):
        self.values = {m: np.asarray(v, dtype=np.float32) for m, v in values.items()}
        # Cell id -> row, when known: a panel's cells are then read by their
        # own truth, not by the truth's best single cut.
        self.rows = None if ids is None else {int(i): r for r, i in enumerate(ids)}
        self.truth = {m: np.asarray(t, dtype=bool) for m, t in truth.items()}
        self.style, self.p = (style.split(":") + ["0.2"])[:2] if style.startswith("noisy") \
            else (style, "0")
        self.p = float(self.p)
        self.rng = np.random.default_rng(seed)
        self._best = {}

    def _errors(self, marker, low):
        called = self.values[marker] > np.float32(low)
        return int((called ^ self.truth[marker]).sum())

    def best(self, marker):
        if marker not in self._best:
            v = np.sort(self.values[marker][np.isfinite(self.values[marker])])
            cuts = np.concatenate([[v[0] - 1], (v[:-1] + v[1:]) / 2])
            if cuts.size > 4000:
                cuts = cuts[np.linspace(0, cuts.size - 1, 4000).astype(int)]
            errors = np.array([self._errors(marker, c) for c in cuts])
            i = int(np.argmin(errors))
            self._best[marker] = (float(cuts[i]), int(errors[i]))
        return self._best[marker]

    def direction(self, marker, low):
        best, err = self.best(marker)
        excess = self._errors(marker, low) - err
        if excess <= max(2, 0.01 * self.values[marker].size):
            return "about_right"
        return "too_low" if low < best else "too_high"

    def panel_labels(self, marker, packet) -> dict:
        """A blind panel read by each cell's own truth (the scene's, by cell
        id), or, without ids, against the truth's best single cut; `noisy:P`
        turns each cell's word with probability P, `lazy` cannot tell any.
        The cells are the stored packet's (`_panel`), which the reader never
        sees."""
        cells = (packet.get("_panel") or {}).get("cells") or []
        if self.style == "lazy":
            return {c["number"]: "uncertain" for c in cells}
        cut, _errors = self.best(marker)
        out = {}
        for c in cells:
            row = None if self.rows is None else self.rows.get(int(c["cell_id"]))
            own = bool(self.truth[marker][row]) if row is not None \
                else float(c["value"]) > cut
            if self.style == "noisy" and self.rng.random() < self.p:
                own = not own
            out[c["number"]] = "own_stain" if own else "not_own_stain"
        return out

    def answer(self, packet):
        kind = packet["kind"]
        ev = packet.get("evidence") or {}
        marker = packet["units"][0]["marker"] if packet.get("units") else None
        if kind == "t1_strip":
            return {"kind": kind, "verdicts": {
                r["marker"]: ("ok" if self.style == "lazy"
                              or self.direction(r["marker"], r["gate"]) == "about_right"
                              else "suspicious") for r in ev.get("markers") or []}}
        if kind in ("t2_confirm", "t3_biological"):
            direction = self.direction(marker, ev["candidate"]["low"])
            if self.style == "lazy":
                direction = "about_right"
            elif self.style == "noisy" and self.rng.random() < self.p:
                direction = {"too_low": "too_high", "too_high": "too_low",
                             "about_right": "too_low"}[direction]
            return {"kind": kind, "confidence": _oracle_confidence(), **rows_for(direction, ev)}
        if kind == "t4_candidates":
            rows = ev.get("intervals") or []
            if self.style == "lazy":
                # "Looks fine": no row says move, none says turn back.
                return {"kind": kind, "intervals": {r["row"]: "cannot_tell" for r in rows},
                        "confidence": _oracle_confidence()}
            noisy = self.style == "noisy"
            verdicts = interval_verdicts(
                self.values[marker], self.truth[marker], rows,
                flip=(lambda _row: self.rng.random() < self.p) if noisy else None)
            return {"kind": kind, "intervals": verdicts, "confidence": "fairly_sure"}
        if kind == "cell_panel":
            return {"kind": kind, "cells": self.panel_labels(marker, packet)}
        if kind == "qc_confirm":
            real = bool(self.truth.get(marker, np.zeros(1, bool)).any())
            return {"kind": kind, "verdict": "real_signal" if real else "technical_failure"}
        if kind == "absence_check":
            # Every region shows the population the truth holds, or none.
            real = bool(self.truth.get(marker, np.zeros(1, bool)).any())
            regions = (packet.get("allowed") or {}).get("regions") or ()
            return {"kind": kind, "regions": {r: "clear" if real else "none" for r in regions}}
        if kind == "regression_confirm":
            return {"kind": kind, "verdict": "holds"}
        if kind == "pixel_setup":
            # An uncalibrated image's size check: the scenes paint nuclei at
            # the size the estimate assumes.
            return {"kind": kind, "basis": "estimate_confirmed"}
        if kind == "display_setup":
            # A simulated reader cannot see the grid: it accepts the darker
            # half of the ladder for both questions, so the rule's own column
            # (c3) is chosen wherever the numbers allow it.
            rows = [r["marker"] for r in ev.get("rows") or []]
            return {"kind": kind, "background_ok": {m: ["c1", "c2", "c3"] for m in rows},
                    "structure_ok": {m: ["c2", "c3"] for m in rows}}
        if kind == "transfer_check":
            project = ev["this"]["project"]
            direction = self.direction(marker, ev["this"]["aligned_gate"])
            return {"kind": kind, "per_image": {
                project: "holds" if direction == "about_right" or self.style == "lazy"
                else direction}}
        if kind == "replan":
            # A simulated reader knows the cells, not the biology: everything
            # it is asked to re-plan stays with a person.
            allowed = packet.get("allowed") or {}
            return {"kind": kind,
                    "markers": {k: "keep_for_review" for k in allowed.get("markers") or ()},
                    "images": {p: "not_image_level" for p in allowed.get("images") or ()}}
        return {"kind": "panel_context", "entries": []}


# -- variants -------------------------------------------------------------------------

#: The ablation's variants: what each sets of a session's options. `tiles`
#: draws every look as the 2 x 2 multi-view block (`SessionOptions.tiles`).
VARIANTS = {"baseline": {}, "tiles": {"tiles": True}, "absence": {"absence_check": True},
            "check": {"classifier": "check"}, "no_classifier": {"classifier": "off"},
            "candidates": {"engine": "candidates"},
            "candidates_2": {"engine": "candidates", "panel_looks": 2}}


def variant_arm(variant, variants) -> str:
    """The arm a variant's session is scored under: `session` for a lone
    baseline (as before the ablation existed), else `session:<variant>`."""
    return "session" if tuple(variants) == ("baseline",) else f"session:{variant}"


def check_variants(variants):
    unknown = [v for v in variants if v not in VARIANTS]
    if unknown:
        raise ValueError(f"unknown variants {unknown}; choose from {sorted(VARIANTS)}")
    return tuple(variants)


def cause_scores(values, truth, low, cell_truth, *, high=None) -> dict:
    """Per marker, where a gate's errors fall against a scene's painted
    per-cell causes: false-positive rates in dense and sparse tissue and in
    raised and flat background, the recall of dim positives, and the
    false-positive rate among neighbour-lit (contaminated) negatives against
    clean ones."""
    from plexora.agent import gate_rule

    values = np.asarray(values, dtype=np.float32)
    finite = np.isfinite(values)
    high = float(np.nanmax(values)) if high is None else float(high)
    called = gate_rule.passes(values, low, high) & finite
    positive = np.asarray(truth, dtype=bool)
    ct = cell_truth or {}

    def rate(mask):
        mask = mask & finite
        n = int(mask.sum())
        return (float(called[mask].mean()) if n else None), n

    out = {}
    if ct.get("density_region") is not None:
        region = np.asarray(ct["density_region"])
        out["fp_rate_dense"], out["n_neg_dense"] = rate(~positive & (region == "dense"))
        out["fp_rate_sparse"], out["n_neg_sparse"] = rate(~positive & (region == "sparse"))
    if ct.get("background_region") is not None:
        background = np.asarray(ct["background_region"]).astype(str)
        out["fp_rate_low_snr"], _ = rate(~positive & (background != "flat"))
        out["fp_rate_high_snr"], _ = rate(~positive & (background == "flat"))
    if ct.get("dim") is not None:
        out["dim_recall"], out["n_dim"] = rate(positive & np.asarray(ct["dim"], dtype=bool))
    if ct.get("contaminated") is not None:
        contaminated = np.asarray(ct["contaminated"], dtype=bool)
        out["contaminated_fp_rate"], out["n_contaminated"] = rate(contaminated)
        out["clean_fp_rate"], _ = rate(~positive & ~contaminated)
    return out


# -- running the arms ------------------------------------------------------------------


def _ok(outcome):
    if not outcome["ok"]:
        raise RuntimeError(json.dumps(outcome["error"], default=str)[:2000])
    return outcome["result"]


def run_session(session, project, markers, agent, *, mode="apply", limit=400, options=None):
    """Drive one gating session to the end; returns (units, status, seconds)."""
    from plexora.agent import invoke, jobs

    started_at = time.perf_counter()
    started = _ok(invoke(session, "gating_session_start", {
        "scope": "project", "project": project, "markers": list(markers), "mode": mode,
        **(options or {})}))
    jobs.drain(600)
    sid = started["session_id"]
    from plexora.plugins.gating.server.autogate import packets as packet_views

    held, sent = {}, {"packets": 0, "chars": 0, "images": 0, "pixels": 0}
    result = _ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 30}))
    for _ in range(limit):
        if result["state"] != "decision":
            break
        packet = result["packet"]
        # What the agent was sent, before a reader fills in its `as_in`s.
        sent["packets"] += 1
        sent["chars"] += len(json.dumps(packet, default=str, separators=(",", ":")))
        sent["images"] += len(packet.get("images") or [])
        sent["pixels"] += sum(int(i.get("width") or 0) * int(i.get("height") or 0)
                              for i in packet.get("images") or [])
        packet = packet_views.resolve_as_in(packet, held)
        if packet.get("kind") == "cell_panel":
            # The panel's cells as code holds them (never sent to a reader):
            # a simulated reader answers from their truth.
            from plexora.plugins.gating.server.autogate import engine as engines

            stored, _ = engines.store().read_packet(sid, packet["packet_id"])
            packet = {**packet, "_panel": stored.get("_panel")}
        result = _ok(invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"],
            "answer": agent.answer(packet)}))["next"]
    status = _ok(invoke(session, "gating_session_status", {"session_id": sid,
                                                           "known_guide": started.get(
                                                               "guide_version")}))
    status["sent_to_agent"] = sent
    seconds = time.perf_counter() - started_at
    units = {u["marker"]: u for u in status["units"] if u["project"] == project}
    return units, status, seconds, sid


def request_counts(units) -> dict:
    """{requests_made, requests_served} over a session's units: how often a
    look asked for the evidence that would settle it, and got it."""
    made = [r for u in units for r in u.get("requests") or []]
    return {"requests_made": len(made),
            "requests_served": sum(1 for r in made if r.get("served"))}


def _session_units(session_id):
    from plexora.plugins.gating.server.autogate import engine

    try:
        return list(engine.store().load(session_id)["units"].values())
    except Exception:
        return []


def gates_for_arms(session, project, markers, agent, arms, *, variants=("baseline",),
                   options=None):
    """({arm: {marker: gate}}, {arm: the session's units and costs}). Each
    of `variants` (`VARIANTS`) runs its own session, scored as arm
    `variant_arm(variant, variants)`; `options` are added to every one."""
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import profile as profmod

    ds = session.data(project)
    out, extras = {}, {}
    if "gmm" in arms:
        out["gmm"] = {m: (model.fit_for(ds, m) or {}).get("gate") for m in markers}
    if "profile" in arms:
        out["profile"] = {m: ((profmod.profile_marker(ds, m).get("fit") or {})
                              .get("gate_raw")) for m in markers}
    if "session" in arms:
        for variant in check_variants(variants):
            arm = variant_arm(variant, variants)
            started = {**(options or {}), **VARIANTS[variant],
                       "agent": f"bench:{variant}", "reuse_answers": False}
            units, status, seconds, sid = run_session(session, project, markers, agent,
                                                      mode="propose", options=started)
            out[arm] = {m: units.get(m, {}).get("final") for m in markers}
            stored = _session_units(sid)
            extras[arm] = {"units": units, "used": status["used"], "seconds": seconds,
                           "session_id": sid, "packets": status["used"].get("packets"),
                           "vision_tokens": status.get("estimated_vision_tokens"),
                           "variant": variant, "stored": {u["marker"]: u for u in stored},
                           "replayed": (status.get("summary") or {}).get("replayed"),
                           **request_counts(stored)}
    return out, extras


def score_image(project, values, truth, gates_by_arm, extras, *, cell_truth=None) -> list:
    """Rows per (arm, marker), a `*code*` row per arm and a `*cost*` row per
    session arm. `extras` is {arm: a session's units and costs} (a lone
    session's dict is taken as the `session` arm's); `cell_truth` (by
    marker) adds where the errors fall (`cause_scores`)."""
    if extras and "units" in extras:
        extras = {"session": extras}
    extras = extras or {}
    rows = []
    for arm, gates in gates_by_arm.items():
        extra = extras.get(arm) or (extras.get("session") if arm == "final" else None)
        for marker, low in gates.items():
            row = {"image": project, "arm": arm, "marker": marker, "gate": low}
            if low is not None:
                row.update(classification(values[marker], truth[marker], low))
            if extra is not None:
                unit = (extra.get("stored") or {}).get(marker) or \
                    (extra["units"].get(marker) or {})
                row.update(state=unit.get("state"), confidence=unit.get("confidence"),
                           tier=unit.get("tier"))
            if cell_truth and marker in cell_truth and low is not None:
                row.update(cause_scores(values[marker], truth[marker], low, cell_truth[marker]))
            rows.append(row)
        rows.append({"image": project, "arm": arm, "marker": "*code*",
                     "code_agreement": code_agreement(values, truth, gates)})
    for arm, extra in extras.items():
        units = list((extra.get("stored") or {}).values())
        rows.append({"image": project, "arm": arm, "marker": "*cost*",
                     "packets": extra["packets"], "chars": extra["used"].get("chars"),
                     "pixels": extra["used"].get("pixels"),
                     "vision_tokens": extra["vision_tokens"], "seconds": extra["seconds"],
                     "session_id": extra["session_id"], "variant": extra.get("variant"),
                     "requests_made": extra.get("requests_made"),
                     "requests_served": extra.get("requests_served"),
                     "cache_hits": extra.get("replayed"),
                     "review_fraction": (float(np.mean([u.get("state") in (
                         "manual_review_recommended", "insufficient_information")
                         for u in units])) if units else None)})
    return rows


def _register_scenario(root, name, scenario, *, grid, size, seed, markers):
    """A scene's files, project and truth: a scene with per-cell causes
    (`bench_data.SPATIAL_SCENARIOS`) or a plain one."""
    from plexora.ai import bench_data

    if scenario in bench_data.SPATIAL_SCENARIOS:
        return bench_data.register_spatial(root, name, scenario=scenario, size=size,
                                           seed=seed, markers=markers)
    return bench_data.register(root, name, scenario=scenario, grid=grid, size=size,
                               seed=seed, markers=markers)


def run_synthetic(scenarios, agent_style, *, arms=ARMS, seed=0, markers=None, grid=24,
                  size=1024, variants=("baseline",)) -> list:
    from plexora import paths
    from plexora.agent import AgentSession, registry

    markers = tuple(markers or ("CD3", "CD8", "CD20", "CD4", "FOXP3"))
    variants = check_variants(variants)
    rows = []
    with tempfile.TemporaryDirectory(prefix="plexora-bench-") as root:
        previous = os.environ.get("PLEXORA_DATA_PATH")
        os.environ["PLEXORA_DATA_PATH"] = root
        paths.reset()
        try:
            registry.discover(["gating"])
            session = AgentSession(table_limit=4)
            for index, scenario in enumerate(scenarios):
                name = f"bench_{scenario}"
                made = _register_scenario(root, name, scenario, grid=grid, size=size,
                                          seed=seed + index, markers=markers)
                agent = TruthAgent(made["values"], made["truth"], agent_style,
                                   seed=seed + index, ids=made.get("ids"))
                gates, extras = gates_for_arms(session, name, markers, agent, arms,
                                               variants=variants)
                for row in score_image(name, made["values"], made["truth"], gates, extras,
                                       cell_truth=made.get("cell_truth")):
                    row["scenario"] = scenario
                    rows.append(row)
            session.close()
        finally:
            from plexora.agent import jobs

            # A session's jobs (its classifier layers) write under the data
            # root: done before the temporary one goes.
            jobs.drain(120)
            if previous is None:
                os.environ.pop("PLEXORA_DATA_PATH", None)
            else:
                os.environ["PLEXORA_DATA_PATH"] = previous
            paths.reset()
    return rows


def run_stability(scenarios, agent_style, *, repeats=3, seed=0, markers=None, grid=24,
                  size=1024, variant="baseline") -> list:
    """How far a marker's gate moves between runs: each scenario gated
    `repeats` times by agents seeded differently (a noisy agent errs at
    different packets), then once more by the first run's agent with its
    answers reused (`memo`) -- which must reach the first run's gates to the
    digit. Rows per marker: {gates, spread, distinct, states, state_changes,
    replay_identical}."""
    from plexora import paths
    from plexora.agent import AgentSession, registry

    markers = tuple(markers or ("CD3", "CD8", "CD20", "CD4", "FOXP3"))
    rows = []
    with tempfile.TemporaryDirectory(prefix="plexora-bench-") as root:
        previous = os.environ.get("PLEXORA_DATA_PATH")
        os.environ["PLEXORA_DATA_PATH"] = root
        paths.reset()
        try:
            registry.discover(["gating"])
            session = AgentSession(table_limit=4)
            options = dict(VARIANTS[check_variants((variant,))[0]])
            for index, scenario in enumerate(scenarios):
                name = f"bench_{scenario}"
                made = _register_scenario(root, name, scenario, grid=grid, size=size,
                                          seed=seed + index, markers=markers)
                runs = []
                for repeat in range(int(repeats)):
                    agent = TruthAgent(made["values"], made["truth"], agent_style,
                                       seed=seed + 1000 * repeat + index, ids=made.get("ids"))
                    units, _status, _s, _sid = run_session(
                        session, name, markers, agent, mode="propose",
                        options={**options, "agent": f"{agent_style}#{repeat}"})
                    runs.append(units)
                replay_agent = TruthAgent(made["values"], made["truth"], agent_style,
                                          seed=seed + index, ids=made.get("ids"))
                replay, _status, _s, _sid = run_session(
                    session, name, markers, replay_agent, mode="propose",
                    options={**options, "agent": f"{agent_style}#0"})
                for marker in markers:
                    gates = [_gate_of(u.get(marker)) for u in runs]
                    finite = [g for g in gates if g is not None]
                    states = [(u.get(marker) or {}).get("state") for u in runs]
                    rows.append({
                        "image": name, "scenario": scenario, "marker": marker,
                        "gates": gates, "states": states,
                        "spread": (max(finite) - min(finite)) if finite else None,
                        "distinct": len(set(finite)),
                        "state_changes": len(set(states)) - 1,
                        "replay_identical": _gate_of(replay.get(marker)) == gates[0]
                        and (replay.get(marker) or {}).get("state") == states[0]})
            session.close()
        finally:
            if previous is None:
                os.environ.pop("PLEXORA_DATA_PATH", None)
            else:
                os.environ["PLEXORA_DATA_PATH"] = previous
            paths.reset()
    return rows


def _gate_of(unit):
    unit = unit or {}
    value = unit.get("final") if unit.get("final") is not None else unit.get("proposed")
    return None if value is None else float(value)


def stability_markdown(rows, *, title) -> str:
    lines = [f"# {title}", "",
             "Per marker across runs: how many distinct gates, their spread (table units), "
             "how many end states, and whether a rerun with the first run's answers reused "
             "reached the same gate.", "",
             "| image | marker | distinct gates | spread | state changes | replay identical |",
             "|---|---|---|---|---|---|"]
    for row in rows:
        spread = "-" if row["spread"] is None else f"{row['spread']:.3f}"
        lines.append(f"| {row['image']} | {row['marker']} | {row['distinct']} | {spread} | "
                     f"{row['state_changes']} | {'yes' if row['replay_identical'] else 'NO'} |")
    identical = sum(1 for r in rows if r["replay_identical"])
    stable = sum(1 for r in rows if r["distinct"] <= 1 and r["state_changes"] == 0)
    lines += ["", f"{stable}/{len(rows)} markers reached one gate in every run; "
                  f"{identical}/{len(rows)} replays were identical."]
    return "\n".join(lines) + "\n"


def reference_gates(path, project=None) -> dict:
    """{marker: gate} from a reference run's gates file (`json:<path>`): the
    markers it accepted (a gate under review is no reference)."""
    from plexora.plugins.gating.server.autogate import schemas

    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if project and data.get("project") not in (None, project):
        raise RuntimeError(f"{path} holds gates for {data.get('project')!r}, not {project!r}")
    return {m: float(row["low"]) for m, row in (data.get("markers") or {}).items()
            if row.get("low") is not None and row.get("state") in schemas.ACCEPTED_STATES}


def reference_csv(path, column=None) -> dict:
    """{marker: gate} from a `marker,value` file (one per line; a header line,
    blank lines and `#` comments are skipped) -- gates someone wrote down by
    hand, in the table's own units.

    `column` names the header column holding the gates (`--truth-column
    "Human gate"`), the markers being the first column; a cell that is not a
    number (`#N/A`) is skipped. Without it only two-column lines are read: a
    file with a column named like `human` and no `column` chosen is refused
    rather than read some other way."""
    import csv
    import io

    text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    header = [h.strip() for h in rows[0]] if rows else []
    if column is not None:
        if column not in header:
            raise ValueError(f"{path} has no column {column!r} (columns: {header})")
        at = header.index(column)
        out = {}
        for row in rows[1:]:
            if len(row) <= at or not row[0].strip() or row[0].lstrip().startswith("#"):
                continue
            try:
                out[row[0].strip()] = float(row[at])
            except ValueError:
                continue
        return out
    if any("human" in h.lower() for h in header[2:]):
        raise ValueError(f"{path} has a {next(h for h in header if 'human' in h.lower())!r} "
                         "column; say which column holds the gates (--truth-column) -- "
                         "the second column is read otherwise")
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, value = (part.strip() for part in line.partition(","))
        try:
            out[name] = float(value)
        except ValueError:
            continue
    return out


def expert_truth(session, project, table=TRUTH_TABLE, column=None):
    """({marker: per-cell truth}, {marker: values}, {marker: expert gate}) from an
    expert's gates stored in the project's AnnData `uns[table]`, from a
    reference run's file when `table` is `json:<path>`, or from a hand-written
    `marker,value` file when it is `csv:<path>` (`column`: which of its
    columns holds the gates)."""
    ds = session.data(project)
    if str(table).startswith(("json:", "csv:")):
        kind, path = str(table).split(":", 1)
        found = reference_gates(path, ds.name) if kind == "json" else \
            reference_csv(path, column)
        gates = {m: v for m, v in found.items() if m in ds.table.markers}
        values = {m: np.asarray(ds.table.columns([m])[m], dtype=np.float32) for m in gates}
        return {m: values[m] > np.float32(gates[m]) for m in gates}, values, gates
    answer = ds.table.run("gating.load_gates", {
        "image_id": ds.name, "table_name": table,
        "imageid_column": ds.schema.image_id if ds.schema else None})
    if not answer.get("ok"):
        raise RuntimeError(answer.get("message") or "the expert gates could not be read")
    gates = {m: v for m, v in (answer.get("gates") or {}).items()
             if m in ds.table.markers and v is not None and np.isfinite(v)}
    values = {m: np.asarray(ds.table.columns([m])[m], dtype=np.float32) for m in gates}
    truth = {m: values[m] > np.float32(gates[m]) for m in gates}
    return truth, values, gates


def run_projects(projects, agent_style, *, truth_table=TRUTH_TABLE, arms=ARMS, seed=0,
                 markers=None, variants=("baseline",), truth_column=None) -> list:
    from plexora.agent import AgentSession, registry
    from plexora.plugins.gating.server.autogate import profile as profmod

    registry.discover(["gating"])
    session = AgentSession(table_limit=4)
    rows = []
    for index, project in enumerate(projects):
        truth, values, expert = expert_truth(session, project, truth_table, truth_column)
        wanted = [m for m in (markers or expert) if m in expert]
        agent = TruthAgent(values, truth, agent_style, seed=seed + index)
        gates, extra = gates_for_arms(session, project, wanted, agent, arms,
                                      variants=variants)
        gates["expert"] = {m: expert[m] for m in wanted}
        ds = session.data(project)
        for row in score_image(project, values, truth, gates, extra):
            if row["arm"] != "expert" and row.get("gate") is not None \
                    and row["marker"] in expert:
                # In the marker's fit space (log1p of raw intensities; a
                # table already in log space is used as it is, not logged twice).
                col = profmod.column(ds, row["marker"])
                row["delta_log1p"] = float(col.to_fit(row["gate"])
                                           - col.to_fit(expert[row["marker"]]))
            rows.append(row)
    return rows


#: What each stage of the pipeline would have written on its own, read from a
#: stored session's unit: the Auto mixture gate, the scored proposal the looks
#: started from, the background ceiling, the most specific partner's negative
#: control, and the gate the session ended at.
SESSION_ARMS = {
    "gmm": lambda u: u.get("gmm"),
    "proposal": lambda u: (u.get("start") or {}).get("low"),
    "ceiling": lambda u: ((u.get("scoring") or {}).get("background") or {}).get("ceiling"),
    "control": lambda u: ((u.get("scoring") or {}).get("control") or {}).get("value"),
    "final": lambda u: u.get("final"),
}

#: An expert's gate is read to one decimal place: a stage within this of it
#: is the same gate.
WITHIN = 0.1


def against_expert(row, expert_gate):
    """`row` with its signed `delta` (gate - expert, table units) and whether
    that is `within` WITHIN."""
    if row.get("gate") is not None and expert_gate is not None:
        delta = float(row["gate"]) - float(expert_gate)
        row.update(delta=delta, within=abs(delta) <= WITHIN + 1e-9)
    return row


def score_session(session_id, *, truth_table=TRUTH_TABLE, truth_column=None) -> list:
    """Score a session a real agent ran against the expert gates in its images:
    every stage of `SESSION_ARMS`, so a miss can be put where it happened."""
    from plexora.agent import AgentSession, registry
    from plexora.plugins.gating.server.autogate import engine

    registry.discover(["gating"])
    record = engine.store().load(session_id)
    session = AgentSession(table_limit=4)
    rows = []
    for project in record["images"]:
        truth, values, expert = expert_truth(session, project, truth_table, truth_column)
        units = {u["marker"]: u for u in record["units"].values()
                 if u["project"] == project and u["marker"] in expert}
        arms = {arm: {m: read(u) for m, u in units.items()}
                for arm, read in SESSION_ARMS.items()}
        extra = {"units": units,
                 "used": record.get("used") or {}, "seconds": None, "session_id": session_id,
                 "packets": (record.get("used") or {}).get("packets"),
                 "vision_tokens": budgets.vision_tokens((record.get("used") or {}).get(
                     "pixels", 0)),
                 **request_counts([u for u in record["units"].values()
                                   if u["project"] == project])}
        for row in score_image(project, values, truth,
                               {**arms, "expert": {m: expert[m] for m in units}}, extra):
            if row["arm"] != "expert" and not row["marker"].startswith("*"):
                against_expert(row, expert.get(row["marker"]))
            rows.append(row)
    return rows


def answer_direction(answer):
    """The way an answer moves the gate (`up`, `down`, `about_right`, ...):
    read from its rows (`verdicts.direction_from_rows`), or from the
    `direction` an older answer said outright; None for a kind that has
    neither."""
    from plexora.plugins.gating.server.autogate import verdicts

    answer = answer or {}
    if answer.get("direction") is not None:
        return {"too_low": "up", "too_high": "down"}.get(answer["direction"],
                                                         answer["direction"])
    if isinstance(answer.get("rows"), dict):
        return verdicts.direction_from_rows(answer["rows"], answer.get("pattern"))[0]
    return None


#: The fields of an answer that decide what happens next: per-row verdicts
#: (compared row by row) and single verdicts.
_DECIDING_ROWS = ("verdicts", "intervals", "per_image", "cells")
_DECIDING = ("verdict",)


def _agreement(stored, replayed):
    """How far two answers to one packet agree on what decides the next step:
    the share of matching rows (an audit strip, a look at candidates, a
    transfer check), 1.0/0.0 for a look's direction or a single verdict, None
    when the two cannot be compared."""
    if not stored or not replayed:
        return None
    for key in _DECIDING_ROWS:
        if isinstance(stored.get(key), dict) and isinstance(replayed.get(key), dict):
            rows = sorted(set(stored[key]) | set(replayed[key]))
            return float(np.mean([stored[key].get(r) == replayed[key].get(r)
                                  for r in rows])) if rows else None
    a, b = answer_direction(stored), answer_direction(replayed)
    if a is not None and b is not None:
        return float(a == b)
    for key in _DECIDING:
        if stored.get(key) is not None and replayed.get(key) is not None:
            return float(stored[key] == replayed[key])
    return None


def _rerender_image(session, image, panel=None):
    """{artifact_id, rerendered, identical} for one of a packet's pictures.

    With `panel` (a `cell_panel` packet's `_panel`, which the reader never
    sees), also `numbers_match`: whether each number captioned in the
    picture is the cell code holds under that number -- the labels a reader
    gives are scored by it."""
    from plexora.agent import artifacts
    from plexora.agent.evidence import collage

    art = image.get("artifact_id")
    try:
        png, sidecar = artifacts.get(art)
    except KeyError:
        return {"artifact_id": art, "rerendered": False, "why": "artifact_missing"}
    manifest = sidecar.get("manifest") or {}
    if manifest.get("kind") != collage.MANIFEST_KIND:
        return {"artifact_id": art, "rerendered": False, "why": manifest.get("kind")}
    drawn = collage.rerender(session, session.data(manifest["project"]), manifest)
    out = {"artifact_id": art, "rerendered": True, "identical": drawn["png"] == png}
    if panel is not None:
        captioned = {cell.get("caption"): int(cell["cell_id"])
                     for row in manifest.get("rows") or [] for cell in row.get("cells") or []}
        held = {c["number"]: int(c["cell_id"]) for c in panel.get("cells") or []}
        out["numbers_match"] = captioned == held
    return out


def replay_session(session_id, agent, *, rerender=False, session=None) -> list:
    """Ask `agent` every packet a stored session issued, in order.

    Rows of {packet_id, kind, marker, stored (the answer the session got),
    replayed (the agent's), agreement}; with `rerender`, also `images`: each
    collage drawn again from its manifest -- a look's rows or a blind cell
    panel -- and whether its bytes match the stored picture (a panel's also
    whether its numbers are the cells code scored, `_rerender_image`).
    `agent` is anything with `answer(packet)`: `TruthAgent` answering from an
    expert's gates, or a model behind a client."""
    from plexora.agent import AgentSession, registry
    from plexora.plugins.gating.server.autogate import engine
    from plexora.plugins.gating.server.autogate import packets as packet_views

    registry.discover(["gating"])
    store = engine.store()
    log = store.decisions(session_id)
    answers = {d["packet_id"]: d.get("answer") for d in log if d.get("event") == "answered"}
    own = session is None
    session = session or AgentSession(table_limit=4)
    held, rows = {}, []
    try:
        for event in log:
            if event.get("event") != "issued":
                continue
            packet, _images = store.read_packet(session_id, event["packet_id"])
            packet = packet_views.resolve_as_in(packet, held)
            units = packet.get("units") or []
            row = {"packet_id": packet["packet_id"], "kind": packet["kind"],
                   "marker": units[0]["marker"] if units else None,
                   "stored": answers.get(packet["packet_id"])}
            if rerender:
                row["images"] = [_rerender_image(session, image, packet.get("_panel"))
                                 for image in packet.get("images") or []]
            row["replayed"] = agent.answer(packet)
            row["agreement"] = _agreement(row["stored"], row["replayed"])
            rows.append(row)
    finally:
        if own:
            session.close()
    return rows


class _PerImage:
    """One scripted agent per image of a session, picked by the packet's unit;
    None for a marker its expert never gated."""

    def __init__(self, agents):
        self.agents = agents

    def answer(self, packet):
        units = packet.get("units") or []
        agent = self.agents.get(units[0]["project"]) if units else None
        try:
            return (agent or next(iter(self.agents.values()))).answer(packet)
        except KeyError:
            return None


def run_replay(session_id, agent_style, *, truth_table=TRUTH_TABLE, rerender=False,
               seed=0, truth_column=None) -> list:
    """`replay_session` with a scripted agent answering from the expert's gates."""
    from plexora.agent import AgentSession, registry
    from plexora.plugins.gating.server.autogate import engine

    registry.discover(["gating"])
    record = engine.store().load(session_id)
    session = AgentSession(table_limit=4)
    try:
        agents = {}
        for index, project in enumerate(record["images"]):
            truth, values, _expert = expert_truth(session, project, truth_table,
                                                  truth_column)
            agents[project] = TruthAgent(values, truth, agent_style, seed=seed + index)
        return replay_session(session_id, _PerImage(agents), rerender=rerender,
                              session=session)
    finally:
        session.close()


def replay_markdown(rows, *, title) -> str:
    redrawn = any("images" in r for r in rows)
    lines = [f"# {title}", "",
             "Per packet kind: how often the stored answer agreed with the replayed one "
             "(direction for a look, rows for a look at candidates, cells for a panel)"
             + ("; and how many of the kind's collages, drawn again from their "
                "manifests, came out byte-identical." if redrawn else "."), "",
             "| kind | packets | compared | agreement |"
             + (" redrawn | identical |" if redrawn else ""),
             "|---|---|---|---|" + ("---|---|" if redrawn else "")]
    kinds = {}
    for row in rows:
        kinds.setdefault(row["kind"], []).append(row)
    for kind, members in kinds.items():
        scored = [r["agreement"] for r in members if r.get("agreement") is not None]
        line = (f"| {kind} | {len(members)} | {len(scored)} | "
                f"{(f'{np.mean(scored):.3f}' if scored else '-')} |")
        if redrawn:
            drawn = [i for r in members for i in r.get("images") or [] if i.get("rerendered")]
            line += f" {len(drawn)} | {sum(1 for i in drawn if i.get('identical'))} |"
        lines.append(line)
    if redrawn:
        drawn = [i for r in rows for i in r.get("images") or [] if i.get("rerendered")]
        same = sum(1 for i in drawn if i.get("identical"))
        lines += ["", f"Collages drawn again from their manifests: {len(drawn)}; "
                      f"byte-identical: {same}."]
        panels = [i for i in drawn if "numbers_match" in i]
        if panels:
            lines.append(f"Cell panels whose numbers are the cells code scored: "
                         f"{sum(1 for i in panels if i['numbers_match'])} of {len(panels)}.")
    return "\n".join(lines) + "\n"


# -- reporting ----------------------------------------------------------------------------


def summarise(rows) -> dict:
    by_arm = {}
    deltas = {}
    for row in rows:
        arm = row["arm"]
        entry = by_arm.setdefault(arm, {"f1": [], "kappa": [], "fraction_error": [],
                                        "code_agreement": [], "delta_log1p": []})
        for key in ("f1", "kappa", "fraction_error", "delta_log1p"):
            if row.get(key) is not None and not row["marker"].startswith("*"):
                entry[key].append(abs(row[key]) if key == "delta_log1p" else row[key])
        if row["marker"] == "*code*" and row.get("code_agreement") is not None:
            entry["code_agreement"].append(row["code_agreement"])
        if row.get("delta") is not None:
            deltas.setdefault(arm, []).append(row)
    summary = {}
    for arm, entry in by_arm.items():
        summary[arm] = {key: (float(np.median(v)) if v else None) for key, v in entry.items()}
        summary[arm]["n_markers"] = len(entry["f1"])
        # Against an expert's gates: the mean miss (table units) and the share
        # of markers read as the same gate (`against_expert`).
        scored = deltas.get(arm) or []
        summary[arm]["mae"] = (float(np.mean([abs(r["delta"]) for r in scored]))
                               if scored else None)
        summary[arm]["within"] = (float(np.mean([bool(r["within"]) for r in scored]))
                                  if scored else None)
        summary[arm]["n"] = len(scored)
    costs = [r for r in rows if r["marker"] == "*cost*"]
    if costs:
        summary["cost"] = {key: float(np.sum([c.get(key) or 0 for c in costs]))
                           for key in ("packets", "chars", "pixels", "vision_tokens",
                                       "seconds", "requests_made", "requests_served")}
        summary["cost"]["images"] = len(costs)
        by_arm_cost = {}
        for cost in costs:
            entry = by_arm_cost.setdefault(cost["arm"], {"packets": 0.0, "cache_hits": 0.0,
                                                         "images": 0, "review_fraction": []})
            entry["packets"] += cost.get("packets") or 0
            entry["cache_hits"] += cost.get("cache_hits") or 0
            entry["images"] += 1
            if cost.get("review_fraction") is not None:
                entry["review_fraction"].append(cost["review_fraction"])
        for entry in by_arm_cost.values():
            entry["review_fraction"] = (float(np.mean(entry["review_fraction"]))
                                        if entry["review_fraction"] else None)
        summary["cost"]["by_arm"] = by_arm_cost
    causes = {}
    for row in rows:
        if row["marker"].startswith("*"):
            continue
        entry = causes.setdefault(row["arm"], {k: [] for k in CAUSE_METRICS})
        for key in CAUSE_METRICS:
            if row.get(key) is not None:
                entry[key].append(float(row[key]))
    causes = {arm: {k: (float(np.mean(v)) if v else None) for k, v in entry.items()}
              for arm, entry in causes.items() if any(entry[k] for k in CAUSE_METRICS)}
    if causes:
        summary["causes"] = causes
    return summary


#: Where the errors fall, averaged per arm (`cause_scores`): the error rates
#: by environment, the dim positives' recall and the clean-negative guard.
CAUSE_METRICS = ("fp_rate_dense", "fp_rate_sparse", "fp_rate_low_snr", "fp_rate_high_snr",
                 "dim_recall", "contaminated_fp_rate", "clean_fp_rate")


def to_markdown(summary, rows, *, title) -> str:
    lines = [f"# {title}", "",
             "Median per marker; code agreement per image (the primary endpoint).", "",
             "| arm | markers | code agreement | F1 | kappa | fraction error | abs delta log1p |",
             "|---|---|---|---|---|---|---|"]

    def fmt(value):
        return "-" if value is None else f"{value:.3f}"

    for arm, entry in summary.items():
        if arm in ("cost", "causes"):
            continue
        lines.append(f"| {arm} | {entry['n_markers']} | {fmt(entry['code_agreement'])} | "
                     f"{fmt(entry['f1'])} | {fmt(entry['kappa'])} | "
                     f"{fmt(entry['fraction_error'])} | {fmt(entry['delta_log1p'])} |")
    against = [arm for arm, entry in summary.items()
               if arm not in ("cost", "causes") and entry.get("n")]
    if against:
        lines += ["", f"Against the expert's gates (mean absolute miss in table units; "
                      f"share within {WITHIN}):", "",
                  "| arm | markers | MAE | within |", "|---|---|---|---|"]
        for arm in against:
            entry = summary[arm]
            lines.append(f"| {arm} | {entry['n']} | {fmt(entry['mae'])} | "
                         f"{fmt(entry['within'])} |")
        by_marker = {}
        for row in rows:
            if row.get("gate") is not None and not row["marker"].startswith("*"):
                by_marker.setdefault((row["image"], row["marker"]), {})[row["arm"]] = row
        if by_marker:
            arms = against + (["expert"] if any("expert" in r for r in by_marker.values())
                              else [])
            lines += ["", "| image | marker | " + " | ".join(arms) + " |",
                      "|---|---|" + "---|" * len(arms)]
            for (image, marker), cells in by_marker.items():
                shown = []
                for arm in arms:
                    row = cells.get(arm)
                    if row is None:
                        shown.append("-")
                    elif row.get("delta") is None:
                        shown.append(f"{row['gate']:.2f}")
                    else:
                        shown.append(f"{row['gate']:.2f} ({row['delta']:+.2f})")
                lines.append(f"| {image} | {marker} | " + " | ".join(shown) + " |")
    if "causes" in summary:
        causes = summary["causes"]
        shown = [k for k in CAUSE_METRICS if any(e.get(k) is not None for e in causes.values())]
        lines += ["", "Where the errors fall (scenes with per-cell causes; mean over markers "
                      "with the measure; rates are shares of cells):", "",
                  "| arm | " + " | ".join(shown) + " |", "|---|" + "---|" * len(shown)]
        for arm, entry in causes.items():
            lines.append(f"| {arm} | " + " | ".join(fmt(entry.get(k)) for k in shown) + " |")
    if "cost" in summary:
        cost = summary["cost"]
        lines += ["", f"Session cost over {int(cost['images'])} image(s): "
                      f"{int(cost['packets'])} packets, {int(cost['chars'])} characters, "
                      f"~{int(cost['vision_tokens'])} vision tokens, "
                      f"{cost['seconds']:.0f} s; {int(cost['requests_made'])} evidence "
                      f"request(s), {int(cost['requests_served'])} served."]
        for arm, entry in (cost.get("by_arm") or {}).items():
            lines.append(f"- {arm}: {int(entry['packets'])} packets, "
                         f"{int(entry['cache_hits'])} replayed, review fraction "
                         f"{fmt(entry['review_fraction'])}")
    lines += ["", "## Per marker", "",
              "| image | arm | marker | gate | F1 | state | confidence |", "|---|---|---|---|---|---|---|"]
    for row in rows:
        if row["marker"].startswith("*"):
            continue
        lines.append(f"| {row['image']} | {row['arm']} | {row['marker']} | "
                     f"{fmt(row.get('gate'))} | {fmt(row.get('f1'))} | "
                     f"{row.get('state') or ''} | {row.get('confidence') or ''} |")
    return "\n".join(lines) + "\n"


def code_state() -> dict:
    """{git_sha, dirty}: which code produced a benchmark (None outside a checkout)."""
    import subprocess

    from plexora._subprocess import popen_kwargs

    root = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                             text=True, timeout=10, **popen_kwargs()).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "plexora"], cwd=root,
                                    capture_output=True, text=True, timeout=10,
                                    **popen_kwargs()).stdout.strip())
    except Exception:
        return {"git_sha": None, "dirty": None}
    return {"git_sha": sha, "dirty": dirty}


def _meta(variants):
    return {"variants": list(variants), "code": code_state()}


def bench_command(*, synthetic=None, projects=None, dataset=None, truth=TRUTH_DEFAULT,
                  agent=AGENT_STYLES[0], arms=ARMS, out=None, markers=None, seed=0,
                  score=None, grid=24, size=1024, stability=None, replay=None,
                  rerender=False, variants=("baseline",), truth_column=None,
                  emit=print) -> int:
    started = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    try:
        variants = check_variants(variants)
    except ValueError as exc:
        emit(str(exc))
        return 2
    if stability:
        if not synthetic:
            emit("--stability runs on synthetic scenarios: pass --synthetic too.")
            return 2
        rows = run_stability(synthetic, agent, repeats=int(stability), seed=seed,
                             markers=markers, grid=grid, size=size, variant=variants[-1])
        title = f"Gating stability ({agent} agent, {int(stability)} runs)"
        if out is None:
            from plexora import paths

            out = paths.agent_root() / "bench" / f"stability_{started}"
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "stability.json").write_text(json.dumps(rows, indent=1, default=str),
                                            encoding="utf-8")
        text = stability_markdown(rows, title=title)
        (out / "stability.md").write_text(text, encoding="utf-8")
        emit(text.rstrip())
        emit(f"\nWritten: {out / 'stability.json'}")
        return 0
    table = truth.split(":", 1)[1] if truth and truth.startswith("uns:") else TRUTH_TABLE
    if truth and truth.startswith(("json:", "csv:")):
        table = truth
    against = table if table.startswith(("json:", "csv:")) else f"uns[{table!r}]"
    if table.startswith("json:"):
        try:
            kind = json.loads(Path(table[5:]).read_text(encoding="utf-8")).get("kind")
        except (OSError, ValueError, AttributeError):
            kind = None
        if kind == "model_gates":
            # A model's earlier gates measure agreement with that run, not
            # accuracy against a person's gates.
            emit(f"warning: {table[5:]} holds a model's gates, not a person's")
            against += " (a model's gates)"
    if replay:
        rows = run_replay(replay, agent, truth_table=table, rerender=rerender, seed=seed,
                          truth_column=truth_column)
        if out is None:
            from plexora import paths

            out = paths.agent_root() / "bench" / f"replay_{started}"
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "replay.json").write_text(json.dumps(rows, indent=1, default=str),
                                         encoding="utf-8")
        text = replay_markdown(rows, title=f"Gating session {replay} replayed by the "
                                           f"{agent} agent against {against}")
        (out / "replay.md").write_text(text, encoding="utf-8")
        emit(text.rstrip())
        emit(f"\nWritten: {out / 'replay.json'}")
        return 0
    if truth_column:
        against += f" ({truth_column!r})"
    if score:
        rows = score_session(score, truth_table=table, truth_column=truth_column)
        title = f"Gating session {score} against {against}"
    elif synthetic:
        rows = run_synthetic(synthetic, agent, arms=arms, seed=seed, markers=markers,
                             grid=grid, size=size, variants=variants)
        title = f"Synthetic gating benchmark ({agent} agent" + \
            (f", variants {', '.join(variants)})" if variants != ("baseline",) else ")")
    else:
        names = list(projects or [])
        if dataset:
            from plexora import datasets

            names += list(datasets.dataset(dataset).projects)
        if not names:
            emit("Nothing to benchmark: pass --synthetic, --project, --dataset or "
                 "--score-session.")
            return 2
        rows = run_projects(names, agent, truth_table=table, arms=arms, seed=seed,
                            markers=markers, variants=variants, truth_column=truth_column)
        title = f"Gating benchmark against {against} ({agent} agent)"
    summary = summarise(rows)
    if out is None:
        from plexora import paths

        out = paths.agent_root() / "bench" / started
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"summary": summary, "rows": rows,
                                                  **_meta(variants)},
                                                 indent=1, default=str), encoding="utf-8")
    (out / "summary.md").write_text(to_markdown(summary, rows, title=title), encoding="utf-8")
    emit(to_markdown(summary, rows, title=title).split("## Per marker")[0].rstrip())
    emit(f"\nWritten: {out / 'results.json'}\n         {out / 'summary.md'}")
    return 0
