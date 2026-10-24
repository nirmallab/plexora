"""Evaluation against labels: the model (held-out CV predictions and/or an
ONNX checkpoint) vs Plexora's heuristics, on the materialised examples.

Runs in the plexora env (onnxruntime, no torch) and reads no images: every
input is in the examples folder. Writes evaluation.json and evaluation.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from biological_autocontrast import augment, examples, features as feat, metrics

BASELINES = ("percentile", "gmm", "fixed")


def _score(pmin, pmax, tmin, tmax):
    e = metrics.errors(pmin, pmax, tmin, tmax)
    return e["min"] + 2.0 * e["max"]


def _onnx_predict(model, items, meta):
    out = []
    for start in range(0, len(items), 16):
        chunk = items[start:start + 16]
        batch = feat.pack(chunk, model.n_patches, model.patch_size)
        pred = model.predict(batch)
        for i in range(len(chunk)):
            out.append({k: float(pred[k][i]) for k in pred})
    return out


def _items(meta, arrays):
    items = []
    for record in meta["examples"]:
        a = arrays[record["id"]]
        items.append({"patches": a["patches"], "categories": a["categories"], "pixels": a["pixels"],
                      "hist": a["hist"], "quant": a["quant"], "dom": a["dom"],
                      "dom_mask": a["dom_mask"]})
    return items


def _method_table(records, preds):
    """preds: {method: [(min, max) or None per record]} -> summaries and win rates."""
    tmin = np.array([r["target_min_norm"] for r in records])
    tmax = np.array([r["target_max_norm"] for r in records])
    keep = np.array([not r["abstain_target"] for r in records])
    out = {}
    for method, windows in preds.items():
        have = keep & np.array([w is not None for w in windows])
        if not have.any():
            continue
        pmin = np.array([w[0] if w else np.nan for w in windows])
        pmax = np.array([w[1] if w else np.nan for w in windows])
        entry = metrics.summary(pmin[have], pmax[have], tmin[have], tmax[have])
        entry["coverage"] = float(have.sum() / max(1, keep.sum()))
        out[method] = entry
    # head-to-head: share of examples where the method beats each baseline
    for method, windows in preds.items():
        if method in BASELINES or method not in out:
            continue
        wins = {}
        for base in BASELINES:
            bw = preds.get(base)
            if not bw:
                continue
            both = [i for i in range(len(records))
                    if keep[i] and windows[i] is not None and bw[i] is not None]
            if not both:
                continue
            a = _score([windows[i][0] for i in both], [windows[i][1] for i in both], tmin[both], tmax[both])
            b = _score([bw[i][0] for i in both], [bw[i][1] for i in both], tmin[both], tmax[both])
            wins[base] = {"win_rate": float((a < b).mean()), "n": len(both)}
        out[method]["vs_baselines"] = wins
    return out


def _subgroups(records, preds, key):
    groups = {}
    for i, r in enumerate(records):
        groups.setdefault(str(r.get(key)), []).append(i)
    return {g: _method_table([records[i] for i in idx],
                             {m: [w[i] for i in idx] for m, w in preds.items()})
            for g, idx in groups.items()}


def _cross_bit_depth(model, items, meta):
    """Re-quantise each example to 8/12/14 bits; the canonical prediction should not move."""
    base = _onnx_predict(model, items, meta)
    out = {}
    for bits in (8, 12, 14):
        changed = []
        for item in items:
            q = dict(item)
            q["patches"] = augment.quantise(item["patches"], bits).astype(np.float32)
            q["pixels"] = augment.quantise(item["pixels"], bits).astype(np.float32)
            q["dom"] = augment.requantised_dom(item["dom"], bits)
            changed.append(augment.refresh(q))
        pred = _onnx_predict(model, changed, meta)
        e_max = metrics.log_err([p["max_norm"] for p in pred], [p["max_norm"] for p in base])
        e_min = metrics.log_err([p["min_norm"] for p in pred], [p["min_norm"] for p in base])
        out[f"{bits}bit"] = {"max_median_log_shift": float(np.median(e_max)),
                             "min_median_log_shift": float(np.median(e_min)),
                             "within_tolerance": float(((e_max < metrics.TOL_MAX) &
                                                        (e_min < metrics.TOL_MIN)).mean())}
    return out


def _stress(model):
    """Degenerate inputs must still give finite, ordered windows."""
    n, s = model.n_patches, model.patch_size
    cases = {"zeros": 0.0, "saturated": 1.0, "dim": 0.002}
    out = {}
    for name, level in cases.items():
        pixels = np.full(4096, level, np.float32)
        item = {"patches": np.full((n, s, s), level, np.float32),
                "categories": np.zeros(n, np.int64), "pixels": pixels,
                "dom": np.zeros(feat.N_DOM, np.float32), "dom_mask": np.ones(feat.N_DOM, np.float32)}
        item = augment.refresh(item)
        pred = model.predict(feat.pack([item], n, s))
        lo, hi = float(pred["min_norm"][0]), float(pred["max_norm"][0])
        out[name] = {"min_norm": lo, "max_norm": hi, "abstain": float(pred["abstain"][0]),
                     "ok": bool(np.isfinite(lo) and np.isfinite(hi) and 0 <= lo < hi)}
    empty = {"patches": np.zeros((0, s, s), np.float32), "categories": np.zeros(0, np.int64),
             "pixels": np.full(4096, 0.01, np.float32), "dom": np.zeros(feat.N_DOM, np.float32),
             "dom_mask": np.ones(feat.N_DOM, np.float32)}
    pred = model.predict(feat.pack([augment.refresh(empty)], n, s))
    out["no_patches"] = {"ok": bool(0 <= pred["min_norm"][0] < pred["max_norm"][0])}
    return out


def run(examples_dir=None, checkpoint=None, out=None, *, cv_dir=None) -> dict:
    meta, arrays = examples.load(examples_dir)
    records = meta["examples"]
    preds = {b: [r["baselines_norm"].get(b) for r in records] for b in BASELINES}
    report = {"n_examples": len(records), "specimens": sorted({r["specimen"] for r in records}),
              "tolerance": {"max_ratio": float(np.exp(metrics.TOL_MAX)),
                            "min_ratio": float(np.exp(metrics.TOL_MIN)), "floor": metrics.FLOOR}}
    confidence = {}
    if cv_dir:
        cv_dir = Path(cv_dir)
        rows = {r["id"]: r for r in json.loads((cv_dir / "cv_predictions.json").read_text())}
        preds["model_cv"] = [[rows[r["id"]]["pred_min_norm"], rows[r["id"]]["pred_max_norm"]]
                             if r["id"] in rows else None for r in records]
        confidence["model_cv"] = [rows[r["id"]]["confidence"] if r["id"] in rows else None for r in records]
        run_metrics = cv_dir / "metrics.json"
        if run_metrics.exists():
            m = json.loads(run_metrics.read_text())
            report["run"] = {k: m.get(k) for k in ("valid", "reason", "holdout", "checkpoint_id",
                                                    "model_version", "params", "n_examples")}
    model = None
    if checkpoint:
        from biological_autocontrast.model import ContrastModel

        model = ContrastModel(checkpoint)
        items = _items(meta, arrays)
        pred = _onnx_predict(model, items, meta)
        preds["model_checkpoint"] = [[p["min_norm"], p["max_norm"]] for p in pred]
        confidence["model_checkpoint"] = [p["confidence"] for p in pred]
        report["checkpoint"] = {"path": str(checkpoint), "checkpoint_id": model.checkpoint_id,
                                "note": "scored on examples it may have been trained on; "
                                        "model_cv is the held-out number"}
        abst = np.array([p["abstain"] >= 0.5 for p in pred])
        truth = np.array([bool(r["abstain_target"]) for r in records])
        if truth.any():
            report["abstain"] = {"precision": float((abst & truth).sum() / max(1, abst.sum())),
                                 "recall": float((abst & truth).sum() / truth.sum())}
        else:
            report["abstain"] = {"note": "no no_signal labels yet; abstain is untested on real data",
                                 "abstained_on_labelled": int(abst.sum())}
        report["cross_bit_depth"] = _cross_bit_depth(model, items, meta)
        report["stress"] = _stress(model)
    report["methods"] = _method_table(records, preds)
    for method, conf in confidence.items():
        windows = preds[method]
        idx = [i for i, r in enumerate(records)
               if not r["abstain_target"] and windows[i] is not None and conf[i] is not None]
        if idx:
            ok = metrics.within([windows[i][0] for i in idx], [windows[i][1] for i in idx],
                                [records[i]["target_min_norm"] for i in idx],
                                [records[i]["target_max_norm"] for i in idx])
            report["methods"][method]["confidence_ece"] = metrics.ece([conf[i] for i in idx], ok)
    report["by_role"] = _subgroups([dict(r, role="nuclear" if r["nuclear"] else "marker") for r in records],
                                   preds, "role")
    report["by_bits_source"] = _subgroups(records, preds, "bits_source")
    report["per_example"] = [
        {"channel": r["channel_name"], "specimen": r["specimen"],
         "label": [round(r["target_min_norm"], 5), round(r["target_max_norm"], 5)],
         **{m: ([round(w[i][0], 5), round(w[i][1], 5)] if w[i] else None) for m, w in preds.items()}}
        for i, r in enumerate(records)]
    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "evaluation.json").write_text(json.dumps(report, indent=1))
    (out / "evaluation.md").write_text(markdown(report))
    return {k: report[k] for k in ("n_examples", "methods") if k in report} | {
        "written": str(out / "evaluation.md")}


def markdown(report) -> str:
    lines = ["# Auto-contrast evaluation", ""]
    run = report.get("run")
    if run:
        lines.append(f"Run `{run.get('model_version')}` ({run.get('checkpoint_id')}), held out by "
                     f"**{run.get('holdout')}** · valid: **{run.get('valid')}**"
                     + (f" — {run.get('reason')}" if run.get("reason") else ""))
        lines.append("")
    tol = report["tolerance"]
    lines += [f"{report['n_examples']} labelled channels from {len(report['specimens'])} specimen(s). "
              f"A window is *within tolerance* when its white point is within ×{tol['max_ratio']:.2f} "
              f"and its black point within ×{tol['min_ratio']:.2f} of the label (floor {tol['floor']} of full scale).",
              "", "| method | n | within tol. | white median err | black median err | wins vs percentile | vs gmm | vs fixed | ECE |",
              "|---|---|---|---|---|---|---|---|---|"]
    for name, m in report["methods"].items():
        vs = m.get("vs_baselines", {})
        def w(b):
            return f"{vs[b]['win_rate']:.0%}" if b in vs else "–"
        lines.append(f"| {name} | {m['n']} | {m['within_tolerance']:.0%} | ×{np.exp(m['max_median_log_err']):.2f} | "
                     f"×{np.exp(m['min_median_log_err']):.2f} | {w('percentile')} | {w('gmm')} | {w('fixed')} | "
                     f"{m['confidence_ece']:.2f} |" if m.get("confidence_ece") is not None else
                     f"| {name} | {m['n']} | {m['within_tolerance']:.0%} | ×{np.exp(m['max_median_log_err']):.2f} | "
                     f"×{np.exp(m['min_median_log_err']):.2f} | {w('percentile')} | {w('gmm')} | {w('fixed')} | – |")
    if "cross_bit_depth" in report:
        lines += ["", "**Cross-bit-depth** (same example re-quantised; median shift of the canonical white point):", ""]
        for k, v in report["cross_bit_depth"].items():
            lines.append(f"- {k}: ×{np.exp(v['max_median_log_shift']):.3f}, {v['within_tolerance']:.0%} within tolerance")
    if "stress" in report:
        lines += ["", "**Stress inputs**: " + ", ".join(f"{k} {'ok' if v['ok'] else 'FAILED'}"
                                                      for k, v in report["stress"].items())]
    if "abstain" in report:
        lines += ["", f"**Abstain**: {json.dumps(report['abstain'])}"]
    return "\n".join(lines) + "\n"
