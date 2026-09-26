"""`plexora ai bench gating`: how well automatic gating gates, and at what cost.

Arms, per marker of each image:

- `gmm`      the Auto button's gate (3-component mixture, top vs rest);
- `profile`  the profile's gate (split-aware pools), no look at all;
- `session`  a full gating session answered by a scripted agent -- `oracle`
             (answers from the truth: the pipeline's ceiling), `lazy` (always
             "looks fine": what the deterministic layer catches on its own), or
             `noisy:P` (wrong about the direction with probability P: whether
             the guards hold).

Truth is either a synthetic scene's phenotypes (`--synthetic`), or an expert's
gates stored in the project's AnnData (`--truth uns:<table>`). The primary
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

ARMS = ("gmm", "profile", "session")


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


class TruthAgent:
    """Answers packets from known per-cell truth (see the module docstring)."""

    def __init__(self, values, truth, style="oracle", seed=0):
        self.values = {m: np.asarray(v, dtype=np.float32) for m, v in values.items()}
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
            return {"kind": kind, "direction": direction, "confidence": 0.85,
                    "plausibility": {"compartment": "matches", "pattern": "membrane",
                                     "positives_look_real": True},
                    "rows": {"below": "plausible", "near": "plausible", "above": "plausible"}}
        if kind == "t4_candidates":
            if self.style == "lazy":
                return {"kind": kind, "chosen_candidate": "keep", "confidence": 0.85}
            best_err = self._errors(marker, ev["current"])
            choice = "keep"
            for cand in ev.get("candidates") or []:
                err = self._errors(marker, cand["low"])
                if err < best_err:
                    choice, best_err = cand["id"], err
            if choice == "keep" and self.direction(marker, ev["current"]) != "about_right":
                choice = "none_separates"
            return {"kind": kind, "chosen_candidate": choice, "confidence": 0.8}
        if kind == "qc_confirm":
            real = bool(self.truth.get(marker, np.zeros(1, bool)).any())
            return {"kind": kind, "verdict": "real_signal" if real else "technical_failure"}
        if kind == "regression_confirm":
            return {"kind": kind, "verdict": "holds"}
        if kind == "transfer_check":
            project = ev["this"]["project"]
            direction = self.direction(marker, ev["this"]["aligned_gate"])
            return {"kind": kind, "per_image": {
                project: "holds" if direction == "about_right" or self.style == "lazy"
                else direction}}
        return {"kind": "panel_context", "entries": []}


# -- running the arms ------------------------------------------------------------------


def _ok(outcome):
    if not outcome["ok"]:
        raise RuntimeError(json.dumps(outcome["error"], default=str)[:2000])
    return outcome["result"]


def run_session(session, project, markers, agent, *, mode="apply", limit=400):
    """Drive one gating session to the end; returns (units, status, seconds)."""
    from plexora.agent import invoke, jobs

    started_at = time.perf_counter()
    started = _ok(invoke(session, "gating_session_start", {
        "scope": "project", "project": project, "markers": list(markers), "mode": mode}))
    jobs.drain(600)
    sid = started["session_id"]
    result = _ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 30}))
    for _ in range(limit):
        if result["state"] != "decision":
            break
        packet = result["packet"]
        result = _ok(invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"],
            "answer": agent.answer(packet)}))["next"]
    status = _ok(invoke(session, "gating_session_status", {"session_id": sid}))
    seconds = time.perf_counter() - started_at
    units = {u["marker"]: u for u in status["units"] if u["project"] == project}
    return units, status, seconds, sid


def gates_for_arms(session, project, markers, agent, arms):
    """{arm: {marker: gate}}, plus the session's units and costs."""
    from plexora.plugins.gating.server import model
    from plexora.plugins.gating.server.autogate import profile as profmod

    ds = session.data(project)
    out, extra = {}, {}
    if "gmm" in arms:
        out["gmm"] = {m: (model.fit_for(ds, m) or {}).get("gate") for m in markers}
    if "profile" in arms:
        out["profile"] = {m: ((profmod.profile_marker(ds, m).get("fit") or {})
                              .get("gate_raw")) for m in markers}
    if "session" in arms:
        units, status, seconds, sid = run_session(session, project, markers, agent,
                                                  mode="propose")
        out["session"] = {m: units.get(m, {}).get("final") for m in markers}
        extra = {"units": units, "used": status["used"], "seconds": seconds,
                 "session_id": sid, "packets": status["used"].get("packets"),
                 "vision_tokens": status.get("estimated_vision_tokens")}
    return out, extra


def score_image(project, values, truth, gates_by_arm, extra) -> list:
    rows = []
    for arm, gates in gates_by_arm.items():
        for marker, low in gates.items():
            row = {"image": project, "arm": arm, "marker": marker, "gate": low}
            if low is not None:
                row.update(classification(values[marker], truth[marker], low))
            if arm == "session":
                unit = extra["units"].get(marker) or {}
                row.update(state=unit.get("state"), confidence=unit.get("confidence"),
                           tier=unit.get("tier"))
            rows.append(row)
        rows.append({"image": project, "arm": arm, "marker": "*code*",
                     "code_agreement": code_agreement(values, truth, gates)})
    if extra:
        rows.append({"image": project, "arm": "session", "marker": "*cost*",
                     "packets": extra["packets"], "chars": extra["used"].get("chars"),
                     "pixels": extra["used"].get("pixels"),
                     "vision_tokens": extra["vision_tokens"], "seconds": extra["seconds"],
                     "session_id": extra["session_id"]})
    return rows


def run_synthetic(scenarios, agent_style, *, arms=ARMS, seed=0, markers=None, grid=24,
                  size=1024) -> list:
    from plexora import paths
    from plexora.agent import AgentSession, registry
    from plexora.ai import bench_data

    markers = tuple(markers or ("CD3", "CD8", "CD20", "CD4", "FOXP3"))
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
                made = bench_data.register(root, name, scenario=scenario, grid=grid,
                                           size=size, seed=seed + index, markers=markers)
                agent = TruthAgent(made["values"], made["truth"], agent_style,
                                   seed=seed + index)
                gates, extra = gates_for_arms(session, name, markers, agent, arms)
                for row in score_image(name, made["values"], made["truth"], gates, extra):
                    row["scenario"] = scenario
                    rows.append(row)
            session.close()
        finally:
            if previous is None:
                os.environ.pop("PLEXORA_DATA_PATH", None)
            else:
                os.environ["PLEXORA_DATA_PATH"] = previous
            paths.reset()
    return rows


def expert_truth(session, project, table="gates"):
    """({marker: per-cell truth}, {marker: values}, {marker: expert gate}) from an
    expert's gates stored in the project's AnnData `uns[table]`."""
    ds = session.data(project)
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


def run_projects(projects, agent_style, *, truth_table="gates", arms=ARMS, seed=0,
                 markers=None) -> list:
    from plexora.agent import AgentSession, registry

    registry.discover(["gating"])
    session = AgentSession(table_limit=4)
    rows = []
    for index, project in enumerate(projects):
        truth, values, expert = expert_truth(session, project, truth_table)
        wanted = [m for m in (markers or expert) if m in expert]
        agent = TruthAgent(values, truth, agent_style, seed=seed + index)
        gates, extra = gates_for_arms(session, project, wanted, agent, arms)
        gates["expert"] = {m: expert[m] for m in wanted}
        for row in score_image(project, values, truth, gates, extra):
            if row["arm"] != "expert" and row.get("gate") is not None \
                    and row["marker"] in expert:
                row["delta_log1p"] = float(np.log1p(max(row["gate"], 0))
                                           - np.log1p(max(expert[row["marker"]], 0)))
            rows.append(row)
    return rows


def score_session(session_id, *, truth_table="gates") -> list:
    """Score a session a real agent ran against the expert gates in its images."""
    from plexora.agent import AgentSession, registry
    from plexora.plugins.gating.server.autogate import engine

    registry.discover(["gating"])
    record = engine.store().load(session_id)
    session = AgentSession(table_limit=4)
    rows = []
    for project in record["images"]:
        truth, values, expert = expert_truth(session, project, truth_table)
        finals = {u["marker"]: u.get("final") for u in record["units"].values()
                  if u["project"] == project and u["marker"] in expert}
        extra = {"units": {u["marker"]: u for u in record["units"].values()
                           if u["project"] == project},
                 "used": record.get("used") or {}, "seconds": None, "session_id": session_id,
                 "packets": (record.get("used") or {}).get("packets"),
                 "vision_tokens": -(-int((record.get("used") or {}).get("pixels", 0)) // 750)}
        rows.extend(score_image(project, values, truth,
                                {"session": finals,
                                 "expert": {m: expert[m] for m in finals}}, extra))
    return rows


# -- reporting ----------------------------------------------------------------------------


def summarise(rows) -> dict:
    by_arm = {}
    for row in rows:
        arm = row["arm"]
        entry = by_arm.setdefault(arm, {"f1": [], "kappa": [], "fraction_error": [],
                                        "code_agreement": [], "delta_log1p": []})
        for key in ("f1", "kappa", "fraction_error", "delta_log1p"):
            if row.get(key) is not None and not row["marker"].startswith("*"):
                entry[key].append(abs(row[key]) if key == "delta_log1p" else row[key])
        if row["marker"] == "*code*" and row.get("code_agreement") is not None:
            entry["code_agreement"].append(row["code_agreement"])
    summary = {}
    for arm, entry in by_arm.items():
        summary[arm] = {key: (float(np.median(v)) if v else None) for key, v in entry.items()}
        summary[arm]["n_markers"] = len(entry["f1"])
    costs = [r for r in rows if r["marker"] == "*cost*"]
    if costs:
        summary["cost"] = {key: float(np.sum([c.get(key) or 0 for c in costs]))
                           for key in ("packets", "chars", "pixels", "vision_tokens",
                                       "seconds")}
        summary["cost"]["images"] = len(costs)
    return summary


def to_markdown(summary, rows, *, title) -> str:
    lines = [f"# {title}", "",
             "Median per marker; code agreement per image (the primary endpoint).", "",
             "| arm | markers | code agreement | F1 | kappa | fraction error | abs delta log1p |",
             "|---|---|---|---|---|---|---|"]

    def fmt(value):
        return "-" if value is None else f"{value:.3f}"

    for arm, entry in summary.items():
        if arm == "cost":
            continue
        lines.append(f"| {arm} | {entry['n_markers']} | {fmt(entry['code_agreement'])} | "
                     f"{fmt(entry['f1'])} | {fmt(entry['kappa'])} | "
                     f"{fmt(entry['fraction_error'])} | {fmt(entry['delta_log1p'])} |")
    if "cost" in summary:
        cost = summary["cost"]
        lines += ["", f"Session cost over {int(cost['images'])} image(s): "
                      f"{int(cost['packets'])} packets, {int(cost['chars'])} characters, "
                      f"~{int(cost['vision_tokens'])} vision tokens, "
                      f"{cost['seconds']:.0f} s."]
    lines += ["", "## Per marker", "",
              "| image | arm | marker | gate | F1 | state | confidence |", "|---|---|---|---|---|---|---|"]
    for row in rows:
        if row["marker"].startswith("*"):
            continue
        lines.append(f"| {row['image']} | {row['arm']} | {row['marker']} | "
                     f"{fmt(row.get('gate'))} | {fmt(row.get('f1'))} | "
                     f"{row.get('state') or ''} | {row.get('confidence') or ''} |")
    return "\n".join(lines) + "\n"


def bench_command(*, synthetic=None, projects=None, dataset=None, truth="uns:gates",
                  agent="oracle", arms=ARMS, out=None, markers=None, seed=0,
                  score=None, grid=24, size=1024, emit=print) -> int:
    started = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    table = truth.split(":", 1)[1] if truth and truth.startswith("uns:") else "gates"
    if score:
        rows = score_session(score, truth_table=table)
        title = f"Gating session {score} against uns[{table!r}]"
    elif synthetic:
        rows = run_synthetic(synthetic, agent, arms=arms, seed=seed, markers=markers,
                             grid=grid, size=size)
        title = f"Synthetic gating benchmark ({agent} agent)"
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
                            markers=markers)
        title = f"Gating benchmark against uns[{table!r}] ({agent} agent)"
    summary = summarise(rows)
    if out is None:
        from plexora import paths

        out = paths.agent_root() / "bench" / started
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"summary": summary, "rows": rows},
                                                 indent=1, default=str), encoding="utf-8")
    (out / "summary.md").write_text(to_markdown(summary, rows, title=title), encoding="utf-8")
    emit(to_markdown(summary, [], title=title).split("## Per marker")[0].rstrip())
    emit(f"\nWritten: {out / 'results.json'}\n         {out / 'summary.md'}")
    return 0
